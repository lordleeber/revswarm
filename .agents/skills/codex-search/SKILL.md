---
name: codex-search
description: 使用 Codex 自己的網頁搜尋、MOPS 與 revswarm DB，逐筆獨立核對月營收公告日期與數值；當使用者說「用 Codex search 驗證」「不要用 Gemini，自己查」或要跑 codex-search loop 時使用。
---

# 用 Codex 搜尋逐筆驗證（codex-search）

這個 skill 一次只處理一筆 revswarm 任務，完全不呼叫 Gemini API，也不執行
`worker.gemini_worker --review-one`。它用 Codex 的網頁搜尋取得獨立證據，再由 server
回報結果；`verified='codex'` 代表這次人工獨立查證，不代表 MOPS 官方章。

## 1. 取一筆任務

只在目前專案主工作目錄 `/home/poyi/GitHubLL/revswarm` 操作，不建立 worktree。
每輪先檢查 `.codex_search_pending.json`：存在就只處理其中那一筆，不租新任務。
這個檔案是本 skill 的交接檔；不要讀取、刪除或覆寫另一條流程的
`.gemini_review_pending.json`。

若沒有本 skill 的 pending，向 server 的 `gemini` 佇列租一筆，但只租任務，不呼叫
Gemini：

```bash
python3 - <<'PY'
import json, os, urllib.request

server = "http://127.0.0.1:8000"
worker = "codex-search"
url = f"{server}/lease?n=1&worker={worker}&engine=gemini"
headers = {"Authorization": "Bearer " + os.environ.get("REVSWARM_TOKEN", "")}
req = urllib.request.Request(url, method="POST", headers=headers)
with urllib.request.urlopen(req, timeout=60) as r:
    body = json.loads(r.read())
print(json.dumps(body, ensure_ascii=False, indent=2))
PY
```

把租到的 `task` 寫入 `.codex_search_pending.json`，並保留 `worker_id`；若 tasks 為空，
回報「codex 佇列空」並停止 loop。401、5xx、連線失敗或權限錯誤是 fatal，停止並回報，
不要直接改 DB。若專案已有明確指定的 `stock_id/roc_year/roc_month`，可直接查那一筆，
但仍一次只做一筆。

## 2. 逐筆查證

先用 Python sqlite3 唯讀查 `revswarm.db`（不是 `data/revswarm.db`），讀目標列、同檔前後
月份、DB 中其他引擎的 `raw_title/revenue/yoy/announce_date`，再用
`mops.mops_validate.fetch_company(stock_id, [roc_year], sleep=0.4)` 查 MOPS。MOPS 沒資料
不是失敗理由；它只是最硬的對照，不能自行補出日期或數值。

用網頁搜尋針對這一筆查：

- 股票代號、現名與歷史公司名 + ROC 年月／西元年月 +「營收」；
- MOPS、公司 IR／官方公告、Goodinfo、StockGo、中央社、鉅亨、MoneyDJ 等歷史頁面；
- 搜尋結果不可直接當全文證據。開啟來源，確認頁面自己的日期、公司／股號、營收單位、
  單月值、年增率及累計值；記下實際 URL。

approve 前必須能把「這一個月」與「公告日期」連在一起：

1. 至少一個官方／第一手來源直接支持日期與目標月份數值，或兩個獨立可靠來源互相核對；
2. 單月營收、年增率、累計營收（頁面有提供時）必須逐項相符，允許明確的四捨五入差異；
3. 若名稱不同，必須用同股號的歷史資料或官方改名資料證明，不可自行猜測；
4. MOPS 日期若與來源日期或 DB 已有日期衝突，停下並 reject；
5. 只靠鄰月量級相近、只看到公司名年月、或只看到一個沒有日期的數字，不足以 approve。

累計值可以用已核對的前幾個月加總；若有缺月，必須在 note 明確寫出是反推，且反推值
要落在該檔合理區間。看到數值量級、月份、公司或日期矛盾時，reject，不要用另一條
弱證據把矛盾掩蓋掉。不可使用分類器、正則批判斷、套版理由或複製上一筆 note。

## 3. 落地與稽核

專用稽核檔是 `data/codex-search-review.csv`，表頭固定為：

```text
stock_id,roc_year,roc_month,announce_date,verdict,note
```

每筆都要有針對該筆的 note，包含實際查到的來源、數值、日期與衝突處理。`verdict=codex`
表示 approve；`verdict=tbd` 表示看過但 reject／證據不足。這個檔案 append-only，重審時
追加新列，不修改舊列；不要寫入 `data/gemini-review-codex.csv`。

approve 的安全順序：

1. 用 server `POST /result` 回報租到的 task：`status=success`、查到的日期、source、
   完整標題與 URL；不要直接改 SQLite。確認 server 接受且 DB 列為 success。
2. 追加一列 `verdict=codex` 到 `data/codex-search-review.csv`，確認該筆已存在。
3. 執行：

   ```bash
   python3 -m stamp_verified --from codex --review data/codex-search-review.csv --server http://127.0.0.1:8000
   ```

4. 查 DB 確認同一筆 `verified='codex'`。若 server 已回報 success 但 CSV 尚未寫入，
   不要重報 `/result`；補上原 note 後再 stamp。

reject 時，先用 server `POST /result` 回報該租務 `status=failed`，再追加一列
`verdict=tbd` 的專屬 note；不要 stamp。若 reject 的任務不是本 skill 租到的 dispatched
列，不可擅自降級 success，應只回報判斷並等待使用者授權重排。

任何 `/result`、`/verify` 或 stamp 的 API／權限 fatal 都停止 loop；不可繞過 server 直接
寫 DB。落地後刪除 `.codex_search_pending.json`，確認它不存在再開始下一輪。

## 4. 每輪回報

回報股號、公司名、ROC 月份、`approve/reject`、具體證據與理由、Codex 搜尋次數（沒有
Gemini 成本；若工具未提供費用就寫「成本不適用」）、以及累計 codex-search 審核筆數。
retry 不應作為一般 verdict：若搜尋工具或 API 沒有完成查詢，保留 pending、回報阻塞，
不要把沒有證據的任務標成 reject 或 approve。

收尾確認：只動到本 skill 的稽核檔與暫存交接檔的正常建立／刪除；`.gemini_review_pending.json`
與 `data/gemini-review-codex.csv` 不得被本 skill 改動。

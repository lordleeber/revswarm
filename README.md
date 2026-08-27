# revswarm

分散式爬蟲，收集台股「每月營收公布日期」，供「營收公布日 vs 股價反應」事件研究使用。

- 範圍：1848 檔（上市+上櫃+興櫃）× 民國 109/1 ~ 115/1（73 個月）＝ **134,904** 個 (股票×月) 任務。
- 精度：公布日精確到「日」。
- 資料源：**Yahoo 台灣搜尋**（`tw.search.yahoo.com`）。公布日只存在於新聞文章裡，
  結構化營收頁（MOPS / Yahoo / MoneyDJ）都只有年月+金額、沒有公布日。
- 為什麼要分散式：單一 IP 對 Yahoo 高頻查詢會被封。多機 = 多 IP，才能把 13.5 萬筆爬完。

> 資料源探索的完整結論與所有踩過的雷（為何不用 MOPS、查詢字串禁忌、窗過濾、名稱撞庫…）
> 見下面的「[踩過的雷](#踩過的雷資料源探索結論)」。**要改任何一支 worker 之前先讀那一節**
> ——裡面每一條都是實測撞出來的，重走一次很貴。

## 架構

`server`（一台，持有全部任務佇列）＋ 多台 `worker`（任意機器/IP，向 server 租任務、爬、回報）。

```
  worker@A ─┐
  worker@B ─┼──HTTP──►  server (SQLite 工作佇列)
  worker@C ─┘           /lease  /result  /stats
```

狀態機：`undone → dispatched → (success | failed)`
- `dispatched` 超過 **10 分鐘**沒回覆 → 派工時「惰性回收」自動視為 `undone`（免背景執行緒）。
- `failed`（worker 民國年+西元年**兩種都試過仍找不到**）或 `rejected`（server 驗窗/撞名
  沒過）→ **自動升級到下一棒引擎**（`yahoo→google→gemini`）：退回 `undone`、`engine`
  換下一個，不用再手動 `/admin/requeue-failed`。已經是 `gemini`、沒有下一棒可換，
  才真的落 `failed`（見 `server.py` 的 `NEXT_ENGINE`）。
- `rate_limited` ≠ `failed`：立刻放回 `undone`（engine 不變），worker 退避重試，不計失敗、不升級。
- 升級與落終點都會 `fail_count+1`（`failed` 與 `rejected` 兩條路一致）——那是「這筆被爬過幾次」
  的歷史，少記一半就會讓一路被 reject 到底的列看起來像從沒派過。

三道防污染（保證 0 髒資料）：
1. worker 端「窗過濾」：只信落在**營收次月 1~15 號**的日期。
2. worker 端「精確名稱錨點」：避免「統一」吃到「統一超」、「台塑」吃到「台塑化」。
3. **server 端再驗一次窗**：`/result` 收到的 success 日期不在窗內就退回重做，不信 worker。
   同一道還會擋兩種「窗內但講錯事」的：**撞名**（標題講的是名字更長的另一家公司）
   與**根本不是營收公告**（標題前段就寫明是董監持股明細／股東常會／除權息一覽表…，
   且全文沒有任何營收字樣——實測 `4173 久裕 111/10` 抓到 goodinfo 董監持股頁）。
   兩者都跟 `failed` 同等看待：`rejected` → 自動升級到下一棒引擎。

## 檔案

| 檔案 | 說明 |
|---|---|
| `build_stocks.py` | 從證交所 ISIN 建 `stocks.csv`（code→中文簡稱→market）|
| `data/active_stocks.txt` | 1848 檔股票代號（一行一個）|
| `data/name_overrides.csv` | 已下市/ISIN 查無者的人工補名（目前 3426 台興、6806 森崴能源）|
| `data/date_overrides.csv` | 人工確認的**遲交**公布日（窗外個案，目前 1 筆；見下）|
| `data/gemini_review.csv` | 逐筆審核 `gemini_worker` 證據的判斷（含**模型讀過的網域**，DB 沒地方存；見「gemini_worker → 審核模式」）|
| `apply_date_overrides.py` | 把上面那份 CSV 套進 DB（冪等；DB 不進版控，重建後要重跑）|
| `stocks.csv` | 產生的對照表（進版控）|
| `init_tasks.py` | 展開 1848×73 成 tasks 寫入 SQLite（冪等）|
| `server.py` | 工作佇列 server（標準庫 http.server + sqlite3，零依賴）|
| `worker/yahoo_worker.py` | 爬蟲 worker（curl --http1.1、民國+西元雙查、窗過濾、退避）|
| `worker/google_worker.py` | 補搜 worker：Playwright 驅動系統 Chrome 查 Google，撿 Yahoo 救不回的 `failed`（見下）|
| `worker/gemini_worker.py` | **實驗性**補搜 worker：Gemini API 的 Grounding with Google Search，零依賴、免圖形環境，但**要錢**且日期出自模型合成文字（見下）|
| `mops/gemini_benchmark.py` | 拿 MOPS 官方申報日當基準，量測 `gemini_worker` 的日期品質（放量前的對照實驗，見下）|
| `requirements.txt` | **只服務 `google_worker.py`** 的相依（playwright）；核心零依賴，不必安裝|
| `revlib.py` | 共用核心：期望窗 + Yahoo 頁解析（server/worker 都用同一套窗）|
| `goodinfo/build_stock_dates.py` | 從官方來源建 `stock_dates.db`（上市/上櫃日現況快照）|
| `goodinfo/goodinfo_worker.py` | 補 goodinfo 的四板日期（上市/上櫃/興櫃/公開發行），拿到**已畢業公司的早年日期**，官方現況快照沒有 |
| `mark_prelisting.py` | 用首次公開日把「公司當時還沒公開發行」的任務標成 `prelisting`；`--demote-success` 連上市前的假 success 一起降級（見「資料品質」）|
| `mops/mops_validate.py` | 用 MOPS 官方申報日**交叉驗證** Yahoo 抓到的公布日（見下）|
| `mops/stamp_verified.py` | 把「兩個獨立來源同意」的列蓋上 `verified` 欄（見「出處與驗證」）|
| `backfill_revenue.py` | 從既有 `raw_title` 回填 `revenue`/`yoy`（不重爬，見下）|

**核心零第三方依賴**，只需 `python3`（3.8+）與 `curl`。worker 機器 `git clone` 本 repo 就能跑，不必額外安裝任何東西。
唯一的例外是 `google_worker.py` 需要 playwright（`requirements.txt`），只在要跑 Google 補搜的機器上裝。

> **子目錄下的程式一律從 repo 根目錄以模組形式執行**，`worker/`、`mops/`、`goodinfo/` 都一樣：
> `python3 -m worker.yahoo_worker`、`python3 -m mops.mops_validate`。
> 直接跑 `python3 worker/yahoo_worker.py` 會 `ModuleNotFoundError: No module named 'revlib'`
> ——`sys.path[0]` 是「腳本所在目錄」而不是 CWD，根目錄沒進 `sys.path` 就找不到 `revlib`。

## Quick start

```bash
# 1. 建 code→name 對照表（需連 isin.twse.com.tw）
python3 build_stocks.py                      # → stocks.csv (1848/1848)

# 2. 展開任務到 SQLite（冪等，可重跑補新股）
python3 init_tasks.py                         # → revswarm.db (134,904 undone)

# 3. 啟動 server（token 寫在 .env，server 自動讀，不用手打）
echo "REVSWARM_TOKEN=$(head -c16 /dev/urandom | base64 | tr -d '\n')" > .env
python3 server.py --host 0.0.0.0 --port 8000  # 自動讀 .env 的 REVSWARM_TOKEN

# 4. 在「每一台」worker 機器上跑（git clone 本 repo，在 repo 根目錄執行）
echo "REVSWARM_TOKEN=<與 server .env 同一組>" > .env
python3 -m worker.yahoo_worker --server http://<SERVER_IP>:8000

# 5. 隨時看進度
source .env
curl -s -H "Authorization: Bearer $REVSWARM_TOKEN" http://<SERVER_IP>:8000/stats | python3 -m json.tool
```

> 補搜 `failed` 的另外兩支 worker 各有自己的前置與跑法：
> [`google_worker`](#google_worker補搜-failedplaywright-系統-chrome)（Playwright，免費）與
> [`gemini_worker`](#gemini_worker用-gemini-grounding-補搜實驗性會花錢)（Vertex，**要錢、實驗性**）。

## API

所有 endpoint 需 header `Authorization: Bearer <token>`（`/healthz` 除外）。

| method | path | 說明 |
|---|---|---|
| POST | `/lease?n=30&worker=<id>&engine=yahoo` | 原子租一批任務（`n` 上限 200）。**越舊營收月越優先**（跨所有股票齊步：全部 109/1 → 109/2 → …），並順便惰性回收逾時租約。`engine` 分流佇列（預設 `yahoo`），三支 worker 不會搶同一批；只收 `yahoo`\|`google`\|`gemini`，其餘回 400（打錯字若靜默放行會讓 worker 一直看到空佇列）|
| POST | `/result` | 批次回報 `{worker, results:[{id,status,date?,source?,title?,url?}]}`；status ∈ success/failed/rate_limited。`url` 是這個日期的出處，server 端會再驗一次格式（非 http/https 一律存 NULL）。`failed` 或 server 驗窗/撞名沒過的 `rejected` 會**自動升級到下一棒引擎**（見上方狀態機）。回應的 `applied` 裡，`failed`/`rejected`/`success`/`rate_limited` 數的是**回報進來的是什麼**，`escalated`/`terminal` 數的是**造成了什麼**（換下一棒 / 真的落 `state='failed'`）——自動升級後兩者會差很多，別把 `failed=N` 讀成「N 筆沒救了」|
| POST | `/verify` | `{by:"mops"\|"gemini", items:[{stock_id,roc_year,roc_month,date}]}`；**只有送上來的 date 與 DB 的 `announce_date` 一致才蓋 `verified`**，不一致回報成 `mismatch` 但不改資料。`by` 走白名單，其餘回 400（見「出處與驗證」）|
| GET | `/stats` | 各 state 計數、進度%、近 5 分吞吐、ETA、成功率，以及 `queue_by_engine`＝各 engine 還有多少未完成（JSON）|
| GET | `/status` | 人類可讀的**狀態頁**（HTML 儀表板，自動更新），含「待做佇列（依 engine）」——升級到 `google`/`gemini` 的那批在 `by_state` 裡只是普通的 `undone`，沒開對應 worker 就會一直卡著，這一列直說。瀏覽器可用 `?token=<token>`；`?refresh=<秒>` 調更新頻率 |
| GET | `/healthz` | 存活探針（免 token）|
| POST | `/admin/requeue-failed` | 把所有 `failed` 重開成 `undone`。**新產生的 `failed`/`rejected` 現在會自動升級到下一棒引擎，平常不需要呼叫這支**；留著是為了一次性回填「自動升級上線前」就已經卡在 `failed` 的歷史列（改西元年常能救回）。加 `?engine=google`（或 `gemini`）則連 engine 一併轉過去，交給該 worker 專門處理，不影響其餘 yahoo 佇列；未知 engine 回 400（否則 3 萬筆會被丟進沒有 worker 會租的佇列）|

## worker 調參

```bash
python3 -m worker.yahoo_worker --server URL \
  --batch 30            # 每次租幾筆
  --delay 3 --jitter 2  # 每筆任務間 3~5s（禮貌 + 降低被封）
  --per-query-sleep 1.5 # 同任務內兩次查詢間隔
  --rl-threshold 5      # 連續幾筆 rate_limited 判定本機 IP 被擋
  --block-sleep 600     # 判定被擋後長睡秒數
  --once                # 只跑一批就結束（測試用）
```

worker 偵測到連續 `rate_limited` 會指數退避，超過門檻就判定「本機 IP 被擋」，
把該批剩餘任務以 rate_limited 放回、長睡一陣子。**別讓單一 worker 燒掉自己的 IP。**

### 換 IP 出口（選配：`--proxy` / `WORKER_PROXY`）

不想多開機器、但想換一顆對外 IP 時，可讓 worker **爬 Yahoo 的 `curl` 走 SOCKS proxy**。
最省的做法是對一台雲端 VM 開 SSH 動態轉發（VM 端零安裝，只用它自帶的 `sshd`）：

```bash
# 本機開一條 SOCKS5 通道到 VM（127.0.0.1:1080 從 VM 的 IP 出去）
ssh -D 1080 -N -f -o ExitOnForwardFailure=yes <user>@<VM_外部IP>
#   或用 gcloud： gcloud compute ssh <VM> --zone <zone> -- -D 1080 -N -f

# worker 的 curl 走它（socks5h 的 h＝DNS 也在 VM 端解，整條請求都從 VM 出）
WORKER_PROXY=socks5h://127.0.0.1:1080 python3 -m worker.yahoo_worker --server http://<SERVER>:8000
#   等同： python3 -m worker.yahoo_worker --proxy socks5h://127.0.0.1:1080 --server ...
```

- **只影響爬 Yahoo 的 `curl`**；worker↔server 的租任務/回報（`urllib`）不受影響、仍走原路
  （所以 server 只在 tailnet 也 OK）。不設時行為完全不變。
- 一台 VM ＝ 一顆 IP；退避邏輯照舊留著（換 IP 是分攤，不是拿來加速轟炸）。
- 詳解見 `docs/worker-proxy.html`。

## 踩過的雷（資料源探索結論）

⚠️ 這一節每一條都是實測撞出來的，不是設計偏好。改 worker 之前先讀。

### 為什麼不用 MOPS 當主資料源

- MOPS「歷史重大訊息」`t05st01` 只涵蓋**自願**把月營收發成重大訊息的公司。抽樣全 1848 檔
  只有約 **49 家**有資料（台積電/聯發科/台塑/中鋼/台塑化…）。這批有精確到「時、分」的
  官方申報時間，是最高信度基準（見「交叉驗證」），但涵蓋太少，撐不起 13.5 萬筆。
- MOPS 的結構化營收報表（`t21sc04_ifrs` 彙總 / `t05st10_ifrs` 逐檔）、Yahoo 與 MoneyDJ 的
  結構化營收頁：**都只有年月+金額，沒有公布日期**。
- → **公布日期只存在於「新聞文章」裡**，所以只能用搜尋引擎撈。

### 為什麼是 Yahoo 台灣搜尋

搜尋「`{公司名} {年}年{月}月`」會把該月營收的新聞帶出來，文章日期就是公布日。
日期準確度已驗證 —— MoneyDJ / Yahoo 文章日期 **等於** MOPS 官方申報日：

> 台積電 2022 年 1 月營收：MOPS 申報 `2022-02-10 13:41`，MoneyDJ 文章 `2022-02-10 13:56`。

而且涵蓋所有公司，連 MOPS 重大訊息查不到的（穩懋等）都有。

### 查詢字串的兩個關鍵教訓

**(a) 絕不在查詢後面加「營收」兩個字。**

```
✓ "台積電 109年8月"          對
✗ "台積電 109年8月營收"       recall 掉一半
```

加了會讓搜尋引擎去比對「最近的營收新聞」→ 回傳近期（錯年份）文章。
`google_worker` 也一樣，而且**額外**不能加 `moneydj`（實測會把 CMoney/中央社/Yahoo
來源的命中排擠掉，任一 90%→80%，純損失）。

**(b) 民國年與西元年兩種都要查，取聯集** —— 它們釣到不同來源，互補是結構性的：

| 查詢 | 釣到 | 因為 |
|---|---|---|
| 民國年 `穩懋 109年1月` | MoneyDJ | 標題用民國年：「穩懋 109年1月營收…」|
| 西元年 `台塑 2020年6月` | Yahoo股市【公告】| 標題用西元年：「【公告】台塑 2020年6月合併營收…」|

⚠️ **兩支 worker 的順序相反**：Yahoo 民國年優先（`q_roc`→`q_ad`），
Google 西元年優先（`g_ad`→`g_roc`，因為 Google 直接忽略民國年 token，
頁面會顯示「缺少字詞：110」）。

### 窗過濾 —— 把髒資料清成 0 的關鍵

月營收依規定次月 10 日前申報，遇假日順延。所以正確公布日**一定**落在
「營收月的次月 1~15 號」：

- `roc_month <= 11` → 西元 `roc_year+1911` 年、`roc_month+1` 月、1~15 日
- `roc_month == 12` → 西元 `roc_year+1912` 年、1 月、1~15 日

（11、13 號常見 = 週末順延；15 號 = 1 月營收遇春節順延。>15 號一律丟棄。）

不在窗內的日期一律當作沒抓到。實作在 `revlib.expected_window` / `in_window`。

> ⚠️ **server 端要用同一個窗再驗一次** worker 回報的日期，不要全信 worker
> （`revlib.validate_date`，`/result` 熱路徑）。這是三道防污染的第三道。

> ⚠️ 窗只管日期落點，管不到「這篇在講什麼」。`revlib.is_non_revenue_title` 補這個縫：
> 標題**前段**就寫明是董監持股明細／股東常會／除權息一覽表…、且**全文**沒有任何營收
> 字樣時，不收。兩個更寬的寫法都拿整個 DB 量過、都會殺好資料，別改回去：
> 「標題必須含營收字樣」會擋掉 549 筆 MOPS 已獨立驗證正確的列（`raw_title` 被截斷成
> 「台塑2020年1月合併<p」這種）；「整段比對黑名單」命中的絕大多數是「開頭是正確的
> 中央社公告、尾巴才被 SERP 拼上董監持股」。窄規則掃 109,452 筆只命中 2 筆。

### 解析陷阱：公司名是子字串會撞

「統一」vs「統一超」、「台塑」vs「台塑化」。必須**精確名稱比對**：鎖定標題形如
「`{名稱} {年}年{月}月`」或「`【公告】{名稱} {年}年{月}月`」，且名稱後緊接空白或數字，
不可被更長的名稱吃掉。實作在 `revlib._anchor_offsets`。

> ⚠️ 錨點**必須連年份一起鎖**。早期版本只鎖月不鎖年（`\d{2,4}年`），於是搜尋結果頁上
> 「同月份、別年份」的近期文章會被當成錨點 —— 例：任務 111/6、112/6、113/6 三筆都錨到
> 同一篇「宏碁智新 115年6月」。窗過濾擋不掉這種「錨錯地方、卻剛好挑到一個窗內日期」的
> 靜默錯配（實測全庫 705 筆、佔 0.57%，見「資料品質」）。

### rate-limit / 封鎖（唯一真正的操作瓶頸，也是為什麼要分散式）

- ⚠️ **curl 打 Yahoo 一定要 `--http1.1`。** HTTP/2 在某些環境會 SSL unexpected eof、
  回 HTTP 000；加了就正常 200。
- 單一 IP 持續高頻查詢 → 連線被 reset（SSL EOF / size=0 / 非 200）。實測狂打一天後
  單 IP 幾乎全被擋 → **把 worker 分散到多台機器 / 多個 IP，這就是 revswarm 的核心**。
- rate-limited 的訊號：curl 失敗 / 非 200 / **頁面 < 2000 bytes**（`MIN_PAGE_BYTES`）。
- ⚠️ **這種絕不可回報 `failed`**，要退避重試。`failed` 只能是「頁面正常、兩種年份都試過、
  仍無窗內日期」。把「還沒查成功」寫成 `failed` 會靜靜污染研究資料 —— 這條戒律在三支
  worker 上各有一個對應的判準（Yahoo 看頁面大小、Google 看 `#search` 容器、
  Gemini 看 `webSearchQueries` 是否為空）。

### pilot 實測數據（10 檔 × 民國 109 年）

- 修正查詢（不加「營收」）+ 窗過濾：連得上的請求 **~78% 正確、0 髒**。
- 西元年補查再救回漏抓的約一半。
- 每次查詢純抓取 ~0.8~1.5s（不含退避）。單 IP 會被封是主要限制。

> ⚠️ 大量爬 Yahoo 可能違反其 ToS，也會招致更強封鎖 —— 務必節制、分散、加延遲。

## google_worker：補搜 failed（Playwright + 系統 Chrome）

`yahoo_worker.py` 爬 Yahoo 兩種年份都試過仍找不到窗內日期的 `failed` 任務，實測有一批
在 Google 搜尋能命中正確的月營收公告（抽測 10 筆真實 failed，命中 9 筆）。
`google_worker.py` 就是專門補搜這批的第二種 worker，用 **Playwright 驅動「系統安裝的
Chrome」**（headful + 持久設定檔）查 Google 搜尋。

為什麼是這條路（三條死路，別重走）：

| 走法 | 結果 |
|---|---|
| Custom Search JSON API | ✗ 已對新客戶關閉（2027-01 全面停用），一律 403 `This project does not have the access...`；換金鑰/換專案無效 |
| `curl` 爬 `www.google.com/search` | ✗ 每次都被轉去 `/httpservice/retry/enablejs`——Google 在核對 Client Hints/TLS 指紋，帶 Chrome UA 也偽裝不了，換 IP 無效 |
| Playwright + **真實 Chrome** | ✓ 通過指紋檢查，實測 40 次查詢 0 驗證碼（`headless=True` 會被導到 `/sorry/`，故預設 headful） |

前置（一次性）：
```bash
pip3 install --user -r requirements.txt   # 唯一一項 playwright；不必再跑 playwright install
google-chrome --version                   # 用系統的 /usr/bin/google-chrome（實測 146、150）
```
> ⚠️ **`requirements.txt` 只服務 `google_worker.py`。**
> playwright 是本 repo 唯一的第三方依賴，`server.py` / `yahoo_worker.py` / `revlib.py`
> 仍維持「純標準庫、零依賴」——**只跑 server 或 Yahoo 主線的機器不必安裝它**，
> `git clone` 下來就能跑。測試也不需要（playwright 是延後 import 的）。

另外需要**圖形環境**（headful 才不會被擋）：Linux 上是 `DISPLAY=:0`。無頭機器請改派有桌面的
機器跑（Windows 桌面本來就有圖形環境，見下面的 PowerShell 跑法）。
Chrome 設定檔存在 `~/.cache/revswarm-chrome`（`--profile` 可改），保留 cookie 以降低驗證碼。

跑法：先把 `failed` 轉去 `google` 佇列（否則 google_worker 租不到任何任務），
再啟動 google_worker（用法與 `yahoo_worker.py` 對稱）：
```bash
curl -X POST -H "Authorization: Bearer $REVSWARM_TOKEN" \
  "http://<SERVER>:8000/admin/requeue-failed?engine=google"

DISPLAY=:0 python3 -m worker.google_worker --server http://<SERVER>:8000 --once   # 先小量驗證
DISPLAY=:0 python3 -m worker.google_worker --server http://<SERVER>:8000         # 再放量（用 tmux）
```

**Windows 11 / PowerShell 跑法**（實測 Chrome 150、Python 3.13、venv 裝 playwright 1.62）：
```powershell
$env:DISPLAY = ":0"
.\.venv\Scripts\python.exe -m worker.google_worker --server http://<SERVER>:8000 --once
```
三個 Windows 專屬的坑，都在上面兩行裡解掉了：

1. **`$env:DISPLAY = ":0"` 一定要設，但它在 Windows 上不做任何事。** `main()` 只檢查這個
   環境變數存不存在，不設就 `sys.exit(1)`；而 `DISPLAY` 是 X11 概念，Chrome 在 Windows 上
   完全忽略它、走原生視窗。所以這行純粹是餵飽那道為 Linux 寫的守衛，餵飽之後行為是對的。
2. **PowerShell 沒有 bash 的行內前綴語法**——`DISPLAY=:0 python3 ...` 是語法錯誤，
   必須拆成獨立一行的 `$env:DISPLAY = ":0"`。
3. **用完整路徑呼叫 `.\.venv\Scripts\python.exe`，不要 activate。** Windows 客戶端的
   execution policy 預設 `Restricted`，`activate.ps1` 會被擋下（`running scripts is
   disabled on this system`）。指名執行檔跟 activate 效果相同（activate 只是改 PATH），
   還省掉動系統安全設定。

`channel="chrome"` 在 Windows 上會自己找到 `C:\Program Files\Google\Chrome\Application\chrome.exe`，
不必額外設定；設定檔則落在 `C:\Users\<你>\.cache\revswarm-chrome`（`~` 由 `expanduser` 展開）。

`engine=google` 佇列跟原本 `yahoo` 佇列完全分開派工，`yahoo_worker.py` 與 `google_worker.py`
可同時開著、不會互搶任務。參數意義與 `yahoo_worker.py` 相同，預設值不同：

```bash
--batch 10             # 一批必須在租約 TTL(600s)內回報完；最壞情況每筆約 49s（導覽逾時
                       #   20s + 查詢間隔 7s + 補查 2s + 延遲 20s），10 筆 ≈ 490s 仍有餘裕
                       #   （12 筆就逼近 590s：調高 delay/jitter 要同步調低 batch）
--delay 10 --jitter 10 # 每筆任務間 10~20s（放慢降低撞驗證碼的機率）
--per-query-sleep 6    # 同任務內兩次查詢間隔
--rl-threshold 3       # 連續幾筆「非驗證碼」的 rate_limited 才提早收批
                       #   （驗證碼一次就停，不套這個門檻——見下）
--block-sleep 1800     # 被擋後最多等多久（驗證碼解掉就立刻接續，不等滿）
--captcha-poll 5       # 等待期間每幾秒檢查驗證是否解除（只讀當前頁，不導覽）
--profile DIR          # Chrome 持久設定檔目錄
--headless             # ⚠️ 僅供除錯：實測會被導到 /sorry/
```

**查詢字串與 Yahoo 相反：Google 要「西元年優先」**（`4碼代號 名稱 2020年3月`），
民國年補第二。因為 Google 直接忽略民國年 token（頁面會顯示「缺少字詞：110」）。
兩者互補是結構性的——中央社【公告】標題寫西元年、MoneyDJ 標題寫民國年，
所以單獨命中 70%/50%、任一 90%。同樣**絕不加「營收」二字**，也**絕不加 `moneydj`**
（實測加了會把 CMoney/中央社/Yahoo 來源的命中排擠掉，任一 90%→80%，純損失）。
命中的 `source` 記成 `g_ad` / `g_roc`（Yahoo 是 `q_ad` / `q_roc`），
匯出資料看得出這筆日期是哪個引擎補到的。

驗證碼處理：偵測到 `/sorry/`、頁面出現「異常流量」等字樣、導覽失敗、或**頁面根本不是
搜尋結果頁**（沒有 `#search` 結果容器），一律當 `rate_limited` 放回佇列，
**絕不當 `failed`**——只有「頁面是正常 SERP、兩種年份都試過、仍無窗內日期」才是真的
`failed`，避免把「這次沒查到」誤記為「Google 也搜不到」。

> ⚠️ 為什麼判準是 `#search` 容器而不是頁面字數：**全新設定檔的第一次查詢會遇到
> Google 同意頁**，它字數很多、網址不含 `/sorry/`、也沒有任何攔阻字樣，用字數判準會被
>當成正常頁 → 找不到窗內日期 → 誤記成真 `failed`，靜靜污染研究資料。反過來，實測合法
> 但「幾乎沒結果」的 SERP 整頁可見文字只有 326 字，字數門檻設高又會誤判 `rate_limited`
> （而 lease 是最舊優先，被放回的任務下一批又排最前面，會永久重試）。同意頁與
> `enablejs` 轉址頁都沒有 `#search`，稀疏結果頁有——所以容器是對的維度。

**撞到需要人工處理的頁面（驗證頁／同意頁）就立刻停批**，不再取下一筆任務 —— 畫面
就停在那頁。之後每 `--captcha-poll`（預設 5s）秒只「讀」一次當前頁判斷是否已解除，
**不導覽**，所以你在上面輸入不會被打斷；**處理完的瞬間就自動接續**，不必等滿
`--block-sleep`。停批有兩個觸發點，寬嚴不同：

| 情境 | 何時停批 |
|---|---|
| `/sorry/`、或頁面出現「異常流量」等字樣 | **第一次就停**，連第二種年份都不打 |
| 其他被擋（沒有 `#search` 等） | 打完第二種年份後，**若畫面還壞著**才停 |

留那一次 fresh `goto` 是為了讓偶發的空白頁自己復原，不要一次抖動就收掉整批。

> ⚠️ 這裡踩過兩層坑，都是線上實測出來的：
> 1. 原本驗證碼也套 `--rl-threshold`（要連續 3 筆才停批），但期間每一筆的 `page.goto`
>    都會把正在解的驗證頁蓋掉，實際上**根本解不完**。
> 2. 改成「撞到就停」之後又發現**驗證頁不一定認得出是驗證頁**：網址不在 `/sorry/`、
>    文字也沒有攔阻字樣時，它只會被分類成「不是 SERP」。那時若照分類去決定怎麼等，
>    就會走到無條件長睡、**一次都不輪詢**，人解掉了照樣等滿 1800s（實測如此）。
>
> 所以「要不要等人」現在**不看被擋的分類，改問當前頁的實際狀態**：還停在壞頁就輪詢，
> 畫面正常才長睡。

`--rl-threshold` 仍然管「畫面正常但連續失敗」（例如導覽一直逾時）：超過門檻就提早收批。
那種情況沒有東西可解，所以刻意保留無條件長睡 `--block-sleep`，避免繼續燒自己的 IP。

### 驗證碼是怎麼被觸發的（實戰紀錄）

實測看到的 `/sorry/` 頁寫著 `IP 位址：59.120.155.101 ≠ 61.219.173.229` —— 這**不是流量太多**，
而是「session 的來源 IP 換掉了」：持久設定檔裡的 cookie 是在舊的動態 IP 上建立的，
ISP 重新配發 IP 後，Google 就把「舊 cookie + 新 IP」視為可疑。

- 解法一：在視窗裡把驗證碼解掉（cookie 會重新綁到當前 IP），worker 自動接續。
- 解法二：停掉 worker、`rm -rf ~/.cache/revswarm-chrome`（或換一個 `--profile`），
  清掉舊綁定。代價是失去同意 cookie，第一次查詢會遇到同意頁 —— 但同意頁會被正確判
  `rate_limited` 而不是 `failed`，所以清設定檔是安全的。
- 動態 IP 的機器會反覆遇到；固定出口 IP（例如走 SOCKS）可從根本消除這個觸發源。

## gemini_worker：用 Gemini grounding 補搜（實驗性、**會花錢**）

第三支 worker，走 `engine=gemini` 佇列。用 Gemini API 的 **Grounding with Google Search**
問「這檔股票這個月的營收是哪天公布的」，而不是自己爬 SERP。

> **這是實驗，不是主力。** `google_worker.py` 已經把 30,877 筆補搜完（24,196 success）。
> 這支存在的目的是回答一個問題：**同樣的題目，grounding 拿到的日期品質比直爬 SERP 好還是壞。**

> **接通這條路的完整經過（含所有死路）在下面的「[怎麼走到 Vertex 這條路的](#怎麼走到-vertex-這條路的2026-08-24-實測)」。要動這支 worker 之前先讀，可以省下重走一次的半天。**

| | google_worker | gemini_worker |
|---|---|---|
| 依賴 | playwright + 系統 Chrome + 圖形環境 | **無**（純 urllib 打 REST）|
| 人工介入 | 會撞驗證碼，要人解 | 不會 |
| 成本 | 0 | **要錢**：走 Vertex，額度算在 Google Cloud 帳單（**沒有**免費 grounding 額度）|
| 拿到的東西 | 搜尋結果頁原文 | **模型合成的文字** ← 這就是要驗的地方 |

### 使用順序（⚠️ 別跳步）

```
① python3 -m mops.gemini_benchmark --dry-run    不花錢，看抽樣組成與預估花費
② python3 -m mops.gemini_benchmark -n 5         煙霧測試（約 $0.5）：驗連得上、CSV 續跑正常
③ python3 -m mops.gemini_benchmark -n 30        看趨勢（約 $2~5）
④ python3 -m mops.gemini_benchmark -n 200       完整結論（約 $11~31）
⑤ 過關才跑 worker 去補 failed                    見「跑法」
```

`--dry-run` 之外每一步都會 append 進 `gemini_benchmark.csv`，**中斷或加大 `-n` 重跑
都會跳過已完成的列，不重複付費** —— 所以 ②→③→④ 是累加的，不是重來。

前置：`.env` 放 `GOOGLE_CLOUD_PROJECT=`（見下面「設定：Vertex AI」），之後所有指令
都不必再打 `--project`。

> **①~④ 不需要 server**，也不碰 `revswarm.db` 的任何 state（`mode=ro`）。
> 只有 ⑤ 需要 server 與 `requeue-failed?engine=gemini`。

### 設定：Vertex AI（額度走 Google Cloud 帳單）

> ⚠️ **另一道門（AI Studio / Gemini Developer API，`x-goog-api-key`）已經刻意移除，別加回來。**
> 兩件事讓它不可用：
> 1. 2026-03 起 Google Cloud 的額度**不能**付 AI Studio 的帳，它走自己的 Prepay 餘額。
> 2. 2026-08 實測本專案的 AI Studio 金鑰**全部**回 `429 prepayment credits are depleted`
>    ——那是 Google 側的已知 bug（全新、確認 free tier、零使用量的專案也照樣被擋），
>    Google 自己的文件卻寫著 free tier 不需要 Prepay。
>
> 完整經過見下面的「怎麼走到 Vertex 這條路的」。那是一條走過的死路，**不要重走**。
> Vertex 走的是同一批 Gemini 模型、同一個 grounding、同一種回應結構，只是換一道門進去。

一次性設定：

```bash
curl https://sdk.cloud.google.com | bash && exec -l $SHELL   # 1. 裝 gcloud
gcloud auth application-default login                        # 2. 建 ADC（會開瀏覽器）
gcloud config set project <你的專案ID>
gcloud services enable aiplatform.googleapis.com             # 3. 開 Vertex（現名 Agent Platform API）
```

專案 ID 可以放進 `.env`（跟 `REVSWARM_TOKEN` 同一個檔），之後就不必每次打 `--project`：

```bash
GOOGLE_CLOUD_PROJECT=your-project-id
```

驗一次整條鏈（API 有沒有開、ADC 有沒有效、grounding 有沒有真的觸發）：

```bash
PROJ=$(gcloud config get-value project)
curl -s "https://aiplatform.googleapis.com/v1/projects/$PROJ/locations/global\
/publishers/google/models/gemini-3.7-flash:generateContent" \
  -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"台積電 2330 2020年1月營收哪一天公布？"}]}],
       "tools":[{"googleSearch":{}}]}' \
  | python3 -c "import json,sys; m=json.load(sys.stdin)['candidates'][0].get('groundingMetadata',{}); print('搜尋查詢:', m.get('webSearchQueries'))"
```

| 輸出 | 意思 |
|---|---|
| `搜尋查詢: ['...']` | 全通了 |
| `搜尋查詢: None` | 通了但 grounding 沒觸發 —— 正是 worker 判 `rate_limited` 而非 `failed` 的情況 |
| `403` + `has not been used in project` | 第 3 步的 enable 沒生效（剛開要等一兩分鐘）|
| `403` 但 body 是空的 | ⚠️ **偶發，會自己好**。實測打過一次空 body 的 403、下一次同樣請求就 200 —— 所以 worker 只在訊息指向永久性問題時才 fatal，其餘 403 退避重試 |
| `401` | ADC 過期或沒建，重跑 `gcloud auth application-default login` |

> ⚠️ **`gcloud auth login` 與 `gcloud auth application-default login` 是兩組不同的憑證。**
> worker 用的是後者（ADC），取 token 的指令是
> `gcloud auth application-default print-access-token`。只跑過 `application-default login`
> 的機器上，`gcloud auth print-access-token` 會失敗說 `No credentialed accounts`
> ——那不是壞掉，是問錯了憑證。
>
> ⚠️ **Vertex 沒有免費 grounding 額度**（每月 5,000 次那個是 AI Studio 的），
> 所以 `--free-quota` 預設 0。單價請以實際帳單校正 `--unit-price`。
>
> ⚠️ **`gemini-2.5-*` / `gemini-2.0-flash` 對新專案已 404 下架**
> （`no longer available to new users`），退不回便宜的舊型號。`DEFAULT_MODEL` 是 `gemini-3.7-flash`。

### 跑法（⚠️ 這是上面的第 ⑤ 步，先做完對照實驗）

> **別跳到這裡。** 直接拿 gemini 去補 `failed`，拿到的日期沒有任何外部基準可驗
> ——理由見下面「[放量前先做對照實驗](#放量前先做對照實驗mopsgemini_benchmarkpy)」。

```bash
# ⚠️ 不做這步 worker 會租不到任何任務（gemini 佇列預設是空的）
curl -X POST -H "Authorization: Bearer $REVSWARM_TOKEN" \
  "http://<SERVER>:8000/admin/requeue-failed?engine=gemini"

# 先小額試跑（預設 --max-searches 300 ≈ 40 筆任務，就是為此）
python3 -m worker.gemini_worker --server http://<SERVER>:8000 \
    --project <你的專案ID> --once

# 確認結果合理後再放量
python3 -m worker.gemini_worker --server http://<SERVER>:8000 \
    --project <你的專案ID> --max-searches 40000
```

> `--project` 可省略，只要 `.env` 裡有 `GOOGLE_CLOUD_PROJECT=`。

```bash
--model gemini-3.7-flash  # ⚠️ gemini-2.5-* / 2.0-flash 對新專案已 404 下架，退不回舊型號
--max-searches 300        # ⚠️ 不是任務數，是搜尋次數（見下）。0=不限
--max-calls 200           # ⚠️ 第二道保險：呼叫次數上限。搜尋次數只有「拿到回應」才數
                          #   得到，所以 gcloud 沒裝／一直 429／模型每次都不搜這類
                          #   持續失敗下它永遠是 0，擋不住無限迴圈
--free-quota 0            # ⚠️ Vertex 沒有免費 grounding 額度；只影響 log 的花費估算
--unit-price 0.014        # 每次搜尋單價，請以實際帳單校正
--batch 10 --delay 1      # 沒有反爬顧慮，可以比 google_worker 快很多
--rl-threshold 3          # 連續 3 筆 rate_limited（多半是 429）就收批退避
```

> ⚠️ **`--max-searches` 算的是搜尋次數，不是任務數**，而兩者差了一個數量級。
> 計費按「模型實際發出的搜尋查詢」，**實測一個任務會發 4~11 次搜尋、平均約 7 次**
> （2026-08-24 於 Vertex 量測 5 筆：4/4/8/8/11）——不是直覺的 1 次。
> worker 累加回應裡 `groundingMetadata.webSearchQueries` 的長度，超過上限就把剩餘租約
> 放回佇列並結束。預設 300 因此大約只夠 **40 筆任務**。
> 預設值刻意設得小：另外兩支跑錯只是浪費時間，這支跑錯是刷 Google Cloud 帳單。

### 三道防污染原封不動，外加一條新戒律

窗過濾、名稱錨點、server 端再驗窗全部沿用 `revlib.parse`——**模型回什麼日期都沒有特權，
一律要通過同一套規則才算數**，另外再過 `title_year_conflict` 擋掉「語氣肯定但年份錯掉」
的合成句。新增的一條是：

> ⚠️ **回應裡 `webSearchQueries` 是空的 → `rate_limited`，絕不是 `failed`。**
> 那代表模型根本沒去搜、是憑記憶回答的。把「沒查過」記成「Google 也沒有」會靜靜污染
> 研究資料——這跟 google_worker 的「頁面沒有 `#search` 容器就不是 SERP」是同一條戒律。
> 同理，金鑰錯／API 沒開／權限不足（401/403/400 API key not valid）會**直接停掉整個
> worker**、剩餘租約放回，而不是把整批刷成 `failed`。

### 命中來源分級：這才是實驗的產出

同一份回應會拆成兩塊分別餵 `revlib.parse`，`source` 記錄命中的是哪一塊：

| source | 意思 | 可信度 |
|---|---|---|
| `m_src` | 日期出現在 `groundingChunks` 的來源標題裡 | Google 索引回來的真實文字，接近 SERP |
| `m_txt` | 日期只出現在模型自己寫的回答裡 | **合成文字，可能是幻覺** |

worker 結束時會印出兩者比例。實務上 Gemini API 的 `groundingChunks.title` 常常只給網域名
（`moneydj.com`）而不是文章標題，所以 `m_src` 命中率可能很低——**那本身就是實驗結論，
不是 bug**：它代表這條路拿到的日期多半是模型「講」出來的，不是「查」出來的。

> `m_txt` 那批併入主資料前，請先跟 `yahoo` / MOPS 的重疊區對照，做「資料品質」那節對
> `google` 批做過的同一套檢查（週末率、偏早分布）。「資料品質 → 沒修掉、要知道的殘留風險」
> 已經記過 `google` 批
> 日期系統性偏早占 8% 且沒有外部基準可驗；grounding 這批只會更需要這道檢查。

### 審核模式：一次一筆，先看證據再決定要不要進 DB

上面那條「`m_txt` 併入主資料前要先對照」的提醒有個現實問題：**要對照的證據批次模式沒有留下來。**
模型憑什麼這麼說——它實際讀了哪些網域（`groundingChunks`）、發了哪些搜尋——`tasks`
一個欄位都裝不下，程序一結束就沒了。`raw_title` 只剩模型自己寫的那行標題。

所以有第二種跑法，把順序倒過來：**先把整份證據交出來，等人看完才決定要不要落地。**

```bash
# ① 租 1 筆、打一次 API、把證據印成 JSON。⚠️ 刻意【不回報】——日期還沒進 DB
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 --review-one

# ② 看完再落地（不打 API、不需要 --project）
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 \
    --review-verdict approve --note "這一筆為什麼撐得起這個日期"
#   reject = 撐不起 → 回報 failed（gemini 是最後一棒，那就是終點）
#   retry  = 模型根本沒去搜 → 放回佇列，不算審過、不寫稽核檔

# ③ 蓋章（讀 data/gemini_review.csv，只送 approve 的列，可重跑）
python3 -m mops.stamp_verified --from gemini-review --server http://127.0.0.1:8000
```

`--review-one` 的 exit code 就是行動指示：`0` 有東西可審／`3` gemini 佇列空的／
`4` 設定層級錯誤（每一筆都會重演，該停下來修）。⚠️ 沒回報的租約會在 `LEASE_TTL`
（600s）後被 server 惰性回收放回 `undone`——**中斷是安全的**，代價是這一筆下次要再付一次錢。

配 `.claude/skills/gemini-review`（`/loop /gemini-review` 一輪一筆）就是「Claude 逐筆審」
的跑法。判準與 `title-review` 同一套，多一條只有這個模式看得到的：

> ⚠️ **`chunks` 全是 `goodinfo` / PTT / 不明網域、或空的，就算標題看起來漂亮也要打問號。**
> 那代表這個日期是模型「講」的而不是「讀」的。`searches` 裡沒有一條真的搜到「這家公司
> 這個月」卻給出肯定答案，是更強的幻覺訊號。

**兩條界線是機制擋著的，不是靠自律：**

1. **審核者只能否決，不能無中生有。** `approve` 送出去的是 `judge()` 算好的那一份
   （逐字），審核者不經手 date/source/title/url——手打日期的路徑不存在。
   `worker_verdict=failed` 的那一筆**不能 approve**（沒有可核准的內容），程式直接拒收：
   要放行請去改判準，不要在單筆繞過。
2. **審核模式與批次模式共用 `judge()`。** 不可以各寫一套判準，否則審核者看到的
   `worker_verdict` 跟批次真的會回報的東西不一樣，等於在審另一套規則。

`--note` 與稽核檔上一列**一字不差會被拒收**。這條看似瑣碎，但 2026-08-25 的 title 審核
就是用共用 note 蓋了十萬筆章、後來全部撤銷——共用理由的稽核檔對稽核者毫無價值。

⚠️ 蓋的章是 **`verified='claude'` 不是 `gemini`**：審核者讀的是模型自己回的那段文字，
沒有引入第二個獨立來源，那是循環（見「出處與驗證 → `claude` 不是驗證」）。

### 怎麼走到 Vertex 這條路的（2026-08-24 實測）

寫這節的理由跟上面「踩過的雷」一樣：下一個人看到「Gemini grounding」四個字，會很自然地
照網路上的教學去申請 AI Studio 金鑰 —— **那條路已經走過而且是死的**。
每一步撞到什麼、怎麼確認不是自己設定錯，都記在這裡。

**1. 以為 Custom Search JSON API 還能用** → 錯。2025 起對新客戶關閉，2026-01 宣布
2027-01-01 全面停用。Vertex AI Search（現名 Agent Search）搜的是「你自己的內容」，
不回傳公開網頁結果，不能替代。→ Google 現在唯一還買得到的「查公開網頁」只剩 Gemini grounding。

**2. Console 搜不到「Generative Language API」** → 顯示名已改成 **Gemini API**
（有時寫 Gemini Developer API），service 名稱沒變。

**3. Create credentials 的「引導式 wizard」永遠給不出 API key** → 那個 wizard 只有
User data(OAuth client) / Application data(service account) 兩個選項。API key 在
「+ CREATE CREDENTIALS」的**下拉選單**裡，不在 wizard 裡。

**4. Gemini API 的 checkbox 是灰的** → tooltip 寫
`This API requires authentication with a service account-bound API key`。
Google 正在淘汰舊的 `AIza` 標準金鑰，換成綁 service account 的 auth key（`AQ.` 開頭）：
2026-06-19 起未加限制的被拒，**2026-09 起全部被拒**。走 Console 必須先自己建一個
service account，selector 才會出現。

**5. 金鑰拿到了、認證全通，但一行都跑不動 —— 429**

| 檢查 | 結果 |
|---|---|
| `ListModels` | ✅ 回 37 個模型 → auth key 有效、API 已啟用、header 正確 |
| `generateContent` | ❌ 一律 `429 Your prepayment credits are depleted` |

帶不帶 `google_search`、換任何型號、換 `-latest` 別名，全都一樣 → **是專案層級**。

> ⚠️ 這是 **Google 的已知 bug**，不是漏設什麼。Google 自己的 billing 文件寫著
> 「The free tier operates independently — it doesn't require Prepay setup」，
> 免費層根本不該檢查 prepay 餘額；但 2026-08-03 起開發者論壇一連串同樣回報：
> 全新、確認 free tier、零使用量的專案，`generateContent` 全部 429。
> 背景是 Prepay/Postpay 兩種 billing plan 2026-03-23 上線，新帳號預設 Prepay，
> 疑似把 free tier 專案也錯誤歸類進 Prepay 而餘額是 0。
>
> ⚠️ 順帶發現：`gemini-2.5-*` / `2.0-flash` 對新使用者已 404 下架
> （`no longer available to new users`），「退回便宜舊型號」這條路也沒了。

**6. 關鍵轉折：Google Cloud 的餘額能不能用？→ 能，但只能走 Vertex**

2026-03 起 Cloud 的 $300 歡迎額度**不能**付 AI Studio 的帳，但**可以**付 Vertex 的。
同樣的模型、同樣的 grounding、同樣的 `groundingMetadata` 結構 —— 只是換一道門進去。

**7. Vertex 實際接通** —— 四個錯誤訊息都很好認：

| 訊息 | 意思 |
|---|---|
| `gcloud: command not found` | `curl https://sdk.cloud.google.com \| bash` |
| `No credentialed accounts` | ⚠️ **不是壞掉，是問錯憑證**（見上面的 ⚠️ 兩組憑證）|
| `403 ... has not been used in project` | `gcloud services enable aiplatform.googleapis.com` |
| `200 OK` 但 `webSearchQueries: None` | 那句 prompt 不需要搜尋 —— 正是 worker 判 `nosearch` 的情況，不是錯 |

> ⚠️ 還打過一次**乾淨的 403 Forbidden（空 body），下一次同樣的請求就 200**。
> 所以 403 不可以一律當 fatal，否則一次抖動就收掉整個 worker。

**8. 首次真實量測：5 筆已知答案（MOPS 重疊區）**

```
6277 宏正  110/5   MOPS 2021-06-08  gemini 2021-06-09  ✗ (+1d)   searches=8
1301 台塑  111/10  MOPS 2022-11-08  gemini 2022-11-08  ✓        searches=11
2428 興勤  112/7   MOPS 2023-08-07  gemini 2023-08-07  ✓        searches=4
2476 鉅祥  111/7   MOPS 2022-08-08  gemini 2022-08-08  ✓        searches=8
8109 博大  111/10  MOPS 2022-11-02  gemini 2022-11-02  ✓        searches=4
```

有效 5/5，與 MOPS 一致 4/5。兩個發現：

- ⚠️ **一個任務會發 4~11 次搜尋、平均約 7 次**，不是直覺的 1 次。計費按搜尋次數，
  所以成本是原估的 7 倍 —— `--max-searches` 的預設值已照這個修正。
- ⚠️ **命中全部是 `m_txt`，`m_src` = 0/4。** `groundingChunks` 只有 1~2 筆且是網域名
  → 日期是模型「講」出來的，不是從檢索原文抽出來的。樣本放大後要重看這個比例。

**9. 決定：AI Studio 那條整個砍掉**（金鑰已刪除）

程式碼只剩 Vertex 一道門，不留旗標、不留 `AIStudioBackend`、不留 `GEMINI_API_KEY`。

> ⚠️ **別因為「多一個 fallback 比較保險」就把它加回來**：Cloud 額度付不了它的帳，
> 而它現在對新專案一律 429。留著只會讓下一個人再走一次第 2~5 步。

### 放量前先做對照實驗：`mops/gemini_benchmark.py`

**別直接拿 gemini 去補 `failed`。** 那批沒有任何外部基準可驗，會複製「資料品質」那節
`google` 批的困境：補到了，但發現日期系統性偏早時只能靠週末率、慣用申報日分布這些
間接證據去推論，無法直接算對錯。

先打「已經知道答案」的那批 —— MOPS 有官方申報日、`yahoo` 也已經成功的重疊區
（目前 **2,376 筆 / 49 檔**，`yahoo` 對 MOPS 的一致率是 **99.83%**，這就是要打敗的基準線；200 筆抽樣約發 800~2,200 次搜尋，實測每筆 4~11 次）：

```bash
python3 -m mops.gemini_benchmark --dry-run     # 不呼叫 API，看抽樣組成與預估花費
python3 -m mops.gemini_benchmark -n 5          # 煙霧測試：驗連得上、CSV 續跑正常
python3 -m mops.gemini_benchmark -n 200        # 真的跑（約 800~2,200 次搜尋）
                                              #   預設 --max-searches 2500 涵蓋高標
python3 -m mops.gemini_benchmark --report-only # 只根據既有 CSV 重印報表
```

只讀 `revswarm.db`（`mode=ro`），**不 lease、不 report、不寫任何 state**，server 跑著也能執行。

CSV 存 `search_queries`（模型實際下的查詢原文，用 ` | ` 分隔），不一致清單也會把它印出來
—— 這是**唯一**能分辨「模型下的查詢本身就爛」還是「查對了但抽錯日期」的證據。
`yahoo` / `google` worker 的查詢是我們自己組的，所以「絕不加『營收』二字」那條實測教訓
在這裡**管不到**，只能事後量。⚠️ 跑完才想加就得重花一次錢。
逐筆 append 進 `gemini_benchmark.csv`，中斷後重跑會跳過已完成的列（**不重複付費**）。

抽樣是**跨 49 檔輪抽**、不是純隨機：重疊區各檔筆數差很多（有的 73 個月全有、有的只有幾個月），
純隨機會讓樣本被少數幾檔灌爆，量到的就變成「模型對某一家的熟悉度」。`--seed` 固定可重現。

報表印四塊，每一塊都是為了擋掉一種誤讀：

| 區塊 | 擋掉什麼誤讀 |
|---|---|
| **召回** | 分母只算 `status=ok`（模型真的搜了）。⚠️ `nosearch`／`error` 不計入——沒查成功不等於查不到 |
| **準確率 + 對照組** | 同一批 `yahoo` 的一致率也算一次。沒有對照組的「gemini 92%」是沒有意義的數字 |
| **信任分級** | `m_src` / `m_txt` 分開算。整體好看但全靠 `m_txt` 撐 → 是「講」對的不是「查」對的 |
| **偏向** | 有號日差（負=偏早）＋週末率，跟「資料品質」那節量 `google` 批用的是同一把尺，結論才可比 |
| **模型實際下的查詢** | ⚠️ 查詢字串是**模型自己決定的，我們控制不到** —— 每筆次數分布，以及含民國年／西元年／「營收」二字的比例 |

> ⚠️ **這個實驗量到的是樂觀上界。** MOPS t05st01 只涵蓋約 49 家自願揭露月營收的公司，
> 幾乎都是大型股 —— 正是模型最可能單靠記憶就答得出來的一群。真正要補的 5,613 筆
> `failed` 是冷門股與舊月份，表現只會更差。
>
> **放量門檻建議：整體一致率 ≥ `yahoo` 對照組，且 `m_txt` 那層單獨也站得住。**
> 不過關就把結論補進這一節、關掉這條路——那也是一個有價值的產出。

## 部署（讓多台 worker 連到 server）

worker **沒有自動探索**，一定要用 `--server http://<位址>:<port>` 明確告訴它 server 在哪。
所以部署 = 「① 找出 server 的可連位址 → ② 起 server → ③ 每台 worker 指過去」。

三種連法：
- **Tailscale（推薦）**：server 與 workers 加入同一 tailnet，用 `100.x` IP，跨網路/NAT 都通、免開公網 port。
- **同區網 LAN**：workers 與 server 在同一區網，用 server 的區網 IP。
- **VPS / ngrok**：server 放公網 VPS 開 port，或 `ngrok http 8000` 臨時對外把 URL 給 workers。

### ① 找出 server 的可連位址（在 server 機器上跑）

```bash
tailscale ip -4     # Tailscale IP（100.x）；有裝就優先用這個
hostname -I         # 所有內網 IP；同區網用其中的區網位址（如 172.x / 192.168.x）
curl -s ifconfig.me # 公網 IP（僅在有對外開 port 時適用）
```
Tailscale IP 是該機器**固定**的位址，記一次即可；tailnet 若開了 MagicDNS 也可直接用主機名
（`--server http://<hostname>:8000`）。

### ② 起 server（server 機器）

token 寫在 repo 目錄的 **`.env`**（`REVSWARM_TOKEN=...`），server/worker 啟動時自動讀，
不用每次手打環境變數。`.env` 已被 `.gitignore` 排除、不會進版控。

```bash
cd /path/to/revswarm
# 產生 token 寫進 .env（只需一次）
echo "REVSWARM_TOKEN=$(head -c16 /dev/urandom | base64 | tr -d '\n')" > .env
cat .env                                        # ← 複製這串 token 給 worker

# 前景跑（Ctrl-C 停）；自動讀 .env
python3 server.py --host 0.0.0.0 --port 8000
```

想「關掉終端機也繼續跑」用 tmux（可隨時 attach 回去看即時輸出）：
```bash
tmux new -d -s revswarm "cd /path/to/revswarm && python3 server.py --host 0.0.0.0 --port 8000"
tmux attach -t revswarm            # 回去看畫面（Ctrl-b 再按 d 脫離，server 繼續跑）
tmux kill-session -t revswarm      # 要停止 server 時
```
`--host 0.0.0.0` 是關鍵：監聽所有介面（含 Tailscale/區網），不是只綁 `127.0.0.1`。

### ③ 每台 worker（其他機器）

```bash
# 把 repo clone 下來（worker 不需要 DB，只跟 server 走 HTTP）
git clone <你的 repo 網址> revswarm && cd revswarm

# token 寫進 .env（與 server 同一組），worker 自動讀
echo "REVSWARM_TOKEN=<貼上 server 那串 token>" > .env

curl http://<server位址>:8000/healthz          # 回 {"ok":true,...} 才代表連得到
python3 -m worker.yahoo_worker --server http://<server位址>:8000
```
多台 worker 就在每台重複本步驟（多機 = 多 IP，分攤 Yahoo 封鎖）。

### 看進度（任一台）

瀏覽器打開狀態頁（最直覺，會自動更新）：
```
http://<server位址>:8000/status?token=<你的 REVSWARM_TOKEN>
```
或用終端機看 JSON：
```bash
source .env     # 載入 REVSWARM_TOKEN
watch -n5 "curl -s -H \"Authorization: Bearer $REVSWARM_TOKEN\" http://<server位址>:8000/stats | python3 -m json.tool"
```

### 故障排除

| 症狀 | 原因 / 解法 |
|---|---|
| worker 回 `401` | 兩邊 `.env` 的 `REVSWARM_TOKEN` 不一致；worker 的要跟 server 那組一模一樣 |
| `curl /healthz` 連不到 | server 沒加 `--host 0.0.0.0`；或 worker 不在同一 tailnet（`tailscale status` 看得到 server 嗎）；或防火牆擋了 port |
| worker 一直印 `×`（rate_limited） | 該機 IP 被 Yahoo 擋；worker 會自動退避長睡，屬正常。多開不同網路的 worker 分攤 |

⚠️ 大量爬 Yahoo 可能違反其 ToS，也會招致更強封鎖。**務必節制、分散、加延遲。**

## 運維備忘

- server 只是一支 process + 一個 `revswarm.db`（WAL 模式）。中斷重啟不丟進度。
- 想擴 worker：多開機器/IP 即可，任務佇列會自動分配、不重派（原子租約已驗證 8 併發 0 重複）。
- 卡在 `dispatched` 的（worker 掛了）10 分鐘後自動回收，不用手動處理。
- 全部跑完後 `failed` 那批可 `POST /admin/requeue-failed` 再掃一輪，改抓西元年常能救回。

## 營收欄位（順手記錄，非權威）

爬公布日時，Yahoo 搜尋 snippet（多為 MoneyDJ）本來就常帶著金額，例如
`台化 109年8月營收189.84億、年減26.94%`。這段文字早已存進 `raw_title`，故 `revlib.parse_revenue`
順手把它解析成兩個欄位，**回報 success 時自動寫入**：

| 欄位 | 意義 |
| --- | --- |
| `revenue` | 單月(合併)營收，正規化為「元」（億/萬 換算）|
| `yoy` | 年增率（%，正=增、負=減）|

- 覆蓋率約 **9 成** success（其餘為格式異常/自結損益，後者刻意排除）。
- ⚠️ **非權威**：來源非官方、金額四捨五入到億/萬兩位，僅供**交叉校驗/研究參考**；要精確到元請用 MOPS 月營收。
- 既有 DB 一次性回填（**不重爬**）：`python3 backfill_revenue.py`（先停 server、先備份；`--dry-run` 可預覽）。
  server 啟動時會自動 `ALTER TABLE` 補上這兩欄。

## 出處與驗證（`url` / `verified`）

兩個補在 `tasks` 上的欄位，回答兩個以前答不出來的問題：**這個日期是誰說的**、
**有沒有第二個人也這麼說**。

| 欄位 | 意義 |
| --- | --- |
| `url` | 這個 `announce_date` 是從哪一篇讀到的。worker 回報 success 時附上，server 端再驗一次格式（非 `http`/`https` 一律存 NULL）|
| `verified` | 誰核對過這個日期、且**日期一致**才蓋章：`mops` \| `gemini` \| `claude` \| `tbd`。NULL = 沒人看過（**不是**「驗過但錯」）|

### 為什麼需要 `url`

`raw_title` 只是一段佐證文字。事後想追「這個日期到底是哪一篇寫的」，就只能拿標題
回頭再搜一次——而搜尋結果隨時在變。11.3 查 google 那批系統性偏早時就吃過這個虧：
有標題、沒有出處，無法回去看原文，只能靠週末率、慣用申報日分布這些間接證據去推論。

**三支 worker 的出處可信度不同，差一階就是差一階，別混用：**

| worker | 怎麼拿到的 | 可信度 |
| --- | --- | --- |
| yahoo | 從 SERP 的 `<a href>` 讀出來，用日期的字元位置往回綁最近的那個連結（`revlib.nearest_url`）| **最硬**。頁面上客觀存在的連結，位置綁定 |
| google | 解析的是 `inner_text`（整頁純文字），日期位置對不到 DOM 連結，只能用**標題錨點命中**去挑（`revlib.pick_url_by_name`，與 `_anchor_offsets` 同一套：公司名＋這個任務的年月）| **弱一階**。只是線索，不保證就是那個日期的出處；挑不到寧可回 NULL 也不亂挑 |
| gemini | 模型自報的 `URL:` 那行 | **弱一階**，而且是另一種弱：模型可能生一個不存在的網址。格式擋得掉、幻覺擋不掉 |

⚠️ Yahoo 的結果連結全部包一層轉址（`r.search.yahoo.com/…/RU=<百分比編碼的原網址>/RK=…`），
存轉址網址等於沒存：`_ylt` 是有時效的簽章、過期就 404，網址本身也看不出是哪一家媒體。
`revlib.unwrap_url` 一律解回 `RU=` 裡那個真正的網址。

⚠️ **往回找連結要從「錨點」開始，不是從「日期」開始**（`parse_detail` 回的 offset 就是
錨點位置）。窗內日期可能出現在錨點【前面】——上一筆結果的摘要裡就有一個窗內日期、而它
離錨點最近——這時從日期位置往回找會抓到上一筆結果的連結，看起來像有效出處卻指錯篇。

⚠️ **URL 過長一律拒收，不截斷**。截一半的網址是「看起來合法、點下去 404」，比 NULL 更糟
——NULL 誠實地說沒有出處，截斷的則謊稱有。

⚠️ **gemini 的 `m_src` 那層 url 一律是 NULL**。那層的日期來自 `groundingChunks`（真實檢索
文字），而模型的 `URL:` 行是它自己寫的、不保證就是那個 chunk 的出處；掛上去等於讓高信任
標記替低信任的網址背書。`groundingChunks` 自己給的則是 Google 的快取轉址，有時效、過期
就打不開。所以 `URL:` 跟 `TITLE:` 走同一條規則：**只在 `m_txt` 這層取**。

⚠️ **凡是改寫 `announce_date` 的敘述，都必須把 `url` / `verified` 一起清成 NULL**。
`url` 記的是「舊日期」出自哪一篇、`verified` 是對「舊日期」蓋的章；改了日期卻留著它們，
就變成一個看起來有出處、有人驗過的新日期——那是這兩欄最糟的失效方式（有值、且是錯的，
比 NULL 難發現得多）。目前五個寫入點都做了：`server.py` 的 `report`、`mops/mops_fill.py`、
`mops/mops_overwrite.py`、`apply_date_overrides.py`、`mark_prelisting.py --demote-success`。

### 為什麼 `verified` 不是信心分數

分數會逼我們發明一個沒有依據的數字。「有沒有第二個獨立來源同意」則是可查證的事實，
而且可以事後補算。所以這欄存的是**誰同意**，不是**多有信心**。

蓋章的判準一律是**日期真的一致**，不是「有跑過這一筆」。少了這個條件，`verified`
就退化成「有人碰過」——欄位裡照樣有值，只是不再代表任何事。日期不一致的會被回報成
`mismatch` 但**不蓋章也不改資料**：那是兩個來源打架的訊號，值得人逐筆去看。

### 四個值的強弱不同，別混著算

```
mops    官方申報文件（公開資訊觀測站 t05st01）——最硬
gemini  模型 grounding 獨立查出同一個日期
claude  ⚠️ 人工讀 raw_title 的判斷，不是第二個獨立來源
tbd     看過了，但不是高信心
```

⚠️ **`claude` 不是驗證，是篩選。** `raw_title` 正是產生 `announce_date` 的那段文字
（`revlib.parse` 從它附近抽日期），再讀一次同一段字沒有引入任何新證據——這是循環。
它能回答的只有一件事：**這段佐證文字撐不撐得起這個日期**。實際抓到的問題長這樣：

```
8176 智捷 110/5 → 2021-06-09
  title：「智捷110年5月27日股東常會延後召開…紀念品延期發放」
  錨點命中的是「110年5月」後面接的「27日」，這根本不是營收公告
3630 新鉅科 113/5
  title：「新鉅科 2024年5月","yptydevice":"desktop"…」← Yahoo 頁面的內嵌 JSON
```

所以排序上 `claude`/`tbd` 墊底（`tbd < claude < gemini < mops`），永遠不會覆蓋
`mops` 或 `gemini` 的章。算「被獨立驗證的量」時**一律排除**它們。

判斷寫在版控的 `data/title_review.csv`，不直接寫 DB：`mops`/`gemini` 那兩條路隨時可以
重跑重現，這條不行——留檔才有得稽核「當初為什麼判高信心」，DB 重建後也補得回來。
每一列都必須有 `note`，沒寫理由的直接拒收。CSV 裡的 `announce_date` 是讀的當下看到的
值，送進 `/verify` 當樂觀鎖：之後日期若被 `mops_overwrite` 之類改掉，比對不上就記成
`mismatch` 不蓋章——判斷是對著舊日期做的，不該套到新日期上。

```bash
python3 -m mops.stamp_verified --from claude --server http://127.0.0.1:8000
python3 -m mops.stamp_verified --from tbd    --server http://127.0.0.1:8000
```

`claude` 這個章有兩條來源，判準相同（都是「這段佐證文字撐不撐得起這個日期」），
差別只在讀的是什麼：

| 來源 CSV | 讀的是 | 跑法 |
|---|---|---|
| `data/title_review.csv` | DB 裡已經落地的 `raw_title`（事後審）| `--from claude` |
| `data/gemini_review.csv` | `gemini_worker --review-one` 交回的**完整證據**，含模型讀過的網域（**寫入前**審，見「gemini_worker → 審核模式」）| `--from gemini-review` |

⚠️ `--from gemini-review` 蓋的是 `claude` **不是 `gemini`**——判斷者是讀 gemini 證據的人，
不是第二個獨立來源。蓋成 `gemini` 會讓它在 `VERIFIER_RANK` 裡爬到 `claude` 之上、
覆蓋掉不該覆蓋的章，而且對外宣稱了一個不存在的獨立確認。

### ⚠️ `verified='mops'` 不全是「兩個獨立來源同意」

```
verified='mops' 共 2,376 筆
  ├─ source='mops'      923 筆  ← 日期本來就是從 MOPS 灌進來的，這是自我驗證
  └─ source 為 q_*/g_*  1,453 筆 ← 爬到的值被官方紀錄獨立確認，這才有資訊量
       q_roc 1,308／q_ad 144／q_ad_manual 1
```

`mops/mops_fill.py` 與 `mops/mops_overwrite.py` 會把日期直接寫成 MOPS 的值並標
`source='mops'`。那批再被 `stamp_verified --from mops` 蓋章，就成了「MOPS 同意 MOPS」
——欄位裡有值，但沒有任何獨立確認發生過。而且每跑一次 `mops_overwrite` 這個比例就往上
走一點（那 923 筆裡有 3 筆就是這樣來的），所以它只會愈來愈需要被指出來。

要算真正被獨立驗證的量，一律排除它們：

```sql
SELECT COUNT(*) FROM tasks WHERE verified='mops' AND source != 'mops';
```

⚠️ **分母也要看清楚：1,453 / 122,932 success = 1.2%。** 這份資料的絕大多數從來沒有被
第二個來源看過，`verified IS NULL` 是常態而不是例外——那是「不知道」，不是「驗過但沒過」。
`verified` 這欄最危險的用法是拿它當品質背書：「我們有驗證機制」跟「這份資料被驗證過」
是兩件不同的事。

```bash
# server 要跑著（蓋章走 /verify，server 是這個 DB 的唯一寫入者）
python3 -m mops.stamp_verified --from mops --server http://127.0.0.1:8000 --dry-run
python3 -m mops.stamp_verified --from mops --server http://127.0.0.1:8000

# gemini 那條要先跑完對照實驗；只收「gemini 與 MOPS 都同意」的列
python3 -m mops.stamp_verified --from gemini --server http://127.0.0.1:8000
```

⚠️ 一列只有一個 `verified` 欄，**裝不下「兩個來源都同意」**。所以 server 端有強弱排序
（`VERIFIER_RANK`：`mops` > `gemini`），弱的不會覆寫強的，會記成 `kept`。兩支的先後順序
因此不影響最終結果——但這是靠排序守住的，不是靠使用者記得順序。

跑完最值得看的數字是 **`mismatch`**，不是蓋章數。蓋章多但 `mismatch` 一堆，代表資料
有系統性問題，不是「大部分都驗過了」。

## 資料品質

爬完之後做過一輪體檢（外部拿 MOPS 官方申報日交叉、內部做分層對照實驗）。結論是
**日期主體可信，但有幾個缺陷會汙染下游分析**，處理方式與殘留風險記在這裡。

### 外部驗證

MOPS「歷史重大訊息」的官方申報日涵蓋 52 檔、2,552 筆。拿它當基準前得先扣掉兩種假重疊：

1. **循環論證** —— 其中 920 筆是 `mops_fill.py` 當初灌進 DB 的，拿 MOPS 驗它必然 100% 一致。
2. **基準自己的 false positive** —— 零營收公司會另發一則「說明本公司113年11月-114年1月
   營業收入呈現為零」的重大訊息，它涵蓋一個月份區間、晚好幾個月才報，卻同時滿足
   「有營收關鍵字 + 有年月」而被 `is_monthly_revenue()` 收進基準。`1438 三地開發` 那 4 筆
   就是這樣來的，害得交叉驗證誤判 DB 三筆抓對的日期是錯的。已加「呈現為零」排除。

**排除後真實重疊 1,456 筆、一致率 99.73%**，而且剩下的 4 筆不一致全是先前查過、
刻意保留的個案：台化 109/9、109/10 與富驊 112/12 差 1 天（【公告】擷取邊界，兩日皆對）、
瑞智 110/7 早 3 天（疑自結搶先揭露，對事件研究反而更相關）。「MOPS 有、DB 卻判 failed」**0 筆**。

⚠️ 這 1,456 筆全部來自 yahoo 引擎且全是大型股 —— **google 補搜的 2.4 萬筆與 MOPS 零重疊，
沒有外部驗證**，只能靠下面的內部對照實驗判斷。

### 已修掉的

| 缺陷 | 規模 | 處理 |
|---|---|---|
| `revlib.parse` 錨點只鎖月不鎖年 | 全庫 705 筆 provenance 錯配 | 錨點改綁本任務年份（民國/西元皆可）|
| 上市前的假 `success` | 165 筆 / 47 檔 | `mark_prelisting.py --demote-success` 打回 `prelisting` |
| `prelisting` 與 `success` 邏輯矛盾 | 1,310 筆 / 46 檔 | 上一條的鏡像，降級後自動歸零 |
| `yoy` 混了單月與累計兩種語意 | 1,273 筆 | 新增 `yoy_scope` 欄分流 |
| 低信度列混在主檔裡 | 18,959 筆（15.4%）| 新增 `confidence` / `flags` 欄標記 |
| MOPS 基準收進「呈現為零」的另一則重大訊息 | 4 筆 / 1 檔 | `is_monthly_revenue()` 加排除；`load_baseline()` 對舊快取重濾 |

錨點漏洞長這樣：任務 111/6、112/6、113/6 三筆全都錨到同一篇「宏碁智新 **115年**6月」。
窗過濾只擋得掉日期本身離譜的，擋不掉「錨錯地方、卻剛好挑到一個窗內日期」。

`confidence='low'` 的四條 flag，每條都是實測過的錯誤來源：

| flag | 筆數 | 意思 |
|---|---:|---|
| `no_revenue` | 18,252 | `raw_title` 抽不到營收金額 —— 多半是 Yahoo 頁面模板碎片（`"yptydevice":"desktop"` 那種），不是新聞標題，日期沒有文字佐證 |
| `no_anchor` | 2,713 | `raw_title` 既無公司名也無代號 —— 可能抓到 CMoney 盤後速報那種一頁列一堆股票的彙總頁 |
| `year_conflict` | 697 | `raw_title` 講的是「別年的同月份」——錨點漏洞的殘留（修錨點前全庫 705 筆，其中 8 筆隨假 success 一起降級了）|
| `pre_public` | 0 | 營收月早於首次公開 2 個月以上（已全數降級，只有 `stock_dates.db` 沒涵蓋的公司才會再出現）|

分層是有效的，不是憑感覺標的：

```
                     週末率    偏離該公司慣用申報日 >=3 天
confidence=high       2.04%              7.77%
confidence=low        4.28%             17.42%

                     可比對    用單月營收自算年增率對得上
yoy_scope=monthly    72,267            97.21%
yoy_scope=cumulative    716             1.54%      ← 這些是累計年增率，別當單月用
```

### 沒修掉、要知道的殘留風險

**① google 引擎的日期系統性偏早（約 1,900 筆，占 google 的 8%）**

三條獨立證據：控制公司習慣後（同時有兩引擎、各 ≥12 筆的 1,056 檔股票內對照）google 週末率
**5.84%** vs yahoo **1.64%**；週日群聚可對到隔天週一的 yahoo 高峰（`2022-01-09(日)` google
109 筆 / yahoo 10 筆 → 隔天 `01-10` yahoo 525 筆）；偏離「該公司慣用申報日」的分布，google
在**偏早那側多出 7.8pp**、偏晚側持平，是方向性偏移而非雜訊變大。

機制是 `g_ad` 走中央社【公告】，Google SERP snippet 上的日期不等於申報日。逐筆修不掉，
但 `engine` 欄留在主檔裡，**做事件研究請拿 `engine` 當敏感度分析的分組變數**。

**② 遲交公司理論上可能被配到「窗內的錯日期」（未觀測到，但也證偽不了）**

窗規則本身沒辦法分辨「這篇窗內文章是公布日」還是「這家公司其實遲交、窗內那篇講的是別的事」。
實際觀測到的遲交行為是**抓不到**而非抓錯（`3494 誠研 109/1` 遲到 2020-02-17 才發、落在窗外
→ 爬取端不採 → 進 `data/date_overrides.csv` 人工補），這是安全的失敗方向。

一度以為 `1438 三地開發` 是「抓到窗內錯日期」的實例，查下去是 MOPS 基準自己的
false positive（見上），DB 抓的才是對的。所以**目前沒有任何一筆已知的「遲交造成窗內錯日期」**
—— 但 MOPS 只涵蓋大型股，冷門股量不到，不能宣稱不存在。

**③ 剩下 5,613 筆 `failed`**

87% 集中在民國 109~111（舊月份 × 冷門股），民國 113 之後只剩 325 筆。其中 4,591 筆夾在
同一檔的 success 中間（真破洞），1,022 筆落在頭尾之外（可能已下市/長期停牌）。

### 檢查過、沒問題的

窗規則（全庫只有 `data/date_overrides.csv` 那筆人工個案在 1~15 之外）、同一公司同一天報兩個月
（0 筆）、revenue 負數（0 筆；`revenue=0` 的 126 筆都是浩鼎、高端疫苗這類真的零營收）、
月覆蓋率無整月塌陷（最低民國 110/5-6 的 77~79%，最高 99.9%）、MOPS 有而 DB 判 failed（1 筆）。

## 交叉驗證（MOPS，選配）

Yahoo 是唯一資料源；MOPS「歷史重大訊息」(t05st01) 只涵蓋約 32 家自願揭露月營收的公司，
但那些是**官方申報日、精確到時分**，是最高信度的黃金基準。`mops_validate.py` 拿它來抽驗 Yahoo：

```bash
python3 -m mops.mops_validate                  # 驗 revswarm 目前已成功的股票
python3 -m mops.mops_validate --codes 2330 2454 1301 6505
python3 -m mops.mops_validate --codes-file data/active_stocks.txt   # 建全量基準(建議 tmux)
```

只讀 `revswarm.db`（不干擾線上爬取），MOPS 結果快取到 `mops_baseline.csv`（可續跑），
逐筆比對寫入 `mops_validation.csv`，並印出：一致率、不一致清單（Yahoo 可能抓錯）、
以及「MOPS 有、Yahoo 卻判 failed」的**可回收**案例（可搭 `/admin/requeue-failed` 重掃）。
> 實測抽驗台積電/台塑/聯發科等，重疊月份與 MOPS 官方申報日 **100% 一致**。

## 遲交的個案：`data/date_overrides.csv`

窗是「營收月的次月 **1~15 日**」。這個上界不是猜的——`mops_baseline.csv` 那 2556 筆
MOPS 官方申報日是**唯一沒被窗過濾過**的權威資料，它的日分佈：

```
日 10 : 520   ← 截止日高峰      日 13 :   8
日 11 : 112                     日 14 :  18
日 12 :  50                     日 15 :   8      >15 日：0 筆（最大日 = 15）
```

也就是「次月 10 日截止 + 遇假日順延 + 小幅落後」全都塞得進 15 日。
**所以窗不該全域放寬**（放寬只會讓三月才發的回顧型文章有機會被當成公布日）。

但確實有公司某個月遲交到 16、17 號。實測：`3494 誠研 109/1` 的 MoneyDJ 文章日期是
`2020-02-17`，而誠研 58 筆歷史公布日都在 7~14 號（28 筆是 10 號）——那個月就是遲交。
這種**不是窗設太窄，是個案異常**，沒有一個「遲交可以到幾號」的規則涵蓋得了，只能逐筆
人工確認。這份 CSV 就是那個出口：

```bash
python3 apply_date_overrides.py --dry-run   # 只檢查與列出
python3 apply_date_overrides.py             # 寫入（會先備份 DB）
```

驗證規則刻意只守強的那一半：

| | 規則 | 為什麼 |
|---|---|---|
| ✓ 驗 | `announce_date` 必須落在**營收月的次月** | 月份錯一定是打錯或誤判 |
| ✓ 驗 | 日必須是該月合法的日、`note` 不可空白 | 擋打錯；每筆都要交代證據 |
| ✓ 驗 | `announce_date` 必須是**補零**的 `YYYY-MM-DD` | 下面那條稽核 SQL 用 `substr` 取「日」，格式歪一格就漏抓 |
| ✓ 驗 | `source` 必須以 `manual` 結尾 | 標記掉了就分不出人工與爬蟲結果 |
| ✓ 驗 | 表頭與每列欄位數要完全對、同一個 key 不可重複 | 半形逗號會靜默截斷 `note`；重複的後者會無聲蓋掉前者 |
| ✗ 不驗 | 幾號以前才算「合理遲交」 | 上限是個案問題，由 `note` 與人負責 |

任一列不合格就整批拒絕、不寫任何東西。冪等的「一致」比到 `announce_date`/`source`/
`raw_title`/`revenue`/`yoy` 全部——改對 CSV 裡打錯的 `raw_title` 會重新導出 `revenue`，
不會被誤判成「已一致」。

`source` 用 `*_manual` 後綴（例 `g_roc_manual`）：這些是全庫唯一會落在窗外的 success，
標記要留得住稽核，否則就靜靜違反「所有 `announce_date` 都過窗」這個保證。稽核用：

```sql
SELECT * FROM tasks WHERE state='success'
 AND CAST(substr(announce_date,9,2) AS INTEGER) NOT BETWEEN 1 AND 15;
```

> 這條 SQL 靠 `substr(...,9,2)` 取「日」，所以 `2020-2-17` 會被讀成 `7`（落在 1~15）→
> 這筆窗外 success 從稽核裡消失。其他 success 的日期都經 `revlib.validate_date` 正規化，
> 這支為了收窗外個案必須繞過窗檢查，但寫入前仍會正規化一次（`canonical_date`）。

> ⚠️ `revswarm.db` 是執行期產物、不進版控，所以**重建 DB 後要重跑一次** `apply_date_overrides.py`
> 才會把人工判斷補回去——這正是這份 CSV 存在的理由（否則那些判斷只活在 DB 裡）。

## 已驗證（端到端小規模測試）

台積電 109/1~109/5 實跑：日期與 MOPS 官方申報日**完全一致**（109/1→2020-02-10…），
其中 109/3 靠西元年補查救回；一筆遇 rate_limited 正確退回 undone（不算 failed）。
併發、惰性回收、窗再驗證、token 驗證、requeue 皆通過。

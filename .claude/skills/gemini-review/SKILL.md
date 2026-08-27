---
name: gemini-review
description: 一次審一筆 gemini_worker 的結果：跑 --review-one 拿到完整證據（模型原文、它讀過的網域、抽出的日期），判斷這段證據撐不撐得起這個日期，再 approve/reject/retry 落地並蓋章。當使用者說「審 gemini」「跑 gemini review」「用 loop 審 gemini」時使用。
---

# 一次審一筆 gemini 的結果（gemini-review）

**一次 skill 只做一筆。** 產出是這一筆的判斷與理由，不是一批統計。
配 `/loop /gemini-review` 使用：一輪一筆，審完就結束這一輪。

## ⚠️ 這個 skill 存在的理由

`gemini_worker` 是三支 worker 裡信任度最低的一支：它回的 `m_txt` 是**模型合成的文字**，
不是搜尋結果原文。而它憑什麼這麼說的證據——**模型實際讀了哪些網域**
（`groundingChunks`）、**發了哪些搜尋**——`tasks` 一個欄位都裝不下，批次模式跑完就
隨程序結束消失。

所以審核模式刻意把順序倒過來：**先看證據，再決定要不要讓這個日期進 DB**。
`--review-one` 租了任務、打了 API，但**不回報**；日期還沒進 DB，這時否決是免費的。

兩條界線（機制上就守著，不是靠自律）：

> **① 你只能否決，不能無中生有。** `approve` 送出去的是 worker 自己算好的那一份
> （`report_if_approved`，逐字），你不經手 date/source/title/url。看到「模型講了一個
> 日期但 `revlib.parse` 沒抽到」，那是 parse 的問題——寫進 note 並回報給使用者，
> **不要自己送日期進去**。
>
> **② 不可以寫分類器。** 每一筆的 verdict 都必須是你看過那份 dump 之後的判斷，
> note 必須是**針對那一筆**寫的。`--note` 與上一列一字不差會被程式直接拒收
> ——2026-08-25 的 title 審核就是用共用 note 蓋了十萬筆章、後來全部撤銷。

## 0. 先確認有沒有未完成的審核

```bash
test -f .gemini_review_pending.json && echo "有未完成的：先審它，不要再租新的" || echo "乾淨"
```

有的話**跳到第 2 步**審那一筆（再跑 `--review-one` 會覆蓋掉它的證據，而那筆租約
已經付過錢了）。

## 1. 取件（租 1 筆 + 打一次 Gemini，不回報）

```bash
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 --review-one
```

⚠️ **這一步會花錢**（實測一筆發 4~11 次搜尋，約 $0.06~0.15）。exit code 就是這一輪的走向：

| exit | 意思 | 怎麼做 |
|---|---|---|
| 0 + `recommend: review` | 有東西可審 | 往下走 |
| 0 + `recommend: retry` | 模型**沒去搜**／連線失敗，這一筆根本沒查過 | 直接 `--review-verdict retry`，不要判 reject，**這一輪結束** |
| 3 | gemini 佇列空的 | 回報「佇列空」並**結束整個 loop**；要補件請先 `POST /admin/requeue-failed?engine=gemini` |
| 4 | 金鑰／權限／API 未啟用 | **停掉 loop**，照 README 檢查 Console，別再叫我 |

租約 600 秒（`lease_ttl`）。逾時沒回報 server 會自動放回 undone——中斷是安全的，
代價只是這一筆下次要再付一次錢。

## 2. 側查（免費，但這是唯一的外部對照）

```bash
python3 - <<'PY'
import csv, datetime, json, os, sqlite3, statistics
d = json.load(open(".gemini_review_pending.json", encoding="utf-8"))
t = d["task"]; sid, ry, rm = t["stock_id"], t["roc_year"], t["roc_month"]
c = sqlite3.connect("file:revswarm.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
r = c.execute("SELECT engine,state,attempts,fail_count FROM tasks WHERE id=?",
              (t["id"],)).fetchone()
print(f"{sid} {t['name']} {ry}/{rm}  engine={r['engine']} state={r['state']} "
      f"attempts={r['attempts']} fail_count={r['fail_count']}")
days = []
print("同檔鄰近月（±3 月）：")
for x in c.execute("SELECT roc_year,roc_month,announce_date,source,verified FROM tasks"
                   " WHERE stock_id=? AND state='success' AND announce_date IS NOT NULL"
                   " ORDER BY roc_year,roc_month", (sid,)):
    days.append(datetime.date.fromisoformat(x["announce_date"]).day)
    if abs((x["roc_year"] - ry) * 12 + x["roc_month"] - rm) <= 3:
        print(f"   {x['roc_year']}/{x['roc_month']} → {x['announce_date']} "
              f"{x['source']} {x['verified'] or ''}")
if days:
    print(f"   慣用公布日中位數 {statistics.median(days)}（n={len(days)}）")
if os.path.exists("mops_baseline.csv"):
    hit = next((x for x in csv.DictReader(open("mops_baseline.csv", encoding="utf-8-sig"))
                if x["stock_id"] == sid and int(x["roc_year"]) == ry
                and int(x["roc_month"]) == rm), None)
    print(f"   ⚠️ MOPS 官方申報日 {hit['announce_date']} {hit['announce_time']}"
          f" ← 最硬的對照，直接比" if hit else
          "   MOPS 基準沒有這一筆（只涵蓋 49 檔）")
ex = d.get("extracted") or {}
if ex.get("date"):
    wd = "一二三四五六日"[datetime.date.fromisoformat(ex["date"]).weekday()]
    print(f"模型的日期 {ex['date']} 是週{wd}")
PY
```

⚠️ **`fail_count` 是重要背景**：這一筆會走到 gemini，正是因為 yahoo 跟 google 都沒抓到。
兩棒都找不到的月份，第三棒忽然「找到了」，本身就值得多疑一分——尤其當 `chunks`
裡沒有任何一個正經財經站的時候。

## 3. 逐條讀 dump，逐條判

dump 裡真正要看的欄位：

| 欄位 | 怎麼讀 |
|---|---|
| `worker_verdict` | 批次模式**會**怎麼判（`success`／`failed`）。你的工作是同意或否決它，不是重算 |
| `extracted` | `revlib.parse` 通過窗過濾＋名稱錨點抽出來的 date/source/title/url。⚠️ `worker_verdict=failed` 時這裡照樣有值——那正是要看的（模型講了什麼、給了什麼假網址）|
| `text` | 模型的原始回答（`DATE:／TITLE:／URL:` 三行）。`TITLE:` 那行是它自稱逐字複製的原標題 |
| `chunks` | ⚠️ **模型實際讀過的來源**。常常只有網域名（`moneydj.com`）不是文章標題——那是 Gemini API 的限制，不是 bug |
| `searches` | 它發出的查詢字串。有沒有真的搜「這家公司這個月」？搜錯關鍵字卻給出肯定答案是強烈的幻覺訊號 |
| `url_check` | `true`=網址活著且頁面有公司名（加分）／`null`=模型誠實回 NONE（可接受）／`false`=編了個死網址（worker 已判 failed）|

**approve 要同時滿足**（跟 title-review 同一套判準，多一條 chunks）：

- 公司名**精確**：不是更長公司名的前綴（「聯上」不是「聯上發」、「安碁」不是「安碁資訊」）
- **這個任務的年月**，不是別年的同月份
- 內容真的是**營收公告**：有金額或年增率
- 日期落點沒有明顯矛盾：離慣用公布日很遠、落在週末，要在 note 裡交代
- `chunks` 至少有一個站得住腳的來源（moneydj／cnyes／中央社／經濟日報／twse／mops…）。
  ⚠️ chunks 全是 `goodinfo`／PTT／不明網域、或空的，就算 title 看起來漂亮也要打問號
  ——那代表這個日期是模型「講」的而不是「讀」的

**gemini 特有的錯法**（除了 title-review 那張表的每一條都仍然適用）：

| 長相 | 問題 |
|---|---|
| `text` 寫得語氣肯定、但 `searches` 沒有一條搜到這家公司這個月 | 憑記憶回答，不是查來的 |
| `chunks` 空的、`extracted.source=m_txt` | 沒有任何檢索原文支撐，純合成 |
| `TITLE:` 那行像新聞標題，但 `chunks` 只有股票資訊站首頁 | 標題是編的（實測：1216 統一那筆 URL 格式完全正確、抓回來是 404）|
| 模型的日期正好是「月初第 10 天」這種慣例值、且沒有任何金額 | 用慣例推算，prompt 明令禁止的那件事 |
| `MOPS 官方申報日` 與模型的日期**不一致** | 直接 reject，這是最硬的證據打架 |

判不出來就 reject。⚠️ 代價要知道：gemini 是升級鏈**最後一棒**，reject 會讓這一筆落
`state='failed'` 成為終點（不會再自動換引擎）。但那比放一個沒人查過的日期進去好
——而且 CSV 留著證據，日後有新引擎可以 requeue 回來。

## 4. 落地（一道指令，資料欄位程式自己填）

```bash
# 撐得起
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 \
  --review-verdict approve --note "這一筆為什麼撐得起：具體寫 chunks/金額/與 MOPS 或鄰近月的對照"

# 撐不起（回報 failed，終點）
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 \
  --review-verdict reject  --note "這一筆的具體疑點"

# 根本沒查過（recommend: retry）——不必寫理由，放回佇列
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 --review-verdict retry
```

- `approve`/`reject` 會把這一筆追加進版控的 `data/gemini_review.csv`（含 `chunks`）。
  `retry` **不寫**——沒查過的那一筆不算審過。
- ⚠️ `worker_verdict=failed` 的那一筆**不能 approve**，程式會拒收：沒有可核准的內容
  （URL 驗證沒過／根本沒抽到窗內日期）。要放行請去改判準，不要在單筆繞過。
- note 寫**這一筆**的理由。與上一列一字不差會被拒收（見上面的界線 ②）。
- 回報成功才會刪 `.gemini_review_pending.json`；server 掉線就原地重跑同一道指令。

## 5. 蓋章（只有 approve 需要）

```bash
python3 -m stamp_verified --from gemini-review --server http://127.0.0.1:8000
```

蓋的是 `verified='claude'`（不是 `gemini`）：你讀的是模型自己回的那段文字，沒有引入
第二個獨立來源——那是循環，是**篩選不是驗證**（見 README「`claude` 不是驗證」）。
它讀整份 `data/gemini_review.csv`、只送 approve 的列，已蓋過的會記成 `kept`，可以重跑。

## 6. 這一輪的回報（保持一行到三行，loop 裡要好讀）

```
1101 台泥 109/1 → approve  2020-02-10 [m_txt]  chunks=moneydj.com;cnyes.com
  理由：<這一筆的理由>
  這一筆 7 次搜尋（約 $0.10）；累計已審 <wc -l data/gemini_review.csv 減 1> 筆
```

- ⚠️ dump 裡的 `cost_note` 是**這一個程序**的花費，不是累計——每次 `--review-one` 都從
  零開始數，別把它當總額回報。
- 出現**沒見過的錯法**就明講，那比數字有價值。
- ⚠️ **每 20 筆停下來問使用者要不要繼續**（約 $2）。這支是按搜尋次數對 Google Cloud
  計費的，一個沒人看著的 loop 就是一張沒人看著的帳單。
- 佇列空（exit 3）或 fatal（exit 4）→ 講清楚原因並**結束 loop**，不要空轉重試。

## 收尾檢查

- `git status` 應該只動到 `data/gemini_review.csv`
- `.gemini_review_pending.json` 應該已經不存在（gitignored，但留著代表這一筆沒落地）
- ⚠️ server 沒跑的話 `--review-verdict` 與 `stamp_verified` 都會連不上。
  server 是這個 DB 的唯一寫入者，不要繞過它直接改

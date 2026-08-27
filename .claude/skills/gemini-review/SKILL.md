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

有的話**跳到第 2 步**審那一筆。⚠️ 程式也會擋：交接檔還在時 `--review-one` 直接
exit 2、不打 API（覆蓋掉的是一份已經付過錢的證據）。真的要丟掉才加 `--force`。

## 1. 取件（租 1 筆 + 打一次 Gemini，不回報）

```bash
python3 -m worker.gemini_worker --server http://127.0.0.1:8000 --review-one
```

⚠️ **這一步會花錢，而且單筆上限不可控**：一次呼叫發幾次搜尋完全由模型決定，程式擋不住
（`--max-searches` 只是跨筆的閘門）。實測 8 筆：4/4/8/8/11（2026-08-24）、6/10/**37**
（2026-08-27）、**46**（2026-08-27 下午，愛爾達-創這筆）——**約 $0.06~0.64 一筆，平均約
$0.25**。46 次那筆跟 37 次那筆同款：模型抱著一個候選答案逐日／逐金額回頭驗證（見下面的
錯法表），錢照算——上限還在往上更新，別把 $0.52 當成硬天花板。exit code 就是這一輪的走向：

| exit | 意思 | 怎麼做 |
|---|---|---|
| 0 + `recommend: review` | 有東西可審 | 往下走 |
| 2 | 上一筆的判斷還沒落地（交接檔還在） | 回第 0 步，先把那一筆審完 |
| 0 + `recommend: retry` | 模型**沒去搜**／連線失敗，這一筆根本沒查過 | 直接 `--review-verdict retry`；判 reject 會被程式拒收（沒查過不是「查過但沒有」），**這一輪結束** |
| 3 | gemini 佇列空的 | 回報「佇列空」並**結束整個 loop**；要補件請先 `POST /admin/requeue-failed?engine=gemini` |
| 4 | 金鑰／權限／API 未啟用 | **停掉 loop**，照 README 檢查 Console，別再叫我 |

租約 600 秒（`lease_ttl`）。逾時沒回報 server 會自動放回 undone——中斷是安全的，
代價只是這一筆下次要再付一次錢。

⚠️ **同一筆連續租到 `worker_verdict=rate_limited`（`recommend: retry`）可能連發生 2~3 次**
（實測 2026-08-27：全友 2305 連 3 次、欣大健康 4198 連 2 次，之後才正常打完）。這是
Gemini API 端限速，不是這筆任務有問題——照表格規則 `retry` 放回去就好，不必因此懷疑
這筆任務或停 loop；真的一直卡在同一筆很多輪才需要考慮暫停等額度恢復。

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
| `chunks` | ⚠️ **模型實際讀過的來源**。常常只有網域名（`moneydj.com`）不是文章標題——那是 Gemini API 的限制，不是 bug。稽核檔裡這一欄存的是 JSON 陣列（標題裡真的會有分號）|
| `searches` | 它發出的查詢字串。有沒有真的搜「這家公司這個月」？搜錯關鍵字卻給出肯定答案是強烈的幻覺訊號 |
| `url_check` | `"ok"`=網址活著且頁面有公司名（加分）／`null`=模型誠實回 NONE（可接受）／`"dead"`=編了個死網址（404/410 或根本是首頁，worker 已判 failed）／`"unknown"`=**我們自己確認不了**（對方擋機器人／逾時／頁面裡沒寫公司名）。⚠️ `unknown` 不是模型的錯，worker 只清掉 url、日期照留——這種要靠你看 `chunks` 與側查來判 |

**approve 要同時滿足**（跟 title-review 同一套判準，多一條 chunks）：

- 公司名**精確**：不是更長公司名的前綴（「聯上」不是「聯上發」、「安碁」不是「安碁資訊」）
- **這個任務的年月**，不是別年的同月份
- 內容真的是**營收公告**：有金額或年增率
- 日期落點沒有明顯矛盾：離慣用公布日很遠、落在週末，要在 note 裡交代
- `chunks` 至少有一個站得住腳的來源（moneydj／cnyes／中央社／經濟日報／twse／mops…）。
  ⚠️ chunks 全是 `goodinfo`／PTT／不明網域、或空的，就算 title 看起來漂亮也要打問號
  ——那代表這個日期是模型「講」的而不是「讀」的

**額外的側查技巧：累計金額交叉核對**（實測 2026-08-27 唯一一筆 approve、1313 聯成 109/3）：
若 TITLE 裡有「1—N月達W億元」這種年初至今累計數字，去 `data/title_review.csv` 找同一檔
**已經審過、verified='claude' 的前幾個月**金額，手動加總比對。這筆聯成的案例：已驗證的
109/1(33.8億)+109/2(17.54億)=51.34億，加上這筆聲稱的3月37.68億＝89.02億，跟 TITLE 自稱的
「1—3月達88.88億元」只差 0.16%——這種精確度很難是編的，是比單看 chunks 更硬的獨立佐證。
沒有累計數字可核對、或核對不上時，這條技巧幫不上忙，仍照上面幾條判準走。

**gemini 特有的錯法**（除了 title-review 那張表的每一條都仍然適用）：

| 長相 | 問題 |
|---|---|
| `text` 寫得語氣肯定、但 `searches` 沒有一條搜到這家公司這個月 | 憑記憶回答，不是查來的 |
| `chunks` 空的、`extracted.source=m_txt` | 沒有任何檢索原文支撐，純合成 |
| `TITLE:` 那行像新聞標題，但 `chunks` 只有股票資訊站首頁 | 標題是編的（實測：1216 統一那筆 URL 格式完全正確、抓回來是 404）|
| 模型的日期正好是「月初第 10 天」這種慣例值、且沒有任何金額 | 用慣例推算，prompt 明令禁止的那件事 |
| `MOPS 官方申報日` 與模型的日期**不一致** | 直接 reject，這是最硬的證據打架 |
| `searches` 裡有**加引號的完整標題**（`"宏和1月營收0.35億元年減4.01%"`），而 `chunks` 是空的 | ⚠️ 它先寫出標題、再拿那串字回頭搜想證明自己，搜不到卻照樣給肯定答案。**自我驗證失敗**——實測 1446 宏和、1472 三洋實業兩筆都這樣 |
| `TITLE:` 裡的金額與 `searches` 裡它自己搜的數字**差 10 倍**（單位沒換對） | 標題是拿數字現算再套模板生成的，不是逐字複製的原標題。實測 1472：搜 `"29,339"`（千元＝2.93 億，與該檔近年量級相符），標題卻寫 0.29 億，而年增率 50.09% 又算得出來 |
| `TITLE:` 用的公司名不是 DB 的 `name`（實測 1472：DB 與 MoneyDJ／玉山都寫「三洋實業」，模型寫「三洋紡」）| ⚠️ **名稱錨點沒中**。`m_txt` 這條路 `revlib.parse` 在錨點沒中時會退回「窗內第一個日期」，所以那個日期**只過了窗過濾**——公司名對不對完全靠你這雙眼睛，程式沒有替你擋 |
| `TITLE:` 的公司名跟 DB 的 `name` **完全是另一家公司**，不是前綴/簡稱/相似字的問題（實測 2026-08-27 一輪 8 筆裡出現 6 次：3115 富榮綱→「寶島極」、3191 雲嘉南→「和進」、3226 龍鋒→「至寶電」／「至寶光電」、3466 德晉→「致振」、4198 欣大健康→「環瑞醫」(109/1 與 109/2 **兩個月都是**)、1538 正峰→「正峰新」）| ⚠️ **本輪最大宗的錯法，比單純「名稱錨點沒中」嚴重**：`searches` 前段通常先搜對公司名找不到，中途整個換成另一家公司的名字繼續搜、生標題。同一檔股票跨月重複同一個錯誤名字（4198 兩個月都是「環瑞醫」）時，看起來不是隨機幻覺，比較像模型對這個股號本身的訓練記憶就是錯的——遇到同一檔又出現同一個錯誤名字，reject 之外可以在 note 裡點出「跨月重現」給後續審核者參考 |
| `searches` 裡混入一句完全不相干公司的完整標題（實測：1109 信大那筆混入「遠東新2月營收160.73億元年減10.64%…」、1472 三洋紡那筆混入「千興3月營收1.16億元年增90.45%…」）| ⚠️ 兩次格式都是同一套模板（`COMPANY X月營收Y億元年增/減Z% 1—X月達W億元`）——像是模型記得這個新聞標題模板、套進一個真實存在但不相干的公司當範例／校準，再套同一模板生成當前任務的假標題。看到 searches 裡出現跟這筆任務股號、公司名都對不上的完整標題，直接視為污染跡象 |

`worker_verdict=failed` 且 `text` 是 `DATE: NONE` 的那一筆是**誠實的 failed**（模型確實搜了、
正常回話了、就是沒有窗內日期）——同意它、`reject`，note 寫「同意 failed」與支持它的旁證即可，
不必猶豫。⚠️ 這與「dump 帶 `error`」完全不同：後者是**沒查過**，只能 retry（程式會拒收 reject）。

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
  （URL 確認為不存在／根本沒抽到窗內日期）。要放行請去改判準，不要在單筆繞過。
- ⚠️ dump 帶 `error`（`nosearch`／`retry`／`fatal`）的那一筆**只能 retry**，approve/reject
  都會被程式拒收：那份 dump 裡沒有任何證據，而空證據看起來很像「該否決」。
- note 寫**這一筆**的理由。與上一列一字不差會被拒收（見上面的界線 ②）。
- 回報成功才會刪 `.gemini_review_pending.json`；server 掉線就原地重跑同一道指令。
- ⚠️ exit 5＝**判斷已經回報 server 了，但稽核列沒寫進 CSV**。它會把那一列印出來：
  手動補進 `data/gemini_review.csv`、刪掉交接檔，**不要重跑**同一道指令（會重複回報）。

## 5. 蓋章（只有 approve 需要）

```bash
python3 -m stamp_verified --from gemini-review --server http://127.0.0.1:8000
```

蓋的是 `verified='claude'`（不是 `gemini`）：你讀的是模型自己回的那段文字，沒有引入
第二個獨立來源——那是循環，是**篩選不是驗證**（見 README「`claude` 不是驗證」）。
它讀整份 `data/gemini_review.csv`、只送 approve 的列，已蓋過的會記成 `kept`，可以重跑。
同一個月份被審過兩次（`requeue-failed` 把 reject 的那筆排回來重審）一律**以最後一列為準**，
覆蓋情形它會印出來——所以重審的判斷直接往後 append 就好，不要回頭改舊列。

## 6. 這一輪的回報（保持一行到三行，loop 裡要好讀）

```
1101 台泥 109/1 → approve  2020-02-10 [m_txt]  chunks=["moneydj.com","cnyes.com"]
  理由：<這一筆的理由>
  這一筆 7 次搜尋（約 $0.10）；累計已審 <wc -l data/gemini_review.csv 減 1> 筆
```

- ⚠️ dump 裡的 `cost_note` 是**這一個程序**的花費，不是累計——每次 `--review-one` 都從
  零開始數，別把它當總額回報。
- 出現**沒見過的錯法**就明講，那比數字有價值。
- ⚠️ **每 10 筆停下來問使用者要不要繼續**（照實測平均 $0.25/筆，約 $2.5；但單筆可能到
  $0.52，所以最壞情況約 $5）。原本寫「每 20 筆約 $2」是照 4~11 次搜尋估的，2026-08-27
  實測有一筆發了 37 次——這支是按搜尋次數對 Google Cloud 計費的，一個沒人看著的 loop
  就是一張沒人看著的帳單。
- 佇列空（exit 3）或 fatal（exit 4）→ 講清楚原因並**結束 loop**，不要空轉重試。

## 收尾檢查

- `git status` 應該只動到 `data/gemini_review.csv`
- `.gemini_review_pending.json` 應該已經不存在（gitignored，但留著代表這一筆沒落地）
- ⚠️ server 沒跑的話 `--review-verdict` 與 `stamp_verified` 都會連不上。
  server 是這個 DB 的唯一寫入者，不要繞過它直接改

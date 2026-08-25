---
name: title-review
description: 從 revswarm DB 取 100 筆 verified IS NULL 的 success，逐條閱讀 raw_title 並審核：撐得起日期的寫進 data/title_review.csv 並蓋 verified='claude'，撐不起的打回 undone 並改排 yahoo 佇列。當使用者說「審 title」「讀 100 筆 title」「繼續審核」時使用。
---

# 逐條審核 raw_title（title-review）

一次 100 筆。產出是**逐筆的判斷與理由**，不是一個分類器。

## ⚠️ 這個 skill 存在的唯一理由

2026-08-25 那次，同樣的任務我寫了 5 條正則、蓋了 106,293 筆 `claude` 章，
實際逐筆讀過的不到 500 筆。使用者的評語是「你這就偷懶啊」——他是對的。
那批章後來全部撤銷。

所以本 skill 的第一條規則是：

> **不可以寫分類器、正則、啟發式規則來代替閱讀。**
> 每一筆的 verdict 都必須是你看過那一筆的 title 之後做的判斷，
> 每一筆的 note 都必須是**針對那一筆**寫的，不可以是共用字串。
> 100 筆就是 100 個不同的判斷理由。

判斷理由重複、或看得出是套版產生的，就是失敗——那份 CSV 是要進版控供人稽核的，
一個「形狀完整」的固定 note 對稽核者毫無價值。

⚠️ 也不要用「先跑規則挑出可疑的、其餘放行」這種折衷。那還是分類器，只是換個說法。
規則抓得到的是**已知**的錯法；逐條讀才抓得到沒見過的（實測就是這樣抓到山東泰山的
天氣記錄、日文雜誌《安心》、股東常會通知、2026 年的文章對到 2022 年的任務）。

## 1. 取件

```bash
python3 - <<'PY'
import sqlite3, datetime, statistics
from collections import defaultdict
c = sqlite3.connect("file:revswarm.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
# 每檔的慣用公布日：判斷「日期合不合理」要用，這是 title 本身看不出來的
days = defaultdict(list)
for r in c.execute("SELECT stock_id,announce_date FROM tasks"
                   " WHERE state='success' AND announce_date IS NOT NULL"):
    days[r["stock_id"]].append(datetime.date.fromisoformat(r["announce_date"]).day)
rows = c.execute(
    "SELECT id,stock_id,name,roc_year,roc_month,announce_date,source,raw_title,revenue,yoy"
    " FROM tasks WHERE state='success' AND verified IS NULL ORDER BY id LIMIT 100").fetchall()
WD = "一二三四五六日"
for r in rows:
    d = datetime.date.fromisoformat(r["announce_date"])
    med = statistics.median(days[r["stock_id"]]) if days[r["stock_id"]] else None
    print(f"{r['stock_id']} {r['name']} {r['roc_year']}/{r['roc_month']} → {r['announce_date']}"
          f"(週{WD[d.weekday()]}) {r['source']}  慣用日 {med}  rev={r['revenue']} yoy={r['yoy']}")
    print(f"   {r['raw_title']}")
PY
```

⚠️ `ORDER BY id LIMIT 100` 不加 OFFSET：蓋章或打回之後那 100 筆就不再是
`verified IS NULL`，下次自然接續。**若有一筆卡住重複出現，先查它為什麼蓋不了章**
（實測踩過：`3494 誠研 109/1 = 2020-02-17` 是合法的窗外遲交個案，早期版本的
`/verify` 樂觀鎖套了窗驗證，害它永遠 mismatch、永遠卡在隊首）。

## 2. 逐條讀，逐條判

**信心高** = 這段 title 自己就撐得起這個日期。要同時滿足：

- 公司名**精確**（不是更長公司名的前綴：「聯上」不是「聯上發」、「安碁」不是
  「安碁資訊」、「統一」不是「統一超」）
- **這個任務的年月**（不是別年的同月份——SERP 會混進近期文章）
- 內容真的是**營收公告**（有金額或年增率，或是公開資訊觀測站的仟元表格）
- 日期落點沒有明顯矛盾（距該檔慣用日很遠、或落在週末，要在 note 裡交代）

⚠️ **`revenue` 是 NULL 不代表可疑**。公開資訊觀測站的
「(單位：仟元)項目合併營業收入淨額本月9,123,490」格式比新聞標題更權威，
只是 `parse_revenue` 不認得它。拿 `revenue` 當判準會把最好的一批資料判成可疑。

**佐證強度是有階的，note 裡要標出來**：

| 強度 | 長相 | 為什麼 |
|---|---|---|
| 最硬 | 標題內含**時戳**且與 `announce_date` 逐字相符<br>`…年減20.14% 人氣 (0) 2020/02/10 16:06`<br>`中央社 2025年4月10日` | 日期不是我們從頁面別處撿的，是文章自己寫的 |
| 硬 | 公開資訊觀測站仟元表格<br>`(單位：仟元)項目合併營業收入淨額本月9,123,490` | 官方原始格式，比新聞轉述權威 |
| 一般 | MoneyDJ／中央社【公告】標題，金額與 `rev` 對得上 | 標題完整但日期靠頁面 |
| 弱 | 只有公司名+年月、沒有金額 | 撐不起日期，通常該打回 |

實測 100 筆裡有 12 筆是「最硬」那層——那是免費的加分，看到就記進 note。

⚠️ **同檔不同月出現相同金額時要停下來核對**。實測亞泥 109/4 與 109/6 的
`revenue` 都是 6752000000（67.52 億）。逐筆看過兩篇標題各自標明自己的月份
（`2020年4月` vs `109年6月`）才確認不是錯配——水泥業月營收穩定，四捨五入到億的
兩位小數撞在一起是合理的。但這個訊號也可能代表**兩筆抓到同一篇文章**，不核對
就分不出來。

**實際踩過的錯法**（拿來提醒自己看什麼，不是拿來寫成規則）：

| 長相 | 問題 |
|---|---|
| `台泥 2022年10月","yptydevice":"desktop"` | 錨點命中但接的是頁面內嵌 JSON，背後沒有文章 |
| `味王 2022年10月 相關 <a class=` | SERP 的「相關搜尋」區塊，不是搜尋結果 |
| `嘉泥 115年6月營收…` 對到任務 111/6 | 別年的同月份 |
| `智捷110年5月27日股東常會延後召開` | 錨點對上「110年5月」，接的是「27日」，根本不是營收公告 |
| `泰山2022年12月份历史天气记录` | 同名而已（山東泰山） |
| `安心 2021年 10月号 [雑誌]` | 同名而已（日文雜誌）|
| `公告-聯上發-2020… 聯上發. 253` | 抓到別家公司 |
| `歷年股利政策及除權息一覽表 ; 2020/08/13` | 那是除息日不是公布日 |
| `預扣款日…抽籤日期, 2020年3月3日` | IPO 申購日程 |
| `台泥 112年3月營收 - 看板 Stock - 批踢踢實業坊`<br>`台泥113年6月營收 - Stock股票板 - PTT Web` | ⚠️ PTT 討論串**標題**不是公告：公司名年月都對、但沒有任何金額，日期取自貼文時間。就算內文寫「來源：公開資訊觀測站」也一樣——引用的人是網友，貼文時間不等於申報日 |

判不出來就判「打回」。⚠️ 但要記得代價：打回會**丟掉那個日期**，而舊月份的 SERP
結果會衰退——實測 12 筆重爬，yahoo 只救回 1 筆。所以判準是「有具體疑點才打回」，
不是「沒十足把握就打回」。

## 3. 高信心的：寫檔 + 蓋章

追加進 `data/title_review.csv`（欄位 `stock_id,roc_year,roc_month,announce_date,verdict,note`），
`verdict=claude`，`note` 寫**這一筆**的理由。然後：

```bash
python3 -m mops.stamp_verified --from claude --server http://127.0.0.1:8000
```

`announce_date` 要填你讀的當下看到的值——它在 `/verify` 當樂觀鎖，那列之後若被
別的東西改過就不會蓋錯。同一個 `(stock_id, roc_year, roc_month)` 重複會被拒收，
重讀同一筆時要先把舊那行換掉。

## 4. 撐不起的：打回 undone + 改排 yahoo

```bash
python3 - <<'PY'
import json, os, sys, urllib.request
sys.path.insert(0, "."); import revlib
revlib.load_env()
items = [  # 填入這批要打回的，date 是你讀的當下看到的 announce_date
    {"stock_id": "1218", "roc_year": 111, "roc_month": 12, "date": "2023-01-12"},
]
req = urllib.request.Request("http://127.0.0.1:8000/admin/requeue",
    data=json.dumps({"items": items, "engine": "yahoo"}).encode(), method="POST",
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ.get("REVSWARM_TOKEN", "")})
with urllib.request.urlopen(req) as r:
    print(json.loads(r.read())["applied"])
PY
```

⚠️ 打回的**不寫進 CSV**——那個檔只收「已判定可信」的紀錄。打回的證據留在
`state=undone` 這個事實本身，重爬會產生新的 title 再重新審。

## 5. 收尾

- 回報這批的數字：蓋章 N 筆／打回 M 筆，以及**打回的那幾筆各自是什麼問題**
- 出現沒見過的錯法就明講，那比數字有價值
- `git status` 確認只動了 `data/title_review.csv`
- ⚠️ server 若沒跑，`stamp_verified` 與 `/admin/requeue` 都會連不上。
  server 是這個 DB 的唯一寫入者，不要繞過它直接改

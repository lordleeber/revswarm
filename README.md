# revswarm

分散式爬蟲，收集台股「每月營收公布日期」，供「營收公布日 vs 股價反應」事件研究使用。

- 範圍：1848 檔（上市+上櫃+興櫃）× 民國 109/1 ~ 115/1（73 個月）＝ **134,904** 個 (股票×月) 任務。
- 精度：公布日精確到「日」。
- 資料源：**Yahoo 台灣搜尋**（`tw.search.yahoo.com`）。公布日只存在於新聞文章裡，
  結構化營收頁（MOPS / Yahoo / MoneyDJ）都只有年月+金額、沒有公布日。
- 為什麼要分散式：單一 IP 對 Yahoo 高頻查詢會被封。多機 = 多 IP，才能把 13.5 萬筆爬完。

> 資料源探索的完整結論與所有踩過的雷（為何不用 MOPS、查詢字串禁忌、窗過濾、名稱撞庫…）
> 見 [`todo.txt`](todo.txt)。這份 README 只講怎麼跑。

## 架構

`server`（一台，持有全部任務佇列）＋ 多台 `worker`（任意機器/IP，向 server 租任務、爬、回報）。

```
  worker@A ─┐
  worker@B ─┼──HTTP──►  server (SQLite 工作佇列)
  worker@C ─┘           /lease  /result  /stats
```

狀態機：`undone → dispatched → (success | failed)`
- `dispatched` 超過 **10 分鐘**沒回覆 → 派工時「惰性回收」自動視為 `undone`（免背景執行緒）。
- `failed` = worker 民國年+西元年**兩種都試過仍找不到**（真的沒有）。
- `rate_limited` ≠ `failed`：立刻放回 `undone`，worker 退避重試，不計失敗。

三道防污染（保證 0 髒資料）：
1. worker 端「窗過濾」：只信落在**營收次月 1~15 號**的日期。
2. worker 端「精確名稱錨點」：避免「統一」吃到「統一超」、「台塑」吃到「台塑化」。
3. **server 端再驗一次窗**：`/result` 收到的 success 日期不在窗內就退回重做，不信 worker。

## 檔案

| 檔案 | 說明 |
|---|---|
| `build_stocks.py` | 從證交所 ISIN 建 `stocks.csv`（code→中文簡稱→market）|
| `data/active_stocks.txt` | 1848 檔股票代號（一行一個）|
| `data/name_overrides.csv` | 已下市/ISIN 查無者的人工補名（目前 3426 台興、6806 森崴能源）|
| `data/date_overrides.csv` | 人工確認的**遲交**公布日（窗外個案，目前 1 筆；見下）|
| `apply_date_overrides.py` | 把上面那份 CSV 套進 DB（冪等；DB 不進版控，重建後要重跑）|
| `stocks.csv` | 產生的對照表（進版控）|
| `init_tasks.py` | 展開 1848×73 成 tasks 寫入 SQLite（冪等）|
| `server.py` | 工作佇列 server（標準庫 http.server + sqlite3，零依賴）|
| `worker.py` | 爬蟲 worker（curl --http1.1、民國+西元雙查、窗過濾、退避）|
| `google_worker.py` | 補搜 worker：Playwright 驅動系統 Chrome 查 Google，撿 Yahoo 救不回的 `failed`（見下）|
| `requirements.txt` | **只服務 `google_worker.py`** 的相依（playwright）；核心零依賴，不必安裝|
| `revlib.py` | 共用核心：期望窗 + Yahoo 頁解析（server/worker 都用同一套窗）|
| `export.py` | 把 DB 的 success 匯出成研究用 CSV（含 `revenue`/`yoy`）|
| `mops_validate.py` | 用 MOPS 官方申報日**交叉驗證** Yahoo 抓到的公布日（見下）|
| `backfill_revenue.py` | 從既有 `raw_title` 回填 `revenue`/`yoy`（不重爬，見下）|

**核心零第三方依賴**，只需 `python3`（3.8+）與 `curl`。部署 worker 只要複製 `worker.py` + `revlib.py`。
唯一的例外是 `google_worker.py` 需要 playwright（`requirements.txt`），只在要跑 Google 補搜的機器上裝。

## Quick start

```bash
# 1. 建 code→name 對照表（需連 isin.twse.com.tw）
python3 build_stocks.py                      # → stocks.csv (1848/1848)

# 2. 展開任務到 SQLite（冪等，可重跑補新股）
python3 init_tasks.py                         # → revswarm.db (134,904 undone)

# 3. 啟動 server（token 寫在 .env，server 自動讀，不用手打）
echo "REVSWARM_TOKEN=$(head -c16 /dev/urandom | base64 | tr -d '\n')" > .env
python3 server.py --host 0.0.0.0 --port 8000  # 自動讀 .env 的 REVSWARM_TOKEN

# 4. 在「每一台」worker 機器上跑（複製 worker.py + revlib.py 過去）
echo "REVSWARM_TOKEN=<與 server .env 同一組>" > .env
python3 worker.py --server http://<SERVER_IP>:8000

# 5. 隨時看進度
source .env
curl -s -H "Authorization: Bearer $REVSWARM_TOKEN" http://<SERVER_IP>:8000/stats | python3 -m json.tool

# 6. 匯出研究資料
python3 export.py --out revenue_dates.csv
```

## API

所有 endpoint 需 header `Authorization: Bearer <token>`（`/healthz` 除外）。

| method | path | 說明 |
|---|---|---|
| POST | `/lease?n=30&worker=<id>&engine=yahoo` | 原子租一批任務（`n` 上限 200）。**越舊營收月越優先**（跨所有股票齊步：全部 109/1 → 109/2 → …），並順便惰性回收逾時租約。`engine` 分流佇列（預設 `yahoo`），worker.py 與 google_worker.py 不會搶同一批；只收 `yahoo`\|`google`，其餘回 400（打錯字若靜默放行會讓 worker 一直看到空佇列）|
| POST | `/result` | 批次回報 `{worker, results:[{id,status,date?,source?,title?}]}`；status ∈ success/failed/rate_limited |
| GET | `/stats` | 各 state 計數、進度%、近 5 分吞吐、ETA、成功率（JSON）|
| GET | `/status` | 人類可讀的**狀態頁**（HTML 儀表板，自動更新）。瀏覽器可用 `?token=<token>`；`?refresh=<秒>` 調更新頻率 |
| GET | `/healthz` | 存活探針（免 token）|
| POST | `/admin/requeue-failed` | 把所有 `failed` 重開成 `undone`，做「最後一輪」（改西元年常能救回）。加 `?engine=google` 則連 engine 一併轉過去，交給 google_worker 專門處理，不影響其餘 yahoo 佇列；未知 engine 回 400（否則 3 萬筆會被丟進沒有 worker 會租的佇列）|

## worker 調參

```bash
python3 worker.py --server URL \
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
WORKER_PROXY=socks5h://127.0.0.1:1080 python3 worker.py --server http://<SERVER>:8000
#   等同： python3 worker.py --proxy socks5h://127.0.0.1:1080 --server ...
```

- **只影響爬 Yahoo 的 `curl`**；worker↔server 的租任務/回報（`urllib`）不受影響、仍走原路
  （所以 server 只在 tailnet 也 OK）。不設時行為完全不變。
- 一台 VM ＝ 一顆 IP；退避邏輯照舊留著（換 IP 是分攤，不是拿來加速轟炸）。
- 詳解見 `docs/worker-proxy.html`。

## google_worker：補搜 failed（Playwright + 系統 Chrome）

`worker.py` 爬 Yahoo 兩種年份都試過仍找不到窗內日期的 `failed` 任務，實測有一批
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
google-chrome --version                   # 用系統的 /usr/bin/google-chrome（實測 146）
```
> ⚠️ **`requirements.txt` 只服務 `google_worker.py`。**
> playwright 是本 repo 唯一的第三方依賴，`server.py` / `worker.py` / `revlib.py`
> 仍維持「純標準庫、零依賴」——**只跑 server 或 Yahoo 主線的機器不必安裝它**，
> 複製 `worker.py` + `revlib.py` 過去就能跑。測試也不需要（playwright 是延後 import 的）。

另外需要**圖形環境**（headful 才不會被擋）：`DISPLAY=:0`。無頭機器請改派有桌面的機器跑。
Chrome 設定檔存在 `~/.cache/revswarm-chrome`（`--profile` 可改），保留 cookie 以降低驗證碼。

跑法：先把 `failed` 轉去 `google` 佇列（否則 google_worker 租不到任何任務），
再啟動 google_worker（用法與 `worker.py` 對稱）：
```bash
curl -X POST -H "Authorization: Bearer $REVSWARM_TOKEN" \
  "http://<SERVER>:8000/admin/requeue-failed?engine=google"

DISPLAY=:0 python3 google_worker.py --server http://<SERVER>:8000 --once   # 先小量驗證
DISPLAY=:0 python3 google_worker.py --server http://<SERVER>:8000         # 再放量（用 tmux）
```
`engine=google` 佇列跟原本 `yahoo` 佇列完全分開派工，`worker.py` 與 `google_worker.py`
可同時開著、不會互搶任務。參數意義與 `worker.py` 相同，預設值不同：

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
# 複製 worker 需要的兩個檔（用 scp 從 server 拉，或任何方式）
mkdir -p ~/revswarm && cd ~/revswarm
scp <user>@<server位址>:/path/to/revswarm/worker.py .
scp <user>@<server位址>:/path/to/revswarm/revlib.py .

# token 寫進 .env（與 server 同一組），worker 自動讀
echo "REVSWARM_TOKEN=<貼上 server 那串 token>" > .env

curl http://<server位址>:8000/healthz          # 回 {"ok":true,...} 才代表連得到
python3 worker.py --server http://<server位址>:8000
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

## 交叉驗證（MOPS，選配）

Yahoo 是唯一資料源；MOPS「歷史重大訊息」(t05st01) 只涵蓋約 32 家自願揭露月營收的公司，
但那些是**官方申報日、精確到時分**，是最高信度的黃金基準。`mops_validate.py` 拿它來抽驗 Yahoo：

```bash
python3 mops_validate.py                       # 驗 revswarm 目前已成功的股票
python3 mops_validate.py --codes 2330 2454 1301 6505
python3 mops_validate.py --codes-file data/active_stocks.txt   # 建全量基準(建議 tmux)
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
| ✗ 不驗 | 幾號以前才算「合理遲交」 | 上限是個案問題，由 `note` 與人負責 |

`source` 用 `*_manual` 後綴（例 `g_roc_manual`）：這些是全庫唯一會落在窗外的 success，
標記要留得住稽核，否則就靜靜違反「所有 `announce_date` 都過窗」這個保證。稽核用：

```sql
SELECT * FROM tasks WHERE state='success'
 AND CAST(substr(announce_date,9,2) AS INTEGER) NOT BETWEEN 1 AND 15;
```

> ⚠️ `revswarm.db` 是執行期產物、不進版控，所以**重建 DB 後要重跑一次** `apply_date_overrides.py`
> 才會把人工判斷補回去——這正是這份 CSV 存在的理由（否則那些判斷只活在 DB 裡）。

## 已驗證（端到端小規模測試）

台積電 109/1~109/5 實跑：日期與 MOPS 官方申報日**完全一致**（109/1→2020-02-10…），
其中 109/3 靠西元年補查救回；一筆遇 rate_limited 正確退回 undone（不算 failed）。
併發、惰性回收、窗再驗證、token 驗證、requeue 皆通過。

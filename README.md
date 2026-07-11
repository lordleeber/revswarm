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
| `stocks.csv` | 產生的對照表（進版控）|
| `init_tasks.py` | 展開 1848×73 成 tasks 寫入 SQLite（冪等）|
| `server.py` | 工作佇列 server（標準庫 http.server + sqlite3，零依賴）|
| `worker.py` | 爬蟲 worker（curl --http1.1、民國+西元雙查、窗過濾、退避）|
| `revlib.py` | 共用核心：期望窗 + Yahoo 頁解析（server/worker 都用同一套窗）|
| `export.py` | 把 DB 的 success 匯出成研究用 CSV |

**零第三方依賴**，只需 `python3`（3.8+）與 `curl`。部署 worker 只要複製 `worker.py` + `revlib.py`。

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
| POST | `/lease?n=30&worker=<id>` | 原子租一批任務（`n` 上限 200），順便惰性回收逾時租約 |
| POST | `/result` | 批次回報 `{worker, results:[{id,status,date?,source?,title?}]}`；status ∈ success/failed/rate_limited |
| GET | `/stats` | 各 state 計數、進度%、近 5 分吞吐、ETA、成功率 |
| GET | `/healthz` | 存活探針（免 token）|
| POST | `/admin/requeue-failed` | 把所有 `failed` 重開成 `undone`，做「最後一輪」（改西元年常能救回）|

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

## 已驗證（端到端小規模測試）

台積電 109/1~109/5 實跑：日期與 MOPS 官方申報日**完全一致**（109/1→2020-02-10…），
其中 109/3 靠西元年補查救回；一筆遇 rate_limited 正確退回 undone（不算 failed）。
併發、惰性回收、窗再驗證、token 驗證、requeue 皆通過。

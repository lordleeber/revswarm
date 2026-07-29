#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm google_worker：向 server 租 engine='google' 的任務、用 Playwright 驅動「系統安裝的
Chrome」查 Google 搜尋、回報結果。用來二次補搜 worker.py（爬 Yahoo）救不回的 failed 任務
——手動抽測 10 筆真實 failed，Google 命中 9 筆。

為什麼是 Playwright 而不是 API 或 curl（三條死路，別重走，見 todo.txt 9.2）：
  ✗ Google Custom Search JSON API：已對新客戶關閉（2027-01 全面停用），一律回
    403 "This project does not have the access to Custom Search JSON API."
    換金鑰/換專案都無效。
  ✗ curl 直接爬 www.google.com/search：每次都被轉去 /httpservice/retry/enablejs，
    因為 Google 在核對 Client Hints / TLS 指紋，帶 Chrome UA 也偽裝不了；換 IP 無效。
  ✓ 真實 Chrome（channel="chrome"）+ headful + 持久設定檔：通過指紋檢查，實測 40 次
    查詢 0 驗證碼。headless=True 曾被導到 /sorry/，所以預設 headful。

跟 worker.py 共用同一套佇列協定與窗過濾邏輯（revlib.parse），只換掉抓取來源：
  1. ⚠️ 查詢順序與 worker.py 相反：Google 要「西元年優先」。Google 會直接忽略民國年
     token（頁面顯示「缺少字詞：110」），故 q_ad 單獨命中 70%、q_roc 補 20%、任一 90%。
     兩者互補是結構性的：中央社【公告】標題寫西元年、MoneyDJ 標題寫民國年。
  2. 絕不在查詢後加「營收」二字（同 worker.py，會害 recall）；也絕不加 "moneydj"
     ——實測加了會把 CMoney/中央社/Yahoo 來源的命中排擠掉（任一 90%→80%），純損失。
  3. page.inner_text("body") 取整頁可見文字（標題+摘要+相關搜尋）直接餵 revlib.parse，
     不需要新 parser：它本來就是純文字的窗過濾 + 名稱錨點。
  4. 驗證頁（/sorry/）、導覽失敗、頁面文字過短 → rate_limited（放回佇列），絕不當
     failed：failed 只能是「頁面正常、兩種年份都試過、仍無窗內日期」。
     headful 之下驗證碼可人工解，解完下一筆自動接續（瀏覽器實例不關）。

前置（⚠️ 這是本 repo 第一個第三方依賴；server.py/worker.py/revlib.py 仍維持零依賴）：
  pip3 install --user playwright      # 不需要 playwright install chromium
  需要圖形環境（如 DISPLAY=:0）與系統 Chrome（/usr/bin/google-chrome）

部署到別台機器：複製 google_worker.py + revlib.py，裝 playwright，.env 放同一組
REVSWARM_TOKEN，然後：
  python3 google_worker.py --server http://SERVER:8000

先把 failed 轉交 google 佇列（否則沒有 engine='google' 的任務可租）：
  curl -X POST -H "Authorization: Bearer SECRET" \\
    "http://SERVER:8000/admin/requeue-failed?engine=google"
"""

import argparse
import importlib.util
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import revlib

SEARCH_URL = "https://www.google.com/search"
PROFILE_DIR = "~/.cache/revswarm-chrome"   # 持久設定檔：保留 cookie/同意狀態，降驗證碼
NAV_TIMEOUT_MS = 30000
MIN_TEXT_CHARS = 200      # 低於此視為錯誤頁/被擋；正常 SERP（含 0 結果頁）遠超過
# 被擋/驗證的訊號。刻意只認明確字樣：誤判成 rate_limited 會讓「Google 真的查不到」
# 的任務永遠放回佇列繞圈，所以寧可漏認也不要濫認。
BLOCK_MARKERS = re.compile(
    r"異常流量|我不是機器人|請啟用\s*JavaScript|unusual traffic|verify you are human",
    re.I)

# 長壽的瀏覽器實例（跨任務重用；啟動成本 ~2~4s，不可每筆重開）。由 run() 設定，
# fetch_google() 讀它——與 worker.py 的 PROXY 同樣是「模組全域」，測試可整個換掉。
SEARCHER = None


# --- 瀏覽器 -----------------------------------------------------------------
class Searcher:
    """包一個持久設定檔的 Chrome，提供 search(query) → (text, ok)。"""

    def __init__(self, profile_dir=PROFILE_DIR, headless=False):
        self.profile_dir = os.path.expanduser(profile_dir)
        self.headless = headless
        self._pw = None
        self._ctx = None
        self._page = None

    def start(self):
        # 延後 import：讓 test/其他工具在沒裝 playwright 的機器上也能 import 本模組。
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self.profile_dir,
            channel="chrome",        # 用系統 Chrome，不是 playwright 下載的 Chromium
            headless=self.headless,  # ⚠️ headless 實測會被導到 /sorry/
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.set_default_timeout(NAV_TIMEOUT_MS)

    def close(self):
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass          # 關閉失敗無所謂，程式正要結束
        self._pw = self._ctx = self._page = None

    def _recycle(self):
        """瀏覽器被使用者關掉/當掉時整個重建，免得之後每筆都失敗、永遠 rate_limited。"""
        self.close()

    def search(self, query, timeout=NAV_TIMEOUT_MS):
        """
        回傳 (text, ok)。ok=False = RATE_LIMITED 訊號（驗證頁 / 導覽失敗 / 文字過短）。
        絕不因為「這頁沒有想要的日期」而回 ok=False——那是 parse 的事。
        """
        if self._page is None:
            try:
                self.start()
            except Exception as e:
                # 只有 _recycle 後才會走到這（啟動時的失敗在 open_searcher 就會炸出來）。
                # 重建失敗（例：設定檔被另一個實例鎖住）就當 rate_limited 讓上層退避，
                # 不要讓整批已租的任務跟著崩掉——它們會等租約逾時被回收。
                print(f"  ⚠️ 瀏覽器重建失敗，本筆當 rate_limited：{e!r}")
                self.close()
                return None, False
        url = f"{SEARCH_URL}?" + urllib.parse.urlencode(
            {"q": query, "hl": "zh-TW", "gl": "tw"})
        try:
            # 只等 DOM：Google 的搜尋結果是伺服器端渲染，等 load（含圖片/追蹤）純浪費。
            self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if "/sorry/" in self._page.url:
                return None, False
            # 保險：結果區偶爾晚一步進 DOM。等不到不作為判斷依據，仍往下讀文字。
            try:
                self._page.wait_for_selector("#search", timeout=5000)
            except Exception:
                pass
            text = self._page.inner_text("body")
        except Exception:
            # playwright 的錯誤型別要 import 才拿得到（本模組刻意延後 import），
            # 且這裡任何失敗都保守當 rate_limited，故一律吞掉。
            # 頁面/context 已關（使用者關窗）→ 重建，否則之後每筆都會失敗。
            if self._page is None or self._page.is_closed():
                self._recycle()
            return None, False
        if not text or len(text) < MIN_TEXT_CHARS or BLOCK_MARKERS.search(text):
            return None, False
        return text, True


def open_searcher(args):
    """建立並啟動瀏覽器。獨立成函式，測試可整個換掉（不真的開瀏覽器）。"""
    s = Searcher(args.profile, headless=args.headless)
    s.start()
    return s


def fetch_google(query, timeout=NAV_TIMEOUT_MS):
    """對模組全域的 SEARCHER 查一次；回傳 (text, ok)。"""
    if SEARCHER is None:
        return None, False
    return SEARCHER.search(query, timeout=timeout)


# --- 單筆任務 ---------------------------------------------------------------
def crawl_task(task, per_query_sleep):
    """
    回傳結果 dict：{id, status, date?, source?, title?}
      status ∈ success | failed | rate_limited

    與 worker.py 的 crawl_task 對稱，兩處差異：
      - 查詢順序：西元年（g_ad）優先，民國年（g_roc）補第二（見模組開頭 1.）
      - source 標 g_* 而非 q_*：讓 /status 與匯出資料看得出這筆是 Google 補搜來的
    """
    name = task["name"]
    sid = task["stock_id"]
    ry, rm = task["roc_year"], task["roc_month"]
    # 名稱前綴 4 碼股票代號：冷門股/舊月份命中率較純名稱高，也消歧同名公司。
    # 解析錨點仍只用 name（見 revlib.parse）。
    queries = [
        ("g_ad", f"{sid} {name} {ry + 1911}年{rm}月"),
        ("g_roc", f"{sid} {name} {ry}年{rm}月"),
    ]
    saw_rate_limited = False
    tried_ok = False
    for i, (variant, q) in enumerate(queries):
        if i > 0:
            time.sleep(per_query_sleep + random.uniform(0, 1.0))
        text, ok = fetch_google(q)
        if not ok:
            saw_rate_limited = True
            continue
        tried_ok = True
        hit = revlib.parse(text, name, ry, rm)
        if hit:
            date, title = hit
            return {"id": task["id"], "status": "success",
                    "date": date, "source": variant, "title": title}
    # 走到這：兩種查詢都沒中窗內日期。
    if saw_rate_limited or not tried_ok:
        # 有任一查詢被擋（可能正好漏掉命中）→ 保守放回重試，不判 failed。
        return {"id": task["id"], "status": "rate_limited"}
    # 兩種年份都試過、頁面都正常、仍無窗內日期 → 真的找不到。
    return {"id": task["id"], "status": "failed"}


# --- 與 server 溝通 ---------------------------------------------------------
class Client:
    def __init__(self, base, token, worker_id):
        self.base = base.rstrip("/")
        self.token = token
        self.worker_id = worker_id

    def _req(self, method, path, body=None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def lease(self, n):
        # engine=google：只租 requeue-failed?engine=google 轉過來的任務，
        # 不會跟 worker.py（engine=yahoo）搶同一批 undone（見 server.py Store.lease）。
        return self._req("POST", f"/lease?n={n}&worker={self.worker_id}&engine=google")

    def report(self, results):
        return self._req("POST", "/result",
                         {"worker": self.worker_id, "results": results})


# --- 主迴圈 -----------------------------------------------------------------
def run(args):
    global SEARCHER
    worker_id = args.worker_id or f"{socket.gethostname()}-{os.getpid()}-g"
    client = Client(args.server, args.token, worker_id)
    print(f"google_worker {worker_id} → {args.server}  batch={args.batch} "
          f"delay={args.delay}+{args.jitter}s rl_threshold={args.rl_threshold}")

    SEARCHER = open_searcher(args)
    try:
        _loop(args, client)
    finally:
        SEARCHER.close()
        SEARCHER = None


def _loop(args, client):
    batches_done = 0
    while True:
        # --- 租一批 ---
        try:
            resp = client.lease(args.batch)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  lease 失敗，10s 後重試：{e!r}")
            time.sleep(10)
            continue
        batch = resp.get("tasks", [])
        if not batch:
            print(f"  google 佇列已空/無可租任務（記得先 requeue-failed?engine=google），"
                  f"{args.idle_sleep}s 後再試。")
            time.sleep(args.idle_sleep)
            continue

        # --- 逐筆爬 ---
        results = []
        consec_rl = 0
        blocked = False
        for task in batch:
            r = crawl_task(task, args.per_query_sleep)
            results.append(r)
            tag = {"success": "✓", "failed": "—", "rate_limited": "×"}[r["status"]]
            extra = f" {r.get('date','')} [{r.get('source','')}]" if r["status"] == "success" else ""
            print(f"  {tag} {task['stock_id']} {task['name']} "
                  f"{task['roc_year']}/{task['roc_month']}{extra}")

            if r["status"] == "rate_limited":
                consec_rl += 1
                # 指數退避（上限 60s）+ 抖動。連續多筆多半是整個 IP 被要求驗證。
                back = min(2 ** consec_rl, 60) + random.uniform(0, 2)
                time.sleep(back)
                if consec_rl >= args.rl_threshold:
                    print(f"  ⚠️ 連續 {consec_rl} 筆 rate_limited，研判被 Google 要求驗證，"
                          f"提早結束本批並長睡 {args.block_sleep}s。\n"
                          f"     視窗還開著：可人工解掉驗證碼，長睡結束後自動接續。")
                    blocked = True
                    break
            else:
                consec_rl = 0
                time.sleep(args.delay + random.uniform(0, args.jitter))

        # 提早中止時，剩餘未爬的租約以 rate_limited 立刻放回（免等租約逾時）。
        if blocked:
            done_ids = {r["id"] for r in results}
            for task in batch:
                if task["id"] not in done_ids:
                    results.append({"id": task["id"], "status": "rate_limited"})

        # --- 回報 ---
        try:
            applied = client.report(results)
            print(f"  回報：{applied.get('applied')}")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  回報失敗（租約逾時後 server 會自動回收）：{e!r}")

        batches_done += 1
        if args.once or (args.max_batches and batches_done >= args.max_batches):
            print(f"完成 {batches_done} 批，結束。")
            return
        if blocked:
            time.sleep(args.block_sleep)


def main():
    revlib.load_env()          # 先載 .env，讓 REVSWARM_TOKEN 免手打
    ap = argparse.ArgumentParser(description="revswarm google_worker（Playwright + 系統 Chrome）")
    ap.add_argument("--server", required=True, help="server base URL, 如 http://1.2.3.4:8000")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"),
                    help="Bearer token；預設讀 .env / 環境變數 REVSWARM_TOKEN")
    ap.add_argument("--worker-id", default=None, help="預設 hostname-pid-g")
    # batch 預設比 worker.py 小：本 worker 每筆約 12~20s，而 server 的租約 TTL 是 600s，
    # 一批跑超過 TTL 會被惰性回收、可能被別台重派（做白工）。15 筆 ≈ 最多 300s，留足餘裕。
    ap.add_argument("--batch", type=int, default=15, help="每次租多少筆")
    ap.add_argument("--delay", type=float, default=6.0, help="每筆任務間基礎延遲秒")
    ap.add_argument("--jitter", type=float, default=4.0,
                    help="每筆延遲的隨機抖動上限秒（預設 6+0~4 = 6~10s）")
    ap.add_argument("--per-query-sleep", type=float, default=6.0,
                    help="同一任務內兩次查詢間的延遲秒")
    ap.add_argument("--rl-threshold", type=int, default=3,
                    help="連續幾筆 rate_limited 判定被要求驗證")
    ap.add_argument("--block-sleep", type=float, default=1800.0,
                    help="判定被擋後長睡秒數（給人工解驗證碼的時間）")
    ap.add_argument("--idle-sleep", type=float, default=30.0,
                    help="佇列空時的等待秒數")
    ap.add_argument("--once", action="store_true", help="只跑一批就結束（測試用）")
    ap.add_argument("--max-batches", type=int, default=0, help="跑幾批後結束(0=不限)")
    ap.add_argument("--profile", default=PROFILE_DIR,
                    help="Chrome 持久設定檔目錄（保留 cookie，降低驗證碼）")
    ap.add_argument("--headless", action="store_true",
                    help="⚠️ 僅供除錯：實測 headless 會被 Google 導到 /sorry/ 驗證頁")
    args = ap.parse_args()

    # 先確認依賴在，才給得出清楚的錯誤訊息（真正的 import 在 Searcher.start()）。
    if importlib.util.find_spec("playwright") is None:
        print("⚠️ 需要 playwright：pip3 install --user playwright"
              "（不必 playwright install，本 worker 用系統的 google-chrome）",
              file=sys.stderr)
        sys.exit(1)
    if not args.headless and not os.environ.get("DISPLAY"):
        print("⚠️ 沒有 DISPLAY：headful Chrome 需要圖形環境（例 DISPLAY=:0 python3 "
              "google_worker.py ...）。headless 會被 Google 擋，不建議。", file=sys.stderr)
        sys.exit(1)

    try:
        run(args)
    except KeyboardInterrupt:
        print("\n收到中斷，結束。")


if __name__ == "__main__":
    main()

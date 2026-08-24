#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm google_worker：向 server 租 engine='google' 的任務、用 Playwright 驅動「系統安裝的
Chrome」查 Google 搜尋、回報結果。用來二次補搜 yahoo_worker.py（爬 Yahoo）救不回的 failed 任務
——手動抽測 10 筆真實 failed，Google 命中 9 筆。

為什麼是 Playwright 而不是 API 或 curl（三條死路，別重走，見 todo.txt 9.2）：
  ✗ Google Custom Search JSON API：已對新客戶關閉（2027-01 全面停用），一律回
    403 "This project does not have the access to Custom Search JSON API."
    換金鑰/換專案都無效。
  ✗ curl 直接爬 www.google.com/search：每次都被轉去 /httpservice/retry/enablejs，
    因為 Google 在核對 Client Hints / TLS 指紋，帶 Chrome UA 也偽裝不了；換 IP 無效。
  ✓ 真實 Chrome（channel="chrome"）+ headful + 持久設定檔：通過指紋檢查，實測 40 次
    查詢 0 驗證碼。headless=True 曾被導到 /sorry/，所以預設 headful。

跟 yahoo_worker.py 共用同一套佇列協定與窗過濾邏輯（revlib.parse），只換掉抓取來源：
  1. ⚠️ 查詢順序與 yahoo_worker.py 相反：Google 要「西元年優先」。Google 會直接忽略民國年
     token（頁面顯示「缺少字詞：110」），故 q_ad 單獨命中 70%、q_roc 補 20%、任一 90%。
     兩者互補是結構性的：中央社【公告】標題寫西元年、MoneyDJ 標題寫民國年。
  2. 絕不在查詢後加「營收」二字（同 yahoo_worker.py，會害 recall）；也絕不加 "moneydj"
     ——實測加了會把 CMoney/中央社/Yahoo 來源的命中排擠掉（任一 90%→80%），純損失。
  3. page.inner_text("body") 取整頁可見文字（標題+摘要+相關搜尋）直接餵 revlib.parse，
     不需要新 parser：它本來就是純文字的窗過濾 + 名稱錨點。
  4. 驗證頁（/sorry/）、導覽失敗、頁面不是 SERP（沒有 #search 容器，例如全新設定檔
     第一次跑遇到的 Google 同意頁）→ rate_limited（放回佇列），絕不當 failed：
     failed 只能是「頁面是正常 SERP、兩種年份都試過、仍無窗內日期」。
  5. ⚠️ 撞到需要人工處理的頁面就「立刻停止導覽」：不再取下一筆任務，畫面就停在那頁
     等人解。之後每 --captcha-poll 秒只『讀』一次當前頁判斷是否解除（不導覽，才不會把
     使用者正在輸入的頁面換掉），解掉就馬上接續。
     這是實戰踩到的兩層：原本要連續 3 筆 rate_limited 才停批，期間每筆的 goto 都會蓋掉
     驗證頁；改成「撞到就停」之後又發現驗證頁不一定認得出是 CAPTCHA（網址不在 /sorry/、
     文字也沒 BLOCK_MARKERS 時只會被分類成 NOT_SERP），於是走了無條件長睡那條、完全
     不輪詢，人解掉了照樣等滿 1800s。所以「要不要等人」改成問當前頁的實際狀態，
     不看 reason（見 should_wait_for_human）。

前置（⚠️ 這是本 repo 第一個第三方依賴；server.py/yahoo_worker.py/revlib.py 仍維持零依賴）：
  pip3 install --user playwright      # 不需要 playwright install chromium
  需要圖形環境（如 DISPLAY=:0）與系統 Chrome（/usr/bin/google-chrome）

部署到別台機器：git clone 本 repo，裝 playwright，.env 放同一組 REVSWARM_TOKEN，
然後從 repo 根目錄：
  python3 -m worker.google_worker --server http://SERVER:8000

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
# 導覽逾時 20s：實測 Google 要嘛 ~1s 回、要嘛給 /sorry/，等更久沒有意義，只會讓
# 「整批耗時 > 租約 TTL(600s)」的風險變高（見 --batch 的說明）。
NAV_TIMEOUT_MS = 20000
SERP_SELECTOR = "#search"     # 結果容器；「這頁到底是不是 SERP」的判準（見 search()）
SERP_TIMEOUT_MS = 5000
# search() 失敗的三種原因。全部一律當 rate_limited（放回佇列），差別只在「要不要立刻停批」：
#   CAPTCHA 是 Google 明確要求人工驗證 → 必須馬上停，再導覽會蓋掉使用者正在解的頁面
#   其餘兩種可能只是一時的 → 沿用 --rl-threshold 的連續門檻
BLOCK_CAPTCHA = "captcha"     # /sorry/ 驗證頁，或頁面出現「異常流量」等字樣
BLOCK_NOT_SERP = "not_serp"   # 不是搜尋結果頁（同意頁 / enablejs / 空白頁）
BLOCK_NAV = "nav"             # 導覽逾時 / 視窗被關 / 瀏覽器重建失敗
# 被擋/驗證的訊號。刻意只認明確字樣：誤判成 rate_limited 會讓「Google 真的查不到」
# 的任務永遠放回佇列繞圈，所以寧可漏認也不要濫認。
BLOCK_MARKERS = re.compile(
    r"異常流量|我不是機器人|請啟用\s*JavaScript|unusual traffic|verify you are human",
    re.I)

# 長壽的瀏覽器實例（跨任務重用；啟動成本 ~2~4s，不可每筆重開）。由 run() 設定，
# fetch_google() 讀它——與 yahoo_worker.py 的 PROXY 同樣是「模組全域」，測試可整個換掉。
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
                    pass          # 關不掉也只能放手（本函式也用於中途重建，不只收尾）
        self._pw = self._ctx = self._page = None

    def _recycle(self):
        """瀏覽器被使用者關掉/當掉時整個重建，免得之後每筆都失敗、永遠 rate_limited。"""
        self.close()

    def _page_dead(self):
        """頁面/context 是否已經沒了（使用者關窗、瀏覽器當掉）。判斷本身不可拋例外。"""
        try:
            return self._page is None or self._page.is_closed()
        except Exception:
            return True

    def _live_url(self):
        """
        當前頁的**真實**網址（一次真的 IPC）。

        ⚠️ 絕不可用 page.url：那是 Playwright 快取的值，人在畫面上自己操作（解掉驗證後
        被導向 continue 網址）時不保證更新。實測踩到：人已回到正常 SERP、網址列也是
        /search?q=…，但 page.url 仍回報舊的 /sorry/…。而 is_blocked_now 只要信了它就
        提早 return True，永遠不會做那次會刷新快取的 IPC——自己把自己鎖在舊值裡，
        於是永遠判定「還停在驗證頁」，人解掉了也不會接續。
        """
        return self._page.evaluate("() => location.href")

    def is_blocked_now(self):
        """
        當前畫面是不是「不能用的頁」（驗證頁 / 同意頁 / 攔阻 / 空白）。

        ⚠️ 只讀當前頁、絕不導覽——這正是它存在的理由：人工在驗證頁上輸入時，
        不能有任何 goto 把頁面換掉（見 wait_until_unblocked）。
        """
        if self._page_dead():
            return False              # 視窗都沒了，等下去也沒意義
        try:
            if "/sorry/" in self._live_url():
                return True
            if self._page.query_selector(SERP_SELECTOR) is None:
                return True           # 同意頁 / enablejs / 空白頁
            return bool(BLOCK_MARKERS.search(self._page.inner_text("body")))
        except Exception:
            # ⚠️ 讀不到就當「還被擋」：誤判成已解除會讓主迴圈導覽下一筆、蓋掉使用者
            # 正在輸入的驗證頁；誤判成還被擋只是多等一個 poll。兩邊代價不對稱。
            return True

    def block_note(self):
        """
        給 log 用的一行說明：現在為什麼判定被擋（只讀，不導覽）。

        存在的理由：is_blocked_now() 只回 True/False，卡住時無從得知是哪一條判準在擋
        （網址?容器?字樣?），而這個機制只在真人面前出錯，事後沒有現場可查。
        """
        try:
            url = self._live_url()          # 不用 page.url：那是快取值（見 _live_url）
        except Exception as e:
            return f"讀不到當前頁：{e!r}"
        if "/sorry/" in url:
            return f"停在驗證頁 {url[:90]}"
        try:
            if self._page.query_selector(SERP_SELECTOR) is None:
                return f"沒有 {SERP_SELECTOR} 容器（同意頁／enablejs／空白頁）：{url[:90]}"
        except Exception as e:
            return f"讀不到 DOM：{e!r}"
        return f"頁面出現攔阻字樣：{url[:90]}"

    def search(self, query, timeout=NAV_TIMEOUT_MS):
        """
        回傳 (text, ok, reason)：
          ok=True  → (整頁可見文字, True, None)
          ok=False → (None, False, BLOCK_*)，一律當 RATE_LIMITED 放回佇列，絕不 failed
        絕不因為「這頁沒有想要的日期」而回 ok=False——那是 parse 的事。
        reason 的唯一用途是讓主迴圈知道「這次要不要立刻停批等人工」。
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
                return None, False, BLOCK_NAV
        url = f"{SEARCH_URL}?" + urllib.parse.urlencode(
            {"q": query, "hl": "zh-TW", "gl": "tw"})
        try:
            # 只等 DOM：Google 的搜尋結果是伺服器端渲染，等 load（含圖片/追蹤）純浪費。
            self._page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except Exception:
            # playwright 的錯誤型別要 import 才拿得到（本模組刻意延後 import），
            # 且導覽失敗一律保守當 rate_limited。視窗被關 → 重建。
            if self._page_dead():
                self._recycle()
            return None, False, BLOCK_NAV
        if "/sorry/" in self._page.url:
            return None, False, BLOCK_CAPTCHA
        try:
            # ⚠️ 「這頁是不是真的 SERP」只認結果容器 #search，不用文字長度判斷。
            # 實測：連「幾乎沒有結果」的查詢都有 #search/#rso，但整頁可見文字只有 326 字
            # ——用字數當門檻兩頭都會錯：
            #   高門檻 → 正常的稀疏結果頁被誤判 rate_limited。而 lease 是最舊優先，
            #            被放回的任務下一批又排最前面，會永久重試、永遠不會定案。
            #   低門檻 → 更糟：全新設定檔的第一次查詢會遇到 Google 同意頁
            #            (consent.google.com)，它字數遠超門檻、網址不含 /sorry/、
            #            也沒有任何 BLOCK_MARKERS 字樣 → 被當成正常頁 → 找不到窗內日期
            #            → 誤記成真的 failed。那會把「還沒查成功」寫成「Google 也沒有」，
            #            靜靜污染研究資料。同意頁/enablejs 轉址頁都沒有 #search。
            # 等不到 #search 就是「不是 SERP」，一樣當 rate_limited。
            self._page.wait_for_selector(SERP_SELECTOR, timeout=SERP_TIMEOUT_MS)
            text = self._page.inner_text("body")
        except Exception:
            if self._page_dead():        # 視窗被關 → 重建，否則之後每筆都會失敗
                self._recycle()
                return None, False, BLOCK_NAV
            return None, False, BLOCK_NOT_SERP
        if not text:
            return None, False, BLOCK_NOT_SERP
        if BLOCK_MARKERS.search(text):
            # 網址還在 /search 但頁面寫著「異常流量」等字樣：跟 /sorry/ 同一件事，
            # 都需要人工處理，所以歸為 CAPTCHA 讓主迴圈立刻停批。
            return None, False, BLOCK_CAPTCHA
        return text, True, None


def open_searcher(args):
    """建立並啟動瀏覽器。獨立成函式，測試可整個換掉（不真的開瀏覽器）。"""
    s = Searcher(args.profile, headless=args.headless)
    s.start()
    return s


def fetch_google(query, timeout=NAV_TIMEOUT_MS):
    """對模組全域的 SEARCHER 查一次；回傳 (text, ok, reason)。"""
    if SEARCHER is None:
        return None, False, BLOCK_NAV
    return SEARCHER.search(query, timeout=timeout)


def blocked_now():
    """
    對模組全域的 SEARCHER 問「當前頁現在還停在壞頁嗎」（不導覽）。

    沒有瀏覽器 → False：那時沒有任何頁面可讓人解，不該因此停批。
    """
    return SEARCHER is not None and SEARCHER.is_blocked_now()


def should_wait_for_human():
    """
    收批後該「等人處理」還是「無條件長睡退避」？

    ⚠️ 判準是當前頁的實際狀態，**不是** search() 回的 reason。reason 只描述導覽當下
    的分類，而「有沒有東西可讓人解」是此刻的頁面狀態——實測 Google 的驗證頁不一定在
    /sorry/、也不一定有 BLOCK_MARKERS 字樣，那時它只會被分類成 NOT_SERP，若照 reason
    分流就會走到無條件 sleep(block_sleep)、完全不輪詢，人解掉了也不會接續（實測：解完
    等滿 1800s 才動）。
      還停在壞頁（驗證／同意／空白）→ True，輪詢，解掉的瞬間就接續
      視窗已經沒了                   → True，長睡毫無意義，讓下一批去重建瀏覽器
      頁面正常（純粹連續導覽失敗）   → False，沒東西可解，照舊長睡，別燒自己 IP
    """
    if SEARCHER is None:
        return True
    return SEARCHER.is_blocked_now() or SEARCHER._page_dead()


def wait_until_unblocked(searcher, limit, poll=5.0):
    """
    把畫面留在驗證頁／同意頁上等人工處理，解掉就立刻回來（回 True），
    等滿 limit 秒仍沒解除回 False。

    每 poll 秒只「讀」一次當前頁（is_blocked_now 不導覽），所以不會打斷輸入。
    這取代原本無條件 sleep(block_sleep)：驗證碼解完還要再乾等 30 分鐘毫無意義，
    而真的無人看顧時行為與原本相同（等滿）。
    """
    poll = max(poll, 0.5)      # 0 會讓 waited 永遠不前進 → 空轉不會結束
    waited = 0.0
    while waited < limit:
        time.sleep(min(poll, limit - waited))
        waited += poll
        if searcher is None or not searcher.is_blocked_now():
            return True
    return False


# --- 單筆任務 ---------------------------------------------------------------
def crawl_task(task, per_query_sleep):
    """
    回傳 (result, stop_batch)：
      result     = {id, status, date?, source?, title?}，status ∈ success|failed|rate_limited
      stop_batch = 是否該立刻停止導覽、把畫面留給人處理

    與 yahoo_worker.py 的 crawl_task 有三處差異：
      - 查詢順序：西元年（g_ad）優先，民國年（g_roc）補第二（見模組開頭 1.）
      - source 標 g_* 而非 q_*：讓 /status 與匯出資料看得出這筆是 Google 補搜來的
      - ⚠️ 多回一個 stop_batch（yahoo_worker.py 只回 dict）：Google 的驗證要人工解，
        撞到就必須「立刻停止導覽」，否則那個 goto 會把使用者正在解的驗證頁蓋掉。
        Yahoo 那邊被擋不需要人介入，所以沒這問題。

    stop_batch 有兩個觸發點，寬嚴不同（實測調過）：
      - CAPTCHA：第一次就停，連第二種年份都不打（那次 goto 一定蓋掉驗證頁）
      - 其他被擋：**打完第二種年份**才問「畫面現在還壞著嗎」。多留這一次 fresh goto
        是為了讓偶發的空白頁自己復原，不要一次抖動就收掉整批；但也只留這一次——
        驗證頁不一定認得出是 CAPTCHA（見 should_wait_for_human），繼續往下打就又回到
        「每筆 goto 都蓋掉使用者正在解的頁」的老問題。
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
        text, ok, reason = fetch_google(q)
        if not ok:
            saw_rate_limited = True
            if reason == BLOCK_CAPTCHA:
                # 立刻收手：第二種年份也會撞同一個驗證頁，而且那次 goto 會蓋掉它。
                return {"id": task["id"], "status": "rate_limited"}, True
            continue
        tried_ok = True
        hit = revlib.parse(text, name, ry, rm)
        if hit:
            date, title = hit
            return {"id": task["id"], "status": "success",
                    "date": date, "source": variant, "title": title}, False
    # 走到這：兩種查詢都沒中窗內日期。
    if saw_rate_limited or not tried_ok:
        # 有任一查詢被擋（可能正好漏掉命中）→ 保守放回重試，不判 failed。
        # 兩種年份都打完了還停在壞頁 → 通報停批（見 docstring 的兩個觸發點）。
        return {"id": task["id"], "status": "rate_limited"}, blocked_now()
    # 兩種年份都試過、頁面都正常、仍無窗內日期 → 真的找不到。
    return {"id": task["id"], "status": "failed"}, False


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
        # 不會跟 yahoo_worker.py（engine=yahoo）搶同一批 undone（見 server.py Store.lease）。
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
        stop_for_human = False      # 停批是因為「畫面停在要人處理的頁」（見收批後的等法）
        for task in batch:
            r, stop_batch = crawl_task(task, args.per_query_sleep)
            results.append(r)
            tag = {"success": "✓", "failed": "—", "rate_limited": "×"}[r["status"]]
            extra = f" {r.get('date','')} [{r.get('source','')}]" if r["status"] == "success" else ""
            print(f"  {tag} {task['stock_id']} {task['name']} "
                  f"{task['roc_year']}/{task['roc_month']}{extra}")

            if stop_batch:
                # ⚠️ 不套 --rl-threshold：那要連續 3 筆才停，期間每筆的 goto 都會蓋掉
                # 使用者正在解的驗證頁（等於根本解不完）。Google 要求驗證代表整個 IP
                # 被攔，繼續打也只是拿到同一頁。停批後不再有任何導覽。
                print(f"  ⚠️ 畫面停在需要人工處理的頁面（驗證／同意頁）。已立刻停批，不再導覽。\n"
                      f"     請在 Chrome 視窗處理掉——完成會自動接續"
                      f"（最多等 {args.block_sleep:.0f}s，每 {args.captcha_poll:.0f}s 檢查一次）。")
                blocked = stop_for_human = True
                break

            if r["status"] == "rate_limited":
                consec_rl += 1
                # 非驗證碼的失敗（導覽逾時、不是 SERP…）可能只是一時的，仍用連續門檻。
                back = min(2 ** consec_rl, 60) + random.uniform(0, 2)
                time.sleep(back)
                if consec_rl >= args.rl_threshold:
                    print(f"  ⚠️ 連續 {consec_rl} 筆 rate_limited（非驗證碼），"
                          f"提早結束本批，最多等 {args.block_sleep:.0f}s。")
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
            # 要不要等人：先看「為什麼停批」這個已知事實，才看當前頁狀態。
            # ⚠️ stop_for_human 不可以用「再讀一次當前頁」取代：停批到這裡之間有一次
            #    client.report() 的網路往返（約 1~2s），人很可能正好在那一兩秒把驗證解掉，
            #    那時重讀會得到「頁面正常」→ 誤判成單純連續失敗 → 無條件長睡 30 分鐘。
            #    實測踩過：使用者解完驗證，worker 卻一次都沒輪詢就長睡。
            if stop_for_human or should_wait_for_human():
                # 壞頁停在畫面上等人處理；解掉就馬上接續，不必乾等剩下的 block_sleep。
                if SEARCHER is not None:
                    print(f"  ⏸ 等人處理（{SEARCHER.block_note()}）")
                if wait_until_unblocked(SEARCHER, args.block_sleep, args.captcha_poll):
                    print("  ✔ 已解除，接續下一批。")
                else:
                    print(f"  仍停在被擋的頁面（等滿 {args.block_sleep:.0f}s），還是接續試下一批。")
            else:
                # 畫面是正常的 → 純粹連續導覽失敗，沒有東西可解，照舊長睡。
                # ⚠️ 這條不可改成輪詢——is_blocked_now 會立刻判定「沒被擋」而秒回，
                #    等於拿掉「別燒自己 IP」的保護。
                print(f"  頁面正常但連續失敗，長睡 {args.block_sleep:.0f}s 退避。")
                time.sleep(args.block_sleep)


def main():
    revlib.load_env()          # 先載 .env，讓 REVSWARM_TOKEN 免手打
    ap = argparse.ArgumentParser(description="revswarm google_worker（Playwright + 系統 Chrome）")
    ap.add_argument("--server", required=True, help="server base URL, 如 http://1.2.3.4:8000")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"),
                    help="Bearer token；預設讀 .env / 環境變數 REVSWARM_TOKEN")
    ap.add_argument("--worker-id", default=None, help="預設 hostname-pid-g")
    # batch 預設遠比 yahoo_worker.py 小，因為一批必須在租約 TTL(600s) 內回報完，否則會被惰性
    # 回收、可能被別台重派做白工。算最壞情況（用 20s 導覽逾時）：
    #   某筆 g_ad 逾時 20s + per-query-sleep 最多 7s + g_roc 命中 ~2s + delay 最多 20s ≈ 49s，
    #   而這種筆數每次都有命中 → consec_rl 被歸零 → --rl-threshold 的保險絲不會跳。
    #   10 筆 × 49s ≈ 490s < 600s，仍有餘裕但已比 delay=6~10s 時窄（原本 380s）；
    #   12 筆就會逼近 590s，所以調高 --delay/--jitter 時別忘了同步調低 --batch。
    # （兩個查詢都逾時的筆數約 47s，但那會連續 rate_limited、3 筆就提早收批，不累積。）
    ap.add_argument("--batch", type=int, default=10, help="每次租多少筆")
    ap.add_argument("--delay", type=float, default=10.0, help="每筆任務間基礎延遲秒")
    ap.add_argument("--jitter", type=float, default=10.0,
                    help="每筆延遲的隨機抖動上限秒（預設 10+0~10 = 10~20s）")
    ap.add_argument("--per-query-sleep", type=float, default=6.0,
                    help="同一任務內兩次查詢間的延遲秒")
    ap.add_argument("--rl-threshold", type=int, default=3,
                    help="連續幾筆「非驗證碼」的 rate_limited 才提早收批"
                         "（驗證碼一次就停，不套這個門檻）")
    ap.add_argument("--block-sleep", type=float, default=1800.0,
                    help="被擋後最多等多久（等人工解驗證碼；解掉就立刻接續，不等滿）")
    ap.add_argument("--captcha-poll", type=float, default=5.0,
                    help="等待期間每幾秒檢查一次驗證是否已解除（只讀當前頁，不導覽）")
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

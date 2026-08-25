#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
google_worker 的分類邏輯、查詢順序、Searcher 判擋與主迴圈回歸測試。純標準庫 unittest：
不連網、不真的開瀏覽器、不真的睡（也不需要裝 playwright——模組是延後 import 的）。

作法沿用 test_worker.py：替換 google_worker 模組命名空間裡的 time / random /
fetch_google / open_searcher / Client 為假物件，就能確定性地驗證：
  - crawl_task 的四種分類，以及 ⚠️「西元年優先」的查詢順序（Google 忽略民國年 token）
  - Searcher.search 的 reason 分類（CAPTCHA / NOT_SERP / NAV）一律 ok=False，絕不 failed
  - ⚠️ 撞驗證碼要「第一筆就停、不再導覽」，以及 is_blocked_now 判斷期間不可導覽
    （否則會蓋掉使用者正在輸入的驗證頁——這是實戰踩到的）
  - 解掉驗證後 wait_until_unblocked 立刻接續；非驗證碼的連續失敗則照舊長睡
  - 主迴圈的指數退避序列、連續 rate_limited 判定被擋、剩餘任務放回、瀏覽器有關掉

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_google_worker
"""

import contextlib
import io
import types
import unittest

from worker import google_worker


class _FakeTime:
    """記錄 sleep 秒數，不真的睡。"""
    def __init__(self):
        self.slept = []

    def sleep(self, s):
        self.slept.append(round(s, 3))


class _FakeRandom:
    """抖動固定為 0，讓退避數字乾淨可斷言。"""
    @staticmethod
    def uniform(a, b):
        return 0.0


class _FakeSearcher:
    """假瀏覽器：只記錄有沒有被關掉（真正的查詢由 fetch_google 的替身負責）。
    blocked_seq 可指定 is_blocked_now() 依序回傳什麼，模擬人工解掉驗證的過程。"""
    def __init__(self, blocked_seq=()):
        self.closed = False
        self.blocked_seq = list(blocked_seq)
        self.block_checks = 0

    def close(self):
        self.closed = True

    def is_blocked_now(self):
        self.block_checks += 1
        return self.blocked_seq.pop(0) if self.blocked_seq else False

    def _page_dead(self):
        return False        # 視窗還在（關窗的情境由 TestIsBlockedNowAndWait 蓋）

    def block_note(self):
        return "fake"       # 只給 log 用


def _page_text(name, roc_year, roc_month):
    """組一段含精確標題錨點 + 窗內日期的假 SERP 文字（供 revlib.parse 命中）。"""
    wy = roc_year + 1911 if roc_month <= 11 else roc_year + 1912
    wm = roc_month + 1 if roc_month <= 11 else 1
    return (f"{name} {roc_year}年{roc_month}月營收 - MoneyDJ理財網\n"
            f"新聞 {wy}年{wm}月10日 發布") + "\n其他搜尋結果" * 30


class TestCrawlTask(unittest.TestCase):
    def setUp(self):
        self._time, self._random, self._fetch = (
            google_worker.time, google_worker.random, google_worker.fetch_google)
        self._searcher = google_worker.SEARCHER
        google_worker.time = _FakeTime()
        google_worker.random = _FakeRandom()
        # 明示前提：沒有瀏覽器 → blocked_now() 一律 False，不倚賴別的測試留下的狀態。
        google_worker.SEARCHER = None

    def tearDown(self):
        google_worker.time, google_worker.random, google_worker.fetch_google = (
            self._time, self._random, self._fetch)
        google_worker.SEARCHER = self._searcher

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_ad_query_goes_first(self):
        """⚠️ Google 必須「西元年優先」（民國年 token 會被 Google 忽略）：
        第一次查詢就要是西元年，且命中即停、不再多打一次。"""
        seen = []

        def fetch(q, timeout=google_worker.NAV_TIMEOUT_MS):
            seen.append(q)
            return (_page_text("台積電", 109, 1), True, None)
        google_worker.fetch_google = fetch
        r, blocked = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertFalse(blocked)
        self.assertEqual(len(seen), 1)                      # 命中就停
        self.assertIn("2020年1月", seen[0])                  # 西元年
        self.assertIn("2330 台積電", seen[0])                # 代號前綴
        self.assertNotIn("營收", seen[0])                    # 絕不加「營收」二字
        self.assertNotIn("moneydj", seen[0].lower())        # 也絕不加 moneydj
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["date"], "2020-02-10")
        self.assertEqual(r["source"], "g_ad")

    def test_success_via_roc_fallback(self):
        """西元年頁面無窗內日期、民國年才命中 → success，source=g_roc。"""
        def fetch(q, timeout=google_worker.NAV_TIMEOUT_MS):
            if "109年" in q:                                # 民國年查詢
                return (_page_text("台積電", 109, 1), True, None)
            return ("台積電 沒有日期的頁面" + "x" * 500, True, None)  # 正常頁但無窗內日期
        google_worker.fetch_google = fetch
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["source"], "g_roc")

    def test_failed_when_both_ok_but_no_date(self):
        """兩種年份頁面都正常、都無窗內日期 → 真的 failed。"""
        google_worker.fetch_google = lambda q, timeout=None: (
            "正常頁但沒有任何窗內日期" + "x" * 500, True, None)
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "failed")

    def test_rate_limited_when_all_blocked(self):
        """兩種查詢都被擋（fetch_google ok=False）→ rate_limited，不判 failed。"""
        google_worker.fetch_google = lambda q, timeout=None: (
            None, False, google_worker.BLOCK_NOT_SERP)
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")

    def test_rate_limited_when_partial_block_no_hit(self):
        """西元年正常但無日期、民國年被擋 → 保守回 rate_limited（民國年可能本會命中）。"""
        def fetch(q, timeout=None):
            if "109年" in q:
                return (None, False, google_worker.BLOCK_NAV)   # 民國年被擋
            return ("正常頁無窗內日期" + "x" * 500, True, None)
        google_worker.fetch_google = fetch
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")

    def test_captcha_stops_immediately_without_second_query(self):
        """⚠️ 實戰踩到的：撞驗證碼必須立刻收手，連第二種年份都不能再打——
        那次 goto 會把使用者正在解的驗證頁蓋掉，等於根本解不完。"""
        seen = []

        def fetch(q, timeout=None):
            seen.append(q)
            return (None, False, google_worker.BLOCK_CAPTCHA)
        google_worker.fetch_google = fetch
        r, blocked = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")      # 絕不是 failed
        self.assertTrue(blocked)                           # 要通報主迴圈停批
        self.assertEqual(len(seen), 1)                     # 只打了第一個查詢就停

    def test_non_captcha_block_does_not_stop_batch(self):
        """非驗證碼的被擋（導覽逾時等）不該立刻停批，仍走 --rl-threshold 的門檻。"""
        google_worker.fetch_google = lambda q, timeout=None: (
            None, False, google_worker.BLOCK_NAV)
        r, blocked = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")
        self.assertFalse(blocked)

    def test_stops_batch_when_page_still_bad_after_both_queries(self):
        """⚠️ 實戰第二層：驗證頁不一定認得出是 CAPTCHA（網址不在 /sorry/、文字也沒
        BLOCK_MARKERS → 只會被分類成 NOT_SERP）。那時要靠「兩種年份都打完後畫面還壞著」
        來停批，否則又回到「每筆 goto 都蓋掉使用者正在解的頁」。"""
        seen = []

        def fetch(q, timeout=None):
            seen.append(q)
            return (None, False, google_worker.BLOCK_NOT_SERP)
        google_worker.fetch_google = fetch
        google_worker.SEARCHER = _FakeSearcher(blocked_seq=[True])

        r, blocked = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")       # 絕不是 failed
        self.assertTrue(blocked)                            # 要通報主迴圈停批
        self.assertEqual(len(seen), 2)                      # 但仍留了第二種年份那次重試

    def test_does_not_stop_batch_when_page_recovered(self):
        """反向：偶發的空白頁若自己復原了（畫面已是正常 SERP）就不該收掉整批——
        否則一次抖動就損失整批的吞吐。"""
        google_worker.fetch_google = lambda q, timeout=None: (
            None, False, google_worker.BLOCK_NOT_SERP)
        google_worker.SEARCHER = _FakeSearcher()            # is_blocked_now() → False

        r, blocked = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")
        self.assertFalse(blocked)

    def test_no_searcher_is_rate_limited_not_failed(self):
        """瀏覽器還沒起（SEARCHER=None）→ rate_limited，絕不能悄悄記成 failed。"""
        google_worker.SEARCHER = None    # 明示前提，不倚賴別的測試類別清乾淨
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")


class _FakePage:
    """假 playwright page：可指定導覽後的 url、頁面文字、有沒有 #search 結果容器，
    或讓 goto 直接拋錯。has_search=False 模擬「不是 SERP」（同意頁 / enablejs / 未知攔阻）。"""
    def __init__(self, text="", url="https://www.google.com/search?q=x",
                 has_search=True, raise_on_goto=False, closed=False):
        self.text, self._url, self.has_search = text, url, has_search
        self.raise_on_goto, self._closed = raise_on_goto, closed
        self.goto_calls = 0

    @property
    def url(self):
        return self._url

    def evaluate(self, expr):
        # _live_url() 用它問「瀏覽器當下的網址」。真 playwright 會做一次 IPC，
        # 所以這裡回的是「真實」網址；page.url 才是可能過期的快取（見下面的測試）。
        return self._url

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls += 1
        if self.raise_on_goto:
            raise RuntimeError("navigation failed")

    def wait_for_selector(self, sel, timeout=None):
        if not self.has_search:
            raise RuntimeError(f"timeout waiting for {sel}")   # 真 playwright 也是拋錯

    def query_selector(self, sel):
        # is_blocked_now 用它做「不等待」的即時檢查；沒有結果容器就回 None
        return object() if self.has_search else None

    def inner_text(self, sel):
        return self.text

    def is_closed(self):
        return self._closed


class TestSearcherClassify(unittest.TestCase):
    """Searcher.search 的「被擋 vs 正常」判斷（不開真瀏覽器，直接塞假 page）。"""

    def _searcher(self, page):
        s = google_worker.Searcher()
        s._page = page
        return s

    def test_normal_page_ok(self):
        s = self._searcher(_FakePage(text="搜尋結果" * 100))
        text, ok, reason = s.search("q")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertIn("搜尋結果", text)

    def test_sorry_page_is_rate_limited(self):
        s = self._searcher(_FakePage(text="驗證" * 100,
                                     url="https://www.google.com/sorry/index?continue=..."))
        # CAPTCHA：需要人工解 → 主迴圈會立刻停批、不再導覽
        self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_CAPTCHA))

    def test_block_marker_is_rate_limited(self):
        s = self._searcher(_FakePage(text="我們的系統偵測到您的電腦網路送出異常流量。" * 20))
        # 網址還在 /search 但頁面寫著「異常流量」→ 跟 /sorry/ 同一件事，一樣算 CAPTCHA
        self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_CAPTCHA))

    def test_non_serp_page_is_rate_limited_not_failed(self):
        """⚠️ 最重要的一條：頁面沒有 #search（Google 同意頁／enablejs／未知攔阻）→
        必須 rate_limited。它字數很多、網址不含 /sorry/、也沒有任何攔阻字樣，
        用字數判準會被當成正常頁 → 找不到窗內日期 → 誤記成真的 failed，
        把「還沒查成功」寫成「Google 也沒有」，靜靜污染研究資料。
        全新設定檔的第一次查詢就會遇到同意頁，所以這是新機器部署的必經路徑。"""
        consent = "在您繼續前 Google 使用 Cookie 和資料 我同意 全部拒絕 更多選項" * 20
        s = self._searcher(_FakePage(text=consent, has_search=False,
                                     url="https://consent.google.com/m?continue=..."))
        self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_NOT_SERP))

    def test_sparse_but_real_serp_is_ok(self):
        """反向：合法但「幾乎沒結果」的 SERP 不可被誤判 rate_limited。實測這種頁面
        整頁可見文字只有 326 字（仍有 #search/#rso）——所以判準是容器，不是字數。
        誤判的代價：lease 是最舊優先，被放回的任務下一批又排最前面 → 永久重試。"""
        s = self._searcher(_FakePage(text="找不到與查詢字詞相符的資料。" * 4))  # ~84 字
        text, ok, reason = s.search("q")
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertIn("找不到", text)

    def test_empty_text_is_rate_limited(self):
        s = self._searcher(_FakePage(text=""))
        self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_NOT_SERP))

    def test_nav_error_is_rate_limited_and_keeps_browser(self):
        """導覽失敗（逾時等）→ ok=False，但頁面還活著就不重建瀏覽器（重啟很貴）。"""
        page = _FakePage(raise_on_goto=True)
        s = self._searcher(page)
        self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_NAV))
        self.assertIs(s._page, page)

    def test_closed_page_recycles(self):
        """使用者把視窗關了 → 重建，否則之後每筆都會失敗、任務永遠繞圈。"""
        s = self._searcher(_FakePage(raise_on_goto=True, closed=True))
        self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_NAV))
        self.assertIsNone(s._page)

    def test_restart_failure_is_rate_limited_not_crash(self):
        """重建瀏覽器失敗（設定檔被鎖等）→ rate_limited，不可讓整批任務跟著崩掉。"""
        s = google_worker.Searcher()          # _page is None → 會嘗試 start()
        s.start = lambda: (_ for _ in ()).throw(RuntimeError("profile locked"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s.search("q"), (None, False, google_worker.BLOCK_NAV))


class TestIsBlockedNowAndWait(unittest.TestCase):
    """人工解驗證的等待機制：只讀當前頁判斷是否解除，解掉就立刻接續。"""

    def setUp(self):
        self._time = google_worker.time
        self.ft = _FakeTime()
        google_worker.time = self.ft

    def tearDown(self):
        google_worker.time = self._time

    def _searcher(self, page):
        s = google_worker.Searcher()
        s._page = page
        return s

    def test_is_blocked_now_detects_pages(self):
        sorry = _FakePage(url="https://www.google.com/sorry/index")
        consent = _FakePage(has_search=False, url="https://consent.google.com/m")
        marker = _FakePage(text="偵測到異常流量" * 5)
        good = _FakePage(text="正常搜尋結果" * 20)
        self.assertTrue(self._searcher(sorry).is_blocked_now())
        self.assertTrue(self._searcher(consent).is_blocked_now())
        self.assertTrue(self._searcher(marker).is_blocked_now())
        self.assertFalse(self._searcher(good).is_blocked_now())

    def test_is_blocked_now_never_navigates(self):
        """⚠️ 這是整個機制的重點：判斷期間絕不能導覽，否則會蓋掉使用者正在輸入的頁面。"""
        page = _FakePage(url="https://www.google.com/sorry/index")
        s = self._searcher(page)
        for _ in range(5):
            s.is_blocked_now()
        self.assertEqual(page.goto_calls, 0)

    def test_wait_returns_early_when_human_solves(self):
        """解掉驗證後應立刻回來，不必等滿 block_sleep（原本無條件睡 1800s）。"""
        class Solving:
            def __init__(self):
                self.checks = 0

            def is_blocked_now(self):
                self.checks += 1
                return self.checks < 3        # 第 3 次檢查時人已解掉

        s = Solving()
        self.assertTrue(google_worker.wait_until_unblocked(s, limit=1800.0, poll=5.0))
        self.assertEqual(self.ft.slept, [5.0, 5.0, 5.0])   # 只睡了 15s，不是 1800s

    def test_wait_gives_up_after_limit(self):
        """無人看顧時行為與原本相同：等滿 limit 才放棄（且不超睡）。"""
        class Stuck:
            def is_blocked_now(self):
                return True

        self.assertFalse(google_worker.wait_until_unblocked(Stuck(), limit=20.0, poll=5.0))
        self.assertEqual(sum(self.ft.slept), 20.0)

    def test_zero_poll_does_not_spin_forever(self):
        """--captcha-poll 0 不可讓 waited 永遠不前進（會變成不會結束的空轉）。"""
        class Stuck:
            def is_blocked_now(self):
                return True

        self.assertFalse(google_worker.wait_until_unblocked(Stuck(), limit=5.0, poll=0.0))
        self.assertLessEqual(len(self.ft.slept), 20)     # 有下限 → 次數有界
        self.assertEqual(sum(self.ft.slept), 5.0)        # 且總睡眠不超過 limit

    def test_stale_cached_url_is_not_trusted(self):
        """⚠️ 線上第四次踩到的：人解掉驗證、瀏覽器已回到正常 SERP（網址列都是 /search?q=…），
        但 page.url 仍回報舊的 /sorry/…（Playwright 的快取值）。若信它就會提早 return True、
        永遠不做那次會刷新快取的 IPC → 自己鎖在舊值裡，永遠判定還在驗證頁。
        網址必須真的問瀏覽器（_live_url → evaluate）。"""
        class StaleUrlPage(_FakePage):
            @property
            def url(self):                     # Playwright 快取住的舊值
                return "https://www.google.com/sorry/index?continue=..."

        page = StaleUrlPage(text="正常搜尋結果" * 20,
                            url="https://www.google.com/search?q=3494")  # evaluate 回的真實值
        self.assertFalse(self._searcher(page).is_blocked_now())

    def test_read_error_counts_as_still_blocked(self):
        """⚠️ 讀不到當前頁時要當「還被擋」：誤判已解除會讓主迴圈導覽下一筆、
        蓋掉使用者正在輸入的驗證頁。誤判還被擋只是多等一個 poll，代價不對稱。"""
        class Flaky:
            url = "https://www.google.com/search?q=x"

            def is_closed(self):
                return False

            def query_selector(self, sel):
                raise RuntimeError("Execution context was destroyed")

        self.assertTrue(self._searcher(Flaky()).is_blocked_now())


class TestBackoffAndBlock(unittest.TestCase):
    """主迴圈：指數退避 + 連續 rate_limited 判定被擋 + 剩餘任務放回 + 關閉瀏覽器。"""

    def setUp(self):
        self._time, self._random = google_worker.time, google_worker.random
        self._fetch, self._client = google_worker.fetch_google, google_worker.Client
        self._open = google_worker.open_searcher
        self.ft = _FakeTime()
        self.fake_searcher = _FakeSearcher()
        google_worker.time = self.ft
        google_worker.random = _FakeRandom()
        # 一律「非驗證碼」的被擋 → 走 --rl-threshold 的連續門檻路徑
        google_worker.fetch_google = lambda q, timeout=None: (
            None, False, google_worker.BLOCK_NOT_SERP)
        google_worker.open_searcher = lambda args: self.fake_searcher

    def _args(self, **over):
        a = dict(server="http://x", token="t", worker_id="test", batch=8,
                 delay=6.0, jitter=4.0, per_query_sleep=6.0,
                 rl_threshold=3, block_sleep=1800.0, captcha_poll=5.0,
                 idle_sleep=30.0, once=False, max_batches=0,
                 profile="/tmp/none", headless=False)
        a.update(over)
        return types.SimpleNamespace(**a)

    def tearDown(self):
        google_worker.time, google_worker.random = self._time, self._random
        google_worker.fetch_google, google_worker.Client = self._fetch, self._client
        google_worker.open_searcher = self._open
        google_worker.SEARCHER = None

    def test_exponential_backoff_and_block(self):
        captured = {}

        class _Stop(Exception):
            pass

        class StubClient:
            def __init__(self, *a, **k):
                self.calls = 0
                captured["client"] = self

            def lease(self, n):
                self.calls += 1
                if self.calls == 1:
                    return {"tasks": [
                        {"id": i, "stock_id": "0000", "name": "測試",
                         "roc_year": 109, "roc_month": (i % 12) + 1}
                        for i in range(1, 9)], "lease_ttl": 600}
                raise _Stop           # 第二次 lease 就停，方便檢查

            def report(self, results):
                captured["reported"] = results
                return {"applied": {}}

        google_worker.Client = StubClient

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                google_worker.run(self._args())
            except _Stop:
                pass

        # rl_threshold=3 → 指數退避序列 2,4,8（第 3 筆連續 rate_limited 觸發判定被擋）
        backoff = [s for s in self.ft.slept if s in (2, 4, 8, 16, 32, 60)]
        self.assertEqual(backoff, [2, 4, 8])
        # 畫面正常（is_blocked_now 一路 False）→ 照舊長睡整段 block_sleep，保護自己的 IP
        self.assertIn(1800.0, self.ft.slept)
        # 而且完全沒進 5s 輪詢——這條路徑不可秒回，否則等於拿掉退避
        self.assertNotIn(5.0, self.ft.slept)
        # 問了 4 次「畫面現在壞著嗎」：3 筆各一次（決定要不要停批）+ 收批後一次（決定等法）
        self.assertEqual(self.fake_searcher.block_checks, 4)
        # 8 筆全部回報 rate_limited（前 3 筆爬到被擋 + 後 5 筆未爬放回）
        reported = captured["reported"]
        self.assertEqual(len(reported), 8)
        self.assertTrue(all(r["status"] == "rate_limited" for r in reported))
        # run() 結束（含異常路徑）一定要把瀏覽器關掉，否則 Chrome 會殘留
        self.assertTrue(self.fake_searcher.closed)

    def test_captcha_stops_batch_on_first_hit_and_waits_for_human(self):
        """⚠️ 使用者實際踩到的問題：撞驗證碼要「第一筆就停」，不可累積到 rl_threshold
        ——期間每筆的 goto 都會蓋掉正在解的驗證頁。停批後改成等人解、解掉即接續。"""
        captured = {}

        class _Stop(Exception):
            pass

        class StubClient:
            def __init__(self, *a, **k):
                self.calls = 0

            def lease(self, n):
                self.calls += 1
                if self.calls == 1:
                    return {"tasks": [
                        {"id": i, "stock_id": "0000", "name": "測試",
                         "roc_year": 109, "roc_month": 1} for i in range(1, 9)],
                        "lease_ttl": 600}
                raise _Stop

            def report(self, results):
                captured["reported"] = results
                return {"applied": {}}

        crawled = []

        def fetch(q, timeout=None):
            crawled.append(q)
            return (None, False, google_worker.BLOCK_CAPTCHA)

        google_worker.Client = StubClient
        google_worker.fetch_google = fetch
        # 停批原因已知（CAPTCHA）→ 不再重讀當前頁，直接輪詢：
        # 前兩次仍在驗證頁，第三次人已解掉 → 應提早結束等待
        self.fake_searcher.blocked_seq = [True, True, False]

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                google_worker.run(self._args())
            except _Stop:
                pass

        # 只爬了第一筆的第一個查詢就停：1 次導覽，不是 3 筆 × 2 查詢
        self.assertEqual(len(crawled), 1)
        # 沒有走指數退避（驗證碼不套那條路）
        self.assertNotIn(2, self.ft.slept)
        # 也沒有無條件長睡 1800，而是 5s 一次的輪詢
        self.assertNotIn(1800.0, self.ft.slept)
        self.assertEqual([s for s in self.ft.slept if s == 5.0], [5.0, 5.0, 5.0])
        # 8 筆全部放回（1 筆爬到被擋 + 7 筆未爬）
        reported = captured["reported"]
        self.assertEqual(len(reported), 8)
        self.assertTrue(all(r["status"] == "rate_limited" for r in reported))

    def test_unrecognised_verify_page_still_polls_not_long_sleep(self):
        """⚠️ 線上第二次踩到的：驗證頁網址不在 /sorry/、文字也沒 BLOCK_MARKERS 時只會被
        分類成 NOT_SERP。原本照 reason 分流 → 走無條件 sleep(1800)、一次都不輪詢，人解
        掉了也不會接續（實測解完等滿 1800s）。改成看當前頁狀態後，這條也要進 5s 輪詢。"""
        captured = {}

        class _Stop(Exception):
            pass

        class StubClient:
            def __init__(self, *a, **k):
                self.calls = 0

            def lease(self, n):
                self.calls += 1
                if self.calls == 1:
                    return {"tasks": [
                        {"id": i, "stock_id": "0000", "name": "測試",
                         "roc_year": 109, "roc_month": 1} for i in range(1, 9)],
                        "lease_ttl": 600}
                raise _Stop

            def report(self, results):
                captured["reported"] = results
                return {"applied": {}}

        crawled = []

        def fetch(q, timeout=None):
            crawled.append(q)
            return (None, False, google_worker.BLOCK_NOT_SERP)

        google_worker.Client = StubClient
        google_worker.fetch_google = fetch
        # 第 1 次是 crawl_task 判斷要不要停批；之後輪詢兩次仍壞著，第三次人處理完
        self.fake_searcher.blocked_seq = [True, True, True, False]

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                google_worker.run(self._args())
            except _Stop:
                pass

        # 第一筆打完兩種年份就停批：2 次導覽，不是 3 筆 × 2 查詢
        self.assertEqual(len(crawled), 2)
        # 關鍵：不可再是無條件長睡，要走 5s 輪詢並在人處理完後提早接續
        self.assertNotIn(1800.0, self.ft.slept)
        self.assertEqual([s for s in self.ft.slept if s == 5.0], [5.0, 5.0, 5.0])
        # 8 筆全部放回（1 筆爬到被擋 + 7 筆未爬）
        reported = captured["reported"]
        self.assertEqual(len(reported), 8)
        self.assertTrue(all(r["status"] == "rate_limited" for r in reported))

    def test_human_solving_during_report_still_polls(self):
        """⚠️ 線上第三次踩到的競態：停批到開始等待之間夾著一次 client.report() 的網路
        往返（1~2s），人正好在那一兩秒解掉驗證。若那時「再讀一次當前頁」來決定等法，
        會讀到「頁面正常」→ 誤判成單純連續失敗 → 無條件長睡 1800s（實測：解完卻沒輪詢）。
        停批的原因是已知事實，不可用會競態的重讀去推導。"""
        captured = {}

        class _Stop(Exception):
            pass

        class StubClient:
            def __init__(self, *a, **k):
                self.calls = 0

            def lease(self, n):
                self.calls += 1
                if self.calls == 1:
                    return {"tasks": [
                        {"id": i, "stock_id": "0000", "name": "測試",
                         "roc_year": 109, "roc_month": 1} for i in range(1, 9)],
                        "lease_ttl": 600}
                raise _Stop

            def report(self, results):
                captured["reported"] = results
                return {"applied": {}}

        google_worker.Client = StubClient
        google_worker.fetch_google = lambda q, timeout=None: (
            None, False, google_worker.BLOCK_CAPTCHA)
        # 停批當下畫面壞著（crawl_task 已知），但回報期間人就解掉了 →
        # 之後每次讀當前頁都是「沒被擋」
        self.fake_searcher.blocked_seq = []

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                google_worker.run(self._args())
            except _Stop:
                pass

        # 關鍵：不可因為「重讀時畫面已正常」就掉進無條件長睡
        self.assertNotIn(1800.0, self.ft.slept)
        # 走輪詢，且第一次就發現已解除 → 只睡一個 poll 就接續
        self.assertEqual([s for s in self.ft.slept if s == 5.0], [5.0])
        self.assertEqual(len(captured["reported"]), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCrawlTaskUrl(unittest.TestCase):
    """
    出處網址：⚠️ 這條路的 url 比 yahoo 弱一階。解析的是 inner_text（純文字），
    日期位置對不到 DOM 連結，只能靠「標題含公司名」去挑（revlib.pick_url_by_name）。
    所以這裡測的重點是「挑不到就承認挑不到」，絕不退而求其次亂挑一個。
    """

    def setUp(self):
        self._time, self._random, self._fetch, self._links = (
            google_worker.time, google_worker.random,
            google_worker.fetch_google, google_worker.google_links)
        self._searcher = google_worker.SEARCHER
        google_worker.time = _FakeTime()
        google_worker.random = _FakeRandom()
        google_worker.SEARCHER = None
        google_worker.fetch_google = (
            lambda q, timeout=google_worker.NAV_TIMEOUT_MS:
            (_page_text("台積電", 109, 1), True, None))

    def tearDown(self):
        (google_worker.time, google_worker.random,
         google_worker.fetch_google, google_worker.google_links) = (
            self._time, self._random, self._fetch, self._links)
        google_worker.SEARCHER = self._searcher

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_picks_link_whose_title_names_the_company(self):
        google_worker.google_links = lambda: [
            ("Yahoo奇摩股市", "https://tw.stock.yahoo.com/"),
            ("台積電 109年1月營收 - MoneyDJ", "https://moneydj.com/a")]
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["url"], "https://moneydj.com/a")

    def test_no_matching_title_gives_none_rather_than_the_first_link(self):
        google_worker.google_links = lambda: [
            ("Yahoo奇摩股市", "https://tw.stock.yahoo.com/")]
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "success")
        self.assertIsNone(r["url"])

    def test_link_harvest_failure_never_affects_the_verdict(self):
        # 出處是附屬資訊。取連結時瀏覽器出事，不可以把一筆好好的 success 拖下水。
        google_worker.google_links = lambda: []
        r, _ = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["date"], "2020-02-10")
        self.assertIsNone(r["url"])

    def test_result_links_returns_empty_without_page(self):
        self.assertEqual(google_worker.Searcher().result_links(), [])
        self.assertEqual(google_worker.google_links(), [])   # SEARCHER is None

    def test_result_links_swallows_browser_errors(self):
        s = google_worker.Searcher()

        class _Boom:
            def eval_on_selector_all(self, *a):
                raise RuntimeError("page crashed")
        s._page = _Boom()
        self.assertEqual(s.result_links(), [])

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
google_worker 的分類邏輯、查詢順序、Searcher 判擋與主迴圈回歸測試。純標準庫 unittest：
不連網、不真的開瀏覽器、不真的睡（也不需要裝 playwright——模組是延後 import 的）。

作法沿用 test_worker.py：替換 google_worker 模組命名空間裡的 time / random /
fetch_google / open_searcher / Client 為假物件，就能確定性地驗證：
  - crawl_task 的四種分類，以及 ⚠️「西元年優先」的查詢順序（Google 忽略民國年 token）
  - Searcher.search 對 /sorry/、驗證字樣、過短頁面一律回 ok=False（絕不變成 failed）
  - 主迴圈的指數退避序列、連續 rate_limited 判定被擋、剩餘任務放回、瀏覽器有關掉

跑法：  python3 -m unittest test_google_worker      或      python3 test_google_worker.py
"""

import contextlib
import io
import types
import unittest

import google_worker


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
    """假瀏覽器：只記錄有沒有被關掉（真正的查詢由 fetch_google 的替身負責）。"""
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


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
        google_worker.time = _FakeTime()
        google_worker.random = _FakeRandom()

    def tearDown(self):
        google_worker.time, google_worker.random, google_worker.fetch_google = (
            self._time, self._random, self._fetch)

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_ad_query_goes_first(self):
        """⚠️ Google 必須「西元年優先」（民國年 token 會被 Google 忽略）：
        第一次查詢就要是西元年，且命中即停、不再多打一次。"""
        seen = []

        def fetch(q, timeout=google_worker.NAV_TIMEOUT_MS):
            seen.append(q)
            return (_page_text("台積電", 109, 1), True)
        google_worker.fetch_google = fetch
        r = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
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
                return (_page_text("台積電", 109, 1), True)
            return ("台積電 沒有日期的頁面" + "x" * 500, True)   # 西元年：正常頁但無窗內日期
        google_worker.fetch_google = fetch
        r = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["source"], "g_roc")

    def test_failed_when_both_ok_but_no_date(self):
        """兩種年份頁面都正常、都無窗內日期 → 真的 failed。"""
        google_worker.fetch_google = lambda q, timeout=None: (
            "正常頁但沒有任何窗內日期" + "x" * 500, True)
        r = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "failed")

    def test_rate_limited_when_all_blocked(self):
        """兩種查詢都被擋（fetch_google ok=False）→ rate_limited，不判 failed。"""
        google_worker.fetch_google = lambda q, timeout=None: (None, False)
        r = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")

    def test_rate_limited_when_partial_block_no_hit(self):
        """西元年正常但無日期、民國年被擋 → 保守回 rate_limited（民國年可能本會命中）。"""
        def fetch(q, timeout=None):
            if "109年" in q:
                return (None, False)                        # 民國年被擋
            return ("正常頁無窗內日期" + "x" * 500, True)
        google_worker.fetch_google = fetch
        r = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")

    def test_no_searcher_is_rate_limited_not_failed(self):
        """瀏覽器還沒起（SEARCHER=None）→ rate_limited，絕不能悄悄記成 failed。"""
        r = google_worker.crawl_task(self._task(), per_query_sleep=6.0)
        self.assertEqual(r["status"], "rate_limited")


class _FakePage:
    """假 playwright page：可指定導覽後的 url、頁面文字，或讓 goto 直接拋錯。"""
    def __init__(self, text="", url="https://www.google.com/search?q=x",
                 raise_on_goto=False, closed=False):
        self.text, self._url = text, url
        self.raise_on_goto, self._closed = raise_on_goto, closed
        self.goto_calls = 0

    @property
    def url(self):
        return self._url

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, timeout=None, wait_until=None):
        self.goto_calls += 1
        if self.raise_on_goto:
            raise RuntimeError("navigation failed")

    def wait_for_selector(self, sel, timeout=None):
        raise RuntimeError("no #search")   # 等不到結果區不該影響判斷，仍往下讀文字

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
        text, ok = s.search("q")
        self.assertTrue(ok)
        self.assertIn("搜尋結果", text)

    def test_sorry_page_is_rate_limited(self):
        s = self._searcher(_FakePage(text="驗證" * 100,
                                     url="https://www.google.com/sorry/index?continue=..."))
        self.assertEqual(s.search("q"), (None, False))

    def test_block_marker_is_rate_limited(self):
        s = self._searcher(_FakePage(text="我們的系統偵測到您的電腦網路送出異常流量。" * 20))
        self.assertEqual(s.search("q"), (None, False))

    def test_short_page_is_rate_limited(self):
        s = self._searcher(_FakePage(text="短"))
        self.assertEqual(s.search("q"), (None, False))

    def test_nav_error_is_rate_limited_and_keeps_browser(self):
        """導覽失敗（逾時等）→ ok=False，但頁面還活著就不重建瀏覽器（重啟很貴）。"""
        page = _FakePage(raise_on_goto=True)
        s = self._searcher(page)
        self.assertEqual(s.search("q"), (None, False))
        self.assertIs(s._page, page)

    def test_closed_page_recycles(self):
        """使用者把視窗關了 → 重建，否則之後每筆都會失敗、任務永遠繞圈。"""
        s = self._searcher(_FakePage(raise_on_goto=True, closed=True))
        self.assertEqual(s.search("q"), (None, False))
        self.assertIsNone(s._page)

    def test_restart_failure_is_rate_limited_not_crash(self):
        """重建瀏覽器失敗（設定檔被鎖等）→ rate_limited，不可讓整批任務跟著崩掉。"""
        s = google_worker.Searcher()          # _page is None → 會嘗試 start()
        s.start = lambda: (_ for _ in ()).throw(RuntimeError("profile locked"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s.search("q"), (None, False))


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
        google_worker.fetch_google = lambda q, timeout=None: (None, False)  # 一律被擋
        google_worker.open_searcher = lambda args: self.fake_searcher

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
        args = types.SimpleNamespace(
            server="http://x", token="t", worker_id="test", batch=8,
            delay=6.0, jitter=4.0, per_query_sleep=6.0,
            rl_threshold=3, block_sleep=1800.0, idle_sleep=30.0,
            once=False, max_batches=0, profile="/tmp/none", headless=False)

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                google_worker.run(args)
            except _Stop:
                pass

        # rl_threshold=3 → 指數退避序列 2,4,8（第 3 筆連續 rate_limited 觸發判定被擋）
        backoff = [s for s in self.ft.slept if s in (2, 4, 8, 16, 32, 60)]
        self.assertEqual(backoff, [2, 4, 8])
        self.assertIn(1800.0, self.ft.slept)
        # 8 筆全部回報 rate_limited（前 3 筆爬到被擋 + 後 5 筆未爬放回）
        reported = captured["reported"]
        self.assertEqual(len(reported), 8)
        self.assertTrue(all(r["status"] == "rate_limited" for r in reported))
        # run() 結束（含異常路徑）一定要把瀏覽器關掉，否則 Chrome 會殘留
        self.assertTrue(self.fake_searcher.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)

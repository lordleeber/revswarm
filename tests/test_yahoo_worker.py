#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yahoo_worker 的退避/cooldown 與單筆分類邏輯回歸測試。純標準庫 unittest，不連網、不真的睡。

作法：把 yahoo_worker 模組命名空間裡的 time / random / fetch / Client 換成假的
（只替換 yahoo_worker 的模組全域，不動到真的 time/random 模組），就能確定性地驗證：
  - crawl_task 對四種情境的分類（success / q_ad 補查 / failed / rate_limited）
  - 主迴圈的指數退避序列、連續 rate_limited 判定 IP 被擋、剩餘任務放回

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_yahoo_worker
"""

import contextlib
import io
import types
import unittest

from worker import yahoo_worker


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


def _page(name, roc_year, roc_month):
    """組一段含精確標題錨點 + 窗內日期的假 Yahoo 頁（供 parse 命中）。"""
    wy = roc_year + 1911 if roc_month <= 11 else roc_year + 1912
    wm = roc_month + 1 if roc_month <= 11 else 1
    body = f'{name} {roc_year}年{roc_month}月營收…新聞 {wy}年{wm}月10日 發布'
    return body + ("x" * 3000)      # 撐過 >2000B（雖然這裡直接回頁面，仍模擬真實長度）


class TestCrawlTask(unittest.TestCase):
    def setUp(self):
        self._time, self._random, self._fetch = (
            yahoo_worker.time, yahoo_worker.random, yahoo_worker.fetch)
        yahoo_worker.time = _FakeTime()
        yahoo_worker.random = _FakeRandom()

    def tearDown(self):
        (yahoo_worker.time, yahoo_worker.random, yahoo_worker.fetch) = (
            self._time, self._random, self._fetch)

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_success_via_roc(self):
        """民國年查詢就命中 → success，source=q_roc。"""
        yahoo_worker.fetch = lambda q, timeout=25: (_page("台積電", 109, 1), True)
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["date"], "2020-02-10")
        self.assertEqual(r["source"], "q_roc")

    def test_success_via_ad_fallback(self):
        """民國年頁面無窗內日期、西元年才命中 → success，source=q_ad。"""
        def fetch(q, timeout=25):
            if "2020年" in q:                      # 西元年查詢
                return (_page("台積電", 109, 1), True)
            return ("台積電 沒有日期的頁面" + "x" * 3000, True)   # 民國年：正常頁但無窗內日期
        yahoo_worker.fetch = fetch
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["source"], "q_ad")

    def test_failed_when_both_ok_but_no_date(self):
        """兩種年份頁面都正常、都無窗內日期 → 真的 failed。"""
        yahoo_worker.fetch = lambda q, timeout=25: (
            "正常頁但沒有任何窗內日期" + "x" * 3000, True)
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "failed")

    def test_rate_limited_when_all_blocked(self):
        """兩種查詢都被限流（fetch ok=False）→ rate_limited，不判 failed。"""
        yahoo_worker.fetch = lambda q, timeout=25: (None, False)
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "rate_limited")

    def test_rate_limited_when_partial_block_no_hit(self):
        """民國年正常但無日期、西元年被限流 → 保守回 rate_limited（西元年可能本會命中）。"""
        def fetch(q, timeout=25):
            if "2020年" in q:
                return (None, False)               # 西元年被擋
            return ("正常頁無窗內日期" + "x" * 3000, True)
        yahoo_worker.fetch = fetch
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "rate_limited")


def _ad_sponsor_page(name, roc_year, roc_month):
    """
    模擬 yahoo SERP 頁固定嵌的廣告贊助 iframe：一顆 <a href> 指到廣告連結，緊接著是
    命中 _anchor_offsets 的 JSON 追蹤片段（含公司名+年月字樣）與一個窗內日期——
    跟真正的搜尋結果無關，但 parse_detail 目前分不出來。
    """
    wy = roc_year + 1911 if roc_month <= 11 else roc_year + 1912
    wm = roc_month + 1 if roc_month <= 11 else 1
    body = (
        '<a href="https://tw.emarketing.yahoo.com/ysmacq/index.html?_ycmp=ad_sponsor">'
        '廣告</a>'
        f'"{name} {roc_year}年{roc_month}月","yptydevice":"desktop"'
        f'{wy}年{wm}月10日'
    )
    return body + ("x" * 3000)


class TestCrawlTaskSkipsAdSponsorHit(unittest.TestCase):
    """
    出處若解回廣告贊助頁（tw.emarketing.yahoo.com/ysmacq），視為沒找到真正的來源，
    不能當 success：那不是搜尋結果，是頁面固定嵌的追蹤腳本，跟公司/月份無關。
    """

    def setUp(self):
        self._time, self._random, self._fetch = (
            yahoo_worker.time, yahoo_worker.random, yahoo_worker.fetch)
        yahoo_worker.time = _FakeTime()
        yahoo_worker.random = _FakeRandom()

    def tearDown(self):
        (yahoo_worker.time, yahoo_worker.random, yahoo_worker.fetch) = (
            self._time, self._random, self._fetch)

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_falls_through_to_ad_query_when_roc_query_only_hits_ad_sponsor(self):
        """民國年查詢只中廣告片段 → 不能當 success，改試西元年查詢；西元年是真命中。"""
        def fetch(q, timeout=25):
            if "2020年" in q:                      # 西元年查詢：真的搜尋結果
                return (_page("台積電", 109, 1), True)
            return (_ad_sponsor_page("台積電", 109, 1), True)   # 民國年：只有廣告片段
        yahoo_worker.fetch = fetch
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["source"], "q_ad")
        self.assertNotEqual(r["url"],
                             "https://tw.emarketing.yahoo.com/ysmacq/index.html?_ycmp=ad_sponsor")

    def test_failed_when_both_queries_only_hit_ad_sponsor(self):
        """兩種年份都只中廣告片段（頁面正常、非限流）→ 真的找不到，判 failed 不是 success。"""
        yahoo_worker.fetch = lambda q, timeout=25: (
            _ad_sponsor_page("台積電", 109, 1), True)
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "failed")


class TestBackoffAndBlock(unittest.TestCase):
    """主迴圈：指數退避 + 連續 rate_limited 判定 IP 被擋 + 剩餘任務放回。"""

    def setUp(self):
        self._time, self._random = yahoo_worker.time, yahoo_worker.random
        self._fetch, self._client = yahoo_worker.fetch, yahoo_worker.Client
        self.ft = _FakeTime()
        yahoo_worker.time = self.ft
        yahoo_worker.random = _FakeRandom()
        yahoo_worker.fetch = lambda q, timeout=25: (None, False)   # 一律 rate_limited

    def tearDown(self):
        yahoo_worker.time, yahoo_worker.random = self._time, self._random
        yahoo_worker.fetch, yahoo_worker.Client = self._fetch, self._client

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

        yahoo_worker.Client = StubClient
        args = types.SimpleNamespace(
            server="http://x", token="t", worker_id="test", batch=8,
            delay=3.0, jitter=2.0, per_query_sleep=1.5,
            rl_threshold=5, block_sleep=600.0, idle_sleep=30.0,
            once=False, max_batches=0)

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                yahoo_worker.run(args)
            except _Stop:
                pass

        # 指數退避序列：2,4,8,16,32（第 5 筆連續 rate_limited 觸發判定被擋）
        backoff = [s for s in self.ft.slept if s in (2, 4, 8, 16, 32, 60)]
        self.assertEqual(backoff, [2, 4, 8, 16, 32])
        # 判定 IP 被擋後長睡 block_sleep=600
        self.assertIn(600.0, self.ft.slept)
        # 8 筆全部回報 rate_limited（前 5 筆爬到被擋 + 後 3 筆未爬放回）
        reported = captured["reported"]
        self.assertEqual(len(reported), 8)
        self.assertTrue(all(r["status"] == "rate_limited" for r in reported))


class TestFetchRetry(unittest.TestCase):
    """fetch() 就地重試：秒回失敗重試、逾時不重試、用完 tries 才放棄。"""

    def setUp(self):
        self._curl = yahoo_worker._curl_page

    def tearDown(self):
        yahoo_worker._curl_page = self._curl

    def _patch(self, seq):
        """讓 _curl_page 依序回傳 seq；記錄被呼叫次數。"""
        self.calls = 0

        def fake(cmd, timeout):
            self.calls += 1
            return seq[self.calls - 1]
        yahoo_worker._curl_page = fake

    def test_retries_transient_then_succeeds(self):
        # 前兩次「可重試」失敗 → 第三次成功
        self._patch([(None, True), (None, True), ("x" * 3000, True)])
        html, ok = yahoo_worker.fetch("q", tries=3, retry_sleep=0)
        self.assertTrue(ok)
        self.assertEqual(self.calls, 3)

    def test_no_retry_on_timeout(self):
        # 逾時類（retryable=False）→ 只打一次，不重試
        self._patch([(None, False), ("x" * 3000, True)])
        html, ok = yahoo_worker.fetch("q", tries=3, retry_sleep=0)
        self.assertFalse(ok)
        self.assertEqual(self.calls, 1)

    def test_gives_up_after_tries(self):
        # 一直可重試失敗 → 試滿 tries 次才放棄
        self._patch([(None, True)] * 5)
        html, ok = yahoo_worker.fetch("q", tries=3, retry_sleep=0)
        self.assertFalse(ok)
        self.assertEqual(self.calls, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCrawlTaskUrl(unittest.TestCase):
    """
    出處網址：三支 worker 裡就這支最硬——SERP 上的 <a href>，客觀存在、位置綁定。
    測的重點是「綁到正確的那一筆」，不是「有沒有值」。
    """

    def setUp(self):
        self._time, self._random, self._fetch = (
            yahoo_worker.time, yahoo_worker.random, yahoo_worker.fetch)
        yahoo_worker.time = _FakeTime()
        yahoo_worker.random = types.SimpleNamespace(uniform=lambda a, b: 0.0)

    def tearDown(self):
        yahoo_worker.time, yahoo_worker.random, yahoo_worker.fetch = (
            self._time, self._random, self._fetch)

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_unwraps_yahoo_redirect_and_binds_to_the_right_result(self):
        html = (
            '<a href="https://r.search.yahoo.com/_ylt=A/RV=2/'
            'RU=https%3a%2f%2fmoneydj.com%2fright/RK=2/RS=z-">'
            '台積電 109年1月營收10.17億</a>'
            '<p>2020年2月10日 發布</p>'
            '<a href="https://r.search.yahoo.com/_ylt=B/RV=2/'
            'RU=https%3a%2f%2fmoneydj.com%2fwrong/RK=2/RS=z-">下一筆結果</a>'
            + "x" * 3000)
        yahoo_worker.fetch = lambda q, timeout=25: (html, True)
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["url"], "https://moneydj.com/right")

    def test_no_link_before_date_gives_none_not_crash(self):
        yahoo_worker.fetch = lambda q, timeout=25: (_page("台積電", 109, 1), True)
        r = yahoo_worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertIsNone(r["url"])       # 純文字假頁沒有 <a>，不是失敗

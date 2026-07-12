#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
worker 的退避/cooldown 與單筆分類邏輯回歸測試。純標準庫 unittest，不連網、不真的睡。

作法：把 worker 模組命名空間裡的 time / random / fetch / Client 換成假的
（只替換 worker 的模組全域，不動到真的 time/random 模組），就能確定性地驗證：
  - crawl_task 對四種情境的分類（success / q_ad 補查 / failed / rate_limited）
  - 主迴圈的指數退避序列、連續 rate_limited 判定 IP 被擋、剩餘任務放回

跑法：  python3 -m unittest test_worker      或      python3 test_worker.py
"""

import contextlib
import io
import types
import unittest

import worker


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
        self._time, self._random, self._fetch = worker.time, worker.random, worker.fetch
        worker.time = _FakeTime()
        worker.random = _FakeRandom()

    def tearDown(self):
        worker.time, worker.random, worker.fetch = self._time, self._random, self._fetch

    def _task(self):
        return {"id": 1, "stock_id": "2330", "name": "台積電",
                "roc_year": 109, "roc_month": 1}

    def test_success_via_roc(self):
        """民國年查詢就命中 → success，source=q_roc。"""
        worker.fetch = lambda q, timeout=25: (_page("台積電", 109, 1), True)
        r = worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["date"], "2020-02-10")
        self.assertEqual(r["source"], "q_roc")

    def test_success_via_ad_fallback(self):
        """民國年頁面無窗內日期、西元年才命中 → success，source=q_ad。"""
        def fetch(q, timeout=25):
            if "2020年" in q:                      # 西元年查詢
                return (_page("台積電", 109, 1), True)
            return ("台積電 沒有日期的頁面" + "x" * 3000, True)   # 民國年：正常頁但無窗內日期
        worker.fetch = fetch
        r = worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["source"], "q_ad")

    def test_failed_when_both_ok_but_no_date(self):
        """兩種年份頁面都正常、都無窗內日期 → 真的 failed。"""
        worker.fetch = lambda q, timeout=25: ("正常頁但沒有任何窗內日期" + "x" * 3000, True)
        r = worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "failed")

    def test_rate_limited_when_all_blocked(self):
        """兩種查詢都被限流（fetch ok=False）→ rate_limited，不判 failed。"""
        worker.fetch = lambda q, timeout=25: (None, False)
        r = worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "rate_limited")

    def test_rate_limited_when_partial_block_no_hit(self):
        """民國年正常但無日期、西元年被限流 → 保守回 rate_limited（西元年可能本會命中）。"""
        def fetch(q, timeout=25):
            if "2020年" in q:
                return (None, False)               # 西元年被擋
            return ("正常頁無窗內日期" + "x" * 3000, True)
        worker.fetch = fetch
        r = worker.crawl_task(self._task(), per_query_sleep=1.5)
        self.assertEqual(r["status"], "rate_limited")


class TestBackoffAndBlock(unittest.TestCase):
    """主迴圈：指數退避 + 連續 rate_limited 判定 IP 被擋 + 剩餘任務放回。"""

    def setUp(self):
        self._time, self._random = worker.time, worker.random
        self._fetch, self._client = worker.fetch, worker.Client
        self.ft = _FakeTime()
        worker.time = self.ft
        worker.random = _FakeRandom()
        worker.fetch = lambda q, timeout=25: (None, False)   # 一律 rate_limited

    def tearDown(self):
        worker.time, worker.random = self._time, self._random
        worker.fetch, worker.Client = self._fetch, self._client

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

        worker.Client = StubClient
        args = types.SimpleNamespace(
            server="http://x", token="t", worker_id="test", batch=8,
            delay=3.0, jitter=2.0, per_query_sleep=1.5,
            rl_threshold=5, block_sleep=600.0, idle_sleep=30.0,
            once=False, max_batches=0)

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                worker.run(args)
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
        self._curl = worker._curl_page

    def tearDown(self):
        worker._curl_page = self._curl

    def _patch(self, seq):
        """讓 _curl_page 依序回傳 seq；記錄被呼叫次數。"""
        self.calls = 0

        def fake(cmd, timeout):
            self.calls += 1
            return seq[self.calls - 1]
        worker._curl_page = fake

    def test_retries_transient_then_succeeds(self):
        # 前兩次「可重試」失敗 → 第三次成功
        self._patch([(None, True), (None, True), ("x" * 3000, True)])
        html, ok = worker.fetch("q", tries=3, retry_sleep=0)
        self.assertTrue(ok)
        self.assertEqual(self.calls, 3)

    def test_no_retry_on_timeout(self):
        # 逾時類（retryable=False）→ 只打一次，不重試
        self._patch([(None, False), ("x" * 3000, True)])
        html, ok = worker.fetch("q", tries=3, retry_sleep=0)
        self.assertFalse(ok)
        self.assertEqual(self.calls, 1)

    def test_gives_up_after_tries(self):
        # 一直可重試失敗 → 試滿 tries 次才放棄
        self._patch([(None, True)] * 5)
        html, ok = worker.fetch("q", tries=3, retry_sleep=0)
        self.assertFalse(ok)
        self.assertEqual(self.calls, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

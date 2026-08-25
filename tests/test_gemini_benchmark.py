#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gemini_benchmark 的抽樣、續跑快取、狀態分類與報表統計回歸測試。
純標準庫 unittest：不連網、不打 Gemini API、不讀 revswarm.db、不花錢。

守的是「錯了會讓結論反過來」的那幾條：
  - ⚠️ nosearch / error 不可以算成「gemini 沒找到」——那會灌水命中率的分母
  - ⚠️ 續跑只跳過 status='ok' 的列，沒有結論的列必須重打
  - 跨股票輪抽（純隨機會讓樣本被筆數多的那幾檔灌爆）、同 seed 完全可重現
  - 有號日差的偏早/偏晚判定（負 = 偏早，這是 11.3 抓 google 批問題的那把尺）

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_gemini_benchmark
"""

import contextlib
import io
import os
import tempfile
import unittest

from mops import gemini_benchmark as gb
from worker import gemini_worker as gw


def _backend():
    b = gw.VertexBackend("test-proj")
    b.token = lambda force=False: "tok"
    return b


def _row(sid, ry, rm, mops="2020-02-10", yahoo="2020-02-10", name="台積電"):
    return {"stock_id": sid, "name": name, "roc_year": ry, "roc_month": rm,
            "mops_date": mops, "yahoo_date": yahoo, "engine": "yahoo"}


# --- 抽樣 -------------------------------------------------------------------
class TestStratifiedSample(unittest.TestCase):
    def _pool(self):
        """A 有 50 筆、B 有 5 筆、C 有 2 筆——刻意做成極度不均。"""
        pool = [_row("A", 109, i) for i in range(1, 13)]
        pool += [_row("A", 110, i) for i in range(1, 13)]
        pool += [_row("A", 111, i) for i in range(1, 27)]
        pool += [_row("B", 109, i) for i in range(1, 6)]
        pool += [_row("C", 109, i) for i in range(1, 3)]
        return pool

    def test_spreads_across_stocks_not_proportional(self):
        """⚠️ 純隨機會讓 A（50/57 筆）吃掉九成樣本，量到的就變成「對 A 的熟悉度」。
        輪抽必須讓每檔各出一筆之後才輪第二圈。"""
        got = gb.stratified_sample(self._pool(), 6, seed=1)
        counts = {}
        for r in got:
            counts[r["stock_id"]] = counts.get(r["stock_id"], 0) + 1
        self.assertEqual(len(got), 6)
        self.assertEqual(set(counts), {"A", "B", "C"})
        self.assertEqual(counts, {"A": 2, "B": 2, "C": 2})

    def test_same_seed_reproduces_exactly(self):
        a = gb.stratified_sample(self._pool(), 10, seed=7)
        b = gb.stratified_sample(self._pool(), 10, seed=7)
        self.assertEqual([gb._key(r) for r in a], [gb._key(r) for r in b])

    def test_different_seed_differs(self):
        a = gb.stratified_sample(self._pool(), 10, seed=7)
        b = gb.stratified_sample(self._pool(), 10, seed=8)
        self.assertNotEqual([gb._key(r) for r in a], [gb._key(r) for r in b])

    def test_n_larger_than_pool_returns_everything_without_hanging(self):
        """要求超過母體時要停下來，不能在 while 迴圈裡空轉。"""
        pool = self._pool()
        got = gb.stratified_sample(pool, 10 ** 4, seed=1)
        self.assertEqual(len(got), len(pool))


# --- 續跑快取 ---------------------------------------------------------------
class TestResumeCache(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        os.unlink(self.path)          # append_row 要能自己建檔並寫 header

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_only_ok_rows_count_as_done(self):
        """⚠️ nosearch/error/fatal 沒有結論，續跑時必須重打——留著只會在報表裡當空洞。"""
        for st in ("ok", "nosearch", "error", "fatal"):
            gb.append_row(self.path, dict(
                _row(st, 109, 1), status=st, gemini_date="", gemini_source="",
                searches=1, raw_title=""))
        done = gb.load_done(self.path)
        self.assertEqual(set(k[0] for k in done), {"ok"})

    def test_append_writes_header_once(self):
        for i in (1, 2):
            gb.append_row(self.path, dict(_row("A", 109, i), status="ok",
                                          gemini_date="", gemini_source="",
                                          searches=1, raw_title=""))
        with open(self.path, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)                 # header + 2 列
        self.assertTrue(lines[0].startswith("stock_id"))


class TestReadCsvDedupes(unittest.TestCase):
    """⚠️ load_done() 只認 status='ok'，所以 nosearch/error 的列下次會被重試並再
    append 一次。report() 逐列計數，同一筆會被算兩次——樣本 N 會隨續跑往上飄。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _put(self, status, date=""):
        gb.append_row(self.path, dict(
            _row("2330", 109, 1), status=status, gemini_date=date,
            gemini_source="", searches=1, raw_title=""))

    def test_keeps_only_latest_row_per_key(self):
        self._put("error")                       # 第一次失敗
        self._put("ok", "2020-02-10")            # 續跑成功
        rows = gb.read_csv(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")   # 取最後一列

    def test_report_sample_count_does_not_inflate(self):
        self._put("nosearch")
        self._put("ok", "2020-02-10")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report(gb.read_csv(self.path))
        self.assertIn("樣本 1 筆", buf.getvalue())
        self.assertNotIn("排除", buf.getvalue())


# --- 逐筆量測的狀態分類 -----------------------------------------------------
class TestMeasureOne(unittest.TestCase):
    def setUp(self):
        self._call, self._time = gw.call_gemini, gb.time
        gb.time = type("T", (), {"sleep": staticmethod(lambda s: None)})()

    def tearDown(self):
        gw.call_gemini, gb.time = self._call, self._time

    def _payload(self, text, searches=("q",)):
        return {"text": text, "chunks": [], "searches": list(searches)}

    def test_ok_with_hit_records_date_and_source(self):
        gw.call_gemini = lambda *a, **k: (self._payload(
            "【公告】台積電 109年1月營收 2020年2月10日"), None)
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", gw.Budget(0))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["gemini_date"], "2020-02-10")
        self.assertEqual(r["gemini_source"], gw.SRC_TEXT)

    def test_ok_without_hit_is_still_ok(self):
        """模型搜了、正常回話、但沒抽到窗內日期 → 算數（進分母），這才是真的 miss。"""
        gw.call_gemini = lambda *a, **k: (self._payload("DATE: NONE"), None)
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", gw.Budget(0))
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["gemini_date"], "")

    def test_nosearch_is_not_ok_and_is_not_charged(self):
        """⚠️ 模型沒去搜 → 不進統計、也不記帳。算成 miss 會灌水分母。"""
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_NOSEARCH)
        b = gw.Budget(0)
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", b)
        self.assertEqual(r["status"], "nosearch")
        self.assertEqual(b.used, 0)

    def test_retry_then_success(self):
        """benchmark 要樣本完整，429 會重試（比 worker 多試幾次）。"""
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                return None, gw.ERR_RETRY
            return self._payload("【公告】台積電 109年1月營收 2020年2月10日"), None
        gw.call_gemini = flaky
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", gw.Budget(0),
                           retries=2, retry_sleep=0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(r["status"], "ok")

    def test_exhausted_retries_is_error_not_miss(self):
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_RETRY)
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", gw.Budget(0),
                           retries=1, retry_sleep=0)
        self.assertEqual(r["status"], "error")

    def test_fatal_propagates(self):
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_FATAL)
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", gw.Budget(0))
        self.assertEqual(r["status"], "fatal")

    def test_records_raw_search_queries_not_just_count(self):
        """⚠️ 查詢字串是模型自己決定的、我們控制不到。一致率不好時，只有這欄能分辨
        「模型下的查詢本身就爛」還是「查對了但抽錯日期」。跑完才想加就得重花錢。"""
        gw.call_gemini = lambda *a, **k: (self._payload(
            "DATE: NONE", searches=["台塑 2022年10月營收", "台塑 111年10月"]), None)
        r = gb.measure_one(_row("1301", 111, 10), _backend(), "m", gw.Budget(0))
        self.assertEqual(r["searches"], 2)
        self.assertEqual(r["search_queries"],
                         "台塑 2022年10月營收" + gb.QUERY_SEP + "台塑 111年10月")

    def test_search_queries_empty_when_call_failed(self):
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_NOSEARCH)
        r = gb.measure_one(_row("1301", 111, 10), _backend(), "m", gw.Budget(0))
        self.assertEqual(r["search_queries"], "")

    def test_searches_counted_per_query_not_per_task(self):
        gw.call_gemini = lambda *a, **k: (
            self._payload("DATE: NONE", searches=["a", "b", "c"]), None)
        b = gw.Budget(0)
        r = gb.measure_one(_row("2330", 109, 1), _backend(), "m", b)
        self.assertEqual((r["searches"], b.used), (3, 3))
        self.assertEqual(b.calls, 1)          # 呼叫次數也要記（擋無限迴圈用）


# --- 報表統計 ---------------------------------------------------------------
class TestReportStats(unittest.TestCase):
    def test_bias_counts_early_exact_late(self):
        """負 = 偏早。11.3 就是用這個方向性抓出 google 批的問題。"""
        out = gb._bias([-2, -1, 0, 3])
        self.assertIn("偏早 2", out)
        self.assertIn("準確 1", out)
        self.assertIn("偏晚 1", out)
        self.assertIn("-0.5", out)          # 中位數 (-1 + 0) / 2

    def test_weekend_rate(self):
        # 2020-02-08 是週六、02-10 是週一
        self.assertEqual(gb._weekend_rate(["2020-02-08", "2020-02-10"]), "50.0%")
        self.assertEqual(gb._weekend_rate([]), "n/a")

    def test_pct_handles_zero_denominator(self):
        self.assertEqual(gb._pct(0, 0), "n/a")

    def _records(self):
        mk = lambda sid, gd, src, st="ok", yd="2020-02-10": dict(
            _row(sid, 109, 1), status=st, gemini_date=gd, gemini_source=src,
            searches=1, raw_title="t", yahoo_date=yd)
        return [
            mk("A", "2020-02-10", gw.SRC_CHUNK),          # 命中且正確
            mk("B", "2020-02-06", gw.SRC_TEXT),           # 命中但偏早 4 天
            mk("C", "", ""),                              # 搜了但沒抽到 → 真 miss
            mk("D", "", "", st="nosearch"),               # ⚠️ 不進分母
            mk("E", "", "", st="error"),                  # ⚠️ 不進分母
        ]

    def test_report_excludes_non_ok_from_denominator(self):
        """有效樣本 3 筆（A/B/C），命中 2 筆 → 召回 66.7%，不是 5 筆裡的 40%。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report(self._records())
        out = buf.getvalue()
        self.assertIn("有效 3 筆", out)
        self.assertIn("2/3 = 66.7%", out)
        self.assertIn("排除 2 筆", out)

    def test_report_shows_control_group_and_grades(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report(self._records())
        out = buf.getvalue()
        self.assertIn("yahoo （對照組", out)      # 沒有對照組的一致率沒有意義
        self.assertIn(gw.SRC_CHUNK, out)
        self.assertIn(gw.SRC_TEXT, out)
        self.assertIn("偏早 1", out)              # B 偏早 4 天
        self.assertIn("樂觀上界", out)            # 解讀提醒一定要印

    def test_report_shows_query_stats_and_year_forms(self):
        """報表要能回答「模型到底有沒有用民國年／西元年兩種」。"""
        rows = [dict(_row("A", 109, 1), status="ok", gemini_date="2020-02-10",
                     gemini_source=gw.SRC_TEXT, searches=2, raw_title="t",
                     search_queries="台塑 2020年1月營收" + gb.QUERY_SEP + "台塑 109年1月")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report(rows)
        out = buf.getvalue()
        self.assertIn("模型實際下的搜尋查詢", out)
        self.assertIn("含民國年 50.0%", out)
        self.assertIn("含西元年 50.0%", out)
        self.assertIn("含「營收」二字 50.0%", out)

    def test_report_lists_queries_under_mismatches(self):
        """不一致的個案要把模型下的查詢一起印出來，才判斷得出是查錯還是抽錯。"""
        rows = [dict(_row("6277", 110, 5, mops="2021-06-08"), status="ok",
                     gemini_date="2021-06-09", gemini_source=gw.SRC_TEXT,
                     searches=1, raw_title="t", search_queries="宏正 2021年5月營收")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report(rows)
        self.assertIn("↳ 宏正 2021年5月營收", buf.getvalue())

    def test_report_survives_rows_without_query_column(self):
        """舊的 CSV 沒有這一欄，不可以炸掉。"""
        rows = [dict(_row("A", 109, 1), status="ok", gemini_date="2020-02-10",
                     gemini_source=gw.SRC_TEXT, searches=1, raw_title="t")]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report(rows)
        self.assertIn("召回", buf.getvalue())

    def test_report_survives_empty_and_all_skipped(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gb.report([])
            gb.report([dict(_row("A", 109, 1), status="error", gemini_date="",
                            gemini_source="", searches=0, raw_title="")])
        self.assertIn("沒有有效樣本", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

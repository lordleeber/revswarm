#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server 的統計導出、/status 頁渲染、dashboard 查詢的回歸測試。純標準庫 unittest。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_server
"""

import os
import tempfile
import time
import unittest

import server


class TestDeriveStats(unittest.TestCase):
    def test_math(self):
        by = {"undone": 100, "dispatched": 10, "success": 80, "failed": 10}
        d = server.derive_stats(by, recent_success=25)   # 近5分25筆 → 5/分
        self.assertEqual(d["total"], 200)
        self.assertEqual(d["done"], 90)                  # success+failed
        self.assertEqual(d["progress_pct"], 45.0)        # 90/200
        self.assertEqual(d["success_rate_pct"], round(80 / 90 * 100, 2))
        self.assertEqual(d["throughput_per_min"], 5.0)
        self.assertEqual(d["eta_min"], round(110 / 5.0, 1))  # remaining 110 / 5

    def test_empty_and_zero_rate(self):
        d = server.derive_stats({}, 0)
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["progress_pct"], 0.0)
        self.assertIsNone(d["success_rate_pct"])         # done=0 → 不算成功率
        self.assertIsNone(d["eta_min"])                  # 吞吐 0 → 無 ETA

    def test_prelisting_excluded(self):
        # prelisting 不計入進度分母：workable = total - prelisting
        by = {"undone": 50, "success": 40, "failed": 10, "prelisting": 100}
        d = server.derive_stats(by, recent_success=25)   # 25/5=5 筆/分
        self.assertEqual(d["total"], 200)
        self.assertEqual(d["prelisting"], 100)
        self.assertEqual(d["workable"], 100)             # 200 - 100
        self.assertEqual(d["done"], 50)                  # success+failed
        self.assertEqual(d["progress_pct"], 50.0)        # 50/100，不是 50/200
        self.assertEqual(d["eta_min"], round(50 / 5.0, 1))  # remaining=workable-done=50


class TestRenderStatus(unittest.TestCase):
    def _d(self, **over):
        d = {
            "total": 100, "prelisting": 0, "workable": 100,
            "by_state": {"success": 40, "failed": 5,
                         "undone": 50, "dispatched": 5},
            "done": 45, "progress_pct": 45.0, "success": 40, "failed": 5,
            "success_rate_pct": 88.89, "recent_success_5min": 12,
            "throughput_per_min": 2.4, "eta_min": 22.9,
            "source_counts": {"q_roc": 30, "q_ad": 10},
            "recent": [{"stock_id": "2330", "name": "台積電", "roc_year": 109,
                        "roc_month": 1, "announce_date": "2020-02-10",
                        "source": "q_roc", "raw_title": "台積電 109年1月營收 - MoneyDJ",
                        "updated_at": int(time.time())}],
        }
        d.update(over)
        return d

    def test_contains_key_parts(self):
        h = server.render_status_html(self._d(), refresh_sec=10)
        self.assertTrue(h.lstrip().lower().startswith("<!doctype html>"))
        for needle in ['meta http-equiv="refresh" content="10"',
                       "revswarm 爬取進度", "45.0%", "q_roc", "q_ad",
                       "台積電", "MoneyDJ", "預估剩餘"]:
            self.assertIn(needle, h, needle)

    def test_html_escaped(self):
        """名稱/標題含惡意字元時必須被跳脫，不能破頁。"""
        d = self._d(recent=[{"stock_id": "9999", "name": "<script>x</script>",
                             "roc_year": 109, "roc_month": 1,
                             "announce_date": "2020-02-10", "source": "q_roc",
                             "raw_title": "<b>evil</b>&stuff",
                             "updated_at": int(time.time())}])
        h = server.render_status_html(d)
        self.assertNotIn("<script>x</script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertNotIn("<b>evil</b>", h)


class TestDashboardQuery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)
        now = int(time.time())
        rows = [
            ("2330", "台積電", 109, 1, "success", "2020-02-10", "q_roc", "t-roc", now),
            ("1301", "台塑", 109, 2, "success", "2020-03-10", "q_ad", "t-ad", now + 1),
            ("3105", "穩懋", 109, 1, "failed", None, None, None, now),
            ("3105", "穩懋", 109, 2, "undone", None, None, None, now),
        ]
        c = self.store.conn
        c.executemany(
            "INSERT INTO tasks(stock_id,name,roc_year,roc_month,state,announce_date,"
            "source,raw_title,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        c.commit()

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def test_dashboard(self):
        d = self.store.dashboard()
        self.assertEqual(d["by_state"],
                         {"success": 2, "failed": 1, "undone": 1})
        self.assertEqual(d["source_counts"], {"q_roc": 1, "q_ad": 1})
        self.assertEqual(len(d["recent"]), 2)
        # recent 依 updated_at 由新到舊：1301(now+1) 應排在 2330(now) 前
        self.assertEqual(d["recent"][0]["stock_id"], "1301")


class TestLeaseOrder(unittest.TestCase):
    """派工排序：越舊營收月越優先；逾時 dispatched 會被回收並依年齡排入。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def _ins(self, stock, ry, rm, state="undone", dispatched_at=None):
        self.store.conn.execute(
            "INSERT INTO tasks(stock_id,name,roc_year,roc_month,state,dispatched_at,"
            "updated_at) VALUES(?,?,?,?,?,?,0)", (stock, "測", ry, rm, state, dispatched_at))

    def test_oldest_first_regardless_of_insert_order(self):
        # 故意「新月份先插入」，使 id 順序與年齡相反；派工仍須最舊優先。
        for ry, rm in [(115, 1), (112, 6), (110, 1), (109, 3), (109, 1), (109, 2)]:
            self._ins("X", ry, rm)
        self.store.conn.commit()
        batch = self.store.lease(3, "w")
        got = [(t["roc_year"], t["roc_month"]) for t in batch]
        self.assertEqual(got, [(109, 1), (109, 2), (109, 3)])

    def test_same_month_across_stocks_before_next_month(self):
        # 兩檔的 109/1 都要排在任何 109/2 之前（齊步推進）。
        self._ins("A", 109, 2)
        self._ins("A", 109, 1)
        self._ins("B", 109, 1)
        self.store.conn.commit()
        batch = self.store.lease(2, "w")
        self.assertTrue(all(t["roc_month"] == 1 for t in batch))

    def test_timed_out_dispatched_reclaimed(self):
        # 一筆最舊的 109/1 卡在逾時 dispatched → lease 應回收並優先派出。
        self._ins("A", 110, 1)                    # 較新的 undone
        self._ins("A", 109, 1, state="dispatched",
                  dispatched_at=int(time.time()) - 700)   # 逾時（>600s）
        self.store.conn.commit()
        batch = self.store.lease(1, "w")
        self.assertEqual((batch[0]["roc_year"], batch[0]["roc_month"]), (109, 1))

    def test_active_lease_not_reclaimed(self):
        # 未逾時的 dispatched（剛派出去、還在租約內）絕不可被回收/重派，
        # 否則兩隻 worker 會拿到同一筆 → 雙重派工。這裡守的就是那條線。
        self._ins("A", 109, 1, state="dispatched",
                  dispatched_at=int(time.time()))          # 新鮮租約（未逾時）
        self._ins("A", 110, 1)                             # 另有一筆 undone
        self.store.conn.commit()
        batch = self.store.lease(5, "w2")
        # 只應拿到 110/1；109/1 仍鎖在有效租約，不得因「最舊」被搶走
        self.assertEqual([(t["roc_year"], t["roc_month"]) for t in batch], [(110, 1)])
        st = self.store.conn.execute(
            "SELECT state FROM tasks WHERE roc_year=109 AND roc_month=1").fetchone()[0]
        self.assertEqual(st, "dispatched")


class TestEngineRouting(unittest.TestCase):
    """engine 分流：lease 依 engine 過濾、requeue_failed(engine=) 轉交佇列。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def _ins(self, stock, ry, rm, state="undone", engine="yahoo"):
        self.store.conn.execute(
            "INSERT INTO tasks(stock_id,name,roc_year,roc_month,state,engine,"
            "updated_at) VALUES(?,?,?,?,?,?,0)", (stock, "測", ry, rm, state, engine))

    def test_lease_defaults_to_yahoo_and_ignores_google(self):
        self._ins("A", 109, 1, engine="yahoo")
        self._ins("B", 109, 1, engine="google")
        self.store.conn.commit()
        batch = self.store.lease(5, "w")               # 不傳 engine → 預設 yahoo
        self.assertEqual([t["stock_id"] for t in batch], ["A"])

    def test_lease_google_only_sees_google_queue(self):
        self._ins("A", 109, 1, engine="yahoo")
        self._ins("B", 109, 1, engine="google")
        self.store.conn.commit()
        batch = self.store.lease(5, "w", engine="google")
        self.assertEqual([t["stock_id"] for t in batch], ["B"])

    def test_requeue_failed_without_engine_keeps_engine_unchanged(self):
        # 既有行為：不指定 engine → 只改 state，不動 engine（沿用同一種 worker 再掃）。
        self._ins("A", 109, 1, state="failed", engine="yahoo")
        self.store.conn.commit()
        n = self.store.requeue_failed()
        self.assertEqual(n, 1)
        row = self.store.conn.execute(
            "SELECT state, engine FROM tasks WHERE stock_id='A'").fetchone()
        self.assertEqual((row["state"], row["engine"]), ("undone", "yahoo"))

    def test_requeue_failed_with_engine_reroutes_queue(self):
        self._ins("A", 109, 1, state="failed", engine="yahoo")
        self._ins("B", 109, 1, state="success", engine="yahoo")  # 不該被動到
        self.store.conn.commit()
        n = self.store.requeue_failed(engine="google")
        self.assertEqual(n, 1)
        row_a = self.store.conn.execute(
            "SELECT state, engine FROM tasks WHERE stock_id='A'").fetchone()
        self.assertEqual((row_a["state"], row_a["engine"]), ("undone", "google"))
        row_b = self.store.conn.execute(
            "SELECT state, engine FROM tasks WHERE stock_id='B'").fetchone()
        self.assertEqual((row_b["state"], row_b["engine"]), ("success", "yahoo"))
        # 剛被轉去 google 的任務，yahoo worker 租不到；google worker 租得到。
        self.assertEqual(self.store.lease(5, "w", engine="yahoo"), [])
        self.assertEqual(
            [t["stock_id"] for t in self.store.lease(5, "w", engine="google")], ["A"])


class TestParseEngine(unittest.TestCase):
    """?engine= 的解析：不合法一定要能讓呼叫端回 400，不可靜默當預設或原樣寫進 DB。"""

    def test_missing_or_empty_uses_default(self):
        # lease 的 default 是 DEFAULT_ENGINE；requeue-failed 的 default 是 None
        # （＝engine 欄位不動，沿用舊行為）。空字串必須與「沒帶參數」同義：
        # 若當真，WHERE engine='' 永遠零筆，worker 會一直印「佇列已空」看不出是打錯參數。
        self.assertEqual(server.parse_engine("", server.DEFAULT_ENGINE), ("yahoo", True))
        self.assertEqual(server.parse_engine(None, server.DEFAULT_ENGINE), ("yahoo", True))
        self.assertEqual(server.parse_engine("", None), (None, True))

    def test_whitelisted(self):
        self.assertEqual(server.parse_engine("yahoo"), ("yahoo", True))
        self.assertEqual(server.parse_engine("google"), ("google", True))

    def test_unknown_is_rejected(self):
        # 打錯字若原樣寫入，3 萬筆會被丟進沒有任何 worker 會租的佇列，
        # 而回應仍是 {"requeued": N} —— 這種靜默壞法最難查，所以一律 ok=False。
        for bad in ("googel", "Google", "YAHOO", "bing", "yahoo google", "'"):
            with self.subTest(bad=bad):
                self.assertEqual(server.parse_engine(bad, "yahoo"), (None, False))


class TestAuth(unittest.TestCase):
    TOK = "s3cret"

    def test_no_token_configured_allows_all(self):
        self.assertTrue(server.check_auth(None, "", None, False))
        self.assertTrue(server.check_auth("", "whatever", "x", True))

    def test_bearer_header(self):
        self.assertTrue(server.check_auth(self.TOK, f"Bearer {self.TOK}", None, False))
        self.assertFalse(server.check_auth(self.TOK, "Bearer wrong", None, False))
        self.assertFalse(server.check_auth(self.TOK, "", None, False))

    def test_query_token_only_when_allowed(self):
        # 唯讀 GET：allow_query_token=True 才接受 ?token=
        self.assertTrue(server.check_auth(self.TOK, "", self.TOK, True))
        self.assertFalse(server.check_auth(self.TOK, "", "wrong", True))
        # 會改狀態的 POST：allow_query_token=False，即使 token 正確也拒絕
        self.assertFalse(server.check_auth(self.TOK, "", self.TOK, False))

    def test_clamp_refresh(self):
        self.assertEqual(server.clamp_refresh("10"), 10)
        self.assertEqual(server.clamp_refresh("1"), 5)      # 下限 5
        self.assertEqual(server.clamp_refresh("9999"), 300)  # 上限 300
        self.assertEqual(server.clamp_refresh("abc"), 10)    # 非數字 → 預設
        self.assertEqual(server.clamp_refresh(None), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)

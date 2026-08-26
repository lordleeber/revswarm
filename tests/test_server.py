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


class TestUrlColumn(unittest.TestCase):
    """report 存 url：worker 送上來的一律再驗格式，不合格存 NULL 而不是原樣寫進去。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,updated_at)"
            " VALUES(1,'1301','台塑',111,10,'dispatched',0)")
        self.store.conn.commit()

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def _url(self):
        return self.store.conn.execute(
            "SELECT url FROM tasks WHERE id=1").fetchone()["url"]

    def _report(self, url):
        self.store.report("w", [{"id": 1, "status": "success",
                                 "date": "2022-11-08", "source": "q_roc",
                                 "title": "台塑 111年10月營收", "url": url}])

    def test_stores_valid_url(self):
        self._report("https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=abc")
        self.assertEqual(self._url(),
                         "https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=abc")

    def test_rejects_non_http_scheme(self):
        # javascript:/file: 之類進了 provenance 欄位，事後有人照著點就是個洞。
        self._report("javascript:alert(1)")
        self.assertIsNone(self._url())

    def test_missing_url_is_null_not_crash(self):
        # yahoo/google 抓不到出處時就是不送這個鍵，不可以因此炸掉整批回報。
        self.store.report("w", [{"id": 1, "status": "success",
                                 "date": "2022-11-08", "source": "q_roc",
                                 "title": "台塑"}])
        self.assertIsNone(self._url())
        self.assertEqual(self.store.conn.execute(
            "SELECT state FROM tasks WHERE id=1").fetchone()["state"], "success")

    def test_migration_adds_columns_to_old_db(self):
        # 舊 DB 沒有這兩欄；_migrate 要補上，否則 report 的 UPDATE 會整個炸開。
        import sqlite3
        old = os.path.join(self.dir, "old.db")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, stock_id TEXT NOT NULL,"
                  " name TEXT NOT NULL, roc_year INTEGER NOT NULL,"
                  " roc_month INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'undone',"
                  " announce_date TEXT, source TEXT, raw_title TEXT,"
                  " revenue INTEGER, yoy REAL, attempts INTEGER DEFAULT 0,"
                  " fail_count INTEGER DEFAULT 0, dispatched_at INTEGER,"
                  " worker_id TEXT, updated_at INTEGER,"
                  " UNIQUE(stock_id, roc_year, roc_month))")
        c.commit()
        c.close()
        st = server.Store(old)
        cols = {r[1] for r in st.conn.execute("PRAGMA table_info(tasks)")}
        self.assertIn("url", cols)
        self.assertIn("verified", cols)
        st.conn.close()
        os.remove(old)


class TestReportRejectsWrongCompany(unittest.TestCase):
    """
    第三道防污染再加一條：worker 回報的 raw_title 若講的是**別家公司**，不收。

    ⚠️ 為什麼擋在 server：revlib.parse 找不到錨點時退回「取第一個窗內日期」，那條
    後備路徑沒有任何公司名保護（_anchor_offsets 的 lookahead 只在錨點命中時起作用）。
    實測 12 筆因此吃到別家公司的公告日：4113 聯上 → 聯上發(2537)、2906 高林 →
    高林股(1531)…全部出自 google worker。
    worker 擋不了——它只知道自己這一筆的公司名，不知道「聯上發」是另一檔股票。
    server 有整個 tasks 表，name→stock_id 的對照本來就在手上，這裡才擋得住。

    擋掉的處理與日期不過窗一致：退回 undone 重做，不當 success 也不當 failed
    ——那不是「查不到」，是「查到了但查錯對象」。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,updated_at)"
            " VALUES(1,'4113','聯上',109,4,'dispatched',0),"
            "       (2,'2537','聯上發',109,4,'undone',0),"
            "       (3,'1216','統一',110,6,'dispatched',0),"
            "       (4,'2912','統一超',110,6,'undone',0)")
        self.store.conn.commit()

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def _report(self, tid, date, title):
        return self.store.report("w", [{"id": tid, "status": "success",
                                        "date": date, "source": "g_ad",
                                        "title": title}])

    def _state(self, tid):
        return self.store.conn.execute(
            "SELECT state, announce_date FROM tasks WHERE id=?", (tid,)).fetchone()

    def test_rejects_a_title_about_a_longer_named_company(self):
        c = self._report(1, "2020-05-08",
                         "公告-聯上發-2020… 2020年5月8日 — 聯上發. 253")
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["success"], 0)
        r = self._state(1)
        # 退回 undone 重做：這不是「查不到」，是「查錯對象」。
        self.assertEqual(r["state"], "undone")
        self.assertIsNone(r["announce_date"])

    def test_accepts_when_our_own_anchor_matches(self):
        # 實測誤報：1216 統一 110/6 的 title 開頭就是本檔正確的公告，
        # 「統一超」只在尾巴的相關文章碎片裡。擋掉它會害正確資料被退。
        c = self._report(3, "2021-07-12",
                         "【公告】統一2021年6月合併營收392.53億元年增1.65%. 上一則 … 統一超表現備")
        self.assertEqual(c["success"], 1)
        self.assertEqual(self._state(3)["state"], "success")

    def test_accepts_a_clean_title(self):
        c = self._report(1, "2020-05-08", "聯上 109年4月營收1.41億")
        self.assertEqual(c["success"], 1)

    def test_missing_title_does_not_block(self):
        # title 是選填的佐證，沒有就沒得檢查，不可以因此拒收。
        c = self.store.report("w", [{"id": 1, "status": "success",
                                     "date": "2020-05-08", "source": "g_ad"}])
        self.assertEqual(c["success"], 1)


class TestEngineEscalationOnFailure(unittest.TestCase):
    """
    yahoo 這一關沒查到／查錯 → 自動轉去 google 佇列重掃；google 沒查到／查錯 →
    自動轉去 gemini；gemini 還是沒查到／查錯 → 沒有下一棒了，才真的標記 state='failed'。

    ⚠️ 這條擋的是「同一個引擎的系統性偏誤重爬也沒用」：worker 找不到窗內日期
    （status='failed'）或 server 驗窗/撞名沒過（'rejected'）都算「這個引擎這次
    沒交出可信結果」，都要升級，不是只有其中一種才升級——退回同一個引擎重爬，
    大機率複製同一個偏誤。rate_limited 是暫時性訊號，不算，維持原邏輯放回同佇列。
    """

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

    def _ins(self, tid, engine, state="dispatched"):
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,engine,"
            "updated_at) VALUES(?,?,?,?,?,?,?,0)",
            (tid, "2330", "台積電", 109, 1, state, engine))
        self.store.conn.commit()

    def _row(self, tid):
        return self.store.conn.execute(
            "SELECT state, engine, fail_count FROM tasks WHERE id=?", (tid,)).fetchone()

    def test_failed_on_yahoo_escalates_to_google(self):
        self._ins(1, "yahoo")
        c = self.store.report("w", [{"id": 1, "status": "failed"}])
        self.assertEqual(c["failed"], 1)
        self.assertEqual(c["escalated"], 1)
        r = self._row(1)
        self.assertEqual((r["state"], r["engine"]), ("undone", "google"))
        self.assertEqual(r["fail_count"], 1)

    def test_failed_on_google_escalates_to_gemini(self):
        self._ins(1, "google")
        self.store.report("w", [{"id": 1, "status": "failed"}])
        r = self._row(1)
        self.assertEqual((r["state"], r["engine"]), ("undone", "gemini"))

    def test_failed_on_gemini_has_no_next_hop_and_terminates(self):
        self._ins(1, "gemini")
        c = self.store.report("w", [{"id": 1, "status": "failed"}])
        self.assertEqual(c["failed"], 1)
        self.assertEqual(c["escalated"], 0)
        r = self._row(1)
        # engine 欄位留著爬取歷史，不用改回去；只有 state 要變成真正的終點。
        self.assertEqual(r["state"], "failed")

    def test_rejected_on_yahoo_escalates_to_google(self):
        # 撞名（見 TestReportRejectsWrongCompany）：查到了但查錯對象，同樣算這個
        # 引擎這次沒交出可信結果，一樣要升級，不是只退回同引擎重爬。
        self._ins(1, "yahoo")
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,engine,"
            "updated_at) VALUES(2,'2537','聯上發',109,4,'undone','yahoo',0)")
        self.store.conn.execute(
            "UPDATE tasks SET stock_id='4113', name='聯上' WHERE id=1")
        self.store.conn.commit()
        c = self.store.report("w", [{"id": 1, "status": "success", "date": "2020-05-08",
                                     "source": "g_ad",
                                     "title": "公告-聯上發-2020… 2020年5月8日 — 聯上發. 253"}])
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["escalated"], 1)
        r = self._row(1)
        self.assertEqual((r["state"], r["engine"]), ("undone", "google"))

    def test_rejected_on_gemini_has_no_next_hop_and_terminates(self):
        self._ins(1, "gemini")
        self.store.conn.execute(
            "UPDATE tasks SET stock_id='4113', name='聯上' WHERE id=1")
        self.store.conn.commit()
        c = self.store.report("w", [{"id": 1, "status": "success", "date": "2020-05-08",
                                     "source": "g_ad",
                                     "title": "公告-聯上發-2020… 2020年5月8日 — 聯上發. 253"}])
        self.assertEqual(c["rejected"], 1)
        self.assertEqual(c["escalated"], 0)
        self.assertEqual(self._row(1)["state"], "failed")

    def test_rate_limited_stays_on_same_engine_regardless_of_tier(self):
        self._ins(1, "gemini")
        c = self.store.report("w", [{"id": 1, "status": "rate_limited"}])
        self.assertEqual(c["rate_limited"], 1)
        self.assertEqual(c.get("escalated", 0), 0)
        r = self._row(1)
        self.assertEqual((r["state"], r["engine"]), ("undone", "gemini"))


class TestRequeue(unittest.TestCase):
    """
    把「已經 success 但確定抓錯」的列打回 undone 重爬。

    ⚠️ 這是全 repo 唯一會把 success 降級的線上操作，破壞力比什麼都大——寫錯一個
    條件就是幾萬筆好資料變成待爬。所以沿用 /verify 的樂觀鎖：呼叫端必須說出它看到的
    announce_date，對不上就不動。你只能 requeue 一筆你真的看過的列。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,announce_date,"
            "source,raw_title,revenue,yoy,url,verified,engine,fail_count,updated_at)"
            " VALUES(1,'4113','聯上',109,4,'success','2020-05-08','g_ad','聯上發…',"
            "100,1.5,'https://x.tw/a','tbd','google',2,111)")
        self.store.conn.commit()

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def _row(self):
        return self.store.conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()

    def _item(self, date="2020-05-08"):
        return [{"stock_id": "4113", "roc_year": 109, "roc_month": 4, "date": date}]

    def test_resets_state_and_clears_everything_derived(self):
        c = self.store.requeue(self._item(), engine=None)
        self.assertEqual(c["requeued"], 1)
        r = self._row()
        self.assertEqual(r["state"], "undone")
        # ⚠️ 全部清掉：留任何一個都會變成「undone 卻帶著上一次抓錯的證據」，
        # 而 url/verified 留著更糟——那是替一個已經被判定錯誤的日期背書。
        for col in ("announce_date", "source", "raw_title", "revenue", "yoy",
                    "url", "verified", "dispatched_at", "worker_id"):
            self.assertIsNone(r[col], col)

    def test_keeps_the_crawl_history(self):
        # fail_count/attempts 是「這筆被爬過幾次」的歷史，不是抓到的內容。
        # 清掉就查不出「這筆一直出問題」，那正是之後要優先看的線索。
        self.store.requeue(self._item(), engine=None)
        self.assertEqual(self._row()["fail_count"], 2)

    def test_can_reroute_engine(self):
        # google 抓錯的就別再給 google——換 yahoo 那條錨點路徑。
        self.store.requeue(self._item(), engine="yahoo")
        self.assertEqual(self._row()["engine"], "yahoo")

    def test_engine_none_keeps_current_queue(self):
        self.store.requeue(self._item(), engine=None)
        self.assertEqual(self._row()["engine"], "google")

    def test_date_mismatch_refuses_to_touch_the_row(self):
        # 呼叫端看到的日期跟現在不一樣 = 這列在我看過之後被改過，我的判斷不再適用。
        c = self.store.requeue(self._item(date="2020-05-09"), engine=None)
        self.assertEqual(c["mismatch"], 1)
        self.assertEqual(self._row()["state"], "success")
        self.assertEqual(self._row()["announce_date"], "2020-05-08")

    def test_failed_rows_can_be_requeued_with_an_empty_date(self):
        """
        failed 的列也要能指名重排——它沒有 announce_date，所以樂觀鎖傳空字串。

        為什麼不用 requeue-failed：那支會把**全部** failed（實測 5,624 筆）一起丟進
        指定佇列。想只挑幾筆換一條路試（例如 yahoo 沒抓到、改叫 google 試）就得有
        指名的方式。
        """
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,engine,"
            "fail_count,updated_at) VALUES(2,'4113','聯上',109,8,'failed','yahoo',3,0)")
        self.store.conn.commit()
        c = self.store.requeue(
            [{"stock_id": "4113", "roc_year": 109, "roc_month": 8, "date": ""}],
            engine="google")
        self.assertEqual(c["requeued"], 1)
        r = self.store.conn.execute("SELECT * FROM tasks WHERE id=2").fetchone()
        self.assertEqual((r["state"], r["engine"]), ("undone", "google"))
        self.assertEqual(r["fail_count"], 3)      # 爬取歷史留著

    def test_undone_can_be_rerouted_to_another_engine(self):
        """
        already-undone 的列也要能改佇列——「這條路試過了不行，換一條」是正當操作。

        ⚠️ 早期版本連 undone 一起擋，理由是「已經在排隊了」。那道防呆擋錯對象：
        undone 沒有 announce_date/raw_title，沒有任何東西可以損失，重排是無害的；
        它唯一的效果就是換 engine，而那正是呼叫端要的。真正該擋的是 dispatched
        （正在被爬，改了會跟回報打架）與 prelisting（刻意標的，不是待辦）。
        """
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,engine,updated_at)"
            " VALUES(2,'9001','甲',109,8,'undone','google',0)")
        self.store.conn.commit()
        c = self.store.requeue(
            [{"stock_id": "9001", "roc_year": 109, "roc_month": 8, "date": ""}],
            engine="gemini")
        self.assertEqual(c["requeued"], 1)
        r = self.store.conn.execute("SELECT state,engine FROM tasks WHERE id=2").fetchone()
        self.assertEqual((r["state"], r["engine"]), ("undone", "gemini"))

    def test_dispatched_and_prelisting_are_left_alone(self):
        # dispatched 正在被某隻 worker 爬，改了會跟它的回報打架；
        # prelisting 是「公司當時還沒公開發行」的刻意標記，不是待辦。
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,updated_at)"
            " VALUES(2,'9001','甲',109,8,'dispatched',0),"
            "       (3,'9002','乙',109,8,'prelisting',0)")
        self.store.conn.commit()
        c = self.store.requeue([
            {"stock_id": "9001", "roc_year": 109, "roc_month": 8, "date": ""},
            {"stock_id": "9002", "roc_year": 109, "roc_month": 8, "date": ""},
        ], engine="google")
        self.assertEqual(c["not_requeueable"], 2)
        self.assertEqual(self.store.conn.execute(
            "SELECT state FROM tasks WHERE id=3").fetchone()[0], "prelisting")

    def test_non_success_and_unknown_rows(self):
        c = self.store.requeue([
            {"stock_id": "9999", "roc_year": 109, "roc_month": 4, "date": "2020-05-08"},
            "我不是 dict",
        ], engine=None)
        self.assertEqual(c["unknown"], 2)

    def test_lease_can_pick_it_up_again(self):
        self.store.requeue(self._item(), engine="yahoo")
        batch = self.store.lease(5, "w", engine="yahoo")
        self.assertEqual([t["stock_id"] for t in batch], ["4113"])


class TestVerify(unittest.TestCase):
    """
    verified 蓋章：**只有日期真的一致才蓋**。

    這一組測試守的是這個欄位的全部意義——若「跑過就蓋」，verified 就退化成
    「有人碰過這一筆」，跟沒有這欄一樣（見 Store.verify 的 docstring）。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.store = server.Store(self.db)
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,"
            "announce_date,updated_at) VALUES"
            "(1,'1301','台塑',111,10,'success','2022-11-08',111),"
            "(2,'2330','台積電',109,1,'failed',NULL,222)")
        self.store.conn.commit()

    def tearDown(self):
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suf)
            except OSError:
                pass
        os.rmdir(self.dir)

    def _row(self, tid):
        return self.store.conn.execute(
            "SELECT verified, updated_at FROM tasks WHERE id=?", (tid,)).fetchone()

    def test_stamps_when_date_agrees(self):
        c = self.store.verify("mops", [{"stock_id": "1301", "roc_year": 111,
                                        "roc_month": 10, "date": "2022-11-08"}])
        self.assertEqual(c["verified"], 1)
        self.assertEqual(self._row(1)["verified"], "mops")

    def test_mismatch_does_not_stamp_and_does_not_overwrite_date(self):
        c = self.store.verify("gemini", [{"stock_id": "1301", "roc_year": 111,
                                          "roc_month": 10, "date": "2022-11-09"}])
        self.assertEqual(c["mismatch"], 1)
        self.assertIsNone(self._row(1)["verified"])
        # 兩個來源打架時，verify 只負責回報，絕不動 announce_date。
        self.assertEqual(self.store.conn.execute(
            "SELECT announce_date FROM tasks WHERE id=1").fetchone()[0], "2022-11-08")

    def test_a_date_that_differs_is_mismatch(self):
        c = self.store.verify("mops", [{"stock_id": "1301", "roc_year": 111,
                                        "roc_month": 10, "date": "2023-05-01"}])
        self.assertEqual(c["mismatch"], 1)
        self.assertIsNone(self._row(1)["verified"])

    def test_a_legitimately_out_of_window_row_can_still_be_stamped(self):
        """
        ⚠️ 樂觀鎖只能比對字串，**不可以借用窗驗證**。

        全庫唯一合法的窗外 success 是 data/date_overrides.csv 收的遲交個案
        （3494 誠研 109/1 = 2020-02-17，17 > 15）。早期版本讓送上來的日期先過
        validate_date，於是那一筆永遠 mismatch、永遠蓋不了章——title_audit 逐批
        掃描時它會卡在隊首無限重複出現。
        窗驗證是 report 那條路的職責（worker 送新日期進來時），verify 收的是
        「我剛才看到的值」，只需要確認那列還沒被改過。
        """
        self.store.conn.execute(
            "INSERT INTO tasks(id,stock_id,name,roc_year,roc_month,state,announce_date,"
            "source,updated_at) VALUES(9,'3494','誠研',109,1,'success','2020-02-17',"
            "'g_roc_manual',0)")
        self.store.conn.commit()
        c = self.store.verify("claude", [{"stock_id": "3494", "roc_year": 109,
                                          "roc_month": 1, "date": "2020-02-17"}])
        self.assertEqual(c["verified"], 1)
        self.assertEqual(self.store.conn.execute(
            "SELECT verified FROM tasks WHERE id=9").fetchone()[0], "claude")

    def test_non_success_row_is_skipped(self):
        c = self.store.verify("mops", [{"stock_id": "2330", "roc_year": 109,
                                        "roc_month": 1, "date": "2020-02-10"}])
        self.assertEqual(c["not_success"], 1)
        self.assertIsNone(self._row(2)["verified"])

    def test_unknown_row_and_bad_month(self):
        c = self.store.verify("mops", [
            {"stock_id": "9999", "roc_year": 111, "roc_month": 10, "date": "2022-11-08"},
            {"stock_id": "1301", "roc_year": "x", "roc_month": 10, "date": "2022-11-08"},
        ])
        self.assertEqual(c["unknown"], 2)

    def test_does_not_touch_updated_at(self):
        # updated_at 餵 /stats 的「近 5 分完成數」與吞吐/ETA。蓋章不是重新抓到，
        # 動了它會讓進度看板憑空多出一批剛完成的任務。
        before = self._row(1)["updated_at"]
        self.store.verify("mops", [{"stock_id": "1301", "roc_year": 111,
                                    "roc_month": 10, "date": "2022-11-08"}])
        self.assertEqual(self._row(1)["updated_at"], before)

    def test_idempotent(self):
        item = [{"stock_id": "1301", "roc_year": 111, "roc_month": 10,
                 "date": "2022-11-08"}]
        self.store.verify("mops", item)
        c = self.store.verify("mops", item)
        self.assertEqual(c["verified"], 1)
        self.assertEqual(self._row(1)["verified"], "mops")

    def test_claude_and_tbd_are_the_weakest_verifiers(self):
        """
        ⚠️ claude/tbd 是**人工讀 raw_title 的判斷**，不是第二個獨立來源。

        title 正是產生 announce_date 的那段文字（revlib.parse 從它附近抽日期），
        再讀一次同一段字沒有引入任何新證據——這是循環，跟 mops/gemini 那種
        「另一個來源獨立查出同一個日期」不是同一件事。
        所以排序上一定要墊底：它們永遠不可以覆蓋 mops 或 gemini 的章。
        """
        self.assertLess(server.VERIFIER_RANK["tbd"], server.VERIFIER_RANK["claude"])
        self.assertLess(server.VERIFIER_RANK["claude"], server.VERIFIER_RANK["gemini"])
        self.assertLess(server.VERIFIER_RANK["gemini"], server.VERIFIER_RANK["mops"])
        item = [{"stock_id": "1301", "roc_year": 111, "roc_month": 10,
                 "date": "2022-11-08"}]
        self.store.verify("mops", item)
        for weak in ("claude", "tbd"):
            c = self.store.verify(weak, item)
            self.assertEqual(c["kept"], 1, weak)
            self.assertEqual(self._row(1)["verified"], "mops", weak)

    def test_claude_upgrades_tbd_but_not_the_reverse(self):
        # 讀過一次判「存疑」，之後補到證據改判高信心 → 升得上去。
        # 反向（claude → tbd）擋掉是排序規則的必然，真要降級就改資料庫或加旗標。
        item = [{"stock_id": "1301", "roc_year": 111, "roc_month": 10,
                 "date": "2022-11-08"}]
        self.store.verify("tbd", item)
        self.store.verify("claude", item)
        self.assertEqual(self._row(1)["verified"], "claude")
        self.store.verify("tbd", item)
        self.assertEqual(self._row(1)["verified"], "claude")

    def test_weak_verifier_never_downgrades_a_strong_stamp(self):
        """
        ⚠️ README 教的跑法就是先 mops 後 gemini，而一列只有一個 verified 欄。若後蓋的
        無條件覆寫，使用者照著文件跑就會把 MOPS 官方文件蓋的章默默換成 gemini
        ——最硬的證據被弱來源弄丟，而且沒有任何跡象。
        """
        item = [{"stock_id": "1301", "roc_year": 111, "roc_month": 10,
                 "date": "2022-11-08"}]
        self.store.verify("mops", item)
        c = self.store.verify("gemini", item)
        self.assertEqual(c["kept"], 1)
        self.assertEqual(c["verified"], 0)
        self.assertEqual(self._row(1)["verified"], "mops")

    def test_strong_verifier_upgrades_a_weak_stamp(self):
        # 反方向要通：gemini 蓋過的列，mops 來了要升級。
        item = [{"stock_id": "1301", "roc_year": 111, "roc_month": 10,
                 "date": "2022-11-08"}]
        self.store.verify("gemini", item)
        c = self.store.verify("mops", item)
        self.assertEqual(c["verified"], 1)
        self.assertEqual(self._row(1)["verified"], "mops")

    def test_malformed_items_never_abort_the_batch(self):
        """
        ⚠️ 整批是一個交易，而 handler 只接 sqlite3.OperationalError。任何一個元素
        丟出 AttributeError 都會 rollback 掉其餘 499 筆，並回 500 加一段 traceback。
        （validate_date 對非字串做 .strip()、item.get 對非 dict——兩種都會炸。）
        """
        good = {"stock_id": "1301", "roc_year": 111, "roc_month": 10,
                "date": "2022-11-08"}
        c = self.store.verify("mops", [
            "我不是 dict",
            None,
            {"stock_id": "1301", "roc_year": 111, "roc_month": 10, "date": 20221108},
            {"stock_id": None, "roc_year": 111, "roc_month": 10, "date": "2022-11-08"},
            good,
        ])
        self.assertEqual(c["unknown"], 4)
        self.assertEqual(c["verified"], 1)          # 好的那筆照樣蓋成
        self.assertEqual(self._row(1)["verified"], "mops")

    def test_report_clears_a_stale_stamp_when_the_date_changes(self):
        # 舊的章是對「上一個日期」蓋的；新抓到日期還留著它，就成了替沒人驗過的值背書。
        self.store.conn.execute(
            "UPDATE tasks SET state='dispatched', verified='mops' WHERE id=1")
        self.store.conn.commit()
        self.store.report("w", [{"id": 1, "status": "success",
                                 "date": "2022-11-09", "source": "q_roc",
                                 "title": "台塑"}])
        row = self.store.conn.execute(
            "SELECT announce_date, verified FROM tasks WHERE id=1").fetchone()
        self.assertEqual(row["announce_date"], "2022-11-09")
        self.assertIsNone(row["verified"])

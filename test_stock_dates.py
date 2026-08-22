#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上市前工具鏈的純函式回歸測試（不連網、不碰 DB）：
  build_stock_dates 日期正規化、mark_prelisting 民國換算、goodinfo_worker 擋爬偵測與日期解析。

跑法：  python3 -m unittest test_stock_dates    或    python3 test_stock_dates.py
"""

import contextlib
import csv
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

import build_stock_dates as bsd
import goodinfo_worker as gw
import mark_prelisting as mp


class TestDateNorm(unittest.TestCase):
    def test_norm_ad8(self):
        self.assertEqual(bsd.norm_ad8("20180808"), "2018-08-08")
        self.assertEqual(bsd.norm_ad8("19901026"), "1990-10-26")
        self.assertIsNone(bsd.norm_ad8("2018"))        # 位數不對
        self.assertIsNone(bsd.norm_ad8(""))
        self.assertIsNone(bsd.norm_ad8("abcd0808"))    # 非數字

    def test_norm_roc_slash(self):
        self.assertEqual(bsd.norm_roc_slash("115/05/22"), "2026-05-22")
        self.assertEqual(bsd.norm_roc_slash("109/1/8"), "2020-01-08")   # 補零
        self.assertIsNone(bsd.norm_roc_slash("-"))     # 空欄
        self.assertIsNone(bsd.norm_roc_slash("2020-05-22"))  # 非民國/斜線


class TestRocKey(unittest.TestCase):
    def test_roc_key(self):
        self.assertEqual(mp.roc_key("2020-01-15"), 10901)   # 民國109/1 = 任務窗頭
        self.assertEqual(mp.roc_key("2026-05-22"), 11505)
        self.assertIsNone(mp.roc_key("-"))
        self.assertIsNone(mp.roc_key(None))
        # 與窗頭比較：窗內公開者 key > WINDOW_START
        self.assertGreater(mp.roc_key("2024-10-25"), mp.WINDOW_START)
        self.assertLessEqual(mp.roc_key("2019-08-01"), mp.WINDOW_START)


class TestIsBadPage(unittest.TestCase):
    def test_is_bad_page(self):
        self.assertTrue(gw.is_bad_page("x" * 10))               # 太小
        self.assertTrue(gw.is_bad_page("A" * 6000))             # >5KB 但無「公司名稱」(如 CF 520)
        self.assertFalse(gw.is_bad_page("公司名稱" + "x" * 6000))  # 真頁標記 + 夠大


class TestDatesFrom(unittest.TestCase):
    def test_graduated_company(self):
        # 藥華藥：四板日期俱全，first_public 取最早(公開發行)
        f = {"股票名稱": "藥華藥", "上市/上櫃": "上市",
             "上市日期": "2024/01/25", "上櫃日期": "2016/07/19",
             "興櫃日期": "2014/03/11", "公開發行日期": "2013/12/24",
             "成立日期": "2000/05/09", "掛牌日期": "2016/07/19"}
        d = gw.dates_from(f)
        self.assertEqual(d["name"], "藥華藥")
        self.assertEqual(d["market_gi"], "上市")
        self.assertEqual(d["listed_date"], "2024-01-25")
        self.assertEqual(d["emerging_date"], "2014-03-11")
        self.assertEqual(d["public_date"], "2013-12-24")
        self.assertEqual(d["first_public_gi"], "2013-12-24")   # min(上市/上櫃/興櫃/公開發行)

    def test_dash_fields(self):
        # 中美晶：上市/興櫃為 '-'，first_public 取公開發行
        f = {"股票名稱": "中美晶", "上市日期": "-", "上櫃日期": "2001/03/02",
             "興櫃日期": "-", "公開發行日期": "1990/10/26", "成立日期": "1981/01/21"}
        d = gw.dates_from(f)
        self.assertIsNone(d["listed_date"])
        self.assertIsNone(d["emerging_date"])
        self.assertEqual(d["otc_date"], "2001-03-02")
        self.assertEqual(d["first_public_gi"], "1990-10-26")

    def test_no_dates(self):
        d = gw.dates_from({"股票名稱": "X"})
        self.assertIsNone(d["first_public_gi"])                # 無任何板日期


class TestShiftKey(unittest.TestCase):
    """roc_year*100+month 不是連續數，位移得換算成絕對月序（不能直接加減）。"""

    def test_同年內位移(self):
        self.assertEqual(mp.shift_key(11311, -1), 11310)
        self.assertEqual(mp.shift_key(11306, -2), 11304)
        self.assertEqual(mp.shift_key(11301, +1), 11302)

    def test_跨年位移(self):
        self.assertEqual(mp.shift_key(11301, -1), 11212)    # 113/1 往前一個月 = 112/12
        self.assertEqual(mp.shift_key(11302, -3), 11211)
        self.assertEqual(mp.shift_key(11212, +1), 11301)

    def test_位移0不動(self):
        self.assertEqual(mp.shift_key(11311, 0), 11311)


class TestDemotePlan(unittest.TestCase):
    """--demote-success 的計畫：緩衝期內的 success 不能被降級。"""

    def setUp(self):
        import server
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(server.SCHEMA)
        # 竑騰：首次公開 2024-06-04（民國 113/6）
        for ry, rm, st in [(113, 3, "success"),    # 早 3 個月 → 降級
                           (113, 4, "success"),    # 早 2 個月 → 降級
                           (113, 5, "success"),    # 早 1 個月 → 緩衝內，保留
                           (113, 2, "failed"),     # 本來就會被標 prelisting
                           (113, 7, "success")]:   # 首次公開之後，完全不在範圍
            self.conn.execute(
                "INSERT INTO tasks (stock_id, name, roc_year, roc_month, state)"
                " VALUES ('7751','竑騰',?,?,?)", (ry, rm, st))
        self.conn.commit()
        self.cutoffs = {"7751": (11306, "2024-06-04")}

    def test_預設不算降級(self):
        plan = mp.build_plan(self.conn, self.cutoffs)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][3], 1)            # prune_count：那筆 failed
        self.assertEqual(plan[0][5], 3)            # succ_before：113/3,4,5
        self.assertEqual(plan[0][7], 0)            # demote_count：沒開就是 0

    def test_緩衝1個月只降級早2個月以上的(self):
        plan = mp.build_plan(self.conn, self.cutoffs, grace_months=1)
        self.assertEqual(plan[0][6], 11305)        # demote_key = 切點往前 1 個月
        self.assertEqual(plan[0][7], 2)            # 只有 113/3、113/4

    def test_緩衝0則連早1個月的也降(self):
        plan = mp.build_plan(self.conn, self.cutoffs, grace_months=0)
        self.assertEqual(plan[0][6], 11306)
        self.assertEqual(plan[0][7], 3)

    def test_只有可降級success的公司也要進計畫(self):
        # 沒有任何 undone/failed，只有上市前 success —— 不開降級時不該出現，開了才出現
        self.conn.execute("UPDATE tasks SET state='prelisting' WHERE state='failed'")
        self.conn.commit()
        self.assertEqual(mp.build_plan(self.conn, self.cutoffs), [])
        plan = mp.build_plan(self.conn, self.cutoffs, grace_months=1)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0][7], 2)


class TestDemoteWritePath(unittest.TestCase):
    """實際跑一次 main() 的寫入路徑（真的建 DB、真的改）。

    純函式測不到的三件事：欄位有沒有被清乾淨、存證檔有沒有拿到「清掉之前」的原值、
    以及 --demote-success 時存證檔是否一律重寫（不留上一輪的殘檔）。
    """

    def setUp(self):
        import server
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.dates = os.path.join(self.dir, "sd.db")
        self.out = os.path.join(self.dir, "plan.csv")
        self.demoted = os.path.join(self.dir, "demoted.csv")
        c = sqlite3.connect(self.db)
        c.executescript(server.SCHEMA)
        # 竑騰：首次公開 2024-06-04（民國 113/6）
        for ry, rm, st, ad, src, rt, rev in [
                (113, 3, "success", "2024-04-08", "q_ad", "竑騰 115年3月營收1億", 100),
                (113, 4, "success", "2024-05-08", "q_ad", "竑騰 115年4月營收1億", 100),
                (113, 5, "success", "2024-06-07", "q_roc", "竑騰 113年5月營收9429萬", 94290000),
                (113, 2, "failed", None, None, None, None),
                (113, 9, "success", "2024-10-08", "q_roc", "竑騰 113年9月營收1億", 100000000)]:
            c.execute("INSERT INTO tasks (stock_id,name,roc_year,roc_month,state,"
                      "announce_date,source,raw_title,revenue) VALUES ('7751','竑騰',?,?,?,?,?,?,?)",
                      (ry, rm, st, ad, src, rt, rev))
        c.commit()
        c.close()
        d = sqlite3.connect(self.dates)
        d.execute("CREATE TABLE goodinfo_dates (stock_id TEXT, first_public_gi TEXT, status TEXT)")
        d.execute("INSERT INTO goodinfo_dates VALUES ('7751','2024-06-04','ok')")
        d.commit()
        d.close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_main(self, *extra):
        argv = ["mark_prelisting.py", "--db", self.db, "--dates-db", self.dates,
                "--out", self.out, "--demoted-out", self.demoted] + list(extra)
        buf = io.StringIO()
        with unittest.mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(buf):
            mp.main()
        return buf.getvalue()

    def states(self):
        c = sqlite3.connect(self.db)
        out = {(r[0], r[1]): r[2:] for r in c.execute(
            "SELECT roc_year, roc_month, state, announce_date, revenue, raw_title FROM tasks")}
        c.close()
        return out

    def test_降級會清欄位且緩衝內的不動(self):
        self.run_main("--demote-success")
        st = self.states()
        for rm in (3, 4):                       # 早 2、3 個月 → 降級且清乾淨
            self.assertEqual(st[(113, rm)], ("prelisting", None, None, None))
        self.assertEqual(st[(113, 2)][0], "prelisting")          # failed 照舊被標
        # 早 1 個月＝合法補報，原封不動
        self.assertEqual(st[(113, 5)],
                         ("success", "2024-06-07", 94290000, "竑騰 113年5月營收9429萬"))
        self.assertEqual(st[(113, 9)][0], "success")             # 首次公開後不受影響

    def test_存證檔拿到清空前的原值(self):
        self.run_main("--demote-success")
        with open(self.demoted, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["roc_month"], r["announce_date"], r["raw_title"]) for r in rows],
                         [("3", "2024-04-08", "竑騰 115年3月營收1億"),
                          ("4", "2024-05-08", "竑騰 115年4月營收1億")])

    def test_第二次跑存證檔會被重寫成空的(self):
        self.run_main("--demote-success")
        with open(self.demoted, encoding="utf-8-sig") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 2)
        self.run_main("--demote-success")            # 冪等，這輪 0 筆
        with open(self.demoted, encoding="utf-8-sig") as f:
            self.assertEqual(list(csv.DictReader(f)), [])   # 只剩表頭，不留上一輪殘檔

    def test_不加旗標時不碰_success(self):
        self.run_main()
        st = self.states()
        self.assertEqual(st[(113, 3)][0], "success")
        self.assertEqual(st[(113, 2)][0], "prelisting")
        self.assertFalse(os.path.exists(self.demoted))   # 沒開降級就不該產存證檔

    def test_dry_run_不寫入(self):
        self.run_main("--demote-success", "--dry-run")
        st = self.states()
        self.assertEqual(st[(113, 3)][0], "success")
        self.assertEqual(st[(113, 2)][0], "failed")

    def test_grace_可調(self):
        self.run_main("--demote-success", "--grace-months", "3")
        st = self.states()
        # 緩衝 3 → 早 3 個月(113/3)以內都保留
        self.assertEqual(st[(113, 3)][0], "success")
        self.assertEqual(st[(113, 4)][0], "success")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_publish_time 的回歸測試。純標準庫 unittest，不連網、不碰真 DB／真 market.csv。

守的是「把 revswarm 的公布日寫進 my_stock_project 的 market.csv」會靜默出錯的地方：
  - 只有 verified 蓋過章（且不是 tbd）的 success 才准寫；NULL 是「沒人看過」，不是「對」。
  - 只改 publish_time 那一格；其他欄、列序、BOM、換行一個 byte 都不能動。
  - 只改檔案裡已經有的 symbol，不新增列（market.csv 是別的 scraper 的產物）。
  - 日期必須落在營收月的次月；落在別的月份代表資料有問題，寧可跳過也不寫。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_export_publish_time
"""

import csv
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

import export_publish_time as ept

HEADER = ["symbol", "name", "revenue", "comment", "market", "publish_time"]


def _make_db(path, rows):
    c = sqlite3.connect(path)
    c.execute(
        "CREATE TABLE tasks(stock_id TEXT, roc_year INTEGER, roc_month INTEGER,"
        " state TEXT, announce_date TEXT, verified TEXT)"
    )
    c.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def _write_market(root, y, m, rows):
    d = os.path.join(root, str(y), "%dM%02d" % (y, m))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "market.csv")
    with io.open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    return p


def _read_market(p):
    with io.open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "r.db")
        self.root = os.path.join(self.dir, "monthly_revenue")

    def tearDown(self):
        shutil.rmtree(self.dir)


class TestLoadVerified(Base):
    def test_only_verified_success_rows_and_not_tbd(self):
        _make_db(self.db, [
            ("1101", 109, 1, "success", "2020-02-07", "claude"),
            ("1102", 109, 1, "success", "2020-02-06", "codex"),
            ("1103", 109, 1, "success", "2020-02-05", "mops"),
            ("1104", 109, 1, "success", "2020-02-04", "gemini"),
            ("1105", 109, 1, "success", "2020-02-03", None),    # 沒人看過
            ("1106", 109, 1, "success", "2020-02-03", "tbd"),   # 看過但不是高信心
            ("1107", 109, 1, "failed", "2020-02-03", "claude"), # 非 success
        ])
        got = ept.load_verified(self.db)
        self.assertEqual(got, {(2020, 1): {
            "1101": "20200207", "1102": "20200206",
            "1103": "20200205", "1104": "20200204",
        }})

    def test_date_outside_next_month_is_dropped(self):
        _make_db(self.db, [
            ("1101", 109, 12, "success", "2021-01-08", "claude"),  # 12 月 → 隔年 1 月，合法
            ("1102", 109, 12, "success", "2020-12-08", "claude"),  # 同月，不可能
            ("1103", 109, 12, "success", "2021-02-08", "claude"),  # 隔兩個月
            ("1104", 109, 12, "success", "garbage", "claude"),
        ])
        got = ept.load_verified(self.db)
        self.assertEqual(got, {(2020, 12): {"1101": "20210108"}})


class TestApplyMonth(Base):
    def test_updates_only_publish_time_of_known_symbols(self):
        p = _write_market(self.root, 2020, 1, [
            ["1101", "台泥", "100", "a,b 逗號", "SII", "20200210"],
            ["1102", "亞泥", "200", "-", "SII", "20200210"],
        ])
        before = io.open(p, "rb").read()
        res = ept.apply_month(p, {"1101": "20200207", "9999": "20200205"})
        rows = _read_market(p)
        self.assertEqual(rows[0], HEADER)
        self.assertEqual(rows[1], ["1101", "台泥", "100", "a,b 逗號", "SII", "20200207"])
        self.assertEqual(rows[2], ["1102", "亞泥", "200", "-", "SII", "20200210"])
        self.assertEqual(len(rows), 3)  # 9999 不在檔案裡 → 不新增列
        self.assertEqual(res["updated"], 1)
        self.assertEqual(res["not_in_csv"], ["9999"])
        # 只有那 8 個 byte 變了：BOM、換行、引號都原樣
        after = io.open(p, "rb").read()
        self.assertEqual(after, before.replace(b"20200210", b"20200207", 1))

    def test_idempotent_and_no_write_when_unchanged(self):
        p = _write_market(self.root, 2020, 1, [["1101", "台泥", "1", "-", "SII", "20200207"]])
        mtime = os.stat(p).st_mtime_ns
        res = ept.apply_month(p, {"1101": "20200207"})
        self.assertEqual(res["updated"], 0)
        self.assertEqual(res["same"], 1)
        self.assertEqual(os.stat(p).st_mtime_ns, mtime)

    def test_dry_run_does_not_write(self):
        p = _write_market(self.root, 2020, 1, [["1101", "台泥", "1", "-", "SII", "20200210"]])
        before = io.open(p, "rb").read()
        res = ept.apply_month(p, {"1101": "20200207"}, dry_run=True)
        self.assertEqual(res["updated"], 1)
        self.assertEqual(io.open(p, "rb").read(), before)

    def test_counts_later_than_existing(self):
        # 真實日晚於原本回填的截止日：PIT 過濾會因此改變，要單獨數出來
        p = _write_market(self.root, 2020, 1, [
            ["1101", "台泥", "1", "-", "SII", "20200210"],
            ["1102", "亞泥", "1", "-", "SII", "20200210"],
        ])
        res = ept.apply_month(p, {"1101": "20200213", "1102": "20200207"})
        self.assertEqual(res["later"], ["1101"])
        self.assertEqual(res["updated"], 2)

    def test_missing_publish_time_column_is_refused(self):
        d = os.path.join(self.root, "2020", "2020M01")
        os.makedirs(d)
        p = os.path.join(d, "market.csv")
        with io.open(p, "w", encoding="utf-8-sig", newline="") as f:
            f.write("symbol,name\r\n1101,台泥\r\n")
        with self.assertRaises(ValueError):
            ept.apply_month(p, {"1101": "20200207"})


class TestRun(Base):
    def test_run_touches_market_csv_only_and_reports_missing_files(self):
        _make_db(self.db, [
            ("1101", 109, 1, "success", "2020-02-07", "claude"),
            ("1101", 109, 2, "success", "2020-03-06", "claude"),  # 沒有 2020M02 檔
        ])
        p = _write_market(self.root, 2020, 1, [["1101", "台泥", "1", "-", "SII", "20200210"]])
        tmp = os.path.join(os.path.dirname(p), "tmp.csv")
        shutil.copy(p, tmp)
        summary = ept.run(self.db, self.root)
        self.assertEqual(_read_market(p)[1][-1], "20200207")
        self.assertEqual(_read_market(tmp)[1][-1], "20200210")
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["missing_files"], [(2020, 2)])


if __name__ == "__main__":
    unittest.main()

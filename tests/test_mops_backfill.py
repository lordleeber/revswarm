#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mops_fill / mops_overwrite 的純邏輯回歸測試（純標準庫 unittest，用記憶體 DB）。

- TestBuildUpdates：mops_fill.build_updates 的分類（fillable / already_ok /
  mismatch / out_of_window / no_task / 超範圍 / 畸形日期）。
- TestDaydiff：mops_overwrite.daydiff 的正負向與跨年。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_mops_backfill
"""

import sqlite3
import unittest

from mops import mops_fill
from mops import mops_overwrite
import server


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(server.SCHEMA)
    return conn


def add_task(conn, sid, ry, rm, state="undone", announce_date=None):
    conn.execute(
        "INSERT INTO tasks (stock_id, name, roc_year, roc_month, state, announce_date)"
        " VALUES (?,?,?,?,?,?)", (sid, sid, ry, rm, state, announce_date))
    conn.commit()


class TestBuildUpdates(unittest.TestCase):
    def test_classification(self):
        conn = make_conn()
        add_task(conn, "1101", 110, 5, state="undone")                              # → fillable
        add_task(conn, "1102", 110, 6, state="failed")                              # → fillable
        add_task(conn, "1103", 110, 7, state="success", announce_date="2021-08-05")  # → already_ok
        add_task(conn, "1104", 110, 8, state="success", announce_date="2021-09-15")  # → mismatch
        # 1105 不建任務 → no_task
        baseline = {
            ("1101", 110, 5): "2021-06-05",    # 落窗、undone → fillable
            ("1102", 110, 6): "2021-07-06",    # 落窗、failed → fillable
            ("1103", 110, 7): "2021-08-05",    # success 且相符 → already_ok
            ("1104", 110, 8): "2021-09-05",    # success 但不符 → mismatch
            ("1105", 110, 9): "2021-10-05",    # 無任務 → no_task
            ("1106", 110, 10): "2021-11-20",   # 日>15 不過窗 → out_of_window
            ("1107", 999, 1): "3000-02-05",    # 超出任務範圍 → 靜默跳過
            ("1108", 110, 11): "garbage",      # 畸形日期 → 靜默跳過(try/except)
        }
        updates, rep = mops_fill.build_updates(conn, baseline, now=12345)

        # fillable：1101、1102；tuple 形狀 (announce_date, now, id)
        self.assertEqual(len(updates), 2)
        self.assertEqual({u[0] for u in updates}, {"2021-06-05", "2021-07-06"})
        self.assertTrue(all(u[1] == 12345 for u in updates))       # now 正確帶入
        self.assertTrue(all(isinstance(u[2], int) for u in updates))  # id

        self.assertEqual(rep["already_ok"], 1)
        self.assertEqual(rep["no_task"], 1)
        self.assertEqual(rep["mismatch"],
                         [("1104", 110, 8, "2021-09-15", "2021-09-05")])
        self.assertEqual(rep["out_of_window"],
                         [("1106", 110, 10, "2021-11-20")])

    def test_success_guard_and_empty(self):
        conn = make_conn()
        add_task(conn, "2330", 113, 3, state="success", announce_date="2024-04-10")
        # baseline 與 DB 完全相符 → 無可回填、無不一致
        updates, rep = mops_fill.build_updates(
            conn, {("2330", 113, 3): "2024-04-10"}, now=1)
        self.assertEqual(updates, [])
        self.assertEqual(rep["already_ok"], 1)
        self.assertEqual(rep["mismatch"], [])


class TestDaydiff(unittest.TestCase):
    def test_sign_and_cross_year(self):
        self.assertEqual(mops_overwrite.daydiff("2021-08-06", "2021-08-09"), -3)  # Yahoo 早於 MOPS
        self.assertEqual(mops_overwrite.daydiff("2020-07-15", "2020-07-03"), 12)  # Yahoo 晚於 MOPS
        self.assertEqual(mops_overwrite.daydiff("2024-01-01", "2023-12-31"), 1)   # 跨年
        self.assertEqual(mops_overwrite.daydiff("2020-10-08", "2020-10-08"), 0)   # 相同


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_date_overrides 的純函式回歸（不碰真的 DB、不連網）。

重點在守住那條刻意的不對稱（見 apply_date_overrides 模組 docstring）：
  ✓ 月份必須是「營收月的次月」——強不變量，錯了一定是打錯或誤判 → 拒
  ✗ 不限制幾號——遲交上限是個案問題，由 note 與人負責 → 16、17、28 號都要收

跑法：  python3 -m unittest test_date_overrides
"""

import csv
import unittest

import apply_date_overrides as ado
import revlib


def _row(**over):
    r = {"stock_id": "3494", "roc_year": "109", "roc_month": "1",
         "announce_date": "2020-02-17", "source": "g_roc_manual",
         "raw_title": "誠研109年1月營收6727萬、年減13.65%", "note": "實測遲交"}
    r.update(over)
    return r


class TestCheckRow(unittest.TestCase):
    def test_late_day_is_accepted(self):
        """⚠️ 這個檔存在的理由：窗外遲交要收得進來（16/17/月底都算合法輸入）。"""
        for d in ("2020-02-16", "2020-02-17", "2020-02-29"):   # 2020 是閏年
            self.assertIsNone(ado.check_row(_row(announce_date=d)), d)

    def test_in_window_date_also_accepted(self):
        """窗內的日期也收（用來更正抓錯的日期），只是 classify 會標成 in_window。"""
        self.assertIsNone(ado.check_row(_row(announce_date="2020-02-10")))
        self.assertEqual(ado.classify(_row(announce_date="2020-02-10")), "in_window")
        self.assertEqual(ado.classify(_row(announce_date="2020-02-17")), "late")

    def test_wrong_month_is_rejected(self):
        """月份是強的那一半不變量：不是營收月的次月就一定有問題，直接拒。"""
        for d in ("2020-01-31", "2020-03-02", "2021-02-17"):
            err = ado.check_row(_row(announce_date=d))
            self.assertIsNotNone(err, d)
            self.assertIn("次月", err)

    def test_december_rolls_to_next_year(self):
        """12 月營收的次月是隔年 1 月（別在跨年處算錯）。"""
        self.assertIsNone(ado.check_row(
            _row(roc_year="109", roc_month="12", announce_date="2021-01-20")))
        self.assertIsNotNone(ado.check_row(
            _row(roc_year="109", roc_month="12", announce_date="2020-01-20")))

    def test_impossible_day_is_rejected(self):
        """2 月沒有 30 日——擋打錯，不是擋遲交。"""
        err = ado.check_row(_row(announce_date="2020-02-30"))
        self.assertIn("沒有 30 日", err)

    def test_note_is_required(self):
        """每一行都要交代證據，否則這個檔就變成沒人看得懂的黑箱。"""
        self.assertIn("note", ado.check_row(_row(note="")))

    def test_out_of_task_range_is_rejected(self):
        self.assertIsNotNone(ado.check_row(
            _row(roc_year="100", roc_month="1", announce_date="2011-02-10")))

    def test_malformed_fields_are_rejected(self):
        self.assertIsNotNone(ado.check_row(_row(announce_date="2020/02/17")))
        self.assertIsNotNone(ado.check_row(_row(roc_month="13")))
        self.assertIsNotNone(ado.check_row(_row(stock_id="")))


class TestBuildUpdates(unittest.TestCase):
    def _lookup(self, cur):
        return lambda sid, ry, rm: cur

    def test_already_matching_is_skipped(self):
        """冪等：重建 DB 後可以放心重跑，已一致的不會再寫。"""
        cur = {"id": 1, "state": "success", "announce_date": "2020-02-17",
               "source": "g_roc_manual"}
        up, skip, orph = ado.build_updates([_row()], self._lookup(cur))
        self.assertEqual((len(up), len(skip), len(orph)), (0, 1, 0))

    def test_failed_task_gets_updated_and_revenue_derived(self):
        """failed → success，且 revenue/yoy 由 raw_title 導出（與 server 同一條路）。"""
        cur = {"id": 7, "state": "failed", "announce_date": None, "source": None}
        up, skip, orph = ado.build_updates([_row()], self._lookup(cur))
        self.assertEqual((len(up), len(skip), len(orph)), (1, 0, 0))
        self.assertEqual(up[0]["id"], 7)
        self.assertEqual(up[0]["revenue"], 67270000)
        self.assertEqual(up[0]["yoy"], -13.65)
        self.assertEqual(up[0]["source"], "g_roc_manual")

    def test_missing_task_is_orphan_not_crash(self):
        up, skip, orph = ado.build_updates([_row()], self._lookup(None))
        self.assertEqual((len(up), len(skip), len(orph)), (0, 0, 1))

    def test_source_defaults_to_manual(self):
        cur = {"id": 1, "state": "failed", "announce_date": None, "source": None}
        up, _, _ = ado.build_updates([_row(source="")], self._lookup(cur))
        self.assertEqual(up[0]["source"], "manual")

    def test_no_raw_title_leaves_revenue_none(self):
        cur = {"id": 1, "state": "failed", "announce_date": None, "source": None}
        up, _, _ = ado.build_updates([_row(raw_title="")], self._lookup(cur))
        self.assertIsNone(up[0]["revenue"])
        self.assertIsNone(up[0]["raw_title"])


class TestShippedCsv(unittest.TestCase):
    """進版控的那份 CSV 本身必須永遠是合格的（否則 apply 會整批拒絕）。"""

    def test_repo_csv_is_valid(self):
        rows = ado.load_overrides("data/date_overrides.csv")
        self.assertTrue(rows, "date_overrides.csv 不該是空的")
        for i, r in enumerate(rows, 2):
            self.assertIsNone(ado.check_row(r), f"第 {i} 行不合格：{r}")

    def test_repo_csv_header_matches_expected_fields(self):
        with open("data/date_overrides.csv", encoding="utf-8-sig") as f:
            self.assertEqual(tuple(next(csv.reader(f))), ado.FIELDS)

    def test_window_upper_bound_still_15(self):
        """釘住這個檔存在的前提：窗沒有被全域放寬（MOPS 2556 筆的上界就是 15）。
        若哪天 in_window 放寬了，這個 override 機制的定位就要重新檢討。"""
        self.assertTrue(revlib.in_window(2020, 2, 15, 109, 1))
        self.assertFalse(revlib.in_window(2020, 2, 16, 109, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)

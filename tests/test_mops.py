#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mops_validate 的解析/映射純函式回歸測試（不連網）。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_mops
"""

import unittest

from mops import mops_validate as mv


class TestMopsParsing(unittest.TestCase):
    def test_roc_to_ad(self):
        self.assertEqual(mv.roc_to_ad("109/02/10"), "2020-02-10")
        self.assertEqual(mv.roc_to_ad("110/1/8"), "2021-01-08")
        self.assertEqual(mv.roc_to_ad("bad"), "bad")

    def test_revenue_roc_from_ad_year(self):
        # 西元年主旨 → 轉民國
        self.assertEqual(mv.revenue_roc("台積公司2020年1月營收報告"), (109, 1))
        self.assertEqual(mv.revenue_roc("某公司2019年12月營收"), (108, 12))

    def test_revenue_roc_from_roc_year(self):
        # 民國年主旨 → 原樣
        self.assertEqual(mv.revenue_roc("穩懋108年12月營收"), (108, 12))
        self.assertEqual(mv.revenue_roc("台塑 109年3月合併營業額"), (109, 3))

    def test_revenue_roc_none(self):
        self.assertIsNone(mv.revenue_roc("沒有年月的主旨"))

    def test_is_monthly_revenue(self):
        self.assertTrue(mv.is_monthly_revenue("台積公司2020年1月營收報告"))
        self.assertTrue(mv.is_monthly_revenue("台塑2020年3月合併營業額"))
        self.assertFalse(mv.is_monthly_revenue("公告本公司法人說明會"))    # NOISE
        self.assertFalse(mv.is_monthly_revenue("2020年1月營收更正"))       # NOISE:更正
        self.assertFalse(mv.is_monthly_revenue("取得固定收益證券"))        # 無營收/年月

    def test_呈現為零的另一則重大訊息不算月營收公告(self):
        # 零營收公司會另發一則涵蓋「月份區間」、晚好幾個月才報的說明，它同時有營收關鍵字
        # 與年月，不排除就會變成基準裡的 false positive（實測 1438 三地開發 4 筆）。
        self.assertFalse(mv.is_monthly_revenue(
            "說明本公司113年11月-114年1月營業收入呈現為零"))
        # 但不能只用「說明」二字擋——這是真的月營收公告
        self.assertTrue(mv.is_monthly_revenue("台積公司2025年1月營收報告與地震影響說明"))

    def test_load_baseline_重濾舊快取(self):
        import csv
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=mv.BASELINE_FIELDS)
                w.writeheader()
                w.writerow({"stock_id": "1301", "roc_year": 109, "roc_month": 1,
                            "announce_date": "2020-02-06", "announce_time": "13:36:41",
                            "after_close": "Y", "subject": "公告本公司2020年1月合併營業額"})
                w.writerow({"stock_id": "1438", "roc_year": 113, "roc_month": 11,
                            "announce_date": "2025-03-03", "announce_time": "18:25:11",
                            "after_close": "Y",
                            "subject": "說明本公司113年11月-114年1月營業收入呈現為零"})
            base = mv.load_baseline(path)
            self.assertIn(("1301", 109, 1), base)
            self.assertNotIn(("1438", 113, 11), base)   # 舊快取裡的 false positive 被濾掉
        finally:
            os.unlink(path)

    def test_is_after_close(self):
        self.assertTrue(mv.is_after_close("14:32:59"))
        self.assertTrue(mv.is_after_close("13:30:00"))     # 收盤(含)算盤後
        self.assertFalse(mv.is_after_close("09:00:00"))
        self.assertFalse(mv.is_after_close(""))

    def test_day(self):
        self.assertEqual(mv._day("2020-02-10"), 10)
        self.assertEqual(mv._day("2021-01-08"), 8)
        self.assertIsNone(mv._day("nope"))

    def test_earnings_regex_excludes_selfsettled(self):
        # 中鋼式「自結合併營收及稅前盈餘」應被 EARNINGS 命中(→ fetch 時排除)
        self.assertTrue(mv.EARNINGS.search("自結合併營收及稅前盈餘"))
        self.assertFalse(mv.EARNINGS.search("2020年1月營收報告"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

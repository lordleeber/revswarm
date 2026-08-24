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

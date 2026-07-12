#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revlib.parse_revenue 的純函式回歸測試（不連網）。

跑法：  python3 -m unittest test_revlib      或      python3 test_revlib.py
"""

import unittest

import revlib as R


class TestParseRevenue(unittest.TestCase):
    def test_moneydj_億_年減(self):
        # 「年減」→ 年增率為負
        self.assertEqual(
            R.parse_revenue("台化 109年8月營收189.84億、年減26.94% - MoneyDJ理財網"),
            (18984000000, -26.94))

    def test_moneydj_萬_年增(self):
        self.assertEqual(
            R.parse_revenue("佰研 109年3月營收5402萬、年增58.73%-MoneyDJ理財網"),
            (54020000, 58.73))

    def test_gonggao_合併營收_元_帶負號(self):
        # 公告格式：「合併營收…億元」＋「年增-15.24%」（數字自帶負號）
        self.assertEqual(
            R.parse_revenue("【公告】福壽 2020年1月合併營收10.17億元 年增-15.24%"),
            (1017000000, -15.24))

    def test_年增_正值(self):
        self.assertEqual(
            R.parse_revenue("味王 109年1月營收5.45億、年增0.07%-MoneyDJ理財網"),
            (545000000, 0.07))

    def test_零營收(self):
        # 生技等無營收月：金額 0、無年增率
        self.assertEqual(
            R.parse_revenue("浩鼎 109年1月營收0萬 - MoneyDJ理財網"),
            (0, None))

    def test_有金額無年增率(self):
        amt, yoy = R.parse_revenue("環泰 109年3月營收3.64億 - MoneyDJ理財網")
        self.assertEqual(amt, 364000000)
        self.assertIsNone(yoy)

    def test_千分位逗號(self):
        amt, _ = R.parse_revenue("某巨企 109年1月營收1,234.56億、年增5%")
        self.assertEqual(amt, 123456000000)

    def test_自結損益排除(self):
        # 自結/盈餘/損益等非月營收公告 → 不抽
        self.assertIsNone(R.parse_revenue("中鋼 109年1月自結合併營收及稅前盈餘"))
        self.assertIsNone(R.parse_revenue("聚陽109年1月份自結合併營收"))

    def test_無營收金額回_None(self):
        self.assertIsNone(R.parse_revenue('台積電 2020年1月","yptydevice":"desktop"'))
        self.assertIsNone(R.parse_revenue("國泰金 2020年6月 相關"))
        self.assertIsNone(R.parse_revenue(""))
        self.assertIsNone(R.parse_revenue(None))

    def test_極端但忠實(self):
        # 小基期暴增是真實資料，不做 clamp
        self.assertEqual(
            R.parse_revenue("大將 109年1月營收2038萬、年增16204.80% - MoneyDJ理財網"),
            (20380000, 16204.8))


if __name__ == "__main__":
    unittest.main(verbosity=2)

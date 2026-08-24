#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上市前工具鏈的純函式回歸測試（不連網、不碰 DB）：
  build_stock_dates 日期正規化、mark_prelisting 民國換算、goodinfo_worker 擋爬偵測與日期解析。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_stock_dates
"""

import unittest

from goodinfo import build_stock_dates as bsd
from goodinfo import goodinfo_worker as gw
import mark_prelisting as mp          # 主流程那端：寫入 revswarm.db，留在 repo 根目錄


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

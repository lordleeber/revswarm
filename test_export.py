#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export.py 的純函式回歸測試（不連網、不碰 DB）：
衍生欄位（rev_ym / lag_days）、哨兵清理、以及可靠度分層 row_flags。

跑法：  python3 -m unittest test_export      或      python3 test_export.py
"""

import unittest

import export as E


def row(stock_id="1101", name="台泥", roc_year=109, roc_month=1,
        revenue=7502000000, raw_title="台泥 109年1月營收75.02億、年減20.14%"):
    """組一列 tasks（row_flags 只用得到這幾個欄位）。"""
    return {"stock_id": stock_id, "name": name, "roc_year": roc_year,
            "roc_month": roc_month, "revenue": revenue, "raw_title": raw_title}


class TestDerivedFields(unittest.TestCase):
    def test_rev_ym(self):
        self.assertEqual(E.rev_ym(109, 1), "2020-01")
        self.assertEqual(E.rev_ym(115, 12), "2026-12")

    def test_lag_days(self):
        # 109/1 營收月底 2020-01-31，2020-02-10 公布 → 10 天
        self.assertEqual(E.lag_days("2020-02-10", 109, 1), 10)
        # 跨年：109/12 月底 2020-12-31，2021-01-05 → 5 天
        self.assertEqual(E.lag_days("2021-01-05", 109, 12), 5)
        # 閏月月底取對：109/2 月底是 2020-02-29
        self.assertEqual(E.lag_days("2020-03-01", 109, 2), 1)

    def test_lag_days_髒資料不中斷匯出(self):
        self.assertIsNone(E.lag_days(None, 109, 1))
        self.assertIsNone(E.lag_days("", 109, 1))
        self.assertIsNone(E.lag_days("2020/02/10", 109, 1))     # 非 ISO
        self.assertIsNone(E.lag_days("2020-02-31", 109, 1))     # 不存在的日

    def test_clean_yoy(self):
        self.assertIsNone(E.clean_yoy(E.YOY_SENTINEL))
        self.assertIsNone(E.clean_yoy(None))
        self.assertEqual(E.clean_yoy(-20.14), -20.14)
        self.assertEqual(E.clean_yoy(0.0), 0.0)                 # 0 不可被當成沒有值


class TestRowFlags(unittest.TestCase):
    def test_乾淨的一列沒有任何_flag(self):
        self.assertEqual(E.row_flags(row()), [])

    def test_no_revenue(self):
        r = row(revenue=None, raw_title='台泥 109年1月","yptydevice":"desktop"')
        self.assertIn("no_revenue", E.row_flags(r))

    def test_no_anchor(self):
        # CMoney 盤後速報那種彙總頁：既無公司名也無代號
        r = row(raw_title="傳產-水泥 2020年2月10日 台股盤後速報")
        self.assertIn("no_anchor", E.row_flags(r))
        # 只有代號沒名稱 → 仍算有錨點
        self.assertNotIn("no_anchor", E.row_flags(row(raw_title="1101 2020年1月營收75億")))

    def test_year_conflict(self):
        r = row(roc_year=111, roc_month=6, name="宏碁智新", stock_id="7794",
                raw_title="宏碁智新 115年6月營收1.13億、年增20.47%")
        self.assertIn("year_conflict", E.row_flags(r))

    def test_pre_public_含緩衝(self):
        fp = {"7751": (113, 6)}          # 竑騰 首次公開 2024-06 → 民國 113/6
        # 早 1 個月 = 首次公開當月補報前月，合法，不標
        self.assertNotIn("pre_public",
                         E.row_flags(row(stock_id="7751", roc_year=113, roc_month=5,
                                         raw_title="竑騰 113年5月營收9429萬"), fp))
        # 早 2 個月 → 當時沒有申報義務
        self.assertIn("pre_public",
                      E.row_flags(row(stock_id="7751", roc_year=113, roc_month=4,
                                      raw_title="竑騰 113年4月營收9429萬"), fp))
        # 首次公開之後的月份完全不受影響
        self.assertNotIn("pre_public",
                         E.row_flags(row(stock_id="7751", roc_year=113, roc_month=9,
                                         raw_title="竑騰 113年9月營收9429萬"), fp))

    def test_pre_public_跨年正確(self):
        fp = {"9999": (113, 1)}          # 首次公開 民國113/1
        self.assertNotIn("pre_public",
                         E.row_flags(row(stock_id="9999", name="X", roc_year=112,
                                         roc_month=12, raw_title="X 112年12月營收1億"), fp))
        self.assertIn("pre_public",
                      E.row_flags(row(stock_id="9999", name="X", roc_year=112,
                                      roc_month=11, raw_title="X 112年11月營收1億"), fp))

    def test_沒有_first_public_時該條靜默停用(self):
        r = row(stock_id="7751", roc_year=113, roc_month=4, raw_title="竑騰 113年4月營收1億")
        self.assertNotIn("pre_public", E.row_flags(r))            # 不傳
        self.assertNotIn("pre_public", E.row_flags(r, {}))        # 傳空

    def test_多條_flag_同時成立(self):
        r = row(revenue=None, raw_title="某彙總頁 2020年2月10日")
        self.assertEqual(sorted(E.row_flags(r)), ["no_anchor", "no_revenue"])


class TestLoadFirstPublic(unittest.TestCase):
    def test_檔案不在回空_dict(self):
        self.assertEqual(E.load_first_public("does_not_exist_9999.db"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

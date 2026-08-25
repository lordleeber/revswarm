#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revlib.parse_revenue 的純函式回歸測試（不連網）。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_revlib
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

    def test_畸形數字不拋例外(self):
        # 畸形數字（多小數點、只有逗號）不得讓 float() 拋 ValueError；
        # 解不出的部分安靜略過，而非炸掉 server 回報熱路徑。
        # 年增畸形 → 金額仍取得、yoy 視為 None
        self.assertEqual(
            R.parse_revenue("某股 109年1月營收5億、年增1.2.3%"), (500000000, None))
        # 金額畸形（只有逗號）→ 整筆 None
        self.assertIsNone(R.parse_revenue("某股 109年1月營收,億、年增5%"))
        # 金額多小數點 → 抽不到合法金額 → None
        self.assertIsNone(R.parse_revenue("某股 109年1月營收1.2.3億、年增5%"))


class TestAnchorYear(unittest.TestCase):
    """錨點必須連年份一起鎖（否則同月份別年的文章會被當錨點，見 _anchor_offsets）。"""

    # 同一頁兩篇「6月營收」：115年的（錯）與 109年的（對）。兩個日期都落在 109/6 的窗
    # (2020-07-01~15)，且錯的那篇「離自己的日期比較近」——年份萬用時會挑到 07-14。
    TWO_ARTICLES = ('宏碁智新 115年6月營收1.13億 2020-07-14'
                    + '填充內容' * 12 +
                    '宏碁智新 109年6月營收0.9億，以下為該公司當月營運概況說明 2020-07-08')

    def test_錨點只認本任務年份(self):
        offs = R._anchor_offsets(self.TWO_ARTICLES, "宏碁智新", 109, 6)
        self.assertEqual(len(offs), 1)                       # 只認 109 那篇，不認 115
        self.assertTrue(self.TWO_ARTICLES[offs[0]:].startswith("宏碁智新 109年6月"))

    def test_不被別年同月的文章搶走錨點(self):
        self.assertEqual(R.parse(self.TWO_ARTICLES, "宏碁智新", 109, 6)[0], "2020-07-08")

    def test_民國與西元年都算數(self):
        self.assertEqual(
            R.parse("寶雅 109年6月營收8億 2020-07-08", "寶雅", 109, 6)[0], "2020-07-08")
        self.assertEqual(
            R.parse("寶雅 2020年6月營收8億 2020-07-08", "寶雅", 109, 6)[0], "2020-07-08")

    def test_只有別年文章時退回後備而非錨到它(self):
        # 頁面只有 115年6月 那篇 → 沒有合格錨點 → 走 parse() 後備（第一個窗內日期）。
        html = '宏碁智新 115年6月營收1.13億 2020-07-10'
        self.assertEqual(R._anchor_offsets(html, "宏碁智新", 109, 6), [])
        self.assertEqual(R.parse(html, "宏碁智新", 109, 6)[0], "2020-07-10")

    def test_窗外日期仍然一律不收(self):
        self.assertIsNone(R.parse("寶雅 109年6月營收8億 2020-07-20", "寶雅", 109, 6))


class TestYoyScope(unittest.TestCase):
    def test_單月(self):
        self.assertEqual(
            R.yoy_scope("大城地產 112年9月營收3萬、年減99.98%"), "monthly")
        self.assertEqual(
            R.yoy_scope("【公告】台化 2020年9月合併營收207.17億元 年增-15.2%"), "monthly")

    def test_累計(self):
        # 年增率黏在「累計營收」後面 → 那是累計年增率，不是單月
        self.assertEqual(
            R.yoy_scope("皇普 113年12月營收53.06億，累計營收53.31億、年增188.14%"),
            "cumulative")

    def test_只有累計沒單月(self):
        self.assertEqual(R.yoy_scope("某股 累計營收9.14億、年增21.00%"), "cumulative")

    def test_無年增率或無金額回_None(self):
        self.assertIsNone(R.yoy_scope("高端疫苗 109年2月營收0萬，累計營收48萬"))
        self.assertIsNone(R.yoy_scope('台積電 2020年1月","yptydevice":"desktop"'))
        self.assertIsNone(R.yoy_scope("中鋼 109年1月自結合併營收及稅前盈餘 年增5%"))
        self.assertIsNone(R.yoy_scope(""))
        self.assertIsNone(R.yoy_scope(None))


class TestTitleYearConflict(unittest.TestCase):
    def test_衝突(self):
        self.assertTrue(R.title_year_conflict(
            "宏碁智新 115年6月營收1.13億、年增20.47%", 111, 6))
        self.assertTrue(R.title_year_conflict(
            "【公告】家碩 2026年6月合併營收1.48億元 年增27.62%", 109, 6))

    def test_不衝突(self):
        self.assertFalse(R.title_year_conflict("宏碁智新 113年10月 相關", 113, 10))
        self.assertFalse(R.title_year_conflict("宏碁智新 2024年10月 相關", 113, 10))

    def test_沒有可比對年月字樣不算衝突(self):
        # Yahoo 模板碎片、CMoney 彙總頁：沒有「N年M月」可比 → 無證據，不判衝突
        self.assertFalse(R.title_year_conflict("台積電 相關 <a class", 109, 4))
        self.assertFalse(R.title_year_conflict("", 109, 4))
        self.assertFalse(R.title_year_conflict(None, 109, 4))

    def test_只比同月份的年(self):
        # title 提到別月份的別年（如「114年3月」），不該影響 6 月這筆的判定
        self.assertFalse(R.title_year_conflict("某股 109年6月營收8億（前值見114年3月）", 109, 6))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestUrlProvenance(unittest.TestCase):
    """來源網址：解 Yahoo 轉址、往回綁定、格式把關（見 revlib「來源網址」段）。"""

    def test_unwrap_yahoo_redirect(self):
        u = ("https://r.search.yahoo.com/_ylt=AwrPqh1D;_ylu=Y29sbwNzZzMEcG9zAzM/"
             "RV=2/RE=1788863555/RO=10/"
             "RU=https%3a%2f%2fwww.moneydj.com%2fkmdj%2fnews%2fnewsviewer.aspx%3fa%3dabc"
             "/RK=2/RS=xxxx-")
        self.assertEqual(
            R.unwrap_url(u),
            "https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=abc")

    def test_unwrap_leaves_plain_url_alone(self):
        self.assertEqual(R.unwrap_url("https://example.com/a?b=1"),
                         "https://example.com/a?b=1")

    def test_clean_url_scheme_whitelist(self):
        self.assertIsNone(R.clean_url("javascript:alert(1)"))
        self.assertIsNone(R.clean_url("//example.com"))
        self.assertIsNone(R.clean_url(None))
        self.assertIsNone(R.clean_url(12345))
        self.assertEqual(R.clean_url("  https://a.tw/x  "), "https://a.tw/x")

    def test_clean_url_truncates(self):
        long = "https://a.tw/" + "x" * 1000
        self.assertEqual(len(R.clean_url(long)), R.MAX_URL)

    def test_nearest_url_looks_backward_not_forward(self):
        # ⚠️ 這條是整個機制的關鍵：連結在日期【前面】。往前找會抓到下一筆結果的
        # 連結，看起來像有效出處但完全指錯篇——比沒有 URL 更糟。
        html = ('<a href="https://right.tw/a">台塑 111年10月營收</a>'
                '<p>2022年11月8日 · 台塑10月營收</p>'
                '<a href="https://wrong.tw/b">南亞 111年10月營收</a>')
        hit = R.parse_detail(html, "台塑", 111, 10)
        self.assertIsNotNone(hit)
        self.assertEqual(R.nearest_url(html, hit[2]), "https://right.tw/a")

    def test_nearest_url_none_on_plain_text(self):
        # google 的 inner_text、gemini 的模型回覆都沒有標籤：回 None 是預期行為。
        self.assertIsNone(R.nearest_url("台塑 111年10月 2022-11-08", 5))
        self.assertIsNone(R.nearest_url("", 0))
        self.assertIsNone(R.nearest_url("<a href='x'>y</a>", None))

    def test_nearest_url_ignores_links_after_offset(self):
        html = '<p>2022年11月8日</p><a href="https://after.tw/a">後面的</a>'
        self.assertIsNone(R.nearest_url(html, 5))

    def test_parse_detail_offset_and_parse_stays_two_tuple(self):
        html = '<a href="https://x.tw/a">台塑 111年10月</a><p>2022-11-08</p>'
        d = R.parse_detail(html, "台塑", 111, 10)
        self.assertEqual(d[0], "2022-11-08")
        self.assertEqual(html[d[2]:d[2] + 10], "2022-11-08")
        # 既有呼叫端與測試都靠這個兩元組，不可以被 parse_detail 帶著改形狀。
        self.assertEqual(R.parse(html, "台塑", 111, 10), (d[0], d[1]))

    def test_parse_and_parse_detail_agree_on_miss(self):
        self.assertIsNone(R.parse_detail("沒有日期", "台塑", 111, 10))
        self.assertIsNone(R.parse("沒有日期", "台塑", 111, 10))

    def test_pick_url_by_name(self):
        links = [("Yahoo奇摩股市", "https://tw.stock.yahoo.com/"),
                 ("台塑 111年10月營收 - MoneyDJ", "https://moneydj.com/a"),
                 ("台塑化 111年10月營收", "https://other.tw/b")]
        self.assertEqual(R.pick_url_by_name(links, "台塑"), "https://moneydj.com/a")

    def test_pick_url_by_name_returns_none_rather_than_guess(self):
        # 挑不到就回 None。退而求其次挑第一個連結幾乎一定是錯的（多半是頁面導覽列）。
        links = [("Yahoo奇摩股市", "https://tw.stock.yahoo.com/")]
        self.assertIsNone(R.pick_url_by_name(links, "台塑"))
        self.assertIsNone(R.pick_url_by_name([], "台塑"))
        self.assertIsNone(R.pick_url_by_name(links, ""))

    def test_pick_url_by_name_skips_bad_scheme(self):
        links = [("台塑 111年10月", "javascript:void(0)")]
        self.assertIsNone(R.pick_url_by_name(links, "台塑"))

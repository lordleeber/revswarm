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

    def test_clean_url_rejects_overlong_instead_of_truncating(self):
        # ⚠️ 截一半的網址是「看起來合法、點下去 404」——比 NULL 更糟，NULL 至少
        # 誠實說沒有出處。這一欄的全部用途就是點得開回到原文。
        self.assertIsNone(R.clean_url("https://a.tw/" + "x" * 1000))
        edge = "https://a.tw/" + "x" * (R.MAX_URL - len("https://a.tw/"))
        self.assertEqual(R.clean_url(edge), edge)          # 剛好等於上限：收

    def test_is_ad_sponsor_url_matches_known_ad_url(self):
        # 實測案例：yahoo SERP 頁固定嵌一段廣告贊助 iframe，_anchor_offsets 的
        # 「公司名+年+月」正則會誤咬到頁面裡的 JSON 追蹤片段（如
        # `"yptydevice":"desktop","yPropertySection":"yahoo`），往回找到的
        # <a href> 剛好都是這顆固定廣告連結——跟真正搜尋結果無關。
        self.assertTrue(R.is_ad_sponsor_url(
            "https://tw.emarketing.yahoo.com/ysmacq/index.html?_ycmp=ad_sponsor"))

    def test_is_ad_sponsor_url_ignores_query_string_variance(self):
        self.assertTrue(R.is_ad_sponsor_url(
            "https://tw.emarketing.yahoo.com/ysmacq/index.html?_ycmp=other&x=1"))

    def test_is_ad_sponsor_url_false_for_real_article(self):
        self.assertFalse(R.is_ad_sponsor_url("https://moneydj.com/right"))
        self.assertFalse(R.is_ad_sponsor_url("https://tw.stock.yahoo.com/news/x"))

    def test_is_ad_sponsor_url_false_for_none(self):
        self.assertFalse(R.is_ad_sponsor_url(None))

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

    def test_parse_detail_offset_is_the_anchor_when_anchored(self):
        html = '<a href="https://x.tw/a">台塑 111年10月</a><p>2022-11-08</p>'
        d = R.parse_detail(html, "台塑", 111, 10)
        self.assertEqual(d[0], "2022-11-08")
        self.assertEqual(d[2], R._anchor_offsets(html, "台塑", 111, 10)[0])
        # 既有呼叫端與測試都靠這個兩元組，不可以被 parse_detail 帶著改形狀。
        self.assertEqual(R.parse(html, "台塑", 111, 10), (d[0], d[1]))

    def test_offset_binds_to_anchor_even_when_date_precedes_it(self):
        """
        ⚠️ 這條擋的是最陰的那個錯法：窗內日期出現在錨點【前面】時（上一筆結果的摘要
        裡就有一個窗內日期），從日期位置往回找會抓到上一筆結果的連結——看起來像有效
        出處卻指錯篇。錨點落在該筆結果的標題文字裡，往回一定命中它自己的 <a href>。
        """
        html = ('<a href="https://wrong.tw/prev">別筆結果</a>'
                '<p>2022年11月8日</p>'
                '<a href="https://right.tw/this">台塑 111年10月營收</a>')
        d = R.parse_detail(html, "台塑", 111, 10)
        self.assertEqual(d[0], "2022-11-08")
        self.assertEqual(R.nearest_url(html, d[2]), "https://right.tw/this")

    def test_offset_falls_back_to_date_when_no_anchor(self):
        # 沒有錨點時只剩日期位置可用（parse 的後備路徑，精度本來就較低）。
        html = '<a href="https://x.tw/a">某篇沒有錨點的文章</a><p>2022-11-08</p>'
        d = R.parse_detail(html, "台塑", 111, 10)
        self.assertEqual(html[d[2]:d[2] + 10], "2022-11-08")
        self.assertEqual(R.nearest_url(html, d[2]), "https://x.tw/a")

    def test_parse_and_parse_detail_agree_on_miss(self):
        self.assertIsNone(R.parse_detail("沒有日期", "台塑", 111, 10))
        self.assertIsNone(R.parse("沒有日期", "台塑", 111, 10))

    def test_pick_url_by_name(self):
        links = [("Yahoo奇摩股市", "https://tw.stock.yahoo.com/"),
                 ("台塑 111年10月營收 - MoneyDJ", "https://moneydj.com/a"),
                 ("台塑化 111年10月營收", "https://other.tw/b")]
        self.assertEqual(R.pick_url_by_name(links, "台塑", 111, 10),
                         "https://moneydj.com/a")

    def test_pick_url_by_name_does_not_fall_for_prefix_collision(self):
        # ⚠️ 這是 _anchor_offsets 花一整段 docstring 在擋的那個坑：「統一」不可以
        # 吃到「統一超」。用 `name in text` 寫就會中；url 指到別家公司比沒有 url 更糟。
        links = [("統一超 109年1月營收", "https://wrong.tw/7"),
                 ("統一 109年1月營收", "https://right.tw/uni")]
        self.assertEqual(R.pick_url_by_name(links, "統一", 109, 1),
                         "https://right.tw/uni")

    def test_pick_url_by_name_requires_matching_year_and_month(self):
        # 錨點鎖的是「這個任務的年月」，別年/別月的同名文章不算數。
        links = [("台塑 110年10月營收", "https://wrong.tw/other-year")]
        self.assertIsNone(R.pick_url_by_name(links, "台塑", 111, 10))
        links = [("台塑 111年9月營收", "https://wrong.tw/other-month")]
        self.assertIsNone(R.pick_url_by_name(links, "台塑", 111, 10))

    def test_pick_url_by_name_returns_none_rather_than_guess(self):
        # 挑不到就回 None。退而求其次挑第一個連結幾乎一定是錯的（多半是頁面導覽列）。
        links = [("Yahoo奇摩股市", "https://tw.stock.yahoo.com/")]
        self.assertIsNone(R.pick_url_by_name(links, "台塑", 111, 10))
        self.assertIsNone(R.pick_url_by_name([], "台塑", 111, 10))
        self.assertIsNone(R.pick_url_by_name(links, "", 111, 10))

    def test_pick_url_by_name_skips_bad_scheme(self):
        links = [("台塑 111年10月", "javascript:void(0)")]
        self.assertIsNone(R.pick_url_by_name(links, "台塑", 111, 10))


class TestLongerNameInText(unittest.TestCase):
    """
    「title 裡是不是提到一家名字更長、以本公司名開頭的別家公司」。

    ⚠️ 這正是 _anchor_offsets 的 lookahead 在擋的那個坑，但**後備路徑完全繞過它**
    ——沒有錨點就沒有那道保護，於是 4113 聯上 的任務吃到 聯上發(2537) 的公告日。
    實測全庫 12 筆，全部出自 google worker（見 data/title_review.csv）。
    """

    NAMES = {"聯上": "4113", "聯上發": "2537", "統一": "1216", "統一超": "2912",
             "台塑": "1301", "台塑化": "6505"}

    def test_detects_longer_company(self):
        self.assertEqual(
            R.longer_name_in_text("公告-聯上發-2020… 聯上發. 253", "聯上", "4113",
                                  self.NAMES), "聯上發")

    def test_own_name_alone_is_not_a_collision(self):
        self.assertIsNone(
            R.longer_name_in_text("聯上 109年4月營收", "聯上", "4113", self.NAMES))

    def test_same_stock_under_a_longer_alias_is_not_a_collision(self):
        # 名單裡若同一檔有更長的別名，不算撞名——比對的是股號不是字串。
        names = {"聯上": "4113", "聯上開發": "4113"}
        self.assertIsNone(
            R.longer_name_in_text("聯上開發 109年4月", "聯上", "4113", names))

    def test_returns_the_longest_match(self):
        names = {"統一": "1216", "統一超": "2912", "統一超商": "9999"}
        self.assertEqual(
            R.longer_name_in_text("統一超商 109年1月", "統一", "1216", names), "統一超商")

    def test_name_absent_entirely(self):
        self.assertIsNone(
            R.longer_name_in_text("完全沒提到", "聯上", "4113", self.NAMES))

    def test_own_anchor_present_beats_a_trailing_mention(self):
        """
        ⚠️ 本檔自己的錨點命中時，後面出現更長的公司名不算撞名。

        實測誤報：1216 統一 110/6 的 raw_title 是
          「【公告】統一2021年6月合併營收392.53億元年增1.65%. 上一則 … 統一超表現備」
        開頭就是本檔正確的公告，「統一超」只是尾巴「相關文章」的碎片。把這種判成
        抓錯公司會害一筆正確的資料被打回重爬——降級 success 是不可逆的。
        """
        t = "【公告】統一2021年6月合併營收392.53億元年增1.65%. 上一則 … 統一超表現備"
        self.assertIsNone(
            R.longer_name_in_text(t, "統一", "1216", self.NAMES,
                                  roc_year=110, roc_month=6))
        # 沒有年月參數時維持原本的純字串行為（呼叫端自己負責過濾）
        self.assertEqual(
            R.longer_name_in_text(t, "統一", "1216", self.NAMES), "統一超")

    def test_anchor_for_a_different_month_does_not_protect(self):
        # 錨點要是「這個任務的年月」才算數，別月的公告救不了這一筆。
        t = "【公告】統一2021年5月合併營收… 統一超表現備"
        self.assertEqual(
            R.longer_name_in_text(t, "統一", "1216", self.NAMES,
                                  roc_year=110, roc_month=6), "統一超")

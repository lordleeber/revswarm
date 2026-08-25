#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_date_overrides 的回歸（不連網；寫入路徑用 :memory: 跑真 schema）。

重點在守住那條刻意的不對稱（見 apply_date_overrides 模組 docstring）：
  ✓ 月份必須是「營收月的次月」——強不變量，錯了一定是打錯或誤判 → 拒
  ✗ 不限制幾號——遲交上限是個案問題，由 note 與人負責 → 16、17、28 號都要收

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_date_overrides
"""

import csv
import io
import sqlite3
import os
import unittest

import apply_date_overrides as ado
import revlib
import server

_TITLE = "誠研109年1月營收6727萬、年減13.65%"


def _row(**over):
    r = {"stock_id": "3494", "roc_year": "109", "roc_month": "1",
         "announce_date": "2020-02-17", "source": "g_roc_manual",
         "raw_title": _TITLE, "note": "實測遲交"}
    r.update(over)
    return r


def _cur(**over):
    """假 lookup 的回傳（欄位比照 main() 的 SELECT）。"""
    c = {"id": 1, "state": "failed", "announce_date": None, "source": None,
         "raw_title": None, "revenue": None, "yoy": None}
    c.update(over)
    return c


def _csv(body):
    """把一段 CSV 文字讀成 (fieldnames, [(行號, row)])，等同 load_overrides 讀檔。"""
    rd = csv.DictReader(io.StringIO(body), restkey=ado._EXTRA)
    return tuple(rd.fieldnames or ()), [(rd.line_num, r) for r in rd]


_HEADER = ",".join(ado.FIELDS) + "\n"


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

    def test_unpadded_date_is_rejected(self):
        """⚠️ '2020-2-17' 的日子是對的，但稽核 SQL 的 substr(...,9,2) 會取到 '7' →
        CAST 7 落在 1~15 → 這筆窗外 success 從稽核查詢裡消失。所以格式要嚴。"""
        for d in ("2020-2-17", "2020-02-7", "20-02-17"):
            err = ado.check_row(_row(announce_date=d))
            self.assertIsNotNone(err, d)
            self.assertIn("補零", err)

    def test_canonical_date_normalizes_whitespace(self):
        """進 DB 的日期由 canonical_date 建構保證是補零格式，不靠 CSV 剛好打對。"""
        row = _row(announce_date=" 2020-02-17 ")
        self.assertIsNone(ado.check_row(row))
        self.assertEqual(ado.canonical_date(row), "2020-02-17")

    def test_source_must_end_with_manual(self):
        """窗外 success 只有這個檔會產生；標記掉了就分不出人工與爬蟲結果。"""
        err = ado.check_row(_row(source="g_roc"))
        self.assertIsNotNone(err)
        self.assertIn("manual", err)
        self.assertIsNone(ado.check_row(_row(source="")))          # 空 → 預設 manual
        self.assertIsNone(ado.check_row(_row(source="g_roc_manual")))

    def test_extra_column_is_rejected(self):
        """note 裡打半形逗號會讓 DictReader 只留逗號前那半段證據——不能靜默吃掉。"""
        _, rows = _csv(_HEADER + "3494,109,1,2020-02-17,g_roc_manual,t,note-前,note-後\n")
        err = ado.check_row(rows[0][1])
        self.assertIsNotNone(err)
        self.assertIn("欄位數", err)


class TestValidateAll(unittest.TestCase):
    """整份 CSV 的驗證：表頭、重複 key、行號（這些逐列驗看不出來）。"""

    def test_clean_csv_has_no_problems(self):
        fn, rows = _csv(_HEADER + f"3494,109,1,2020-02-17,g_roc_manual,{_TITLE},遲交\n")
        self.assertEqual(ado.validate_all(fn, rows), [])

    def test_duplicate_key_is_rejected(self):
        """同一個 (stock_id,roc_year,roc_month) 兩行：兩條 UPDATE 都會跑、後者無聲
        蓋掉前者（build_updates 看到的都是寫入前的舊狀態），所以擋在驗證階段。"""
        fn, rows = _csv(_HEADER
                        + "3494,109,1,2020-02-17,g_roc_manual,t,第一版\n"
                        + "3494,109,1,2020-02-16,g_roc_manual,t,複製一行改一半\n")
        probs = ado.validate_all(fn, rows)
        self.assertEqual(len(probs), 1)
        self.assertEqual(probs[0][0], 3)                 # 指的是後面那一行
        self.assertIn("重複", probs[0][2])

    def test_all_problems_reported_in_one_pass(self):
        """壞掉的那行不會遮住它造成的重複——一輪報完，不必修一個跑一次。"""
        fn, rows = _csv(_HEADER
                        + "3494,109,1,2020-02-17,g_roc,t,source 少了後綴\n"
                        + "3494,109,1,2020-02-16,g_roc_manual,t,又重複\n")
        probs = ado.validate_all(fn, rows)
        self.assertEqual([p[0] for p in probs], [2, 3])
        self.assertIn("manual", probs[0][2])
        self.assertIn("重複", probs[1][2])

    def test_header_typo_is_rejected_and_stops_there(self):
        fn, rows = _csv(_HEADER.replace("note", "notes")
                        + "3494,109,1,2020-02-17,g_roc_manual,t,遲交\n")
        probs = ado.validate_all(fn, rows)
        self.assertEqual(len(probs), 1)                  # 表頭錯就不必逐列再抱怨
        self.assertIn("表頭", probs[0][2])

    def test_line_number_survives_embedded_newline(self):
        """行號取 reader.line_num：note 裡有換行時，enumerate 出來的序號會漂掉。"""
        fn, rows = _csv(_HEADER
                        + '3494,109,1,2020-02-17,g_roc_manual,t,"證據第一行\n第二行"\n'
                        + "3494,109,2,2020-03-99,g_roc_manual,t,壞掉的日期\n")
        probs = ado.validate_all(fn, rows)
        self.assertEqual(len(probs), 1)
        self.assertEqual(probs[0][0], 4)                 # 檔案實際行號，不是第 3 筆


class TestBuildUpdates(unittest.TestCase):
    def _lookup(self, cur):
        return lambda sid, ry, rm: cur

    def test_already_matching_is_skipped(self):
        """冪等：重建 DB 後可以放心重跑，已一致的不會再寫。"""
        cur = _cur(state="success", announce_date="2020-02-17", source="g_roc_manual",
                   raw_title=_TITLE, revenue=67270000, yoy=-13.65)
        up, skip, orph = ado.build_updates([_row()], self._lookup(cur))
        self.assertEqual((len(up), len(skip), len(orph)), (0, 1, 0))

    def test_edited_raw_title_is_not_skipped(self):
        """⚠️ 「一致」要比到 raw_title 與由它導出的 revenue/yoy：只比日期與 source 的話，
        把 CSV 裡打錯的 raw_title 改對之後會被判「已一致」，revenue 永遠停在舊值。"""
        cur = _cur(state="success", announce_date="2020-02-17", source="g_roc_manual",
                   raw_title="誠研109年1月營收6727萬、年減13.65%",
                   revenue=67270000, yoy=-13.65)
        fixed = _row(raw_title="誠研109年1月營收9999萬、年減1.00%")
        up, skip, _ = ado.build_updates([fixed], self._lookup(cur))
        self.assertEqual((len(up), len(skip)), (1, 0))
        self.assertEqual(up[0]["revenue"], 99990000)
        self.assertEqual(up[0]["yoy"], -1.0)

    def test_failed_task_gets_updated_and_revenue_derived(self):
        """failed → success，且 revenue/yoy 由 raw_title 導出（與 server 同一條路）。"""
        up, skip, orph = ado.build_updates([_row()], self._lookup(_cur(id=7)))
        self.assertEqual((len(up), len(skip), len(orph)), (1, 0, 0))
        self.assertEqual(up[0]["id"], 7)
        self.assertEqual(up[0]["announce_date"], "2020-02-17")
        self.assertEqual(up[0]["revenue"], 67270000)
        self.assertEqual(up[0]["yoy"], -13.65)
        self.assertEqual(up[0]["source"], "g_roc_manual")

    def test_missing_task_is_orphan_not_crash(self):
        up, skip, orph = ado.build_updates([_row()], self._lookup(None))
        self.assertEqual((len(up), len(skip), len(orph)), (0, 0, 1))

    def test_source_defaults_to_manual(self):
        up, _, _ = ado.build_updates([_row(source="")], self._lookup(_cur()))
        self.assertEqual(up[0]["source"], "manual")

    def test_no_raw_title_leaves_revenue_none(self):
        up, _, _ = ado.build_updates([_row(raw_title="")], self._lookup(_cur()))
        self.assertIsNone(up[0]["revenue"])
        self.assertIsNone(up[0]["raw_title"])


class TestWritePath(unittest.TestCase):
    """真 schema、真 SQL（:memory:）——把「寫進去長什麼樣」釘住，不只驗純函式。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(server.SCHEMA)
        self.conn.execute(
            "INSERT INTO tasks (stock_id, name, roc_year, roc_month, state, fail_count)"
            " VALUES ('3494','誠研',109,1,'failed',3)")
        self.conn.commit()

    def _lookup(self, sid, ry, rm):
        return self.conn.execute(
            "SELECT id, state, announce_date, source, raw_title, revenue, yoy"
            " FROM tasks WHERE stock_id=? AND roc_year=? AND roc_month=?",
            (sid, ry, rm)).fetchone()

    def _apply(self, row):
        up, skip, orph = ado.build_updates([row], self._lookup)
        ado.apply_updates(self.conn, up, 1600000000)
        return len(up), len(skip), len(orph)

    def _task(self):
        return self._lookup("3494", 109, 1)

    def test_failed_becomes_success_with_canonical_date(self):
        self.assertEqual(self._apply(_row(announce_date=" 2020-02-17 ")), (1, 0, 0))
        t = self._task()
        self.assertEqual((t["state"], t["announce_date"]), ("success", "2020-02-17"))
        self.assertEqual((t["revenue"], t["yoy"]), (67270000, -13.65))
        self.assertEqual(t["source"], "g_roc_manual")
        row = self.conn.execute(
            "SELECT worker_id, dispatched_at, fail_count FROM tasks").fetchone()
        # fail_count 是被爬的歷史，刻意不清；租約則要放掉。
        self.assertEqual((row["worker_id"], row["dispatched_at"], row["fail_count"]),
                         ("manual", None, 3))

    def test_audit_query_finds_the_out_of_window_row(self):
        """README 那條稽核 SQL 必須抓到窗外個案——日期格式歪一格就會漏抓。"""
        self._apply(_row())
        hits = self.conn.execute(
            "SELECT stock_id FROM tasks WHERE state='success'"
            " AND CAST(substr(announce_date,9,2) AS INTEGER) NOT BETWEEN 1 AND 15"
        ).fetchall()
        self.assertEqual([r["stock_id"] for r in hits], ["3494"])

    def test_rerun_is_idempotent(self):
        self.assertEqual(self._apply(_row()), (1, 0, 0))
        before = dict(self._task())
        self.assertEqual(self._apply(_row()), (0, 1, 0))     # 第二次全部略過
        self.assertEqual(dict(self._task()), before)

    def test_rerun_after_editing_raw_title_rewrites_revenue(self):
        self._apply(_row())
        self.assertEqual(self._apply(_row(raw_title="誠研109年1月營收9999萬")), (1, 0, 0))
        self.assertEqual(self._task()["revenue"], 99990000)


class TestShippedCsv(unittest.TestCase):
    """進版控的那份 CSV 本身必須永遠是合格的（否則 apply 會整批拒絕）。"""

    def test_repo_csv_is_valid(self):
        fieldnames, rows = ado.load_overrides("data/date_overrides.csv")
        self.assertTrue(rows, "date_overrides.csv 不該是空的")
        self.assertEqual(ado.validate_all(fieldnames, rows), [])

    def test_repo_csv_header_matches_expected_fields(self):
        fieldnames, _ = ado.load_overrides("data/date_overrides.csv")
        self.assertEqual(fieldnames, ado.FIELDS)
        self.assertIsNone(ado.check_header(fieldnames))

    def test_window_upper_bound_still_15(self):
        """釘住這個檔存在的前提：窗沒有被全域放寬（MOPS 2556 筆的上界就是 15）。
        若哪天 in_window 放寬了，這個 override 機制的定位就要重新檢討。"""
        self.assertTrue(revlib.in_window(2020, 2, 15, 109, 1))
        self.assertFalse(revlib.in_window(2020, 2, 16, 109, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRealOverridesFile(unittest.TestCase):
    """
    版控裡那份 data/date_overrides.csv 本身要通過驗證。

    ⚠️ 這支盯的是「真檔案」而不是合成字串：這個檔是人工逐行維護的，最典型的失誤
    （複製一行改一半、note 裡打了半形逗號、日期沒補零）只有拿真檔去驗才擋得到。
    DB 是執行期產物、不進版控，所以這個檔就是那些人工判斷的唯一存放處。
    """

    PATH = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "date_overrides.csv")

    def test_real_file_validates(self):
        fn, rows = ado.load_overrides(self.PATH)
        self.assertEqual(ado.validate_all(fn, rows), [])

    def test_carries_the_fuhua_correction(self):
        """
        5465 富驊 112/12：DB 原本是 2024-01-08，比 MOPS 官方申報日早 1 天。

        這一筆與那份檔案原本收的「遲交」個案不同——它不是窗外，是**窗內但抓錯**。
        重跑證實了：今天用同樣兩種查詢重抓，兩種都得到 2024-01-09（＝MOPS），
        出處是玉山證券那篇，原文寫「2024年1月9日 · 公司名稱：富驊 (5465)發言人：
        王駿東 (總經理)」——那是 MOPS 申報公告的格式。
        """
        _, rows = ado.load_overrides(self.PATH)
        hit = [r for _, r in rows
               if (r["stock_id"], r["roc_year"], r["roc_month"]) == ("5465", "112", "12")]
        self.assertEqual(len(hit), 1, "富驊 112/12 的更正不在 date_overrides.csv 裡")
        self.assertEqual(hit[0]["announce_date"], "2024-01-09")
        self.assertTrue(hit[0]["source"].endswith("manual"))
        self.assertIn("2024-01-09", hit[0]["note"])

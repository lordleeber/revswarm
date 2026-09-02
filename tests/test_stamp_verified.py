#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stamp_verified 的候選挑選邏輯回歸測試。純標準庫 unittest，不連網、不碰真 DB。

守的是這支唯一容易寫錯、且錯了會靜默污染 verified 欄位的地方：
  ⚠️ gemini 那條路**必須**同時要求「status=ok」「gemini_date == mops_date」。
     少任何一個條件，蓋出來的章就不是「兩個獨立來源同意」，verified 這一欄的
     全部意義就沒了（而且是靜默的——欄位裡照樣有值，只是不再代表任何事）。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_stamp_verified
"""

import csv
import io
import os
import tempfile
import unittest

import stamp_verified as sv
from mops.gemini_benchmark import FIELDS


def _write_bench(path, rows):
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


class TestItemsFromGemini(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "b.csv")

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        os.rmdir(self.dir)

    def _rows(self, rows):
        _write_bench(self.path, rows)
        return sv.items_from_gemini(self.path)

    def test_takes_rows_where_gemini_agrees_with_mops(self):
        items = self._rows([{"stock_id": "1301", "roc_year": "111", "roc_month": "10",
                             "mops_date": "2022-11-08", "gemini_date": "2022-11-08",
                             "status": "ok"}])
        self.assertEqual(items, [{"stock_id": "1301", "roc_year": 111,
                                  "roc_month": 10, "date": "2022-11-08"}])

    def test_drops_disagreement(self):
        # 兩邊打架就是不蓋。這一筆值得人看，不該被一個章掩蓋。
        self.assertEqual(self._rows([
            {"stock_id": "1301", "roc_year": "111", "roc_month": "10",
             "mops_date": "2022-11-08", "gemini_date": "2022-11-09",
             "status": "ok"}]), [])

    def test_drops_non_ok_status(self):
        # nosearch/error 那幾筆根本沒有結論。
        for st in ("nosearch", "error", "fatal", ""):
            self.assertEqual(self._rows([
                {"stock_id": "1301", "roc_year": "111", "roc_month": "10",
                 "mops_date": "2022-11-08", "gemini_date": "2022-11-08",
                 "status": st}]), [], st)

    def test_drops_empty_gemini_date(self):
        # gemini 沒抽到日期時 gemini_date 是空字串；空 == 空 不可以被當成一致。
        self.assertEqual(self._rows([
            {"stock_id": "1301", "roc_year": "111", "roc_month": "10",
             "mops_date": "", "gemini_date": "", "status": "ok"}]), [])

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            sv.items_from_gemini(os.path.join(self.dir, "nope.csv"))


class TestItemsFromReview(unittest.TestCase):
    """
    人工讀 title 的判斷 → 候選清單。

    ⚠️ 判斷存成版控裡的 CSV 而不是直接寫 DB，是因為它是**主觀的**：mops/gemini 那兩條
    路隨時可以重跑重現，這條不行。留檔才有得稽核「當初為什麼判高信心」，DB 重建後也
    補得回來（DB 是執行期產物、不進版控）。

    CSV 的 announce_date 是「讀的當下看到的日期」，送進 /verify 當樂觀鎖：那之後日期
    若被別的東西改掉（mops_overwrite、date_overrides…），比對不上就記成 mismatch、
    不蓋章——判斷是對著舊日期做的，不該套到新日期上。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "r.csv")

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        os.rmdir(self.dir)

    def _write(self, body):
        io.open(self.path, "w", encoding="utf-8").write(
            "stock_id,roc_year,roc_month,announce_date,verdict,note\n" + body)

    def test_filters_by_verdict(self):
        self._write("1301,111,10,2022-11-08,claude,標題三要素齊全\n"
                    "2330,109,1,2020-02-10,tbd,標題被截斷\n")
        self.assertEqual(sv.items_from_review(self.path, "claude"),
                         [{"stock_id": "1301", "roc_year": 111,
                           "roc_month": 10, "date": "2022-11-08"}])
        self.assertEqual([i["stock_id"] for i in
                          sv.items_from_review(self.path, "tbd")], ["2330"])

    def test_unknown_verdict_is_rejected(self):
        # 打錯字不可以靜默變成「這個 verdict 沒有任何列」——那會讓整批無聲跳過。
        self._write("1301,111,10,2022-11-08,claude,ok\n"
                    "2330,109,1,2020-02-10,claud,打錯字\n")
        with self.assertRaises(SystemExit):
            sv.items_from_review(self.path, "claude")

    def test_note_is_required(self):
        # 判斷是主觀的，沒有理由就沒有稽核價值。
        self._write("1301,111,10,2022-11-08,claude,\n")
        with self.assertRaises(SystemExit):
            sv.items_from_review(self.path, "claude")

    def test_the_last_row_for_a_key_wins(self):
        """⚠️ 同一個月份出現第二列判斷是**合法的**：tbd 是「看過了但沒把握」，
        那一筆之後可能被重讀、改判 claude（或反過來）。這種檔是 append-only 的，
        所以後寫的就是後審的——以最後一列為準，不要整批停擺。"""
        self._write("1301,111,10,2022-11-08,tbd,先擱著\n"
                    "1301,111,10,2022-11-08,claude,重讀後確認三要素齊全\n")
        self.assertEqual([i["stock_id"] for i in
                          sv.items_from_review(self.path, "claude")], ["1301"])
        self.assertEqual(sv.items_from_review(self.path, "tbd"), [])

    def test_a_repeated_key_does_not_block_the_rest_of_the_batch(self):
        """⚠️ 這才是舊行為真正的代價：一列重複就 sys.exit，整批（含幾萬列無關的
        判斷）一列都蓋不到章，而檔案又是 append-only、不准刪列——等於沒有出路。"""
        self._write("1301,111,10,2022-11-08,tbd,先擱著\n"
                    "1301,111,10,2022-11-08,claude,重讀後確認\n"
                    "2330,109,1,2020-02-10,claude,無關的另一筆\n")
        self.assertEqual([i["stock_id"] for i in
                          sv.items_from_review(self.path, "claude")],
                         ["1301", "2330"])

    def test_a_superseded_row_is_still_validated(self):
        """被覆蓋掉不等於不必檢查：打錯字的 verdict 仍然是壞資料，要硬錯。"""
        self._write("1301,111,10,2022-11-08,claud,打錯字\n"
                    "1301,111,10,2022-11-08,claude,第二版\n")
        with self.assertRaises(SystemExit):
            sv.items_from_review(self.path, "claude")

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            sv.items_from_review(os.path.join(self.dir, "nope.csv"), "claude")


class TestItemsFromGeminiReview(unittest.TestCase):
    """
    Claude 逐筆審 gemini 交回的證據 → 候選清單（data/gemini_review.csv）。

    ⚠️ 這條路蓋的章是 `claude` 不是 `gemini`：判斷者讀的是 gemini 自己回的那段文字，
    沒有引入新證據——那是循環，不是第二個獨立來源（見 README「claude 不是驗證」）。
    蓋成 `gemini` 會讓它在 VERIFIER_RANK 裡爬到 claude 之上、覆蓋不該覆蓋的章。

    reject 的列**不送**：那些筆在 DB 裡是 state='failed'、沒有 announce_date 可核對。
    但它們留在 CSV 裡才是這張表的重點——被否決的證據長什麼樣，只有這裡記得住。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "g.csv")

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        os.rmdir(self.dir)

    def _write(self, body):
        io.open(self.path, "w", encoding="utf-8").write(
            ",".join(sv.GEMINI_REVIEW_FIELDS) + "\n" + body)

    def test_takes_only_approved_rows(self):
        self._write("1101,109,1,2020-02-10,m_txt,,moneydj.com,approve,標題含金額\n"
                    "2330,109,1,,m_txt,,goodinfo.tw,reject,模型回 NONE\n")
        self.assertEqual(sv.items_from_gemini_review(self.path),
                         [{"stock_id": "1101", "roc_year": 109,
                           "roc_month": 1, "date": "2020-02-10"}])

    def test_stamps_as_the_reviewer_never_as_gemini(self):
        # ⚠️ 這條路蓋的是【審核者】的章。蓋成 gemini 等於宣稱有第二個獨立來源，
        # 而且會在 VERIFIER_RANK 裡爬到審核者之上、覆蓋掉不該覆蓋的章。
        for name in sv.REVIEWERS:
            self.assertEqual(sv.verifier_for("gemini-review", name), name)
            self.assertNotEqual(sv.verifier_for("gemini-review", name), "gemini")
        self.assertEqual(sv.verifier_for("mops"), "mops")

    def test_unknown_verdict_is_rejected(self):
        self._write("1101,109,1,2020-02-10,m_txt,,x.com,approv,打錯字\n")
        with self.assertRaises(SystemExit):
            sv.items_from_gemini_review(self.path)

    def test_note_is_required(self):
        self._write("1101,109,1,2020-02-10,m_txt,,x.com,approve,\n")
        with self.assertRaises(SystemExit):
            sv.items_from_gemini_review(self.path)

    def test_approved_row_without_a_date_is_rejected(self):
        """⚠️ 沒有日期的 approve 是自相矛盾（核准了什麼？），不可以靜默跳過。"""
        self._write("1101,109,1,,m_txt,,x.com,approve,看起來對\n")
        with self.assertRaises(SystemExit):
            sv.items_from_gemini_review(self.path)

    def test_the_last_row_for_a_key_wins(self):
        """⚠️ 同一個月份被審第二次是**這條流程保證會發生的事**：SKILL 第 1 步在佇列
        排空時要 `POST /admin/requeue-failed?engine=gemini`，那會把先前 reject 的每
        一筆重新排回 gemini 再審一次。檔案是 append-only（`_append_review_row` 用
        "a" 模式、不去重）且 docstring 明文不准刪列，所以後寫的就是後審的：以最後
        一列為準。

        ⚠️ 覆寫只影響「這次送出哪些候選」；先前已經蓋上的章不會因為後來改判 reject
        就被撤掉（撤章要另外處理）。"""
        self._write("1101,109,1,,m_txt,,[],reject,證據不足\n"
                    "1101,109,1,2020-02-10,m_txt,,[\"moneydj.com\"],approve,重審拿到 MoneyDJ 原文\n")
        self.assertEqual(sv.items_from_gemini_review(self.path),
                         [{"stock_id": "1101", "roc_year": 109,
                           "roc_month": 1, "date": "2020-02-10"}])

    def test_a_later_reject_supersedes_an_earlier_approve(self):
        self._write("1101,109,1,2020-02-10,m_txt,,[],approve,當時看起來夠\n"
                    "1101,109,1,,m_txt,,[],reject,對照 MOPS 後發現日期打架\n")
        self.assertEqual(sv.items_from_gemini_review(self.path), [])

    def test_a_repeated_key_does_not_block_the_rest_of_the_batch(self):
        """⚠️ 舊行為真正的代價：重審一筆就 sys.exit，同一批裡每一個無關的 approve
        也一起蓋不到章。"""
        self._write("1101,109,1,,m_txt,,[],reject,證據不足\n"
                    "1101,109,1,2020-02-10,m_txt,,[],approve,重審後確認\n"
                    "2330,109,1,2020-02-11,m_txt,,[],approve,無關的另一筆\n")
        self.assertEqual([i["stock_id"] for i in
                          sv.items_from_gemini_review(self.path)],
                         ["1101", "2330"])

    def test_a_superseded_row_is_still_validated(self):
        self._write("1101,109,1,2020-02-10,m_txt,,[],approv,打錯字\n"
                    "1101,109,1,2020-02-10,m_txt,,[],approve,第二版\n")
        with self.assertRaises(SystemExit):
            sv.items_from_gemini_review(self.path)

    def test_wrong_header_exits(self):
        io.open(self.path, "w", encoding="utf-8").write(
            "stock_id,roc_year,roc_month,announce_date,verdict,note\n")
        with self.assertRaises(SystemExit):
            sv.items_from_gemini_review(self.path)

    def test_header_is_the_writers_schema_not_a_copy(self):
        """⚠️ 表頭只有一份定義：寫的人（gemini_worker）宣告，讀的人驗。
        各抄一份就會漂——而漂掉的症狀是「整批無聲跳過」或「硬錯」。"""
        from worker.gemini_worker import REVIEW_FIELDS
        self.assertIs(sv.GEMINI_REVIEW_FIELDS, REVIEW_FIELDS)

    def test_missing_file_exits(self):
        with self.assertRaises(SystemExit):
            sv.items_from_gemini_review(os.path.join(self.dir, "nope.csv"))


class TestPostPayload(unittest.TestCase):
    """送出去的形狀要跟 server 的 /verify 對得上（by + items）。"""

    def test_builds_bearer_and_body(self):
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = req.data
            seen["auth"] = req.headers.get("Authorization")

            class _R:
                def read(self_):
                    return b'{"applied": {"verified": 1}}'

                def __enter__(self_):
                    return self_

                def __exit__(self_, *a):
                    return False
            return _R()

        orig = sv.urllib.request.urlopen
        sv.urllib.request.urlopen = fake_urlopen
        try:
            applied = sv.post("http://h:8000/", "SECRET", "mops",
                              [{"stock_id": "1301", "roc_year": 111,
                                "roc_month": 10, "date": "2022-11-08"}])
        finally:
            sv.urllib.request.urlopen = orig
        self.assertEqual(applied, {"verified": 1})
        self.assertEqual(seen["url"], "http://h:8000/verify")   # 尾斜線不可變成 //verify
        self.assertEqual(seen["auth"], "Bearer SECRET")
        self.assertIn(b'"by": "mops"', seen["body"])


class TestReviewerDrivesTheStamp(unittest.TestCase):
    """--from gemini-review 有兩條線（claude / codex），讀哪份檔與蓋哪個章
    必須來自**同一個** Reviewer，不可以各自查表。

    ⚠️ 這是 2026-09-02 那個 bug 的正臉：稽核檔換成了 codex 那份，蓋章值卻還是
    另一個字——兩邊各改各的、程式不報錯。所以這裡不驗「值對不對」，驗的是
    「兩個值是不是同一個來源給的」。
    """

    def test_the_registry_is_the_writers_not_a_copy(self):
        # 表頭那條規矩（test_header_is_the_writers_schema_not_a_copy）同一個道理：
        # 寫的人宣告，讀的人 import。抄一份就會漂。
        from worker.gemini_worker import REVIEWERS
        self.assertIs(sv.REVIEWERS, REVIEWERS)

    def test_each_reviewer_stamps_its_own_name(self):
        for name, r in sv.REVIEWERS.items():
            self.assertEqual(sv.verifier_for("gemini-review", name), r.stamp)

    def test_each_reviewer_reads_its_own_csv(self):
        seen = set()
        for name, r in sv.REVIEWERS.items():
            path = sv.gemini_review_csv_for(name)
            self.assertEqual(path, r.csv)
            seen.add(path)
        self.assertEqual(len(seen), len(sv.REVIEWERS))

    def test_gemini_review_without_a_reviewer_is_refused(self):
        # 猜錯的代價是「一整批蓋上別人的章」，寧可拒收。
        with self.assertRaises(SystemExit):
            sv.verifier_for("gemini-review", None)

    def test_other_sources_are_unaffected(self):
        self.assertEqual(sv.verifier_for("mops", None), "mops")
        self.assertEqual(sv.verifier_for("claude", None), "claude")


class TestBothStampsAreLegalAndEqualRank(unittest.TestCase):
    """兩條線的章都必須是 server 認得的值，而且**同一階**。

    ⚠️ 若其中一個排名較高，兩條線審同一個月份時，後蓋的會默默覆蓋前一個
    ——但它們的證據強度其實一樣（都是「讀模型自己回的那段文字」）。
    """

    def test_every_stamp_is_on_the_server_whitelist(self):
        import server
        for r in sv.REVIEWERS.values():
            self.assertIn(r.stamp, server.VERIFIERS, r.stamp)

    def test_the_two_lines_rank_the_same(self):
        import server
        ranks = {server.VERIFIER_RANK[r.stamp] for r in sv.REVIEWERS.values()}
        self.assertEqual(len(ranks), 1, ranks)

    def test_neither_line_outranks_gemini_itself(self):
        # 讀 gemini 交回的證據 ≠ 第二個獨立來源（見 verifier_for 的註解）。
        import server
        for r in sv.REVIEWERS.values():
            self.assertLess(server.VERIFIER_RANK[r.stamp],
                            server.VERIFIER_RANK["gemini"], r.stamp)


if __name__ == "__main__":
    unittest.main()

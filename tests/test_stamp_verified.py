#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mops.stamp_verified 的候選挑選邏輯回歸測試。純標準庫 unittest，不連網、不碰真 DB。

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

from mops import stamp_verified as sv
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


if __name__ == "__main__":
    unittest.main()

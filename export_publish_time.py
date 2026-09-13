#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 revswarm 蓋過 verified 章的公布日，寫進 my_stock_project 的月營收原始檔
（data/raw/monthly_revenue/<YYYY>/<YYYY>M<MM>/market.csv 的 publish_time 欄）。

那批檔在 2026M01 以前的 publish_time 是回填的法定截止日（次月 10 號，整月同一個值），
不是真的公布日。這支只換掉「有第二個人看過」的那些格：

  - 只收 state='success' 且 verified ∈ {mops, gemini, claude, codex}。
    NULL 是「沒人看過」、tbd 是「看過但不是高信心」，兩者一律不寫，保留原本的截止日。
  - 只改 market.csv 裡**已經有**的 symbol 的 publish_time 那一格；不新增列、不動其他欄，
    也不碰同目錄的 tmp.csv（那是 scraper 的合併暫存檔）。
  - 日期必須落在營收月的次月，否則跳過並回報（不信任、不寫）。
  - 只處理到 2026M01（LAST_BACKFILLED）；之後的月份 publish_time 已是 scraper 的真實日，不覆蓋。
  - 原檔若「讀進來原樣寫回」會變（BOM/換行/引號不同），整檔拒寫，保證只動那一格。
  - 冪等：沒有變動的檔案不重寫。

⚠️ 目標檔不在 my_stock_project 的版控裡（data/ 被 .gitignore），跑之前自己備份。
⚠️ 寫完只改了 raw CSV；my_stock_project 的 DB 要重跑它自己的 processor/importer 才會看到。

跑法（在 repo 根目錄）：
  python3 -m export_publish_time --dry-run
  python3 -m export_publish_time
"""

import argparse
import csv
import io
import os
import re
import sqlite3
import sys
from collections import defaultdict

DEFAULT_TARGET = os.path.expanduser("~/GitHubLL/my_stock_project/data/raw/monthly_revenue")
TRUSTED = ("mops", "gemini", "claude", "codex")
# 最後一個 publish_time 是「回填截止日」的月份。之後的月份 scraper 寫的是抓到當天的真實日，
# 不覆蓋（那批檔也是無 BOM/LF，寫回會整檔改寫；apply_month 另有原樣寫回檢查兜底）
LAST_BACKFILLED = (2026, 1)
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def load_verified(db_path):
    """{(西元年, 月): {stock_id: 'YYYYMMDD'}}，只含可信章、且日期落在次月的列。"""
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    try:
        rows = con.execute(
            "SELECT stock_id, roc_year, roc_month, announce_date FROM tasks"
            " WHERE state='success' AND verified IN (%s)" % ",".join("?" * len(TRUSTED)),
            TRUSTED,
        ).fetchall()
    finally:
        con.close()
    out = defaultdict(dict)
    for sid, ry, rm, d in rows:
        y = ry + 1911
        mt = _DATE.match(d or "")
        if not mt or (int(mt.group(1)), int(mt.group(2))) != _next_month(y, rm):
            print("skip %s %d/%d: announce_date=%r 不在次月" % (sid, ry, rm, d), file=sys.stderr)
            continue
        out[(y, rm)][sid] = d.replace("-", "")
    return dict(out)


def _serialize(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def apply_month(path, dates, dry_run=False):
    """把 dates（symbol→YYYYMMDD）寫進一份 market.csv 的 publish_time。回傳統計。"""
    with io.open(path, "rb") as f:
        raw = f.read()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    # 寫回用的是 BOM+CRLF+最少引號；原檔不是這個格式就等於整檔改寫 → 拒寫
    if _serialize(rows) != raw:
        raise ValueError("%s: 原樣寫回會改到 publish_time 以外的 byte（BOM/換行/引號），拒寫" % path)
    if not rows or "publish_time" not in rows[0] or "symbol" not in rows[0]:
        raise ValueError("%s: 缺 symbol/publish_time 欄" % path)
    si, pi = rows[0].index("symbol"), rows[0].index("publish_time")
    res = {"updated": 0, "same": 0, "later": [], "not_in_csv": []}
    seen = set()
    for r in rows[1:]:
        sym = r[si]
        if sym not in dates:
            continue
        seen.add(sym)
        new = dates[sym]
        if r[pi] == new:
            res["same"] += 1
            continue
        if new > r[pi]:
            res["later"].append(sym)
        r[pi] = new
        res["updated"] += 1
    res["not_in_csv"] = sorted(set(dates) - seen)
    if res["updated"] and not dry_run:
        tmp = path + ".tmp"
        with io.open(tmp, "wb") as f:
            f.write(_serialize(rows))
        os.replace(tmp, path)
    return res


def run(db_path, target, dry_run=False):
    summary = {"files": 0, "updated": 0, "same": 0, "later": 0, "not_in_csv": 0,
               "missing_files": [], "skipped_months": []}
    for (y, m), dates in sorted(load_verified(db_path).items()):
        if (y, m) > LAST_BACKFILLED:
            summary["skipped_months"].append((y, m))
            continue
        path = os.path.join(target, str(y), "%dM%02d" % (y, m), "market.csv")
        if not os.path.exists(path):
            summary["missing_files"].append((y, m))
            continue
        res = apply_month(path, dates, dry_run=dry_run)
        summary["files"] += 1
        summary["updated"] += res["updated"]
        summary["same"] += res["same"]
        summary["later"] += len(res["later"])
        summary["not_in_csv"] += len(res["not_in_csv"])
        print("%dM%02d  updated=%d same=%d later=%d not_in_csv=%d" % (
            y, m, res["updated"], res["same"], len(res["later"]), len(res["not_in_csv"])))
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    s = run(a.db, a.target, dry_run=a.dry_run)
    print("%s files=%d updated=%d same=%d later=%d not_in_csv=%d missing_files=%s"
          " skipped_months=%s" % (
              "[dry-run]" if a.dry_run else "[done]", s["files"], s["updated"], s["same"],
              s["later"], s["not_in_csv"], s["missing_files"], s["skipped_months"]))


if __name__ == "__main__":
    main()

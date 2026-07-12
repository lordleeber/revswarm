#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 server DB 裡爬到的月營收公布日匯出成研究用 CSV。

預設只匯出 success（附 provenance: source / raw_title）；--all 連同 failed/undone
也一起輸出（announce_date 留空），方便盤點覆蓋率。

用法：
  python3 export.py --db revswarm.db --out revenue_dates.csv
  python3 export.py --db revswarm.db --out all.csv --all
"""

import argparse
import csv
import sqlite3
import sys

FIELDS = ["stock_id", "name", "roc_year", "roc_month",
          "announce_date", "revenue", "yoy", "state", "source", "raw_title"]


def main():
    ap = argparse.ArgumentParser(description="匯出月營收公布日 CSV")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--out", default="revenue_dates.csv")
    ap.add_argument("--all", action="store_true",
                    help="連 failed/undone 也輸出（預設只輸出 success）")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    where = "" if args.all else "WHERE state='success'"
    rows = conn.execute(
        f"""SELECT stock_id, name, roc_year, roc_month, announce_date,
                   revenue, yoy, state, source, raw_title
              FROM tasks {where}
             ORDER BY stock_id, roc_year, roc_month""").fetchall()

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in FIELDS})

    # 覆蓋率摘要
    by_state = {s: c for s, c in conn.execute(
        "SELECT state, COUNT(*) FROM tasks GROUP BY state")}
    total = sum(by_state.values())
    succ = by_state.get("success", 0)
    print(f"匯出 {len(rows)} 列 → {args.out}")
    print(f"覆蓋率：success {succ}/{total} "
          f"({100.0*succ/total:.1f}%)  各 state：{by_state}")


if __name__ == "__main__":
    main()

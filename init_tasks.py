#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
init：把 stocks.csv 的每一檔 × 民國 109/1~115/1（73 個月）展開成 tasks(undone)，寫入 SQLite。

冪等：用 UNIQUE(stock_id,roc_year,roc_month) + INSERT OR IGNORE，重跑不會重複或覆蓋既有進度。
（若 stocks.csv 之後補了缺漏代號，再跑一次即可把新任務補進去。）

用法：
  python3 init_tasks.py --stocks stocks.csv --db revswarm.db
"""

import argparse
import csv
import sqlite3
import sys
import time

import revlib
from server import SCHEMA, connect


def load_stocks(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for d in r:
            sid = (d.get("stock_id") or "").strip()
            name = (d.get("name") or "").strip()
            if sid and name:
                rows.append((sid, name))
    return rows


def main():
    ap = argparse.ArgumentParser(description="展開 tasks 到 SQLite")
    ap.add_argument("--stocks", default="stocks.csv")
    ap.add_argument("--db", default="revswarm.db")
    args = ap.parse_args()

    try:
        stocks = load_stocks(args.stocks)
    except FileNotFoundError:
        print(f"找不到 {args.stocks}，請先跑 build_stocks.py。", file=sys.stderr)
        sys.exit(1)
    if not stocks:
        print(f"{args.stocks} 沒有可用資料。", file=sys.stderr)
        sys.exit(1)

    months = list(revlib.iter_roc_months())
    print(f"股票 {len(stocks)} 檔 × {len(months)} 月"
          f"（民國{revlib.ROC_START[0]}/{revlib.ROC_START[1]}"
          f"~{revlib.ROC_END[0]}/{revlib.ROC_END[1]}）"
          f" = 預期 {len(stocks) * len(months)} 筆任務")

    conn = connect(args.db)
    now = int(time.time())
    before = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]

    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.executemany(
            """INSERT OR IGNORE INTO tasks
                 (stock_id, name, roc_year, roc_month, state, updated_at)
               VALUES (?, ?, ?, ?, 'undone', ?)""",
            [(sid, name, ry, rm, now)
             for (sid, name) in stocks for (ry, rm) in months],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after = conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
    print(f"新增 {after - before} 筆（既有 {before} → 現有 {after}）。db={args.db}")


if __name__ == "__main__":
    main()

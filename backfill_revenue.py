#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性回填：從既有 tasks.raw_title 解析「月營收/年增率」寫入 revenue/yoy 欄位。

不重爬（raw_title 早已存在 DB）。可重複執行（idempotent）。
營收由非官方 snippet（多為 MoneyDJ）解析、四捨五入，僅供交叉校驗，非權威值。

用法：
  python3 backfill_revenue.py --dry-run     # 只看能解析幾筆，不寫入
  python3 backfill_revenue.py               # 實際回填（請先停 server、先備份）
"""

import argparse
import sqlite3

import revlib


def ensure_cols(conn):
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, decl in (("revenue", "INTEGER"), ("yoy", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")


def main():
    ap = argparse.ArgumentParser(description="從 raw_title 回填 revenue/yoy")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--dry-run", action="store_true", help="只統計、不寫入")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    if not args.dry_run:
        ensure_cols(conn)

    rows = conn.execute(
        "SELECT id, raw_title FROM tasks"
        " WHERE state='success' AND raw_title IS NOT NULL").fetchall()
    updates = []
    for r in rows:
        res = revlib.parse_revenue(r["raw_title"])
        if res:
            updates.append((res[0], res[1], r["id"]))

    pct = 100 * len(updates) // max(1, len(rows))
    print(f"success 有 raw_title：{len(rows)}；可解析營收：{len(updates)}（{pct}%）")

    if args.dry_run:
        print("dry-run：不寫入。前幾筆預覽：")
        for amt, yoy, i in updates[:5]:
            print(f"  id={i}  revenue={amt:,}  yoy={yoy}")
        conn.close()
        return

    conn.executemany(
        "UPDATE tasks SET revenue=?, yoy=? WHERE id=?", updates)
    conn.commit()
    n_rev = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE revenue IS NOT NULL").fetchone()[0]
    print(f"已回填 {len(updates)} 筆；目前有 revenue 的列共 {n_rev}。")
    conn.close()


if __name__ == "__main__":
    main()

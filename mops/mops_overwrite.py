#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
覆蓋既有 success 中「Yahoo 公布日晚於 MOPS 官方申報日」(diff>0) 的那批，改用 MOPS 官方日。

- 母體：mops_baseline.csv（官方申報日）∩ 任務範圍 ∩ 過窗 ∩ DB 現為 success 且日期不符。
- 只動 diff>0（Yahoo 晚於 MOPS，多為 Yahoo 匹配到較晚的回顧型文章）。
- diff<0（Yahoo 反而早於官方，可疑）一律**保留待查**，只列出、不覆蓋。
- 覆蓋後標 source='mops'，並清掉 Yahoo 衍生的 raw_title/revenue/yoy（避免與新日期矛盾；
  與 mops_fill.py 對 mops 列的處理一致）。
- 樂觀鎖：WHERE id=? AND state='success' AND announce_date=<原Yahoo日>；只在該列未變動時覆蓋，
  故冪等、且對線上 worker 安全（worker 的 report() 本就不動 success 列）。

用法：
  python3 -m mops.mops_overwrite --dry-run   # 只統計、不寫入
  python3 -m mops.mops_overwrite             # 實際覆蓋（會先自動用 SQLite backup API 備份 DB）
"""

import argparse
import csv
import sqlite3
import time
from datetime import date

import revlib


def load_baseline(path):
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[(r["stock_id"], int(r["roc_year"]), int(r["roc_month"]))] = \
                r["announce_date"]
    return out


def daydiff(a, b):
    """a - b（天），輸入 'YYYY-MM-DD'。"""
    ya, ma, da = (int(x) for x in a.split("-"))
    yb, mb, db = (int(x) for x in b.split("-"))
    return (date(ya, ma, da) - date(yb, mb, db)).days


def main():
    ap = argparse.ArgumentParser(description="用 MOPS 官方日覆蓋 Yahoo 晚報的 success")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--baseline", default="mops_baseline.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = load_baseline(args.baseline)
    now = int(time.time())
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    overwrite = []      # (mops_date, now, id, orig_yahoo_date)
    keep_neg = []       # (sid, ry, rm, yahoo, mops, diff)
    for (sid, ry, rm), md in base.items():
        if (ry, rm) < revlib.ROC_START or (ry, rm) > revlib.ROC_END:
            continue
        try:
            y, mo, d = (int(x) for x in md.split("-"))
        except ValueError:            # 畸形 MOPS 日期：skip，不中斷整個 run（對齊 mops_fill）
            continue
        if not revlib.in_window(y, mo, d, ry, rm):
            continue
        row = conn.execute(
            "SELECT id, state, announce_date FROM tasks"
            " WHERE stock_id=? AND roc_year=? AND roc_month=?",
            (sid, ry, rm)).fetchone()
        if not row or row["state"] != "success" or not row["announce_date"] \
                or row["announce_date"] == md:
            continue
        diff = daydiff(row["announce_date"], md)      # Yahoo - MOPS
        if diff > 0:
            overwrite.append((md, now, row["id"], row["announce_date"]))
        elif diff < 0:
            keep_neg.append((sid, ry, rm, row["announce_date"], md, diff))

    print(f"要覆蓋（Yahoo 晚於 MOPS, diff>0）：{len(overwrite)} 筆")
    print(f"保留待查（Yahoo 早於 MOPS, diff<0）：{len(keep_neg)} 筆")
    for sid, ry, rm, yd, md, diff in sorted(keep_neg):
        print(f"   保留 {sid} {ry}/{rm:02d}  Yahoo={yd}  MOPS={md}  ({diff:+d}天)")

    if args.dry_run:
        print("\n[dry-run] 不寫入。")
        conn.close()
        return

    bak = f"{args.db}.bak-mopsovr-{now}"
    dst = sqlite3.connect(bak)
    with dst:
        conn.backup(dst)
    dst.close()
    print(f"\n已備份 DB → {bak}")

    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.executemany(
            # ⚠️ url/verified 一起清：url 指的是「舊日期」出自哪一篇，verified 是對
            # 「舊日期」蓋的章。留著就變成一個看起來有出處、有人驗過的新日期。
            # （這裡也不該順手蓋 verified='mops'——source 已經記了日期來自 MOPS，
            #   同一個來源不能拿來驗證自己。）
            "UPDATE tasks SET announce_date=?, source='mops',"
            " raw_title=NULL, revenue=NULL, yoy=NULL,"
            " url=NULL, verified=NULL, updated_at=?"
            " WHERE id=? AND state='success' AND announce_date=?",
            overwrite)
        n = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已覆蓋 {n} 筆（announce_date←MOPS, source='mops'）。")
    conn.close()


if __name__ == "__main__":
    main()

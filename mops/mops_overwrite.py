#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
覆蓋既有 success 中「公布日與 MOPS 官方申報日不符」的那批，一律改用 MOPS 官方日。

- 母體：mops_baseline.csv（官方申報日）∩ 任務範圍 ∩ 過窗 ∩ DB 現為 success 且日期不符。
- **兩個方向都覆蓋**。MOPS t05st01 是公開資訊觀測站的官方申報紀錄，就是「這筆營收哪天
  公布」的權威定義；兩邊不一致時沒有第二種解讀，以 MOPS 為準。
- ⚠️ 早期版本只動 diff>0（Yahoo 晚於 MOPS），diff<0 保留待查，理由是「新聞早於官方申報
  日很可疑，覆蓋掉就看不見那個訊號」。改掉的依據：5465 富驊 112/12 原本 diff=-1，
  2026-08-25 用原本那兩種查詢重抓，Yahoo 自己也給出 MOPS 那個日期——偏早只是配錯文章，
  不是新聞真的搶先（見 data/date_overrides.csv 那筆的 note）。
  訊號沒有丟掉：diff<0 仍單獨列出來印，只是不再擋著不寫。
- 覆蓋後標 source='mops'，並清掉 Yahoo 衍生的 raw_title/revenue/yoy 與 url/verified
  （避免與新日期矛盾；與 mops_fill.py 對 mops 列的處理一致）。
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


def select_overwrites(conn, base, now):
    """
    挑出「DB 現為 success 但日期與 MOPS 不符」的列。

    回傳 (overwrite, negatives)：
      overwrite = [(mops_date, now, id, 原日期), ...]  ← 直接餵 executemany，最後一個
                  欄位是樂觀鎖用的原值（見 main 的 UPDATE ... AND announce_date=?）
      negatives = [(sid, ry, rm, 原日期, mops_date, diff), ...]
                  overwrite 的子集合，只挑 diff<0（DB 早於官方）那些，單獨印出來當訊號。
                  ⚠️ 它不再是「保留不寫」的名單——兩個方向都會覆蓋，見模組 docstring。

    跳過的四種：不在任務範圍、MOPS 日期畸形、MOPS 日期落在窗外（拿它覆蓋會把窗外值寫進
    DB，破壞「所有 announce_date 都過窗」這個專案級保證）、DB 不是 success（那是
    mops_fill 的守備範圍，這支只改已經定案但值不對的列）。
    """
    overwrite, negatives = [], []
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
        overwrite.append((md, now, row["id"], row["announce_date"]))
        diff = daydiff(row["announce_date"], md)      # DB - MOPS
        if diff < 0:
            negatives.append((sid, ry, rm, row["announce_date"], md, diff))
    return overwrite, negatives


def main():
    ap = argparse.ArgumentParser(description="用 MOPS 官方日覆蓋不符的 success")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--baseline", default="mops_baseline.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    base = load_baseline(args.baseline)
    now = int(time.time())
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    overwrite, negatives = select_overwrites(conn, base, now)

    print(f"要覆蓋（與 MOPS 官方申報日不符）：{len(overwrite)} 筆")
    # ⚠️ diff<0 仍單獨印出來：DB 的日期早於官方申報日是異常，即使照樣覆蓋，也該讓人
    # 看見它出現在哪幾筆、是不是集中在某種 raw_title 格式或某段時間。
    print(f"   其中 DB 早於官方（diff<0，異常，仍會覆蓋）：{len(negatives)} 筆")
    for sid, ry, rm, yd, md, diff in sorted(negatives):
        print(f"   ⚠️ {sid} {ry}/{rm:02d}  DB={yd}  MOPS={md}  ({diff:+d}天)")

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

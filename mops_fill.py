#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一次性回填：把 MOPS 官方申報日填進「非 success」的任務（多為 requeue 後的 ex-failed）。

- 母體：mops_baseline.csv（約 53 檔自願把月營收發成重大訊息者，官方申報日、最高信度）。
- 只填「該 (股票,月) 在 revswarm 任務範圍內、且官方日通過 revlib 期望窗(次月1-15)」者。
- 標記 source='mops' 以示來源（非 Yahoo 爬取）。
- 不覆蓋既有 success：WHERE state!='success'。與 Yahoo 抓到的日期不符者只報告、不動。
- 冪等 + 對線上 worker 安全：worker 的 report() 對已 success 列一律忽略(server.py)，
  故填成 success 後 worker 遲到回報不會被降級；雙方皆 BEGIN IMMEDIATE，SQLite 序列化。

用法：
  python3 mops_fill.py --dry-run   # 只統計、不寫入
  python3 mops_fill.py             # 實際回填（會先自動用 SQLite backup API 備份 DB）
"""

import argparse
import csv
import sqlite3
import time

import revlib

RANGE_LO, RANGE_HI = revlib.ROC_START, revlib.ROC_END   # (109,1)..(115,1)


def load_baseline(path):
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[(r["stock_id"], int(r["roc_year"]), int(r["roc_month"]))] = \
                r["announce_date"]
    return out


def build_updates(conn, baseline, now):
    """回傳 (updates, report)；updates = [(announce_date, now, id), ...]。"""
    updates = []
    rep = {"already_ok": 0, "mismatch": [], "out_of_window": [],
           "no_task": 0}
    for (sid, ry, rm), d in baseline.items():
        if (ry, rm) < RANGE_LO or (ry, rm) > RANGE_HI:
            continue
        try:
            y, mo, dd = (int(x) for x in d.split("-"))
        except ValueError:
            continue
        if not revlib.in_window(y, mo, dd, ry, rm):
            rep["out_of_window"].append((sid, ry, rm, d))
            continue
        row = conn.execute(
            "SELECT id, state, announce_date FROM tasks"
            " WHERE stock_id=? AND roc_year=? AND roc_month=?",
            (sid, ry, rm)).fetchone()
        if row is None:
            rep["no_task"] += 1
            continue
        if row["state"] == "success":
            if row["announce_date"] != d:
                rep["mismatch"].append((sid, ry, rm, row["announce_date"], d))
            else:
                rep["already_ok"] += 1
            continue
        updates.append((d, now, row["id"]))
    return updates, rep


def main():
    ap = argparse.ArgumentParser(description="用 MOPS 官方申報日回填 failed/undone 任務")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--baseline", default="mops_baseline.csv")
    ap.add_argument("--dry-run", action="store_true", help="只統計、不寫入")
    args = ap.parse_args()

    baseline = load_baseline(args.baseline)
    now = int(time.time())

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    updates, rep = build_updates(conn, baseline, now)

    print(f"MOPS baseline：{len(baseline)} 筆，涵蓋 "
          f"{len({k[0] for k in baseline})} 檔")
    print(f"★ 可回填（非 success + 過窗 + 有對應任務）：{len(updates)} 筆")
    print(f"  已是 success 且日期相符（不動）：{rep['already_ok']}")
    print(f"  已是 success 但與 MOPS 不符（不動，另列）：{len(rep['mismatch'])}")
    print(f"  官方日不過窗（跳過）：{len(rep['out_of_window'])} "
          f"{[f'{s} {y}/{m}={d}' for s, y, m, d in rep['out_of_window']]}")
    print(f"  範圍內但 DB 無對應任務（跳過）：{rep['no_task']}")

    if args.dry_run:
        print("\n[dry-run] 不寫入。前 8 筆預覽：")
        for d, _, i in updates[:8]:
            print(f"  id={i} <- announce_date={d}  source=mops")
        conn.close()
        return

    bak = f"{args.db}.bak-mopsfill-{now}"
    dst = sqlite3.connect(bak)
    with dst:
        conn.backup(dst)           # 線上一致快照，含 WAL；worker 同時寫也安全
    dst.close()
    print(f"\n已備份 DB → {bak}")

    conn.execute("BEGIN IMMEDIATE;")
    try:
        cur = conn.executemany(
            "UPDATE tasks SET state='success', announce_date=?, source='mops',"
            " raw_title=NULL, revenue=NULL, yoy=NULL,"
            " dispatched_at=NULL, worker_id=NULL, updated_at=?"
            " WHERE id=? AND state!='success'",
            updates)
        n = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已回填 {n} 筆（source='mops'）。")
    by = {r["state"]: r["c"] for r in conn.execute(
        "SELECT state, COUNT(*) c FROM tasks GROUP BY state")}
    print("目前各狀態：", by)
    conn.close()


if __name__ == "__main__":
    main()

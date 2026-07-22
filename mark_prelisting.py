#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「公司首次公開之前（不可能有月營收申報）」的任務移出待爬池，標成 state='prelisting'。

切點（精準）：每檔的 goodinfo first_public_gi＝min(上市/上櫃/興櫃/公開發行日)，
是公司「開始成為公開發行公司、負有月營收申報義務」的最早日（含已畢業到上市者的早年興櫃/
公開發行日，官方現況快照拿不到、goodinfo 有）。任務月早於此日者→不可能有資料→安全移出。

- 只動 undone/failed/dispatched；success 一律不碰。可逆（prelisting→undone 即復原）。
- 冪等 + 對線上 worker 安全（BEGIN IMMEDIATE；標成 prelisting 後不再被 lease）。
- first_public_gi 早於/等於任務窗頭(民國109/1)者→全窗已公開→不砍（其空白是 recall 漏抓）。

前置：先跑 build_stock_dates.py + goodinfo_worker.py 讓 stock_dates.db 的 goodinfo_dates 有資料。

用法：
  python3 mark_prelisting.py --dry-run   # 列出每檔切點與砍除數 + 輸出 CSV，不寫入
  python3 mark_prelisting.py             # 實際標記（先自動 SQLite backup revswarm.db）
"""

import argparse
import csv
import sqlite3
import time

import revlib

WINDOW_START = revlib.ROC_START[0] * 100 + revlib.ROC_START[1]   # 10901（民國109/1）
ACTIVE = ("undone", "failed", "dispatched")


def roc_key(ad_date):
    """'YYYY-MM-DD'(西元) -> 民國 roc_year*100+month；不符回 None。"""
    try:
        y, m, _ = ad_date.split("-")
        return (int(y) - 1911) * 100 + int(m)
    except (AttributeError, ValueError):
        return None


def load_cutoffs(dates_db):
    """回傳 {stock_id: (first_public_key, first_public_gi)}，僅取 first_public 晚於窗頭者。"""
    conn = sqlite3.connect(f"file:{dates_db}?mode=ro", uri=True)
    out = {}
    for sid, fp in conn.execute(
            "SELECT stock_id, first_public_gi FROM goodinfo_dates WHERE status='ok'"):
        k = roc_key(fp)
        if k and k > WINDOW_START:            # 窗頭之後才公開 → 有上市前月份可砍
            out[sid] = (k, fp)
    conn.close()
    return out


def build_plan(conn, cutoffs):
    """回傳 [(stock_id, cutoff_key, first_public_gi, prune_count, state_breakdown)]。"""
    plan = []
    for sid, (ckey, fp) in cutoffs.items():
        rows = conn.execute(
            "SELECT state FROM tasks WHERE stock_id=? AND (roc_year*100+roc_month)<?",
            (sid, ckey)).fetchall()
        pre = [r[0] for r in rows if r[0] in ACTIVE]
        succ = sum(1 for r in rows if r[0] == "success")
        if pre:
            from collections import Counter
            plan.append((sid, ckey, fp, len(pre), dict(Counter(pre)), succ))
    return plan


def main():
    ap = argparse.ArgumentParser(description="用 goodinfo 首次公開日標記上市前任務")
    ap.add_argument("--db", default="revswarm.db", help="任務 DB")
    ap.add_argument("--dates-db", default="stock_dates.db", help="goodinfo 日期 DB")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="tmp/prelisting_candidates.csv")
    args = ap.parse_args()

    cutoffs = load_cutoffs(args.dates_db)
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    # 取每檔名稱供輸出
    names = {r["stock_id"]: r["name"] for r in conn.execute(
        "SELECT DISTINCT stock_id, name FROM tasks")}
    plan = build_plan(conn, cutoffs)
    plan.sort(key=lambda x: -x[3])
    total = sum(p[3] for p in plan)
    succ_before = sum(p[5] for p in plan)

    print(f"goodinfo 首次公開晚於窗頭的公司: {len(cutoffs)} 檔")
    print(f"★ 其中有『上市前 undone/failed/dispatched 任務』可標 prelisting: "
          f"{len(plan)} 檔, {total} 筆")
    if succ_before:
        print(f"  ⚠️ 另有 {succ_before} 筆 success 落在首次公開之前(疑 Yahoo 錯配)——不動、僅提示")

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["stock_id", "name", "first_public_gi", "prune_count",
                    "state_breakdown", "success_before_cutoff"])
        for sid, ck, fp, n, br, sc in plan:
            w.writerow([sid, names.get(sid, ""), fp, n, br, sc])
    print(f"  完整清單 → {args.out}")

    print(f"\n{'代號':<6}{'名稱':<9}{'首次公開':<12}{'砍除':>5}  各state")
    for sid, ck, fp, n, br, sc in plan:
        print(f"{sid:<6}{names.get(sid,''):<9}{fp:<12}{n:>5}  {br}"
              + (f"  ⚠️success前{sc}" if sc else ""))

    if args.dry_run:
        print("\n[dry-run] 未寫入。")
        conn.close()
        return

    now = int(time.time())
    bak = f"{args.db}.bak-prelisting-{now}"
    dst = sqlite3.connect(bak)
    with dst:
        conn.backup(dst)
    dst.close()
    print(f"\n已備份 DB → {bak}")

    conn.execute("BEGIN IMMEDIATE;")
    try:
        n = 0
        for sid, ckey, fp, cnt, br, sc in plan:
            cur = conn.execute(
                "UPDATE tasks SET state='prelisting', dispatched_at=NULL,"
                " worker_id=NULL, updated_at=?"
                " WHERE stock_id=? AND (roc_year*100+roc_month)<?"
                " AND state IN ('undone','failed','dispatched')",
                (now, sid, ckey))
            n += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已標記 {n} 筆為 prelisting。")
    by = {r["state"]: r["c"] for r in conn.execute(
        "SELECT state, COUNT(*) c FROM tasks GROUP BY state")}
    print("目前各狀態：", by)
    conn.close()


if __name__ == "__main__":
    main()

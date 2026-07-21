#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「上市前（公司尚未公開、不可能有月營收）」的任務移出待爬池，標成 state='prelisting'。

判定（結合兩種訊號，互相佐證/否決）：
  1. 經驗空白：某檔「第一個 success 之前、從任務窗頭(民國109/1)起連續全無 success」的月份，
     是上市前的候選（公開公司每月都報，整段連續空白極可能是還沒公開）。
  2. 掛牌日（tmp/107-114.csv 上市買賣日 + tmp/t187ap03_O.csv 上櫃日，取最早）：
     - 掛牌於窗內 → 佐證「窗內才公開」→ 確認上市前。
     - 掛牌早於窗頭 → 公司全窗都已公開 → 空白必是 recall 漏抓或零營收 → **否決，不標**。
     - 無掛牌日 → 退而求其次：空白 >= 12 月才標（短空白多為 recall 漏抓）。

只動 undone/failed/dispatched；success 一律不碰。可逆（要復原：prelisting→undone）。
冪等 + 對線上 worker 安全（BEGIN IMMEDIATE；標成 prelisting 後不再被 lease）。

用法：
  python3 mark_prelisting.py --dry-run   # 列出候選 + 輸出 CSV，不寫入
  python3 mark_prelisting.py             # 實際標記（先自動 SQLite backup）
"""

import argparse
import csv
import glob
import io
import sqlite3
import time
from collections import defaultdict

import revlib

WINDOW_START = revlib.ROC_START[0] * 100 + revlib.ROC_START[1]   # 10901
NO_LISTING_MIN_BLANK = 12       # 無掛牌日時的空白門檻（月）


def load_listing_dates():
    """回傳 {code: (西元年, 月)}，取上市買賣日與上櫃日的最早者。"""
    out = {}
    def setmin(code, ym):
        if code and (code not in out or ym < out[code]):
            out[code] = ym
    # 上市：tmp/107.csv ~ 114.csv（Big5；跳2行；col1=代號 col9=股票上市買賣日期 民國/）
    for f in glob.glob("tmp/1[0-1][0-9].csv"):
        for r in list(csv.reader(io.StringIO(
                open(f, "rb").read().decode("cp950"))))[2:]:
            if len(r) > 9 and r[1].strip() and "/" in r[9]:
                p = r[9].split("/")
                if p[0].isdigit():
                    setmin(r[1].strip(), (int(p[0]) + 1911, int(p[1])))
    # 上櫃：tmp/t187ap03_O.csv（utf-8-sig；上櫃日期 YYYYMMDD 西元）
    try:
        for r in csv.DictReader(io.StringIO(
                open("tmp/t187ap03_O.csv", "rb").read().decode("utf-8-sig"))):
            d = r["上櫃日期"].strip()
            if r["公司代號"].strip() and len(d) == 8 and d.isdigit():
                setmin(r["公司代號"].strip(), (int(d[:4]), int(d[4:6])))
    except FileNotFoundError:
        pass
    return out


def listing_key(ym):
    """(西元年,月) -> 民國 key（roc_year*100+month）。"""
    return (ym[0] - 1911) * 100 + ym[1]


def build_candidates(conn, listing):
    tasks = defaultdict(list)
    names = {}
    for r in conn.execute(
            "SELECT stock_id, name, roc_year, roc_month, state FROM tasks"):
        tasks[r["stock_id"]].append(
            (r["roc_year"] * 100 + r["roc_month"], r["state"]))
        names.setdefault(r["stock_id"], r["name"])

    cands, vetoed = [], 0
    for sid, rows in tasks.items():
        rows.sort()
        succ = [k for k, s in rows if s == "success"]
        if not succ:
            continue
        first = min(succ)
        if first == WINDOW_START:
            continue
        before = sorted(k for k, s in rows if k < first)
        if not before or before[0] != WINDOW_START:
            continue                         # 空白沒從窗頭起 → 不處理
        blank = len(before)
        ld = listing.get(sid)
        # 🛑 否決：掛牌(任一板)早於/等於窗頭 → 全窗都已公開 → 空白必是 recall 漏抓/零營收
        if ld and listing_key(ld) <= WINDOW_START:
            vetoed += 1
            continue
        # 空白長度是唯一可靠的信心指標：≥12 月連續空白才判上市前。
        # （主板掛牌可能比「首次公開(興櫃)」晚數年，故「窗內掛牌」對短空白不足為證，
        #   如 藥華藥/驊陞 空白1月卻已多年在報營收——那是漏抓，不是上市前。）
        if blank < NO_LISTING_MIN_BLANK:
            continue
        reason = (f"空白{blank}月+掛牌{ld[0]}/{ld[1]:02d}" if ld
                  else f"空白{blank}月(無掛牌日)")
        ww = sum(1 for _ in before)          # before 全是非 success
        cands.append({
            "stock_id": sid, "name": names.get(sid, ""),
            "blank_months": blank, "prune_tasks": ww,
            "first_revenue": f"{first//100}/{first%100:02d}",
            "listing": f"{ld[0]}/{ld[1]:02d}" if ld else "",
            "reason": reason, "cut_before_key": first,
        })
    return cands, vetoed


def main():
    ap = argparse.ArgumentParser(description="標記上市前任務為 prelisting")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="tmp/prelisting_candidates.csv")
    args = ap.parse_args()

    listing = load_listing_dates()
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    cands, vetoed = build_candidates(conn, listing)
    cands.sort(key=lambda x: (-x["blank_months"], x["stock_id"]))
    total_prune = sum(c["prune_tasks"] for c in cands)

    print(f"掛牌日資料: {len(listing)} 檔")
    print(f"★ 安全可標為 prelisting: {len(cands)} 檔, {total_prune} 筆任務")
    corr = sum(1 for c in cands if c["reason"].startswith("掛牌佐證"))
    print(f"   其中 掛牌佐證 {corr} 檔 / 無掛牌日靠空白≥{NO_LISTING_MIN_BLANK}月 {len(cands)-corr} 檔")
    print(f"   🛑 被掛牌日否決(不標，救回誤砍): {vetoed} 檔")

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["stock_id", "name", "blank_months",
                           "prune_tasks", "first_revenue", "listing", "reason"])
        w.writeheader()
        for c in cands:
            w.writerow({k: c[k] for k in w.fieldnames})
    print(f"   完整清單已輸出 → {args.out}")

    print(f"\n{'代號':<6}{'名稱':<9}{'空白':>4} {'首營收':>7} {'掛牌':>9}  依據")
    for c in cands:
        print(f"{c['stock_id']:<6}{c['name']:<9}{c['blank_months']:>4} "
              f"{c['first_revenue']:>7} {c['listing']:>9}  {c['reason']}")

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
        for c in cands:
            cur = conn.execute(
                "UPDATE tasks SET state='prelisting', dispatched_at=NULL,"
                " worker_id=NULL, updated_at=?"
                " WHERE stock_id=? AND (roc_year*100+roc_month) < ?"
                " AND state!='success'",
                (now, c["stock_id"], c["cut_before_key"]))
            n += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已標記 {n} 筆為 prelisting。")
    conn.close()


if __name__ == "__main__":
    main()

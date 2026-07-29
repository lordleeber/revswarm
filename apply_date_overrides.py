#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 data/date_overrides.csv 裡「人工確認過的公布日」寫進 DB。

為什麼需要這個檔（別把它跟放寬窗搞混）：
  revlib 的窗是「營收月的次月 1~15 日」。這個上界不是猜的——mops_baseline.csv 那 2556
  筆 MOPS 官方申報日（唯一沒被窗過濾過的權威資料）最大日就是 15，>15 日 0 筆。也就是
  「截止日 10 號 + 假日順延 + 小幅落後」全都塞得進 15，所以**窗不該全域放寬**。
  但確實有公司某個月遲交到 16、17 號（實測：3494 誠研 109/1 為 2020-02-17）。那不是
  窗設太窄，是個案異常，沒有一個「遲交可以到幾號」的規則能涵蓋——只能逐筆人工確認。
  這個檔就是那個出口：一行一個個案，每行都要有 note 交代證據。

刻意不設遲交上限，但保留強的那一半不變量：
  ✓ announce_date 必須落在「營收月的次月」（月份錯 = 一定是打錯或誤判，直接拒）
  ✗ 不限制是幾號（只檢查是該月合法的日）——上限是個案問題，由 note 與人負責

source 用 *_manual 後綴（例 g_roc_manual）：這些是全庫唯一會落在窗外的 success，
標記要留得住稽核，否則就靜靜違反「所有 announce_date 都過窗」這個專案級保證。
稽核用：
  SELECT * FROM tasks WHERE state='success'
   AND CAST(substr(announce_date,9,2) AS INTEGER) NOT BETWEEN 1 AND 15;

用法：
  python3 apply_date_overrides.py --dry-run   # 只檢查與列出，不寫入
  python3 apply_date_overrides.py             # 實際寫入（會先用 SQLite backup API 備份 DB）

冪等：已與 CSV 一致的列會被略過。DB 是執行期產物（不進版控），所以重建 DB 後要再跑一次
這支才會把人工判斷補回去——這正是它存在的理由。
"""

import argparse
import calendar
import csv
import sqlite3
import time

import revlib

FIELDS = ("stock_id", "roc_year", "roc_month", "announce_date",
          "source", "raw_title", "note")


def load_overrides(path):
    """讀 CSV → list[dict]（不做驗證，驗證在 check_row）。"""
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def check_row(row):
    """
    驗一列 override，回傳錯誤字串；通過回 None。

    ⚠️ 只驗「月份對不對」與「日是不是該月的合法日」，不驗「幾號以前才算合理」——
    後者是個案問題（見模組 docstring），由 note 與人負責。
    """
    missing = [k for k in ("stock_id", "roc_year", "roc_month", "announce_date")
               if not (row.get(k) or "").strip()]
    if missing:
        return f"缺欄位 {','.join(missing)}"
    if not (row.get("note") or "").strip():
        return "note 不可空白（這個檔的每一行都要交代證據）"
    try:
        ry, rm = int(row["roc_year"]), int(row["roc_month"])
    except ValueError:
        return "roc_year/roc_month 不是整數"
    if not 1 <= rm <= 12:
        return f"roc_month={rm} 不在 1~12"
    if (ry, rm) < revlib.ROC_START or (ry, rm) > revlib.ROC_END:
        return f"{ry}/{rm} 不在任務範圍 {revlib.ROC_START}~{revlib.ROC_END}"
    try:
        y, mo, d = (int(x) for x in row["announce_date"].split("-"))
    except ValueError:
        return f"announce_date={row['announce_date']!r} 不是 YYYY-MM-DD"
    wy, wm = revlib.expected_window(ry, rm)
    if (y, mo) != (wy, wm):
        # 月份是強的那一半不變量：營收月的次月，錯了就一定是打錯或誤判。
        return f"announce_date 月份應為 {wy}-{wm:02d}（營收月的次月），實得 {y}-{mo:02d}"
    if not 1 <= d <= calendar.monthrange(y, mo)[1]:
        return f"{y}-{mo:02d} 沒有 {d} 日"
    return None


def classify(row):
    """回 'in_window' / 'late'：只影響列印，不影響是否寫入。"""
    y, mo, d = (int(x) for x in row["announce_date"].split("-"))
    return "in_window" if revlib.in_window(
        y, mo, d, int(row["roc_year"]), int(row["roc_month"])) else "late"


def build_updates(rows, lookup):
    """
    純函式：(已驗證的 rows, lookup) → (updates, skipped, orphans)

    lookup(stock_id, roc_year, roc_month) → dict(id,state,announce_date,source) 或 None，
    讓測試不必碰真的 DB。
      updates : (announce_date, source, raw_title, revenue, yoy, updated_at?, id) 前的素材
      skipped : 已與 CSV 一致，不必動
      orphans : DB 裡找不到這筆任務
    """
    updates, skipped, orphans = [], [], []
    for row in rows:
        cur = lookup(row["stock_id"], int(row["roc_year"]), int(row["roc_month"]))
        if cur is None:
            orphans.append(row)
            continue
        source = (row.get("source") or "manual").strip()
        if (cur["state"] == "success" and cur["announce_date"] == row["announce_date"]
                and cur["source"] == source):
            skipped.append(row)
            continue
        title = (row.get("raw_title") or "").strip() or None
        rev = revlib.parse_revenue(title) if title else None
        revenue, yoy = rev if rev else (None, None)
        updates.append({"id": cur["id"], "row": row, "source": source,
                        "raw_title": title, "revenue": revenue, "yoy": yoy,
                        "was": (cur["state"], cur["announce_date"], cur["source"])})
    return updates, skipped, orphans


def main():
    ap = argparse.ArgumentParser(description="套用人工確認的公布日 override")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--csv", default="data/date_overrides.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = load_overrides(args.csv)
    bad = [(i, r, err) for i, r in enumerate(rows, 2)
           if (err := check_row(r)) is not None]
    for line, r, err in bad:
        print(f"  ✗ 第 {line} 行 {r.get('stock_id','?')} "
              f"{r.get('roc_year','?')}/{r.get('roc_month','?')}：{err}")
    if bad:
        raise SystemExit(f"{len(bad)} 行不合格，未寫入任何東西（先修 CSV）。")

    good = rows
    n_late = sum(1 for r in good if classify(r) == "late")
    print(f"{args.csv}：{len(good)} 行全部通過（其中 {n_late} 行是窗外遲交）")

    now = int(time.time())
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    def lookup(sid, ry, rm):
        return conn.execute(
            "SELECT id, state, announce_date, source FROM tasks"
            " WHERE stock_id=? AND roc_year=? AND roc_month=?", (sid, ry, rm)).fetchone()

    updates, skipped, orphans = build_updates(good, lookup)
    for r in orphans:
        print(f"  ⚠️ DB 找不到 {r['stock_id']} {r['roc_year']}/{r['roc_month']}，略過")
    for r in skipped:
        print(f"  = {r['stock_id']} {r['roc_year']}/{r['roc_month']} "
              f"已是 {r['announce_date']}，略過")
    for u in updates:
        r = u["row"]
        print(f"  → {r['stock_id']} {r['roc_year']}/{r['roc_month']} "
              f"{u['was'][0]}/{u['was'][1]} ⇒ success/{r['announce_date']} "
              f"[{u['source']}] ({classify(r)})")

    if not updates:
        print("沒有需要寫入的變更。")
        conn.close()
        return
    if args.dry_run:
        print("\n[dry-run] 不寫入。")
        conn.close()
        return

    bak = f"{args.db}.bak-dateovr-{now}"
    dst = sqlite3.connect(bak)
    with dst:
        conn.backup(dst)
    dst.close()
    print(f"\n已備份 DB → {bak}")

    conn.execute("BEGIN IMMEDIATE;")
    try:
        for u in updates:
            conn.execute(
                "UPDATE tasks SET state='success', announce_date=?, source=?,"
                " raw_title=?, revenue=?, yoy=?, worker_id='manual',"
                " dispatched_at=NULL, updated_at=? WHERE id=?",
                (u["row"]["announce_date"], u["source"], u["raw_title"],
                 u["revenue"], u["yoy"], now, u["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已寫入 {len(updates)} 筆。")
    conn.close()


if __name__ == "__main__":
    main()

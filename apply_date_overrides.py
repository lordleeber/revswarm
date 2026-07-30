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
  ✓ announce_date 必須是補零的 YYYY-MM-DD，且寫入前再正規化一次（canonical_date）
  ✓ source 必須以 manual 結尾（稽核標記，見下）
  ✓ 表頭與每列欄位數必須完全等於 FIELDS；同一個 (stock_id,roc_year,roc_month) 不可重複
  ✗ 不限制是幾號（只檢查是該月合法的日）——上限是個案問題，由 note 與人負責

source 用 *_manual 後綴（例 g_roc_manual）：這些是全庫唯一會落在窗外的 success，
標記要留得住稽核，否則就靜靜違反「所有 announce_date 都過窗」這個專案級保證。
稽核用：
  SELECT * FROM tasks WHERE state='success'
   AND CAST(substr(announce_date,9,2) AS INTEGER) NOT BETWEEN 1 AND 15;

⚠️ 上面那條 SQL 是靠 substr 取第 9~10 個字元當「日」，所以日期格式歪掉一格就會漏抓
（'2020-2-17' 的 substr(...,9,2) 是 '7' → 落在 1~15 → 這筆窗外 success 從稽核裡消失）。
其他 success 的日期都經 revlib.validate_date 正規化成 'YYYY-MM-DD'；這支為了收窗外個案
必須繞過窗檢查，但不能連正規化一起繞過——所以驗證擋不補零的寫法，寫入再走 canonical_date
保證進 DB 的一定是補零格式（mops_validate 也是純字串比對日期）。

用法：
  python3 apply_date_overrides.py --dry-run   # 只檢查與列出，不寫入
  python3 apply_date_overrides.py             # 實際寫入（會先用 SQLite backup API 備份 DB）

冪等：已與 CSV 一致的列會被略過。DB 是執行期產物（不進版控），所以重建 DB 後要再跑一次
這支才會把人工判斷補回去——這正是它存在的理由。
"""

import argparse
import calendar
import csv
import re
import sqlite3
import time

import revlib

FIELDS = ("stock_id", "roc_year", "roc_month", "announce_date",
          "source", "raw_title", "note")

# DictReader 的 restkey：欄位數多於表頭時多出來的值會落在這個 key。
# 不設的話多出來的欄位會被靜默丟掉，而 note 剛好是最後一欄——note 裡打了半形逗號
# 就只會留下逗號前那半段證據，還驗得過。所以接起來、由 check_row 明白拒掉。
_EXTRA = "__extra__"

# 只收補零的 YYYY-MM-DD（'2020-2-17' 要拒，見模組 docstring 的 substr 陷阱）。
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def load_overrides(path):
    """
    讀 CSV → (fieldnames, [(csv 行號, dict), ...])。完全不驗，驗證在 validate_all。

    行號取 reader.line_num 而不是 enumerate：欄位若被引號包住並含換行，enumerate 的
    序號會跟檔案實際行號漂掉，錯誤訊息就指不到人。
    """
    with open(path, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f, restkey=_EXTRA)
        return tuple(rd.fieldnames or ()), [(rd.line_num, row) for row in rd]


def check_header(fieldnames):
    """表頭必須完全等於 FIELDS（多、少、錯字都拒）；通過回 None。"""
    if tuple(fieldnames) != FIELDS:
        return (f"表頭應為 {','.join(FIELDS)}，"
                f"實得 {','.join(fieldnames) if fieldnames else '(空)'}")
    return None


def canonical_date(row):
    """回寫進 DB 的 'YYYY-MM-DD'（已過 check_row 才呼叫）。

    check_row 已擋掉不補零的寫法，這裡再重組一次，讓「進 DB 的日期一定是補零格式」
    由建構保證，而不是靠 CSV 剛好打對（稽核 SQL 的 substr 吃這個，見模組 docstring）。
    """
    y, mo, d = (int(x) for x in _ISO_DATE.fullmatch(
        row["announce_date"].strip()).groups())
    return f"{y:04d}-{mo:02d}-{d:02d}"


def check_row(row):
    """
    驗一列 override，回傳錯誤字串；通過回 None。

    ⚠️ 只驗「月份對不對」與「日是不是該月的合法日」，不驗「幾號以前才算合理」——
    後者是個案問題（見模組 docstring），由 note 與人負責。
    """
    if row.get(_EXTRA):
        return (f"欄位數多於表頭（多出 {len(row[_EXTRA])} 個值）"
                f"——note 裡若有半形逗號請改全形或用雙引號包起來")
    missing = [k for k in ("stock_id", "roc_year", "roc_month", "announce_date")
               if not (row.get(k) or "").strip()]
    if missing:
        return f"缺欄位 {','.join(missing)}"
    if not (row.get("note") or "").strip():
        return "note 不可空白（這個檔的每一行都要交代證據）"
    source = (row.get("source") or "manual").strip()
    if not source.endswith("manual"):
        # 窗外 success 只有這個檔會產生，標記掉了就再也分不出人工與爬蟲結果。
        return f"source={source!r} 必須以 manual 結尾（例 g_roc_manual；稽核標記）"
    try:
        ry, rm = int(row["roc_year"]), int(row["roc_month"])
    except ValueError:
        return "roc_year/roc_month 不是整數"
    if not 1 <= rm <= 12:
        return f"roc_month={rm} 不在 1~12"
    if (ry, rm) < revlib.ROC_START or (ry, rm) > revlib.ROC_END:
        return f"{ry}/{rm} 不在任務範圍 {revlib.ROC_START}~{revlib.ROC_END}"
    m = _ISO_DATE.fullmatch(row["announce_date"].strip())
    if not m:
        return (f"announce_date={row['announce_date']!r} 不是 YYYY-MM-DD"
                "（月、日都要補零）")
    y, mo, d = (int(x) for x in m.groups())
    wy, wm = revlib.expected_window(ry, rm)
    if (y, mo) != (wy, wm):
        # 月份是強的那一半不變量：營收月的次月，錯了就一定是打錯或誤判。
        return f"announce_date 月份應為 {wy}-{wm:02d}（營收月的次月），實得 {y}-{mo:02d}"
    if not 1 <= d <= calendar.monthrange(y, mo)[1]:
        return f"{y}-{mo:02d} 沒有 {d} 日"
    return None


def _row_key(row):
    """(stock_id, roc_year, roc_month)；欄位壞到算不出來回 None。"""
    try:
        return ((row.get("stock_id") or "").strip(),
                int(row["roc_year"]), int(row["roc_month"]))
    except (KeyError, TypeError, ValueError):
        return None


def validate_all(fieldnames, rows):
    """
    驗整份 CSV → [(行號, row, 錯誤字串)]（空 list = 全部合格，可以寫）。

    除了逐列 check_row，這裡還擋跨列才看得出來的重複 key：同一個
    (stock_id, roc_year, roc_month) 出現兩次時，build_updates 兩次都看到寫入前的舊狀態，
    於是產出兩筆 update、兩條 UPDATE 都跑、後者無聲蓋掉前者。人工維護的檔案最典型的
    失誤（複製一行改一半），直接拒比讓它安靜生效好。
    """
    err = check_header(fieldnames)
    if err:
        # 表頭錯了，逐列的欄位對應本身就不可信，不必再往下驗。
        return [(1, {}, err)]
    problems, seen = [], {}
    for line, row in rows:
        err = check_row(row)
        if err:
            problems.append((line, row, err))
        # 重複檢查不看 check_row 過不過：只要 key 算得出來就登記，一輪把問題報完
        # （否則使用者得先修好前一行、再跑一次才看得到重複）。
        key = _row_key(row)
        if key is None:
            continue
        if key in seen:
            problems.append((line, row,
                             f"與第 {seen[key]} 行重複（{key[0]} {key[1]}/{key[2]}）"))
        else:
            seen[key] = line
    return problems


def classify(row):
    """回 'in_window' / 'late'：只影響列印，不影響是否寫入。"""
    y, mo, d = (int(x) for x in canonical_date(row).split("-"))
    return "in_window" if revlib.in_window(
        y, mo, d, int(row["roc_year"]), int(row["roc_month"])) else "late"


def build_updates(rows, lookup):
    """
    純函式：(已驗證的 rows, lookup) → (updates, skipped, orphans)

    lookup(stock_id, roc_year, roc_month) → dict(id,state,announce_date,source,
    raw_title,revenue,yoy) 或 None，讓測試不必碰真的 DB。
      updates : 寫入所需素材（announce_date/source/raw_title/revenue/yoy/id）
      skipped : 已與 CSV 一致，不必動
      orphans : DB 裡找不到這筆任務

    ⚠️ 冪等的「一致」要比到 CSV 實際承載的每一個欄位，包含 raw_title 與由它導出的
    revenue/yoy。只比日期與 source 的話，把 CSV 裡打錯的 raw_title 改對之後會被判
    「已一致，略過」，revenue/yoy 永遠停在舊值——腳本說成功、資料卻沒更正。
    """
    updates, skipped, orphans = [], [], []
    for row in rows:
        cur = lookup(row["stock_id"].strip(),
                     int(row["roc_year"]), int(row["roc_month"]))
        if cur is None:
            orphans.append(row)
            continue
        source = (row.get("source") or "manual").strip()
        date = canonical_date(row)
        title = (row.get("raw_title") or "").strip() or None
        rev = revlib.parse_revenue(title) if title else None
        revenue, yoy = rev if rev else (None, None)
        if (cur["state"] == "success" and cur["announce_date"] == date
                and cur["source"] == source and cur["raw_title"] == title
                and cur["revenue"] == revenue and cur["yoy"] == yoy):
            skipped.append(row)
            continue
        updates.append({"id": cur["id"], "row": row, "source": source,
                        "announce_date": date, "raw_title": title,
                        "revenue": revenue, "yoy": yoy,
                        "was": (cur["state"], cur["announce_date"], cur["source"])})
    return updates, skipped, orphans


def apply_updates(conn, updates, now):
    """把 build_updates 的結果寫進 DB（單一交易，失敗全回滾）。

    與 server 的 success 路徑同構：dispatched_at 清掉、worker_id 標 'manual'。
    fail_count/attempts/engine 刻意不動——那是這筆任務被爬的歷史，留著才查得出來。
    先寫 success 對線上 worker 是安全的：worker 遲到回報時 server 看到 state='success'
    就 ignored，不會降級（同 mops_fill.py）。
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        for u in updates:
            conn.execute(
                "UPDATE tasks SET state='success', announce_date=?, source=?,"
                " raw_title=?, revenue=?, yoy=?, worker_id='manual',"
                " dispatched_at=NULL, updated_at=? WHERE id=?",
                (u["announce_date"], u["source"], u["raw_title"],
                 u["revenue"], u["yoy"], now, u["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(updates)


def main():
    ap = argparse.ArgumentParser(description="套用人工確認的公布日 override")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--csv", default="data/date_overrides.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fieldnames, numbered = load_overrides(args.csv)
    bad = validate_all(fieldnames, numbered)
    for line, r, err in bad:
        print(f"  ✗ 第 {line} 行 {r.get('stock_id','?')} "
              f"{r.get('roc_year','?')}/{r.get('roc_month','?')}：{err}")
    if bad:
        raise SystemExit(f"{len(bad)} 處不合格，未寫入任何東西（先修 CSV）。")

    good = [row for _, row in numbered]
    n_late = sum(1 for r in good if classify(r) == "late")
    print(f"{args.csv}：{len(good)} 行全部通過（其中 {n_late} 行是窗外遲交）")

    now = int(time.time())
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    def lookup(sid, ry, rm):
        return conn.execute(
            "SELECT id, state, announce_date, source, raw_title, revenue, yoy"
            " FROM tasks WHERE stock_id=? AND roc_year=? AND roc_month=?",
            (sid, ry, rm)).fetchone()

    updates, skipped, orphans = build_updates(good, lookup)
    for r in orphans:
        print(f"  ⚠️ DB 找不到 {r['stock_id']} {r['roc_year']}/{r['roc_month']}，略過")
    for r in skipped:
        print(f"  = {r['stock_id']} {r['roc_year']}/{r['roc_month']} "
              f"已是 {canonical_date(r)}，略過")
    for u in updates:
        r = u["row"]
        print(f"  → {r['stock_id']} {r['roc_year']}/{r['roc_month']} "
              f"{u['was'][0]}/{u['was'][1]} ⇒ success/{u['announce_date']} "
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

    print(f"已寫入 {apply_updates(conn, updates, now)} 筆。")
    conn.close()


if __name__ == "__main__":
    main()

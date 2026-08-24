#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「公司首次公開之前（不可能有月營收申報）」的任務移出待爬池，標成 state='prelisting'。

切點（精準）：每檔的 goodinfo first_public_gi＝min(上市/上櫃/興櫃/公開發行日)，
是公司「開始成為公開發行公司、負有月營收申報義務」的最早日（含已畢業到上市者的早年興櫃/
公開發行日，官方現況快照拿不到、goodinfo 有）。任務月早於此日者→不可能有資料→安全移出。

- 預設只動 undone/failed/dispatched；success 要加 --demote-success 才碰。
- 冪等 + 對線上 worker 安全（BEGIN IMMEDIATE；標成 prelisting 後不再被 lease）。
- first_public_gi 早於/等於任務窗頭(民國109/1)者→全窗已公開→不砍（其空白是 recall 漏抓）。

--demote-success：把「上市前卻是 success」的假資料打回 prelisting
--------------------------------------------------------------------------------
本工具原本只提示「⚠️ N 筆 success 落在首次公開之前(疑 Yahoo 錯配)」而不動它。實測那 183
筆確實是錯配：公司當時還不是公開發行公司，沒有月營收申報義務，事件不存在。加這個旗標就
會把它們一起標成 prelisting（而不是 failed——failed 會被重新 lease，再抓一次只會重現
同一個錯配）。

緩衝 --grace-months（預設 1）：公司在「首次公開當月」補報「前一個月」營收是合法的，
實測 18 筆都長這樣且 raw_title 對得上（例：竑騰 113/5 營收於 2024-06-07 公布、公開發行
日 2024-06-04）。只有早 2 個月以上才降級——那 165 筆每一筆都有獨立佐證：157 筆 raw_title
根本抽不到營收金額（是 Yahoo 頁面模板碎片），另 8 筆 raw_title 寫的是別年的同月份
（如「宏碁智新 115年6月」被掛到任務 111/6）。降級前會把原值連同 raw_title 寫進 CSV 存證。

前置：先跑 python3 -m goodinfo.build_stock_dates 與 python3 -m goodinfo.goodinfo_worker，
      讓 stock_dates.db 的 goodinfo_dates 有資料。

用法：
  python3 mark_prelisting.py --dry-run                     # 只看計畫，不寫入
  python3 mark_prelisting.py                               # 標記上市前的 undone/failed
  python3 mark_prelisting.py --demote-success --dry-run    # 連假 success 一起看
  python3 mark_prelisting.py --demote-success              # 連假 success 一起標
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


def shift_key(key, months):
    """roc_year*100+roc_month 這種 key 的月份位移（可正可負）。11311 位移 -1 → 11310。

    key 不是連續數（每年只用 01~12），所以不能直接加減，得換算成「絕對月序」再換回來。
    """
    y, m = divmod(key, 100)
    t = y * 12 + (m - 1) + months
    return (t // 12) * 100 + (t % 12) + 1


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


def build_plan(conn, cutoffs, grace_months=None):
    """回傳 [(stock_id, cutoff_key, first_public_gi, prune_count, state_breakdown,
              succ_before, demote_key, demote_count)]。

    - prune_count / state_breakdown：切點前的 undone/failed/dispatched（本來就要標的）
    - succ_before：切點前的 success 總數，含緩衝期內的合法補報，僅供提示
    - demote_key / demote_count：套 grace 後真正該降級的 success（早於切點 >grace 個月）。
      grace_months=None 代表這輪不打算降級，demote_count 一律 0、也不因它把公司列進計畫。
    """
    from collections import Counter
    plan = []
    for sid, (ckey, fp) in cutoffs.items():
        rows = conn.execute(
            "SELECT state, roc_year*100+roc_month AS k FROM tasks WHERE stock_id=?"
            " AND (roc_year*100+roc_month)<?", (sid, ckey)).fetchall()
        pre = [r[0] for r in rows if r[0] in ACTIVE]
        succ = sum(1 for r in rows if r[0] == "success")
        dkey = shift_key(ckey, -grace_months) if grace_months is not None else None
        dcnt = (sum(1 for r in rows if r[0] == "success" and r[1] < dkey)
                if dkey is not None else 0)
        if pre or dcnt:
            plan.append((sid, ckey, fp, len(pre), dict(Counter(pre)), succ, dkey, dcnt))
    return plan


def main():
    ap = argparse.ArgumentParser(description="用 goodinfo 首次公開日標記上市前任務")
    ap.add_argument("--db", default="revswarm.db", help="任務 DB")
    ap.add_argument("--dates-db", default="stock_dates.db", help="goodinfo 日期 DB")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="tmp/prelisting_candidates.csv")
    ap.add_argument("--demote-success", action="store_true",
                    help="把上市前的假 success 也打回 prelisting（見模組 docstring）")
    ap.add_argument("--grace-months", type=int, default=revlib.PRE_PUBLIC_GRACE_MONTHS,
                    help="降級緩衝月數：首次公開前這幾個月內的 success 視為合法補報，"
                         f"不降級（預設 {revlib.PRE_PUBLIC_GRACE_MONTHS}）。改了這個值，"
                         "日後重寫的匯出工具要跟著改，否則 pre_public flag 會不一致")
    ap.add_argument("--demoted-out", default="tmp/prelisting_demoted.csv",
                    help="降級前把原值（含 announce_date/raw_title）寫這裡存證")
    args = ap.parse_args()

    if args.grace_months < 0:
        ap.error("--grace-months 不可為負")

    cutoffs = load_cutoffs(args.dates_db)
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")

    # 取每檔名稱供輸出（純顯示用，不影響決策，放交易外無妨）
    names = {r["stock_id"]: r["name"] for r in conn.execute(
        "SELECT DISTINCT stock_id, name FROM tasks")}

    now = int(time.time())
    if not args.dry_run:
        bak = f"{args.db}.bak-prelisting-{now}"
        dst = sqlite3.connect(bak)
        with dst:
            conn.backup(dst)
        dst.close()
        print(f"已備份 DB → {bak}\n")
        # 計畫、存證查詢、UPDATE 必須在同一個交易裡。線上 worker 隨時可能把切點前某筆
        # 回報成 success：若存證 SELECT 在交易外先跑，那筆會被後面的 UPDATE 連
        # announce_date/raw_title/revenue/yoy 一起清掉，卻不在 --demoted-out 裡（只剩
        # DB 備份可救），印出的降級筆數也會對不上。代價是整段（約 200 個小查詢）持寫鎖
        # 一兩秒，對維運工具可以接受。
        conn.execute("BEGIN IMMEDIATE;")

    try:
        plan = build_plan(conn, cutoffs,
                          grace_months=args.grace_months if args.demote_success else None)
        plan.sort(key=lambda x: -(x[3] + x[7]))
        total = sum(p[3] for p in plan)
        succ_before = sum(p[5] for p in plan)
        demote_total = sum(p[7] for p in plan)

        # 緩衝期內保留的 success 要橫跨「所有」有切點的公司來數：只有緩衝內 success、
        # 沒有任何可砍任務的公司不會進 plan，拿 plan 的 succ_before 相減會少算。
        kept = 0
        if args.demote_success:
            for sid, (ckey, _fp) in cutoffs.items():
                kept += conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE stock_id=? AND state='success'"
                    " AND (roc_year*100+roc_month)>=? AND (roc_year*100+roc_month)<?",
                    (sid, shift_key(ckey, -args.grace_months), ckey)).fetchone()[0]

        # 降級存證：趁 UPDATE 清掉這些欄位之前，在同一個交易裡把原值撈出來。
        demote_rows = []
        if args.demote_success:
            for sid, ck, fp, n, br, sc, dk, dc in plan:
                if not dc:
                    continue
                for r in conn.execute(
                        "SELECT roc_year, roc_month, announce_date, revenue, yoy,"
                        "       engine, source, raw_title FROM tasks"
                        " WHERE stock_id=? AND state='success'"
                        "   AND (roc_year*100+roc_month)<?"
                        " ORDER BY roc_year, roc_month", (sid, dk)):
                    demote_rows.append([sid, names.get(sid, ""), fp, r["roc_year"],
                                        r["roc_month"], r["announce_date"], r["revenue"],
                                        r["yoy"], r["engine"], r["source"], r["raw_title"]])

        marked = demoted = 0
        if not args.dry_run:
            for sid, ckey, fp, cnt, br, sc, dkey, dcnt in plan:
                cur = conn.execute(
                    "UPDATE tasks SET state='prelisting', dispatched_at=NULL,"
                    " worker_id=NULL, updated_at=?"
                    " WHERE stock_id=? AND (roc_year*100+roc_month)<?"
                    " AND state IN ('undone','failed','dispatched')",
                    (now, sid, ckey))
                marked += cur.rowcount
                if args.demote_success and dcnt:
                    # 連同爬到的內容一起清掉：留著會變成「prelisting 卻有公布日」的髒
                    # 資料，原值已收進 demote_rows（同交易內讀的），DB 備份也還在。
                    cur = conn.execute(
                        "UPDATE tasks SET state='prelisting', announce_date=NULL,"
                        " source=NULL, raw_title=NULL, revenue=NULL, yoy=NULL,"
                        " dispatched_at=NULL, worker_id=NULL, updated_at=?"
                        " WHERE stock_id=? AND (roc_year*100+roc_month)<?"
                        " AND state='success'", (now, sid, dkey))
                    demoted += cur.rowcount
            conn.commit()
    except Exception:
        if not args.dry_run:
            conn.rollback()
        raise

    # --- 以下都是報表，交易已收掉 ---
    print(f"goodinfo 首次公開晚於窗頭的公司: {len(cutoffs)} 檔")
    print(f"★ 其中有『上市前 undone/failed/dispatched 任務』可標 prelisting: "
          f"{len(plan)} 檔, {total} 筆")
    if args.demote_success:
        print(f"★ --demote-success：另有 {demote_total} 筆上市前 success 要降級"
              f"（緩衝 {args.grace_months} 個月，緩衝內的 {kept} 筆視為合法補報，保留）")
    elif succ_before:
        print(f"  ⚠️ 另有 {succ_before} 筆 success 落在首次公開之前(疑 Yahoo 錯配)"
              f"——不動、僅提示。要一起處理請加 --demote-success")

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["stock_id", "name", "first_public_gi", "prune_count",
                    "state_breakdown", "success_before_cutoff", "demote_count"])
        for sid, ck, fp, n, br, sc, dk, dc in plan:
            w.writerow([sid, names.get(sid, ""), fp, n, br, sc, dc])
    print(f"  完整清單 → {args.out}")

    if args.demote_success:
        # 即使這輪 0 筆也要重寫（只有表頭）：不然上一輪的存證檔原地留著，
        # 下次來看會以為那是本輪降級的名單。
        os.makedirs(os.path.dirname(args.demoted_out) or ".", exist_ok=True)
        with open(args.demoted_out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["stock_id", "name", "first_public_gi", "roc_year", "roc_month",
                        "announce_date", "revenue", "yoy", "engine", "source", "raw_title"])
            w.writerows(demote_rows)
        print(f"  降級存證 ({len(demote_rows)} 筆) → {args.demoted_out}")

    print(f"\n{'代號':<6}{'名稱':<9}{'首次公開':<12}{'砍除':>5}{'降級':>5}  各state")
    for sid, ck, fp, n, br, sc, dk, dc in plan:
        print(f"{sid:<6}{names.get(sid,''):<9}{fp:<12}{n:>5}{dc:>5}  {br}"
              + (f"  ⚠️success前{sc}" if sc else ""))

    if args.dry_run:
        print("\n[dry-run] 未寫入。")
        conn.close()
        return

    print(f"\n已標記 {marked} 筆上市前任務為 prelisting。")
    if args.demote_success:
        print(f"已降級 {demoted} 筆假 success 為 prelisting（原值見 {args.demoted_out}）。")
    by = {r["state"]: r["c"] for r in conn.execute(
        "SELECT state, COUNT(*) c FROM tasks GROUP BY state")}
    print("目前各狀態：", by)
    conn.close()


if __name__ == "__main__":
    main()

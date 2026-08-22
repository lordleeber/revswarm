#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 server DB 裡爬到的月營收公布日匯出成研究用 CSV。

產出兩份，各司其職（見 README「匯出」章節）：

  1. revenue_dates.csv  分析主檔。只有 state='success'，一列 = 一個「公布日事件」。
     除了 DB 原欄位，另補下游一定會自己算的東西：
       - market      從 stocks.csv join（sii=上市 / otc=上櫃），上市櫃交易規則不同要分組
       - rev_ym      營收「所屬」月份的西元 YYYY-MM，接股價資料用這欄（DB 存的是民國）
       - lag_days    公布日 − 營收月月底，依窗規則必落 1~15
       - yoy_scope   yoy 那個數字是單月(monthly)還是累計(cumulative)年增率——DB 兩種
                     混在同一欄，不分就會拿累計當單月用（見 revlib.yoy_scope）
       - flags       這列的已知疑點，分號分隔；空 = 沒踩到任何一條（見 row_flags）
       - confidence  flags 非空 → low，否則 high。做事件研究請先篩 confidence='high'
     並把 yoy 的 999999.99 哨兵值清成空值（見 clean_yoy）。

  2. missing.csv        缺口清單。所有非 success 的列，一列 = 一個「沒有事件日」的 (股票×月)。
     state 有四種，語意完全不同，別混為一談：
       - failed      爬過（民國年+西元年都試）仍找不到 → 真缺口，可再撈
       - prelisting  該月公司尚未公開發行 → 事件本來就不存在，不該算進覆蓋率分母
       - undone      還沒爬到（爬蟲未收工時才會有）→ 不是缺口，是還沒做
       - dispatched  已租給 worker、還沒回報 → 同上
     刻意不在 SQL 裡濾掉 undone/dispatched：那會讓「還沒爬」在缺口清單裡靜靜消失，
     下游把 missing.csv 的列數當成缺口總量就會低估。要算真缺口請自己篩 state='failed'。

用法：
  python3 export.py                                     # → revenue_dates.csv + missing.csv
  python3 export.py --db revswarm.db --out out.csv --missing-out gaps.csv
  python3 export.py --no-missing                        # 只出主檔
"""

import argparse
import calendar
import csv
import datetime
import os
import sqlite3
import sys

import revlib

MAIN_FIELDS = ["stock_id", "name", "market", "rev_ym", "roc_year", "roc_month",
               "announce_date", "lag_days", "revenue", "yoy", "yoy_scope",
               "engine", "source", "confidence", "flags", "raw_title"]

MISSING_FIELDS = ["stock_id", "name", "market", "rev_ym", "roc_year", "roc_month",
                  "state", "attempts", "fail_count"]

# 999999.99 不是我們寫進去的哨兵——全 repo 沒有任何程式產生它（grep 一下只會命中本檔）。
# 它是**來源文字自己就這麼寫**：去年同期基期為零時，中央社【公告】會寫成
# 「【公告】櫻花建 2022年8月合併營收23.64億元 年增999999.99%」，parse_revenue 照實抽進來。
# 全庫 50 筆，raw_title 100% 都含這串。當成真年增率去算平均會炸，所以匯出時清成空值。
YOY_SENTINEL = 999999.99


def load_markets(path):
    """讀 stocks.csv → {stock_id: market}。檔案不在就回空 dict（market 欄留空）。"""
    if not os.path.isfile(path):
        print(f"警告：找不到 {path}，market 欄將留空", file=sys.stderr)
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r["stock_id"]: r["market"] for r in csv.DictReader(f)}


def rev_ym(roc_year, roc_month):
    """(109, 1) → '2020-01'：營收所屬月份的西元年月。"""
    return f"{roc_year + 1911:04d}-{roc_month:02d}"


def lag_days(announce_date, roc_year, roc_month):
    """
    公布日距「營收月月底」幾天。109/1 營收月底 2020-01-31，公布 2020-02-10 → 10。
    announce_date 為空或格式異常回 None（不讓一筆髒資料中斷整份匯出）。
    """
    if not announce_date:
        return None
    try:
        d = datetime.date.fromisoformat(announce_date)
    except ValueError:
        return None
    year = roc_year + 1911
    eom = datetime.date(year, roc_month, calendar.monthrange(year, roc_month)[1])
    return (d - eom).days


def clean_yoy(yoy):
    """把哨兵值清成 None；真實值原樣回傳。"""
    if yoy is None or yoy == YOY_SENTINEL:
        return None
    return yoy


def load_first_public(path):
    """讀 stock_dates.db 的 goodinfo 首次公開日 → {stock_id: (roc_year, roc_month)}。

    首次公開＝min(上市/上櫃/興櫃/公開發行)，也就是公司開始負月營收申報義務的最早月份。
    檔案不在就回空 dict（pre_public 這條 flag 靜默停用，其餘照常）。
    """
    if not os.path.isfile(path):
        print(f"警告：找不到 {path}，pre_public flag 將停用", file=sys.stderr)
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    out = {}
    try:
        rows = conn.execute("SELECT stock_id, first_public_gi FROM goodinfo_dates"
                            " WHERE status='ok' AND first_public_gi IS NOT NULL")
        for sid, fp in rows:
            try:
                out[sid] = (int(fp[:4]) - 1911, int(fp[5:7]))
            except (TypeError, ValueError):
                continue
    except sqlite3.Error as e:                  # 舊版 stock_dates.db 沒這張表
        print(f"警告：讀 goodinfo_dates 失敗（{e}），pre_public flag 將停用", file=sys.stderr)
    finally:
        conn.close()
    return out


def row_flags(row, first_public=None, grace_months=None):
    """回傳這列的已知疑點 list（空 list = 沒踩到）。每條都是實測過的錯誤來源：

      no_revenue   raw_title 抽不到營收金額 → 多半只是 Yahoo 頁面模板碎片，不是新聞
                   標題，日期沒有文字佐證。這層週末率 4.04%、偏離慣用申報日>=3天 18.35%，
                   有金額那層只有 1.15% / 7.15%。
      no_anchor    raw_title 既無公司名也無股票代號 → 可能抓到彙總頁（CMoney 盤後速報
                   那種一頁列一堆股票的），無法確認講的是這家。
      year_conflict raw_title 講的是「別年的同月份」（見 revlib.title_year_conflict）。
      pre_public   營收月早於公司首次公開月「超過 grace_months 個月」→ 當時沒有申報義務，
                   事件不存在。（mark_prelisting --demote-success 會把這種打回 prelisting；
                   還留在主檔的多半是 stock_dates.db 沒涵蓋到的公司。）

    grace_months 預設取 revlib.PRE_PUBLIC_GRACE_MONTHS，與 mark_prelisting 的降級門檻
    同源。若降級時用了非預設的 --grace-months，這裡要傳同一個值，否則會把「刻意保留的
    合法補報」標成 pre_public/low。
    """
    flags = []
    title = row["raw_title"] or ""
    if row["revenue"] is None:
        flags.append("no_revenue")
    if row["name"] not in title and row["stock_id"] not in title:
        flags.append("no_anchor")
    if revlib.title_year_conflict(title, row["roc_year"], row["roc_month"]):
        flags.append("year_conflict")
    fp = (first_public or {}).get(row["stock_id"])
    if fp:
        grace = (revlib.PRE_PUBLIC_GRACE_MONTHS if grace_months is None else grace_months)
        lead = (fp[0] * 12 + fp[1]) - (row["roc_year"] * 12 + row["roc_month"])
        if lead > grace:
            flags.append("pre_public")
    return flags


def pct(part, whole):
    """百分比字串；分母為 0 回 'n/a'（空 DB / 全 prelisting 的子集 DB 會遇到）。"""
    return f"{100.0 * part / whole:.1f}%" if whole else "n/a"


def write_csv(path, fields, rows):
    """utf-8-sig：Excel 直接開中文不亂碼。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="匯出月營收公布日 CSV")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--stocks", default="stocks.csv", help="market 對照表")
    ap.add_argument("--out", default="revenue_dates.csv", help="分析主檔（只有 success）")
    ap.add_argument("--missing-out", default="missing.csv", help="缺口清單（非 success）")
    ap.add_argument("--no-missing", action="store_true", help="不產缺口清單")
    ap.add_argument("--dates-db", default="stock_dates.db",
                    help="goodinfo 首次公開日（供 pre_public flag；找不到就停用該條）")
    ap.add_argument("--grace-months", type=int, default=revlib.PRE_PUBLIC_GRACE_MONTHS,
                    help="pre_public flag 的緩衝月數，須與 mark_prelisting --grace-months "
                         f"一致（預設 {revlib.PRE_PUBLIC_GRACE_MONTHS}）")
    args = ap.parse_args()

    if args.grace_months < 0:
        ap.error("--grace-months 不可為負")

    markets = load_markets(args.stocks)
    first_public = load_first_public(args.dates_db)
    # 唯讀開啟：匯出時 worker/server 可能還在寫，不要意外持鎖或改到 DB。
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 三段查詢（主檔／缺口／統計）必須看同一個快照：DB 是 WAL，一個唯讀交易就能鎖住
    # 一致的讀取視圖。否則 worker 在中間把一筆 undone 改成 success，那筆會兩份檔案
    # 都不出現、統計也對不上列數。isolation_level=None 才不會被 python 自動插交易。
    conn.isolation_level = None
    conn.execute("BEGIN")

    main_rows = []
    flag_tally = {}
    for r in conn.execute(
            """SELECT stock_id, name, roc_year, roc_month, announce_date,
                      revenue, yoy, engine, source, raw_title
                 FROM tasks WHERE state='success'
                ORDER BY stock_id, roc_year, roc_month"""):
        flags = row_flags(r, first_public, args.grace_months)
        for f in flags:
            flag_tally[f] = flag_tally.get(f, 0) + 1
        main_rows.append({
            "stock_id": r["stock_id"],
            "name": r["name"],
            "market": markets.get(r["stock_id"], ""),
            "rev_ym": rev_ym(r["roc_year"], r["roc_month"]),
            "roc_year": r["roc_year"],
            "roc_month": r["roc_month"],
            "announce_date": r["announce_date"],
            "lag_days": lag_days(r["announce_date"], r["roc_year"], r["roc_month"]),
            "revenue": r["revenue"],
            "yoy": clean_yoy(r["yoy"]),
            "yoy_scope": revlib.yoy_scope(r["raw_title"]),
            "engine": r["engine"],
            "source": r["source"],
            "confidence": "low" if flags else "high",
            "flags": ";".join(flags),
            "raw_title": r["raw_title"],
        })
    write_csv(args.out, MAIN_FIELDS, main_rows)
    print(f"主檔  {len(main_rows):>7,} 列 → {args.out}")

    low = sum(1 for r in main_rows if r["confidence"] == "low")
    cum = sum(1 for r in main_rows if r["yoy_scope"] == "cumulative")
    print(f"      confidence=high {len(main_rows) - low:>7,} 列"
          f"（{pct(len(main_rows) - low, len(main_rows))}）"
          f"／low {low:,} 列  flags：{flag_tally}")
    print(f"      yoy_scope=cumulative {cum:,} 列（這些的 yoy 是累計年增率，別當單月用）")

    if not args.no_missing:
        miss_rows = []
        for r in conn.execute(
                """SELECT stock_id, name, roc_year, roc_month,
                          state, attempts, fail_count
                     FROM tasks WHERE state<>'success'
                    ORDER BY stock_id, roc_year, roc_month"""):
            miss_rows.append({
                "stock_id": r["stock_id"],
                "name": r["name"],
                "market": markets.get(r["stock_id"], ""),
                "rev_ym": rev_ym(r["roc_year"], r["roc_month"]),
                "roc_year": r["roc_year"],
                "roc_month": r["roc_month"],
                "state": r["state"],
                "attempts": r["attempts"],
                "fail_count": r["fail_count"],
            })
        write_csv(args.missing_out, MISSING_FIELDS, miss_rows)
        print(f"缺口  {len(miss_rows):>7,} 列 → {args.missing_out}")

    # --- 覆蓋率摘要 ---
    by_state = {s: c for s, c in conn.execute(
        "SELECT state, COUNT(*) FROM tasks GROUP BY state")}
    conn.execute("ROLLBACK")          # 唯讀交易，收掉快照
    total = sum(by_state.values())
    succ = by_state.get("success", 0)
    pre = by_state.get("prelisting", 0)
    # 空 DB 或整份都是 prelisting（小型子集 DB 會這樣）時分母為 0，
    # 不能讓最後一行摘要炸掉整份已經寫好的匯出。
    print(f"\n覆蓋率  {succ:,}/{total:,} = {pct(succ, total)}（含全部任務）")
    if pre:
        # prelisting 是「公司尚未公開發行」，事件不存在，不該當分母。
        print(f"        {succ:,}/{total - pre:,} = {pct(succ, total - pre)}"
              f"（扣除 {pre:,} 筆 prelisting，這才是真實可得率）")
    print(f"各 state：{by_state}")


if __name__ == "__main__":
    main()

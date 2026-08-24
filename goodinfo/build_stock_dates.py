#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建立 stock_dates.db：記錄 stocks.csv 每一檔的 上市 / 上櫃 / 興櫃登錄 日期。

資料來源（皆放 tmp/，可重抓）：
  - stocks.csv                 股票宇宙（stock_id, name, market）
  - tmp/twse_listed_L.csv      TWSE 上市公司基本資料（上市日期）      openapi.twse.com.tw t187ap03_L
  - tmp/t187ap03_O.csv         TPEx 上櫃公司基本資料（上櫃日期）      tpex openapi mopsfin_t187ap03_O
  - tmp/tpex_emerging_R.csv    TPEx 興櫃公司基本資料（興櫃登錄日）    tpex openapi mopsfin_t187ap03_R
  - tmp/107.csv ~ 114.csv      TPEx 申請上市公司（股票上市買賣日，補 TWSE 缺漏）

限制（誠實記錄）：上市/上櫃/興櫃「基本資料」都是「現況成員」快照——公司一旦畢業到更高板，
低板的歷史日期就查不到（如已上市的 6446 查不到它早年的上櫃/興櫃日）。故 otc_date / emerging_date
對「已畢業公司」多為空；first_public 只能取「目前查得到的最早日」，是下界近似，非絕對首次公開。

用法（repo 根目錄）：
  python3 -m goodinfo.build_stock_dates            # 重建 stock_dates.db
  python3 -m goodinfo.build_stock_dates --db X.db
"""

import argparse
import csv
import glob
import io
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_dates(
  stock_id      TEXT PRIMARY KEY,
  name          TEXT,
  market        TEXT,            -- stocks.csv: sii=上市 / otc=上櫃
  listed_date   TEXT,            -- 上市日 (TWSE)          YYYY-MM-DD
  otc_date      TEXT,            -- 上櫃日 (TPEx)          YYYY-MM-DD
  emerging_date TEXT,            -- 興櫃登錄日 (TPEx)      YYYY-MM-DD
  first_public  TEXT,            -- 目前查得到的最早日（下界近似）
  sources       TEXT             -- 有值的來源標記
);
"""


def norm_ad8(s):
    """'20180808' -> '2018-08-08'；不符回 None。"""
    s = (s or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None


def norm_roc_slash(s):
    """'115/05/22' -> '2026-05-22'；不符回 None。"""
    s = (s or "").strip()
    p = s.split("/")
    if len(p) == 3 and p[0].isdigit():
        return f"{int(p[0])+1911:04d}-{int(p[1]):02d}-{int(p[2]):02d}"
    return None


def load_stocks(path="stocks.csv"):
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["stock_id"].strip()] = r.get("name", "").strip(), r.get("market", "").strip()
    return out


def load_twse_listed(listed, path="tmp/twse_listed_L.csv"):
    try:
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            d = norm_ad8(r.get("上市日期"))
            if r["公司代號"].strip() and d:
                listed[r["公司代號"].strip()] = d
    except FileNotFoundError:
        pass


def load_twse_applicants(listed, name_hint):
    """tmp/107-114.csv（Big5）：補 TWSE 缺漏的上市買賣日（取較早者）。"""
    for f in glob.glob("tmp/1[0-1][0-9].csv"):
        for r in list(csv.reader(io.StringIO(open(f, "rb").read().decode("cp950"))))[2:]:
            if len(r) > 9 and r[1].strip():
                d = norm_roc_slash(r[9])
                if d and (r[1].strip() not in listed or d < listed[r[1].strip()]):
                    listed[r[1].strip()] = d


def load_otc(path="tmp/t187ap03_O.csv"):
    out = {}
    try:
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            d = norm_ad8(r.get("上櫃日期"))
            if r["公司代號"].strip() and d:
                out[r["公司代號"].strip()] = d
    except FileNotFoundError:
        pass
    return out


def load_emerging(path="tmp/tpex_emerging_R.csv"):
    out = {}
    try:
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            d = norm_ad8(r.get("興櫃登錄日"))
            if r["公司代號"].strip() and d:
                out[r["公司代號"].strip()] = d
    except FileNotFoundError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser(description="建立 stock_dates.db")
    ap.add_argument("--db", default="stock_dates.db")
    args = ap.parse_args()

    stocks = load_stocks()
    listed = {}
    load_twse_listed(listed)
    load_twse_applicants(listed, stocks)
    otc = load_otc()
    emerging = load_emerging()

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM stock_dates;")     # 冪等：每次重建

    rows = []
    for sid, (name, market) in stocks.items():
        ld, od, ed = listed.get(sid), otc.get(sid), emerging.get(sid)
        avail = [d for d in (ld, od, ed) if d]
        first = min(avail) if avail else None
        src = "+".join(s for s, d in (("上市", ld), ("上櫃", od), ("興櫃", ed)) if d) or "無"
        rows.append((sid, name, market, ld, od, ed, first, src))
    conn.executemany(
        "INSERT INTO stock_dates(stock_id,name,market,listed_date,otc_date,"
        "emerging_date,first_public,sources) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()

    n = len(rows)
    def cnt(col): return conn.execute(
        f"SELECT COUNT(*) FROM stock_dates WHERE {col} IS NOT NULL").fetchone()[0]
    print(f"stock_dates.db 建立完成：{n} 檔")
    print(f"  有上市日   : {cnt('listed_date')}")
    print(f"  有上櫃日   : {cnt('otc_date')}")
    print(f"  有興櫃登錄日: {cnt('emerging_date')}")
    print(f"  有 first_public(至少一種): {cnt('first_public')}")
    print(f"  完全無日期 : {n - cnt('first_public')}")
    conn.close()


if __name__ == "__main__":
    main()

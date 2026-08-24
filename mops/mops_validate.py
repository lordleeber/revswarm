#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 MOPS「歷史重大訊息」(t05st01) 的官方申報日，交叉驗證 revswarm(Yahoo) 抓到的公布日。

MOPS 只涵蓋「自願把月營收發成重大訊息」的約 32 家（台積電/聯發科/台塑/中鋼/台塑化…），
但那些是**官方申報日、精確到時分**，是最高信度的黃金基準。用它來：
  1. 驗證 Yahoo 抓到的日期對不對（一致率）。
  2. 抓出不一致的（Yahoo 可能抓錯）。
  3. 找出「MOPS 有、Yahoo 卻沒成功」的，作為可回收/校準的線索。

只讀 revswarm.db（不寫、不干擾線上爬取）；MOPS 結果快取到 baseline CSV，可續跑。

用法：
  # 驗證目前 revswarm 已成功的那些股票（預設）
  python3 -m mops.mops_validate

  # 指定股票
  python3 -m mops.mops_validate --codes 2330 2454 1301 6505

  # 建全量基準（1848 檔都問一次 MOPS，多數回空；建議 tmux 背景跑，可續跑）
  python3 -m mops.mops_validate --codes-file data/active_stocks.txt

輸出：
  mops_baseline.csv     MOPS 官方申報日快取（stock_id,roc_year,roc_month,announce_date,...）
  mops_validation.csv   逐筆比對結果（agree/disagree/yahoo_missing）
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://mops.twse.com.tw/mops/api/t05st01"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Content-Type": "application/json", "Accept": "application/json",
    "Origin": "https://mops.twse.com.tw", "Referer": "https://mops.twse.com.tw/mops/",
}

# --- 月營收公告辨識（沿用已驗證的規則，見 playground/mops_revenue_dates.py）------
REV_KW = re.compile(r"營收|營業額|營業收入")
YM_PAT = re.compile(r"(\d{2,4})\s*年\s*(\d{1,2})\s*月")
NOISE = re.compile(r"受邀|說明會|法說|論壇|概況|法人|會議|Conference|股東|"
                   r"董事會|媒體|更正|修正|更新|重編|補充|澄清|差異|核閱|查核|調整")
EARNINGS = re.compile(r"盈餘|損益|稅前|稅後|淨利|獲利|EPS|每股")
DAY_MAX = 15                 # 月營收次月10日前申報，遇假日順延，公布日 <= 15
MARKET_CLOSE = (13, 30)      # 台股收盤；之後(含)視為盤後

BASELINE_FIELDS = ["stock_id", "roc_year", "roc_month", "announce_date",
                   "announce_time", "after_close", "subject"]


def roc_to_ad(roc_date):
    """'109/02/10' -> '2020-02-10'；不符原樣回傳。"""
    m = re.match(r"^\s*(\d{2,3})/(\d{1,2})/(\d{1,2})\s*$", str(roc_date))
    if not m:
        return roc_date
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return f"{y + 1911:04d}-{mo:02d}-{d:02d}"


def revenue_roc(subject):
    """由主旨的『年月』推營收所屬的 (民國年, 月)；西元自動轉民國。抓不到回 None。"""
    m = YM_PAT.search(subject)
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    roc_year = y - 1911 if y > 1000 else y      # >1000 視為西元
    if not (1 <= mo <= 12):
        return None
    return roc_year, mo


def is_after_close(hhmmss):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", str(hhmmss))
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= MARKET_CLOSE


def _day(ad_date):
    m = re.match(r"^\d{4}-\d{2}-(\d{2})$", str(ad_date))
    return int(m.group(1)) if m else None


def is_monthly_revenue(subject):
    return bool(REV_KW.search(subject)) and bool(YM_PAT.search(subject)) \
        and not NOISE.search(subject)


# --- MOPS API ---------------------------------------------------------------
def fetch_year(company_id, roc_year, retries=3, sleep=0.4):
    payload = {"companyId": str(company_id), "year": str(roc_year),
               "month": "all", "firstDay": "", "lastDay": ""}
    body = json.dumps(payload).encode("utf-8")
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if data.get("code") == 200 and isinstance(data.get("result"), dict):
                return data["result"], "ok"
            if data.get("code") == 406:              # 查無相符（該公司該年無重大訊息）
                return None, "empty"
            last = f"code={data.get('code')}"
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = repr(e)
        if attempt < retries:
            time.sleep(sleep * attempt * 2)
    return None, f"error:{last}"


def fetch_company(company_id, years, sleep=0.4):
    """回傳 {(roc_year,roc_month): {announce_date,announce_time,after_close,subject}}。"""
    out = {}
    for y in years:
        result, st = fetch_year(company_id, y, sleep=sleep)
        if result:
            titles = [t.get("main") for t in result.get("titles", [])]
            def idx(n, d): return titles.index(n) if n in titles else d
            i_date, i_time, i_sub = idx("發言日期", 2), idx("發言時間", 3), idx("主旨", 4)
            for row in result.get("data", []):
                subject = str(row[i_sub])
                if not is_monthly_revenue(subject):
                    continue
                ad_date = roc_to_ad(row[i_date])
                d = _day(ad_date)
                if d is None or not (1 <= d <= DAY_MAX):     # 非次月1~15 → 非月營收申報
                    continue
                if EARNINGS.search(subject):                 # 排除自結損益(如中鋼)
                    continue
                key = revenue_roc(subject)
                if not key:
                    continue
                if key not in out:                           # 同月多筆取最早那筆
                    out[key] = {"announce_date": ad_date, "announce_time": row[i_time],
                                "after_close": "Y" if is_after_close(row[i_time]) else "N",
                                "subject": subject}
        time.sleep(sleep)
    return out


# --- 基準快取（可續跑）------------------------------------------------------
def load_done(path):
    done_path = path + ".done"
    if not os.path.exists(done_path):
        return set()
    with open(done_path, encoding="utf-8") as f:
        return {ln.strip() for ln in f if ln.strip()}


def mark_done(path, code):
    with open(path + ".done", "a", encoding="utf-8") as f:
        f.write(code + "\n")


def build_baseline(codes, years, path, sleep, refresh):
    done = set() if refresh else load_done(path)
    todo = [c for c in codes if c not in done]
    if refresh and os.path.exists(path):
        os.remove(path)
        if os.path.exists(path + ".done"):
            os.remove(path + ".done")
    print(f"MOPS 抓取：{len(todo)}/{len(codes)} 檔待抓"
          f"（已快取 {len(codes) - len(todo)}）")
    new_exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=BASELINE_FIELDS)
        if not new_exists:
            w.writeheader()
        for i, code in enumerate(todo, 1):
            got = fetch_company(code, years, sleep=sleep)
            for (ry, rm), v in sorted(got.items()):
                w.writerow({"stock_id": code, "roc_year": ry, "roc_month": rm, **v})
            f.flush()
            mark_done(path, code)
            if got or i % 50 == 0:
                print(f"  [{i}/{len(todo)}] {code}: MOPS 有 {len(got)} 筆月營收")


def load_baseline(path):
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[(r["stock_id"], int(r["roc_year"]), int(r["roc_month"]))] = r
    return out


# --- 讀 revswarm ------------------------------------------------------------
def load_revswarm(db, codes, chunk=500):
    """回傳 {(stock,ry,rm): (state, announce_date)}，限定 codes。

    分批查詢：全量(1848 檔)時 IN(?…) 的參數數會超過舊版 SQLite(<3.32)的
    變數上限 999 而丟 too many SQL variables；切成每批 <=chunk 個避免。
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    codes = list(codes)
    for i in range(0, len(codes), chunk):
        batch = codes[i:i + chunk]
        qmarks = ",".join("?" * len(batch))
        for r in conn.execute(
            f"SELECT stock_id, roc_year, roc_month, state, announce_date"
            f" FROM tasks WHERE stock_id IN ({qmarks})", batch):
            out[(r[0], r[1], r[2])] = (r[3], r[4])
    conn.close()
    return out


def load_names(path="stocks.csv"):
    names = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                names[r["stock_id"]] = r["name"]
    return names


# --- 比對 -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="MOPS 交叉驗證 revswarm 公布日")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--codes", nargs="*", default=[], help="指定股票代號")
    ap.add_argument("--codes-file", help="代號清單檔（每行一個）")
    ap.add_argument("--baseline", default="mops_baseline.csv")
    ap.add_argument("--out", default="mops_validation.csv")
    ap.add_argument("--years", default="109-115")
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--refresh", action="store_true", help="重抓 MOPS（清掉既有快取）")
    args = ap.parse_args()

    a, b = args.years.split("-")
    years = list(range(int(a), int(b) + 1))

    # 決定要驗哪些股票
    codes = list(args.codes)
    if args.codes_file:
        with open(args.codes_file, encoding="utf-8") as f:
            codes += [ln.strip().split(",")[0] for ln in f if ln.strip()
                      and not ln.startswith("#")]
    if not codes:               # 預設：revswarm 目前已有 success 的股票
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT stock_id FROM tasks WHERE state='success'")]
        conn.close()
        print(f"未指定 codes：改用 revswarm 目前已成功的 {len(codes)} 檔")
    codes = sorted(set(codes))
    if not codes:
        print("沒有可驗證的股票。"); sys.exit(1)

    # 1) 建/更新 MOPS 基準
    build_baseline(codes, years, args.baseline, args.sleep, args.refresh)
    baseline = load_baseline(args.baseline)
    mops_codes = {k[0] for k in baseline}
    print(f"MOPS 基準：{len(baseline)} 筆月營收，涵蓋 {len(mops_codes)} 檔"
          f"（其餘 {len(codes) - len(mops_codes)} 檔 MOPS 查無，屬正常）")

    # 2) 讀 revswarm 對應狀態
    rev = load_revswarm(args.db, codes)
    names = load_names()

    # 3) 逐筆比對（以 MOPS 有的 key 為母體）
    rows, agree, disagree, missing = [], 0, 0, 0
    for key, mv in sorted(baseline.items()):
        stock, ry, rm = key
        mops_date = mv["announce_date"]
        state, yahoo_date = rev.get(key, (None, None))
        if state == "success" and yahoo_date:
            verdict = "agree" if yahoo_date == mops_date else "disagree"
            agree += verdict == "agree"
            disagree += verdict == "disagree"
        else:
            verdict = f"yahoo_missing({state or 'no-task'})"
            missing += 1
        rows.append({"stock_id": stock, "name": names.get(stock, ""),
                     "roc_year": ry, "roc_month": rm, "yahoo_date": yahoo_date or "",
                     "mops_date": mops_date, "mops_time": mv["announce_time"],
                     "after_close": mv["after_close"], "verdict": verdict})

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["stock_id", "name", "roc_year", "roc_month",
                                          "yahoo_date", "mops_date", "mops_time",
                                          "after_close", "verdict"])
        w.writeheader()
        w.writerows(rows)

    # 4) 報告
    overlap = agree + disagree
    print("\n" + "=" * 56)
    print(f"MOPS 可比對母體：{len(rows)} 筆（{len(mops_codes)} 檔）")
    print(f"  與 Yahoo 皆有 → {overlap} 筆：一致 {agree}、不一致 {disagree}"
          + (f"（一致率 {100.0*agree/overlap:.1f}%）" if overlap else ""))
    print(f"  MOPS 有、Yahoo 尚未成功 → {missing} 筆")
    if disagree:
        print("\n⚠️ 不一致（Yahoo vs MOPS）：")
        for r in rows:
            if r["verdict"] == "disagree":
                print(f"  {r['stock_id']} {r['name']} {r['roc_year']}/{r['roc_month']:>2}"
                      f"  Yahoo={r['yahoo_date']}  MOPS={r['mops_date']} {r['mops_time']}")
    # 「MOPS 有、Yahoo 判 failed」= 可回收（值得重掃）
    recover = [r for r in rows if r["verdict"] == "yahoo_missing(failed)"]
    if recover:
        print(f"\n💡 MOPS 有、但 Yahoo 判 failed 的 {len(recover)} 筆（可回收，"
              f"建議 /admin/requeue-failed 重掃）：")
        for r in recover[:15]:
            print(f"  {r['stock_id']} {r['name']} {r['roc_year']}/{r['roc_month']:>2}"
                  f"  MOPS={r['mops_date']}")
    print(f"\n逐筆結果寫入 {args.out}")


if __name__ == "__main__":
    main()

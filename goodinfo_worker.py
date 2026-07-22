#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Goodinfo 基本資料爬蟲 worker：抓每檔的完整基本資料頁，存成檔並把關鍵日期寫入 stock_dates.db。

輸出：
  goodinfo/basicInfo/<代號>.html   原始頁（存證/可重解析）
  goodinfo/basicInfo/<代號>.json   解析出的全部欄位（~43 欄，含 上市/上櫃/興櫃/公開發行/成立/掛牌 日）
  stock_dates.db  goodinfo_dates 表：關鍵日期索引 + first_public_gi（可查詢、可續跑判斷）

為什麼要它：官方 TWSE/TPEx 現存快照只有「目前所在板」的日期，畢業公司查不到早年上櫃/興櫃日；
goodinfo 對畢業公司仍保有完整歷史（含「公開發行日期」＝月營收申報義務起點），
是判「上市前任務」最準的否決來源。

反爬與繞法（已實測）：goodinfo 有 JS cookie 關卡(CLIENT_KEY)+REINIT 重載 + Cloudflare 被動偵測。
  兩步式：① 先打一次取 stub、從中抓當次 REINIT；② 帶手工 CLIENT_KEY cookie + REINIT 再打 → 真頁。

負責任地用：
  - 預設每檔間隔 --sleep 秒 + 隨機抖動；偵測到擋爬 stub 會退避重試，仍失敗記 blocked 續跑。
  - 可續跑：goodinfo/basicInfo/<代號>.json 已存在即跳過。
  - 可走 SOCKS proxy（--proxy）繞 per-IP 限流。
  - 預設只爬「需要的那批」：stock_dates.db 中 first_public 落窗內(>2020-01)或空白者（--all 爬全部）。
  - 長跑請用 tmux，不要 nohup。goodinfo 資料受其 ToS 保護、勿進版控/散布（goodinfo/ 已 gitignore）。

用法：
  python3 goodinfo_worker.py --dry-run
  python3 goodinfo_worker.py --limit 3
  python3 goodinfo_worker.py --sleep 6
  python3 goodinfo_worker.py --proxy socks5h://127.0.0.1:1080
  python3 goodinfo_worker.py --all
"""

import argparse
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import time

BASE = "https://goodinfo.tw/tw/BasicInfo.asp"
OUTDIR = "goodinfo/basicInfo"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 依 goodinfo stub JS 格式手工組（實測可過關卡）：4.5|const|const|tzoffset|date|0|0|0
CLIENT_KEY = "4.5|41174.1506786617|46729.7062342172|-480|46224.65|0|0|0"

DATE_FIELDS = {          # goodinfo 中文欄位 -> DB 欄位
    "上市日期": "listed_date", "上櫃日期": "otc_date", "興櫃日期": "emerging_date",
    "公開發行日期": "public_date", "成立日期": "incorp_date", "掛牌日期": "ticker_date",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS goodinfo_dates(
  stock_id TEXT PRIMARY KEY, name TEXT, market_gi TEXT,
  listed_date TEXT, otc_date TEXT, emerging_date TEXT,
  public_date TEXT, incorp_date TEXT, ticker_date TEXT,
  first_public_gi TEXT,        -- 上述日期(含公開發行)的最早者＝真正「開始公開申報」起點
  status TEXT, fetched_at INTEGER
);
"""


def curl(url, proxy, timeout=25):
    cmd = ["curl", "-sS", "--http1.1", "--compressed", "-m", str(timeout),
           "-A", UA, "-H", "Accept-Language: zh-TW,zh;q=0.9",
           "-H", "Referer: https://goodinfo.tw/tw/StockList.asp",
           "--cookie", f"CLIENT_KEY={CLIENT_KEY}"]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True)
    return r.stdout.decode("utf-8", "replace"), r.returncode


def is_bad_page(t):
    """非真頁：太小 / JS 重載 stub / Cloudflare 錯誤頁(520/522…)。
    真的基本資料頁一定含「公司名稱」標籤；據此擋掉所有假頁（含 >5KB 的 CF 錯誤頁）。"""
    return len(t) < 5000 or "公司名稱" not in t


def parse_all(t):
    """回傳頁面所有 th(bg_h2 標籤)->td(值) 欄位 dict。"""
    toks = []
    for m in re.finditer(r"<(th|td)\b([^>]*)>(.*?)</\1>", t, re.S):
        tag, attrs, inner = m.group(1), m.group(2), m.group(3)
        txt = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner))).strip()
        toks.append(("L" if (tag == "th" and "bg_h2" in attrs) else "V", txt))
    fields = {}
    for i, (k, txt) in enumerate(toks):
        if k == "L" and txt:
            for j in range(i + 1, len(toks)):
                if toks[j][0] == "V":
                    if txt not in fields and toks[j][1]:
                        fields[txt] = toks[j][1]
                    break
    return fields


def dates_from(fields):
    out = {"name": fields.get("股票名稱") or fields.get("公司名稱"),
           "market_gi": fields.get("上市/上櫃")}
    for zh, col in DATE_FIELDS.items():
        m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", fields.get(zh, ""))
        out[col] = (f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    if m else None)
    ds = [out[c] for c in ("listed_date", "otc_date", "emerging_date", "public_date") if out[c]]
    out["first_public_gi"] = min(ds) if ds else None
    return out


def fetch_one(sid, proxy):
    """兩步式取真頁；回 (raw_html, fields, status)。"""
    stub, _ = curl(f"{BASE}?STOCK_ID={sid}", proxy)
    m = re.search(r"REINIT=([0-9.]+)", stub)
    reinit = m.group(1) if m else "46224.6557291667"
    t, rc = curl(f"{BASE}?STOCK_ID={sid}&REINIT={reinit}", proxy)
    if rc != 0:
        return None, None, f"curl_err({rc})"
    if is_bad_page(t):
        return None, None, "blocked"
    return t, parse_all(t), "ok"


def pick_targets(conn, args):
    if args.codes:
        return list(args.codes)
    if args.all:
        return [r[0] for r in conn.execute(
            "SELECT stock_id FROM stock_dates ORDER BY stock_id")]
    return [r[0] for r in conn.execute(
        "SELECT stock_id FROM stock_dates"
        " WHERE first_public IS NULL OR first_public > '2020-01-31'"
        " ORDER BY stock_id")]


def main():
    ap = argparse.ArgumentParser(description="Goodinfo 基本資料爬蟲")
    ap.add_argument("--db", default="stock_dates.db")
    ap.add_argument("--codes", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--sleep", type=float, default=6.0)
    ap.add_argument("--proxy")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    conn = sqlite3.connect(args.db, timeout=60)
    conn.executescript(SCHEMA)

    def done(sid):     # 續跑：已存檔即跳過
        return os.path.exists(f"{OUTDIR}/{sid}.json")

    targets = [c for c in pick_targets(conn, args) if not done(c)]
    if args.limit:
        targets = targets[:args.limit]

    print(f"待爬 {len(targets)} 檔（輸出 {OUTDIR}/；proxy={args.proxy or '無'}）")
    if args.dry_run:
        print("前 20 檔:", targets[:20])
        conn.close(); return
    if not targets:
        print("沒有待爬項目。"); conn.close(); return

    ok = bad = 0
    for i, sid in enumerate(targets, 1):
        raw, fields, status = fetch_one(sid, args.proxy)
        tries = 0
        while status == "blocked" and tries < 2:
            tries += 1
            time.sleep(15 * tries + random.uniform(0, 5))
            raw, fields, status = fetch_one(sid, args.proxy)
        now = int(time.time())
        if status == "ok":
            with open(f"{OUTDIR}/{sid}.html", "w", encoding="utf-8") as f:
                f.write(raw)
            with open(f"{OUTDIR}/{sid}.json", "w", encoding="utf-8") as f:
                json.dump(fields, f, ensure_ascii=False, indent=1)
            d = dates_from(fields)
            conn.execute(
                "INSERT OR REPLACE INTO goodinfo_dates(stock_id,name,market_gi,"
                "listed_date,otc_date,emerging_date,public_date,incorp_date,"
                "ticker_date,first_public_gi,status,fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, d["name"], d["market_gi"], d["listed_date"], d["otc_date"],
                 d["emerging_date"], d["public_date"], d["incorp_date"],
                 d["ticker_date"], d["first_public_gi"], "ok", now))
            conn.commit()
            ok += 1
            mark = f"公開發行={d['public_date']} 興櫃={d['emerging_date']} first={d['first_public_gi']}"
            name = d["name"] or ""
        else:
            conn.execute("INSERT OR REPLACE INTO goodinfo_dates(stock_id,status,"
                         "fetched_at) VALUES(?,?,?)", (sid, status, now))
            conn.commit()
            bad += 1
            mark = f"[{status}]"; name = ""
        print(f"  [{i}/{len(targets)}] {sid} {name}  {mark}")
        if i < len(targets):
            time.sleep(args.sleep + random.uniform(0, 2))

    print(f"\n完成：ok {ok}，blocked/err {bad}。重跑可續（已存檔者跳過）。")
    conn.close()


if __name__ == "__main__":
    main()

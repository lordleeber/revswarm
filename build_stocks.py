#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
建立 code→name→market 對照表 stocks.csv（worker 查詢 Yahoo 需要中文簡稱）。

資料源：證交所 ISIN 網頁 https://isin.twse.com.tw/isin/C_public.jsp?strMode=N
  strMode=2 上市(sii)、4 上櫃(otc)、5 興櫃(rotc)。頁面為 big5/ms950 編碼的 HTML 表格，
  每列首格為「代號　名稱」（全形空白分隔），另有 ISIN、上市日、市場別、產業別…等欄。

流程：抓三個市場 → 解析出所有 (code,name,market) → 只留 active_stocks.txt 內的代號 →
      寫 stocks.csv(stock_id,name,market)。缺漏的代號會列出，方便人工補。

只用標準庫。用法：
  python3 build_stocks.py                       # 用預設 data/active_stocks.txt → stocks.csv
  python3 build_stocks.py --active X --out Y
"""

import argparse
import csv
import re
import sys
import time
import urllib.request

ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
MODES = [("2", "sii"), ("4", "otc"), ("5", "rotc")]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

_TR = re.compile(r'<tr[^>]*>(.*?)</tr>', re.I | re.S)
_TD = re.compile(r'<td[^>]*>(.*?)</td>', re.I | re.S)
_TAG = re.compile(r'<[^>]+>')
# 首格「代號　名稱」：代號為 4~6 碼數字(可帶一個英數尾碼，如權證/KY)，之後全形/半形空白，再名稱。
_CODE_NAME = re.compile(r'^\s*([0-9]{4,6}[A-Z0-9]?)[\s　]+(\S.*?)\s*$')


def _clean(cell):
    return _TAG.sub('', cell).replace('&nbsp;', ' ').strip()


def fetch(mode, retries=3):
    url = ISIN_URL.format(mode=mode)
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            # ISIN 頁為 big5；ms950 是其超集，較不易 decode 失敗。
            html = raw.decode("ms950", errors="replace")
            if len(html) > 5000:                 # 太短多半是被擋/半截，重試
                return html
            last = f"page too short ({len(html)}B)"
        except Exception as e:                   # noqa: BLE001
            last = repr(e)
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError(last)


def parse_isin(html, market):
    """回傳 {code: name}（僅該市場頁的股票列）。"""
    out = {}
    for tr in _TR.findall(html):
        cells = [_clean(c) for c in _TD.findall(tr)]
        if len(cells) < 6:      # 類別標題列(colspan)只有 1 格，略過
            continue
        m = _CODE_NAME.match(cells[0])
        if not m:
            continue
        code, name = m.group(1), m.group(2)
        name = re.sub(r'[\s　]+', '', name)   # 名稱內不應有空白
        if code and name:
            out[code] = name
    return out


def load_active(path):
    codes = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = line.strip().split(",")[0].strip()
            if c and not c.startswith("#"):
                codes.append(c)
    # 去重保序
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser(description="建 stocks.csv (code→name→market)")
    ap.add_argument("--active", default="data/active_stocks.txt",
                    help="股票代號清單（每行一個）")
    ap.add_argument("--out", default="stocks.csv", help="輸出 CSV")
    ap.add_argument("--overrides", default="data/name_overrides.csv",
                    help="人工補名檔(stock_id,name,market)，用於已下市/ISIN 查無者")
    ap.add_argument("--sleep", type=float, default=1.0, help="抓各市場頁間隔秒")
    args = ap.parse_args()

    active = load_active(args.active)
    print(f"active 代號: {len(active)} 檔  來源: {args.active}")

    # code → (name, market)。先抓的市場優先（sii > otc > rotc），避免重複代號跨市場。
    name_market = {}
    # 先載入人工補名（已下市/ISIN 快照查無者），ISIN 若有同代號會覆蓋為最新官方名稱。
    overrides = {}
    try:
        with open(args.overrides, encoding="utf-8-sig") as f:
            for d in csv.DictReader(f):
                sid = (d.get("stock_id") or "").strip()
                nm = (d.get("name") or "").strip()
                mk = (d.get("market") or "manual").strip()
                if sid and nm:
                    overrides[sid] = (nm, mk)
        if overrides:
            print(f"  載入人工補名 {len(overrides)} 檔：{args.overrides}")
    except FileNotFoundError:
        pass
    for mode, market in MODES:
        try:
            html = fetch(mode)
        except Exception as e:                       # noqa: BLE001
            print(f"  [!] 抓 {market}(strMode={mode}) 失敗：{e!r}", file=sys.stderr)
            continue
        got = parse_isin(html, market)
        added = 0
        for code, name in got.items():
            if code not in name_market:
                name_market[code] = (name, market)
                added += 1
        print(f"  {market:5s} 解析 {len(got):5d} 檔，新增 {added}")
        time.sleep(args.sleep)

    rows, missing = [], []
    for code in active:
        if code in name_market:                      # ISIN 官方名稱優先
            name, market = name_market[code]
        elif code in overrides:                      # ISIN 查無 → 用人工補名
            name, market = overrides[code]
        else:
            missing.append(code)
            continue
        rows.append({"stock_id": code, "name": name, "market": market})

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["stock_id", "name", "market"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n寫出 {args.out}：{len(rows)}/{len(active)} 檔有名稱。")
    if missing:
        print(f"⚠️ 缺名稱 {len(missing)} 檔（未寫入，需人工補）：")
        print("   " + " ".join(missing[:60]) + (" ..." if len(missing) > 60 else ""))
        sys.exit(2 if len(missing) > len(active) * 0.05 else 0)


if __name__ == "__main__":
    main()

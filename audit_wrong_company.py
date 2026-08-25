#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
找出「raw_title 講的是別家公司」的 success，並可把它們打回 undone 重爬。

怎麼發生的：
  revlib.parse 找不到精確名稱錨點時，會退回「取頁面上第一個窗內日期」。那條後備路徑
  **沒有任何公司名保護**——_anchor_offsets 的 lookahead（擋「統一」吃到「統一超」）只在
  錨點命中時才起作用。於是 4113 聯上 的任務吃到了 聯上發(2537) 的公告日。

  這幾乎是 google_worker 專屬的問題：它解析 page.inner_text("body")，Google SERP 在純
  文字裡長成「stock.yahoo.com › news › 公告-聯上發-2020... 2020年5月8日 — 聯上發. 253」
  ——公司名塞在網址 slug、用連字號連著、後面還被截斷，錨點對不上。實測後備路徑
  96.8% 出自 google（3,499/3,613），而抓錯公司的 12 筆 100% 出自 google。

⚠️ 這支會降級 success，是不可逆的操作（重爬不保證抓得回來）。所以：
  - 預設 dry-run，要寫入得明確加 --requeue
  - 走 server 的 /admin/requeue，帶著 announce_date 當樂觀鎖（見 Store.requeue）
  - 只挑「本檔名字是別家公司名的前綴、且那個更長的名字出現在 title 裡」——這是可判定
    的事實，不是相似度猜測

跑法（repo 根目錄，server 要跑著）：
  python3 audit_wrong_company.py                                    # 只列出
  python3 audit_wrong_company.py --requeue --server http://127.0.0.1:8000
  python3 audit_wrong_company.py --requeue --server ... --engine yahoo   # 順便換佇列
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

import revlib

DEFAULT_DB = "revswarm.db"
DEFAULT_STOCKS = "stocks.csv"


def load_names(path):
    """{公司名: 股號}。撞名比對的是股號，所以這裡不能只存名字。"""
    if not os.path.exists(path):
        sys.exit(f"⚠️ 讀不到 {path}（先跑 build_stocks.py）。")
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            code = r.get("code") or r.get("stock_id")
            name = (r.get("name") or "").strip()
            if name and code:
                out[name] = code
    return out


def find_wrong(db, names):
    """回傳 [(row, 更長的那個公司名), ...]，只看 success 且有 raw_title 的列。"""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    hits = []
    for r in conn.execute(
            "SELECT stock_id,name,roc_year,roc_month,announce_date,source,engine,raw_title"
            " FROM tasks WHERE state='success' AND raw_title IS NOT NULL"):
        # 帶年月：本檔自己的錨點命中就不算撞名（見 longer_name_in_text 的誤報說明）
        other = revlib.longer_name_in_text(
            r["raw_title"], r["name"], r["stock_id"], names,
            r["roc_year"], r["roc_month"])
        if other:
            hits.append((r, other))
    conn.close()
    return hits


def post_requeue(server, token, items, engine, timeout=60):
    body = {"items": items}
    if engine:
        body["engine"] = engine
    req = urllib.request.Request(
        server.rstrip("/") + "/admin/requeue",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}" if token else ""})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["applied"]


def main():
    revlib.load_env()
    ap = argparse.ArgumentParser(description="找出 raw_title 講的是別家公司的 success")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--stocks", default=DEFAULT_STOCKS)
    ap.add_argument("--server")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"))
    ap.add_argument("--engine", default=None,
                    help="重爬時改走哪個佇列（google 抓錯的建議改 yahoo）")
    ap.add_argument("--requeue", action="store_true",
                    help="⚠️ 真的把它們打回 undone（預設只列出）")
    args = ap.parse_args()

    hits = find_wrong(args.db, load_names(args.stocks))
    print(f"找到 {len(hits)} 筆 raw_title 講的是別家公司\n")
    for r, other in sorted(hits, key=lambda h: (h[0]["stock_id"], h[0]["roc_year"],
                                                h[0]["roc_month"])):
        print(f"  {r['stock_id']} {r['name']} {r['roc_year']}/{r['roc_month']:02d}"
              f"  {r['announce_date']}  ({r['source']})  → title 講的是「{other}」")
        print(f"      {r['raw_title'][:80]}")
    if not hits:
        return
    if not args.requeue:
        print("\n[dry-run] 未寫入。要真的打回重爬請加 --requeue（⚠️ 會降級 success）。")
        return
    if not args.server:
        sys.exit("⚠️ --requeue 需要 --server。")

    items = [{"stock_id": r["stock_id"], "roc_year": r["roc_year"],
              "roc_month": r["roc_month"], "date": r["announce_date"]}
             for r, _ in hits]
    try:
        applied = post_requeue(args.server, args.token, items, args.engine)
    except urllib.error.HTTPError as e:
        sys.exit(f"⚠️ HTTP {e.code}：{e.read()[:200]!r}")
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"⚠️ 連不到 server：{e}")
    print(f"\n打回重爬 {applied['requeued']} 筆"
          + (f"（佇列改成 {args.engine}）" if args.engine else ""))
    if applied["mismatch"]:
        # 我看過之後那列被改過了，判斷不再適用——不動是對的。
        print(f"⚠️ 日期對不上而未處理 {applied['mismatch']} 筆（那列在此之間被改過）")
    if applied["not_requeueable"] or applied["unknown"]:
        print(f"   狀態不可重排 {applied['not_requeueable']} 筆／查無 {applied['unknown']} 筆")


if __name__ == "__main__":
    main()

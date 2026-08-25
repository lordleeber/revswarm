#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 data/title_review.csv 裡某個 verdict 的列全部打回 undone 重爬。

為什麼要有這支：人工讀 raw_title 判成 tbd 的那批，多數不是「日期錯」而是
「證據不足」——title 被 inner_text 截斷、只剩網址片段、或落在多家彙總頁上。
那批全部沒有 url（url 欄是後來才加的），重跑至少能換到出處，讓下次判斷有東西可依據。

⚠️ 這是大規模降級 success 的操作，跑之前想清楚：
  - tbd ≠ 錯。多數的日期很可能是對的，只是無法證明。打回去就是拿「多半正確但
    無法證明的日期」換「重爬結果」，而重爬不保證抓得回來。
  - 實測參考：那 12 筆抓錯公司的重爬，yahoo 1/12、google 4/11 成功。舊月份的
    SERP 結果會衰退——民國 109~111 年那段尤其明顯。
  - 預設 dry-run，要寫入得明確加 --go。跑之前自己先備份 DB。

走 server 的 /admin/requeue，每筆帶著 announce_date 當樂觀鎖（見 Store.requeue）：
判斷是對著那個日期做的，之間若被別的東西改過就不動那一筆。

跑法（repo 根目錄，server 要跑著）：
  python3 requeue_reviewed.py --verdict tbd --server http://127.0.0.1:8000
  python3 requeue_reviewed.py --verdict tbd --server http://127.0.0.1:8000 --go
  python3 requeue_reviewed.py --verdict tbd --server ... --engine gemini --go
"""

import argparse
import os
import sys
import urllib.error

import revlib
from audit_wrong_company import post_requeue
from mops.stamp_verified import DEFAULT_REVIEW, REVIEW_VERDICTS, items_from_review

CHUNK = 500          # 分批只是別讓單一請求太肥；server 端每批本來就是一個交易


def main():
    revlib.load_env()
    ap = argparse.ArgumentParser(description="把某個 verdict 的列打回 undone 重爬")
    ap.add_argument("--verdict", required=True, choices=REVIEW_VERDICTS)
    ap.add_argument("--review", default=DEFAULT_REVIEW)
    ap.add_argument("--server", required=True)
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"))
    ap.add_argument("--engine", default=None,
                    help="順便改佇列；不給則沿用各列原本的 engine")
    ap.add_argument("--go", action="store_true",
                    help="⚠️ 真的寫入（預設只印筆數）")
    args = ap.parse_args()

    items = items_from_review(args.review, args.verdict)
    print(f"verdict={args.verdict}：{len(items)} 筆")
    if not args.go:
        for it in items[:5]:
            print(f"  {it['stock_id']} {it['roc_year']}/{it['roc_month']} {it['date']}")
        print(f"  …（共 {len(items)} 筆）\n[dry-run] 未寫入。要真的打回請加 --go"
              f"（⚠️ 會降級 success，先備份 DB）。")
        return

    total = {"requeued": 0, "mismatch": 0, "not_requeueable": 0, "unknown": 0}
    for i in range(0, len(items), CHUNK):
        batch = items[i:i + CHUNK]
        try:
            applied = post_requeue(args.server, args.token, batch, args.engine)
        except urllib.error.HTTPError as e:
            sys.exit(f"⚠️ HTTP {e.code}：{e.read()[:200]!r}")
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"⚠️ 連不到 server：{e}")
        for k in total:
            total[k] += applied.get(k, 0)
        print(f"  {i + len(batch)}/{len(items)} …{total}")

    print(f"\n打回重爬 {total['requeued']} 筆"
          + (f"（佇列改成 {args.engine}）" if args.engine else "（沿用原佇列）"))
    # ⚠️ mismatch 是「我判斷之後那列被改過了」——判斷不再適用，不動是對的。
    if total["mismatch"]:
        print(f"⚠️ 日期對不上而未處理 {total['mismatch']} 筆")
    if total["not_requeueable"] or total["unknown"]:
        print(f"   狀態不可重排 {total['not_requeueable']} 筆／查無 {total['unknown']} 筆")


if __name__ == "__main__":
    main()

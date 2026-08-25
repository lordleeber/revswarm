#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「已經被第二個獨立來源核對過」的列，蓋上 tasks.verified。

為什麼需要這支：DB 裡的 announce_date 全都是單一來源抓來的——yahoo 抓的就只有 yahoo
說過，google 抓的就只有 google 說過。哪些是「兩個來源各自查、答案一樣」的，過去只存在
於一次性的對照報表裡，報表關掉就沒了。verified 欄位把這件事留在資料裡。

兩個來源，可信度不同：
  --from mops    MOPS 公開資訊觀測站的官方申報日（t05st01）。這是**官方原始文件**，
                 是這個 repo 裡最硬的基準；重疊區約 2,376 筆 / 49 檔。
  --from gemini  gemini 對照實驗的結果（gemini_benchmark.csv）。⚠️ 弱一階：那是模型
                 grounding 查出來的，而且實測 m_src 為 0（全靠模型合成文字，見 README
                 「對照實驗」）。只有「gemini 與 MOPS 都同意 DB 的日期」才蓋。

⚠️ 蓋章的判準一律是「日期真的一致」，不是「有跑過這一筆」——不一致的會被列出來但不蓋，
   那是兩個來源打架的訊號，值得人去看，不該被一個章掩蓋掉。判斷在 server 端做
   （見 Store.verify），這支只負責把候選送過去。

⚠️ 一列只有一個 verified 欄，裝不下「兩個來源都同意」。所以 server 端有強弱排序
   （VERIFIER_RANK：mops > gemini），弱的不會覆寫強的，會記成 kept。兩支的先後順序
   因此不影響最終結果。

為什麼走 server 而不直接開 DB：server 是這個 DB 的唯一寫入者，全靠它的 lock 串行化。
多開一個寫入者就得自己處理鎖競爭，而且 mops/ 底下的工具一向是唯讀的，不該破例。

跑法（repo 根目錄，server 要跑著）：
  python3 -m mops.stamp_verified --from mops   --server http://127.0.0.1:8000 --dry-run
  python3 -m mops.stamp_verified --from mops   --server http://127.0.0.1:8000
  python3 -m mops.stamp_verified --from gemini --server http://127.0.0.1:8000
"""

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request

import revlib
from mops.mops_validate import load_baseline

DEFAULT_BASELINE = "mops_baseline.csv"
DEFAULT_BENCH = "gemini_benchmark.csv"
CHUNK = 500          # 一次送幾筆；分批只是別讓單一請求太肥，server 端本來就是一個交易


def items_from_mops(path):
    """MOPS 官方申報日 → 候選清單。"""
    base = load_baseline(path)
    if not base:
        sys.exit(f"⚠️ 讀不到基準 {path}（先跑 mops 的抓取腳本）。")
    return [{"stock_id": sid, "roc_year": ry, "roc_month": rm,
             "date": r["announce_date"]}
            for (sid, ry, rm), r in sorted(base.items())]


def items_from_gemini(path):
    """
    gemini 對照實驗 → 候選清單。

    ⚠️ 只收「gemini 與 MOPS 兩邊都同意」的列。gemini 單獨同意 DB 不足以蓋章：那只是
    兩個都可能錯的來源湊在一起，而 MOPS 是官方文件。status 也必須是 ok——nosearch /
    error 那幾筆根本沒有結論。
    """
    if not os.path.exists(path):
        sys.exit(f"⚠️ 讀不到對照實驗結果 {path}（先跑 python3 -m mops.gemini_benchmark）。")
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("status") != "ok":
                continue
            g, m = r.get("gemini_date"), r.get("mops_date")
            if not g or g != m:
                continue
            out.append({"stock_id": r["stock_id"],
                        "roc_year": int(r["roc_year"]),
                        "roc_month": int(r["roc_month"]),
                        "date": g})
    return out


def post(server, token, by, items, timeout=60):
    req = urllib.request.Request(
        server.rstrip("/") + "/verify",
        data=json.dumps({"by": by, "items": items}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}" if token else ""})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["applied"]


def main():
    revlib.load_env()
    ap = argparse.ArgumentParser(description="蓋 tasks.verified")
    ap.add_argument("--from", dest="src", required=True, choices=("mops", "gemini"))
    ap.add_argument("--server", required=True, help="server base URL")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"))
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--dry-run", action="store_true", help="只印候選數，不送出")
    args = ap.parse_args()

    items = (items_from_mops(args.baseline) if args.src == "mops"
             else items_from_gemini(args.bench))
    print(f"來源 {args.src}：候選 {len(items)} 筆")
    if args.dry_run:
        for it in items[:5]:
            print(f"  {it['stock_id']} {it['roc_year']}/{it['roc_month']} {it['date']}")
        print("[dry-run] 未送出。")
        return

    total = {"verified": 0, "kept": 0, "mismatch": 0,
             "not_success": 0, "unknown": 0}
    for i in range(0, len(items), CHUNK):
        try:
            applied = post(args.server, args.token, args.src, items[i:i + CHUNK])
        except urllib.error.HTTPError as e:
            sys.exit(f"⚠️ HTTP {e.code}：{e.read()[:200]!r}")
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"⚠️ 連不到 server：{e}")
        for k in total:
            total[k] += applied.get(k, 0)
        print(f"  {i + len(items[i:i + CHUNK])}/{len(items)} …{total}")

    print(f"\n蓋章 {total['verified']} 筆（verified='{args.src}'）")
    # ⚠️ mismatch 才是這支跑完最值得看的數字：那是兩個來源對同一個月份給出不同日期。
    # 蓋章數漂亮但 mismatch 一堆，代表資料有系統性問題，不是「大部分都驗過了」。
    print(f"日期不一致 {total['mismatch']} 筆 ← 兩個來源打架，值得逐筆看")
    if total["kept"]:
        # 一列只有一個 verified 欄，裝不下「兩個來源都同意」。弱來源不覆寫強來源
        # （見 server.VERIFIER_RANK），所以這個數字是「本來就有更硬的章」。
        print(f"保留原有更強的章 {total['kept']} 筆（沒有降級）")
    print(f"尚未定案 {total['not_success']} 筆／DB 裡沒有 {total['unknown']} 筆")


if __name__ == "__main__":
    main()

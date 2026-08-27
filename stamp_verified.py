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
  --from gemini-review
                 Claude 逐筆審 gemini_worker --review-one 交回的證據（data/gemini_review.csv）。
                 ⚠️ 蓋的章是 **claude** 不是 gemini：判斷者讀的是模型自己回的那段文字，
                 沒有引入新證據（循環，見 README「claude 不是驗證」）。
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
  python3 -m stamp_verified --from mops   --server http://127.0.0.1:8000 --dry-run
  python3 -m stamp_verified --from mops   --server http://127.0.0.1:8000
  python3 -m stamp_verified --from gemini --server http://127.0.0.1:8000
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
# ⚠️ 跨套件 import 是刻意的：gemini_review.csv 的表頭只有【一份】定義，由寫的人
# （gemini_worker）宣告、讀的人驗。各抄一份遲早會漂，而漂掉的症狀是整批無聲跳過。
from worker.gemini_worker import REVIEW_FIELDS as GEMINI_REVIEW_FIELDS

DEFAULT_BASELINE = "mops_baseline.csv"
DEFAULT_BENCH = "gemini_benchmark.csv"
DEFAULT_REVIEW = "data/title_review.csv"
DEFAULT_GEMINI_REVIEW = "data/gemini_review.csv"
# --from 的名字通常就是 verified 要蓋的值；gemini-review 是唯一的例外（見下）。
VERIFIER_FOR = {"gemini-review": "claude"}
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


def verifier_for(src):
    """--from 的來源名 → verified 要蓋的值。

    ⚠️ gemini-review 蓋的是 `claude`，不是 `gemini`。那條路的判斷者是 Claude 讀
    gemini 交回的證據（模型自己寫的那段文字＋它讀過的網域），沒有第二個獨立來源
    出現過——蓋成 `gemini` 會讓它在 VERIFIER_RANK 裡爬到 claude 之上，覆蓋掉不該
    覆蓋的章，而且對外宣稱了一個不存在的獨立確認。
    """
    return VERIFIER_FOR.get(src, src)


GEMINI_REVIEW_VERDICTS = ("approve", "reject")


def items_from_gemini_review(path):
    """
    Claude 逐筆審 gemini 證據的判斷 → 候選清單（只取 approve）。

    ⚠️ reject 的列不送、但一定要留在 CSV 裡：那些筆在 DB 裡是 state='failed'、沒有
    announce_date 可核對，可是「被否決的證據長什麼樣」只有這張表記得住（DB 不留、
    模型回應也不留）。這是這張表相對 title_review.csv 多出來的價值，別為了送出方便
    把它們刪掉。

    ⚠️ approve 卻沒有日期 → 直接失敗，不靜默跳過：那是自相矛盾（核准了什麼？），
    多半代表寫檔的那一步出了錯，而靜默跳過看起來跟「這批沒有 approve」一模一樣。

    ⚠️ 同一個月份出現第二列判斷是**這條流程保證會發生的事**，不是資料壞掉：SKILL
    第 1 步在佇列排空時要 `POST /admin/requeue-failed?engine=gemini`，那會把先前
    reject 的每一筆重新排回 gemini 再審一次；而這個檔是 append-only 的
    （gemini_worker._append_review_row 用 "a" 模式、不去重），docstring 上面那條又
    明文不准刪列。所以【以最後一列為準】——後寫的就是後審的。舊行為（重複就
    sys.exit）等於一筆重審就讓整批（含每一個無關的 approve）一列都蓋不到章，
    而且沒有任何出路。
    ⚠️ 覆寫只影響「這次送出哪些候選」；先前已經蓋上的章不會因為後來改判 reject
    就被撤掉（撤章要另外處理）。
    """
    if not os.path.exists(path):
        sys.exit(f"⚠️ 讀不到 {path}"
                 f"（先跑 python3 -m worker.gemini_worker --review-one 並落地判斷）。")
    with open(path, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        if tuple(rd.fieldnames or ()) != GEMINI_REVIEW_FIELDS:
            sys.exit(f"⚠️ {path} 表頭應為 {','.join(GEMINI_REVIEW_FIELDS)}，"
                     f"實得 {','.join(rd.fieldnames or ())}")
        rows = list(rd)

    winners = {}
    for i, r in enumerate(rows, start=2):
        v = (r.get("verdict") or "").strip()
        # ⚠️ 每一列都驗，包含之後會被覆蓋掉的：被覆蓋不等於不必檢查，壞資料仍是壞資料。
        if v not in GEMINI_REVIEW_VERDICTS:
            sys.exit(f"⚠️ 第 {i} 行 verdict='{v}' 不合法；"
                     f"可用：{list(GEMINI_REVIEW_VERDICTS)}")
        if not (r.get("note") or "").strip():
            sys.exit(f"⚠️ 第 {i} 行缺 note。判斷是主觀的，一定要寫下理由。")
        try:
            key = (r["stock_id"], int(r["roc_year"]), int(r["roc_month"]))
        except (TypeError, ValueError, KeyError):
            sys.exit(f"⚠️ 第 {i} 行的 stock_id/roc_year/roc_month 有問題。")
        date = (r.get("announce_date") or "").strip()
        if v == "approve" and not date:
            sys.exit(f"⚠️ 第 {i} 行 approve 但沒有 announce_date——核准了什麼？")
        _supersede(winners, key, i, v, date)
    _report_supersedes(winners, path)
    return [{"stock_id": k[0], "roc_year": k[1], "roc_month": k[2],
             "date": w["date"]}
            for k, w in winners.items() if w["verdict"] == "approve"]


def _supersede(winners, key, line, verdict, date):
    """把這一列記成 key 的現任判斷，順手記下它蓋掉了第幾行（見 _report_supersedes）。"""
    prev = winners.get(key)
    winners[key] = {"line": line, "verdict": verdict, "date": date,
                    "over": (prev["over"] + [prev["line"]]) if prev else []}


def _report_supersedes(winners, path):
    """把「這個月份有多列判斷、以最後一列為準」講出來。

    ⚠️ 一定要印：靜默地只取最後一列，跟「這個檔只有一列」看起來一模一樣，而兩者
    差很多（後者是資料，前者是有人改過判斷）。
    """
    dups = [(k, w) for k, w in winners.items() if w["over"]]
    if not dups:
        return
    n = sum(len(w["over"]) for _, w in dups)
    print(f"⚠️ {path}：{len(dups)} 個月份有多列判斷（共 {n} 列被覆蓋），"
          f"一律以最後一列為準（append-only 檔，後寫的就是後審的）：")
    for k, w in dups[:5]:
        print(f"   {k[0]} {k[1]}/{k[2]}：第 {', '.join(str(x) for x in w['over'])} 行"
              f" → 第 {w['line']} 行（{w['verdict']}）")
    if len(dups) > 5:
        print(f"   …另外 {len(dups) - 5} 個月份")


REVIEW_FIELDS = ("stock_id", "roc_year", "roc_month", "announce_date",
                 "verdict", "note")
REVIEW_VERDICTS = ("claude", "tbd")


def items_from_review(path, verdict):
    """
    人工讀 raw_title 的判斷 → 候選清單（只取 verdict 這一種）。

    ⚠️ claude 不是「第二個獨立來源」。title 正是產生 announce_date 的那段文字
    （revlib.parse 從它附近抽日期），再讀一次同一段字沒有引入新證據——這是循環。
    它能回答的是「這段佐證文字撐不撐得起這個日期」：例如錨點命中的其實是
    「智捷110年5月27日股東常會延後召開」裡的年月、或整段 title 只是 Yahoo 頁面的
    JSON 碎片。當篩選線索用，不要當驗證。tbd = 看過了但不是高信心（與 NULL 的差別
    是「已經有人看過」，避免重複讀）。

    判斷存版控的 CSV 而不是直接寫 DB：mops/gemini 那兩條路隨時可以重跑重現，這條
    不行。留檔才有得稽核「當初為什麼判高信心」，DB 重建後也補得回來。
    CSV 的 announce_date 是讀的當下看到的值，送進 /verify 當樂觀鎖（見模組 docstring）。

    ⚠️ 同一個月份出現第二列判斷是合法的（tbd 是「看過了但沒把握」，那一筆之後可能
    被重讀改判 claude，或反過來），所以【以最後一列為準】——這個檔是 append-only 的。
    理由與 items_from_gemini_review 同一條：舊行為（重複就 sys.exit）會讓一列重複
    就整批停擺，而三萬列的檔案裡那一列不見得找得到。
    """
    if not os.path.exists(path):
        sys.exit(f"⚠️ 讀不到 {path}。")
    with open(path, encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        if tuple(rd.fieldnames or ()) != REVIEW_FIELDS:
            sys.exit(f"⚠️ {path} 表頭應為 {','.join(REVIEW_FIELDS)}，"
                     f"實得 {','.join(rd.fieldnames or ())}")
        rows = list(rd)

    winners = {}
    for i, r in enumerate(rows, start=2):
        v = (r.get("verdict") or "").strip()
        if v not in REVIEW_VERDICTS:
            # ⚠️ 打錯字不可以靜默變成「這個 verdict 沒有任何列」——那會讓整批無聲跳過，
            # 而且看起來跟「真的沒有這種判斷」一模一樣。每一列都驗，包含被覆蓋的。
            sys.exit(f"⚠️ 第 {i} 行 verdict='{v}' 不合法；可用：{list(REVIEW_VERDICTS)}")
        if not (r.get("note") or "").strip():
            # 判斷是主觀的，沒有理由就沒有稽核價值。
            sys.exit(f"⚠️ 第 {i} 行缺 note。判斷是主觀的，一定要寫下理由。")
        try:
            key = (r["stock_id"], int(r["roc_year"]), int(r["roc_month"]))
        except (TypeError, ValueError, KeyError):
            sys.exit(f"⚠️ 第 {i} 行的 stock_id/roc_year/roc_month 有問題。")
        _supersede(winners, key, i, v, (r.get("announce_date") or "").strip())
    _report_supersedes(winners, path)
    return [{"stock_id": k[0], "roc_year": k[1], "roc_month": k[2],
             "date": w["date"]}
            for k, w in winners.items() if w["verdict"] == verdict]


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
    ap.add_argument("--from", dest="src", required=True,
                    choices=("mops", "gemini", "gemini-review") + REVIEW_VERDICTS)
    ap.add_argument("--server", required=True, help="server base URL")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"))
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--bench", default=DEFAULT_BENCH)
    ap.add_argument("--review", default=DEFAULT_REVIEW,
                    help="人工讀 title 的判斷 CSV（--from claude/tbd 用）")
    ap.add_argument("--gemini-review", default=DEFAULT_GEMINI_REVIEW,
                    dest="gemini_review",
                    help="Claude 審 gemini 證據的判斷 CSV（--from gemini-review 用）")
    ap.add_argument("--dry-run", action="store_true", help="只印候選數，不送出")
    args = ap.parse_args()

    by = verifier_for(args.src)          # ⚠️ 來源名 ≠ 蓋的章（見 verifier_for）
    if args.src == "mops":
        items = items_from_mops(args.baseline)
    elif args.src == "gemini":
        items = items_from_gemini(args.bench)
    elif args.src == "gemini-review":
        items = items_from_gemini_review(args.gemini_review)
    else:
        items = items_from_review(args.review, args.src)
    print(f"來源 {args.src}：候選 {len(items)} 筆"
          + (f"（蓋的章是 verified='{by}'）" if by != args.src else ""))
    if args.dry_run:
        for it in items[:5]:
            print(f"  {it['stock_id']} {it['roc_year']}/{it['roc_month']} {it['date']}")
        print("[dry-run] 未送出。")
        return

    total = {"verified": 0, "kept": 0, "mismatch": 0,
             "not_success": 0, "unknown": 0}
    for i in range(0, len(items), CHUNK):
        try:
            applied = post(args.server, args.token, by, items[i:i + CHUNK])
        except urllib.error.HTTPError as e:
            sys.exit(f"⚠️ HTTP {e.code}：{e.read()[:200]!r}")
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"⚠️ 連不到 server：{e}")
        for k in total:
            total[k] += applied.get(k, 0)
        print(f"  {i + len(items[i:i + CHUNK])}/{len(items)} …{total}")

    print(f"\n蓋章 {total['verified']} 筆（verified='{by}'）")
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

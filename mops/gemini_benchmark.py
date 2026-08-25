#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拿 MOPS 官方申報日當黃金基準，量測 gemini_worker 那條路（Gemini grounding）的日期品質。

為什麼是這個實驗、而不是直接拿 gemini 去補 failed（README「放量前先做對照實驗」）：
  補 failed 拿到的日期**沒有任何外部基準可驗**。11.3 已經吃過這個虧——google_worker
  補到的 24,196 筆與 MOPS 零重疊，事後發現日期系統性偏早（占 8%）時，只能靠週末率、
  慣用申報日分布這些間接證據去推論，無法直接算對錯。
  所以先打「已經知道答案」的那批：MOPS 有官方申報日、yahoo 也已經成功的重疊區
  （目前 2,376 筆 / 49 檔），一次試跑就知道這條路值不值得放量。

⚠️ 這個實驗量到的是**樂觀上界**，不是平均水準。MOPS t05st01 只涵蓋約 49 家「自願揭露
   月營收」的公司，那幾乎都是大型股——正是模型最可能單靠記憶就答得出來的一群。
   在這裡拿到的一致率，到冷門股/舊月份（也就是 5,613 筆 failed 的真實組成）只會更差。
   報表最後會把這句再印一次，別讓漂亮的數字被斷章取義。

三個控制項，缺一不可：
  1. **對照組**：同一批任務 yahoo 對 MOPS 的一致率也算一次印出來。沒有對照組的
     「gemini 一致率 92%」是沒有意義的數字——yahoo 在同一批是 99.83%。
  2. **信任分級**：m_src（檢索原文）與 m_txt（模型合成）分開算一致率。若整體數字好看
     但全靠 m_txt 撐，那代表模型是「講」對的不是「查」對的，換到它沒背過的冷門股就會垮。
  3. **偏向**：算有號日差（負=偏早）與週末率。這是 11.3 抓出 google 批問題的那兩把尺，
     用同一把尺量 gemini，結論才可比。

走 Vertex AI（額度算在 Google Cloud 帳單），與 gemini_worker.py 同一道門、同一套解析。

安全性：
  - **只讀** revswarm.db（mode=ro），不 lease、不 report、不寫任何 state。跟線上爬取無關，
    可以在 server 跑著的時候執行。
  - 結果逐筆 append 進 --out CSV，中斷後重跑會跳過已完成的列（**不會重複付費**）。
  - --max-searches 沿用 gemini_worker.Budget，算的是搜尋次數不是任務數。
  - --dry-run 只印抽樣組成與預估花費，一毛不花。

跑法（repo 根目錄）：
  python3 -m mops.gemini_benchmark --dry-run                    # 先看抽到什麼、要花多少
  python3 -m mops.gemini_benchmark -n 200 --project YOUR_PROJ   # 真的跑（預設 200 筆）
  python3 -m mops.gemini_benchmark --report-only                # 只根據既有 CSV 重印報表
"""

import argparse
import csv
import datetime
import os
import random
import re
import sqlite3
import statistics
import sys
import time

import revlib
from mops.mops_validate import load_baseline
from worker import gemini_worker as gw

DEFAULT_DB = "revswarm.db"
DEFAULT_BASELINE = "mops_baseline.csv"
DEFAULT_OUT = "gemini_benchmark.csv"

FIELDS = ["stock_id", "name", "roc_year", "roc_month",
          "mops_date", "yahoo_date", "gemini_date", "gemini_source",
          "status", "searches", "raw_title", "url", "search_queries"]

# ⚠️ 絕不可以用 \b 當左界。Python 的 \w 包含 CJK，所以中文字與數字之間【沒有】
# 詞界——"台塑111年10月" 用 r"\b1[01]\d年" 比對是 False，只有 "台塑 111年10月"
# 這種剛好有空格的才會中。而模型下的中文查詢多半沒空格，於是整份報表會印
# 「含民國年 0.0%／含西元年 0.0%」，剛好把本欄唯一的用途歸零（重跑要再花錢）。
# 改用 (?<!\d) 只擋「數字中間」，例如 2022 不該被當成民國 202 年。
_ROC_YEAR = re.compile(r"(?<!\d)1[01]\d\s*年")
_AD_YEAR = re.compile(r"(?<!\d)20\d{2}\s*年")

# search_queries 用它分隔。⚠️ 不用逗號：查詢字串本身就常含逗號，csv 引號雖然擋得住，
# 但人用 grep/Excel 拆欄時會拆錯。管線符號在中文查詢裡幾乎不會出現。
QUERY_SEP = " | "


def _join_queries(queries):
    """把查詢串成一欄。

    ⚠️ 查詢字串是【模型】產生的，我們控制不到內容。萬一某個查詢自己含有 " | "，
    split 回來就會多出幽靈項目，len(qs) 被灌水、所有百分比跟著歪掉。
    join 時先把 | 換掉，讓 split 保持可逆。
    """
    return QUERY_SEP.join(q.replace("|", "／") for q in queries)


# --- 抽樣 -------------------------------------------------------------------
def load_overlap(db, baseline_path):
    """
    回傳重疊區的列：MOPS 有官方申報日、且 revswarm 已經 success 的 (股票,月份)。

    只收 state='success' 且 announce_date 非空的——failed/prelisting 沒有 yahoo 日期
    可當對照組，混進來會讓「對照組一致率」失去意義。
    """
    base = load_baseline(baseline_path)
    if not base:
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = []
    for sid, name, ry, rm, date, engine in conn.execute(
            "SELECT stock_id,name,roc_year,roc_month,announce_date,engine"
            " FROM tasks WHERE state='success' AND announce_date IS NOT NULL"):
        b = base.get((sid, ry, rm))
        if not b:
            continue
        rows.append({"stock_id": sid, "name": name, "roc_year": ry, "roc_month": rm,
                     "mops_date": b["announce_date"], "yahoo_date": date,
                     "engine": engine})
    conn.close()
    return rows


def stratified_sample(rows, n, seed):
    """
    跨股票輪抽，讓 n 筆盡量平均分佈在 49 檔上。

    ⚠️ 不用純隨機抽：重疊區各檔的筆數差很多（有的 73 個月全有、有的只有幾個月），
    純隨機會讓樣本被少數幾檔灌爆，屆時量到的其實是「gemini 對某一家的熟悉度」。
    輪抽同時也順帶把年份攤開（各檔內先洗牌再依序取）。
    """
    rng = random.Random(seed)
    by_stock = {}
    for r in rows:
        by_stock.setdefault(r["stock_id"], []).append(r)
    for lst in by_stock.values():
        rng.shuffle(lst)
    order = sorted(by_stock)          # 先定序再洗，確保同 seed 完全可重現
    rng.shuffle(order)
    out = []
    depth = 0
    while len(out) < n:
        added = False
        for sid in order:
            lst = by_stock[sid]
            if depth < len(lst):
                out.append(lst[depth])
                added = True
                if len(out) >= n:
                    break
        if not added:                 # 全部抽光了還不夠 n
            break
        depth += 1
    return out


# --- 逐筆量測 ---------------------------------------------------------------
def measure_one(row, backend, model, budget, retries=2, retry_sleep=8.0):
    """
    對單筆任務打一次 gemini，回傳補上量測欄位的 dict。

    status 的語意跟 worker 一致，而且**分得比 worker 更細**——這裡不需要「放回佇列」，
    需要的是「這筆到底算不算數」：
      ok        模型搜了、正常回話了（不論有沒有抽到日期）→ 進統計
      nosearch  模型沒發出任何搜尋（憑記憶回答）         → ⚠️ 不進統計，也不算 miss
      error     429/5xx/連線問題，重試完仍失敗           → 不進統計
      fatal     金鑰/權限錯                              → 上層立刻中止

    ⚠️ nosearch 與 error 絕不可以併進「gemini 沒找到」：那會把「沒查成功」算成
    「查了但沒有」，直接灌水命中率的分母。這是 worker 那條戒律在報表端的對應。
    """
    out = dict(row, gemini_date="", gemini_source="", searches=0, raw_title="",
               search_queries="")
    prompt = gw.build_prompt(row["stock_id"], row["name"],
                             row["roc_year"], row["roc_month"])
    payload = err = None
    for attempt in range(retries + 1):
        budget.attempt()          # ⚠️ 重試也算一次呼叫（見 gw.Budget.attempt）
        payload, err = gw.call_gemini(prompt, backend, model=model)
        if err != gw.ERR_RETRY:
            break
        if attempt < retries:
            time.sleep(retry_sleep * (attempt + 1))
    if err == gw.ERR_FATAL:
        out["status"] = "fatal"
        return out
    if err == gw.ERR_NOSEARCH:
        out["status"] = "nosearch"
        return out
    if err is not None:
        out["status"] = "error"
        return out

    budget.spend(len(payload["searches"]))
    out["searches"] = len(payload["searches"])
    # ⚠️ 存原文，不只存次數。查詢字串是我們【控制不到】的東西——grounding 由模型自己
    # 決定下什麼、下幾次（不像 yahoo/google worker 是我們自己組查詢）。一致率不好時，
    # 只有這欄能分辨「模型下的查詢本身就爛」還是「查對了但抽錯日期」，也是唯一能驗證
    # 「它到底有沒有用民國年／西元年兩種」的證據。跑完才想加就得重花一次錢。
    out["search_queries"] = _join_queries(payload["searches"])
    out["status"] = "ok"
    hit = gw.extract(payload, row["name"], row["roc_year"], row["roc_month"])
    if hit:
        # url 是模型自報的出處。差 1 天那種個案要回頭看原文時，只有這欄能直接點開
        # ——沒有它就得拿標題再搜一次，而搜尋結果隨時在變（見 revlib「來源網址」）。
        (out["gemini_date"], out["gemini_source"],
         out["raw_title"], out["url"]) = hit
    return out


# --- 結果快取（可續跑，不重複付費）------------------------------------------
def _key(r):
    return (r["stock_id"], int(r["roc_year"]), int(r["roc_month"]))


def load_done(path):
    """
    讀既有結果。⚠️ 只有 status='ok' 才算完成：nosearch/error 那幾筆下次要重打
    （它們沒有結論，留著只會在報表裡當空洞），fatal 更是設定沒修好前不該算數。
    """
    done = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("status") == "ok":
                done[_key(r)] = r
    return done


def append_row(path, row):
    """
    逐筆 append，中斷也不會掉資料。

    ⚠️ 開檔前先驗表頭。表頭只在「檔案不存在」時才寫，所以 FIELDS 一旦加欄，
    舊的 CSV 就會變成「N+1 個值塞進 N 欄的表頭」——DictReader 會把多出來的值
    默默丟進 restkey(None) 而不報錯，等於花了錢卻讀不回來。
    欄位對不上就直接停，讓人決定要改名保留還是刪掉重跑。
    """
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, encoding="utf-8-sig", newline="") as f:
            header = next(csv.reader(f), [])
        if header != FIELDS:
            raise SystemExit(
                f"⚠️ {path} 的欄位與現行版本不符，停止以免寫出讀不回來的資料。\n"
                f"   檔案：{header}\n   現行：{FIELDS}\n"
                f"   把舊檔改名保留（之後可用 --report-only 讀）或刪掉重跑。")
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


# --- 報表 -------------------------------------------------------------------
def _d(s):
    return datetime.date.fromisoformat(s) if s else None


def _pct(a, b):
    return f"{a / b * 100:.1f}%" if b else "n/a"


def _bias(deltas):
    """有號日差的分布摘要。負 = 比官方申報日早（11.3 抓 google 批問題的那把尺）。"""
    if not deltas:
        return "  （無可比對的日期）"
    early = sum(1 for d in deltas if d < 0)
    late = sum(1 for d in deltas if d > 0)
    exact = sum(1 for d in deltas if d == 0)
    return (f"  中位數 {statistics.median(deltas):+.1f} 天／"
            f"平均 {statistics.fmean(deltas):+.2f} 天\n"
            f"  偏早 {early}（{_pct(early, len(deltas))}）、"
            f"準確 {exact}（{_pct(exact, len(deltas))}）、"
            f"偏晚 {late}（{_pct(late, len(deltas))}）")


def _weekend_rate(dates):
    ds = [_d(x) for x in dates if x]
    if not ds:
        return "n/a"
    return _pct(sum(1 for d in ds if d.weekday() >= 5), len(ds))


def report(records, budget=None):
    """把 CSV 的列變成一份能直接下判斷的報表。"""
    ok = [r for r in records if r.get("status") == "ok"]
    skipped = [r for r in records if r.get("status") != "ok"]
    print("\n" + "=" * 72)
    print(f"樣本 {len(records)} 筆／有效 {len(ok)} 筆"
          f"／{len(set(r['stock_id'] for r in records))} 檔股票")
    if skipped:
        from collections import Counter
        c = Counter(r.get("status") for r in skipped)
        print(f"  ⚠️ 排除 {len(skipped)} 筆未取得有效回應：{dict(c)}"
              f"（不計入分母——沒查成功不等於查不到）")
    if not ok:
        print("沒有有效樣本，無法出結論。")
        return

    hits = [r for r in ok if r["gemini_date"]]
    print(f"\n【召回】gemini 抽到窗內日期：{len(hits)}/{len(ok)} = {_pct(len(hits), len(ok))}")

    # --- 準確率：對照組是同一批的 yahoo，不是憑空的期待值 ---
    g_agree = sum(1 for r in hits if r["gemini_date"] == r["mops_date"])
    y_ok = [r for r in ok if r["yahoo_date"]]
    y_agree = sum(1 for r in y_ok if r["yahoo_date"] == r["mops_date"])
    print(f"\n【準確率 vs MOPS 官方申報日】")
    print(f"  gemini（在命中的 {len(hits)} 筆裡）  {g_agree:>4}  {_pct(g_agree, len(hits))}")
    print(f"  yahoo （對照組，同一批 {len(y_ok)} 筆）{y_agree:>4}  {_pct(y_agree, len(y_ok))}")

    # --- 信任分級：好看的數字是「查」來的還是「講」來的 ---
    print(f"\n【信任分級】m_src=檢索原文／m_txt=模型合成")
    for src in (gw.SRC_CHUNK, gw.SRC_TEXT):
        sub = [r for r in hits if r["gemini_source"] == src]
        if not sub:
            print(f"  {src}  0 筆")
            continue
        a = sum(1 for r in sub if r["gemini_date"] == r["mops_date"])
        print(f"  {src}  {len(sub):>4} 筆（占命中 {_pct(len(sub), len(hits))}）"
              f"，一致 {_pct(a, len(sub))}")

    # --- 偏向：11.3 用來抓 google 批問題的兩把尺 ---
    deltas = [(_d(r["gemini_date"]) - _d(r["mops_date"])).days for r in hits]
    print(f"\n【偏向】gemini 日期 − MOPS 官方日（負 = 偏早）")
    print(_bias(deltas))
    print(f"  週末率  gemini {_weekend_rate([r['gemini_date'] for r in hits])}"
          f"／yahoo {_weekend_rate([r['yahoo_date'] for r in y_ok])}"
          f"（官方申報日不會落假日，偏高就是抓錯日期的訊號）")

    # --- 錯誤樣本，人工看得到才修得動 ---
    wrong = [r for r in hits if r["gemini_date"] != r["mops_date"]]
    _report_queries(ok)

    if wrong:
        print(f"\n【不一致清單】前 15 筆（共 {len(wrong)}）")
        for r in wrong[:15]:
            d = (_d(r["gemini_date"]) - _d(r["mops_date"])).days
            print(f"  {r['stock_id']} {r['name']:<6} {r['roc_year']}/{r['roc_month']:<2}"
                  f"  MOPS {r['mops_date']}  gemini {r['gemini_date']} ({d:+d}d)"
                  f"  [{r['gemini_source']}]  {r['raw_title'][:40]}")
            # ⚠️ 把模型實際下的查詢一起印出來：判斷「查錯」還是「抽錯」全靠這個。
            qs = (r.get("search_queries") or "").split(QUERY_SEP)
            for q in [q for q in qs if q][:4]:
                print(f"        ↳ {q}")

    if budget:
        print(f"\n【花費】{budget.note()}")
    print("\n" + "-" * 72)
    print("⚠️ 解讀提醒：MOPS 只涵蓋約 49 家自願揭露的公司，幾乎都是大型股——正是模型最")
    print("   可能單靠記憶就答得出來的一群。這裡的數字是**樂觀上界**；真正要補的 5,613")
    print("   筆 failed 是冷門股與舊月份，表現只會更差。")
    print("   要放量的門檻建議：整體一致率 >= yahoo 對照組，且 m_txt 那層單獨也站得住。")
    print("=" * 72)


def _report_queries(ok):
    """模型實際下了什麼查詢的統計。

    這是本 worker 與 yahoo/google worker 最根本的差別：查詢字串不是我們組的。
    「絕不加『營收』二字」那條實測教訓在這裡管不到，只能事後量。
    """
    withq = [r for r in ok if (r.get("search_queries") or "").strip()]
    if not withq:
        return
    counts = [int(r["searches"]) for r in ok if str(r.get("searches", "")).isdigit()]
    print("\n【模型實際下的搜尋查詢】⚠️ 這是模型自己決定的，我們控制不到")
    if counts:
        print(f"  每筆查詢次數  中位數 {statistics.median(counts):.0f}／"
              f"平均 {statistics.fmean(counts):.1f}／最多 {max(counts)}"
              f"（計費按這個，不是按任務數）")
    qs = [q for r in withq for q in (r["search_queries"] or "").split(QUERY_SEP) if q]
    roc = sum(1 for q in qs if _ROC_YEAR.search(q))
    ad = sum(1 for q in qs if _AD_YEAR.search(q))
    rev = sum(1 for q in qs if "營收" in q)
    print(f"  共 {len(qs)} 個查詢：含民國年 {_pct(roc, len(qs))}、"
          f"含西元年 {_pct(ad, len(qs))}、含「營收」二字 {_pct(rev, len(qs))}")
    print("  （「營收」佔比高值得注意：yahoo/google 實測那兩個字會害 recall，"
          "但這裡我們擋不掉）")


def read_csv(path):
    """
    讀結果 CSV，並依 (股票,年,月) 去重、只保留最後一列。

    ⚠️ 去重不可省：load_done() 只認 status='ok'，所以 nosearch/error 的列下次會被重試、
    再 append 一次。report() 是逐列計數的，同一筆任務就會被算兩次——樣本 N 與
    「排除 N 筆」會隨每次續跑往上飄（ok 的統計本身沒錯，但標題數字失真）。
    取最後一列＝取該筆最新的結果。
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    latest = {}
    for r in rows:
        latest[_key(r)] = r          # 後面的覆蓋前面的
    return list(latest.values())



# --- main -------------------------------------------------------------------
def main():
    revlib.load_env()
    ap = argparse.ArgumentParser(
        description="用 MOPS 官方申報日量測 Gemini grounding 的日期品質")
    ap.add_argument("-n", "--sample", type=int, default=200,
                    help="抽幾筆（預設 200；跨 49 檔輪抽）")
    ap.add_argument("--seed", type=int, default=20260824,
                    help="抽樣種子，固定才重現得了")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--out", default=DEFAULT_OUT, help="結果 CSV（可續跑）")
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    help="GCP 專案 ID（額度算在它的 Google Cloud 帳單上）；"
                         "預設讀 .env / GOOGLE_CLOUD_PROJECT")
    ap.add_argument("--location", default=gw.VERTEX_LOCATION)
    ap.add_argument("--gcloud", default=None)
    ap.add_argument("--model", default=gw.DEFAULT_MODEL)
    # ⚠️ 實測一個任務會發 4~11 次搜尋、平均約 7（2026-08-24 於 Vertex 量測），
    # 所以 200 筆抽樣大約要 1,400 次搜尋。1500 是照這個算出來的，不是隨手填的。
    # 實測每筆 4~11 次搜尋，200 筆的高標是 2,200——上限取 2500 才不會在正常情況下
    # 提前收工（提前停不會壞資料、可續跑，但會讓報表樣本不足而誤導）。
    ap.add_argument("--max-searches", type=int, default=2500,
                    help="搜尋次數上限（⚠️ 不是任務數；一個任務 4~11 次）。0=不限")
    ap.add_argument("--max-calls", type=int, default=0,
                    help="最多呼叫 API 幾次就停（0=不限）。預設不限是因為本腳本的呼叫數"
                         "本來就被 -n 綁死；worker 那邊才需要它擋無限迴圈")
    ap.add_argument("--free-quota", type=int, default=0,
                    help="⚠️ 預設 0：Vertex 沒有免費 grounding 額度")
    ap.add_argument("--unit-price", type=float, default=gw.DEFAULT_UNIT_PRICE,
                    help="每次搜尋的單價美元；請以實際帳單校正")
    ap.add_argument("--delay", type=float, default=1.0, help="每筆之間的延遲秒")
    ap.add_argument("--retries", type=int, default=2,
                    help="429/5xx 的重試次數（benchmark 要樣本完整，比 worker 多試幾次）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只印抽樣組成與預估花費，不呼叫 API")
    ap.add_argument("--report-only", action="store_true",
                    help="不呼叫 API，只根據既有 --out CSV 重印報表")
    args = ap.parse_args()

    if args.report_only:
        rows = read_csv(args.out)
        if not rows:
            print(f"⚠️ {args.out} 不存在或是空的。", file=sys.stderr)
            sys.exit(1)
        report(rows)
        return

    pool = load_overlap(args.db, args.baseline)
    if not pool:
        print("⚠️ 重疊區是空的：確認 mops_baseline.csv 存在、且 DB 有 success 的重疊月份。"
              "（建基準：python3 -m mops.mops_validate --codes-file data/active_stocks.txt）",
              file=sys.stderr)
        sys.exit(1)
    sample = stratified_sample(pool, args.sample, args.seed)
    stocks = sorted(set(r["stock_id"] for r in sample))
    years = sorted(set(r["roc_year"] for r in sample))
    print(f"重疊區共 {len(pool)} 筆／{len(set(r['stock_id'] for r in pool))} 檔；"
          f"抽出 {len(sample)} 筆／{len(stocks)} 檔／民國 {years[0]}~{years[-1]} 年")

    done = load_done(args.out)
    todo = [r for r in sample if _key(r) not in done]
    if done:
        print(f"  {args.out} 已有 {len(done)} 筆有效結果，本次只需再打 {len(todo)} 筆"
              f"（續跑不重複付費）")

    if args.dry_run:
        # ⚠️ 實測一筆任務發 4~11 次搜尋（2026-08-24 於 Vertex 量測 5 筆：4/4/8/8/11），
        # 不是直覺的 1 次。用實測區間估，別用任務數。
        lo, hi = len(todo) * 4, len(todo) * 11
        free, price = args.free_quota, args.unit_price
        print(f"\n[dry-run] Vertex AI（額度走 Google Cloud 帳單）")
        print(f"          預計呼叫 {len(todo)} 次，發出約 {lo}~{hi} 次搜尋。")
        billable_lo, billable_hi = max(0, lo - free), max(0, hi - free)
        print(f"          約 ${billable_lo * price:.2f}~${billable_hi * price:.2f}"
              f"（單價 ${price}/次；⚠️ Vertex 沒有免費 grounding 額度，"
              f"實際單價請以帳單校正 --unit-price）")
        for r in sample[:10]:
            print(f"          {r['stock_id']} {r['name']} "
                  f"{r['roc_year']}/{r['roc_month']}  MOPS {r['mops_date']}")
        if len(sample) > 10:
            print(f"          …其餘 {len(sample) - 10} 筆")
        return

    backend = gw.make_backend(args)
    print(f"  後端：{backend.describe()}  model={args.model}")

    budget = gw.Budget(args.max_searches, args.free_quota, args.unit_price,
                       args.max_calls)
    for i, row in enumerate(todo, 1):
        if budget.exhausted():
            print(f"\n已達上限（{budget.note()}），停止。"
                  f"已完成的結果都在 {args.out}，調高 --max-searches 後重跑會續打。")
            break
        rec = measure_one(row, backend, args.model, budget,
                          retries=args.retries)
        append_row(args.out, rec)
        if rec["status"] == "fatal":
            print("⚠️ 金鑰／權限錯誤，中止。請照 README 檢查 Console 設定。", file=sys.stderr)
            break
        mark = "·"
        if rec["gemini_date"]:
            mark = "✓" if rec["gemini_date"] == rec["mops_date"] else "✗"
        print(f"  [{i}/{len(todo)}] {mark} {row['stock_id']} {row['name']} "
              f"{row['roc_year']}/{row['roc_month']}  MOPS {row['mops_date']}  "
              f"gemini {rec['gemini_date'] or '—'} [{rec['gemini_source'] or rec['status']}]")
        time.sleep(args.delay)

    report(read_csv(args.out), budget)


if __name__ == "__main__":
    main()

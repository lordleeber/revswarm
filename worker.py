#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm worker：可在任意機器啟動，向 server 租任務、爬 Yahoo 搜尋、回報結果。

爬取配方（todo.txt 第3節）：
  對單一 (股票,月份)：
    1. 依序試查詢字串（中一個就停）：
         q_roc = "{name} {roc_year}年{roc_month}月"        # 民國年 → 常釣到 MoneyDJ
         q_ad  = "{name} {roc_year+1911}年{roc_month}月"    # 西元年 → 常釣到 Yahoo【公告】
       ⚠️ 絕不在查詢後加「營收」二字（會害 recall 掉一半，todo 2.3a）。
    2. curl 一定加 --http1.1（否則 HTTP/2 在某些環境 SSL EOF、回 000）。
    3. 分類：curl 非0 / http!=200 / 頁面 <2000B → RATE_LIMITED（退避重試，不算 failed）。
    4. 解析：窗過濾（次月1~15）+ 精確名稱錨點（見 revlib.parse）。
    5. 只有「兩種年份都試過、頁面正常、仍無窗內日期」才回 failed。
    6. 每次查詢間 delay+jitter；偵測連續 rate_limited → 指數退避（多半是整個 IP 被擋）。

部署：把 worker.py 與 revlib.py 複製到任一台機器即可跑：
  python3 worker.py --server http://SERVER:8000 --token SECRET
"""

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import revlib

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MIN_PAGE_BYTES = 2000        # 小於此視為被限流/擋（todo 2.6）
# 讓 curl 走 proxy（例 socks5h://127.0.0.1:1080，經 SSH SOCKS 從別的 IP 出去繞 per-IP
# rate limit）。只影響爬 Yahoo 的 curl；worker↔server 的 urllib 不受影響、仍走原路。
PROXY = os.environ.get("WORKER_PROXY") or None


# --- 抓取 -------------------------------------------------------------------
def fetch(query, timeout=25):
    """
    curl --http1.1 抓 Yahoo 搜尋頁。回傳 (html, ok)。
    ok=False = RATE_LIMITED 訊號（curl 失敗 / 非200 / 頁面過小）。
    """
    cmd = ["curl", "-sS", "--http1.1", "-m", str(timeout), "-G",
           "-w", "\n%{http_code}",            # 末行附上 HTTP 狀態碼
           "https://tw.search.yahoo.com/search",
           "--data-urlencode", f"p={query}",
           "-A", UA,
           "-H", "Accept-Language: zh-TW,zh;q=0.9"]
    if PROXY:
        cmd += ["--proxy", PROXY]              # socks5h:// → DNS 也在 proxy 端解
    try:
        r = subprocess.run(
            cmd,
            # Yahoo 頁是 UTF-8：務必明確指定，否則 Windows 會用系統 locale
            # (如 cp950) 解碼 → UnicodeDecodeError、stdout 變 None。errors=replace
            # 讓少數壞位元組不致中斷解析。
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout + 8,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, False
    if r.returncode != 0 or r.stdout is None:
        return None, False
    out = r.stdout
    # 拆出最後一行的 http_code
    nl = out.rfind("\n")
    code = out[nl + 1:].strip() if nl >= 0 else ""
    html = out[:nl] if nl >= 0 else out
    if code != "200":
        return None, False
    if not html or len(html) < MIN_PAGE_BYTES:
        return None, False
    return html, True


def crawl_task(task, per_query_sleep):
    """
    回傳結果 dict：{id, status, date?, source?, title?}
      status ∈ success | failed | rate_limited
    """
    name = task["name"]
    ry, rm = task["roc_year"], task["roc_month"]
    queries = [
        ("q_roc", f"{name} {ry}年{rm}月"),
        ("q_ad", f"{name} {ry + 1911}年{rm}月"),
    ]
    saw_rate_limited = False
    tried_ok = False
    for i, (variant, q) in enumerate(queries):
        if i > 0:
            time.sleep(per_query_sleep + random.uniform(0, 1.0))
        html, ok = fetch(q)
        if not ok:
            saw_rate_limited = True
            continue
        tried_ok = True
        hit = revlib.parse(html, name, ry, rm)
        if hit:
            date, title = hit
            return {"id": task["id"], "status": "success",
                    "date": date, "source": variant, "title": title}
    # 走到這：兩種查詢都沒中窗內日期。
    if saw_rate_limited or not tried_ok:
        # 有任一查詢被限流（可能正好漏掉命中）→ 保守放回重試，不判 failed。
        return {"id": task["id"], "status": "rate_limited"}
    # 兩種年份都試過、頁面都正常、仍無窗內日期 → 真的找不到。
    return {"id": task["id"], "status": "failed"}


# --- 與 server 溝通 ---------------------------------------------------------
class Client:
    def __init__(self, base, token, worker_id):
        self.base = base.rstrip("/")
        self.token = token
        self.worker_id = worker_id

    def _req(self, method, path, body=None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=40) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def lease(self, n):
        return self._req("POST", f"/lease?n={n}&worker={self.worker_id}")

    def report(self, results):
        return self._req("POST", "/result",
                         {"worker": self.worker_id, "results": results})


# --- 主迴圈 -----------------------------------------------------------------
def run(args):
    worker_id = args.worker_id or f"{socket.gethostname()}-{os.getpid()}"
    client = Client(args.server, args.token, worker_id)
    print(f"worker {worker_id} → {args.server}  batch={args.batch} "
          f"delay={args.delay}s rl_threshold={args.rl_threshold}")

    batches_done = 0
    while True:
        # --- 租一批 ---
        try:
            resp = client.lease(args.batch)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  lease 失敗，10s 後重試：{e!r}")
            time.sleep(10)
            continue
        batch = resp.get("tasks", [])
        if not batch:
            print(f"  佇列已空/無可租任務，{args.idle_sleep}s 後再試。")
            time.sleep(args.idle_sleep)
            continue

        # --- 逐筆爬 ---
        results = []
        consec_rl = 0
        blocked = False
        for idx, task in enumerate(batch):
            r = crawl_task(task, args.per_query_sleep)
            results.append(r)
            tag = {"success": "✓", "failed": "—", "rate_limited": "×"}[r["status"]]
            extra = f" {r.get('date','')} [{r.get('source','')}]" if r["status"] == "success" else ""
            print(f"  {tag} {task['stock_id']} {task['name']} "
                  f"{task['roc_year']}/{task['roc_month']}{extra}")

            if r["status"] == "rate_limited":
                consec_rl += 1
                # 指數退避（上限 60s）+ 抖動。連續多筆多半是整個 IP 被擋。
                back = min(2 ** consec_rl, 60) + random.uniform(0, 2)
                time.sleep(back)
                if consec_rl >= args.rl_threshold:
                    print(f"  ⚠️ 連續 {consec_rl} 筆 rate_limited，"
                          f"研判本機 IP 被擋，提早結束本批並長睡 {args.block_sleep}s。")
                    blocked = True
                    break
            else:
                consec_rl = 0
                time.sleep(args.delay + random.uniform(0, args.jitter))

        # 提早中止時，剩餘未爬的租約以 rate_limited 立刻放回（免等租約逾時）。
        if blocked:
            done_ids = {r["id"] for r in results}
            for task in batch:
                if task["id"] not in done_ids:
                    results.append({"id": task["id"], "status": "rate_limited"})

        # --- 回報 ---
        try:
            applied = client.report(results)
            print(f"  回報：{applied.get('applied')}")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  回報失敗（租約逾時後 server 會自動回收）：{e!r}")

        batches_done += 1
        if args.once or (args.max_batches and batches_done >= args.max_batches):
            print(f"完成 {batches_done} 批，結束。")
            return
        if blocked:
            time.sleep(args.block_sleep)


def main():
    revlib.load_env()          # 先載 .env，讓 REVSWARM_TOKEN 免手打
    ap = argparse.ArgumentParser(description="revswarm worker")
    ap.add_argument("--server", required=True, help="server base URL, 如 http://1.2.3.4:8000")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"),
                    help="Bearer token；預設讀 .env / 環境變數 REVSWARM_TOKEN")
    ap.add_argument("--worker-id", default=None, help="預設 hostname-pid")
    ap.add_argument("--batch", type=int, default=30, help="每次租多少筆")
    ap.add_argument("--delay", type=float, default=3.0, help="每筆任務間基礎延遲秒")
    ap.add_argument("--jitter", type=float, default=2.0, help="每筆延遲的隨機抖動上限秒")
    ap.add_argument("--per-query-sleep", type=float, default=1.5,
                    help="同一任務內兩次查詢間的延遲秒")
    ap.add_argument("--rl-threshold", type=int, default=5,
                    help="連續幾筆 rate_limited 判定本機 IP 被擋")
    ap.add_argument("--block-sleep", type=float, default=600.0,
                    help="判定被擋後長睡秒數")
    ap.add_argument("--idle-sleep", type=float, default=30.0,
                    help="佇列空時的等待秒數")
    ap.add_argument("--once", action="store_true", help="只跑一批就結束（測試用）")
    ap.add_argument("--max-batches", type=int, default=0, help="跑幾批後結束(0=不限)")
    ap.add_argument("--proxy", default=os.environ.get("WORKER_PROXY"),
                    help="爬 Yahoo 的 curl 走此 proxy，如 socks5h://127.0.0.1:1080"
                         "（繞 per-IP rate limit）；預設讀環境變數 WORKER_PROXY")
    args = ap.parse_args()

    global PROXY
    PROXY = args.proxy or None

    try:
        run(args)
    except KeyboardInterrupt:
        print("\n收到中斷，結束。")


if __name__ == "__main__":
    main()

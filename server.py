#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm server：分散式工作佇列（FastAPI 概念，但用標準庫 http.server 實作，零依賴）。

state machine:  undone → dispatched → (success | failed)
  - dispatched 逾 LEASE_TTL 秒沒回覆 → 派工時「惰性回收」自動視為 undone
  - failed = 真的找不到（worker 民國+西元兩種年份都試過）；不再重試
  - rate_limited ≠ failed：立刻放回 undone，不計失敗

endpoints（都需 header  Authorization: Bearer <token>，除 /stats 之外可設）：
  POST /lease?n=30&worker=<id>       原子租一批任務
  POST /result   {results:[{id,status,date?,source?,title?}, ...]}  批次回報
  GET  /stats                        進度、各 state 計數、近況
  GET  /healthz                      存活探針（免 token）

用法：
  python3 server.py --db revswarm.db --host 0.0.0.0 --port 8000 --token SECRET
若不給 --token 則從環境變數 REVSWARM_TOKEN 讀；兩者皆無則不驗證(僅限本機測試)。
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import revlib

LEASE_TTL = 600          # 秒；dispatched 超過此值未回覆即可被重派
MAX_LEASE = 200          # 單次 lease 上限，避免一隻 worker 掃光佇列

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id            INTEGER PRIMARY KEY,
  stock_id      TEXT NOT NULL,
  name          TEXT NOT NULL,
  roc_year      INTEGER NOT NULL,
  roc_month     INTEGER NOT NULL,
  state         TEXT NOT NULL DEFAULT 'undone',
  announce_date TEXT,
  source        TEXT,
  raw_title     TEXT,
  attempts      INTEGER DEFAULT 0,
  fail_count    INTEGER DEFAULT 0,
  dispatched_at INTEGER,
  worker_id     TEXT,
  updated_at    INTEGER,
  UNIQUE(stock_id, roc_year, roc_month)
);
CREATE INDEX IF NOT EXISTS idx_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_dispatched ON tasks(state, dispatched_at);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class Store:
    """
    封裝 SQLite 存取。ThreadingHTTPServer 會多執行緒併發呼叫，而這裡共用單一
    sqlite 連線，故用一把行程內大鎖把每個 DB 操作序列化：SQLite 本來就只允許一個
    writer，序列化不損吞吐，卻能徹底避免「共用連線上兩個 BEGIN IMMEDIATE 疊在
    一起 → cannot start a transaction within a transaction」。派工的原子性由
    BEGIN IMMEDIATE + 這把鎖共同保證，兩隻 worker 絕不會拿到同一筆。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = connect(db_path)
        self._lock = threading.Lock()

    # --- 派工：原子鎖定 + 惰性回收 ---------------------------------------
    def lease(self, n, worker_id):
        n = max(1, min(int(n), MAX_LEASE))
        now = int(time.time())
        cutoff = now - LEASE_TTL
        conn = self.conn
        # 大鎖 + BEGIN IMMEDIATE：立刻取寫鎖，兩隻 worker 不會拿到同一批。
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                rows = conn.execute(
                    """
                    SELECT id FROM tasks
                     WHERE state='undone'
                        OR (state='dispatched' AND (dispatched_at IS NULL OR dispatched_at < ?))
                     ORDER BY state='dispatched' DESC, id      -- 逾時的優先撿回
                     LIMIT ?
                    """,
                    (cutoff, n),
                ).fetchall()
                ids = [r["id"] for r in rows]
                if ids:
                    qmarks = ",".join("?" * len(ids))
                    conn.execute(
                        f"""UPDATE tasks
                              SET state='dispatched', dispatched_at=?, worker_id=?,
                                  attempts=attempts+1, updated_at=?
                            WHERE id IN ({qmarks})""",
                        (now, worker_id, now, *ids),
                    )
                    batch = conn.execute(
                        f"""SELECT id, stock_id, name, roc_year, roc_month
                              FROM tasks WHERE id IN ({qmarks})""",
                        ids,
                    ).fetchall()
                else:
                    batch = []
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return [dict(r) for r in batch]

    # --- 回報：success 需窗再驗證；rate_limited 放回；failed 記數 --------
    def report(self, worker_id, results):
        now = int(time.time())
        counts = {"success": 0, "failed": 0, "rate_limited": 0,
                  "rejected": 0, "ignored": 0, "unknown": 0}
        conn = self.conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for item in results:
                    tid = item.get("id")
                    status = item.get("status")
                    if tid is None or status not in ("success", "failed", "rate_limited"):
                        counts["unknown"] += 1
                        continue
                    row = conn.execute(
                        "SELECT state, roc_year, roc_month FROM tasks WHERE id=?", (tid,)
                    ).fetchone()
                    if row is None:
                        counts["unknown"] += 1
                        continue
                    # 已 success 的一律不降級：忽略逾時遲到 / 重複回報（idempotent）。
                    if row["state"] == "success":
                        counts["ignored"] += 1
                        continue

                    if status == "rate_limited":
                        # 立刻 release 租約，放回 undone，不計失敗。
                        conn.execute(
                            "UPDATE tasks SET state='undone', dispatched_at=NULL,"
                            " worker_id=NULL, updated_at=? WHERE id=?",
                            (now, tid),
                        )
                        counts["rate_limited"] += 1

                    elif status == "failed":
                        conn.execute(
                            "UPDATE tasks SET state='failed', fail_count=fail_count+1,"
                            " worker_id=?, dispatched_at=NULL, updated_at=? WHERE id=?",
                            (worker_id, now, tid),
                        )
                        counts["failed"] += 1

                    else:  # success：server 端用同一套窗再驗一次（todo.txt 5.5）
                        valid = revlib.validate_date(
                            item.get("date"), row["roc_year"], row["roc_month"])
                        if valid is None:
                            # 日期不在窗內 → 不信任，退回 undone 重做（不當 success）。
                            conn.execute(
                                "UPDATE tasks SET state='undone', dispatched_at=NULL,"
                                " worker_id=NULL, updated_at=? WHERE id=?",
                                (now, tid),
                            )
                            counts["rejected"] += 1
                        else:
                            conn.execute(
                                "UPDATE tasks SET state='success', announce_date=?,"
                                " source=?, raw_title=?, worker_id=?, dispatched_at=NULL,"
                                " updated_at=? WHERE id=?",
                                (valid, item.get("source"), item.get("title"),
                                 worker_id, now, tid),
                            )
                            counts["success"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return counts

    def stats(self):
        conn = self.conn
        with self._lock:
            by_state = {r["state"]: r["c"] for r in conn.execute(
                "SELECT state, COUNT(*) c FROM tasks GROUP BY state")}
            recent_cut = int(time.time()) - 300
            recent_success = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state='success' AND updated_at>=?",
                (recent_cut,)).fetchone()["c"]
        total = sum(by_state.values())
        done = by_state.get("success", 0) + by_state.get("failed", 0)
        rate_per_min = recent_success / 5.0        # 近 5 分鐘成功數 → 吞吐/ETA
        remaining = total - done
        eta_min = (remaining / rate_per_min) if rate_per_min > 0 else None
        return {
            "total": total,
            "by_state": by_state,
            "done": done,
            "progress_pct": round(100.0 * done / total, 2) if total else 0.0,
            "success": by_state.get("success", 0),
            "failed": by_state.get("failed", 0),
            "success_rate_pct": round(
                100.0 * by_state.get("success", 0) / done, 2) if done else None,
            "recent_success_5min": recent_success,
            "throughput_per_min": round(rate_per_min, 1),
            "eta_min": round(eta_min, 1) if eta_min is not None else None,
        }

    # --- 管理：把所有 failed 重開做「最後一輪」（todo.txt 5.1）----------
    def requeue_failed(self):
        conn = self.conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "UPDATE tasks SET state='undone', dispatched_at=NULL, worker_id=NULL,"
                    " updated_at=? WHERE state='failed'", (int(time.time()),))
                n = cur.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return n


class Handler(BaseHTTPRequestHandler):
    store = None
    token = None

    def log_message(self, fmt, *args):     # 靜音預設 access log；改用自訂精簡輸出
        pass

    # --- helpers -----------------------------------------------------------
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not self.token:
            return True     # 未設 token = 本機測試模式
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {self.token}"

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:   # noqa: BLE001
            return None

    # --- routes ------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, {"ok": True, "ts": int(time.time())})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if u.path == "/stats":
            return self._send(200, self.store.stats())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        q = parse_qs(u.query)

        if u.path == "/lease":
            n = int(q.get("n", ["30"])[0])
            worker = q.get("worker", ["anon"])[0]
            try:
                batch = self.store.lease(n, worker)
            except sqlite3.OperationalError as e:
                return self._send(503, {"error": f"db busy: {e}"})
            return self._send(200, {"tasks": batch, "lease_ttl": LEASE_TTL})

        if u.path == "/result":
            body = self._read_json()
            if body is None:
                return self._send(400, {"error": "bad json"})
            worker = body.get("worker") or q.get("worker", ["anon"])[0]
            results = body.get("results", [])
            if not isinstance(results, list):
                return self._send(400, {"error": "results must be a list"})
            try:
                counts = self.store.report(worker, results)
            except sqlite3.OperationalError as e:
                return self._send(503, {"error": f"db busy: {e}"})
            return self._send(200, {"applied": counts})

        if u.path == "/admin/requeue-failed":
            n = self.store.requeue_failed()
            return self._send(200, {"requeued": n})

        return self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="revswarm 工作佇列 server")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"),
                    help="Bearer token；預設讀環境變數 REVSWARM_TOKEN，皆無則不驗證")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"⚠️ 資料庫 {args.db} 不存在，請先跑 init_tasks.py 建任務。", file=sys.stderr)
        sys.exit(1)

    Handler.store = Store(args.db)
    Handler.token = args.token
    auth = "有 token 驗證" if args.token else "⚠️ 無 token（僅限本機測試）"
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"revswarm server 啟動  http://{args.host}:{args.port}  db={args.db}  {auth}")
    print(f"  lease TTL={LEASE_TTL}s  單次 lease 上限={MAX_LEASE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中斷，關閉。")
        httpd.shutdown()


if __name__ == "__main__":
    main()

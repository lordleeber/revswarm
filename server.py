#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm server：分散式工作佇列（FastAPI 概念，但用標準庫 http.server 實作，零依賴）。

state machine:  undone → dispatched → (success | failed)
  - dispatched 逾 LEASE_TTL 秒沒回覆 → 派工時「惰性回收」自動視為 undone
  - failed = 真的找不到（worker 民國+西元兩種年份都試過）；不再重試
  - rate_limited ≠ failed：立刻放回 undone，不計失敗

endpoints（都需 header  Authorization: Bearer <token>，除 /stats 之外可設）：
  POST /lease?n=30&worker=<id>&engine=yahoo   原子租一批任務（engine 分流佇列，預設 yahoo）
  POST /result   {results:[{id,status,date?,source?,title?}, ...]}  批次回報
  GET  /stats                        進度、各 state 計數、近況
  GET  /healthz                      存活探針（免 token）
  POST /admin/requeue-failed?engine=google   把 failed 轉去指定 engine 的佇列重掃

用法：
  python3 server.py --db revswarm.db --host 0.0.0.0 --port 8000 --token SECRET
若不給 --token 則從環境變數 REVSWARM_TOKEN 讀；兩者皆無則不驗證(僅限本機測試)。
"""

import argparse
import hmac
import html
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
  revenue       INTEGER,          -- 月營收(元)，由 raw_title 解析；非官方、四捨五入，僅供校驗
  yoy           REAL,             -- 年增率(%)，同上
  engine        TEXT NOT NULL DEFAULT 'yahoo',  -- 佇列分流：yahoo|google（見 lease/requeue_failed）
  attempts      INTEGER DEFAULT 0,
  fail_count    INTEGER DEFAULT 0,
  dispatched_at INTEGER,
  worker_id     TEXT,
  updated_at    INTEGER,
  UNIQUE(stock_id, roc_year, roc_month)
);
CREATE INDEX IF NOT EXISTS idx_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_dispatched ON tasks(state, dispatched_at);
-- /status 的近5分計數、來源分佈、最近成功排序都靠這個複合索引，避免全表掃描
-- （connect() 每次都跑 executescript，既有 DB 重啟後也會自動補上此索引）
CREATE INDEX IF NOT EXISTS idx_state_updated ON tasks(state, updated_at);
-- 派工「越舊營收月越優先」：讓 WHERE state='undone' ORDER BY roc_year,roc_month
-- 直接走索引順序，免 TEMP B-TREE 排序（見 lease）
CREATE INDEX IF NOT EXISTS idx_undone_age ON tasks(state, roc_year, roc_month);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """既有 DB 補欄位：CREATE TABLE IF NOT EXISTS 不會替舊表加欄位，故手動 ALTER。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    for col, decl in (("revenue", "INTEGER"), ("yoy", "REAL"),
                      ("engine", "TEXT NOT NULL DEFAULT 'yahoo'")):
        if col not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl}")
    # 這個索引依賴 engine 欄位，須放在上面 ALTER TABLE 之後才能建（SCHEMA 的
    # executescript 在舊 DB 上跑在 _migrate 之前，此時 engine 欄位可能還不存在）。
    # google_worker 補搜：requeue-failed?engine=google 把 failed 轉去 engine='google'，
    # lease 依 engine 分流，Yahoo/Google worker 才不會搶同一批 undone（見 lease/requeue_failed）。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_undone_engine_age"
        " ON tasks(state, engine, roc_year, roc_month)")


def _tok_eq(a, b):
    """常數時間比較，避免 token 比對的計時側信道。"""
    return a is not None and b is not None and hmac.compare_digest(str(a), str(b))


def check_auth(configured_token, auth_header, query_token, allow_query_token):
    """
    純函式的驗證判斷，方便單元測試。
    - 未設 token → 一律放行（本機測試模式）。
    - 接受 header `Authorization: Bearer <token>`。
    - 只有 allow_query_token=True（唯讀 GET）才接受 `?token=<token>`；
      會改狀態的 POST 一律不吃查詢字串 token，避免 token 經 URL/log/Referer 外洩。
    """
    if not configured_token:
        return True
    if auth_header.startswith("Bearer ") and _tok_eq(auth_header[len("Bearer "):], configured_token):
        return True
    if allow_query_token and _tok_eq(query_token, configured_token):
        return True
    return False


def clamp_refresh(raw, default=10):
    """/status 自動刷新秒數：下限 5（避免輪詢風暴與 worker 搶鎖）、上限 300。"""
    try:
        return max(5, min(int(raw), 300))
    except (ValueError, TypeError):
        return default


def derive_stats(by_state, recent_success):
    """由各 state 計數 + 近 5 分鐘成功數，導出進度/成功率/吞吐/ETA。stats 與 dashboard 共用。

    prelisting（公司首次公開前、不可能有月營收，見 mark_prelisting.py）不會被 lease，
    故排除在「應做總數(workable)」之外——進度與 ETA 以 workable 為分母，數字才誠實。
    """
    total = sum(by_state.values())
    excluded = by_state.get("prelisting", 0)      # 上市前：排除在進度分母外
    workable = total - excluded
    done = by_state.get("success", 0) + by_state.get("failed", 0)
    rate_per_min = recent_success / 5.0
    remaining = workable - done
    eta_min = (remaining / rate_per_min) if rate_per_min > 0 else None
    return {
        "total": total,
        "prelisting": excluded,
        "workable": workable,
        "by_state": by_state,
        "done": done,
        "progress_pct": round(100.0 * done / workable, 2) if workable else 0.0,
        "success": by_state.get("success", 0),
        "failed": by_state.get("failed", 0),
        "success_rate_pct": round(
            100.0 * by_state.get("success", 0) / done, 2) if done else None,
        "recent_success_5min": recent_success,
        "throughput_per_min": round(rate_per_min, 1),
        "eta_min": round(eta_min, 1) if eta_min is not None else None,
    }


_STATUS_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0d1117;color:#c9d1d9;font:15px/1.5 -apple-system,
 "Segoe UI",Roboto,"Noto Sans TC",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8b949e;font-size:13px;margin-bottom:20px}
.bar{height:26px;background:#161b22;border-radius:6px;overflow:hidden;
 border:1px solid #30363d}
.bar > span{display:block;height:100%;background:linear-gradient(90deg,#238636,#2ea043);
 text-align:right;color:#fff;font-size:12px;line-height:26px;padding-right:8px;
 white-space:nowrap;min-width:2.5em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
 gap:12px;margin:18px 0}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px}
.card .k{color:#8b949e;font-size:12px}
.card .v{font-size:22px;font-weight:600;margin-top:2px}
.v.ok{color:#3fb950}.v.bad{color:#f85149}.v.warn{color:#d29922}.v.dim{color:#8b949e}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500}
td.t{color:#8b949e;max-width:280px;overflow:hidden;text-overflow:ellipsis;
 white-space:nowrap}
.foot{color:#484f58;font-size:12px;margin-top:22px}
h2{font-size:15px;color:#c9d1d9;margin:22px 0 4px}
"""


def _humanize_min(m):
    if m is None:
        return "—"
    if m < 60:
        return f"{m:.0f} 分"
    if m < 1440:
        return f"{m / 60:.1f} 小時"
    return f"{m / 1440:.1f} 天"


def render_status_html(d, refresh_sec=10):
    """把 dashboard() 的資料渲染成自足的深色狀態頁（含 meta refresh 自動更新）。"""
    esc = html.escape
    by = d["by_state"]
    cards = [
        ("進度", f'{d["progress_pct"]}%', "dim"),
        ("完成 / 應做", f'{d["done"]} / {d["workable"]}', "dim"),
        ("success", by.get("success", 0), "ok"),
        ("failed", by.get("failed", 0), "bad"),
        ("undone", by.get("undone", 0), "dim"),
        ("dispatched", by.get("dispatched", 0), "warn"),
        ("上市前(排除)", by.get("prelisting", 0), "dim"),
        ("成功率", "—" if d["success_rate_pct"] is None else f'{d["success_rate_pct"]}%', "dim"),
        ("吞吐 / 分", d["throughput_per_min"], "dim"),
        ("近 5 分成功", d["recent_success_5min"], "dim"),
        ("預估剩餘", _humanize_min(d["eta_min"]), "dim"),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{esc(str(k))}</div>'
        f'<div class="v {cls}">{esc(str(v))}</div></div>'
        for k, v, cls in cards)

    src = d.get("source_counts", {})
    src_html = " &nbsp;·&nbsp; ".join(
        f"{esc(str(k))}: <b>{v}</b>" for k, v in sorted(src.items())) or "—"

    rows = d.get("recent", [])
    rows_html = "".join(
        f'<tr><td>{esc(r["stock_id"])}</td><td>{esc(r["name"])}</td>'
        f'<td>{r["roc_year"]}/{r["roc_month"]:02d}</td>'
        f'<td>{esc(r["announce_date"] or "")}</td>'
        f'<td>{esc(r["source"] or "")}</td>'
        f'<td class="t">{esc((r["raw_title"] or "")[:60])}</td></tr>'
        for r in rows) or '<tr><td colspan="6" class="t">（還沒有成功資料）</td></tr>'

    pct = d["progress_pct"]
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{int(refresh_sec)}">
<title>revswarm status · {pct}%</title><style>{_STATUS_CSS}</style></head>
<body><div class="wrap">
<h1>revswarm 爬取進度</h1>
<div class="sub">每 {int(refresh_sec)} 秒自動更新 · JSON 版見 <code>/stats</code></div>
<div class="bar"><span style="width:{max(pct, 3)}%">{pct}%</span></div>
<div class="grid">{cards_html}</div>
<h2>來源分佈（success）</h2><div class="sub">{src_html}</div>
<h2>最近成功</h2>
<table><thead><tr><th>代號</th><th>名稱</th><th>營收月</th><th>公布日</th>
<th>來源</th><th>標題（provenance）</th></tr></thead><tbody>{rows_html}</tbody></table>
<div class="foot">revswarm · ts={int(time.time())}</div>
</div></body></html>"""


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
    def lease(self, n, worker_id, engine="yahoo"):
        n = max(1, min(int(n), MAX_LEASE))
        now = int(time.time())
        cutoff = now - LEASE_TTL
        conn = self.conn
        # 大鎖 + BEGIN IMMEDIATE：立刻取寫鎖，兩隻 worker 不會拿到同一批。
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 惰性回收：先把逾時的 dispatched 全部收回 undone（走 idx_dispatched，很快）。
                # 拆成獨立 UPDATE（而非塞進 SELECT 的 OR）是為了讓下面的挑選能純走
                # WHERE state='undone'，靠 idx_undone_age 免 TEMP B-TREE 排序。不分
                # engine：租約逾時就該放回，不管原本是哪個 worker 種類拿走的。
                conn.execute(
                    "UPDATE tasks SET state='undone', dispatched_at=NULL, worker_id=NULL"
                    " WHERE state='dispatched'"
                    "   AND (dispatched_at IS NULL OR dispatched_at < ?)",
                    (cutoff,))
                # 挑最舊的 undone（越舊營收月越優先，跨所有股票齊步推進）。
                # engine 過濾：Yahoo/Google worker 分流各自的佇列，不互搶任務
                # （failed 批次靠 requeue_failed(engine='google') 轉去 google 佇列）。
                rows = conn.execute(
                    "SELECT id FROM tasks WHERE state='undone' AND engine=?"
                    " ORDER BY roc_year, roc_month, id LIMIT ?",
                    (engine, n),
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
                              FROM tasks WHERE id IN ({qmarks})
                             ORDER BY roc_year, roc_month, id""",  # 回傳也照年齡序
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
                            title = item.get("title")
                            rev = revlib.parse_revenue(title)   # 順手抽營收(非權威，僅校驗)
                            revenue, yoy = rev if rev else (None, None)
                            conn.execute(
                                "UPDATE tasks SET state='success', announce_date=?,"
                                " source=?, raw_title=?, revenue=?, yoy=?, worker_id=?,"
                                " dispatched_at=NULL, updated_at=? WHERE id=?",
                                (valid, item.get("source"), title, revenue, yoy,
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
            recent_success = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state='success' AND updated_at>=?",
                (int(time.time()) - 300,)).fetchone()["c"]
        return derive_stats(by_state, recent_success)

    def dashboard(self):
        """/status 用：stats + 來源分佈(q_roc/q_ad) + 最近成功樣本。"""
        conn = self.conn
        with self._lock:
            by_state = {r["state"]: r["c"] for r in conn.execute(
                "SELECT state, COUNT(*) c FROM tasks GROUP BY state")}
            recent_success = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state='success' AND updated_at>=?",
                (int(time.time()) - 300,)).fetchone()["c"]
            source_counts = {(r["source"] or "?"): r["c"] for r in conn.execute(
                "SELECT source, COUNT(*) c FROM tasks WHERE state='success'"
                " GROUP BY source")}
            recent = [dict(r) for r in conn.execute(
                "SELECT stock_id, name, roc_year, roc_month, announce_date, source,"
                " raw_title, updated_at FROM tasks WHERE state='success'"
                " ORDER BY updated_at DESC LIMIT 15")]
        d = derive_stats(by_state, recent_success)
        d["source_counts"] = source_counts
        d["recent"] = recent
        return d

    # --- 管理：把所有 failed 重開做「最後一輪」（todo.txt 5.1）----------
    def requeue_failed(self, engine=None):
        """把 state='failed' 的任務重開回 undone。

        engine=None（預設，沿用既有行為）：只改 state，engine 欄位不動，
        原地讓現有 worker 再掃一輪（例如改抓西元年常能救回）。
        engine='google' 等：連 engine 一併改過去，轉交給該 engine 的 worker
        專門處理，不會跟原 engine 的 worker 搶同一批 undone（見 lease）。
        """
        conn = self.conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                if engine:
                    cur = conn.execute(
                        "UPDATE tasks SET state='undone', engine=?, dispatched_at=NULL,"
                        " worker_id=NULL, updated_at=? WHERE state='failed'",
                        (engine, int(time.time())))
                else:
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

    def _send_html(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, allow_query_token=False):
        # allow_query_token 只在唯讀 GET 開啟；POST（會改狀態）維持只吃 header。
        q = parse_qs(urlparse(self.path).query)
        return check_auth(self.token, self.headers.get("Authorization", ""),
                          q.get("token", [None])[0], allow_query_token)

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
        # GET 皆唯讀，允許瀏覽器用 ?token= 方便看 /status、/stats
        if not self._authed(allow_query_token=True):
            # /status 是給瀏覽器看的，401 也回 HTML 提示怎麼帶 token
            if u.path == "/status":
                return self._send_html(
                    401, "<h3>unauthorized</h3><p>加上 <code>?token=你的TOKEN</code>"
                    "，或用 header <code>Authorization: Bearer &lt;token&gt;</code>。</p>")
            return self._send(401, {"error": "unauthorized"})
        if u.path == "/stats":
            return self._send(200, self.store.stats())
        if u.path == "/status":
            q = parse_qs(u.query)
            refresh = clamp_refresh(q.get("refresh", ["10"])[0])
            return self._send_html(200, render_status_html(self.store.dashboard(), refresh))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        q = parse_qs(u.query)

        if u.path == "/lease":
            n = int(q.get("n", ["30"])[0])
            worker = q.get("worker", ["anon"])[0]
            engine = q.get("engine", ["yahoo"])[0]
            try:
                batch = self.store.lease(n, worker, engine)
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
            engine = q.get("engine", [None])[0]
            n = self.store.requeue_failed(engine)
            return self._send(200, {"requeued": n})

        return self._send(404, {"error": "not found"})


def main():
    revlib.load_env()          # 先載 .env，讓 REVSWARM_TOKEN 免手打
    ap = argparse.ArgumentParser(description="revswarm 工作佇列 server")
    ap.add_argument("--db", default="revswarm.db")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"),
                    help="Bearer token；預設讀 .env / 環境變數 REVSWARM_TOKEN，皆無則不驗證")
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
    # 不把 token 明文印進 log；請自行接 ?token=<你的 REVSWARM_TOKEN>
    tok_hint = "?token=<REVSWARM_TOKEN>" if args.token else ""
    print(f"  狀態頁: http://{args.host}:{args.port}/status{tok_hint}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中斷，關閉。")
        httpd.shutdown()


if __name__ == "__main__":
    main()

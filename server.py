#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm server：分散式工作佇列（FastAPI 概念，但用標準庫 http.server 實作，零依賴）。

state machine:  undone → dispatched → (success | failed)
  - dispatched 逾 LEASE_TTL 秒沒回覆 → 派工時「惰性回收」自動視為 undone
  - failed（worker 兩種年份都試過仍找不到）或 rejected（server 驗窗/撞名沒過）
    都會依 NEXT_ENGINE 自動升級：退回 undone、engine 換下一棒（yahoo→google→gemini）
    重掃；已經是 gemini、沒有下一棒才真的落 state='failed'，不再重試（見 Store.report）
  - rate_limited ≠ failed：立刻放回 undone、engine 不變，不計失敗、不升級

endpoints（都需 header  Authorization: Bearer <token>，除 /stats 之外可設）：
  POST /lease?n=30&worker=<id>&engine=yahoo   原子租一批任務
                                     （engine 分流佇列，預設 yahoo；只收 yahoo|google|gemini，其餘 400）
  POST /result   {results:[{id,status,date?,source?,title?,url?}, ...]}  批次回報
  POST /verify   {by:"mops"|"gemini", items:[{stock_id,roc_year,roc_month,date}, ...]}
                                     第二來源同意才蓋 verified（見 Store.verify）
  GET  /stats                        進度、各 state 計數、近況
  GET  /healthz                      存活探針（免 token）
  POST /admin/requeue   {engine?, items:[{stock_id,roc_year,roc_month,date}, ...]}
                                     指名把「確定抓錯」的 success 打回 undone 重爬
                                     （⚠️ 唯一會降級 success 的操作，date 當樂觀鎖）
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
ENGINES = ("yahoo", "google", "gemini")   # 佇列分流白名單；未列入的一律 400（見 parse_engine）
DEFAULT_ENGINE = "yahoo"
# 自動升級鏈：這一關沒交出可信結果（worker 回報 failed，或 server 驗窗/撞名沒過的
# rejected）就換下一棒重掃，而不是傻傻退回同一個引擎——同一個引擎的系統性偏誤
# （anchor 咬到廣告片段、SERP 排序偏舊…）重爬大機率複製同一個錯。gemini 沒有下一棒，
# 見 Store.report 的 elif status == "failed" 與 valid is None 兩處。
NEXT_ENGINE = {"yahoo": "google", "google": "gemini"}
# verified 欄位的合法值白名單與**強弱排序**。刻意用白名單而不是自由字串：這一欄的
# 全部價值就在於「看到它就知道有第二個獨立來源核對過」，放任何人寫任何字進去就等於
# 沒有這個保證。
#
# ⚠️ 排序不是裝飾，是必要的：一列只有一個 verified 欄，裝不下「兩個來源都同意」。
# 而 README 教的跑法就是先 mops 後 gemini，若後蓋的無條件覆寫，那些 MOPS 官方文件
# 蓋過的章會被 gemini 這個弱來源默默降級——使用者照著文件跑就會把最硬的證據弄丟。
# 數字大 = 強：
#   mops    官方申報文件（公開資訊觀測站 t05st01）——最硬
#   gemini  模型 grounding 獨立查出同一個日期（實測 m_src 為 0，全靠模型合成文字，
#           見 README「對照實驗」）
#   claude  ⚠️ **逐筆讀證據的判斷，不是第二個獨立來源**。title 正是產生
#   codex   announce_date 的那段文字（revlib.parse 從它附近抽日期），再讀一次同一段
#           字沒有引入新證據——這是循環。它只回答「這段佐證文字撐不撐得起這個日期」，
#           例如標題其實是股東會通知、或只是一段 JSON 碎片。當篩選線索用，不是驗證。
#           ⚠️ claude 與 codex 是**兩條各自獨立的審核線**（見 gemini_worker.REVIEWERS），
#           判準完全相同，所以**同階**：誰都不該壓過誰。代價是兩條線審到同一個月份
#           時，後蓋的會靜默覆蓋前一個——所以那邊的指令一律要求指名 --reviewer。
#   tbd     看過了、但不是高信心。與 NULL 的差別是「已經有人看過」，避免重複讀。
VERIFIER_RANK = {"mops": 4, "gemini": 3, "codex": 2, "claude": 2, "tbd": 1}
VERIFIERS = tuple(VERIFIER_RANK)

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
  url           TEXT,             -- 這個日期是從哪一篇讀到的（provenance；見 revlib「來源網址」）
  verified      TEXT,             -- 第二個獨立來源同意 announce_date 才蓋章：mops|gemini（見 Store.verify）
  revenue       INTEGER,          -- 月營收(元)，由 raw_title 解析；非官方、四捨五入，僅供校驗
  yoy           REAL,             -- 年增率(%)，同上
  engine        TEXT NOT NULL DEFAULT 'yahoo',  -- 佇列分流：yahoo|google|gemini（見 lease/requeue_failed）
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
                      ("engine", "TEXT NOT NULL DEFAULT 'yahoo'"),
                      # ⚠️ 既有 ~12 萬筆 success 這兩欄一定是 NULL，補不回來——當初沒存。
                      # 「NULL = 不知道」，不要在查詢裡把它當成「沒有出處/未驗證」的結論。
                      ("url", "TEXT"), ("verified", "TEXT")):
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


def parse_engine(raw, default=None):
    """
    解析 ?engine= 參數，回傳 (engine, ok)。

    不合法的 engine 一定要回 400，不可靜默當成預設或原樣寫進 DB——兩種靜默壞法都很難查：
      - lease?engine=（空字串）當真 → WHERE engine='' 永遠零筆，worker 每 30s 印
        「佇列已空」，看起來像沒任務、其實是參數打錯。
      - requeue-failed?engine=googel（打錯字）原樣寫入 → 3 萬筆被丟進沒有任何 worker
        會租的佇列，回應卻照樣是 {"requeued": 30877}。
    大小寫不做正規化：'Google' 直接 400，比默默寫進一個跟 DB 值不同的字串好查。

    缺參數/空字串 → (default, True)；default 由呼叫端決定語意
    （lease 是 DEFAULT_ENGINE，requeue-failed 是 None＝engine 欄位不動）。
    """
    if not raw:
        return default, True
    if raw in ENGINES:
        return raw, True
    return None, False


def derive_stats(by_state, recent_success, queue_by_engine=None):
    """由各 state 計數 + 近 5 分鐘成功數，導出進度/成功率/吞吐/ETA。stats 與 dashboard 共用。

    queue_by_engine（{engine: {state: n}}，只含未完成的 undone/dispatched）是
    「哪個佇列還有多少活」：by_state 只 GROUP BY state，自動升級搬到 google/gemini
    的列在畫面上就是普通的 undone，只開 yahoo worker 時它們會永遠卡著——undone
    一直漲、ETA 照算，卻沒有任何線索說「這要換一支 worker 才吃得動」。

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
        "queue_by_engine": queue_by_engine or {},
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

    # 依 ENGINES 的接棒順序排，其餘（理論上不該有）排在後面，才讀得出升級方向。
    queue = d.get("queue_by_engine", {})
    order = {e: i for i, e in enumerate(ENGINES)}
    q_html = " &nbsp;·&nbsp; ".join(
        f"{esc(str(eng))}: <b>{q.get('undone', 0)}</b>"
        + (f"（+{q['dispatched']} 派工中）" if q.get("dispatched") else "")
        for eng, q in sorted(queue.items(), key=lambda kv: (order.get(kv[0], 99), kv[0]))
    ) or "—"
    # 非 yahoo 的佇列要各自的 worker 才吃得動（google 需 Playwright、gemini 需
    # 付費金鑰，都得手動開）；沒開就會一直卡著，這裡直說免得被當成普通 undone。
    stalled = [e for e, q in queue.items() if e != DEFAULT_ENGINE and q.get("undone")]
    if stalled:
        q_html += ("　⚠️ " + "/".join(sorted(stalled))
                   + " 佇列要各自的 worker 才吃得動")

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
<h2>待做佇列（依 engine）</h2><div class="sub">{q_html}</div>
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
        self._name_cache = None    # _names() 的快取

    # --- 派工：原子鎖定 + 惰性回收 ---------------------------------------
    def lease(self, n, worker_id, engine=DEFAULT_ENGINE):
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
                # 等值條件，靠 idx_undone_engine_age 免 TEMP B-TREE 排序（實測
                # EXPLAIN QUERY PLAN：SEARCH USING COVERING INDEX idx_undone_engine_age。
                # ORDER BY 的 id 由索引隱含尾隨的 rowid 滿足——id 就是 rowid）。
                # ⚠️ 加了 engine 條件後，舊索引 idx_undone_age(state,roc_year,roc_month)
                # 已無任何查詢會用到，可安全 DROP（留著只是白付寫入成本）。
                # 惰性回收本身不分 engine：租約逾時就該放回，不管哪種 worker 拿走的。
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

    def _names(self):
        """
        {公司名: 股號}，用來擋「raw_title 講的是別家公司」。

        ⚠️ 這份對照只有 server 拿得到：worker 只知道自己這一筆的公司名，不知道
        「聯上發」是另一檔股票，所以它擋不了（見 report 的說明）。
        建一次就快取——tasks 的公司名在 init_tasks 之後就不會變，而 report 是熱路徑。
        """
        if self._name_cache is None:
            self._name_cache = {
                r["name"]: r["stock_id"]
                for r in self.conn.execute(
                    "SELECT DISTINCT name, stock_id FROM tasks")}
        return self._name_cache

    # --- 回報：success 需窗再驗證；rate_limited 放回；failed 記數 --------
    def report(self, worker_id, results):
        now = int(time.time())
        # failed/rejected/success/rate_limited 數的是「回報進來的是什麼」，
        # escalated/terminal 數的是「這批回報造成了什麼」：自動升級上線後兩者會差
        # 很多（只開 yahoo worker 時整批都是升級、一筆終點也沒有），只印 failed=N
        # 會讓讀 log 的人以為 N 筆已經沒救了。terminal 明講真的落 state='failed'
        # 的筆數。
        counts = {"success": 0, "failed": 0, "rate_limited": 0,
                  "rejected": 0, "ignored": 0, "unknown": 0,
                  "escalated": 0, "terminal": 0}
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
                        "SELECT state, roc_year, roc_month, name, stock_id, engine"
                        " FROM tasks WHERE id=?", (tid,)
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
                        next_engine = NEXT_ENGINE.get(row["engine"])
                        if next_engine:
                            # 這個引擎兩種年份都試過仍找不到 → 換下一棒，不當終點。
                            conn.execute(
                                "UPDATE tasks SET state='undone', engine=?,"
                                " fail_count=fail_count+1, worker_id=NULL,"
                                " dispatched_at=NULL, updated_at=? WHERE id=?",
                                (next_engine, now, tid),
                            )
                            counts["escalated"] += 1
                        else:
                            # 已經是最後一棒（gemini）沒有下一個可換，才是真的終點。
                            conn.execute(
                                "UPDATE tasks SET state='failed', fail_count=fail_count+1,"
                                " worker_id=?, dispatched_at=NULL, updated_at=? WHERE id=?",
                                (worker_id, now, tid),
                            )
                            counts["terminal"] += 1
                        counts["failed"] += 1

                    else:  # success：server 端用同一套窗再驗一次（README「踩過的雷 → 窗過濾」）
                        valid = revlib.validate_date(
                            item.get("date"), row["roc_year"], row["roc_month"])
                        # ⚠️ 再多擋一條：raw_title 講的若是「名字更長的另一家公司」，
                        # 不收。revlib.parse 的後備路徑（錨點沒中 → 取第一個窗內日期）
                        # 沒有任何公司名保護，實測 12 筆因此吃到別家的公告日
                        # （4113 聯上 → 聯上發(2537)…），全部出自 google worker。
                        # 帶 roc_year/roc_month：本檔自己的錨點命中就不算撞名，否則
                        # 會誤擋「開頭是正確公告、尾巴提到相關公司」那種正確的列。
                        if valid is not None and revlib.longer_name_in_text(
                                item.get("title") or "", row["name"], row["stock_id"],
                                self._names(), row["roc_year"], row["roc_month"]):
                            valid = None
                        # ⚠️ 第三條：標題前段就講明這不是營收公告（實測 4173 久裕
                        # 111/10 抓到 goodinfo 董監持股明細頁，窗與撞名都攔不住）。
                        # 只擋「前段命中且全文無營收字樣」，尾巴被 SERP 拼上董監
                        # 持股的正確公告不受影響（見 revlib.is_non_revenue_title）。
                        if valid is not None and revlib.is_non_revenue_title(
                                item.get("title") or ""):
                            valid = None
                        if valid is None:
                            # 日期不在窗內／撞到別家公司 → 不信任，這個引擎這次
                            # 沒交出可信結果，跟 status='failed' 同等看待：換下一棒。
                            next_engine = NEXT_ENGINE.get(row["engine"])
                            # fail_count 跟 status='failed' 那條路一樣要加：它是
                            # 「這筆被爬過幾次」的歷史，rejected 既然與 failed 同等
                            # 看待，歷史就不能只記一半——否則一路被 reject 到底的列
                            # 會停在 fail_count=0，跟從沒被派過的列長得一模一樣。
                            if next_engine:
                                conn.execute(
                                    "UPDATE tasks SET state='undone', engine=?,"
                                    " fail_count=fail_count+1,"
                                    " dispatched_at=NULL, worker_id=NULL,"
                                    " updated_at=? WHERE id=?",
                                    (next_engine, now, tid),
                                )
                                counts["escalated"] += 1
                            else:
                                # 已經是最後一棒（gemini），沒有下一個可換 → 終點。
                                conn.execute(
                                    "UPDATE tasks SET state='failed',"
                                    " fail_count=fail_count+1, dispatched_at=NULL,"
                                    " worker_id=NULL, updated_at=? WHERE id=?",
                                    (now, tid),
                                )
                                counts["terminal"] += 1
                            counts["rejected"] += 1
                        else:
                            title = item.get("title")
                            rev = revlib.parse_revenue(title)   # 順手抽營收(非權威，僅校驗)
                            revenue, yoy = rev if rev else (None, None)
                            # url 跟 date 一樣是 worker 送上來的，一律再驗一次格式：
                            # 不合格就存 NULL，不要讓垃圾字串混進 provenance 欄位。
                            url = revlib.clean_url(item.get("url"))
                            conn.execute(
                                # verified=NULL：這是一個新抓到的日期，舊的章是對
                                # 「上一個日期」蓋的，留著就會替一個沒人驗過的值背書。
                                "UPDATE tasks SET state='success', announce_date=?,"
                                " source=?, raw_title=?, url=?, revenue=?, yoy=?,"
                                " verified=NULL, worker_id=?,"
                                " dispatched_at=NULL, updated_at=? WHERE id=?",
                                (valid, item.get("source"), title, url, revenue, yoy,
                                 worker_id, now, tid),
                            )
                            counts["success"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return counts

    # --- 蓋章：第二個獨立來源同意 announce_date ------------------------------
    def verify(self, by, items):
        """
        把「已經被第二個來源核對過」的列蓋上 verified=by。

        為什麼蓋章要走 server、而不是讓對照實驗自己開 DB 寫：
          server 是這個 DB 的**唯一寫入者**（全靠 self._lock + BEGIN IMMEDIATE 串行化）。
          多開一個寫入者就得自己處理鎖競爭，而 gemini_benchmark 的賣點之一正是「唯讀，
          可以在 server 跑著的時候執行」。走 endpoint 兩邊都保住。

        ⚠️ 蓋章的前提是**日期真的一致**：呼叫端送上它那邊看到的 date，跟 DB 裡的
        announce_date 逐字對不上就記成 mismatch、不蓋（純字串比對，不套窗驗證
        ——見下方註解）。不然 verified 只是「有人跑過這一筆」，
        不是「有人證實過這一筆」——那就一文不值了。
        日期不一致本身是有價值的訊號（代表兩個來源打架），留給呼叫端去看，這裡不改資料。

        ⚠️ 弱來源不會蓋掉強來源（見 VERIFIER_RANK）：已經是 mops 的列再被 gemini
        打到會記成 kept、原樣不動。

        回傳各類計數。重複蓋同一個 by 是冪等的。
        """
        rank = VERIFIER_RANK[by]
        counts = {"verified": 0, "kept": 0, "mismatch": 0,
                  "not_success": 0, "unknown": 0}
        conn = self.conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for item in items:
                    # ⚠️ 整批是一個交易：任何一個元素丟出例外都會 rollback 掉其餘
                    # 499 筆，而 handler 只接 sqlite3.OperationalError，會變成 500
                    # 加一段 traceback。所以型別一律在這裡擋掉、記成 unknown。
                    if not isinstance(item, dict):
                        counts["unknown"] += 1
                        continue
                    sid = item.get("stock_id")
                    date = item.get("date")
                    if not isinstance(sid, (str, int)) or not isinstance(date, str):
                        # validate_date 會對非字串做 .strip() → AttributeError。
                        counts["unknown"] += 1
                        continue
                    try:
                        ry = int(item.get("roc_year"))
                        rm = int(item.get("roc_month"))
                    except (TypeError, ValueError):
                        counts["unknown"] += 1
                        continue
                    row = conn.execute(
                        "SELECT id, state, announce_date, verified FROM tasks"
                        " WHERE stock_id=? AND roc_year=? AND roc_month=?",
                        (str(sid), ry, rm)).fetchone()
                    if row is None:
                        counts["unknown"] += 1
                        continue
                    if row["state"] != "success":
                        # 還沒定案的列沒有 announce_date 可以核對，蓋章沒有意義。
                        counts["not_success"] += 1
                        continue
                    # ⚠️ 樂觀鎖只比對字串，**不可以借用 validate_date**：全庫唯一
                    # 合法的窗外 success 是 date_overrides.csv 收的遲交個案
                    # （3494 誠研 109/1 = 2020-02-17），窗驗證會讓它永遠 mismatch、
                    # 永遠蓋不了章，逐批掃描時還會卡在隊首無限重複出現。
                    # 窗驗證是 report 那條路的職責（worker 送新日期進來時）；這裡收的
                    # 是「呼叫端剛才看到的值」，只需要確認那列還沒被改過。
                    if date.strip() != (row["announce_date"] or ""):
                        counts["mismatch"] += 1
                        continue
                    if VERIFIER_RANK.get(row["verified"], 0) > rank:
                        # 已經有更強的章了，別降級（見 VERIFIER_RANK）。
                        counts["kept"] += 1
                        continue
                    # ⚠️ 只動 verified，不碰 updated_at：updated_at 是「這筆資料何時被
                    # 抓到/改過」，/stats 的近 5 分計數與最近成功排序都吃它。蓋章不是
                    # 重新抓到，混進去會讓進度看板憑空多出一批「剛完成」的任務。
                    conn.execute("UPDATE tasks SET verified=? WHERE id=?", (by, row["id"]))
                    counts["verified"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return counts

    # --- 把確定抓錯的 success 打回 undone 重爬 ------------------------------
    def requeue(self, items, engine=None):
        """
        指名把幾筆 success 降級成 undone 重爬。

        ⚠️ 這是全 repo 唯一會把 success 降級的線上操作。requeue-failed 只動 failed、
        worker 的 report 看到 success 一律 ignored——就這支能砍掉已經定案的資料。
        所以樂觀鎖是必要的而不是裝飾：呼叫端要說出它看到的 announce_date，對不上就
        不動。你只能 requeue 一筆你真的看過的列。

        用途是「已經確定抓錯」的個案，例如 google 的後備路徑抓到別家公司的公告日
        （4113 聯上 吃到 聯上發(2537)，見 data/title_review.csv）。

        也吃 failed 的列——那種沒有 announce_date，樂觀鎖傳空字串。這是為了「只挑
        幾筆換一條路試」：requeue-failed 會把**全部** failed（實測 5,624 筆）一起
        丟進指定佇列，想只動 11 筆就得有指名的方式。

        engine 給值就順便改佇列——google 抓錯的別再給 google。給 None 則沿用原佇列。

        清掉所有「抓來的內容」，但**保留 attempts/fail_count**：那是這筆被爬過幾次的
        歷史，清掉就查不出「這筆一直出問題」，而那正是之後該優先看的線索。
        """
        now = int(time.time())
        counts = {"requeued": 0, "mismatch": 0, "not_requeueable": 0, "unknown": 0}
        conn = self.conn
        with self._lock:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                for item in items:
                    if not isinstance(item, dict):
                        counts["unknown"] += 1
                        continue
                    sid = item.get("stock_id")
                    date = item.get("date")
                    if not isinstance(sid, (str, int)) or not isinstance(date, str):
                        counts["unknown"] += 1
                        continue
                    try:
                        ry = int(item.get("roc_year"))
                        rm = int(item.get("roc_month"))
                    except (TypeError, ValueError):
                        counts["unknown"] += 1
                        continue
                    row = conn.execute(
                        "SELECT id, state, announce_date FROM tasks"
                        " WHERE stock_id=? AND roc_year=? AND roc_month=?",
                        (str(sid), ry, rm)).fetchone()
                    if row is None:
                        counts["unknown"] += 1
                        continue
                    if row["state"] not in ("success", "failed", "undone"):
                        # dispatched 正在被某隻 worker 爬，改了會跟它的回報打架；
                        # prelisting 是「公司當時還沒公開發行」的刻意標記，不是待辦。
                        # ⚠️ undone 刻意**放行**：它沒有 announce_date/raw_title，沒有
                        # 任何東西可以損失，重排唯一的效果就是換 engine——而「這條路
                        # 試過了不行，換一條」正是這支存在的理由。
                        counts["not_requeueable"] += 1
                        continue
                    # 樂觀鎖：failed 的列沒有 announce_date，呼叫端傳空字串即可。
                    if date.strip() != (row["announce_date"] or ""):
                        counts["mismatch"] += 1
                        continue
                    # url/verified 一起清（見 report 的同一條戒律）：留著就是替一個
                    # 已經被判定錯誤的日期背書。
                    sql = ("UPDATE tasks SET state='undone', announce_date=NULL,"
                           " source=NULL, raw_title=NULL, revenue=NULL, yoy=NULL,"
                           " url=NULL, verified=NULL, dispatched_at=NULL,"
                           " worker_id=NULL, updated_at=?")
                    args = [now]
                    if engine is not None:
                        sql += ", engine=?"
                        args.append(engine)
                    sql += " WHERE id=?"
                    args.append(row["id"])
                    conn.execute(sql, args)
                    counts["requeued"] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return counts

    def _queue_by_engine(self):
        """{engine: {state: n}}，只算未完成的 undone/dispatched（呼叫端須已持有鎖）。

        success/failed 已經有歸宿，不是佇列深度，算進去只會讓數字看起來很忙。
        """
        out = {}
        for r in self.conn.execute(
                "SELECT engine, state, COUNT(*) c FROM tasks"
                " WHERE state IN ('undone','dispatched') GROUP BY engine, state"):
            out.setdefault(r["engine"], {})[r["state"]] = r["c"]
        return out

    def stats(self):
        conn = self.conn
        with self._lock:
            by_state = {r["state"]: r["c"] for r in conn.execute(
                "SELECT state, COUNT(*) c FROM tasks GROUP BY state")}
            recent_success = conn.execute(
                "SELECT COUNT(*) c FROM tasks WHERE state='success' AND updated_at>=?",
                (int(time.time()) - 300,)).fetchone()["c"]
            queue_by_engine = self._queue_by_engine()
        return derive_stats(by_state, recent_success, queue_by_engine)

    def dashboard(self):
        """/status 用：stats + 各 engine 佇列深度 + 來源分佈(q_roc/q_ad) + 最近成功樣本。"""
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
            queue_by_engine = self._queue_by_engine()
        d = derive_stats(by_state, recent_success, queue_by_engine)
        d["source_counts"] = source_counts
        d["recent"] = recent
        return d

    # --- 管理：把所有 failed 重開做「最後一輪」（README「API」）--------
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
            engine, ok = parse_engine(q.get("engine", [""])[0], DEFAULT_ENGINE)
            if not ok:
                return self._send(400, {"error": f"unknown engine; 可用: {list(ENGINES)}"})
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

        if u.path == "/verify":
            body = self._read_json()
            if body is None:
                return self._send(400, {"error": "bad json"})
            by = body.get("by")
            if by not in VERIFIERS:
                return self._send(400, {"error": f"unknown verifier; 可用: {list(VERIFIERS)}"})
            items = body.get("items", [])
            if not isinstance(items, list):
                return self._send(400, {"error": "items must be a list"})
            try:
                counts = self.store.verify(by, items)
            except sqlite3.OperationalError as e:
                return self._send(503, {"error": f"db busy: {e}"})
            return self._send(200, {"applied": counts})

        if u.path == "/admin/requeue":
            body = self._read_json()
            if body is None:
                return self._send(400, {"error": "bad json"})
            items = body.get("items", [])
            if not isinstance(items, list):
                return self._send(400, {"error": "items must be a list"})
            raw = body.get("engine")
            if raw is None:
                engine = None
            else:
                engine, ok = parse_engine(raw, None)
                if not ok:
                    return self._send(400, {"error": f"unknown engine; 可用: {list(ENGINES)}"})
            try:
                counts = self.store.requeue(items, engine)
            except sqlite3.OperationalError as e:
                return self._send(503, {"error": f"db busy: {e}"})
            return self._send(200, {"applied": counts})

        if u.path == "/admin/requeue-failed":
            # default=None ＝ 不給 engine 就沿用舊行為（只改 state，engine 欄位不動）。
            engine, ok = parse_engine(q.get("engine", [""])[0], None)
            if not ok:
                return self._send(400, {"error": f"unknown engine; 可用: {list(ENGINES)}"})
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

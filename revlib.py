#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm 共用核心：月營收公布日的「期望窗」與 Yahoo 搜尋頁解析。

server 與 worker 都依賴這裡的窗邏輯，確保「worker 抓到的日期」與
「server 存 success 前的再驗證」用的是同一套規則（見 todo.txt 2.4 / 5.5）。

純函式、只用標準庫，沒有網路副作用；worker 的抓取(curl)放在 worker.py。
"""

import os
import re


# --- .env 自動載入 ---------------------------------------------------------
def load_env(path=None):
    """
    把 .env（每行 KEY=VALUE）載入 os.environ，方便 server/worker 免打一長串環境變數。
    - 不覆蓋「已存在」的環境變數（顯式 export 或 --token 仍優先）。
    - 找不到檔案就靜默略過。支援 # 註解、export 前綴、單/雙引號。
    - 預設在「cwd」與「本檔所在目錄」各找一個 .env。
    """
    if path:
        candidates = [path]
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [os.path.join(os.getcwd(), ".env"), os.path.join(here, ".env")]
    seen = set()
    for p in candidates:
        p = os.path.abspath(p)
        if p in seen or not os.path.isfile(p):
            continue
        seen.add(p)
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


# --- 任務範圍設定 ----------------------------------------------------------
# 民國 109/1 ~ 115/1（含），= 73 個月。roc_* 指「營收所屬」月份。
ROC_START = (109, 1)
ROC_END = (115, 1)


def iter_roc_months(start=ROC_START, end=ROC_END):
    """依序產出 (roc_year, roc_month)，涵蓋 start..end（含兩端）。"""
    (sy, sm), (ey, em) = start, end
    y, m = sy, sm
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            y += 1
            m = 1


# --- 期望窗（todo.txt 2.4）-------------------------------------------------
def expected_window(roc_year, roc_month):
    """
    月營收依規定次月10日前申報，遇假日順延。正確公布日一定落在
    「營收月的次月」的 1~15 號。回傳該次月的 (西元年, 月)。
    """
    if roc_month <= 11:
        return roc_year + 1911, roc_month + 1
    return roc_year + 1912, 1


def in_window(year, month, day, roc_year, roc_month):
    """(西元 year, month, day) 是否落在 (roc_year, roc_month) 的期望窗、且日 1~15。"""
    wy, wm = expected_window(roc_year, roc_month)
    return year == wy and month == wm and 1 <= day <= 15


# --- 日期擷取 --------------------------------------------------------------
# 支援三種寫法：2022年2月10日 / 2022/2/10 / 2022-02-10
_DATE_PATTERNS = [
    re.compile(r'(20\d{2})年(\d{1,2})月(\d{1,2})日'),
    re.compile(r'(20\d{2})/(\d{1,2})/(\d{1,2})'),
    re.compile(r'(20\d{2})-(\d{1,2})-(\d{1,2})'),
]


def _iter_dates(html):
    """產出 (char_offset, y, m, d)，涵蓋頁面中所有可辨識日期。"""
    for pat in _DATE_PATTERNS:
        for mt in pat.finditer(html):
            yield mt.start(), int(mt.group(1)), int(mt.group(2)), int(mt.group(3))


def _anchor_offsets(html, name, roc_year, roc_month):
    """
    精確名稱標題錨點的字元位置（todo.txt 2.5：避免「統一」吃到「統一超」）。
    鎖定「{名稱} {年}年{月}月」，名稱後須緊接空白或數字。民國/西元年都接受。
    """
    esc = re.escape(name)
    # (?:【公告】)? 兼容 Yahoo 股市公告標題；名稱與年之間允許 0~1 個空白；
    # 名稱後用 lookahead 確保緊接空白或數字，不被更長公司名吃掉。
    pat = re.compile(
        r'(?:【公告】)?' + esc + r'(?=[\s　\d])[\s　]?\d{2,4}年\s*' +
        str(roc_month) + r'月'
    )
    return [m.start() for m in pat.finditer(html)]


def parse(html, name, roc_year, roc_month):
    """
    從 Yahoo 搜尋頁擷取該 (股票,月份) 的公布日。

    回傳 (announce_date 'YYYY-MM-DD', matched_title_hint) 或 None。
      - 只保留落在 expected_window 內的日期（窗過濾 → 0 髒資料）。
      - 若有精確名稱錨點，取「離錨點最近」的窗內日期；否則取第一個窗內日期。
    matched_title_hint 為錨點附近的一小段文字，作為 provenance 佐證。
    """
    if not html:
        return None
    cands = []  # (offset, 'YYYY-MM-DD')
    for off, y, m, d in _iter_dates(html):
        if in_window(y, m, d, roc_year, roc_month):
            cands.append((off, f"{y:04d}-{m:02d}-{d:02d}"))
    if not cands:
        return None

    anchors = _anchor_offsets(html, name, roc_year, roc_month)
    if anchors:
        off, date = min(cands, key=lambda c: min(abs(c[0] - a) for a in anchors))
        near = min(anchors, key=lambda a: abs(a - off))
        title = _clean_snippet(html[near:near + 60])
    else:
        off, date = cands[0]
        title = _clean_snippet(html[max(0, off - 40):off + 20])
    return date, title


_TAG = re.compile(r'<[^>]+>')
_WS = re.compile(r'[\s　]+')


def _clean_snippet(s):
    """去標籤、壓空白，取一段可讀的標題佐證字串。"""
    s = _TAG.sub('', s)
    s = _WS.sub(' ', s).strip()
    return s[:80]


# --- 給 server 再驗證用 -----------------------------------------------------
_ISO = re.compile(r'^(20\d{2})-(\d{1,2})-(\d{1,2})$')


def validate_date(date_str, roc_year, roc_month):
    """
    server 收到 worker 回報的 success 日期時，用同一套窗再驗一次（todo.txt 5.5）。
    通過回傳正規化 'YYYY-MM-DD'，否則 None。
    """
    if not date_str:
        return None
    m = _ISO.match(date_str.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if in_window(y, mo, d, roc_year, roc_month):
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# --- 從 raw_title 順手抽營收（交叉校驗用，非權威）--------------------------
# raw_title 的兩種主要來源格式：
#   MoneyDJ  ：「台化 109年8月營收189.84億、年減26.94% - MoneyDJ理財網」
#   Yahoo公告：「【公告】福壽 2020年1月合併營收10.17億元 年增-15.24%」
# 抽出的是「單月(合併)營收」金額 + 年增率。來源非官方、金額四捨五入（億/萬 兩位），
# 僅供交叉校驗，不可當權威值（權威到元請用 MOPS 月營收）。
# 數字部分收緊為「必以數字開頭、至多一組小數」，保證後面 float() 不會因畸形字串
# （如 "1.2.3"、只有逗號）拋 ValueError——這函式跑在 server 回報熱路徑、吃外部內容。
_REV_AMT = re.compile(r'(?:合併)?營收\s*(\d[\d,]*(?:\.\d+)?)\s*(億|萬)')
_REV_YOY = re.compile(r'年\s*(增|減)?\s*(-?\d+(?:\.\d+)?)\s*%')
# 自結損益/EPS 等非「月營收公告」的雜訊，見到就不抽（比照 mops_validate 的排除）
_REV_SKIP = re.compile(r'自結|稅前|稅後|盈餘|損益|每股|EPS')


def parse_revenue(text):
    """從 raw_title 抽 (revenue_yuan:int, yoy_pct:float|None)；抽不到回 None。

    - revenue_yuan：單月(合併)營收，億/萬 正規化為「元」（int）。
    - yoy_pct：年增率（正=增、負=減）；抽不到年增率但有金額時為 None。
    - 屬自結損益等非月營收公告 → 回 None。
    """
    if not text or _REV_SKIP.search(text):
        return None
    m = _REV_AMT.search(text)
    if not m:
        return None
    amt = float(m.group(1).replace(",", "")) * (1e8 if m.group(2) == "億" else 1e4)
    yoy = None
    my = _REV_YOY.search(text)
    if my:
        val = float(my.group(2))
        if my.group(1) == "減":          # 「年減26.94%」→ 負；「年增-15.24%」數字自帶負號
            val = -abs(val)
        yoy = val
    return int(round(amt)), yoy

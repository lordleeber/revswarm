#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm 共用核心：月營收公布日的「期望窗」與 Yahoo 搜尋頁解析。

server 與 worker 都依賴這裡的窗邏輯，確保「worker 抓到的日期」與
「server 存 success 前的再驗證」用的是同一套規則（見 README「踩過的雷 → 窗過濾」）。

純函式、只用標準庫，沒有網路副作用；worker 的抓取(curl)放在 yahoo_worker.py。
"""

import os
import re
import urllib.parse


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


# --- 上市前緩衝（mark_prelisting 用；export 重寫後也走這個值）---------------
# 公司在「首次公開當月」補報「前一個月」營收是合法的：實測 18 筆都長這樣且 raw_title
# 對得上（例：竑騰 113/5 營收於 2024-06-07 公布、公開發行日 2024-06-04）。所以判定
# 「這筆不可能存在」時要放這麼多個月的水。
#
# 放這裡而不是放進 mark_prelisting，是因為「回寫 DB 降級」與「匯出時標 pre_public
# flag」必須用同一個數字——兩邊各寫各的會出現「已降級的留著、沒降級的被標 low」這種
# 不一致。export.py 待重寫（見 git log ad4810f），重寫時直接引用這個常數。
PRE_PUBLIC_GRACE_MONTHS = 1


# --- 期望窗（README「踩過的雷 → 窗過濾」）---------------------------------
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
    r"""
    精確名稱標題錨點的字元位置（README「踩過的雷 → 解析陷阱」：避免「統一」吃到「統一超」）。
    鎖定「{名稱} {年}年{月}月」，名稱後須緊接空白或數字。民國/西元年都接受。

    年份必須是「這個任務的年」（民國 roc_year 或西元 roc_year+1911）。早期版本這裡寫
    \d{2,4}年，只鎖月不鎖年，於是搜尋結果頁上「同月份、別年份」的近期文章會被當成錨點
    ——例：任務 111/6、112/6、113/6 三筆都錨到同一篇「宏碁智新 115年6月」。窗過濾只擋
    得掉日期本身離譜的，擋不掉「錨錯地方、卻剛好挑到一個窗內日期」，形成靜默錯配
    （實測全庫 705 筆、佔 0.57%，見 README「資料品質」）。

    收緊之後找不到錨點的頁面會落到 parse() 的後備路徑（取第一個窗內日期），精度較低但
    不會指向別年的文章；寧可退回後備，也不要一個確定錯的錨點。
    """
    esc = re.escape(name)
    # (?:【公告】)? 兼容 Yahoo 股市公告標題；名稱與年之間允許 0~1 個空白；
    # 名稱後用 lookahead 確保緊接空白或數字，不被更長公司名吃掉。
    year = r'(?:%d|%d)' % (roc_year, roc_year + 1911)
    pat = re.compile(
        r'(?:【公告】)?' + esc + r'(?=[\s　\d])[\s　]?' + year + r'年\s*' +
        str(roc_month) + r'月'
    )
    return [m.start() for m in pat.finditer(html)]


def parse_detail(html, name, roc_year, roc_month):
    """
    parse() 的完整版：多回一個「往回找來源連結該從哪個字元位置開始」。

    回傳 (announce_date 'YYYY-MM-DD', matched_title_hint, offset) 或 None。
      - 只保留落在 expected_window 內的日期（窗過濾 → 0 髒資料）。
      - 若有精確名稱錨點，取「離錨點最近」的窗內日期；否則取第一個窗內日期。
    matched_title_hint 為錨點附近的一小段文字，作為 provenance 佐證。

    ⚠️ offset 是「往回找連結該從哪裡開始」，**有錨點時給的是錨點位置，不是日期位置**。
    這兩個位置不一定同序：窗內日期可能出現在錨點【前面】（例如上一筆結果的摘要裡就
    有一個窗內日期，而它離錨點最近），此時從日期位置往回找會抓到上一筆結果的連結
    ——看起來像有效出處卻指錯篇。錨點是「公司名＋這個任務的年月」的精確比對，位置就
    落在該筆結果的標題文字裡，往回一定命中它自己的 <a href>。
    沒有錨點時只剩日期位置可用，那條路本來精度就較低（見 parse 的後備路徑）。
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
        off = near                      # ⚠️ 綁定用錨點位置，見 docstring
    else:
        off, date = cands[0]
        title = _clean_snippet(html[max(0, off - 40):off + 20])
    return date, title, off


def parse(html, name, roc_year, roc_month):
    """
    從 Yahoo 搜尋頁擷取該 (股票,月份) 的公布日。回傳 (date, title) 或 None。

    parse_detail 的兩元組版本。絕大多數呼叫端不需要 offset，只有要記來源網址的
    yahoo_worker 用得到，所以完整版另外開一個函式，不動這裡的既有介面。
    """
    hit = parse_detail(html, name, roc_year, roc_month)
    return (hit[0], hit[1]) if hit else None


_TAG = re.compile(r'<[^>]+>')
_WS = re.compile(r'[\s　]+')


def _clean_snippet(s):
    """去標籤、壓空白，取一段可讀的標題佐證字串。"""
    s = _TAG.sub('', s)
    s = _WS.sub(' ', s).strip()
    return s[:80]


# --- 來源網址（provenance）--------------------------------------------------
# 為什麼要存 URL：raw_title 只是一段佐證文字，事後想追「這個日期到底是哪一篇寫的」
# 就只能靠關鍵字回頭再搜一次，而搜尋結果早就變了。11.3 查 google 那批系統性偏早時
# 吃過這個虧——有標題、沒有出處，無法回去看原文（見 README「資料品質」）。
#
# ⚠️ Yahoo 的結果連結全部包一層轉址：
#   https://r.search.yahoo.com/_ylt=.../RU=<百分比編碼的原網址>/RK=.../RS=...
# 存轉址網址等於沒存：_ylt 是有時效的簽章、過期就 404，網址本身也看不出是哪一家媒體。
# 所以一律解回 RU= 裡那個真正的網址。
_YAHOO_RU = re.compile(r'/RU=(.*?)/RK=')
_HREF = re.compile(r'<a\s[^>]*?href="(https?://[^"]+)"', re.I)
MAX_URL = 500          # 存進 DB 前的長度上限（Yahoo 轉址網址本身就近 200 字元）

# Yahoo SERP 頁固定嵌一段廣告贊助 iframe。_anchor_offsets 的「公司名+年+月」正則
# 純比對文字、不管出現位置，於是這段廣告 iframe 附近若剛好嵌了回顯查詢字串的 JSON
# 追蹤片段（`"泓格 2022年10月","yptydevice":"desktop"...`），也會被當成合法錨點，
# nearest_url 往回找到的固定就是這顆廣告連結——跟真正的搜尋結果無關（見 README）。
_AD_SPONSOR_URL = re.compile(r'^https?://[^/]*emarketing\.yahoo\.com/ysmacq/', re.I)


def is_ad_sponsor_url(u):
    """u 是不是那顆固定的 yahoo 廣告贊助連結——命中的話代表 hit 不是真正的搜尋結果。"""
    return bool(u) and bool(_AD_SPONSOR_URL.match(u))


def unwrap_url(u):
    """Yahoo 轉址 → 原始網址；不是轉址的原樣回傳。"""
    if not u:
        return None
    m = _YAHOO_RU.search(u)
    if m:
        u = urllib.parse.unquote(m.group(1))
    return u


def clean_url(u):
    """
    存進 DB 前的把關：必須是 http(s) 開頭、不超過 MAX_URL。不合格回 None。

    server 端也用這支再驗一次——url 跟 announce_date 一樣是 worker 送上來的，
    不能因為「是自己人寫的 worker」就免驗（三道防污染的同一個道理）。
    """
    if not isinstance(u, str):
        return None
    u = u.strip()
    if not u.startswith(("http://", "https://")):
        return None
    if len(u) > MAX_URL:
        # ⚠️ 過長就整個拒收，不可以截斷。這一欄的全部用途就是點得開回到原文，
        # 而截一半的網址是「看起來合法、點下去是 404」——比 NULL 更糟，因為
        # NULL 誠實地說「沒有出處」，截斷的則謊稱有。
        return None
    return u


def nearest_url(html, offset):
    """
    取 offset 之前「最近的一個 <a href="http...">」，當作這個日期的出處。

    ⚠️ 一定要往回找、不能往前找：Yahoo 的一筆結果排成
        <h3><a href=...>標題</a></h3> … <p>摘要（日期在這裡）</p>
    連結永遠在日期前面。往前找會抓到「下一筆」結果的連結，張冠李戴——那比沒有 URL
    更糟，因為它看起來像個有效的出處。

    純文字（google_worker 的 inner_text、gemini 的模型回覆）沒有任何標籤，這裡會回
    None。那是預期行為不是失敗，那兩支各有自己的取法。
    """
    if not html or offset is None:
        return None
    last = None
    for m in _HREF.finditer(html, 0, offset):
        last = m.group(1)
    return clean_url(unwrap_url(last))


def longer_name_in_text(text, name, stock_id, names,
                        roc_year=None, roc_month=None):
    """
    text 裡有沒有「以 name 開頭、但更長」的別家公司名？有就回那個名字，否則 None。

    ⚠️ 這是 _anchor_offsets 那個 lookahead 擋的同一個坑（「統一」不可以吃到「統一超」），
    但**parse 的後備路徑完全繞過它**——沒錨點就沒有名稱保護，直接取頁面上第一個窗內
    日期。實測全庫 12 筆因此吃到別家公司的公告日（4113 聯上 → 聯上發(2537)、
    2906 高林 → 高林股(1531)…），全部出自 google worker：它解析的是 inner_text，
    公司名被塞進網址 slug 用連字號連著（「公告-聯上發-2020…」），錨點對不上。

    names 是 {公司名: 股號}。比對的是**股號**不是字串：同一檔在名單裡若有更長的別名
    （「聯上」/「聯上開發」都是 4113），那不是撞名。
    回最長的那個，讓訊息指得準（「統一超商」比「統一超」有用）。

    ⚠️ 給了 roc_year/roc_month 且**本檔自己的錨點在 text 裡命中**時一律回 None。
    實測誤報：1216 統一 110/6 的 raw_title 是「【公告】統一2021年6月合併營收392.53
    億元…上一則 … 統一超表現備」——開頭就是本檔正確的公告，「統一超」只是尾巴
    「相關文章」的碎片。把這種判成抓錯公司，會害一筆正確的資料被打回重爬，而降級
    success 是不可逆的。錨點要是「這個任務的年月」才算數，別月的公告救不了這一筆。
    """
    if not text or not name:
        return None
    if name not in text:
        return None
    if roc_year is not None and roc_month is not None \
            and _anchor_offsets(text, name, roc_year, roc_month):
        return None
    hit = None
    for other, code in names.items():
        if (len(other) > len(name) and other.startswith(name)
                and other in text and str(code) != str(stock_id)):
            if hit is None or len(other) > len(hit):
                hit = other
    return hit


def pick_url_by_name(links, name, roc_year, roc_month):
    """
    從 (連結文字, 網址) 清單裡挑出「標題錨點命中」的第一個。google_worker 用。

    ⚠️ 判準必須是 _anchor_offsets（公司名＋這個任務的年月），**不可以只寫
    `name in text`**：那會讓「統一」吃到「統一超 109年1月營收」，正是
    _anchor_offsets 花了一整段 docstring 在擋的那個前綴撞名。url 指到別家公司比
    沒有 url 更糟，而且 google 這條路本來就弱一階，禁不起再多一個已知的錯法。

    弱在哪：Google 這條路解析的是整頁純文字 (inner_text)，日期的字元位置與 DOM 裡
    的連結對不起來，沒辦法像 Yahoo 那樣靠位置鄰近去綁定。這裡只能用「標題錨點命中」
    當關聯，不保證就是那個日期的出處，只當線索用。
    挑不到就回 None，不要退而求其次挑第一個連結——那幾乎一定是頁面導覽列。
    """
    if not links or not name:
        return None
    for text, href in links:
        if text and _anchor_offsets(text, name, roc_year, roc_month):
            u = clean_url(href)
            if u:
                return u
    return None


# --- 給 server 再驗證用 -----------------------------------------------------
_ISO = re.compile(r'^(20\d{2})-(\d{1,2})-(\d{1,2})$')


def validate_date(date_str, roc_year, roc_month):
    """
    server 收到 worker 回報的 success 日期時，用同一套窗再驗一次
    （三道防污染的第三道，見 README「踩過的雷 → 窗過濾」）。
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


def yoy_scope(text):
    """判斷 raw_title 裡那個年增率講的是「單月」還是「累計」。

    MoneyDJ 兩種寫法混用，年增率黏在哪個營收後面就是誰的：
      單月：「大城地產 112年9月營收3萬、年減99.98%」                    → 'monthly'
      累計：「皇普 113年12月營收53.06億，累計營收53.31億、年增188.14%」  → 'cumulative'
    後者的 188.14% 是「累計 53.31 億 vs 去年同期累計 18.50 億」，不是單月年增率。
    parse_revenue 兩種都照抽進 yoy 欄，語意混在一起——實測有「累計」字樣的那批，
    拿單月營收自算年增率只有 67.5% 對得上，沒有的那批是 97.2%（見 README「資料品質」）。

    回傳 'monthly' / 'cumulative'；沒有金額或沒有年增率可定位時回 None。
    判準：金額與年增率之間（或金額緊鄰的前兩字）出現「累計」→ 累計。
    """
    if not text or _REV_SKIP.search(text):
        return None
    m_amt = _REV_AMT.search(text)
    m_yoy = _REV_YOY.search(text)
    if not m_amt or not m_yoy:
        return None
    head = text[max(0, m_amt.start() - 2):m_amt.start()]     # 「累計營收…」只有累計沒單月
    between = text[m_amt.end():m_yoy.start()]                # 「營收X，累計營收Y、年增Z%」
    return "cumulative" if ("累計" in head or "累計" in between) else "monthly"


_TITLE_YM = re.compile(r'(\d{2,4})\s*年\s*(\d{1,2})\s*月')


def title_year_conflict(text, roc_year, roc_month):
    """raw_title 的佐證文字講的是不是「別年的同月份」。

    專治 _anchor_offsets 收緊前留下的錯配：title 寫「115年6月」卻掛在任務 111/6 上。
    只在「有 N年{roc_month}月 字樣、但 N 全都不是本任務年份」時回 True；
    找不到可比對的年月字樣回 False（沒有證據不算衝突——Yahoo 模板碎片就屬這類）。
    """
    if not text:
        return False
    same_month = [int(y) for y, m in _TITLE_YM.findall(text) if int(m) == roc_month]
    if not same_month:
        return False
    return not any(y in (roc_year, roc_year + 1911) for y in same_month)

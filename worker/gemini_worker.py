#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revswarm gemini_worker：向 server 租 engine='gemini' 的任務，用 Gemini API 的
「Grounding with Google Search」查月營收公布日、回報結果。第三種 worker。

定位是**實驗**，不是主力：google_worker.py（Playwright 開真 Chrome）已經把 30,877 筆
補搜完（24,196 success），這支是用來回答「同樣的題目，換成 grounding 拿到的日期，
品質跟 SERP 直爬比是好是壞」——所以它刻意把「日期是從哪來的」標進 source（見下 3.）。

跟另外兩支 worker 的差別：
  ✓ 零第三方依賴（純 urllib 打 REST），也不需要圖形環境／Chrome／人工解驗證碼
    ——這是它相對 google_worker 唯一明確的優勢，可以跑在無頭機器上、可以多開。
  ✗ 要錢，且回的是**模型合成的文字**，不是搜尋結果原文（見 3. 的信任分級）。
    ⚠️ 按「模型實際發出的搜尋次數」計費，不是按 prompt。而【實測一個任務會發 4~11 次
       搜尋、平均約 7 次】（2026-08-24 於 Vertex 量測 5 筆：4/4/8/8/11）——不是直覺的
       1 次。所以成本上限用 webSearchQueries 的長度累加，不是用任務數（見 --max-searches）。

走 Vertex AI（Agent Platform API），Bearer OAuth，額度算在【正常的 Google Cloud 帳單】。
  ⚠️ 另一道門（AI Studio / Gemini Developer API，x-goog-api-key）已經【刻意移除】，
     別再加回來：2026-03 起 Google Cloud 的額度【不能】付那道門的帳，而 2026-08 實測
     它對本專案一律回 429 "prepayment credits are depleted"（Google 側已知 bug）。
     完整經過見 README「怎麼走到 Vertex 這條路的」——那是一條走過的死路，不要重走。

三個關鍵設計（都是從 google_worker.py 的教訓直接搬過來的）：

1. ⚠️ 沒搜尋就不算 failed。回應裡 groundingMetadata.webSearchQueries 是空的 → 代表模型
   根本沒去搜、是憑記憶回答，這種「沒查過」絕不可以記成「Google 也沒有」，一律
   rate_limited 放回佇列。這就是 google_worker「頁面沒有 #search 容器 → NOT_SERP，
   不是 failed」的同一條戒律：把「還沒查成功」寫成 failed 會靜靜污染研究資料。
   failed 的定義只有一種：模型確實搜了、正常回話了、仍然沒有窗內日期。

2. ⚠️ 金鑰錯／專案沒開 API → 直接結束行程，不是 failed 也不硬撐。這類設定錯誤對每一筆
   都會重演，繼續跑只會把整個佇列刷成 rate_limited 或燒錢。fatal 就停，讓人去修 Console。
   ⚠️ 但 403 不可以一律當 fatal：實測 Vertex 打過一次空 body 的 403，下一次同樣的請求
   就 200。只有訊息明確指向永久性問題（API 沒啟用、權限不足…）才 fatal，見 _FATAL_403。

3. 信任分級：同一份回應拆成兩塊分別餵 revlib.parse，source 記錄命中的是哪一塊——
     m_src = 日期出現在 groundingMetadata.groundingChunks 的來源標題裡
             （Google 索引回來的真實文字，沒有經過模型改寫，可信度接近 SERP）
     m_txt = 日期只出現在模型自己寫的回答裡（合成文字，可能是幻覺）
   ⚠️ 這個分級就是本實驗的重點。README「資料品質 → 沒修掉、要知道的殘留風險」已經
   記過 google 那批日期系統性偏早
   （占 8%）且沒有外部基準可驗；如果 m_txt 的比例很高，代表這條路拿到的日期
   多半是模型「講」出來的而不是「查」出來的，那品質判斷要另外做，不能直接混進主資料。
   實務上 Gemini API 的 groundingChunks.title 常常只給網域名（moneydj.com）而不是文章
   標題，所以 m_src 命中率可能很低——那本身就是一個結論，不是 bug。

4. 審核模式（--review-one / --review-verdict）：把「要不要讓這個日期進 DB」交給人/Claude。
   ⚠️ 動機直接來自 3.：m_txt 是三支 worker 裡信任度最低的一層，而它的佐證（模型讀了
   哪些網域＝groundingChunks、發了哪些搜尋）**DB 一個欄位都裝不下**，批次模式跑完就
   隨程序結束消失。審核模式把整份證據倒成 JSON、且【刻意不回報】，等審核者看完才落地。
   兩個不可以放寬的界線：
     ✗ 審核者不能自己送日期進來。approve 送出去的是 judge() 算好的那一份（逐字），
       所以審核只能【否決】一個 success，不能無中生有一個 success（見 review_verdict）。
     ✗ 審核模式與批次模式共用 judge()，不可以各寫一套判準——不然審核者看的
       worker_verdict 跟批次真的會回報的東西不一樣，等於在審另一套規則。
   逐筆判斷寫進版控的 data/gemini_review.csv（含 chunks），再由
   mops.stamp_verified --from gemini-review 蓋 verified='claude'。

三道防污染原封不動沿用（沒有任何一道因為換了資料源而放寬）：
   窗過濾 + 名稱錨點（revlib.parse）→ title_year_conflict 再擋一次錯年 → server 端再驗窗。
   模型回什麼日期都沒有特權，一律要通過 revlib.parse 才算數。

前置（見 README「gemini_worker」）。先把 failed 轉進 gemini 佇列，否則沒有任務可租：
  curl -X POST -H "Authorization: Bearer SECRET" \
    "http://SERVER:8000/admin/requeue-failed?engine=gemini"

前置（一次性）：
  gcloud auth application-default login          # 建 ADC（會開瀏覽器）
  gcloud services enable aiplatform.googleapis.com

跑法（repo 根目錄）：
  python3 -m worker.gemini_worker --server http://SERVER:8000 \
      --project YOUR_GCP_PROJECT --max-searches 300

審核模式（一次一筆，給 .claude/skills/gemini-review 用）：
  python3 -m worker.gemini_worker --server http://SERVER:8000 \
      --project YOUR_GCP_PROJECT --review-one          # 倒證據，不回報
  python3 -m worker.gemini_worker --server http://SERVER:8000 \
      --review-verdict approve --note "這一筆為什麼撐得起這個日期"
"""

import argparse
import csv
import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import revlib

VERTEX_HOST = "https://aiplatform.googleapis.com"
VERTEX_LOCATION = "global"
# ⚠️ Vertex 用 camelCase 的 googleSearch（AI Studio 那道門寫 google_search，但那條已移除）。
TOOL_SPEC = {"googleSearch": {}}
# ⚠️ Vertex 沒有 AI Studio 那 5,000 次/月的免費 grounding 額度；單價請以實際帳單校正。
DEFAULT_UNIT_PRICE = 0.014
# ⚠️ 呼叫次數上限，與搜尋次數上限【兩道都要】。理由見 Budget.attempt()：
# 搜尋次數只有拿到回應才數得到，持續失敗時它永遠是 0，擋不住無限迴圈。
DEFAULT_MAX_CALLS = 200
# 預設用 3.x：AI Studio 每月 5,000 次 search 免費，且按 search 計費
# （2.5/2.0 系列對新專案已 404 下架，"no longer available to new users"，退不回去）。
DEFAULT_MODEL = "gemini-3.7-flash"
HTTP_TIMEOUT = 90          # grounding 要真的去搜，比純生成慢；實測一次會發 4~11 個查詢
URL_CHECK_TIMEOUT = 8      # 驗證模型自報的 URL；附屬動作，不值得等太久
# 讀多少 body 來確認「這篇是不是本檔的報導」。標題與導言都在最前面，實測 160KB 的
# 新聞頁只要前 64KB 就夠；讀全文只是白花頻寬與時間。
URL_CHECK_BYTES = 65536
# 驗證時裝成瀏覽器：實測用預設 UA 打 chinatimes 直接吃 403（擋機器人），
# 換成瀏覽器 UA 才拿得到真正的狀態碼（那次是 404）。不裝的話每個網址都「確認不了」。
URL_CHECK_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# ADC token 的實際壽命是 1 小時；提早 10 分鐘換掉，免得剛好在請求途中過期。
TOKEN_TTL = 3000

# 命中來源分級（寫進 result 的 source 欄，見模組開頭 3.）
SRC_CHUNK = "m_src"        # 日期在 groundingChunks 的來源標題裡 → 真實檢索文字
SRC_TEXT = "m_txt"         # 日期只在模型自己寫的回答裡 → 合成文字，存疑

# fetch() 的失敗原因。全部一律 rate_limited（放回佇列），差別只在要不要立刻停整個 worker。
ERR_RETRY = "retry"        # 429/5xx/連線問題 → 退避重試
ERR_FATAL = "fatal"        # 金鑰/權限/配額/請求格式 → 設定錯了，停掉讓人去修
ERR_NOSEARCH = "nosearch"  # 模型沒發出任何搜尋 → 見模組開頭 1.，絕不可當 failed

# --- 審核模式（見模組開頭 4.）------------------------------------------------
# 兩個模式之間的交接檔。⚠️ 走檔案而不是叫審核者把 JSON 貼回來：approve 要送出去的
# date/source/title/url 一律由程式從這裡讀，審核者只交出 verdict 與一段理由，
# 不經手任何資料欄位——手打就有打錯的可能，而打錯的方向剛好是「一個沒人查過的
# 日期被寫進 DB」。gitignored（是執行期產物，且回報完就刪）。
REVIEW_PENDING = ".gemini_review_pending.json"
REVIEW_CSV = "data/gemini_review.csv"
# ⚠️ 這份表頭 mops.stamp_verified 會逐字驗（它 import 這個常數，不自己抄一份）。
# chunks 是這張表比 title_review.csv 多的那一欄：模型實際讀了哪些網域，DB 沒有
# 地方存，不寫進來就沒了（見模組開頭 4.）。
REVIEW_FIELDS = ("stock_id", "roc_year", "roc_month", "announce_date",
                 "source", "url", "chunks", "verdict", "note")
# approve = 這個日期撐得起，讓它進 DB；reject = 撐不起，回報 failed（gemini 是升級鏈
# 最後一棒，failed 就是終點，語意與 worker 自己判 failed 一致）；
# retry = 這一筆根本沒查過（模型沒搜／連線失敗），沒有東西可審，放回佇列。
REVIEW_VERDICTS = ("approve", "reject", "retry")
EXIT_REVIEW = 0            # 證據已倒出，等審核
EXIT_REFUSED = 2           # 審核者的指令本身不合法（見 ReviewError）
EXIT_EMPTY = 3             # gemini 佇列空的，這一輪沒事可做
EXIT_ABORT = 4             # 設定錯誤，每一筆都會重演 → loop 該停


class ReviewError(Exception):
    """審核指令不合法（沒有可核准的內容、缺理由、理由與上一列一字不差…）。

    刻意用例外而不是印個警告繼續：這條路徑的每一次執行都會改到 DB，
    「指令有問題但我猜你的意思」在這裡等於靜默污染。
    """

# 400 的兩種意義完全不同：金鑰無效是 fatal，其餘（例如偶發的內容過濾參數問題）當可重試。
_FATAL_400 = re.compile(r"API key not valid|API_KEY_INVALID|not supported|PERMISSION_DENIED", re.I)
# prompt 要求模型逐字輸出的三行（見 build_prompt）。TITLE 那行是本 worker 唯一拿得到的
# 「文章標題原文」——⚠️ 一定要用它當 provenance，不要讓 revlib.parse 的後備路徑去截
# 日期前後 ±40 字元：那樣存進 raw_title 的會是 prompt 的鷹架（實測存成
# "DATE: 2022-11-08 TITLE: 台塑"），而整個 repo 拿 raw_title 當證據用
# （parse_revenue / yoy_scope / title_year_conflict 都吃它）。
_MODEL_TITLE = re.compile(r"^[ \t]*TITLE:[ \t]*(\S.*?)[ \t]*$", re.M)
# URL 那行同理。⚠️ 這是三支 worker 裡唯一「出處由模型自己回報」的一支——yahoo 是從
# SERP 的 <a href> 讀出來的客觀事實，這裡則是模型說它引了哪一篇，模型有可能生一個
# 不存在的網址出來。所以只當線索，不當保證；revlib.clean_url 只擋格式，擋不了幻覺。
_MODEL_URL = re.compile(r"^[ \t]*URL:[ \t]*(https?://\S+)[ \t]*$", re.M)
# ⚠️ 403 在 Vertex 上**不可以一律當 fatal**：實測打過一次乾淨的 403 Forbidden（空 body），
# 下一次同樣的請求就 200。若照 401 那樣直接停掉整個 worker，一次抖動就收工。
# 所以只有訊息明確指向「永久性設定問題」時才 fatal，其餘 403 退避重試。
_FATAL_403 = re.compile(
    r"has not been used in project|SERVICE_DISABLED|is disabled|"
    r"billing|API key not valid|caller does not have permission|"
    # ⚠️ 這三個才是 Vertex 在 --project 打錯／缺 aiplatform.user 時實際回的字樣。
    # 漏掉的話設定錯會被判成 retry，每一筆無聲重排（_FATAL_400 早就收了
    # PERMISSION_DENIED，兩條規則不該自相矛盾）。
    r"PERMISSION_DENIED|Permission .{0,80}denied|does not have permission", re.I)



# --- Vertex 後端 ------------------------------------------------------------
class VertexBackend:
    """
    Vertex AI（Agent Platform）：Bearer token，額度走 Google Cloud 帳單。

    token 從 `gcloud auth application-default print-access-token` 取得並快取 TOKEN_TTL 秒。
    ⚠️ 刻意用 subprocess 叫 gcloud，而不是 import google.auth：本 repo 只有 google_worker
    有第三方相依（playwright），不值得為了拿一個 token 再多一個。gcloud 本來就得裝
    ——ADC 是它建的。

    ⚠️ 是 `application-default print-access-token` 不是 `print-access-token`：
    前者讀 ADC（application-default login 建的），後者讀 gcloud CLI 自己的帳號
    （gcloud auth login 建的）。實測只跑過 application-default login 的機器上，
    後者會失敗說 "No credentialed accounts"——兩者是不同的憑證，別混用。
    """

    def __init__(self, project, location=VERTEX_LOCATION, gcloud=None):
        self.project = project
        self.location = location
        self.gcloud = gcloud or _find_gcloud()
        self._token = None
        self._token_at = 0.0

    def endpoint(self, model):
        host = (VERTEX_HOST if self.location == "global"
                else f"https://{self.location}-aiplatform.googleapis.com")
        return (f"{host}/v1/projects/{self.project}/locations/{self.location}"
                f"/publishers/google/models/{urllib.parse.quote(model)}:generateContent")

    def token(self, force=False):
        now = time.time()
        if force or self._token is None or now - self._token_at > TOKEN_TTL:
            cmd = [self.gcloud, "auth", "application-default", "print-access-token"]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except FileNotFoundError:
                # ⚠️ 一定要在這裡轉成 RuntimeError(→fatal)。FileNotFoundError 是 OSError
                # 的子類，會被 call_gemini 最後那個 except OSError 當成 ERR_RETRY 吃掉
                # ——gcloud 沒裝的話每一筆都會這樣，worker 就變成無聲的無限迴圈。
                raise RuntimeError(
                    f"找不到 gcloud（試過 {self.gcloud}）。裝好 SDK 或用 --gcloud 指路。")
            except subprocess.TimeoutExpired:
                # 同理：不接的話會直接 traceback 出 run()，整個 worker 當掉。
                raise RuntimeError(f"gcloud 取 token 逾時（{self.gcloud}）。")
            except OSError as e:
                raise RuntimeError(f"gcloud 無法執行（{self.gcloud}）：{e}")
            if out.returncode != 0:
                raise RuntimeError(
                    "取不到 ADC token（先跑 `gcloud auth application-default login`）："
                    + (out.stderr or "").strip()[:200])
            self._token = out.stdout.strip()
            self._token_at = now
        return self._token

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    def describe(self):
        return f"Vertex AI project={self.project} location={self.location}"


def _find_gcloud():
    """找 gcloud 執行檔。PATH 之外也找官方安裝腳本的預設落點（~/google-cloud-sdk/bin）。"""
    found = shutil.which("gcloud")
    if found:
        return found
    cand = os.path.expanduser("~/google-cloud-sdk/bin/gcloud")
    return cand if os.path.isfile(cand) else "gcloud"


def make_backend(args):
    """建 Vertex 後端；設定不全就直接講清楚缺什麼，不要等打了才 4xx。"""
    if not args.project:
        print("⚠️ 需要 --project（或環境變數 GOOGLE_CLOUD_PROJECT）。"
              "設定方式見 README「gemini_worker」。", file=sys.stderr)
        sys.exit(1)
    return VertexBackend(args.project, args.location, args.gcloud)


def build_prompt(sid, name, roc_year, roc_month):
    """
    一個任務一個 prompt。要求逐字複製原始標題，是為了讓 revlib.parse 的名稱錨點
    （「{名稱} {年}年{月}月」）有機會對上——模型若改寫成「該公司於…公布」就只會落到
    「取第一個窗內日期」的後備路徑，精度較低。

    ⚠️ 刻意【不】寫「請用 Google 搜尋查證」。宣告了 googleSearch 工具之後，這種
    「某年某月某檔股票哪天公布」的具體事實查詢，模型本來就會去搜；那句話多半是多餘的，
    而搜尋次數就是錢（一次任務 4~11 次）。真正吃重的是下面那句「不可以推測、估算或用
    慣例推算」——月營收「次月 10 日前申報」是公開常識，模型可以不查就答 10 號，
    而那個日期【還會通過窗過濾】(1~15 內)，變成看起來合理、實際上是猜的髒資料。
    萬一模型真的沒去搜，webSearchQueries 會是空的 → 判 nosearch 放回佇列，不會污染資料。

    ⚠️ 民國年與西元年兩種都寫進 prompt（不像 yahoo/google worker 分兩次查）：
    grounding 由模型自己決定發幾次搜尋、自己會做同義擴展，分兩次打只是把成本乘二。
    也沿用另外兩支的禁忌：不在查詢裡塞「營收」以外的引導詞去限定來源（例如 moneydj），
    實測那會把其他來源的命中排擠掉。

    ⚠️ 2026-08-27 加了 URL 的代價提醒：crawl_task 現在把「m_txt 命中但 URL 打不開」
    整筆判 failed（見該函式），不再是「日期照留、只清 url」。之前的寬容縱容模型
    隨手編一個看起來合理的網址反正頂多被清掉；代價提高後，把它寫進 prompt 讓模型
    自己知道不確定就該回 NONE，別賭。
    """
    ad_year = roc_year + 1911
    return (
        f"台股「{sid} {name}」民國 {roc_year} 年 {roc_month} 月（西元 {ad_year} 年 "
        f"{roc_month} 月）的單月營收，是哪一天正式公布的？\n"
        f"只回覆下面三行，不要有其他文字、不要解釋：\n"
        f"DATE: YYYY-MM-DD\n"
        f"TITLE: <你引用的那篇新聞或公告的原始標題，逐字複製，不要改寫或翻譯>\n"
        f"URL: <該篇報導的網址；不確定是否真實存在就回 NONE，不要編造>\n"
        f"公布日必須是新聞／公告裡實際寫出來的日期，不可以推測、估算或用慣例推算。\n"
        f"URL 若打不開或是編造的，這筆會被整個判定失敗、連日期也不會保留。\n"
        f"查不到就只回 DATE: NONE。"
    )


def call_gemini(prompt, backend, model=DEFAULT_MODEL, timeout=HTTP_TIMEOUT):
    """
    打一次 generateContent（帶 Google Search grounding 工具）。

    回傳 (payload, err)：
      成功 → ({"text":…, "chunks":[標題…], "searches":[查詢字串…]}, None)
      失敗 → (None, ERR_*)

    ⚠️ 絕不因為「模型說查不到」而回 err——那是 parse 的事（同 google_worker.search）。
    err 只描述「這次呼叫本身有沒有成功拿到一份搜過的回應」。
    """
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # 搜尋工具不能跟非搜尋類工具混用；這裡本來也只需要它。
        "tools": [TOOL_SPEC],
        # temperature 0：這是抽事實，不要創意。抽樣只會增加同一題兩次答案不同的機率。
        "generationConfig": {"temperature": 0.0},
    }
    payload_bytes = json.dumps(body).encode("utf-8")

    def _post(refresh_token=False):
        if refresh_token and hasattr(backend, "token"):
            backend.token(force=True)
        req = urllib.request.Request(
            backend.endpoint(model), data=payload_bytes, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in backend.headers().items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    for attempt in (0, 1):
        try:
            data = _post(refresh_token=(attempt == 1))
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if e.code == 401:
                # Vertex 的 access token 一小時就過期。先無條件換一把再試一次，
                # 換完還是 401 才是真的認證壞了。
                if attempt == 0 and hasattr(backend, "token"):
                    continue
                print(f"  ⚠️ HTTP 401：認證失敗。{detail}", file=sys.stderr)
                return None, ERR_FATAL
            if e.code == 403:
                # ⚠️ 不可一律 fatal：實測 Vertex 打過一次空 body 的 403，下一次就 200。
                if _FATAL_403.search(detail):
                    print(f"  ⚠️ HTTP 403：{detail}", file=sys.stderr)
                    return None, ERR_FATAL
                return None, ERR_RETRY
            if e.code == 429:
                # 配額/限速用完。退避重試即可（真的用完會一直 429，靠 --rl-threshold
                # 收批 + 長睡，不會空轉燒錢）。
                return None, ERR_RETRY
            if e.code == 400 and _FATAL_400.search(detail):
                print(f"  ⚠️ HTTP 400：{detail}", file=sys.stderr)
                return None, ERR_FATAL
            if e.code in (404, 405):
                # endpoint 是固定組出來的，404/405 只可能是型號打錯或已退役
                # （gemini-2.5-* / 2.0-flash 對新專案就是 404）。這對每一筆都會重演，
                # 判 retry 只會讓整批無聲 rate_limited，看不出是型號問題。
                print(f"  ⚠️ HTTP {e.code}：型號 {model} 不存在或已退役。{detail}",
                      file=sys.stderr)
                return None, ERR_FATAL
            return None, ERR_RETRY
        except (RuntimeError, subprocess.SubprocessError) as e:
            # 取 token 失敗（gcloud 沒裝、沒跑 application-default login、逾時…）
            # ——重試不會變好，一律 fatal。
            # ⚠️ SubprocessError 要一起接：TimeoutExpired 不是 OSError 也不是
            # RuntimeError，漏接會直接 traceback 出 run()，整個 worker 當掉。
            print(f"  ⚠️ 取 token 失敗：{e}", file=sys.stderr)
            return None, ERR_FATAL
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None, ERR_RETRY
    else:
        return None, ERR_RETRY

    cands = data.get("candidates") or []
    if not cands:
        # promptFeedback.blockReason（安全過濾）等；不是「查不到」，放回重試。
        return None, ERR_RETRY
    cand = cands[0]
    meta = cand.get("groundingMetadata") or {}
    searches = meta.get("webSearchQueries") or []
    if not searches:
        # ⚠️ 見模組開頭 1.：模型沒去搜，等於這一筆根本沒查過，不可以判 failed。
        return None, ERR_NOSEARCH
    text = "".join(
        p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
    chunks = []
    for ch in meta.get("groundingChunks") or []:
        web = ch.get("web") or {}
        title = web.get("title")
        if title:
            chunks.append(title)
    return {"text": text, "chunks": chunks, "searches": searches}, None


def extract(payload, name, roc_year, roc_month):
    """
    從一份回應裡取公布日，並標記可信度來源。回傳 (date, source, title, url) 或 None。
    url 是模型自報的出處，可能為 None（見 _MODEL_URL 的警告）。

    先問「真實檢索文字」（groundingChunks 標題），再問「模型合成文字」——順序就是
    信任順序，不是效能考量。兩塊都要通過同一套 revlib.parse（窗過濾＋名稱錨點），
    模型講的日期沒有任何特權。

    最後再過 title_year_conflict：佐證文字若講的是「別年的同月份」就退回 None。
    這道在這裡比在 yahoo/google 更必要——模型很擅長寫出語氣肯定但年份錯掉的句子。
    """
    for blob, src in ((("\n".join(payload["chunks"])), SRC_CHUNK),
                      (payload["text"], SRC_TEXT)):
        if src == SRC_CHUNK and not revlib._anchor_offsets(
                blob, name, roc_year, roc_month):
            # ⚠️ chunk 這條路一定要錨點命中才算數。blob 是把「一次任務 4~11 次搜尋」
            # 的所有來源標題串起來的，裡面混著別家公司的結果；revlib.parse 在錨點沒中
            # 時會退回「窗內第一個日期」，而 title_year_conflict 只擋得掉「錯年同月」。
            # 不擋的話，台塑 109/1 的任務可能吃到「南亞 109年1月營收…2020/02/10」，
            # 還被標成 m_src（高信任層）——那比標成 m_txt 更糟。
            continue
        hit = revlib.parse(blob, name, roc_year, roc_month)
        if not hit:
            continue
        date, title = hit
        url = None
        if src == SRC_TEXT:
            # 模型回的 TITLE: 那行就是文章標題原文，比 parse 的後備截字好得多
            # （見 _MODEL_TITLE 的說明）。⚠️ 只換 provenance 字串，日期仍然是
            # revlib.parse 通過窗過濾＋名稱錨點抽出來的那個，模型沒有多任何特權。
            m = _MODEL_TITLE.search(payload["text"])
            if m:
                title = m.group(1)[:80]
            # URL 跟 TITLE 走同一條規則：⚠️ **只在 m_txt 這層取**。
            # 走 m_src 時日期來自 groundingChunks（真實檢索文字），而模型的 URL: 那行
            # 是它自己寫的、不保證就是那個 chunk 的出處；把它掛到高信任層那一列，等於
            # 讓「date 來自檢索原文」這個標記替一個低信任的網址背書。
            # groundingChunks 自己那條路也給不出可用的網址：它回的是 Google 的快取轉址
            # （vertexaisearch.cloud.google.com/grounding-api-redirect/...），有時效、
            # 過期就打不開。所以 m_src 那層寧可 url=None，誠實說「沒有出處」。
            m = _MODEL_URL.search(payload["text"])
            url = revlib.clean_url(m.group(1)) if m else None
        if revlib.title_year_conflict(title, roc_year, roc_month):
            continue
        return date, src, title, url
    return None


# --- 模型自報 URL 的驗證 ----------------------------------------------------
def verify_url(url, name=None, timeout=URL_CHECK_TIMEOUT):
    """
    確認模型回的 URL 真的打得開、而且真的是**這一檔**的報導。打不開或確認不了一律 False。

    ⚠️ 為什麼非驗不可：實測 1216 統一 109/2 那筆，模型回的
    chinatimes.com/newspapers/20200311000404-260204 格式完全正確（日期碼、版面碼
    都對得上新聞網的規則），實際抓回來卻是「404錯誤 - 中時新聞網」。日期本身另有
    旁證是對的，但出處是編的——一個 404 的網址看起來像有憑有據，比沒有出處更危險，
    而 revlib.clean_url 只擋格式、擋不了幻覺。

    取捨刻意不對稱：**確認不了就丟掉**。存到假網址的代價高（正是要防的失效模式），
    漏掉真網址的代價低（就是 NULL，跟沒有這欄之前一樣）。所以只有 2xx/3xx 才留，
    403（擋機器人）、429、5xx、逾時、連不上全部當作沒有。

    用 GET 不用 HEAD：實測不少新聞站對 HEAD 直接回 403。只讀狀態碼、不讀 body。

    ⚠️ 200 不等於「這是本檔的報導」。實測：模型給 4113 聯上 110/5 的網址
    news.cnyes.com/news/id/4659779 是真的（HTTP 200、16 萬字元），但那篇的標題是
    「新復興5月營收0.47億元年減23.47% | 鉅亨網」——真實存在、屬於別家公司。只看
    狀態碼會放行，而 url 這一欄的全部用途就是點開回到**這一筆**的原文。
    所以給了 name 就順便讀 body 找公司名（請求都已經發出去了，多讀幾 KB 很便宜）。

    ⚠️ 仍然驗不了「是不是**這個月份**的報導」：新聞標題不一定寫年月，硬比會把好的
    也擋掉。所以 gemini 的 url 就算通過驗證，可信度仍然低於 yahoo 從 SERP 的
    <a href> 讀出來的那種。
    """
    if not url:
        return False
    # ⚠️ 光禿禿的首頁不可能是「某公司某月的公告」，當出處毫無用處，先擋掉再說。
    # 實測 2906 高林 111/9 那筆模型給的是 https://www.masterlink.com.tw/（元富證券
    # 首頁）——它當然打得開，於是通過了「網址活著」這道檢查，卻證明不了任何事。
    # 擋在發請求之前：省一次 HTTP，也省得被自己的檢查騙過。
    if urllib.parse.urlparse(url).path.strip("/") == "":
        return False
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={"User-Agent": URL_CHECK_UA,
                     "Accept": "text/html", "Accept-Language": "zh-TW,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if not 200 <= getattr(resp, "status", 0) < 400:
                return False
            if not name:
                return True
            try:
                body = resp.read(URL_CHECK_BYTES).decode("utf-8", "replace")
            except Exception:
                # body 讀不到是「確認不了名字」，不是「這頁不存在」——狀態碼那關已經
                # 過了，不該因此把一個好網址判死。
                return True
            return name in body
    except Exception:
        # ⚠️ 一律吞掉。驗證是附屬動作，絕不可以讓一筆好好的任務因為它炸掉。
        return False


# --- 判準（批次與審核模式共用）----------------------------------------------
def judge(task, payload):
    """
    把一份**已經拿到的**回應判成回報用的 result。不打 API、不碰佇列。

    ⚠️ 為什麼要獨立成一個函式：批次模式（crawl_task）與審核模式（review_one）必須用
    同一份判準。審核模式印給人看的 worker_verdict 若跟批次真的會回報的東西不一樣，
    審核者就是在對著另一套規則做判斷——那比沒有審核更糟（看起來把過關了）。
    所以 crawl_task 不再自己寫一遍判斷，只呼叫這裡。

    回傳 (result, evidence)：
      result   = {id, status, date?, source?, title?, url?}，status ∈ success|failed
      evidence = {extracted, url_check}，只給審核模式用（批次模式丟掉）
        extracted = extract() 原本抽到的東西，⚠️ **即使 result 判 failed 也照樣帶著**：
                    「模型講了哪個日期、給了哪個假網址」正是審核者要看的，result 依規則
                    不能帶（見下面 URL 那段），只能走這條路出去。
        url_check = True 驗過活著／False 驗過打不開／None 沒有 url 可驗。
                    ⚠️ None 與 False 不可以混：前者是模型誠實回 NONE，後者是它編了
                    一個死網址，稽核時是兩件不同的事。
    """
    name = task["name"]
    ry, rm = task["roc_year"], task["roc_month"]
    hit = extract(payload, name, ry, rm)
    if not hit:
        # 模型確實搜了、正常回話了、仍無窗內日期 → 這才是真的 failed。
        return ({"id": task["id"], "status": "failed"},
                {"extracted": None, "url_check": None})
    date, src, title, url = hit
    ev = {"extracted": {"date": date, "source": src, "title": title, "url": url},
          "url_check": None}
    if url:
        ev["url_check"] = verify_url(url, name=name)
        if not ev["url_check"]:
            print(f"  ⚠️ 模型給的 URL 打不開，不存：{url}", file=sys.stderr)
            # ⚠️ 2026-08-27 收緊：m_txt（模型合成文字）這層本來就是三支 worker 裡
            # 信任度最低的一層，URL 是它唯一自己交出來、還算能查證的佐證——那個
            # 佐證也是假的，代表這篇引用整個站不住腳，不能再像以前那樣「只清
            # url、日期照留」。整筆當 failed，跟其他來源判 failed 一致看待。
            # m_src 不受影響：那條路的 url 依 extract() 的規則恆為 None，不會
            # 走到這裡；這裡仍判斷 src 是為了不讓這條規則意外波及 m_src。
            if src == SRC_TEXT:
                return {"id": task["id"], "status": "failed"}, ev
            url = None
    return ({"id": task["id"], "status": "success",
             "date": date, "source": src, "title": title, "url": url}, ev)


# --- 單筆任務 ---------------------------------------------------------------
def crawl_task(task, backend, model, budget):
    """
    回傳 (result, fatal)：
      result = {id, status, date?, source?, title?, url?}，status ∈ success|failed|rate_limited
      fatal  = 是否該立刻停掉整個 worker（設定錯誤，重試沒有意義）

    budget 是 Budget 實例；呼叫前先問它還能不能花，回來後把實際發出的搜尋次數記進去
    （⚠️ Gemini 3 按 search 計費，一個 prompt 可能發多次，見模組開頭）。
    """
    budget.attempt()          # ⚠️ 呼叫前就記，成功與否都算（見 Budget.attempt）
    payload, err = call_gemini(
        build_prompt(task["stock_id"], task["name"],
                     task["roc_year"], task["roc_month"]), backend, model=model)
    if err == ERR_FATAL:
        return {"id": task["id"], "status": "rate_limited"}, True
    if err is not None:
        # ERR_RETRY / ERR_NOSEARCH 都放回佇列。⚠️ ERR_NOSEARCH 尤其不可以當 failed。
        return {"id": task["id"], "status": "rate_limited"}, False

    budget.spend(len(payload["searches"]))
    result, _ = judge(task, payload)
    return result, False


# --- 花費上限 ---------------------------------------------------------------
class Budget:
    """
    累計「已發出的搜尋次數」並在超過上限時喊停。

    為什麼一定要有：另外兩支 worker 跑錯了只是浪費時間，這支跑錯了是刷 Google Cloud 帳單。
    上限刻意預設得小（見 --max-searches），要跑大批必須顯式加大——寧可多打一次指令，
    也不要半夜一個迴圈把額度燒穿還繼續往下刷。
    """

    def __init__(self, limit, free_quota=0, unit_price=DEFAULT_UNIT_PRICE,
                 max_calls=DEFAULT_MAX_CALLS):
        self.limit = limit
        self.used = 0                  # 已發出的搜尋次數（只有成功回應才數得到）
        self.calls = 0                 # ⚠️ 呼叫嘗試次數，成功與否都數
        self.max_calls = max_calls
        self.free_quota = free_quota
        self.unit_price = unit_price

    def attempt(self):
        """每次呼叫 API 前都要記一筆，⚠️ 不論結果。

        為什麼不能只數搜尋次數：spend() 只在拿到回應時才累加，所以任何「持續失敗」的
        情境（gcloud 沒裝、配額用完一直 429、模型每次都不搜）下 used 永遠是 0，
        exhausted() 永遠 False，而 run() 的 while True 沒有別的出口——這道保險
        剛好在它唯一該擋的場景失效。ERR_NOSEARCH 更糟：那是【已計費的 200 回應】，
        會變成無上限的付費迴圈。
        """
        self.calls += 1

    def spend(self, n):
        self.used += n

    def exhausted(self):
        if self.limit > 0 and self.used >= self.limit:
            return True
        return self.max_calls > 0 and self.calls >= self.max_calls

    def note(self):
        billable = max(0, self.used - self.free_quota)
        cost = billable * self.unit_price
        cap = self.limit if self.limit > 0 else "∞"
        ccap = self.max_calls if self.max_calls > 0 else "∞"
        tail = f"、呼叫 {self.calls}/{ccap} 次"
        if self.free_quota:
            return (f"搜尋 {self.used}/{cap} 次"
                    f"（免費 {self.free_quota}/月，超出約 ${cost:.2f}）{tail}")
        return f"搜尋 {self.used}/{cap} 次（約 ${cost:.2f}）{tail}"


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
        # engine=gemini：只租 requeue-failed?engine=gemini 轉過來的任務，
        # 不跟 yahoo/google 兩支 worker 搶同一批 undone（見 server.py Store.lease）。
        return self._req("POST", f"/lease?n={n}&worker={self.worker_id}&engine=gemini")

    def report(self, results):
        return self._req("POST", "/result",
                         {"worker": self.worker_id, "results": results})


# --- 審核模式：一次一筆，把證據交出來（見模組開頭 4.）----------------------
def review_one(client, backend, model, budget):
    """
    租一筆 gemini 任務、打一次 API、把整份證據組成 dump，⚠️ **絕不回報**。

    回傳 (dump, exit_code)。「不回報」是這個模式唯一不可以寫錯的地方：一旦它自己
    回報了，審核就退化成事後補章（日期已經在 DB 裡了），整個模式的意義歸零。
    沒回報的租約會在 server 的 LEASE_TTL（600s）後被惰性回收放回 undone——所以
    審核中斷、當掉、關掉終端機都是安全的，最壞的結果是這一筆下次重跑（要再付一次錢）。

    dump 的 recommend 是給呼叫端（skill）看的行動建議，不是判斷：
      review = 有東西可審（worker_verdict 告訴你批次模式會怎麼判）
      retry  = 這一筆根本沒查過（模型沒搜／連線失敗），沒有東西可審，該放回
      abort  = 設定層級的錯誤，每一筆都會重演，loop 要停下來讓人去修
      empty  = gemini 佇列空的（記得先 requeue-failed?engine=gemini）
    """
    resp = client.lease(1)
    dump = {"mode": "review-one", "worker": getattr(client, "worker_id", None),
            "lease_ttl": resp.get("lease_ttl"), "task": None, "error": None,
            "recommend": "empty", "searches": [], "chunks": [], "text": "",
            "extracted": None, "url_check": None, "worker_verdict": None,
            "report_if_approved": None, "searches_used": 0,
            "cost_note": budget.note()}
    tasks = resp.get("tasks") or []
    if not tasks:
        # ⚠️ 佇列空的時候不可以打 API：這支是按搜尋次數計費的，空轉一次就是白花錢。
        return dump, EXIT_EMPTY

    task = tasks[0]
    dump["task"] = task
    budget.attempt()          # ⚠️ 呼叫前就記，成功與否都算（見 Budget.attempt）
    payload, err = call_gemini(
        build_prompt(task["stock_id"], task["name"],
                     task["roc_year"], task["roc_month"]), backend, model=model)
    if err is not None:
        dump["error"] = err
        dump["worker_verdict"] = "rate_limited"
        dump["cost_note"] = budget.note()
        # ⚠️ ERR_NOSEARCH 走的是 retry 不是 reject：沒查過不可以記成「查過但沒有」
        # （見模組開頭 1.）。這條在審核模式更容易寫錯——證據看起來「空的」很像
        # 一筆該否決的結果，但它其實是一筆還沒發生的查詢。
        dump["recommend"] = "abort" if err == ERR_FATAL else "retry"
        return dump, EXIT_ABORT if err == ERR_FATAL else EXIT_REVIEW

    budget.spend(len(payload["searches"]))
    result, ev = judge(task, payload)
    dump.update({
        "recommend": "review",
        "searches": payload["searches"],
        "chunks": payload["chunks"],
        "text": payload["text"],
        "extracted": ev["extracted"],
        "url_check": ev["url_check"],
        "worker_verdict": result["status"],
        # ⚠️ 核准時要送出去的那一份，逐字就是 judge() 算出來的 result。審核者不重打
        # 任何欄位（見模組開頭 4. 的第一條界線）。worker 判 failed 時這裡是 None
        # ——那代表「沒有可核准的內容」，不是「還沒填」。
        "report_if_approved": result if result["status"] == "success" else None,
        "searches_used": len(payload["searches"]),
        "cost_note": budget.note(),
    })
    return dump, EXIT_REVIEW


def save_pending(path, dump):
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(dump, f, ensure_ascii=False, indent=2)


def load_pending(path):
    if not os.path.exists(path):
        sys.exit(f"⚠️ 找不到 {path}：--review-verdict 只能接在 --review-one 之後。"
                 f"（回報完會刪掉，避免同一筆被回報兩次）")
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def _last_note(csv_path):
    """CSV 最後一列的 note，沒有檔／沒有列就 None。給套版偵測用（見 review_verdict）。"""
    if not os.path.exists(csv_path):
        return None
    with io.open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return (rows[-1].get("note") if rows else None)


def _append_review_row(csv_path, dump, verdict, note):
    """把這一筆的判斷追加進版控的稽核檔。資料欄位一律從 dump 填，不從審核者手上拿。"""
    task = dump["task"]
    ex = dump.get("extracted") or {}
    row = {"stock_id": task["stock_id"], "roc_year": task["roc_year"],
           "roc_month": task["roc_month"],
           "announce_date": ex.get("date") or "",
           "source": ex.get("source") or "",
           "url": ex.get("url") or "",
           # 模型實際讀了哪些網域。DB 沒有欄位裝它，這是唯一的留存處。
           "chunks": ";".join(dump.get("chunks") or []),
           "verdict": verdict, "note": note}
    exists = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    with io.open(csv_path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)
    return row


def review_verdict(client, dump, verdict, note, csv_path=REVIEW_CSV):
    """
    把審核者的判斷落地：回報給 server，並把理由追加進 data/gemini_review.csv。

    ⚠️ 審核者只交出 verdict 與 note，**所有資料欄位都由程式從 dump 填**。approve 送出去
    的是 review_one 存下來的 report_if_approved（judge 算的那一份），所以審核只能
    【否決】一個 success，不能無中生有一個 success——手打日期的路徑根本不存在。

    三道拒絕（都用 ReviewError，不猜意思）：
      1. worker 判 failed 的那一筆沒有可核准的內容。允許核准等於讓審核者繞過 URL
         幻覺那條規則，而且日期會變成人打的。要放行就去改規則，不要在單筆繞過。
      2. 缺 note。判斷是主觀的，沒有理由的章沒有稽核價值（與 title_review.csv 同一條）。
      3. note 與上一列一字不差。⚠️ 這是擋套版：2026-08-25 的 title 審核就是用共用
         note 蓋了十萬筆章、後來全部撤銷。逐筆讀過的人不會寫出跟上一筆一樣的理由。

    ⚠️ 先回報、後寫 CSV：回報失敗（server 沒跑／掉線）時不留下「已核准」的紀錄。
    稽核檔寧可漏一列（重跑就補回來），也不可以宣稱一件沒發生的事（那列會一直說謊）。
    """
    if verdict not in REVIEW_VERDICTS:
        raise ReviewError(f"verdict='{verdict}' 不合法；可用：{list(REVIEW_VERDICTS)}")
    task = dump.get("task")
    if not task:
        raise ReviewError("這份 dump 沒有任務（上一次 --review-one 租不到任務）。")

    if verdict == "retry":
        # 沒查過的那一筆不算「審過」，不留稽核紀錄——留了就是把一次沒發生的判斷
        # 記成判斷過。放回佇列，下次重打。
        return client.report([{"id": task["id"], "status": "rate_limited"}]), None

    note = (note or "").strip()
    if not note:
        raise ReviewError("缺 --note。判斷是主觀的，一定要寫下【這一筆】的理由。")
    if note == _last_note(csv_path):
        raise ReviewError("--note 與上一列一字不差。逐筆讀過的理由不會長一樣；"
                          "共用理由的稽核檔沒有價值（見本函式 docstring 第 3 條）。")

    if verdict == "approve":
        body = dump.get("report_if_approved")
        if not body:
            raise ReviewError(
                f"這一筆 worker 判 {dump.get('worker_verdict')}，沒有可核准的內容"
                f"（URL 驗證沒過／根本沒抽到窗內日期）。只能 reject 或 retry；"
                f"要放行請去改判準，不要在單筆繞過。")
        results = [body]
    else:
        # gemini 是升級鏈最後一棒 → server 會落 state='failed' 當終點。
        results = [{"id": task["id"], "status": "failed"}]

    counts = client.report(results)
    row = _append_review_row(csv_path, dump, verdict, note)
    return counts, row


# --- 主迴圈 -----------------------------------------------------------------
def run(args, client, budget):
    batches_done = 0
    tally = {"success": 0, "failed": 0, "rate_limited": 0, SRC_CHUNK: 0, SRC_TEXT: 0}
    while True:
        if budget.exhausted():
            print(f"已達上限（{budget.note()}），結束。"
                  f"要繼續請調高 --max-searches / --max-calls。")
            return tally

        try:
            resp = client.lease(args.batch)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  lease 失敗，10s 後重試：{e!r}")
            time.sleep(10)
            continue
        batch = resp.get("tasks", [])
        if not batch:
            print(f"  gemini 佇列已空/無可租任務"
                  f"（記得先 requeue-failed?engine=gemini），{args.idle_sleep}s 後再試。")
            time.sleep(args.idle_sleep)
            continue

        results = []
        consec_rl = 0
        stopped = fatal = False
        for task in batch:
            if budget.exhausted():
                print(f"  已達上限，本批剩餘放回。{budget.note()}")
                stopped = True
                break
            r, fatal = crawl_task(task, args.backend, args.model, budget)
            results.append(r)
            tally[r["status"]] += 1
            if r["status"] == "success":
                tally[r["source"]] += 1
            tag = {"success": "✓", "failed": "—", "rate_limited": "×"}[r["status"]]
            extra = f" {r.get('date','')} [{r.get('source','')}]" if r["status"] == "success" else ""
            print(f"  {tag} {task['stock_id']} {task['name']} "
                  f"{task['roc_year']}/{task['roc_month']}{extra}")

            if fatal:
                print("  ⚠️ 設定層級的錯誤（金鑰／權限／API 未啟用），立刻停止，"
                      "剩餘任務放回。請照 README 檢查 Console 設定。", file=sys.stderr)
                stopped = True
                break

            if r["status"] == "rate_limited":
                consec_rl += 1
                time.sleep(min(2 ** consec_rl, 60) + random.uniform(0, 2))
                if consec_rl >= args.rl_threshold:
                    print(f"  ⚠️ 連續 {consec_rl} 筆 rate_limited，提早收批"
                          f"（多半是 429 配額或限速），最多等 {args.block_sleep:.0f}s。")
                    stopped = True
                    break
            else:
                consec_rl = 0
                time.sleep(args.delay + random.uniform(0, args.jitter))

        if stopped:
            done_ids = {r["id"] for r in results}
            for task in batch:
                if task["id"] not in done_ids:
                    results.append({"id": task["id"], "status": "rate_limited"})

        try:
            applied = client.report(results)
            print(f"  回報：{applied.get('applied')}   {budget.note()}")
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"  回報失敗（租約逾時後 server 會自動回收）：{e!r}")

        if fatal:
            return tally
        batches_done += 1
        if args.once or (args.max_batches and batches_done >= args.max_batches):
            print(f"完成 {batches_done} 批，結束。")
            return tally
        if stopped and consec_rl >= args.rl_threshold:
            print(f"  退避 {args.block_sleep:.0f}s。")
            time.sleep(args.block_sleep)


def summarize(tally):
    """
    印實驗結論用的一行摘要。m_src / m_txt 的比例是本 worker 存在的理由——
    m_txt 佔多數就代表「日期多半是模型講的、不是查來的」（見模組開頭 3.）。
    """
    ok = tally["success"]
    print(f"\n本次：success {ok}／failed {tally['failed']}／"
          f"rate_limited {tally['rate_limited']}")
    if ok:
        print(f"  來源分級：{SRC_CHUNK}（檢索原文）{tally[SRC_CHUNK]} 筆、"
              f"{SRC_TEXT}（模型合成）{tally[SRC_TEXT]} 筆"
              f" → 合成佔 {tally[SRC_TEXT] / ok * 100:.0f}%")
        print("  ⚠️ m_txt 那批未經外部驗證，併入主資料前請先跟 yahoo/MOPS 重疊區對照"
              "（見 README「資料品質」對 google 批的同類檢查）。")


def main():
    revlib.load_env()          # 先載 .env：REVSWARM_TOKEN 與 GOOGLE_CLOUD_PROJECT
    ap = argparse.ArgumentParser(
        description="revswarm gemini_worker（Gemini API + Grounding with Google Search）")
    ap.add_argument("--server", required=True, help="server base URL, 如 http://1.2.3.4:8000")
    ap.add_argument("--token", default=os.environ.get("REVSWARM_TOKEN"),
                    help="Bearer token；預設讀 .env / 環境變數 REVSWARM_TOKEN")
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    help="GCP 專案 ID；預設讀 .env / 環境變數 GOOGLE_CLOUD_PROJECT。"
                         "額度算在這個專案的 Google Cloud 帳單上")
    ap.add_argument("--location", default=VERTEX_LOCATION,
                    help=f"Vertex location（預設 {VERTEX_LOCATION}）")
    ap.add_argument("--gcloud", default=None,
                    help="gcloud 執行檔路徑；預設找 PATH 與 ~/google-cloud-sdk/bin")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"預設 {DEFAULT_MODEL}。⚠️ gemini-2.5-* / 2.0-flash 對新專案已 404 "
                         f"下架（no longer available to new users），退不回舊型號")
    ap.add_argument("--worker-id", default=None, help="預設 hostname-pid-m")
    # ⚠️ 預設值刻意保守：這支會花錢，寧可多跑幾次也不要一個迴圈燒穿額度。
    ap.add_argument("--max-searches", type=int, default=300,
                    help="本次最多發出幾次「搜尋」就停（0=不限）。⚠️ 不是任務數："
                         "計費按搜尋次數，而【實測一個任務會發 4~11 次搜尋、平均約 7 次】"
                         "（2026-08-24 於 Vertex 量測），所以 300 大約只夠 40 筆任務。"
                         "預設刻意設小；要跑整批請顯式加大")
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                    help=f"最多呼叫 API 幾次就停（0=不限，預設 {DEFAULT_MAX_CALLS}）。"
                         f"⚠️ 與 --max-searches 是兩道獨立保險：搜尋次數只有拿到回應"
                         f"才數得到，持續失敗時擋不住無限迴圈")
    ap.add_argument("--free-quota", type=int, default=0,
                    help="每月免費搜尋次數，只用來把 log 裡的花費估算算對。"
                         "⚠️ 預設 0——Vertex 沒有免費 grounding 額度"
                         "（每月 5,000 次那個是 AI Studio 的，本 worker 不走那道門）")
    ap.add_argument("--unit-price", type=float, default=DEFAULT_UNIT_PRICE,
                    help=f"每次搜尋的單價美元（預設 {DEFAULT_UNIT_PRICE}）。"
                         f"Vertex 的 grounding 定價請以實際帳單校正")
    ap.add_argument("--batch", type=int, default=10, help="每次租多少筆")
    ap.add_argument("--delay", type=float, default=1.0, help="每筆任務間基礎延遲秒")
    ap.add_argument("--jitter", type=float, default=1.0, help="每筆延遲的隨機抖動上限秒")
    ap.add_argument("--rl-threshold", type=int, default=3,
                    help="連續幾筆 rate_limited 就提早收批")
    ap.add_argument("--block-sleep", type=float, default=120.0,
                    help="收批後退避秒數（429 多半是每分鐘限速，兩分鐘就夠）")
    ap.add_argument("--idle-sleep", type=float, default=30.0, help="佇列空時的等待秒數")
    ap.add_argument("--once", action="store_true", help="只跑一批就結束（試跑用）")
    ap.add_argument("--max-batches", type=int, default=0, help="跑幾批後結束(0=不限)")
    # --- 審核模式（見模組開頭 4.）---
    ap.add_argument("--review-one", action="store_true",
                    help="審核模式：租 1 筆、打一次 API、把整份證據印成 JSON，"
                         "⚠️ 不回報。exit 0=有東西可審／3=佇列空／4=設定錯該停")
    ap.add_argument("--review-verdict", choices=REVIEW_VERDICTS, default=None,
                    help="把 --review-one 那一筆的判斷落地："
                         "approve=讓日期進 DB／reject=回報 failed／"
                         "retry=沒查過，放回佇列。不打 API、不用 --project")
    ap.add_argument("--note", default="",
                    help="這一筆的判斷理由（approve/reject 必填，會進版控的稽核檔）")
    ap.add_argument("--review-csv", default=REVIEW_CSV,
                    help=f"逐筆判斷的稽核檔（預設 {REVIEW_CSV}）")
    ap.add_argument("--pending", default=REVIEW_PENDING,
                    help=f"兩個審核模式之間的交接檔（預設 {REVIEW_PENDING}）")
    args = ap.parse_args()

    # 審核模式共用一個固定的 worker_id：它會被寫進 tasks.worker_id，看板上該看得出
    # 「這一筆是審過才進來的」，而不是一個隨機 pid。
    review = args.review_one or args.review_verdict
    worker_id = args.worker_id or (
        "claude-review" if review else f"{socket.gethostname()}-{os.getpid()}-m")
    client = Client(args.server, args.token, worker_id)

    if args.review_verdict:
        # ⚠️ 這條路不打 API，所以【不建 backend】：落地一個判斷不該因為這台機器
        # 沒設 --project / 沒裝 gcloud 而失敗。
        dump = load_pending(args.pending)
        try:
            counts, row = review_verdict(client, dump, args.review_verdict,
                                         args.note, args.review_csv)
        except ReviewError as e:
            sys.exit(f"⚠️ {e}")
        # 回報成功才刪交接檔：失敗時留著，修好 server 再跑一次同一道指令即可。
        os.remove(args.pending)
        t = dump["task"]
        print(f"{t['stock_id']} {t['name']} {t['roc_year']}/{t['roc_month']} "
              f"→ {args.review_verdict}   server: {counts}")
        if row:
            print(f"  已記入 {args.review_csv}：{row['announce_date'] or '(無日期)'} "
                  f"{row['source']}  chunks={row['chunks'] or '(無)'}")
            print(f"  ⚠️ 還沒蓋章。要蓋 verified='claude' 請跑："
                  f"python3 -m mops.stamp_verified --from gemini-review "
                  f"--server {args.server}")
        return

    args.backend = make_backend(args)
    budget = Budget(args.max_searches, args.free_quota, args.unit_price,
                    args.max_calls)

    if args.review_one:
        dump, code = review_one(client, args.backend, args.model, budget)
        if dump["task"] is not None:
            save_pending(args.pending, dump)
        print(json.dumps(dump, ensure_ascii=False, indent=2))
        sys.exit(code)

    print(f"gemini_worker {worker_id} → {args.server}  {args.backend.describe()}  "
          f"model={args.model} batch={args.batch} "
          f"max_searches={args.max_searches or '∞'}")
    try:
        tally = run(args, client, budget)
    except KeyboardInterrupt:
        print("\n收到中斷，結束。")
        return
    if tally:
        summarize(tally)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gemini_worker（Vertex AI 後端）的分類邏輯、信任分級、花費上限與主迴圈回歸測試。純標準庫 unittest：
不連網、不打 Gemini API、不真的睡、不花錢。

作法沿用 test_google_worker.py：替換 gemini_worker 模組命名空間裡的 time / random /
call_gemini / urlopen 為假物件。重點驗證的是「絕不可以寫錯的那幾條」：

  - ⚠️ 模型沒發出搜尋（webSearchQueries 空）→ rate_limited，**絕不是 failed**。
    這是本 worker 最容易靜默污染資料的一條：把「沒查過」記成「查過但沒有」。
  - ⚠️ 金鑰／權限錯 → fatal，立刻停整個 worker，也不記成 failed。
  - 信任分級 m_src（檢索原文）優先於 m_txt（模型合成），且兩者都要通過窗過濾。
  - title_year_conflict 擋掉「語氣肯定但年份錯掉」的合成句。
  - ⚠️ 花費上限算的是「搜尋次數」不是任務數（Gemini 3 一個 prompt 可能發多次搜尋）。

跑法（在 repo 根目錄）：  python3 -m unittest tests.test_gemini_worker
"""

import contextlib
import io
import json
import unittest
import urllib.error

from worker import gemini_worker as gw


class _FakeTime:
    def __init__(self):
        self.slept = []

    def sleep(self, s):
        self.slept.append(round(s, 3))


class _FakeRandom:
    @staticmethod
    def uniform(a, b):
        return 0.0


def _payload(text="", chunks=(), searches=("q",)):
    return {"text": text, "chunks": list(chunks), "searches": list(searches)}


def _titled(name="台積電", roc_year=109, roc_month=1, year=None):
    """組一段含精確名稱錨點 + 窗內日期的假標題（供 revlib.parse 命中）。
    year 可指定成別年，用來測 title_year_conflict。"""
    y = year if year is not None else roc_year
    return (f"【公告】{name} {y}年{roc_month}月營收10.17億元 年增-15.24%\n"
            f"2020年2月10日 中央社")


TASK = {"id": 1, "stock_id": "2330", "name": "台積電", "roc_year": 109, "roc_month": 1}


# --- prompt ----------------------------------------------------------------
class TestBuildPrompt(unittest.TestCase):
    def test_carries_both_year_forms_and_avoids_source_hints(self):
        """民國年與西元年都要在 prompt 裡（不像 yahoo/google 分兩次查，那只是把成本乘二），
        且沿用另外兩支的禁忌：不指定 moneydj 之類的來源去限定搜尋。"""
        p = gw.build_prompt("2330", "台積電", 109, 1)
        self.assertIn("109", p)
        self.assertIn("2020", p)
        self.assertIn("2330 台積電", p)
        self.assertNotIn("moneydj", p.lower())
        self.assertIn("逐字複製", p)          # 要原始標題，錨點才對得上
        # ⚠️ 刻意不叫模型「請用 Google 搜尋」：宣告了工具就會搜，那句多半多餘，
        # 而搜尋次數就是錢。沒搜到的情況由 nosearch 那條戒律接住（不會污染資料）。
        self.assertNotIn("搜尋查證", p)
        # 這句才是吃重的：擋掉「次月10日前申報」這種常識推算——猜出來的日期會通過窗過濾
        self.assertIn("不可以推測", p)


# --- 信任分級 --------------------------------------------------------------
class TestExtract(unittest.TestCase):
    def test_chunk_title_wins_and_is_marked_m_src(self):
        """日期在 groundingChunks 標題裡（真實檢索文字）→ source=m_src，優先於模型文字。"""
        p = _payload(text="台積電 109年1月營收於 2020年2月11日 公布。",
                     chunks=[_titled()])
        date, src, _, _u = gw.extract(p, "台積電", 109, 1)
        self.assertEqual(src, gw.SRC_CHUNK)
        self.assertEqual(date, "2020-02-10")     # 取 chunk 的，不是模型文字的 02-11

    def test_model_text_only_is_marked_m_txt(self):
        """groundingChunks 只給網域名（Gemini API 的常態）→ 只能靠模型文字，標 m_txt。"""
        p = _payload(text=_titled(), chunks=["moneydj.com", "cna.com.tw"])
        date, src, _, _u = gw.extract(p, "台積電", 109, 1)
        self.assertEqual(src, gw.SRC_TEXT)
        self.assertEqual(date, "2020-02-10")

    def test_uses_model_title_line_as_provenance(self):
        """⚠️ raw_title 要存文章標題原文，不是 prompt 鷹架。

        實戰踩到：台塑 111/10 的真實回應裡標題就在 TITLE: 那行，但名稱錨點對不上
        （「台塑四寶…」的「四」不是空白或數字），revlib.parse 走後備路徑截日期前後
        ±40 字元 → raw_title 存成 "DATE: 2022-11-08 TITLE: 台塑"。而整個 repo 拿
        raw_title 當證據用（parse_revenue / yoy_scope / title_year_conflict）。
        """
        p = _payload(text=("DATE: 2022-11-08\n"
                           "TITLE: 台塑四寶10月營收3降1升台塑化獨成長\n"
                           "URL: https://ctee.com.tw/news/industry/750438.html"))
        date, src, title, _u = gw.extract(p, "台塑", 111, 10)
        self.assertEqual(date, "2022-11-08")
        self.assertEqual(src, gw.SRC_TEXT)
        self.assertEqual(title, "台塑四寶10月營收3降1升台塑化獨成長")
        self.assertNotIn("DATE:", title)

    def test_model_title_does_not_override_chunk_hit(self):
        """m_src 的 title 來自檢索原文，不該被模型自己寫的 TITLE 蓋掉。"""
        p = _payload(text="TITLE: 模型自己寫的標題",
                     chunks=[_titled()])
        _, src, title, _u = gw.extract(p, "台積電", 109, 1)
        self.assertEqual(src, gw.SRC_CHUNK)
        self.assertNotIn("模型自己寫的", title)

    def test_falls_back_when_no_title_line(self):
        """模型沒照格式回 TITLE: 時，仍沿用 revlib.parse 的後備截字，不要炸掉。"""
        p = _payload(text=_titled())
        date, _, title, _u = gw.extract(p, "台積電", 109, 1)
        self.assertEqual(date, "2020-02-10")
        self.assertTrue(title)

    def test_out_of_window_date_is_rejected(self):
        """⚠️ 換了資料源不代表窗過濾放寬：模型講的日期照樣要落在次月 1~15。"""
        p = _payload(text="台積電 109年1月營收於 2020年3月20日 公布。")
        self.assertIsNone(gw.extract(p, "台積電", 109, 1))

    def test_wrong_year_evidence_is_rejected(self):
        """佐證文字講的是別年的同月份 → 退回 None。模型很擅長寫出語氣肯定但年份錯的句子。"""
        p = _payload(text=_titled(year=114) + "\n2020年2月10日")
        self.assertIsNone(gw.extract(p, "台積電", 109, 1))


# --- HTTP 分類 --------------------------------------------------------------
class _FakeResp:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _api_reply(text="", chunk_titles=(), searches=("台積電 2020年1月營收",)):
    meta = {"webSearchQueries": list(searches),
            "groundingChunks": [{"web": {"uri": "u", "title": t}} for t in chunk_titles]}
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "groundingMetadata": meta}]}


class _HTTPPatch:
    """暫時換掉 gemini_worker 看到的 urlopen。"""

    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        self.orig = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = self.fn

    def __exit__(self, *a):
        gw.urllib.request.urlopen = self.orig
        return False


def _backend():
    """測試用的 Vertex 後端：不真的叫 gcloud 拿 token。"""
    b = gw.VertexBackend("test-proj")
    b.token = lambda force=False: "tok"
    return b


def _http_error(code, body=b""):
    def raise_it(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://x", code, "err", {}, io.BytesIO(body))
    return raise_it


class TestCallGemini(unittest.TestCase):
    def test_success_returns_text_chunks_and_searches(self):
        with _HTTPPatch(lambda req, timeout=None: _FakeResp(
                _api_reply(text="hi", chunk_titles=["cna.com.tw"],
                           searches=["a", "b"]))):
            p, err = gw.call_gemini("q", _backend())
        self.assertIsNone(err)
        self.assertEqual(p["text"], "hi")
        self.assertEqual(p["chunks"], ["cna.com.tw"])
        self.assertEqual(len(p["searches"]), 2)     # 計費單位，不是 1

    def test_empty_web_search_queries_is_nosearch(self):
        """⚠️ 模型憑記憶回答、根本沒去搜 → ERR_NOSEARCH。這條是本 worker 的核心戒律，
        對應 google_worker 的「頁面沒有 #search 容器就不是 SERP」。"""
        with _HTTPPatch(lambda req, timeout=None: _FakeResp(
                _api_reply(text="2020年2月10日", searches=[]))):
            p, err = gw.call_gemini("q", _backend())
        self.assertIsNone(p)
        self.assertEqual(err, gw.ERR_NOSEARCH)

    def test_no_candidates_is_retry(self):
        """安全過濾等擋掉整個回應 → 放回重試，不是查不到。"""
        with _HTTPPatch(lambda req, timeout=None: _FakeResp({"promptFeedback": {}})):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_RETRY)

    def test_401_is_fatal(self):
        with _HTTPPatch(_http_error(401)):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_FATAL)

    def test_403_is_fatal_only_when_message_says_permanent(self):
        """API 沒啟用、權限不足這類每一筆都會重演 → fatal。"""
        body = b'{"error":{"message":"Agent Platform API has not been used in project x"}}'
        with _HTTPPatch(_http_error(403, body)):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_FATAL)

    def test_bare_403_is_retry_not_fatal(self):
        """⚠️ 實戰踩到：Vertex 打過一次空 body 的 403 Forbidden，下一次同樣請求就 200。
        若照 401 一樣直接停掉整個 worker，一次抖動就收工。"""
        with _HTTPPatch(_http_error(403)):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_RETRY)

    def test_bad_key_400_is_fatal_but_other_400_retries(self):
        """400 的兩種意義不同：金鑰無效每一筆都會重演（fatal），其餘偶發的當可重試。"""
        with _HTTPPatch(_http_error(400, b'{"error":{"message":"API key not valid"}}')):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_FATAL)
        with _HTTPPatch(_http_error(400, b'{"error":{"message":"transient"}}')):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_RETRY)

    def test_429_and_5xx_are_retry(self):
        for code in (429, 500, 503):
            with _HTTPPatch(_http_error(code)):
                _, err = gw.call_gemini("q", _backend())
            self.assertEqual(err, gw.ERR_RETRY, f"HTTP {code} 應為 retry")

    def test_network_failure_is_retry(self):
        def boom(req, timeout=None):
            raise urllib.error.URLError("down")
        with _HTTPPatch(boom):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_RETRY)


# --- Vertex 後端 ------------------------------------------------------------
class _FakeVertex(gw.VertexBackend):
    """不真的叫 gcloud 的 VertexBackend：記錄 token() 被要求過幾次。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.token_calls = []

    def token(self, force=False):
        self.token_calls.append(force)
        return f"tok{len(self.token_calls)}"


class TestVertexBackend(unittest.TestCase):
    def test_endpoint_and_tool_shape(self):
        b = _FakeVertex("my-proj")
        url = b.endpoint("gemini-3.7-flash")
        self.assertIn("aiplatform.googleapis.com/v1/projects/my-proj/locations/global", url)
        self.assertTrue(url.endswith("/publishers/google/models/gemini-3.7-flash:generateContent"))
        # ⚠️ Vertex 是 camelCase 的 googleSearch（AI Studio 的 google_search 那道門已移除）
        self.assertEqual(gw.TOOL_SPEC, {"googleSearch": {}})

    def test_regional_location_uses_regional_host(self):
        b = _FakeVertex("p", location="us-central1")
        self.assertIn("https://us-central1-aiplatform.googleapis.com/v1/", b.endpoint("m"))

    def test_bearer_header(self):
        self.assertEqual(_FakeVertex("p").headers(), {"Authorization": "Bearer tok1"})

    def test_budget_defaults_to_no_free_quota(self):
        """⚠️ 每月 5,000 次免費是 AI Studio 的額度，Vertex 沒有——預設值算錯會低估花費。"""
        b = gw.Budget(0)
        b.spend(100)
        self.assertEqual(b.free_quota, 0)
        self.assertIn("$1.40", b.note())

    def test_401_refreshes_token_once_then_gives_up(self):
        """Vertex 的 access token 一小時過期：先強制換一把重試，換完還是 401 才 fatal。"""
        b = _FakeVertex("p")
        with _HTTPPatch(_http_error(401)):
            _, err = gw.call_gemini("q", b)
        self.assertEqual(err, gw.ERR_FATAL)
        self.assertIn(True, b.token_calls)       # 有強制刷新過

    def test_token_failure_is_fatal_not_retry(self):
        """沒跑 application-default login → 重試不會變好，直接 fatal 讓人去修。"""
        class _Broken(gw.VertexBackend):
            def token(self, force=False):
                raise RuntimeError("取不到 ADC token")
        _, err = gw.call_gemini("q", _Broken("p"))
        self.assertEqual(err, gw.ERR_FATAL)


class TestFatalMisconfiguration(unittest.TestCase):
    """設定錯誤必須 fatal。判成 retry 的話 worker 會無聲空轉——這正是 code review
    抓到的核心問題：錯誤分類太寬鬆 + budget 只在成功時累加 = 無上限的付費迴圈。"""

    def test_missing_gcloud_is_fatal_not_retry(self):
        """⚠️ FileNotFoundError 是 OSError 子類，會被 call_gemini 最後那個
        except OSError 當成 ERR_RETRY 吃掉。gcloud 沒裝時每一筆都會這樣。"""
        b = gw.VertexBackend("p", gcloud="/nonexistent/gcloud")
        with contextlib.redirect_stderr(io.StringIO()):
            _, err = gw.call_gemini("q", b)
        self.assertEqual(err, gw.ERR_FATAL)

    def test_gcloud_timeout_is_fatal_not_traceback(self):
        """subprocess.TimeoutExpired 沒人接的話會直接 traceback 出 run()。"""
        import subprocess

        class _Slow(gw.VertexBackend):
            def token(self, force=False):
                raise subprocess.TimeoutExpired("gcloud", 60)
        with contextlib.redirect_stderr(io.StringIO()):
            _, err = gw.call_gemini("q", _Slow("p"))
        self.assertEqual(err, gw.ERR_FATAL)

    def test_403_permission_denied_variants_are_fatal(self):
        """--project 打錯／缺 aiplatform.user 時 Vertex 實際回的字樣。"""
        for body in (b'{"error":{"status":"PERMISSION_DENIED"}}',
                     b'{"error":{"message":"Permission \'aiplatform.endpoints.predict\''
                     b' denied on resource"}}',
                     b'{"error":{"message":"caller does not have permission"}}'):
            with _HTTPPatch(_http_error(403, body)), \
                 contextlib.redirect_stderr(io.StringIO()):
                _, err = gw.call_gemini("q", _backend())
            self.assertEqual(err, gw.ERR_FATAL, body)

    def test_404_retired_model_is_fatal(self):
        """endpoint 是固定組出來的，404 只可能是型號打錯或已退役（2.5-* 就是）。"""
        with _HTTPPatch(_http_error(404, b'no longer available to new users')), \
             contextlib.redirect_stderr(io.StringIO()):
            _, err = gw.call_gemini("q", _backend())
        self.assertEqual(err, gw.ERR_FATAL)


class TestBudgetStopsOnPersistentFailure(unittest.TestCase):
    """⚠️ spend() 只在拿到回應時才累加，所以持續失敗時 used 永遠是 0。
    沒有呼叫次數上限的話 run() 的 while True 沒有出口。"""

    def setUp(self):
        self._call, self._time, self._random = gw.call_gemini, gw.time, gw.random
        gw.time, gw.random = _FakeTime(), _FakeRandom()

    def tearDown(self):
        gw.call_gemini, gw.time, gw.random = self._call, self._time, self._random

    def test_attempts_counted_even_when_call_fails(self):
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_RETRY)
        b = gw.Budget(0, max_calls=3)
        for _ in range(3):
            gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, b)
        self.assertEqual((b.used, b.calls), (0, 3))
        self.assertTrue(b.exhausted())

    def test_nosearch_loop_terminates(self):
        """ERR_NOSEARCH 是【已計費的 200 回應】，不擋就是無上限的付費迴圈。"""
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_NOSEARCH)
        b = gw.Budget(0, max_calls=5)
        client = _FakeClient([_tasks(3), _tasks(3), _tasks(3), _tasks(3)])
        args = _Args()
        args.once = False
        gw.run(args, client, b)          # 不會無限迴圈
        self.assertTrue(b.exhausted())

    def test_max_calls_zero_means_unlimited(self):
        b = gw.Budget(0, max_calls=0)
        b.calls = 10 ** 6
        self.assertFalse(b.exhausted())


class TestChunkPathRequiresAnchor(unittest.TestCase):
    def test_chunk_without_anchor_does_not_become_m_src(self):
        """⚠️ chunks 是把 4~11 次搜尋的所有標題串起來的，混著別家公司。
        錨點沒中時 revlib.parse 會退回「窗內第一個日期」，可能把南亞的日期
        標成台塑的 m_src（高信任層）——比標成 m_txt 更糟。"""
        p = _payload(text="", chunks=["南亞 109年1月營收 2020年2月10日 - MoneyDJ"])
        self.assertIsNone(gw.extract(p, "台塑", 109, 1))

    def test_chunk_with_correct_anchor_still_works(self):
        p = _payload(text="", chunks=["南亞 109年1月營收\n台塑 109年1月營收 2020年2月10日"])
        date, src, _, _u = gw.extract(p, "台塑", 109, 1)
        self.assertEqual((date, src), ("2020-02-10", gw.SRC_CHUNK))


class TestMakeBackend(unittest.TestCase):
    class _Args:
        project = None
        location = gw.VERTEX_LOCATION
        gcloud = None

    def test_builds_vertex_backend(self):
        a = self._Args()
        a.project = "p1"
        b = gw.make_backend(a)
        self.assertIsInstance(b, gw.VertexBackend)
        self.assertEqual(b.project, "p1")

    def test_missing_project_exits_with_a_useful_message(self):
        """設定不全要當場講清楚缺什麼，不要等打了才 4xx。"""
        buf = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(buf):
            gw.make_backend(self._Args())
        self.assertIn("--project", buf.getvalue())


# --- 單筆任務 ---------------------------------------------------------------
class TestCrawlTask(unittest.TestCase):
    def setUp(self):
        self._call = gw.call_gemini
        self.budget = gw.Budget(0)

    def tearDown(self):
        gw.call_gemini = self._call

    def test_success_records_source_grade(self):
        gw.call_gemini = lambda *a, **k: (_payload(text=_titled()), None)
        r, fatal = gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, self.budget)
        self.assertFalse(fatal)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["date"], "2020-02-10")
        self.assertEqual(r["source"], gw.SRC_TEXT)

    def test_nosearch_is_rate_limited_never_failed(self):
        """⚠️ 最重要的一條：沒查過不可以記成「查過但沒有」——那會靜靜污染研究資料。"""
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_NOSEARCH)
        r, fatal = gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, self.budget)
        self.assertEqual(r["status"], "rate_limited")
        self.assertFalse(fatal)

    def test_fatal_reports_rate_limited_and_signals_stop(self):
        """金鑰錯：這一筆放回（不是 failed），同時通報主迴圈停掉整個 worker。"""
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_FATAL)
        r, fatal = gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, self.budget)
        self.assertEqual(r["status"], "rate_limited")
        self.assertTrue(fatal)

    def test_failed_only_when_searched_and_nothing_in_window(self):
        """模型確實搜了、正常回話、仍無窗內日期 → 這才是真的 failed。"""
        gw.call_gemini = lambda *a, **k: (_payload(text="DATE: NONE"), None)
        r, _ = gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, self.budget)
        self.assertEqual(r["status"], "failed")

    def test_budget_counts_searches_not_tasks(self):
        """⚠️ Gemini 3 按「模型實際發出的搜尋次數」計費：一筆任務發 3 次搜尋就記 3。"""
        gw.call_gemini = lambda *a, **k: (
            _payload(text="DATE: NONE", searches=["a", "b", "c"]), None)
        gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, self.budget)
        self.assertEqual(self.budget.used, 3)

    def test_budget_not_charged_when_call_failed(self):
        """呼叫失敗沒有拿到回應 → 不記帳（否則上限會被幻覺的花費提早燒完）。"""
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_RETRY)
        gw.crawl_task(TASK, _backend(), gw.DEFAULT_MODEL, self.budget)
        self.assertEqual(self.budget.used, 0)


class TestBudget(unittest.TestCase):
    def test_limit_zero_is_unlimited(self):
        b = gw.Budget(0)
        b.spend(10 ** 6)
        self.assertFalse(b.exhausted())

    def test_exhausts_at_limit(self):
        b = gw.Budget(3)
        b.spend(2)
        self.assertFalse(b.exhausted())
        b.spend(1)
        self.assertTrue(b.exhausted())

    def test_note_only_charges_beyond_free_quota(self):
        b = gw.Budget(0, free_quota=100, unit_price=0.014)
        b.spend(100)
        self.assertIn("$0.00", b.note())
        b.spend(100)
        self.assertIn("$1.40", b.note())


# --- 主迴圈 -----------------------------------------------------------------
class _FakeClient:
    def __init__(self, batches):
        self.batches = list(batches)
        self.reported = []

    def lease(self, n):
        return {"tasks": self.batches.pop(0) if self.batches else []}

    def report(self, results):
        self.reported.append(results)
        return {"applied": len(results)}


class _Args:
    batch = 3
    delay = 0.0
    jitter = 0.0
    rl_threshold = 3
    block_sleep = 120.0
    idle_sleep = 30.0
    once = True
    max_batches = 0
    api_key = "key"
    backend = None    # run() 由 main 設好；測試在 setUp 補
    model = gw.DEFAULT_MODEL


def _tasks(n):
    return [dict(TASK, id=i) for i in range(1, n + 1)]


class TestRunLoop(unittest.TestCase):
    def setUp(self):
        self._time, self._random, self._call = gw.time, gw.random, gw.call_gemini
        gw.time, gw.random = _FakeTime(), _FakeRandom()

    def tearDown(self):
        gw.time, gw.random, gw.call_gemini = self._time, self._random, self._call

    def test_fatal_puts_remaining_tasks_back_and_stops(self):
        """⚠️ 設定錯誤時，未處理的租約必須立刻以 rate_limited 放回，
        絕不能整批被記成 failed，也不該乾等租約逾時。"""
        gw.call_gemini = lambda *a, **k: (None, gw.ERR_FATAL)
        client = _FakeClient([_tasks(3)])
        gw.run(_Args(), client, gw.Budget(0))
        results = client.reported[0]
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r["status"] == "rate_limited" for r in results))

    def test_budget_exhaustion_releases_rest_of_batch(self):
        """搜尋上限用完 → 本批剩下的放回佇列，下次可繼續，不會被吃掉。"""
        gw.call_gemini = lambda *a, **k: (_payload(text=_titled()), None)
        client = _FakeClient([_tasks(3)])
        args = _Args()
        args.once = False                       # 靠上限自己停，驗證 exhausted 這條路
        gw.run(args, client, gw.Budget(1))
        results = client.reported[0]
        self.assertEqual(len(results), 3)
        self.assertEqual(sum(r["status"] == "success" for r in results), 1)
        self.assertEqual(sum(r["status"] == "rate_limited" for r in results), 2)

    def test_tally_reports_source_grades(self):
        """來源分級要累計出來——m_txt 佔比就是這個實驗要看的數字。"""
        gw.call_gemini = lambda *a, **k: (_payload(text=_titled()), None)
        client = _FakeClient([_tasks(2)])
        tally = gw.run(_Args(), client, gw.Budget(0))
        self.assertEqual(tally["success"], 2)
        self.assertEqual(tally[gw.SRC_TEXT], 2)
        self.assertEqual(tally[gw.SRC_CHUNK], 0)


if __name__ == "__main__":
    unittest.main()


class TestExtractUrl(unittest.TestCase):
    """
    模型自報的出處。三支 worker 裡就這支的 url 是模型「說」的，不是頁面上讀到的
    ——所以測的重點是「格式不合就丟掉」，而不是「一定要有」。
    """

    def setUp(self):
        # ⚠️ 換掉的是模組全域，測完一定要換回來：漏掉會讓後面所有測試都吃到這個假的
        # call_gemini（實測會讓 8 個錯誤分類的測試莫名其妙一起紅）。
        self._call, self._verify = gw.call_gemini, gw.verify_url
        # ⚠️ verify_url 會真的連外網。測試不連網是這整份檔案的前提（見模組
        # docstring），漏掉這行會讓測試時間翻倍、且結果取決於對方站台活不活著。
        gw.verify_url = lambda u, **k: True

    def tearDown(self):
        gw.call_gemini, gw.verify_url = self._call, self._verify

    def _p(self, text):
        return _payload(text=text, chunks=["moneydj.com"])

    def test_takes_model_url_line(self):
        p = self._p(_titled() + "\nURL: https://www.moneydj.com/kmdj/news/x.aspx?a=1")
        self.assertEqual(gw.extract(p, "台積電", 109, 1)[3],
                         "https://www.moneydj.com/kmdj/news/x.aspx?a=1")

    def test_no_url_line_is_none_not_failure(self):
        # 模型少回一行是常態，日期照樣要成立。
        hit = gw.extract(self._p(_titled()), "台積電", 109, 1)
        self.assertEqual(hit[0], "2020-02-10")
        self.assertIsNone(hit[3])

    def test_url_none_when_model_says_none(self):
        p = self._p(_titled() + "\nURL: NONE")
        self.assertIsNone(gw.extract(p, "台積電", 109, 1)[3])

    def test_rejects_non_http_scheme(self):
        p = self._p(_titled() + "\nURL: javascript:alert(1)")
        self.assertIsNone(gw.extract(p, "台積電", 109, 1)[3])

    def test_chunk_path_refuses_the_model_url(self):
        """
        ⚠️ m_src 那層一定要 url=None。日期來自 groundingChunks（真實檢索文字），而
        模型的 URL: 那行是它自己寫的、不保證就是那個 chunk 的出處。掛上去等於讓
        「date 來自檢索原文」這個高信任標記，替一個低信任的網址背書。
        TITLE: 那行在這條路上已經被拒收了，URL: 沒有理由破例。
        """
        p = _payload(text=_titled() + "\nURL: https://cna.com.tw/a",
                     chunks=[_titled()])
        hit = gw.extract(p, "台積電", 109, 1)
        self.assertEqual(hit[1], gw.SRC_CHUNK)
        self.assertIsNone(hit[3])

    def test_crawl_task_carries_url_into_result(self):
        p = self._p(_titled() + "\nURL: https://cna.com.tw/a")
        gw.call_gemini = lambda *a, **k: (p, None)
        r, fatal = gw.crawl_task(TASK, backend=None, model="m", budget=gw.Budget(10, 0))
        self.assertFalse(fatal)
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["url"], "https://cna.com.tw/a")


class TestVerifyUrl(unittest.TestCase):
    """
    模型自報的 URL 存進 DB 前要先確認它真的存在。

    ⚠️ 這條是實測踩到的：1216 統一 109/2 那筆，模型回的
    chinatimes.com/newspapers/20200311000404-260204 格式完全正確（日期碼、版面碼
    都對），用瀏覽器 UA 抓回來卻是「404錯誤 - 中時新聞網」。日期本身另有旁證是對的，
    但出處是編的——一個 404 的網址看起來像有憑有據，比沒有出處更危險。

    取捨刻意不對稱：存到假網址的代價高（正是要防的失效），漏掉真網址的代價低
    （就是 NULL，跟加這欄之前一樣）。所以**確認不了就丟掉**。
    """

    def _resp(self, code):
        class _R:
            status = code
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
        return _R()

    def test_keeps_a_live_url(self):
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(200)
        self.assertTrue(gw.verify_url("https://a.tw/real"))

    def test_drops_a_404(self):
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        gw.urllib.request.urlopen = boom
        self.assertFalse(gw.verify_url("https://a.tw/fake"))

    def test_drops_when_blocked_or_erroring(self):
        # 403/429/5xx 都是「確認不了」，不是「確認存在」。照上面的取捨一律丟掉。
        for code in (403, 429, 500, 503):
            def boom(*a, **k):
                raise urllib.error.HTTPError("u", code, "x", {}, None)
            gw.urllib.request.urlopen = boom
            self.assertFalse(gw.verify_url("https://a.tw/x"), code)

    def test_network_failure_never_raises(self):
        # 驗證是附屬動作，絕不可以讓一筆好好的任務因為它炸掉。
        for exc in (urllib.error.URLError("down"), TimeoutError(), OSError()):
            def boom(*a, **k):
                raise exc
            gw.urllib.request.urlopen = boom
            self.assertFalse(gw.verify_url("https://a.tw/x"))

    def test_empty_url(self):
        self.assertFalse(gw.verify_url(None))
        self.assertFalse(gw.verify_url(""))


class TestCrawlTaskDropsHallucinatedUrl(unittest.TestCase):
    def setUp(self):
        self._call, self._verify = gw.call_gemini, gw.verify_url

    def tearDown(self):
        gw.call_gemini, gw.verify_url = self._call, self._verify

    def _run(self):
        p = _payload(text=_titled() + "\nURL: https://a.tw/x", chunks=["moneydj.com"])
        gw.call_gemini = lambda *a, **k: (p, None)
        return gw.crawl_task(TASK, backend=None, model="m", budget=gw.Budget(10, 0))

    def test_dead_url_becomes_none_but_the_date_still_counts(self):
        gw.verify_url = lambda u, **k: False
        r, fatal = self._run()
        self.assertEqual(r["status"], "success")     # ⚠️ 日期不受影響
        self.assertEqual(r["date"], "2020-02-10")
        self.assertIsNone(r["url"])

    def test_live_url_is_kept(self):
        gw.verify_url = lambda u, **k: True
        r, _ = self._run()
        self.assertEqual(r["url"], "https://a.tw/x")

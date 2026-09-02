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
import csv
import io
import json
import os
import sys
import tempfile
import types
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

    def test_warns_that_a_dead_url_fails_the_whole_task(self):
        """2026-08-27 收緊：m_txt 命中若帶 URL 卻打不開，crawl_task 現在整筆判 failed
        （見 TestCrawlTaskDropsHallucinatedUrl），不再只是清掉 url、留住日期。
        把代價寫進 prompt，讓模型不確定就回 NONE，別隨手編一個。"""
        p = gw.build_prompt("2330", "台積電", 109, 1)
        self.assertIn("不要編造", p)
        self.assertIn("整個判定失敗", p)


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


class TestExtractUrl(unittest.TestCase):
    """
    模型自報的出處。三支 worker 裡就這支的 url 是模型「說」的，不是頁面上讀到的
    ——所以測的重點是「格式不合就丟掉」，而不是「一定要有」。
    """

    def setUp(self):
        # ⚠️ 換掉的是模組全域，測完一定要換回來：漏掉會讓後面所有測試都吃到這個假的
        # call_gemini（實測會讓 8 個錯誤分類的測試莫名其妙一起紅）。
        self._call, self._verify = gw.call_gemini, gw.check_url
        # ⚠️ check_url 會真的連外網。測試不連網是這整份檔案的前提（見模組
        # docstring），漏掉這行會讓測試時間翻倍、且結果取決於對方站台活不活著。
        gw.check_url = lambda u, **k: gw.URL_OK

    def tearDown(self):
        gw.call_gemini, gw.check_url = self._call, self._verify

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


class TestCheckUrl(unittest.TestCase):
    """
    模型自報的 URL 存進 DB 前要先確認它真的存在。⚠️ 回傳是**三態**，不是真假：

      URL_OK      2xx/3xx（有給 name 就再確認頁面上真的有這家公司）
      URL_DEAD    對方明確回答「沒有這份文件」（404/410），或那個網址本身不可能是
                  出處（光禿禿的首頁）——「模型編的」，與網路狀況無關的確定事實
      URL_UNKNOWN 確認不了：403（擋機器人）、429、5xx、逾時、連不上、頁面裡找不到
                  公司名

    ⚠️ DEAD 與 UNKNOWN 不可以合成一個布林，因為 judge() 只對 DEAD 把 m_txt 整筆判
    failed，而 gemini 是升級鏈最後一棒、failed 就是終點。「對方今天擋我們的 UA」
    跟「這篇文章不存在」混在一起，就是拿一次連線抖動去永久丟掉一個正確的日期。

    ⚠️ 這條規則是實測踩到的：1216 統一 109/2 那筆，模型回的
    chinatimes.com/newspapers/20200311000404-260204 格式完全正確（日期碼、版面碼
    都對），用瀏覽器 UA 抓回來卻是「404錯誤 - 中時新聞網」。日期本身另有旁證是對的，
    但出處是編的——一個 404 的網址看起來像有憑有據，比沒有出處更危險。

    url 欄位本身的取捨仍然不對稱：**只有 URL_OK 才存**（存到假網址的代價高，
    漏掉真網址的代價低，就是 NULL）。分三態只影響「日期」的去留。
    """

    def setUp(self):
        # ⚠️ 這個類別會換掉模組全域的 urlopen，測完一定要換回來（見 TestExtractUrl
        # 的同一條教訓：漏掉會讓後面別的測試莫名其妙吃到這裡的假網路）。
        self._urlopen = gw.urllib.request.urlopen

    def tearDown(self):
        gw.urllib.request.urlopen = self._urlopen

    def _resp(self, code, body=b""):
        class _R:
            status = code
            def read(self_, n=None): return body
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
        return _R()

    def test_keeps_a_live_url(self):
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(200)
        self.assertEqual(gw.check_url("https://a.tw/real"), gw.URL_OK)

    def test_a_404_is_dead(self):
        """404/410 才是「對方明確說沒有這份文件」——就是那個 chinatimes 案例。"""
        for code in (404, 410):
            def boom(*a, **k):
                raise urllib.error.HTTPError("u", code, "Not Found", {}, None)
            gw.urllib.request.urlopen = boom
            self.assertEqual(gw.check_url("https://a.tw/fake"), gw.URL_DEAD, code)

    def test_blocked_or_erroring_is_unknown_not_dead(self):
        """⚠️ 403（擋機器人——實測 chinatimes 對預設 UA 就這樣）／429／5xx／401 都是
        「今天問不到」，不是「這篇不存在」。判成 DEAD 會讓 judge() 把一個可能正確的
        日期永久丟掉，還把任務推到終點 failed。"""
        for code in (401, 403, 429, 500, 503):
            def boom(*a, **k):
                raise urllib.error.HTTPError("u", code, "x", {}, None)
            gw.urllib.request.urlopen = boom
            self.assertEqual(gw.check_url("https://a.tw/x"), gw.URL_UNKNOWN, code)

    def test_network_failure_never_raises_and_is_unknown(self):
        # 驗證是附屬動作，絕不可以讓一筆好好的任務因為它炸掉；連不上也不是「不存在」。
        for exc in (urllib.error.URLError("down"), TimeoutError(), OSError()):
            def boom(*a, **k):
                raise exc
            gw.urllib.request.urlopen = boom
            self.assertEqual(gw.check_url("https://a.tw/x"), gw.URL_UNKNOWN)

    def test_non_2xx_status_without_an_exception_is_graded_the_same_way(self):
        # 有些路徑會直接回非 2xx 而不丟 HTTPError；分級不可以跟上面兩條不一致。
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(404)
        self.assertEqual(gw.check_url("https://a.tw/x"), gw.URL_DEAD)
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(403)
        self.assertEqual(gw.check_url("https://a.tw/x"), gw.URL_UNKNOWN)

    def test_empty_url(self):
        # 沒有網址就沒有「確認過不存在」這件事（judge 那邊另有 None 這一態）。
        self.assertEqual(gw.check_url(None), gw.URL_UNKNOWN)
        self.assertEqual(gw.check_url(""), gw.URL_UNKNOWN)

    def test_bare_homepage_is_dead_without_a_request(self):
        """
        ⚠️ 光禿禿的首頁不可能是「某公司某月的公告」，當出處毫無用處。

        實測：2906 高林 111/9 那筆，模型給的是 https://www.masterlink.com.tw/
        ——元富證券首頁。它當然打得開，於是通過了「網址活著」這道檢查，但它證明不了
        任何事。這種要在發請求之前就擋掉（省一次 HTTP，也省得被自己的檢查騙過）。

        算 DEAD 不算 UNKNOWN：這裡沒有任何「對方站台今天如何」的成分，純粹從網址
        本身就斷定「這不是一篇報導」——跟 404 一樣是確定的事實。
        """
        called = []
        gw.urllib.request.urlopen = lambda *a, **k: called.append(1) or self._resp(200)
        for u in ("https://www.masterlink.com.tw/", "https://a.tw",
                  "http://b.com.tw/", "https://c.tw/?x=1"):
            self.assertEqual(gw.check_url(u), gw.URL_DEAD, u)
        self.assertEqual(called, [], "首頁不該發出任何請求")

    def test_a_real_article_path_still_goes_through(self):
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(
            200, "…聯上 110年5月營收…".encode("utf-8"))
        self.assertEqual(
            gw.check_url("https://www.moneydj.com/kmdj/news/newsviewer.aspx?a=abc",
                         name="聯上"),
            gw.URL_OK)

    def test_a_live_url_about_another_company_is_unknown(self):
        """
        ⚠️ 200 不等於「這是本檔的報導」。實測：模型給 4113 聯上 110/5 的網址
        news.cnyes.com/news/id/4659779 是真的（HTTP 200、16 萬字元），但那篇的標題是
        「新復興5月營收0.47億元年減23.47% | 鉅亨網」——真實存在、屬於別家公司。
        只看狀態碼會放行，而 url 這一欄的全部用途就是點開回到**這一筆**的原文。

        但這是 UNKNOWN 不是 DEAD：body 裡找不到公司名也可能是 DB 存的簡稱與內文的
        全稱不同（「台積電」vs「台灣積體電路」）、或整頁是 JS 算出來的。所以網址不存，
        日期留著——不足以把整筆判死。
        """
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(
            200, "新復興5月營收0.47億元年減23.47% | 鉅亨網".encode("utf-8"))
        self.assertEqual(gw.check_url("https://news.cnyes.com/news/id/4659779",
                                      name="聯上"), gw.URL_UNKNOWN)

    def test_without_a_name_it_falls_back_to_status_only(self):
        # 沒給名字就只能驗「活著」——維持舊行為，不要讓漏傳參數變成全部拒收。
        gw.urllib.request.urlopen = lambda *a, **k: self._resp(200, b"whatever")
        self.assertEqual(gw.check_url("https://a.tw/x"), gw.URL_OK)

    def test_unreadable_body_does_not_reject_a_200(self):
        # body 讀不到（連線中斷、編碼壞掉）不該把一個 200 判死——那是「確認不了名字」，
        # 而狀態碼這一關已經過了。
        class _R:
            status = 200
            def read(self_, n=None): raise OSError("boom")
            def __enter__(self_): return self_
            def __exit__(self_, *a): return False
        gw.urllib.request.urlopen = lambda *a, **k: _R()
        self.assertEqual(gw.check_url("https://a.tw/x", name="聯上"), gw.URL_OK)


class TestCrawlTaskDropsHallucinatedUrl(unittest.TestCase):
    """
    ⚠️ 2026-08-27 收緊：m_txt（模型合成文字）命中若帶了 URL、但那個 URL 打不開，
    整筆改判 failed，不再是「清掉 url、日期照留」。

    舊行為（見 git blame）是「日期是 revlib.parse 通過窗過濾＋名稱錨點抽出來的，
    跟網址真不真無關」，出自 1216 統一實測案例（URL 假但日期對）。但那個案例是
    特例，多數情況模型連 URL 都編的話，日期本身的可信度也該打問號——m_txt 這層
    本來就是三支 worker裡信任度最低的一層，沒有比對象時不該再放行「連自己給的
    唯一佐證都是假的」這種結果。

    m_src（groundingChunks 檢索原文）不受影響：那條路徑的 url 依 extract() 的規則
    恆為 None（見 TestExtractUrl.test_chunk_path_refuses_the_model_url），根本不會
    走到 check_url 這一步。
    """

    def setUp(self):
        self._call, self._verify = gw.call_gemini, gw.check_url

    def tearDown(self):
        gw.call_gemini, gw.check_url = self._call, self._verify

    def _run(self):
        p = _payload(text=_titled() + "\nURL: https://a.tw/x", chunks=["moneydj.com"])
        gw.call_gemini = lambda *a, **k: (p, None)
        return gw.crawl_task(TASK, backend=None, model="m", budget=gw.Budget(10, 0))

    def test_dead_url_on_model_text_fails_the_whole_task(self):
        gw.check_url = lambda u, **k: gw.URL_DEAD
        r, fatal = self._run()
        self.assertFalse(fatal)
        self.assertEqual(r["status"], "failed")
        # 整筆判 failed：不該再帶 date/url——跟 status='failed' 的其他來源一致，
        # 避免呼叫端誤以為還有可信的日期可用。
        self.assertNotIn("date", r)
        self.assertNotIn("url", r)

    def test_a_403_does_not_fail_the_task(self):
        """端到端（不 stub check_url，只 stub 網路）：實測 chinatimes 對非瀏覽器 UA
        直接回 403，Vertex 也打過隨機的 403 下一次就 200。這種「今天問不到」若跟
        404 一樣整筆判 failed，就是拿一次擋機器人去永久丟掉一個可能正確的日期
        ——而 gemini 是最後一棒，failed 沒有下一個引擎會再試。"""
        self._urlopen = gw.urllib.request.urlopen
        def boom(*a, **k):
            raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        gw.urllib.request.urlopen = boom
        try:
            r, _ = self._run()
        finally:
            gw.urllib.request.urlopen = self._urlopen
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["date"], "2020-02-10")
        self.assertIsNone(r["url"], "確認不了的網址不存，但日期要留")

    def test_live_url_is_kept(self):
        gw.check_url = lambda u, **k: gw.URL_OK
        r, _ = self._run()
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["url"], "https://a.tw/x")

    def test_dead_url_on_chunk_source_still_only_drops_the_url(self):
        """防呆：就算未來 extract() 哪天不小心讓 m_src 帶出非 None 的 url，
        也不該被這條新規則波及——新規則明確只鎖 m_txt（見類別 docstring）。
        直接假造一個 extract() 回傳來驗證這個 src 判斷確實有在做，而不是
        「反正 m_src 的 url 恆為 None」這個巧合在保護它。"""
        self._extract = gw.extract
        gw.extract = lambda *a, **k: ("2020-02-10", gw.SRC_CHUNK, "title", "https://a.tw/x")
        gw.check_url = lambda u, **k: gw.URL_DEAD
        try:
            gw.call_gemini = lambda *a, **k: (_payload(text="x"), None)
            r, fatal = gw.crawl_task(TASK, backend=None, model="m", budget=gw.Budget(10, 0))
        finally:
            gw.extract = self._extract
        self.assertEqual(r["status"], "success")
        self.assertIsNone(r["url"])


# --- 審核模式（--review-one / --review-verdict）-----------------------------
class TestJudgeIsSharedWithBatchMode(unittest.TestCase):
    """
    ⚠️ judge() 存在的唯一理由：批次模式與審核模式必須用**同一份**判準。

    審核模式印給人看的 worker_verdict，若跟批次模式真的會回報的東西不一樣，
    審核者就是在對著另一套規則做判斷——那比沒有審核更糟（看起來把過關了）。
    所以 crawl_task 不可以自己再寫一遍判斷，只能呼叫 judge。
    """

    def setUp(self):
        self._call, self._verify = gw.call_gemini, gw.check_url

    def tearDown(self):
        gw.call_gemini, gw.check_url = self._call, self._verify

    def test_same_payload_gives_same_status_as_crawl_task(self):
        p = _payload(text=_titled(), chunks=["moneydj.com"])
        gw.call_gemini = lambda *a, **k: (p, None)
        batch, _ = gw.crawl_task(TASK, backend=None, model="m", budget=gw.Budget(0))
        judged, _ = gw.judge(TASK, p)
        self.assertEqual(batch, judged)

    def test_evidence_keeps_the_date_even_when_verdict_is_failed(self):
        """⚠️ 審核模式的全部價值：URL 假、整筆判 failed 的那一筆，審核者仍然要
        看得到模型講了什麼日期、給了哪個假網址。judge 的 result 依規則不帶 date
        （見 TestCrawlTaskDropsHallucinatedUrl），所以那份資訊只能靠 evidence 帶出來。"""
        gw.check_url = lambda u, **k: gw.URL_DEAD
        p = _payload(text=_titled() + "\nURL: https://a.tw/x")
        result, ev = gw.judge(TASK, p)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(ev["extracted"]["date"], "2020-02-10")
        self.assertEqual(ev["extracted"]["url"], "https://a.tw/x")
        self.assertEqual(ev["url_check"], gw.URL_DEAD)

    def test_unconfirmable_url_only_drops_the_url_and_keeps_the_date(self):
        """⚠️ 收緊那條規則的邊界（2026-08-27）：URL 只是**確認不了**（403 擋機器人、
        429、5xx、逾時、body 裡沒找到公司名）時不可以判 failed，只清掉 url、日期照留
        ——也就是收緊之前的行為。理由：gemini 是升級鏈最後一棒，failed 就是終點，
        沒有下一個引擎會再試；拿一次連線抖動換掉一個可能正確的日期是永久性的損失。
        整筆判 failed 只留給 URL_DEAD（對方明確說沒這頁／根本是首頁）。"""
        gw.check_url = lambda u, **k: gw.URL_UNKNOWN
        result, ev = gw.judge(TASK, _payload(text=_titled() + "\nURL: https://a.tw/x"))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["date"], "2020-02-10")
        self.assertIsNone(result["url"])
        self.assertEqual(ev["url_check"], gw.URL_UNKNOWN)
        # 稽核仍看得到模型給了哪個網址（審核模式的全部價值）。
        self.assertEqual(ev["extracted"]["url"], "https://a.tw/x")

    def test_no_url_leaves_url_check_unknown(self):
        """沒有 url 可驗 ≠ 驗過沒通過。前者是 None，後者是 URL_DEAD/URL_UNKNOWN
        ——CSV 稽核時混在一起就分不出「模型誠實回 NONE」與「模型編了一個死網址」。"""
        _, ev = gw.judge(TASK, _payload(text=_titled()))
        self.assertIsNone(ev["url_check"])


class TestReviewOne(unittest.TestCase):
    """
    --review-one：租一筆、打一次 Gemini、把證據倒出來，⚠️ **絕不回報**。

    「不回報」是這個模式唯一不可以寫錯的地方：一旦它自己回報了，Claude 的審核就
    退化成事後補章（日期已經在 DB 裡了），整個模式的存在意義歸零。租約沒回報會在
    LEASE_TTL 後被 server 惰性回收，這是安全的預設。
    """

    def setUp(self):
        self._call, self._verify = gw.call_gemini, gw.check_url

    def tearDown(self):
        gw.call_gemini, gw.check_url = self._call, self._verify

    def _one(self, payload_or_err, budget=None):
        gw.call_gemini = lambda *a, **k: payload_or_err
        client = _FakeClient([_tasks(1)])
        dump, code = gw.review_one(client, backend=None, model="m",
                                  budget=budget or gw.Budget(0))
        return dump, code, client

    def test_never_reports_to_server(self):
        dump, code, client = self._one((_payload(text=_titled()), None))
        self.assertEqual(client.reported, [])
        self.assertEqual(code, gw.EXIT_REVIEW)
        self.assertEqual(dump["recommend"], "review")

    def test_dump_carries_the_evidence_that_the_db_never_keeps(self):
        """chunks／searches／模型原文是這個模式要回答的那個問題（「gemini 是從哪裡
        找到的」），DB 沒有任何欄位裝得下它們——不倒出來就是隨程序結束消失。"""
        p = _payload(text=_titled(), chunks=["moneydj.com", "cnyes.com"],
                     searches=["台泥 109年1月營收", "台泥 2020年1月 營收"])
        dump, _, _ = self._one((p, None))
        self.assertEqual(dump["chunks"], ["moneydj.com", "cnyes.com"])
        self.assertEqual(len(dump["searches"]), 2)
        self.assertIn("109年1月營收", dump["text"])
        self.assertEqual(dump["extracted"]["source"], gw.SRC_TEXT)
        self.assertEqual(dump["worker_verdict"], "success")

    def test_approvable_body_is_verbatim_what_batch_mode_would_report(self):
        """Claude 核准時送出去的東西必須是 worker 算出來的那一份，不是 Claude
        重打一遍——這條就是「Claude 不可以自己送日期進 DB」的機制保障。"""
        dump, _, _ = self._one((_payload(text=_titled()), None))
        self.assertEqual(dump["report_if_approved"],
                         {"id": 1, "status": "success", "date": "2020-02-10",
                          "source": gw.SRC_TEXT, "title": dump["extracted"]["title"],
                          "url": None})

    def test_nothing_to_approve_when_worker_says_failed(self):
        gw.check_url = lambda u, **k: gw.URL_DEAD
        dump, _, _ = self._one(
            (_payload(text=_titled() + "\nURL: https://a.tw/x"), None))
        self.assertEqual(dump["worker_verdict"], "failed")
        self.assertIsNone(dump["report_if_approved"])
        self.assertEqual(dump["extracted"]["date"], "2020-02-10")   # 但看得到

    def test_nosearch_recommends_retry_and_is_not_reviewable(self):
        """⚠️ 模型沒去搜 → 這一筆根本沒查過，沒有東西可審，也不可以判 failed。"""
        dump, code, client = self._one((None, gw.ERR_NOSEARCH))
        self.assertEqual(dump["error"], gw.ERR_NOSEARCH)
        self.assertEqual(dump["recommend"], "retry")
        self.assertEqual(dump["worker_verdict"], "rate_limited")
        self.assertIsNone(dump["extracted"])
        self.assertEqual(code, gw.EXIT_REVIEW)
        self.assertEqual(client.reported, [])

    def test_fatal_recommends_abort_with_its_own_exit_code(self):
        """設定錯誤對每一筆都會重演：loop 要看得出「停下來，別再叫我」。"""
        dump, code, _ = self._one((None, gw.ERR_FATAL))
        self.assertEqual(dump["recommend"], "abort")
        self.assertEqual(code, gw.EXIT_ABORT)

    def test_empty_queue_has_its_own_exit_code(self):
        gw.call_gemini = lambda *a, **k: (_payload(), None)
        client = _FakeClient([[]])
        dump, code = gw.review_one(client, None, "m", gw.Budget(0))
        self.assertIsNone(dump["task"])
        self.assertEqual(dump["recommend"], "empty")
        self.assertEqual(code, gw.EXIT_EMPTY)

    def test_empty_queue_does_not_call_the_paid_api(self):
        calls = []
        gw.call_gemini = lambda *a, **k: (calls.append(1), (_payload(), None))[1]
        gw.review_one(_FakeClient([[]]), None, "m", gw.Budget(0))
        self.assertEqual(calls, [])

    def test_budget_counts_searches_and_is_reported(self):
        b = gw.Budget(0)
        dump, _, _ = self._one(
            (_payload(text=_titled(), searches=["a", "b", "c"]), None), budget=b)
        self.assertEqual(b.used, 3)
        self.assertEqual(dump["searches_used"], 3)
        self.assertIn("搜尋 3/", dump["cost_note"])


def _dump(status="success", **over):
    """組一份 review_one 的產出，供 review_verdict 的測試使用。"""
    d = {"mode": "review-one", "worker": "claude-review-1",
         "task": dict(TASK), "error": None, "recommend": "review",
         "searches": ["q1"], "chunks": ["moneydj.com", "cnyes.com"],
         "text": "DATE: 2020-02-10", "url_check": None,
         "extracted": {"date": "2020-02-10", "source": gw.SRC_TEXT,
                       "title": "台積電109年1月營收…", "url": None},
         "worker_verdict": status,
         "report_if_approved": ({"id": 1, "status": "success",
                                 "date": "2020-02-10", "source": gw.SRC_TEXT,
                                 "title": "台積電109年1月營收…", "url": None}
                                if status == "success" else None),
         "searches_used": 1, "cost_note": "搜尋 1/∞ 次（約 $0.01）"}
    d.update(over)
    return d


class TestReviewVerdict(unittest.TestCase):
    """
    --review-verdict：把 Claude 的判斷落地。Claude 只交出 approve/reject 與一段
    理由，**所有資料欄位都由程式從 dump 裡填**——它不經手日期、來源、網址。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.csv = os.path.join(self.dir, "gemini_review.csv")

    def _rows(self):
        with io.open(self.csv, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_approve_posts_exactly_the_dump_body(self):
        c = _FakeClient([])
        gw.review_verdict(c, _dump(), "approve", "MoneyDJ 標題三要素齊全", self.csv)
        self.assertEqual(c.reported, [[_dump()["report_if_approved"]]])

    def test_reject_reports_failed(self):
        """gemini 是升級鏈最後一棒，failed 就是終點——語意上跟 worker 自己判
        failed 完全一致，不新增第四種狀態。"""
        c = _FakeClient([])
        gw.review_verdict(c, _dump(), "reject", "標題是股東常會通知不是營收公告", self.csv)
        self.assertEqual(c.reported, [[{"id": 1, "status": "failed"}]])

    def test_retry_reports_rate_limited_and_writes_no_row(self):
        """沒查過的那一筆不是「審過」，不該留下審核紀錄。"""
        c = _FakeClient([])
        gw.review_verdict(c, _dump(status="rate_limited", error=gw.ERR_NOSEARCH,
                                   recommend="retry", extracted=None),
                          "retry", "", self.csv)
        self.assertEqual(c.reported, [[{"id": 1, "status": "rate_limited"}]])
        self.assertFalse(os.path.exists(self.csv))

    def test_row_fields_are_machine_filled_from_the_dump(self):
        gw.review_verdict(_FakeClient([]), _dump(), "approve",
                          "標題含金額且與 revenue 對得上", self.csv)
        r = self._rows()[0]
        self.assertEqual(r["stock_id"], "2330")
        self.assertEqual(r["announce_date"], "2020-02-10")
        self.assertEqual(r["source"], gw.SRC_TEXT)
        self.assertEqual(json.loads(r["chunks"]), ["moneydj.com", "cnyes.com"])
        self.assertEqual(r["verdict"], "approve")
        self.assertEqual(r["note"], "標題含金額且與 revenue 對得上")

    def test_chunks_survive_a_semicolon_in_the_title(self):
        """⚠️ chunks 是這張表相對 title_review.csv 多出來的那一欄，而 grounding 的
        來源標題是自由文字、真的會有分號（新聞標題、站名都可能）。用 ";" 串接的話
        這一欄就切不回原本的清單——一個進版控的稽核欄位「大部分時候切得回來」等於
        不能用。存 JSON 才是無損的。"""
        d = _dump(chunks=["台泥 109年1月營收; 年增 3%", "cnyes.com"])
        gw.review_verdict(_FakeClient([]), d, "approve", "分號測試", self.csv)
        self.assertEqual(json.loads(self._rows()[0]["chunks"]),
                         ["台泥 109年1月營收; 年增 3%", "cnyes.com"])

    def test_the_csv_directory_is_created_if_missing(self):
        """全新 clone 沒有 data/、或 --review-csv 指到別的子目錄——這一步噴例外的
        時候 server 已經套用了判斷，稽核列卻寫不進去（見下一條）。先別讓它發生。"""
        path = os.path.join(self.dir, "nested", "deeper", "g.csv")
        gw.review_verdict(_FakeClient([]), _dump(), "approve", "建目錄", path)
        with io.open(path, encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_a_failed_csv_write_says_what_already_landed(self):
        """⚠️ 順序是刻意的（先回報、後寫 CSV），但寫檔失敗時例外不可以就這樣穿出去：
        那時 server【已經】套用了這個判斷，而呼叫端會以為整件事沒發生 →
        重跑同一道指令就是重複回報，而那個 approve 對 stamp_verified 永遠隱形。
        所以要換成帶著「已經落地了什麼」與那一列內容的例外，讓人補得回來。"""
        c = _FakeClient([])
        real = gw._append_review_row
        def boom(*a, **k):
            raise OSError("disk on fire")
        gw._append_review_row = boom
        try:
            with self.assertRaises(gw.ReviewRowLost) as cm:
                gw.review_verdict(c, _dump(), "approve", "寫檔失敗", self.csv)
        finally:
            gw._append_review_row = real
        self.assertEqual(c.reported, [[_dump()["report_if_approved"]]])
        self.assertEqual(cm.exception.applied, 1)          # server 回的 applied
        self.assertEqual(cm.exception.row["verdict"], "approve")
        self.assertIn("stock_id", cm.exception.csv_line())  # 可直接貼回 CSV 的一列
        self.assertIn("approve", cm.exception.csv_line())

    def test_an_unreadable_csv_refuses_before_reporting(self):
        """⚠️ 稽核檔讀不到就不能確認 note 有沒有跟上一列一字不差（擋套版那條）。
        這時要在【回報之前】拒絕：順序站在我們這邊，什麼都還沒發生。
        裸奔的 OSError 會被當成「整件事沒發生」——這裡剛好是真的，但要講清楚。"""
        c = _FakeClient([])
        blocked = os.path.join(self.dir, "as-a-dir.csv")
        os.mkdir(blocked)
        with self.assertRaises(gw.ReviewError):
            gw.review_verdict(c, _dump(), "approve", "理由", blocked)
        self.assertEqual(c.reported, [])

    def test_header_matches_the_schema_stamp_verified_validates(self):
        gw.review_verdict(_FakeClient([]), _dump(), "approve", "理由甲", self.csv)
        with io.open(self.csv, encoding="utf-8") as f:
            self.assertEqual(tuple(csv.DictReader(f).fieldnames), gw.REVIEW_FIELDS)

    def test_appends_without_rewriting_the_header(self):
        gw.review_verdict(_FakeClient([]), _dump(), "approve", "理由甲", self.csv)
        d2 = _dump()
        d2["task"] = dict(TASK, id=2, roc_month=2)
        gw.review_verdict(_FakeClient([]), d2, "reject", "理由乙", self.csv)
        self.assertEqual([r["note"] for r in self._rows()], ["理由甲", "理由乙"])

    def test_approve_refused_when_there_is_nothing_to_approve(self):
        """⚠️ worker 判 failed 的那一筆沒有「可核准的內容」。允許核准就等於讓
        審核者繞過 URL 幻覺那條規則，而且日期會變成 Claude 手打的——一律拒絕。"""
        c = _FakeClient([])
        with self.assertRaises(gw.ReviewError):
            gw.review_verdict(c, _dump(status="failed"), "approve", "看起來對", self.csv)
        self.assertEqual(c.reported, [])
        self.assertFalse(os.path.exists(self.csv))

    def test_note_is_required(self):
        """判斷是主觀的，沒有理由的章沒有稽核價值（與 title_review.csv 同一條規則）。"""
        for v in ("approve", "reject"):
            with self.assertRaises(gw.ReviewError):
                gw.review_verdict(_FakeClient([]), _dump(), v, "   ", self.csv)

    def test_note_identical_to_the_previous_row_is_refused(self):
        """⚠️ 擋套版：100 筆共用一句理由的 CSV 對稽核者毫無價值（2026-08-25 的
        title 審核就是這樣失敗的）。逐筆讀過的人不會寫出跟上一筆一字不差的理由。"""
        gw.review_verdict(_FakeClient([]), _dump(), "approve", "同一句話", self.csv)
        d2 = _dump()
        d2["task"] = dict(TASK, id=2, roc_month=2)
        with self.assertRaises(gw.ReviewError):
            gw.review_verdict(_FakeClient([]), d2, "approve", "同一句話", self.csv)

    def test_reject_is_refused_when_the_query_never_happened(self):
        """⚠️ 模組開頭 1. 那條戒律的機制保障：ERR_NOSEARCH／ERR_RETRY 的那一筆根本
        沒查過，dump 裡的 extracted/chunks/text 當然是空的——而「空證據」看起來非常
        像一筆該否決的結果（「沒有任何證據支持這個日期」）。這裡若讓 reject 過去，
        一筆從未被查詢過的任務就落 state='failed' 成為終點（gemini 是最後一棒），
        正是「把還沒查成功寫成 failed」的那種靜默污染。只能 retry。"""
        for err in (gw.ERR_NOSEARCH, gw.ERR_RETRY):
            c = _FakeClient([])
            d = _dump(status="rate_limited", error=err, recommend="retry",
                      extracted=None, chunks=[], text="")
            with self.assertRaises(gw.ReviewError):
                gw.review_verdict(c, d, "reject", f"沒有證據支持（{err}）", self.csv)
            self.assertEqual(c.reported, [], err)
            self.assertFalse(os.path.exists(self.csv), err)

    def test_approve_is_refused_on_an_aborted_dump(self):
        """ERR_FATAL 的 dump 同理：裡面什麼證據都沒有，唯一合法的判斷是 retry。"""
        c = _FakeClient([])
        d = _dump(status="rate_limited", error=gw.ERR_FATAL, recommend="abort",
                  extracted=None)
        with self.assertRaises(gw.ReviewError):
            gw.review_verdict(c, d, "approve", "看起來對", self.csv)
        self.assertEqual(c.reported, [])

    def test_unknown_verdict_is_refused(self):
        with self.assertRaises(gw.ReviewError):
            gw.review_verdict(_FakeClient([]), _dump(), "maybe", "理由", self.csv)

    def test_csv_is_written_after_the_report_so_no_row_claims_an_unapplied_change(self):
        """回報失敗時不留下「已核准」的紀錄：稽核檔寧可漏一列，也不可以宣稱一件
        沒發生的事（漏的那列重跑就補回來，假的那列會一直說謊）。"""
        class _Boom(_FakeClient):
            def report(self, results):
                raise urllib.error.URLError("down")
        with self.assertRaises(urllib.error.URLError):
            gw.review_verdict(_Boom([]), _dump(), "approve", "理由", self.csv)
        self.assertFalse(os.path.exists(self.csv))


class TestPendingGuards(unittest.TestCase):
    """
    交接檔（`.gemini_review_pending.json`）的兩道守門。⚠️ 這兩條原本只寫在
    SKILL.md 的文字裡，而它們保護的是**已經付過錢的證據**：一筆 --review-one
    實測要發 4~11 次搜尋。靠自律的規則遲早會被一次手滑或一個 loop 繞過。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "pending.json")

    def test_review_one_refuses_to_overwrite_an_unfinished_review(self):
        """⚠️ 交接檔還在＝上一筆的判斷還沒落地（多半是 server 掉線，report 丟了
        例外）。再跑一次 --review-one 會覆蓋掉那份證據，還把那筆租約丟在那裡等
        600s TTL——付過的錢就這樣沒了。"""
        io.open(self.path, "w", encoding="utf-8").write("{}")
        with self.assertRaises(gw.ReviewError):
            gw.check_no_pending(self.path)
        # 明確要覆蓋（--force）才放行，且要自己知道在丟掉什麼。
        gw.check_no_pending(self.path, force=True)

    def test_a_clean_slate_passes(self):
        gw.check_no_pending(self.path)

    def test_an_aborted_run_is_not_worth_saving(self):
        """⚠️ ERR_FATAL 的 dump 裡沒有任何證據（recommend=abort、extracted=None）。
        存下來只會讓 SKILL 第 0 步把下一輪導去「先審它」，而那份 dump 唯一合法的
        判斷是 retry——一次白跑的來回，還誘人在沒有證據的情況下 reject。"""
        self.assertFalse(gw.should_save_pending(
            {"task": dict(TASK), "recommend": "abort"}))

    def test_reviewable_and_retryable_runs_are_saved(self):
        # retry 也要存：--review-verdict retry 得靠交接檔才知道是哪一筆任務。
        for rec in ("review", "retry"):
            self.assertTrue(gw.should_save_pending(
                {"task": dict(TASK), "recommend": rec}), rec)

    def test_an_empty_queue_is_not_saved(self):
        self.assertFalse(gw.should_save_pending(
            {"task": None, "recommend": "empty"}))


class TestReviewerRegistry(unittest.TestCase):
    """審核者身分是**一組**綁在一起的東西：稽核 CSV、交接檔、worker_id、蓋章值。

    ⚠️ 這四樣任何一樣被單獨改掉，症狀都是靜默的——判斷寫進另一條線的稽核檔、
    或蓋上另一個審核者的章，程式不會報錯，跑起來一切正常。2026-09-02 真的發生
    過：文件寫 verified='claude'、工作區的程式寫死 'codex'，兩邊各跑各的，
    直到有人去比對才發現。所以身分只准有一個定義處（REVIEWERS），這裡守住它。
    """

    def test_both_lines_are_registered(self):
        self.assertEqual(set(gw.REVIEWERS), {"claude", "codex"})

    def test_stamp_is_the_registry_key_itself(self):
        # 蓋章值就是身分本身。能分開寫，遲早會分開——那正是上面說的那個 bug。
        for name, r in gw.REVIEWERS.items():
            self.assertEqual(r.stamp, name, name)

    def test_no_two_reviewers_share_a_csv(self):
        # 共用稽核檔＝兩條線的判斷混進同一份，事後分不出哪一筆是誰審的。
        paths = [r.csv for r in gw.REVIEWERS.values()]
        self.assertEqual(len(paths), len(set(paths)), paths)

    def test_no_two_reviewers_share_a_pending_file(self):
        # ⚠️ 共用交接檔＝兩條線同時跑時，後租的那筆會覆蓋掉前一筆**已經付過錢**
        # 的證據（見 check_no_pending 的 --force 註解）。
        paths = [r.pending for r in gw.REVIEWERS.values()]
        self.assertEqual(len(paths), len(set(paths)), paths)

    def test_no_two_reviewers_share_a_worker_id(self):
        # worker_id 會寫進 tasks.worker_id：看板上要分得出這一筆是誰審過的。
        ids = [r.worker_id for r in gw.REVIEWERS.values()]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_every_pending_file_is_gitignored(self):
        """⚠️ 交接檔是執行期產物，進版控等於把付費證據推上去。

        .gitignore 從前釘死單一檔名 .gemini_review_pending.json；一旦每條線
        各有自己的交接檔，那條規則就漏了——而漏掉是靜默的（git status 才看得到）。
        """
        import fnmatch
        root = os.path.dirname(os.path.dirname(os.path.abspath(gw.__file__)))
        with io.open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
            pats = [ln.strip() for ln in f
                    if ln.strip() and not ln.startswith("#")]
        for r in gw.REVIEWERS.values():
            self.assertTrue(
                any(fnmatch.fnmatch(r.pending, p) for p in pats),
                f"{r.pending} 沒有被 .gitignore 蓋到")


class TestReviewerMustBeExplicit(unittest.TestCase):
    """審核模式一定要講清楚自己是誰，不准有預設值。

    ⚠️ 預設值正是上面那個 bug 的溫床：忘了帶旗標時，程式會安靜地用「某一條線」
    的檔案與章跑完，而那條線不見得是你以為的那條。寧可拒收也不要猜。
    """

    def _settings(self, **kw):
        kw.setdefault("reviewer", None)
        kw.setdefault("review_one", False)
        kw.setdefault("review_verdict", None)
        kw.setdefault("review_csv", None)
        kw.setdefault("pending", None)
        kw.setdefault("worker_id", None)
        return gw.review_settings(types.SimpleNamespace(**kw))

    def test_review_one_without_a_reviewer_is_refused(self):
        with self.assertRaises(gw.ReviewError):
            self._settings(review_one=True)

    def test_review_verdict_without_a_reviewer_is_refused(self):
        with self.assertRaises(gw.ReviewError):
            self._settings(review_verdict="approve")

    def test_batch_mode_needs_no_reviewer(self):
        # 批次模式不寫稽核檔、不蓋章，不該被這條規則綁住。
        self.assertIsNone(self._settings().reviewer)

    def test_reviewer_supplies_all_four_coupled_values(self):
        s = self._settings(review_one=True, reviewer="codex")
        r = gw.REVIEWERS["codex"]
        self.assertEqual((s.csv, s.pending, s.worker_id),
                         (r.csv, r.pending, r.worker_id))

    def test_the_two_lines_never_resolve_to_the_same_files(self):
        a = self._settings(review_one=True, reviewer="claude")
        b = self._settings(review_one=True, reviewer="codex")
        self.assertNotEqual(a.csv, b.csv)
        self.assertNotEqual(a.pending, b.pending)

    def test_an_explicit_path_still_wins(self):
        # 明講的路徑照舊蓋過預設（測試與一次性重跑都靠這個）。
        s = self._settings(review_one=True, reviewer="claude",
                           review_csv="/tmp/x.csv", pending="/tmp/x.json")
        self.assertEqual((s.csv, s.pending), ("/tmp/x.csv", "/tmp/x.json"))

    def test_worker_id_says_which_line_reviewed_it(self):
        for name in gw.REVIEWERS:
            s = self._settings(review_one=True, reviewer=name)
            self.assertIn(name, s.worker_id)


class TestBatchModeStillStarts(unittest.TestCase):
    """⚠️ main() 的批次分支沒有任何測試蓋到，而它是這支 worker 的**主要用途**。

    2026-09-02 的教訓：把審核模式的 worker_id 收進 review_settings 時，批次那條
    路上還留著一個對舊區域變數的參照——整個批次 worker 每次啟動就 NameError，
    149 個測試照樣全綠，因為沒有一個走過 main()。這裡只驗最低限度的那件事：
    批次模式走得到 run()，而且帶著自己算出來的 worker_id。
    """

    def setUp(self):
        self.argv = sys.argv
        self.seen = {}

        def fake_run(args, client, budget):
            self.seen["worker"] = client.worker_id
            return {}

        self.run_orig, gw.run = gw.run, fake_run
        self.backend_orig, gw.make_backend = gw.make_backend, lambda a: _FakeVertex("p")

    def tearDown(self):
        sys.argv = self.argv
        gw.run = self.run_orig
        gw.make_backend = self.backend_orig

    def _main(self, *extra):
        sys.argv = ["gemini_worker", "--server", "http://x", "--once"] + list(extra)
        with contextlib.redirect_stdout(io.StringIO()):
            gw.main()

    def test_batch_mode_reaches_the_loop(self):
        self._main()
        self.assertIn("worker", self.seen)

    def test_batch_worker_id_is_host_and_pid_not_a_reviewer(self):
        self._main()
        self.assertTrue(self.seen["worker"].endswith("-m"), self.seen["worker"])
        for r in gw.REVIEWERS.values():
            self.assertNotEqual(self.seen["worker"], r.worker_id)

    def test_an_explicit_worker_id_still_wins(self):
        self._main("--worker-id", "box-7")
        self.assertEqual(self.seen["worker"], "box-7")


class TestEverySkillHandoffFileIsIgnored(unittest.TestCase):
    """所有 skill 的交接檔都必須被 .gitignore 蓋到，不只 gemini-review 那兩支。

    ⚠️ 交接檔存的是【已經付過錢】的證據，是執行期產物。中斷的那一輪會把它留在
    工作區，下一次 git add -A 就順手提交上去。TestReviewerRegistry 已經守住
    REVIEWERS 那兩個檔，但那只涵蓋這支 worker 自己；別的 skill 各自發明的交接檔
    （.codex_search_pending.json…）一樣會漏。這裡直接掃 skill 文件裡出現的檔名，
    新增 skill 時不必記得回來補測試。
    """

    def test_no_handoff_file_escapes_gitignore(self):
        import fnmatch
        import glob
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(gw.__file__)))
        with io.open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
            pats = [ln.strip() for ln in f
                    if ln.strip() and not ln.startswith("#")]
        names = set()
        for d in (".claude", ".agents"):
            for doc in glob.glob(os.path.join(root, d, "skills", "*", "SKILL.md")):
                with io.open(doc, encoding="utf-8") as f:
                    names |= set(re.findall(r"\.[a-z0-9_]+_pending[a-z0-9_.]*\.json",
                                            f.read()))
        self.assertTrue(names, "掃不到任何交接檔名，這個測試就沒在守東西了")
        for n in sorted(names):
            self.assertTrue(any(fnmatch.fnmatch(n, p) for p in pats),
                            f"{n} 沒有被 .gitignore 蓋到")


if __name__ == "__main__":
    unittest.main()

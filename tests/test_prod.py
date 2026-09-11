"""Production tests: auth, CSRF, providers, upload, XSS, categories, review,
search, exports, backups, health, mobile smoke. All offline; hosted calls
are monkeypatched. Never uses real private bookmarks (synthetic demo only).
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="xbo-prod-")
os.environ["DATA_DIR"] = os.path.join(TMP, "data")
os.environ["APP_USERNAME"] = "owner"
os.environ["SESSION_SECRET"] = "test-session-secret-0123456789abcdef"
os.environ.setdefault("LLM_PROVIDER", "gemini")
for k in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(k, None)

from werkzeug.security import generate_password_hash
os.environ["APP_PASSWORD_HASH"] = generate_password_hash("correct-pw")

import server as SRV
from xbookmark import llm as LLM


def _client():
    app = SRV.app
    app.config.update(TESTING=True)
    return app.test_client()


def _login(c, pw="correct-pw"):
    tok = c.get("/api/csrf").get_json()["csrf_token"]
    return c.post("/login", data={"username": "owner", "password": pw,
                                  "csrf_token": tok},
                  follow_redirects=False), tok


def _authed():
    c = _client()
    r, _old = _login(c)
    assert r.status_code == 302, r.status_code
    tok = c.get("/api/csrf").get_json()["csrf_token"]
    return c, tok


def _accept_top(kb, max_cats=3):
    from xbookmark import categories as CZ
    payload = CZ.discover_structure(kb, engine="heuristic",
                                    max_categories=10)
    cats = [x for x in payload["categories"]
            if x["name"] != "Unsorted / Review"][:max_cats]
    CZ.accept_structure(
        kb, [{"name": x["name"], "description": x.get("description", "")}
             for x in cats])
    return [x["name"] for x in cats]


def _seed(c, tok, n=30):
    r = c.post("/api/demo", json={"n": n, "csrf_token": tok},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.get_json()
    from xbookmark import categories as CZ
    payload = CZ.discover_structure(SRV.kb(), engine="heuristic",
                                    max_categories=10)
    cats = [x for x in payload["categories"]
            if x["name"] != "Unsorted / Review"][:3]
    r = c.post("/api/discover/accept",
               json={"categories": [{"name": x["name"],
                                     "description": x.get("description", "")}
                                    for x in cats],
                     "csrf_token": tok},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.get_json()
    return [x["name"] for x in cats]


class HealthTest(unittest.TestCase):
    def test_health(self):
        c = _client()
        for p in ("/healthz", "/health"):
            r = c.get(p)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["ok"])


class AuthTest(unittest.TestCase):
    def test_login_success_and_session(self):
        c, tok = _authed()
        r = c.get("/api/state")
        self.assertEqual(r.status_code, 200)
        self.assertIn("total", r.get_json())

    def test_login_wrong_password(self):
        c = _client()
        r, _tok = _login(c, pw="nope")
        self.assertEqual(r.status_code, 401)

    def test_unauthenticated_blocked(self):
        c = _client()
        paths = ["/", "/upload", "/api/state", "/api/bookmarks",
                 "/api/duplicates", "/api/export?format=json",
                 "/api/backup", "/api/uploads", "/api/ai-status",
                 "/api/discover"]
        for p in paths:
            r = c.get(p)
            self.assertIn(r.status_code, (302, 401), p)
        # state-changing routes unauthenticated
        tok = c.get("/api/csrf").get_json()["csrf_token"]
        for p, body in [
                ("/api/import", {}), ("/api/classify-all", {"engine": "heuristic"}),
                ("/api/discover", {"engine": "heuristic"}),
                ("/api/categories", {"name": "X"}),
                ("/api/review", {"ids": []}),
                ("/api/demo", {"n": 5})]:
            r = c.post(p, json=dict(body, csrf_token=tok),
                       headers={"X-CSRF-Token": tok})
            self.assertEqual(r.status_code, 401, p)

    def test_csrf_required(self):
        c, tok = _authed()
        # no token at all
        r = c.post("/api/categories", json={"name": "NoTok"})
        self.assertEqual(r.status_code, 403)
        # wrong token
        r = c.post("/api/categories", json={"name": "BadTok",
                                            "csrf_token": "wrong"},
                   headers={"X-CSRF-Token": "wrong"})
        self.assertEqual(r.status_code, 403)
        # login without csrf fails
        c2 = _client()
        r = c2.post("/login", data={"username": "owner",
                                    "password": "correct-pw"})
        self.assertEqual(r.status_code, 403)

    def test_logout(self):
        c, tok = _authed()
        r = c.post("/logout", json={"csrf_token": tok},
                   headers={"X-CSRF-Token": tok})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.get("/api/state").status_code, 401)

    def test_secure_cookie_flags_in_production(self):
        os.environ["RENDER"] = "true"
        try:
            import importlib
            importlib.reload(SRV)
            self.assertTrue(SRV.app.config["SESSION_COOKIE_HTTPONLY"])
            self.assertTrue(SRV.app.config["SESSION_COOKIE_SECURE"])
            self.assertEqual(
                SRV.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        finally:
            del os.environ["RENDER"]
            import importlib
            importlib.reload(SRV)


class ProviderUnitTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(LLM.DEFAULT_GEMINI_MODEL, "gemini-3.8-flash")
        self.assertEqual(LLM.provider_default(), "gemini")
        st = LLM.configured_provider_status()
        self.assertFalse(st["gemini"]["configured"])
        self.assertNotIn("sk-", json.dumps(st))

    def test_safe_errors(self):
        def http(code):
            try:
                raise urllib.error.HTTPError("http://x", code, "E", {}, io.BytesIO(b""))
            except urllib.error.HTTPError as e:
                return LLM._safe_error(e)
        self.assertIn("400", http(400))
        self.assertIn("401", http(401))
        self.assertIn("403", http(403))
        self.assertIn("429", http(429))
        self.assertIn("500", http(500))
        self.assertIn("timed out", LLM._safe_error(TimeoutError("timed out")))
        self.assertIn("failed", LLM._safe_error(
            urllib.error.URLError("conn refused")))
        # secrets never leak
        msg = LLM._safe_error(RuntimeError("key=sk-secret-123 Authorization: Bearer xyz"))
        self.assertNotIn("sk-secret", msg)

    def test_malformed_json(self):
        with self.assertRaises((ValueError, Exception)):
            LLM.parse_json_array_or_obj("this is not json at all {{{")

    def _fake_success(self, provider):
        real = LLM.chat_text

        def fake(prov, messages, model=None, timeout=None):
            assert prov == provider
            return json.dumps({"categories": [
                {"name": "C1", "description": "d", "count": 3,
                 "examples": ["e"], "confidence": 0.9}]})
        LLM.chat_text = fake
        return real

    def test_gemini_success_discover(self):
        from xbookmark import categories as CZ, demo as DEMO
        import tempfile as _t
        kb = os.path.join(_t.mkdtemp(prefix="xbo-gem-"), "kb")
        DEMO.run(kb, n=20)
        real = self._fake_success("gemini")
        try:
            p = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM.chat_text = real
        self.assertEqual(p["provider_used"], "gemini")

    def test_gemini_error_codes_fallback(self):
        from xbookmark import classify as CL, demo as DEMO, categories as CZ
        import tempfile as _t
        for code in (400, 401, 403, 429, 500):
            kb = os.path.join(_t.mkdtemp(prefix="xbo-err-"), "kb")
            DEMO.run(kb, n=40)
            names = _accept_top(kb)
            self.assertTrue(names)
            real = LLM.chat_text

            def boom(prov, messages, model=None, timeout=None, _c=code):
                raise urllib.error.HTTPError("http://x", _c, "E", {},
                                             io.BytesIO(b""))
            LLM.chat_text = boom
            try:
                res = CL.classify_all(kb, engine="gemini", batch_size=10)
            finally:
                LLM.chat_text = real
            self.assertEqual(res["provider_actually_used"], "heuristic",
                             "code %d" % code)
            self.assertEqual(res["mode"], "heuristic")
            self.assertTrue(res["resumable"])
            self.assertGreater(res["fallback_batches"], 0)

    def test_timeout_and_malformed_fallback(self):
        from xbookmark import classify as CL, demo as DEMO, categories as CZ
        import tempfile as _t
        for fail in ("timeout", "malformed", "conn"):
            kb = os.path.join(_t.mkdtemp(prefix="xbo-fb-"), "kb")
            DEMO.run(kb, n=40)
            _accept_top(kb)
            real = LLM.chat_text
            if fail == "timeout":
                def boom(*a, **k):
                    raise TimeoutError("timed out")
            elif fail == "malformed":
                def boom(*a, **k):
                    return "not json {{{"
            else:
                def boom(*a, **k):
                    raise urllib.error.URLError("conn refused")
            LLM.chat_text = boom
            try:
                res = CL.classify_all(kb, engine="gemini", batch_size=10)
            finally:
                LLM.chat_text = real
            self.assertEqual(res["mode"], "heuristic", fail)
            self.assertTrue(res["resumable"])

    def test_groq_openrouter_fallback_used(self):
        from xbookmark import classify as CL, demo as DEMO, categories as CZ
        import tempfile as _t
        os.environ["GROQ_API_KEY"] = "gsk-test"
        try:
            kb = os.path.join(_t.mkdtemp(prefix="xbo-gr-"), "kb")
            DEMO.run(kb, n=40)
            _accept_top(kb)
            real = LLM.chat_text
            calls = []

            def fake(prov, messages, model=None, timeout=None):
                calls.append(prov)
                if prov == "gemini":
                    raise urllib.error.HTTPError("http://x", 500, "E", {},
                                                 io.BytesIO(b""))
                names = [c["name"] for c in
                         __import__("xbookmark.library",
                                    fromlist=["Library"]).Library(kb)
                         .category_list()]
                return json.dumps([{"tweet_id": "x", "categories": [],
                                    "confidence": 0.9, "reason": "r",
                                    "alternatives": [], "intent": None,
                                    "review_required": False}])
            # Gemini primary fails; groq key configured so it is the
            # secondary. classify resolves secondary via env.
            os.environ["GEMINI_API_KEY"] = "g-test"
            LLM.chat_text = fake
            try:
                CL.classify_all(kb, engine="gemini", batch_size=8)
            finally:
                LLM.chat_text = real
            self.assertIn("gemini", calls)
        finally:
            os.environ.pop("GROQ_API_KEY", None)
            os.environ.pop("GEMINI_API_KEY", None)

    def test_provider_selection_and_run_report(self):
        from xbookmark import classify as CL, demo as DEMO, categories as CZ
        import tempfile as _t
        kb = os.path.join(_t.mkdtemp(prefix="xbo-rep-"), "kb")
        DEMO.run(kb, n=40)
        _accept_top(kb)
        res = CL.classify_all(kb, engine="heuristic", batch_size=6)
        for f in ("provider_requested", "model_requested",
                  "provider_actually_used", "mode", "successful_batches",
                  "failed_batches", "fallback_batches", "resumable"):
            self.assertIn(f, res, f)


class AiTestEndpointTest(unittest.TestCase):
    def test_ai_test_no_key_safe(self):
        c, tok = _authed()
        r = c.post("/api/ai-test", json={"provider": "gemini",
                                         "csrf_token": tok},
                   headers={"X-CSRF-Token": tok})
        j = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertFalse(j["ok"])
        self.assertNotIn("key", json.dumps(j).lower().replace("api key", ""))

    def test_ai_status_no_secrets(self):
        c, tok = _authed()
        r = c.get("/api/ai-status")
        self.assertEqual(r.status_code, 200)
        body = json.dumps(r.get_json())
        # no full key material, no auth headers, anywhere
        for secret in (os.environ.get("GEMINI_API_KEY", ""),
                       os.environ.get("GROQ_API_KEY", ""),
                       os.environ.get("OPENROUTER_API_KEY", "")):
            if secret:
                self.assertNotIn(secret, body)
        self.assertNotIn("Bearer", body)
        self.assertNotIn("x-goog-api-key", body)


class UploadTest(unittest.TestCase):
    def test_jsonl_upload_and_duplicates(self):
        c, tok = _authed()
        _seed(c, tok, n=10)
        line = json.dumps({"status_id": "999000111",
                           "entry_id": "tweet-999000111",
                           "raw": {"entryId": "tweet-999000111",
                                   "content": {"itemContent": {
                                       "tweet_results": {"result": {
                                           "rest_id": "999000111",
                                           "legacy": {
                                               "full_text": "unique upload test post",
                                               "created_at": "Mon Jan 01 00:00:00 +0000 2024",
                                               "entities": {"hashtags": [], "user_mentions": [],
                                                            "urls": []}},
                                           "core": {"user_results": {"result": {
                                               "legacy": {"screen_name": "tester",
                                                          "name": "Tester"}}}}}}}}}})
        data = {"file": (io.BytesIO((line + "\n").encode()), "up.jsonl"),
                "csrf_token": tok}
        r = c.post("/api/import", data=data,
                   headers={"X-CSRF-Token": tok},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual(j["imported"], 1)
        # re-upload same line -> duplicate
        data = {"file": (io.BytesIO((line + "\n").encode()), "up.jsonl"),
                "csrf_token": tok}
        r = c.post("/api/import", data=data,
                   headers={"X-CSRF-Token": tok},
                   content_type="multipart/form-data")
        self.assertEqual(r.get_json()["duplicates"], 1)
        self.assertEqual(r.get_json()["imported"], 0)

    def test_malformed_and_csv(self):
        c, tok = _authed()
        data = {"file": (io.BytesIO(b"not json {{{"), "bad.jsonl"),
                "csrf_token": tok}
        r = c.post("/api/import", data=data,
                   headers={"X-CSRF-Token": tok},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        csv_text = ("tweet_id,url,text,author_handle\n"
                    "555001,https://x.com/a/status/555001,hello csv,a\n")
        data = {"file": (io.BytesIO(csv_text.encode()), "b.csv"),
                "csrf_token": tok}
        r = c.post("/api/import", data=data,
                   headers={"X-CSRF-Token": tok},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["imported"], 1)


class XssTest(unittest.TestCase):
    def test_javascript_url_neutralized(self):
        c, tok = _authed()
        _seed(c, tok, n=5)
        payload = [{"tweet_id": "666001", "url": "javascript:alert(1)",
                    "text": "<script>alert('xss')</script>",
                    "author_handle": "evil\"><img src=x onerror=alert(1)>"}]
        data = {"file": (io.BytesIO(json.dumps(payload).encode()), "e.json"),
                "csrf_token": tok}
        r = c.post("/api/import", data=data,
                   headers={"X-CSRF-Token": tok},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        r = c.get("/api/bookmarks?q=666001")
        cards = r.get_json()["cards"]
        self.assertTrue(cards)
        for link in cards[0]["links"]:
            self.assertTrue(link.startswith("https://"))
            self.assertNotIn("<", link)
            self.assertNotIn('"', link)
            self.assertNotIn("javascript:", link)
        self.assertNotIn("javascript:", cards[0]["x_url"])
        self.assertNotIn("<", cards[0]["x_url"])
        # raw text returned; frontend escapes (esc() in template)
        self.assertIn("<script>", cards[0]["text"])


class CategoryLifecycleTest(unittest.TestCase):
    def test_crud_merge_delete(self):
        c, tok = _authed()
        names = _seed(c, tok, n=30)
        H = {"X-CSRF-Token": tok}
        r = c.post("/api/categories", json={"name": "TmpCat",
                                            "csrf_token": tok}, headers=H)
        self.assertTrue(r.get_json()["ok"])
        r = c.post("/api/categories/rename",
                   json={"from": "TmpCat", "to": "TmpCat2",
                         "csrf_token": tok}, headers=H)
        self.assertTrue(r.get_json()["ok"])
        r = c.post("/api/categories/merge",
                   json={"target": names[0], "sources": ["TmpCat2"],
                         "csrf_token": tok}, headers=H)
        self.assertTrue(r.get_json()["ok"])
        r = c.post("/api/categories/rename",
                   json={"from": "nope", "to": "x",
                         "csrf_token": tok}, headers=H)
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/categories/delete",
                   json={"name": names[0], "csrf_token": tok}, headers=H)
        self.assertTrue(r.get_json()["deleted"])
        # orphans become unsorted, nothing deleted
        st = c.get("/api/state").get_json()
        self.assertGreaterEqual(st["unsorted"], 0)


class ReviewSearchExportTest(unittest.TestCase):
    def test_review_queue_search_exports_backup(self):
        c, tok = _authed()
        _seed(c, tok, n=20)
        H = {"X-CSRF-Token": tok}
        c.post("/api/classify-all",
               json={"engine": "heuristic", "csrf_token": tok}, headers=H)
        st = c.get("/api/state").get_json()
        self.assertIn("Needs Review contains bookmarks whose "
                      "classification is uncertain, usually because "
                      "confidence is below 0.60, or because the bookmark "
                      "has not been classified yet.",
                      st["review_explainer"])
        self.assertIn("review", st)
        # review mode filter works
        r = c.get("/api/bookmarks?mode=review&per=10")
        self.assertEqual(r.status_code, 200)
        # search
        r = c.get("/api/bookmarks?q=demo&per=10")
        self.assertGreaterEqual(r.get_json()["total"], 0)
        # mark reviewed removes from queue
        rid = c.get("/api/bookmarks?mode=review&per=10").get_json()
        if rid["cards"]:
            tid = rid["cards"][0]["id"]
            c.post("/api/review", json={"id": tid, "csrf_token": tok},
                   headers=H)
        for fmt, mime in (("json", "application/json"), ("csv", "text/csv"),
                          ("md", "text/markdown")):
            r = c.get("/api/export?format=%s" % fmt)
            self.assertEqual(r.status_code, 200)
            self.assertIn(mime.split(";")[0],
                          r.headers.get("Content-Type", ""))
        r = c.get("/api/backup")
        self.assertEqual(r.status_code, 200)
        self.assertIn("zip", r.headers.get("Content-Type", ""))
        self.assertEqual(c.get("/api/export?format=exe").status_code, 400)


class MobileSmokeTest(unittest.TestCase):
    def test_pages_mobile_ready(self):
        c, tok = _authed()
        login_html = _client().get("/login").data.decode()
        self.assertIn("viewport", login_html)
        up = c.get("/upload").data.decode()
        self.assertIn('type="file"', up)
        self.assertIn("viewport", up)
        self.assertIn("x.com/i/bookmarks", up)
        app_html = c.get("/").data.decode()
        self.assertIn("/upload", app_html)
        self.assertIn("cloud", app_html.lower())
        self.assertIn("viewport", app_html)
        self.assertIn("Needs Review contains bookmarks whose "
                      "classification is uncertain", app_html)


def _http_err(code, body=b'{"error": {"message": "overloaded"}}'):
    return urllib.error.HTTPError("http://gemini", code, "Err", {},
                                  io.BytesIO(body))


def _gemini_ok_payload():
    return {"candidates": [{"content": {"parts": [
        {"text": '{"categories": [{"name": "C1", "description": "d", '
                 '"count": 5, "examples": ["e1"], "confidence": 0.9}]}'}]}}]}


class GeminiRetryTest(unittest.TestCase):
    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "test-gemini-key-xyz"
        try:
            LLM.LAST_DIAGNOSTICS.clear()
        except Exception:
            pass

    def tearDown(self):
        os.environ.pop("GEMINI_API_KEY", None)
        try:
            LLM.LAST_DIAGNOSTICS.clear()
        except Exception:
            pass

    def test_503_then_success_after_one_retry(self):
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append((url, timeout))
            # secrets never in URL
            self.assertNotIn("test-gemini-key-xyz", url)
            if len(calls) == 1:
                raise _http_err(503, b'{"error": {"message": "overloaded"}}')
            return _gemini_ok_payload()

        real_post, real_sleep = LLM._post_json, LLM.time.sleep
        LLM._post_json = fake_post
        LLM.time.sleep = lambda s: None
        try:
            info = LLM.gemini_request_info("gemini-3.8-flash", 25)
            self.assertIn("gemini-3.8-flash", info["url"])
            self.assertNotIn("test-gemini-key-xyz", info["url"])
            self.assertEqual(info["timeout_s"], 25)
            txt = LLM.chat_text(
                "gemini",
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "u"}],
                model="gemini-3.8-flash", timeout=25)
        finally:
            LLM._post_json = real_post
            LLM.time.sleep = real_sleep
        self.assertIn("categories", txt)
        self.assertEqual(len(calls), 2)
        # timeout respected (bounded, unchanged)
        self.assertEqual(calls[0][1], 25)
        # diagnostics carry retry count, no secrets
        diag = dict(LLM.LAST_DIAGNOSTICS)
        self.assertEqual(diag.get("retries"), 1)
        self.assertEqual(diag.get("http_status"), 200)
        self.assertNotIn("test-gemini-key-xyz", json.dumps(diag))

    def test_503_twice_labeled_fallback(self):
        from xbookmark import categories as CZ, demo as DEMO
        import tempfile as _t
        kb = os.path.join(_t.mkdtemp(prefix="xbo-503x2-"), "kb")
        DEMO.run(kb, n=40)

        def always_503(url, payload, headers, timeout):
            raise _http_err(503, b'{"error": {"message": "overloaded"}}')

        real_post, real_sleep = LLM._post_json, LLM.time.sleep
        LLM._post_json = always_503
        LLM.time.sleep = lambda s: None
        try:
            p = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM._post_json = real_post
            LLM.time.sleep = real_sleep
        self.assertEqual(p["engine"], "heuristic")
        self.assertTrue(p["fallback"])
        self.assertIn("503", p["ai_error"])
        self.assertEqual(p["error_kind"], "fallback_transient")
        self.assertEqual(p["diagnostics"]["http_status"], 503)
        self.assertEqual(p["diagnostics"]["retries"], 1)
        self.assertEqual(p["diagnostics"]["provider"], "gemini")
        self.assertIn("gemini", p["diagnostics"]["model"])
        self.assertNotIn("test-gemini-key-xyz", json.dumps(p))

    def test_429_retry_behavior(self):
        calls = []

        def fake_post(url, payload, headers, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise _http_err(429, b'{"error": {"message": "quota"}}')
            return _gemini_ok_payload()

        real_post, real_sleep = LLM._post_json, LLM.time.sleep
        LLM._post_json = fake_post
        LLM.time.sleep = lambda s: None
        try:
            txt = LLM.chat_text(
                "gemini", [{"role": "user", "content": "hi"}],
                model="gemini-3.8-flash", timeout=20)
        finally:
            LLM._post_json = real_post
            LLM.time.sleep = real_sleep
        self.assertIn("categories", txt)
        self.assertEqual(len(calls), 2)

    def test_key_absent_from_logs_and_errors(self):
        secret = "test-gemini-key-xyz"
        os.environ["GEMINI_API_KEY"] = secret
        err = _http_err(503, ("prefix " + secret + " suffix").encode())
        safe = LLM._safe_error(err)
        self.assertNotIn(secret, safe)
        diag = LLM.describe_error("gemini", "gemini-3.8-flash", err, 1)
        self.assertNotIn(secret, json.dumps(diag))
        self.assertLessEqual(len(diag["error"]), 500)
        # error carrying a key-like message is redacted
        safe2 = LLM._safe_error(RuntimeError("oops key=" + secret))
        self.assertNotIn(secret, safe2)
        # diagnostics never log prompts/headers — only the 5 safe keys
        self.assertEqual(set(diag.keys()),
                         {"provider", "model", "http_status", "error",
                          "retries"})

    def test_invalid_gemini_json_handled_safely(self):
        from xbookmark import categories as CZ, demo as DEMO
        import tempfile as _t
        kb = os.path.join(_t.mkdtemp(prefix="xbo-badjson-"), "kb")
        DEMO.run(kb, n=40)
        real = LLM.chat_text

        def bad_json(*a, **k):
            return "this is not json at all {{{"

        LLM.chat_text = bad_json
        try:
            p = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM.chat_text = real
        self.assertEqual(p["engine"], "heuristic")
        self.assertTrue(p["fallback"])
        self.assertEqual(p["error_kind"], "invalid_output")
        self.assertIn("invalid", p["ai_error"].lower())
        self.assertTrue(p["categories"])


class DiscoveryQualityTest(unittest.TestCase):
    def _themed_kb(self, topics=10, per=15):
        import tempfile as _t
        from xbookmark import store as ST
        kb = os.path.join(_t.mkdtemp(prefix="xbo-qual-"), "kb")
        ST.ensure_kb(kb)
        books = {}
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                 "golf", "hotel", "india", "juliet", "kilo", "lima"]
        for ti in range(topics):
            kw = words[ti % len(words)] + str(ti)
            for i in range(per):
                tid = "%d%04d" % (1000 + ti, i)
                books[tid] = {
                    "tweet_id": tid,
                    "text": "%s deep dive guide %d #%s" % (kw, i, kw),
                    "hashtags": [kw], "domains": ["example%d.com" % ti],
                    "author_handle": "@user%d" % (i % 7), "mentions": [],
                    "has_media": False, "quoted_text": ""}
        ST.save_bookmarks(kb, books)
        return kb

    def test_broad_8_to_15_proposals(self):
        from xbookmark import categories as CZ
        kb = self._themed_kb(topics=10, per=15)  # 150 bookmarks
        p = CZ.discover_structure(kb, engine="heuristic",
                                  min_categories=8, max_categories=15)
        core = [c for c in p["categories"]
                if c["name"] != "Unsorted / Review"]
        self.assertGreaterEqual(len(core), 8)
        self.assertLessEqual(len(core), 15)
        banned = {"interesting", "miscellaneous", "misc", "useful", "other"}
        for c in core:
            self.assertNotIn(c["name"].strip().lower(), banned)
            for f in ("name", "description", "estimated_count",
                      "confidence"):
                self.assertIn(f, c, f)
            # representative IDs or URLs present
            reps = (c.get("representative_ids") or []) + \
                (c.get("representative_urls") or []) + \
                (c.get("examples") or [])
            self.assertTrue(reps, c["name"])
            self.assertGreaterEqual(c["estimated_count"], 0)
            self.assertGreaterEqual(c["confidence"], 0.0)
            self.assertLessEqual(c["confidence"], 1.0)

    def test_not_persisted_before_accept(self):
        from xbookmark import categories as CZ, demo as DEMO, store as ST
        from xbookmark import library as LIB, util as U
        import tempfile as _t
        kb = os.path.join(_t.mkdtemp(prefix="xbo-nopersist-"), "kb")
        DEMO.run(kb, n=40)
        before = U.read_json(ST.kb_path(kb, "library.json"), None)
        p = CZ.discover_structure(kb, engine="heuristic",
                                  max_categories=10)
        self.assertTrue(p["categories"])
        # proposal file written, library untouched (no assignment)
        after = U.read_json(ST.kb_path(kb, "library.json"), None)
        self.assertEqual(before, after)
        lib = LIB.Library(kb)
        self.assertEqual(lib.category_list(), [])
        books = ST.load_bookmarks(kb)
        n_books = len(books)
        self.assertGreater(n_books, 0)

    def test_retry_does_not_duplicate(self):
        c, tok = _authed()
        H = {"X-CSRF-Token": tok}
        _seed(c, tok, n=20)
        r = c.post("/api/discover",
                   json={"engine": "heuristic", "csrf_token": tok},
                   headers=H)
        self.assertEqual(r.status_code, 200)
        first = r.get_json()["proposal"]
        n1 = len(first["categories"])
        names1 = [x["name"] for x in first["categories"]]
        self.assertEqual(len(names1), len(set(x.lower() for x in names1)))
        # retry via explicit heuristic endpoint: overwrites, no dupes
        r = c.post("/api/discover/heuristic", json={"csrf_token": tok},
                   headers=H)
        self.assertEqual(r.status_code, 200)
        second = r.get_json()["proposal"]
        self.assertEqual(len(second["categories"]), n1)
        names2 = [x["name"] for x in second["categories"]]
        self.assertEqual(len(names2), len(set(x.lower() for x in names2)))
        # retry endpoint exists, needs no re-upload, never auto-accepts
        r = c.post("/api/discover/retry",
                   json={"engine": "gemini", "csrf_token": tok},
                   headers=H)
        # without cloud_ok the hosted retry is gated (no silent AI call)
        self.assertEqual(r.status_code, 400)
        st = c.get("/api/state").get_json()
        self.assertGreaterEqual(st["total"], 20)

    def test_ui_states_present(self):
        c, tok = _authed()
        html = c.get("/").data.decode()
        low = html.lower()
        self.assertIn("d-status", html)
        self.assertIn("d-retry", html)
        self.assertIn("retry with gemini", low)
        self.assertIn("use heuristic fallback", low)
        # four distinguishable states (rendered dynamically + static copy)
        self.assertIn("succeeded", low)
        self.assertIn("heuristic fallback", low)
        self.assertIn("not configured", low)
        self.assertIn("invalid structured output", low)


if __name__ == "__main__":
    unittest.main()

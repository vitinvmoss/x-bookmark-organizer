"""Multi-provider AI layer tests: configuration, auto fallback chain,
per-provider adapters, safe diagnostics, and strict discovery validation.

All offline: hosted providers are monkeypatched; no X calls, no network,
no real API keys.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xbookmark import categories as CZ
from xbookmark import demo as DEMO
from xbookmark import llm as LLM
from xbookmark import store as ST

_ENV_KEYS = ("LLM_PROVIDER", "LLM_FALLBACK_PROVIDERS",
             "LLM_ALLOW_HEURISTIC_FALLBACK", "GEMINI_API_KEY",
             "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_MODEL",
             "GROQ_MODEL", "OPENROUTER_MODEL")


class _Env:
    """Save/restore the provider env vars around a test."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k in _ENV_KEYS:
            self.saved[k] = os.environ.get(k)
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.setdefault("LLM_PROVIDER", "auto")
        for k, v in self.values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k in _ENV_KEYS:
            if self.saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = self.saved[k]
        return False


def _kb(n=24):
    tmp = tempfile.mkdtemp(prefix="xbo-prov-")
    kb = os.path.join(tmp, "kb")
    DEMO.run(kb, n=n)
    return kb


def _ids(kb):
    return list(ST.load_bookmarks(kb))


def _valid_cats(ids, n=3, prefix="Topic"):
    cats = []
    for i in range(n):
        chunk = ids[i * 3:i * 3 + 3] or ids[:3]
        cats.append({
            "name": "%s %d" % (prefix, i + 1),
            "description": "A grounded topic number %d." % (i + 1),
            "estimated_count": max(1, len(chunk)),
            "representative_ids": chunk,
            "representative_urls": [],
            "examples": [],
            "confidence": round(0.70 + 0.02 * i, 2)})
    return cats


def _stage_a(ids, themes=3):
    out = []
    for i in range(themes):
        out.append({"theme": "Theme %d" % (i + 1),
                    "description": "Recurring theme %d." % (i + 1),
                    "evidence_ids": ids[i * 3:i * 3 + 3] or ids[:3],
                    "approx_count": 5})
    return json.dumps({"themes": out})


def _http(code):
    return urllib.error.HTTPError(
        "http://provider", code, "Err", {},
        io.BytesIO(b'{"error": {"message": "overloaded"}}'))


class _FakeProvider:
    """Monkeypatchable chat_text that fails/succeeds per provider."""

    def __init__(self, ids, fail=(), bad_json=(), invalid=(), n_cats=3):
        self.ids = ids
        self.fail = set(fail)
        self.bad_json = set(bad_json)
        self.invalid = set(invalid)
        self.n_cats = n_cats
        self.calls = []

    def __call__(self, prov, messages, model=None, timeout=None):
        self.calls.append(prov)
        if prov in self.fail:
            raise _http(503)
        if prov in self.bad_json:
            return "this is not json at all {{{"
        sys_msg = " ".join(m.get("content", "") for m in messages
                           if m.get("role") == "system")
        if "recurring themes" in sys_msg:
            return _stage_a(self.ids, themes=max(3, self.n_cats))
        if prov in self.invalid:
            return json.dumps({"categories": [
                {"name": "Only One", "description": "d",
                 "estimated_count": 5,
                 "representative_ids": self.ids[:3],
                 "examples": [], "confidence": 0.8}]})
        return json.dumps({"categories": _valid_cats(self.ids,
                                                     n=self.n_cats)})

    def providers(self):
        return list(self.calls)


class ProviderConfigurationTest(unittest.TestCase):
    def test_auto_chain_only_configured_keys(self):
        with _Env(GEMINI_API_KEY="g", OPENROUTER_API_KEY="o"):
            self.assertEqual(LLM.provider_default(), "auto")
            self.assertEqual(LLM.resolve_chain_names("auto"),
                             ["gemini", "openrouter"])
            # groq missing -> never included
            self.assertNotIn("groq", LLM.resolve_chain_names("auto"))
            self.assertEqual(LLM.primary_hosted_provider(), "gemini")
            self.assertEqual(LLM.configured_hosted_providers(),
                             ["gemini", "openrouter"])

    def test_missing_key_provider_skipped(self):
        with _Env():
            self.assertEqual(LLM.resolve_chain_names("auto"), [])

    def test_explicit_mode_chain_is_single(self):
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            self.assertEqual(LLM.resolve_chain_names("gemini"), ["gemini"])
            self.assertEqual(LLM.resolve_chain_names("groq"), ["groq"])
            self.assertEqual(LLM.resolve_chain_names("openrouter"),
                             ["openrouter"])
            self.assertEqual(LLM.resolve_chain_names("heuristic"),
                             ["heuristic"])

    def test_fallback_order_is_configurable(self):
        with _Env(GROQ_API_KEY="q", OPENROUTER_API_KEY="o",
                  GEMINI_API_KEY="g",
                  LLM_FALLBACK_PROVIDERS="openrouter,groq,gemini"):
            self.assertEqual(LLM.resolve_chain_names("auto"),
                             ["openrouter", "groq", "gemini"])

    def test_adapters_report_configured_and_models(self):
        with _Env(GEMINI_API_KEY="g", GROQ_MODEL="my-groq"):
            gem = LLM.get_provider("gemini")
            groq = LLM.get_provider("groq")
            self.assertTrue(gem.is_configured())
            self.assertFalse(groq.is_configured())
            self.assertEqual(gem.model, "gemini-3.8-flash")
            self.assertEqual(groq.model, "my-groq")
            self.assertEqual(gem.name, "gemini")
            self.assertTrue(LLM.get_provider("heuristic").is_configured())

    def test_config_report_has_no_secrets(self):
        with _Env(GEMINI_API_KEY="secret-gemini-value"):
            st = LLM.configured_provider_status()
            blob = json.dumps(st)
            self.assertNotIn("secret-gemini-value", blob)
            self.assertTrue(st["gemini"]["configured"])
            self.assertEqual(st["mode"], "auto")
            self.assertIn("fallback_chain", st)


class AutoFallbackTest(unittest.TestCase):
    def _run(self, kb, fake, engine="auto"):
        real = LLM.chat_text
        LLM.chat_text = fake
        try:
            return CZ.discover_structure(kb, engine=engine)
        finally:
            LLM.chat_text = real

    def test_gemini_503_then_groq_succeeds(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            fake = _FakeProvider(ids, fail=["gemini"])
            p = self._run(kb, fake)
        self.assertEqual(p["error_kind"], "succeeded")
        self.assertEqual(p["successful_provider"], "groq")
        self.assertEqual(p["provider_used"], "groq")
        self.assertIn("gemini", p["fallback_chain"])
        self.assertIn("groq", p["fallback_chain"])
        self.assertEqual(p["attempts"][0]["provider"], "gemini")
        self.assertFalse(p["attempts"][0]["success"])
        self.assertEqual(p["attempts"][0]["http_status"], 503)
        self.assertEqual(p["attempts"][1]["provider"], "groq")
        self.assertTrue(p["attempts"][1]["success"])
        # openrouter was never needed
        self.assertNotIn("openrouter", fake.providers())

    def test_gemini_503_groq_503_openrouter_succeeds(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            fake = _FakeProvider(ids, fail=["gemini", "groq"])
            p = self._run(kb, fake)
        self.assertEqual(p["successful_provider"], "openrouter")
        self.assertEqual([a["provider"] for a in p["attempts"]],
                         ["gemini", "groq", "openrouter"])
        self.assertEqual(p["attempts"][0]["http_status"], 503)
        self.assertEqual(p["attempts"][1]["http_status"], 503)

    def test_all_providers_fail_reports_every_attempt(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            fake = _FakeProvider(ids, fail=["gemini", "groq", "openrouter"])
            p = self._run(kb, fake)
        self.assertEqual(p["categories"], [])
        self.assertEqual(p["provider_used"], "none")
        self.assertEqual(p["successful_provider"], "none")
        self.assertTrue(p["heuristic_available"])
        self.assertEqual([a["provider"] for a in p["attempts"]],
                         ["gemini", "groq", "openrouter"])
        self.assertTrue(all(not a["success"] for a in p["attempts"]))
        self.assertIn("Gemini", p["ai_error"])
        self.assertIn("Groq", p["ai_error"])
        self.assertIn("OpenRouter", p["ai_error"])

    def test_invalid_json_moves_to_next_provider(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q"):
            fake = _FakeProvider(ids, bad_json=["gemini"])
            p = self._run(kb, fake)
        self.assertEqual(p["successful_provider"], "groq")
        self.assertEqual(p["attempts"][0]["error_kind"], "invalid_output")
        self.assertFalse(p["attempts"][0]["success"])

    def test_invalid_structured_output_moves_to_next_provider(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q"):
            fake = _FakeProvider(ids, invalid=["gemini"])
            p = self._run(kb, fake)
        self.assertEqual(p["successful_provider"], "groq")
        self.assertEqual(p["attempts"][0]["error_kind"], "invalid_output")

    def test_unconfigured_provider_is_skipped(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(OPENROUTER_API_KEY="o"):
            fake = _FakeProvider(ids)
            p = self._run(kb, fake)
        self.assertEqual(fake.providers(), ["openrouter", "openrouter"])
        self.assertEqual(p["successful_provider"], "openrouter")

    def test_explicit_mode_does_not_invoke_other_providers(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            fake = _FakeProvider(ids, fail=["gemini"])
            p = self._run(kb, fake, engine="gemini")
        self.assertEqual(set(fake.providers()), {"gemini"})
        self.assertEqual(p["categories"], [])
        self.assertEqual(p["error_kind"], "fallback_transient")
        self.assertEqual(p["diagnostics"]["http_status"], 503)

    def test_heuristic_not_used_in_auto_unless_enabled(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            fake = _FakeProvider(ids, fail=["gemini", "groq", "openrouter"])
            p = self._run(kb, fake)
        self.assertEqual(p["categories"], [])
        self.assertNotIn("heuristic", p["fallback_chain"])
        self.assertFalse(p["heuristic_fallback_used"])

    def test_heuristic_final_fallback_when_enabled(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o",
                  LLM_ALLOW_HEURISTIC_FALLBACK="true"):
            fake = _FakeProvider(ids, fail=["gemini", "groq", "openrouter"])
            p = self._run(kb, fake)
        self.assertEqual(p["provider_used"], "heuristic")
        self.assertEqual(p["successful_provider"], "heuristic")
        self.assertTrue(p["heuristic_fallback_used"])
        self.assertIn("heuristic", p["fallback_chain"])
        self.assertTrue(p["categories"])

    def test_attempt_records_have_safe_contract(self):
        kb = _kb()
        ids = _ids(kb)
        with _Env(GEMINI_API_KEY="g"):
            fake = _FakeProvider(ids, fail=["gemini"])
            p = self._run(kb, fake)
        for a in p["attempts"]:
            self.assertEqual(set(a.keys()), {
                "provider", "model", "http_status", "error_kind",
                "error_message_truncated", "retry_count", "duration_ms",
                "success"})
            self.assertLessEqual(len(a["error_message_truncated"]), 500)


class RetryBehaviorTest(unittest.TestCase):
    def test_transient_503_retried_once_then_next_provider(self):
        kb = _kb()
        calls = {"gemini": 0, "groq": 0}

        def fake_post(url, payload, headers, timeout):
            if "generativelanguage" in url:
                calls["gemini"] += 1
            elif "groq" in url:
                calls["groq"] += 1
            raise _http(503)

        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q"):
            real_post, real_sleep = LLM._post_json, LLM.time.sleep
            LLM._post_json = fake_post
            LLM.time.sleep = lambda s: None
            try:
                p = CZ.discover_structure(kb, engine="auto")
            finally:
                LLM._post_json = real_post
                LLM.time.sleep = real_sleep
        # gemini made exactly 2 HTTP attempts (1 + 1 controlled retry)
        self.assertEqual(calls["gemini"], 2)
        # then auto moved on to groq (also bounded to 2 attempts)
        self.assertEqual(calls["groq"], 2)
        self.assertEqual(p["attempts"][0]["provider"], "gemini")
        self.assertEqual(p["attempts"][0]["retry_count"], 1)

    def test_non_retryable_401_not_retried(self):
        calls = {"n": 0}

        def fake_post(url, payload, headers, timeout):
            calls["n"] += 1
            raise _http(401)

        with _Env(GEMINI_API_KEY="g"):
            real_post, real_sleep = LLM._post_json, LLM.time.sleep
            LLM._post_json = fake_post
            LLM.time.sleep = lambda s: None
            try:
                with self.assertRaises(urllib.error.HTTPError):
                    LLM.chat_text("gemini",
                                  [{"role": "user", "content": "x"}])
            finally:
                LLM._post_json = real_post
                LLM.time.sleep = real_sleep
        self.assertEqual(calls["n"], 1)

    def test_openai_compat_response_format_downgrade(self):
        calls = {"n": 0}

        def fake_post(url, payload, headers, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "http://groq", 400, "Bad", {},
                    io.BytesIO(b'{"error": {"message": '
                               b'"response_format is not supported"}}'))
            self.assertNotIn("response_format", payload)
            return {"choices": [{"message": {"content": "{}"}}]}

        with _Env(GROQ_API_KEY="q"):
            real_post = LLM._post_json
            LLM._post_json = fake_post
            try:
                out = LLM.chat_text("groq",
                                    [{"role": "user", "content": "x"}])
            finally:
                LLM._post_json = real_post
        self.assertEqual(out, "{}")
        self.assertEqual(calls["n"], 2)


class ConnectionTestTest(unittest.TestCase):
    def test_auto_test_stops_at_first_success(self):
        ids = []

        def fake(prov, messages, model=None, timeout=None):
            if prov == "gemini":
                raise _http(503)
            return '{"ok": true}'

        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q",
                  OPENROUTER_API_KEY="o"):
            real = LLM.chat_text
            LLM.chat_text = fake
            try:
                r = LLM.test_connection("auto")
            finally:
                LLM.chat_text = real
        self.assertTrue(r["success"])
        self.assertEqual(r["provider"], "groq")
        by = {a["provider"]: a for a in r["attempts"]}
        self.assertFalse(by["gemini"]["success"])
        self.assertEqual(by["gemini"]["http_status"], 503)
        self.assertTrue(by["groq"]["success"])
        # Groq succeeded, so OpenRouter is reported as not attempted.
        self.assertIn("openrouter", by)
        self.assertIs(by["openrouter"]["attempted"], False)
        self.assertFalse(by["openrouter"]["success"])

    def test_unconfigured_provider_actionable_message(self):
        with _Env():
            r = LLM.test_provider_connection("groq")
        self.assertFalse(r["success"])
        self.assertFalse(r["configured"])
        self.assertIn("Groq", r["error"])
        self.assertIn("API key", r["error"])


class SecurityTest(unittest.TestCase):
    def test_keys_never_in_discovery_payload(self):
        kb = _kb()
        ids = _ids(kb)
        secret = "super-secret-key-value-xyz"
        with _Env(GEMINI_API_KEY=secret, GROQ_API_KEY=secret + "-g"):
            fake = _FakeProvider(ids, fail=["gemini"])
            real = LLM.chat_text
            LLM.chat_text = fake
            try:
                p = CZ.discover_structure(kb, engine="auto")
            finally:
                LLM.chat_text = real
        blob = json.dumps(p)
        self.assertNotIn(secret, blob)
        self.assertNotIn("Bearer", blob)
        self.assertNotIn("x-goog-api-key", blob)

    def test_provider_error_body_does_not_leak_key(self):
        secret = "leak-me-key-12345"
        with _Env(GEMINI_API_KEY=secret):
            err = urllib.error.HTTPError(
                "http://gemini", 503, "E", {},
                io.BytesIO(("prefix " + secret + " suffix").encode()))
            safe = LLM._safe_error(err, "gemini")
            diag = LLM.describe_error("gemini", "m", err, 1)
        self.assertNotIn(secret, safe)
        self.assertNotIn(secret, json.dumps(diag))
        self.assertEqual(set(diag.keys()),
                         {"provider", "model", "http_status", "error",
                          "retries"})

    def test_provider_response_cannot_inject_credentials(self):
        kb = _kb()
        ids = _ids(kb)
        injected = "Bearer injected-secret-123 x-goog-api-key=abc"

        class Evil:
            def __call__(self, prov, messages, model=None, timeout=None):
                if prov == "gemini":
                    raise urllib.error.HTTPError(
                        "http://gemini", 503, "E", {},
                        io.BytesIO(injected.encode()))
                sys_msg = " ".join(m.get("content", "")
                                   for m in messages
                                   if m.get("role") == "system")
                if "recurring themes" in sys_msg:
                    return _stage_a(ids)
                return json.dumps({"categories": _valid_cats(ids)})

        with _Env(GEMINI_API_KEY="g", GROQ_API_KEY="q"):
            real = LLM.chat_text
            LLM.chat_text = Evil()
            try:
                p = CZ.discover_structure(kb, engine="auto")
            finally:
                LLM.chat_text = real
        blob = json.dumps(p)
        self.assertNotIn("injected-secret-123", blob)
        self.assertNotIn("x-goog-api-key=abc", blob)

    def test_ai_status_endpoint_has_no_secrets(self):
        secret = "endpoint-secret-key-9876"
        with _Env(GEMINI_API_KEY=secret, GROQ_API_KEY=secret + "q",
                  OPENROUTER_API_KEY=secret + "o"):
            try:
                import server as SRV
            except Exception:
                self.skipTest("server import unavailable")
            c = SRV.app.test_client()
            # unauthenticated -> 401, but the status config must never leak
            r = c.get("/api/ai-status")
            self.assertIn(r.status_code, (200, 401))
            body = r.get_data(as_text=True)
            self.assertNotIn(secret, body)


class DiscoveryIntegrationTest(unittest.TestCase):
    def test_two_stage_broad_categories(self):
        kb = _kb(90)
        ids = _ids(kb)
        prompts = []
        real = LLM.chat_text

        def fake(prov, messages, model=None, timeout=None):
            sys_msg = " ".join(m.get("content", "") for m in messages
                               if m.get("role") == "system")
            prompts.append(sys_msg)
            if "recurring themes" in sys_msg:
                return _stage_a(ids, themes=10)
            return json.dumps({"categories": _valid_cats(ids, n=10)})

        with _Env(GEMINI_API_KEY="g"):
            LLM.chat_text = fake
            try:
                p = CZ.discover_structure(kb, engine="gemini")
            finally:
                LLM.chat_text = real
        self.assertEqual(len(prompts), 2)
        core = [c for c in p["categories"]
                if c["name"] != "Unsorted / Review"]
        self.assertGreaterEqual(len(core), 8)
        self.assertLessEqual(len(core), 15)
        for c in core:
            self.assertTrue(c["name"].strip())
            self.assertTrue(c["description"].strip())
            self.assertGreater(c["estimated_count"], 0)

    def test_passing_result_only_after_strict_validation(self):
        kb = _kb()
        ids = _ids(kb)
        # A provider whose stage-B output is structurally JSON but has no
        # grounded evidence must NOT be accepted as an AI result.
        with _Env(GEMINI_API_KEY="g"):
            fake = _FakeProvider(ids, invalid=["gemini"])
            real = LLM.chat_text
            LLM.chat_text = fake
            try:
                p = CZ.discover_structure(kb, engine="gemini")
            finally:
                LLM.chat_text = real
        self.assertEqual(p["categories"], [])
        self.assertEqual(p["provider_used"], "none")

    def test_proposals_not_persisted_before_accept(self):
        from xbookmark import library as LIB
        from xbookmark import util as U
        kb = _kb()
        ids = _ids(kb)
        before = U.read_json(ST.kb_path(kb, "library.json"), None)
        with _Env(GEMINI_API_KEY="g"):
            fake = _FakeProvider(ids)
            real = LLM.chat_text
            LLM.chat_text = fake
            try:
                p = CZ.discover_structure(kb, engine="gemini")
            finally:
                LLM.chat_text = real
        self.assertTrue(p["categories"])
        after = U.read_json(ST.kb_path(kb, "library.json"), None)
        self.assertEqual(before, after)
        self.assertEqual(LIB.Library(kb).category_list(), [])


if __name__ == "__main__":
    unittest.main()

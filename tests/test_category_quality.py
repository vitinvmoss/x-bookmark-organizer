"""Focused tests for Discover Categories quality + failure handling.

Covers the required strict validation, the explicit-heuristic contract,
the two-stage compact Gemini discovery, provider-error states (503 after
retry -> no proposals), and the no-duplicate/no-persist retry behavior.

All offline: the hosted provider is monkeypatched; no X calls, no network.
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


def _kb(n=30):
    tmp = tempfile.mkdtemp(prefix="xbo-qual2-")
    kb = os.path.join(tmp, "kb")
    DEMO.run(kb, n=n)
    return kb


def _books():
    return {
        "1": {"text": "deep dive into rust async patterns",
              "quoted_text": "", "author_handle": "@a",
              "urls": [{"expanded": "https://github.com/x"}]},
        "2": {"text": "bitcoin halving analysis explained",
              "quoted_text": "", "author_handle": "@b",
              "urls": [{"expanded": "https://coinbase.com/y"}]},
    }


def _valid(**over):
    item = {"name": "Rust Async Patterns",
            "description": "Rust concurrency and async runtimes.",
            "estimated_count": 12,
            "representative_ids": ["1"],
            "representative_urls": ["https://github.com/x"],
            "examples": ["deep dive into rust async patterns"],
            "confidence": 0.82}
    item.update(over)
    return item


class StrictValidationUnitTest(unittest.TestCase):
    def test_accepts_valid_grounded_proposal(self):
        valid, rejected = CZ._strict_validate(
            [_valid()], _books(), 100, 15)
        self.assertEqual(len(valid), 1)
        self.assertFalse(rejected)
        c = valid[0]
        self.assertEqual(c["name"], "Rust Async Patterns")
        self.assertEqual(c["estimated_count"], 12)
        self.assertEqual(c["representative_ids"], ["1"])

    def test_rejects_blank_name(self):
        valid, rejected = CZ._strict_validate(
            [_valid(name="   ")], _books(), 100, 15)
        self.assertEqual(valid, [])
        self.assertIn("blank category name", rejected[0]["reasons"])

    def test_rejects_blank_description(self):
        valid, rejected = CZ._strict_validate(
            [_valid(description="")], _books(), 100, 15)
        self.assertEqual(valid, [])
        self.assertIn("blank description", rejected[0]["reasons"])

    def test_rejects_bare_50pct_confidence(self):
        valid, rejected = CZ._strict_validate(
            [_valid(confidence=0.5)], _books(), 100, 15)
        self.assertEqual(valid, [])
        self.assertIn("unjustified 50% confidence",
                      rejected[0]["reasons"])

    def test_allows_50pct_when_explicitly_justified(self):
        valid, rejected = CZ._strict_validate(
            [_valid(confidence=0.5, confidence_justified=True)],
            _books(), 100, 15)
        self.assertEqual(len(valid), 1)
        self.assertFalse(rejected)
        valid2, _ = CZ._strict_validate(
            [_valid(confidence=0.5,
                    confidence_reason="cluster split evenly")],
            _books(), 100, 15)
        self.assertEqual(len(valid2), 1)

    def test_rejects_generic_names(self):
        for nm in ("Other", "Miscellaneous", "Interesting", "Random",
                   "Useful", "General Saves"):
            valid, rejected = CZ._strict_validate(
                [_valid(name=nm)], _books(), 100, 15)
            self.assertEqual(valid, [], nm)
            self.assertIn("generic name", rejected[0]["reasons"], nm)

    def test_rejects_no_examples(self):
        # unknown IDs, invented example, and unknown URL -> no grounded
        # evidence at all
        valid, rejected = CZ._strict_validate(
            [_valid(representative_ids=["nope"],
                    representative_urls=["https://nope.example/z"],
                    examples=["totally invented text zzz"])],
            _books(), 100, 15)
        self.assertEqual(valid, [])
        self.assertIn("no grounded examples", rejected[0]["reasons"])

    def test_rejects_zero_estimated_count(self):
        valid, rejected = CZ._strict_validate(
            [_valid(estimated_count=0)], _books(), 100, 15)
        self.assertEqual(valid, [])
        self.assertIn("zero estimated count", rejected[0]["reasons"])

    def test_removes_duplicate_and_near_duplicate_names(self):
        raw = [
            _valid(name="AI & ML", confidence=0.8),
            _valid(name="AI and ML", confidence=0.7),
            _valid(name="ai-ml", confidence=0.9),
        ]
        valid, rejected = CZ._strict_validate(raw, _books(), 100, 15)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["name"], "AI & ML")
        self.assertTrue(any("duplicate name" in r["reasons"]
                            for r in rejected))

    def test_normalizer_collides_on_punctuation(self):
        self.assertEqual(CZ._norm_name("AI & ML"),
                         CZ._norm_name("AI and ML"))
        self.assertEqual(CZ._norm_name("ai-ml"), CZ._norm_name("AI & ML"))


class ExplicitHeuristicTest(unittest.TestCase):
    def test_explicit_heuristic_names_are_valid(self):
        p = CZ.discover_structure(_kb(60), engine="heuristic",
                                  min_categories=8, max_categories=15)
        self.assertEqual(p["error_kind"], "succeeded")
        self.assertEqual(p["provider_used"], "heuristic")
        core = [c for c in p["categories"]
                if c["name"] != "Unsorted / Review"]
        self.assertGreaterEqual(len(core), 3)
        for c in core:
            self.assertTrue(c["name"].strip(), c)
            self.assertTrue(c["description"].strip(), c)
            self.assertGreater(c["estimated_count"], 0)
            self.assertNotAlmostEqual(c["confidence"], 0.5, places=9)
            self.assertTrue(c["examples"] or c["representative_ids"])

    def test_no_zero_count_placeholder_when_all_classified(self):
        p = CZ.discover_structure(_kb(60), engine="heuristic")
        # demo data fully clusters: no zero-count Unsorted placeholder
        self.assertEqual(p["signal_summary"]["unclustered"], 0)
        self.assertFalse(any(c["name"] == "Unsorted / Review"
                             for c in p["categories"]))


class TwoStageGeminiTest(unittest.TestCase):
    def setUp(self):
        os.environ["GEMINI_API_KEY"] = "test-key-two-stage"
        LLM.LAST_DIAGNOSTICS.clear()

    def tearDown(self):
        os.environ.pop("GEMINI_API_KEY", None)
        LLM.LAST_DIAGNOSTICS.clear()

    def test_two_stage_compact_prompts_and_valid_proposals(self):
        kb = _kb(24)
        ids = list(ST.load_bookmarks(kb))
        prompts = []
        real = LLM.chat_text

        def fake(prov, messages, model=None, timeout=None):
            sys_msg = " ".join(m.get("content", "") for m in messages
                               if m.get("role") == "system")
            user_msg = " ".join(m.get("content", "") for m in messages
                                if m.get("role") == "user")
            prompts.append((sys_msg, user_msg))
            if "recurring themes" in sys_msg:
                return json.dumps({"themes": [
                    {"theme": "AI", "description": "LLM prompting.",
                     "evidence_ids": ids[:3], "approx_count": 8},
                    {"theme": "Crypto", "description": "Bitcoin.",
                     "evidence_ids": ids[3:6], "approx_count": 8},
                    {"theme": "Coding", "description": "Rust.",
                     "evidence_ids": ids[6:9], "approx_count": 8},
                ]})
            return json.dumps({"categories": [
                {"name": "AI Prompting",
                 "description": "Prompts and LLMs.",
                 "estimated_count": 8, "representative_ids": ids[:3],
                 "examples": [], "confidence": 0.83},
                {"name": "Crypto Markets",
                 "description": "Bitcoin analysis.",
                 "estimated_count": 8, "representative_ids": ids[3:6],
                 "examples": [], "confidence": 0.79},
                {"name": "Software Engineering",
                 "description": "Rust and async.",
                 "estimated_count": 8, "representative_ids": ids[6:9],
                 "examples": [], "confidence": 0.75},
            ]})

        LLM.chat_text = fake
        try:
            p = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM.chat_text = real
        # exactly two hosted calls: stage A then stage B
        self.assertEqual(len(prompts), 2)
        self.assertIn("recurring themes", prompts[0][0])
        self.assertIn("category structure", prompts[1][0])
        # compact per-bookmark representation: ID | @author | url | text
        self.assertIn("| @", prompts[0][1])
        self.assertIn("OpenAI" if False else "ids:", prompts[0][1])
        # result is a valid AI proposal
        self.assertEqual(p["provider_used"], "gemini")
        self.assertEqual(p["error_kind"], "succeeded")
        self.assertEqual(p["stage_a_themes"], 3)
        core = [c for c in p["categories"]
                if c["name"] != "Unsorted / Review"]
        self.assertGreaterEqual(len(core), 3)
        self.assertTrue(all(c["name"].strip() for c in core))
        self.assertTrue(all(c["description"].strip() for c in core))

    def test_compact_line_truncates_and_carries_id_author_url(self):
        line = CZ._compact_line("123", {
            "author_handle": "@someone",
            "text": "x" * 400,
            "urls": [{"expanded": "https://example.com/a"}]})
        self.assertTrue(line.startswith("123 | @someone | "
                                        "https://example.com/a | "))
        self.assertLessEqual(len(line), 121 + 40 + 40 + 10)
        self.assertNotIn("x" * 400, line)

    def test_503_after_retry_error_state_no_proposals(self):
        kb = _kb(24)

        def always_503(url, payload, headers, timeout):
            raise urllib.error.HTTPError(
                "http://gemini", 503, "Err", {},
                io.BytesIO(b'{"error": {"message": "overloaded"}}'))

        real_post, real_sleep = LLM._post_json, LLM.time.sleep
        LLM._post_json = always_503
        LLM.time.sleep = lambda s: None
        try:
            p = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM._post_json = real_post
            LLM.time.sleep = real_sleep
        self.assertEqual(p["categories"], [])
        self.assertEqual(p["provider_used"], "none")
        self.assertEqual(p["error_kind"], "fallback_transient")
        self.assertEqual(p["diagnostics"]["http_status"], 503)
        self.assertEqual(p["diagnostics"]["retries"], 1)
        self.assertTrue(p["heuristic_available"])
        self.assertNotIn("test-key-two-stage", json.dumps(p))

    def test_fewer_than_three_valid_proposals_is_error(self):
        kb = _kb(24)
        ids = list(ST.load_bookmarks(kb))
        real = LLM.chat_text

        def fake(prov, messages, model=None, timeout=None):
            sys_msg = " ".join(m.get("content", "") for m in messages
                               if m.get("role") == "system")
            if "recurring themes" in sys_msg:
                return json.dumps({"themes": [
                    {"theme": "AI", "description": "LLM.",
                     "evidence_ids": ids[:3], "approx_count": 8}]})
            # only one strictly-valid proposal, one blank-description
            return json.dumps({"categories": [
                {"name": "AI Prompting",
                 "description": "Prompts.",
                 "estimated_count": 8, "representative_ids": ids[:3],
                 "examples": [], "confidence": 0.8},
                {"name": "Blank Desc", "description": "",
                 "estimated_count": 5, "representative_ids": ids[3:5],
                 "examples": [], "confidence": 0.7},
            ]})

        LLM.chat_text = fake
        try:
            p = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM.chat_text = real
        self.assertEqual(p["categories"], [])
        self.assertNotEqual(p["error_kind"], "succeeded")
        self.assertIn("strictly valid", p["ai_error"])
        self.assertTrue(p["heuristic_available"])

    def test_error_state_does_not_clobber_good_proposal(self):
        kb = _kb(24)
        good = CZ.discover_structure(kb, engine="heuristic")
        names_before = [c["name"] for c in good["categories"]]
        real_post, real_sleep = LLM._post_json, LLM.time.sleep
        LLM._post_json = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError("http://gemini", 503, "E", {},
                                   io.BytesIO(b"{}")))
        LLM.time.sleep = lambda s: None
        try:
            bad = CZ.discover_structure(kb, engine="gemini")
        finally:
            LLM._post_json = real_post
            LLM.time.sleep = real_sleep
        self.assertEqual(bad["categories"], [])
        # stored proposal still the previous good one
        stored, err = CZ.get_proposal(kb)
        self.assertIsNone(err)
        self.assertEqual([c["name"] for c in stored["categories"]],
                         names_before)


class RetryNoDuplicateTest(unittest.TestCase):
    def test_repeated_discovery_overwrites_without_duplicates(self):
        from xbookmark import library as LIB
        from xbookmark import util as U
        kb = _kb(40)
        lib_before = U.read_json(ST.kb_path(kb, "library.json"), None)
        seen = []
        for _ in range(3):
            p = CZ.discover_structure(kb, engine="heuristic",
                                      max_categories=15)
            names = [c["name"] for c in p["categories"]]
            self.assertEqual(len(names), len(set(n.lower() for n in names)))
            seen.append(names)
        for names in seen[1:]:
            self.assertEqual(names, seen[0])
        # nothing persisted to the working library before Accept
        self.assertEqual(U.read_json(ST.kb_path(kb, "library.json"), None),
                         lib_before)
        self.assertEqual(LIB.Library(kb).category_list(), [])


if __name__ == "__main__":
    unittest.main()

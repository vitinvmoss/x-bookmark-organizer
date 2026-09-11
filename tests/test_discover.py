"""Stage 3 / Milestone 1 tests: whole-library "Discover Categories".

Covers the local clustering pre-pass, the AI-free heuristic proposal path,
validation clamps, and the explicit accept/review workflow. All offline;
no X calls, no network.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ClustersTest(unittest.TestCase):
    def test_build_finds_clusters_and_signals(self):
        from xbookmark import clusters as CL
        books = {}
        for i in range(30):
            books[str(1000 + i)] = {
                "tweet_id": str(1000 + i),
                "text": "rust async patterns guide %d #rust" % i,
                "hashtags": ["rust"], "domains": ["github.com"],
                "author_handle": "@dev%d" % (i % 5), "mentions": [],
                "has_media": False, "quoted_text": ""}
        for i in range(20):
            books[str(2000 + i)] = {
                "tweet_id": str(2000 + i),
                "text": "bitcoin halving analysis %d #crypto" % i,
                "hashtags": ["crypto"], "domains": ["coinbase.com"],
                "author_handle": "@fin%d" % (i % 4), "mentions": [],
                "has_media": False, "quoted_text": ""}
        clusters, signals = CL.build(books)
        names = {t[1] for c in clusters for t in c["tokens"]}
        self.assertTrue(names & {"rust", "crypto", "github", "coinbase"})
        self.assertEqual(signals["total"], 50)
        self.assertLess(signals["unclustered"], 50)
        self.assertTrue(clusters[0]["count"] >= clusters[-1]["count"])
        self.assertTrue(all(c["examples"] for c in clusters[:2]))

    def test_empty_and_tiny(self):
        from xbookmark import clusters as CL
        c, s = CL.build({})
        self.assertEqual(c, [])
        self.assertEqual(s["total"], 0)
        c, s = CL.build({"1": {"tweet_id": "1", "text": "one-off thing",
                               "hashtags": [], "domains": [],
                               "author_handle": "@a", "mentions": [],
                               "has_media": False, "quoted_text": ""}})
        self.assertEqual(s["total"], 1)


class DiscoverStructureTest(unittest.TestCase):
    def _kb(self, n=60):
        from xbookmark import demo
        tmp = tempfile.mkdtemp(prefix="xbo-disc-")
        kb = os.path.join(tmp, "kb")
        demo.run(kb, n=n)
        return kb

    def test_heuristic_proposal(self):
        from xbookmark import categories as CZ
        kb = self._kb()
        payload = CZ.discover_structure(kb, engine="heuristic",
                                        min_categories=8,
                                        max_categories=20)
        self.assertEqual(payload["collection_size"], 60)
        cats = payload["categories"]
        self.assertLessEqual(len(cats), 20)
        self.assertGreaterEqual(len(cats), 2)
        self.assertTrue(any(c["name"] == "Unsorted / Review"
                            for c in cats))
        core = [c for c in cats if c["name"] != "Unsorted / Review"]
        self.assertTrue(all(c["count"] > 0 for c in core))
        self.assertTrue(any(c["examples"] for c in core))
        for c in cats:
            self.assertLessEqual(c["confidence"], 1.0)
            self.assertGreaterEqual(c["confidence"], 0.0)

    def test_no_hardcoded_topic_names(self):
        from xbookmark import categories as CZ
        kb = self._kb()
        payload = CZ.discover_structure(kb, engine="heuristic",
                                        max_categories=20)
        banned = {"ai", "crypto", "coding", "startups", "design", "health"}
        names = {c["name"].lower() for c in payload["categories"]}
        self.assertFalse(names & banned)

    def test_validate_clamps_and_dedupes(self):
        from xbookmark import categories as CZ
        raw = [{"name": "T" + str(i), "description": "", "count": i + 1,
                "examples": ["e" + str(i)], "confidence": 5}
               for i in range(40)]
        raw += [{"name": "t1", "description": "", "count": 99,
                 "examples": [], "confidence": 0.9},
                {"name": "Unsorted / Review", "count": 0, "examples": [],
                 "confidence": 1.0}]
        out = CZ._validate_structure(raw, min_categories=8,
                                     max_categories=20)
        core = [c for c in out if c["name"] != "Unsorted / Review"]
        self.assertEqual(len(core), 20)   # hard cap
        lowers = [c["name"].lower() for c in core]
        self.assertEqual(len(lowers), len(set(lowers)))   # deduped
        self.assertTrue(all(c["confidence"] <= 1.0 for c in out))
        self.assertEqual(out[-1]["name"], "Unsorted / Review")

    def test_empty_kb_raises_systemexit(self):
        from xbookmark import categories as CZ
        tmp = tempfile.mkdtemp(prefix="xbo-empty-")
        kb = os.path.join(tmp, "kb")
        with self.assertRaises(SystemExit):
            CZ.discover_structure(kb, engine="heuristic")


class AcceptStructureTest(unittest.TestCase):
    def test_accept_installs_into_library(self):
        import json
        from xbookmark import demo
        from xbookmark import categories as CZ
        from xbookmark import library as LIB
        from xbookmark import store, util
        tmp = tempfile.mkdtemp(prefix="xbo-acc-")
        kb = os.path.join(tmp, "kb")
        demo.run(kb, n=40)
        payload = CZ.discover_structure(kb, engine="heuristic",
                                        max_categories=20)
        # user renames + trims the list before accepting
        cats = [c for c in payload["categories"]
                if c["name"] != "Unsorted / Review"][:3]
        cats[0] = {"name": "My Renamed Topic",
                   "description": "user-edited description"}
        res = CZ.accept_structure(kb, cats)
        self.assertTrue(res["accepted"])
        lib = LIB.Library(kb)
        names = {c["name"] for c in lib.category_list()}
        self.assertIn("My Renamed Topic", names)
        self.assertEqual(len(names), 3)
        # computed state: never stored as a category
        stored = {c["name"].lower() for c in lib.category_list()}
        self.assertNotIn("unsorted / review", stored)
        # accepted flag persisted
        with open(store.kb_path(kb, "category_structure.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertTrue(doc["accepted"])
        # legacy mirror keeps dry-run/plan-writes working
        with open(store.kb_path(kb, "categories.json"),
                  encoding="utf-8") as fh:
            ldoc = json.load(fh)
        self.assertEqual({c["name"] for c in ldoc["categories"]}, names)
        # change log entry appended
        events = list(util.iter_jsonl(store.kb_path(kb, "change_log.jsonl")))
        self.assertIn("category_structure_accepted",
                      {e["kind"] for e in events})

    def test_accept_rejects_only_unsorted(self):
        from xbookmark import categories as CZ
        tmp = tempfile.mkdtemp(prefix="xbo-acc2-")
        kb = os.path.join(tmp, "kb")
        with self.assertRaises(ValueError):
            CZ.accept_structure(kb, [{"name": "Unsorted / Review",
                                      "description": ""}])


if __name__ == "__main__":
    unittest.main()

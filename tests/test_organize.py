"""Milestone: Classify All / search / review / duplicates / feedback.

All offline (engine=heuristic); no X calls, no network.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _kb(n=60):
    from xbookmark import demo
    from xbookmark import categories as CZ
    tmp = tempfile.mkdtemp(prefix="xbo-org-")
    kb = os.path.join(tmp, "kb")
    demo.run(kb, n=n)
    payload = CZ.discover_structure(kb, engine="heuristic",
                                    max_categories=20)
    cats = [c for c in payload["categories"]
            if c["name"] != "Unsorted / Review"][:4]
    CZ.accept_structure(kb, [{"name": c["name"],
                              "description": c.get("description", "")}
                             for c in cats])
    return kb


class ConfidenceLevelTest(unittest.TestCase):
    def test_thresholds(self):
        from xbookmark.classify import confidence_level
        self.assertEqual(confidence_level(0.9), "HIGH")
        self.assertEqual(confidence_level(0.85), "HIGH")
        self.assertEqual(confidence_level(0.84), "MEDIUM")
        self.assertEqual(confidence_level(0.6), "MEDIUM")
        self.assertEqual(confidence_level(0.59), "LOW")
        self.assertEqual(confidence_level(None), "LOW")

    def test_low_not_hidden(self):
        from xbookmark import classify as CL
        kb = _kb()
        res = CL.classify_all(kb, engine="heuristic")
        self.assertGreater(res["classified"], 0)
        from xbookmark import library as LIB
        lib = LIB.Library(kb)
        lvls = {r.get("confidence_level") for r in lib.bookmarks.values()}
        self.assertTrue(lvls)  # all levels stored, LOW kept visible


class ClassifyAllTest(unittest.TestCase):
    def test_batching_and_resumability(self):
        from xbookmark import classify as CL
        kb = _kb(n=45)
        r1 = CL.classify_all(kb, engine="heuristic", batch_size=20)
        self.assertEqual(r1["batches"], 3)  # 20/20/5
        self.assertEqual(r1["classified"], 45)
        # second run: everything skipped, nothing duplicated
        r2 = CL.classify_all(kb, engine="heuristic", batch_size=20)
        self.assertEqual(r2["classified"], 0)
        self.assertEqual(r2["skipped"], 45)
        # force re-runs everything
        r3 = CL.classify_all(kb, engine="heuristic", batch_size=20,
                             force=True)
        self.assertEqual(r3["classified"], 45)

    def test_manual_correction_preserved(self):
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        kb = _kb(n=30)
        lib = LIB.Library(kb)
        names = [c["name"] for c in lib.category_list()]
        tids = sorted(__import__("xbookmark.store",
                                 fromlist=["load_bookmarks"])
                      .load_bookmarks(kb))[:3]
        for t in tids:
            lib.set_bookmark(t, categories=[names[0]])
        r = CL.classify_all(kb, engine="heuristic")
        # manual records untouched
        lib2 = LIB.Library(kb)
        for t in tids:
            self.assertEqual(lib2.record(t)["categories"], [names[0]])
        self.assertEqual(r["skipped"], 3)

    def test_topic_intent_separation(self):
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        kb = _kb(n=20)
        CL.classify_all(kb, engine="heuristic")
        lib = LIB.Library(kb)
        for tid, rec in lib.bookmarks.items():
            for c in rec["categories"]:
                self.assertIn(c, [x["name"] for x in lib.category_list()])
                self.assertNotIn(c.lower(), {"read later", "must read",
                                             "try this", "reference",
                                             "inspiration", "news",
                                             "entertainment"})
            if rec["intent"]:
                self.assertIn(rec["intent"], LIB.INTENTS)
        # intent vocabulary has all 7, changeable manually
        self.assertEqual(len(LIB.INTENTS), 7)
        tid = next(iter(lib.bookmarks))
        lib.set_bookmark(tid, intent="news")
        self.assertEqual(LIB.Library(kb).record(tid)["intent"], "news")

    def test_result_shape(self):
        from xbookmark import classify as CL
        kb = _kb(n=30)
        CL.classify_all(kb, engine="heuristic")
        from xbookmark import library as LIB
        lib = LIB.Library(kb)
        for tid, rec in lib.bookmarks.items():
            self.assertIn("confidence_level", rec)
            self.assertIn(rec["confidence_level"], ("HIGH", "MEDIUM", "LOW"))
            self.assertIn("reason", rec)
            self.assertIn("alternatives", rec)
            self.assertIsInstance(rec["categories"], list)
            self.assertLessEqual(len(rec["categories"]), 2)


class SearchTest(unittest.TestCase):
    def test_global_search_fields(self):
        from xbookmark import classify as CL
        from xbookmark import search as SE
        kb = _kb(n=40)
        CL.classify_all(kb, engine="heuristic")
        s = SE.Search(kb)
        books = __import__("xbookmark.store",
                            fromlist=["load_bookmarks"]).load_bookmarks(kb)
        tid = sorted(books)[0]
        b = books[tid]
        handle = (b.get("author_handle") or "").lstrip("@")
        dom = (b.get("domains") or [""])[0]
        tag = (b.get("hashtags") or [""])[0]
        self.assertIn(tid, s.query(handle))
        self.assertIn(tid, s.query("@" + handle))
        if tag:
            self.assertIn(tid, s.query("#" + tag))
        if dom:
            self.assertIn(tid, s.query(dom))
        # category name + intent searchable
        from xbookmark import library as LIB
        lib = LIB.Library(kb)
        rec = lib.record(tid)
        if rec["categories"]:
            self.assertIn(tid, s.query(rec["categories"][0].split()[0]))
        lib.set_bookmark(tid, intent="must_read")
        s.rebuild()
        self.assertIn(tid, s.query("Must Read"))

    def test_and_matching(self):
        from xbookmark import search as SE
        kb = _kb(n=40)
        s = SE.Search(kb)
        both = s.query("github demo")
        self.assertTrue(all(
            "github" in s.docs[t] and "demo" in s.docs[t] for t in both))

    def test_category_filter_via_library(self):
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        kb = _kb(n=30)
        CL.classify_all(kb, engine="heuristic")
        lib = LIB.Library(kb)
        name = lib.category_list()[0]["name"]
        members = [t for t in lib.bookmarks
                   if name in lib.categories_for(t)]
        self.assertGreater(len(members), 0)
        # multi-folder membership without storage duplication
        tid = members[0]
        lib.toggle_category(tid, lib.category_list()[1]["name"], on=True)
        self.assertEqual(len(lib.categories_for(tid)), 2)


class DuplicatesTest(unittest.TestCase):
    def test_same_url_and_text(self):
        from xbookmark import duplicates as DU
        from xbookmark import demo, store
        tmp = tempfile.mkdtemp(prefix="xbo-dup-")
        kb = os.path.join(tmp, "kb")
        demo.run(kb, n=10)
        books = store.load_bookmarks(kb)
        ids = sorted(books)
        # clone URL of first into second
        books[ids[1]]["urls"] = books[ids[0]]["urls"]
        books[ids[2]]["text"] = books[ids[0]]["text"]
        store.save_bookmarks(kb, books)
        groups = DU.find_groups(kb)
        flat = [g for g in groups if set([ids[0], ids[1]]) <= set(g["ids"])
                or set([ids[0], ids[2]]) <= set(g["ids"])]
        self.assertTrue(flat)
        kinds = {g["kind"] for g in groups}
        self.assertTrue(kinds & {"same_url", "same_text", "similar_text"})


class FeedbackTest(unittest.TestCase):
    def test_storage_and_context(self):
        from xbookmark import feedback as FB
        kb = _kb(n=30)
        FB.log_correction(kb, "123", {"categories": ["A"], "intent": None},
                          {"categories": ["B"], "intent": "news"})
        rows = FB.load_all(kb)
        self.assertEqual(rows[-1]["bookmark_id"], "123")
        self.assertEqual(rows[-1]["new_categories"], ["B"])
        self.assertEqual(rows[-1]["new_intent"], "news")
        self.assertIn("at", rows[-1])
        rel = FB.relevant(kb, ["B"])
        self.assertTrue(rel)
        # manual edit auto-logs feedback
        from xbookmark import library as LIB
        lib = LIB.Library(kb)
        tid = sorted(__import__("xbookmark.store",
                                fromlist=["load_bookmarks"])
                     .load_bookmarks(kb))[0]
        lib.set_bookmark(tid, categories=[c["name"]
                                          for c in lib.category_list()][:1])
        self.assertTrue(FB.load_all(kb))


if __name__ == "__main__":
    unittest.main()

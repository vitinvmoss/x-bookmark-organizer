"""Stabilization regression tests: search, delete lifecycle, review queue,
AI 502 fallback with mode reporting, HTTP disconnect-safe responses.

All offline; AI is monkeypatched to simulate HTTP 502. No X calls.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _kb(n=60):
    from xbookmark import demo
    from xbookmark import categories as CZ
    tmp = tempfile.mkdtemp(prefix="xbo-stab-")
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


class SearchRegressionTest(unittest.TestCase):
    def test_whole_library_search(self):
        from xbookmark import search as SE
        kb = _kb(n=40)
        s = SE.Search(kb)
        self.assertEqual(len(s.query("")), 40)

    def test_category_search(self):
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        from xbookmark import search as SE
        kb = _kb(n=40)
        CL.classify_all(kb, engine="heuristic")
        lib = LIB.Library(kb)
        name = lib.category_list()[0]["name"]
        members = [t for t in lib.bookmarks
                   if name in lib.categories_for(t)]
        self.assertGreater(len(members), 0)
        s = SE.Search(kb)
        hits = s.query(name.split()[0])
        self.assertTrue(set(members) & set(hits))

    def test_hashtag_username_domain(self):
        from xbookmark import search as SE
        from xbookmark import store
        kb = _kb(n=40)
        s = SE.Search(kb)
        books = store.load_bookmarks(kb)
        tid = sorted(books)[0]
        b = books[tid]
        handle = (b.get("author_handle") or "").lstrip("@")
        self.assertIn(tid, s.query(handle))
        self.assertIn(tid, s.query("@" + handle))
        self.assertIn(tid, s.query("from:" + handle))
        if b.get("hashtags"):
            tag = b["hashtags"][0]
            self.assertIn(tid, s.query("#" + tag))
            self.assertIn(tid, s.query("tag:" + tag))
        if b.get("domains"):
            dom = b["domains"][0]
            self.assertIn(tid, s.query(dom))
            self.assertIn(tid, s.query("domain:" + dom))

    def test_combined_terms_and(self):
        from xbookmark import search as SE
        kb = _kb(n=40)
        s = SE.Search(kb)
        both = s.query("github demo")
        for t in both:
            self.assertIn("github", s.docs[t])
            self.assertIn("demo", s.docs[t])

    def test_no_results(self):
        from xbookmark import search as SE
        kb = _kb(n=20)
        s = SE.Search(kb)
        self.assertEqual(s.query("zzz-no-such-term-zzz"), [])

    def test_search_with_library_filters(self):
        # search combined with category/intent/confidence filters as
        # app.py _bookmarks does: query then filter.
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        from xbookmark import search as SE
        kb = _kb(n=30)
        CL.classify_all(kb, engine="heuristic")
        lib = LIB.Library(kb)
        s = SE.Search(kb)
        ids = s.query("")
        name = lib.category_list()[0]["name"]
        cat_ids = {t for t in ids if name in lib.categories_for(t)}
        self.assertGreater(len(cat_ids), 0)
        tid = next(iter(lib.bookmarks))
        lib.set_bookmark(tid, intent="news")
        s.rebuild()
        self.assertIn(tid, s.query("news"))
        # confidence filter spot-check
        highs = [t for t, r in lib.bookmarks.items()
                 if (r.get("confidence_level") or "") == "HIGH"]
        for t in highs:
            self.assertEqual(
                (lib.record(t).get("confidence_level")), "HIGH")


class CategoryDeleteTest(unittest.TestCase):
    def test_delete_keeps_bookmarks_orphans_unsorted(self):
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        kb = _kb(n=30)
        CL.classify_all(kb, engine="heuristic")
        lib = LIB.Library(kb)
        names = [c["name"] for c in lib.category_list()]
        target = names[0]
        members = [t for t in lib.bookmarks
                   if target in lib.categories_for(t)]
        self.assertGreater(len(members), 0)
        # give one member a second category so it survives deletion
        other = names[1]
        keep = members[0]
        lib.toggle_category(keep, other, on=True)
        from xbookmark import store
        n_before = len(store.load_bookmarks(kb))
        self.assertTrue(lib.delete_category(target))
        lib2 = LIB.Library(kb)
        self.assertNotIn(target.lower(),
                         {c["name"].lower() for c in lib2.category_list()})
        # no bookmarks deleted
        self.assertEqual(len(store.load_bookmarks(kb)), n_before)
        # multi-category bookmark keeps the other category
        self.assertIn(other, lib2.categories_for(keep))
        self.assertNotIn(target, lib2.categories_for(keep))
        # bookmarks only in deleted category become Unsorted
        for t in members[1:]:
            if other not in lib.categories_for(t):
                self.assertEqual(lib2.categories_for(t), [])

    def test_delete_unknown_returns_false(self):
        from xbookmark import library as LIB
        kb = _kb(n=30)
        lib = LIB.Library(kb)
        self.assertFalse(lib.delete_category("No Such Folder"))

    def test_proposal_delete_before_accept(self):
        from xbookmark import categories as CZ
        from xbookmark import demo
        tmp = tempfile.mkdtemp(prefix="xbo-prop-")
        kb = os.path.join(tmp, "kb")
        demo.run(kb, n=30)
        payload = CZ.discover_structure(kb, engine="heuristic",
                                        max_categories=10)
        cats = [c for c in payload["categories"]
                if c["name"] != "Unsorted / Review"]
        dropped = cats[0]["name"]
        kept = [{"name": c["name"], "description": c.get("description", "")}
                for c in cats[1:3]]
        res = CZ.accept_structure(kb, kept)
        self.assertNotIn(dropped, res["names"])
        from xbookmark import library as LIB
        lib = LIB.Library(kb)
        self.assertNotIn(dropped.lower(),
                         {c["name"].lower() for c in lib.category_list()})
        # stale names do not reappear after reload
        lib3 = LIB.Library(kb)
        self.assertNotIn(dropped.lower(),
                         {c["name"].lower() for c in lib3.category_list()})


class ReviewQueueTest(unittest.TestCase):
    def test_mark_reviewed_leaves_all(self):
        from xbookmark import classify as CL
        from xbookmark import library as LIB
        from xbookmark import store
        kb = _kb(n=30)
        CL.classify_all(kb, engine="heuristic")
        lib = LIB.Library(kb)
        rids = lib.review_ids()
        self.assertGreater(len(rids), 0)
        tid = rids[0]
        why = lib.review_reason(tid)
        self.assertTrue(why)  # reason shown in UI
        lib.set_bookmark(tid, reviewed=True)
        lib2 = LIB.Library(kb)
        self.assertNotIn(tid, lib2.review_ids())
        # still visible under All (bookmarks.json untouched)
        self.assertIn(tid, store.load_bookmarks(kb))
        self.assertIn(tid, lib2.bookmarks)

    def test_never_classified_needs_review(self):
        from xbookmark import library as LIB
        kb = _kb(n=30)
        lib = LIB.Library(kb)
        # nothing classified yet: every bookmark needs review + unsorted
        self.assertEqual(len(lib.review_ids()), 30)
        self.assertEqual(lib.unsorted_count(), 30)
        rids = sorted(lib.review_ids())
        self.assertEqual(lib.review_reason(rids[0]), "not classified yet")


class AiFallbackTest(unittest.TestCase):
    def test_classify_all_502_mixed_mode(self):
        from xbookmark import classify as CL
        from xbookmark import ai as AI
        kb = _kb(n=25)
        real = AI.chat_json

        def boom(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 502, "Bad Gateway", {}, io.BytesIO(b""))
        AI.chat_json = boom
        try:
            res = CL.classify_all(kb, engine="ai", batch_size=10)
        finally:
            AI.chat_json = real
        self.assertEqual(res["classified"], 25)
        self.assertEqual(res["mode"], "heuristic")
        self.assertGreater(res["heuristic_fills"], 0)
        self.assertTrue(res["ai_errors"])
        # resumable: second run skips everything
        res2 = CL.classify_all(kb, engine="heuristic", batch_size=10)
        self.assertEqual(res2["classified"], 0)
        self.assertEqual(res2["skipped"], 25)

    def test_discover_502_heuristic_fallback(self):
        from xbookmark import categories as CZ
        from xbookmark import demo
        from xbookmark import ai as AI
        tmp = tempfile.mkdtemp(prefix="xbo-ai502-")
        kb = os.path.join(tmp, "kb")
        demo.run(kb, n=30)
        real = AI.chat_json

        def boom(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 502, "Bad Gateway", {}, io.BytesIO(b""))
        AI.chat_json = boom
        try:
            payload = CZ.discover_structure(kb, engine="ai")
        finally:
            AI.chat_json = real
        self.assertEqual(payload["engine"], "heuristic")
        self.assertTrue(payload["fallback"])
        self.assertIn("502", payload["ai_error"])
        self.assertTrue(payload["categories"])

    def test_ai_timeout_bounded(self):
        from xbookmark import ai as AI
        self.assertLessEqual(AI.TIMEOUT, 60)
        self.assertGreaterEqual(AI.TIMEOUT, 5)


class HttpRobustnessTest(unittest.TestCase):
    def test_disconnect_tuple_covers_winerror_10053(self):
        import app as APP
        for exc in (BrokenPipeError, ConnectionResetError,
                    ConnectionAbortedError):
            self.assertIn(exc, APP._CLIENT_DISCONNECTS)

    def test_json_swallow_disconnect_no_second_response(self):
        import app as APP
        h = APP.Handler.__new__(APP.Handler)
        calls = []

        class DeadSocket:
            def write(self, _b):
                raise ConnectionAbortedError(10053, "aborted")
        h.wfile = DeadSocket()
        h.send_response = lambda *a: calls.append("resp")
        h.send_header = lambda *a: calls.append("head")
        h.end_headers = lambda: calls.append("end")
        # must not raise
        h._json({"ok": True})
        self.assertIn("resp", calls)

    def test_dup_cache_avoids_recompute(self):
        import app as APP
        kb = _kb(n=15)
        APP._DUP_CACHE.update(kb=None, at=0.0, value=None)
        n1 = APP._dup_count(kb)
        at1 = APP._DUP_CACHE["at"]
        n2 = APP._dup_count(kb)
        self.assertEqual(n1, n2)
        self.assertEqual(APP._DUP_CACHE["at"], at1)


if __name__ == "__main__":
    unittest.main()

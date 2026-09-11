import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DiscoveryTest(unittest.TestCase):
    def test_ids(self):
        from xbookmark import discovery as D
        js = ('{"queryId":"AAA111","operationName":"Bookmarks"}'
              '{"operationName":"BookmarkFoldersSlice","queryId":"BBB222"}')
        ids = D.discover_ids_in_text(js)
        self.assertEqual(ids["Bookmarks"], "AAA111")
        self.assertEqual(ids["BookmarkFoldersSlice"], "BBB222")
        u = "https://x.com/i/api/graphql/CCC333/createBookmarkFolder?v=1"
        self.assertEqual(D.discover_ids_from_url_path(u),
                         {"createBookmarkFolder": "CCC333"})

    def test_no_delete(self):
        from xbookmark import requests as R
        with self.assertRaises(RuntimeError):
            R.assert_no_delete("DeleteBookmark")
        R.assert_no_delete("bookmarkTweetToFolder")


class PipelineTest(unittest.TestCase):
    def test_demo(self):
        from xbookmark import demo, categories, classify, propose, writes
        tmp = tempfile.mkdtemp(prefix="xbo-test-")
        kb = os.path.join(tmp, "kb")
        demo.run(kb, n=40)
        cats = categories.discover(kb, max_categories=10, min_size=2,
                                   engine="heuristic")
        self.assertTrue(any(c["name"] == "Unsorted / Review"
                            for c in cats["categories"]))
        self.assertLessEqual(len(cats["categories"]), 10)
        res = classify.classify(kb, engine="heuristic")
        self.assertEqual(res["classified"], 40)
        prop = propose.build(kb)
        self.assertEqual(prop["total_bookmarks"], 40)
        plan = writes.plan(kb)
        self.assertTrue(plan["summary"]["add_to_folder"] >= 40)


if __name__ == "__main__":
    unittest.main()

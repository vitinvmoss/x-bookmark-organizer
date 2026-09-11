"""Collector regression tests: assets/collector.user.js on X History.

Drives the real userscript in Node with stubbed browser globals
(fetch + XHR, tabs, location) and asserts capture behavior. No network,
no X calls, no backend changes.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "assets", "collector.user.js")
NODE = shutil.which("node")

BOOKMARKS_TABS = [{"selected": True, "label": "Bookmarks"},
                  {"selected": False, "label": "Likes"}]
LIKES_TABS = [{"selected": False, "label": "Bookmarks"},
              {"selected": True, "label": "Likes"}]

HARNESS = r"""
const fs = require('fs');
const SRC_PATH = %SRC%;
const TABS0 = %TABS%;
const PATH0 = %PATHNAME%;
const ACTIONS = %ACTIONS%;
const logs = [];
const origLog = console.log;
console.log = (...a) => { logs.push(a.map(String).join(' ')); };
global.__tabs = TABS0;
global.location = { pathname: PATH0, search: '', hash: '' };
global.history = { pushState() {}, replaceState() {} };
global.__label = null;
global.__blobText = null;
global.__box = null;
let __divCount = 0;
global.document = {
  readyState: 'complete',
  body: { appendChild(el) { global.__box = el; } },
  documentElement: {},
  _buttons: [],
  createElement(tag) {
    if (tag === 'a') return { href: '', download: '', click() {} };
    if (tag === 'div') {
      __divCount++;
      const el = { children: [], style: { display: '' }, textContent: '',
        onclick: null, appendChild(c) { this.children.push(c); } };
      if (__divCount === 1) global.__boxEl = el;
      if (__divCount === 2) global.__label = el;
      return el;
    }
    const el = { textContent: '', onclick: null,
      appendChild() {} };
    if (tag === 'button') global.document._buttons.push(el);
    return el;
  },
  querySelectorAll(sel) {
    if (sel === '[role="tab"]') {
      return global.__tabs.map((t) => ({
        getAttribute(n) {
          if (n === 'aria-selected') return t.selected ? 'true' : 'false';
          if (n === 'aria-label') return t.label;
          return null;
        },
        textContent: t.label,
      }));
    }
    return [];
  },
  addEventListener() {},
};
global.Blob = class { constructor(parts) {
  global.__blobText = (parts || []).join('');
} };
global.URL = { createObjectURL() { return 'blob:fake'; } };
global.__nextFetchBody = null;
function origFetch() {
  const body = global.__nextFetchBody;
  return Promise.resolve({ status: 200,
    clone() { return { json() {
      return body instanceof Error ? Promise.reject(body)
                                   : Promise.resolve(body);
    } }; } });
}
global.window = { fetch: origFetch, addEventListener() {} };
global.XMLHttpRequest = class {
  constructor() { this._listeners = {}; this.readyState = 0;
    this.status = 0; this.responseText = ''; }
  open() {}
  send() {}
  addEventListener(ev, fn) {
    (this._listeners[ev] = this._listeners[ev] || []).push(fn);
  }
  __fireLoad(status, text) {
    this.status = status; this.responseText = text; this.readyState = 4;
    (this._listeners.load || []).forEach((f) => f());
  }
};
const src = fs.readFileSync(SRC_PATH, 'utf8');
eval(src);
async function run() {
  for (const a of ACTIONS) {
    if (a.setTabs) global.__tabs = a.setTabs;
    if (a.setPath) global.location.pathname = a.setPath;
    if (a.transport === 'fetch') {
      global.__nextFetchBody = a.body;
      await global.window.fetch(a.url, { method: a.method || 'GET' });
    } else {
      const x = new global.XMLHttpRequest();
      x.open(a.method || 'GET', a.url);
      x.send();
      x.__fireLoad(200, JSON.stringify(a.body));
    }
    await new Promise((r) => setTimeout(r, 50));
  }
  const btn = global.document._buttons[0];
  if (btn && btn.onclick) btn.onclick();
  origLog('RESULT:' + JSON.stringify({
    label: global.__label ? global.__label.textContent : null,
    logs, blob: global.__blobText,
    boxDisplay: global.__boxEl ? global.__boxEl.style.display : null,
  }));
  process.exit(0);
}
run().catch((e) => { origLog('HARNESS-ERROR:' + e.message);
  process.exit(2); });
"""


def _timeline(ids, cursor="cursor123", root="bookmark_timeline_v2"):
    entries = [{"entryId": "tweet-%s" % i, "sortIndex": str(k)}
               for k, i in enumerate(ids)]
    entries.append({"entryId": "cursor-bottom-x",
                    "content": {"value": cursor}})
    return {"data": {root: {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": entries}]}}}}


def _run(pathname, tabs, actions):
    js = (HARNESS.replace("%SRC%", json.dumps(SRC))
          .replace("%TABS%", json.dumps(tabs))
          .replace("%PATHNAME%", json.dumps(pathname))
          .replace("%ACTIONS%", json.dumps(actions)))
    with tempfile.NamedTemporaryFile("w", suffix=".js",
                                     delete=False) as fh:
        fh.write(js)
        name = fh.name
    try:
        proc = subprocess.run([NODE, name], capture_output=True, text=True,
                              timeout=60)
    finally:
        os.unlink(name)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines()
            if ln.startswith("RESULT:")]
    assert line, proc.stdout[-2000:]
    return json.loads(line[0][len("RESULT:"):])


def _fetch(url, body, **kw):
    d = {"transport": "fetch", "url": url, "body": body}
    d.update(kw)
    return d


def _xhr(url, body, **kw):
    d = {"transport": "xhr", "url": url, "body": body}
    d.update(kw)
    return d


GQL = "https://x.com/i/api/graphql/%s/%s?v=1"


@unittest.skipIf(not NODE, "node not available")
class CollectorHistoryTest(unittest.TestCase):
    def test_history_bookmarks_fetch_non_bookmarks_op(self):
        body = _timeline(["111", "222"], root="viewer")
        res = _run("/i/history", BOOKMARKS_TABS,
                   [_fetch(GQL % ("AbC123", "HistoryTimeline"), body)])
        self.assertEqual(res["label"], "Captured 2 bookmarks")

    def test_history_bookmarks_xhr(self):
        body = _timeline(["333", "444"])
        res = _run("/i/history", BOOKMARKS_TABS,
                   [_xhr(GQL % ("XyZ999", "Bookmarks"), body)])
        self.assertEqual(res["label"], "Captured 2 bookmarks")

    def test_likes_active_no_capture(self):
        body = _timeline(["555", "666"])
        res = _run("/i/history", LIKES_TABS,
                   [_fetch(GQL % ("AbC123", "HistoryTimeline"), body)])
        self.assertEqual(res["label"], "Captured 0 bookmarks")

    def test_unrelated_graphql_no_false_positives(self):
        body = {"data": {"viewer": {"id": "1", "name": "x"}}}
        res = _run("/i/history", BOOKMARKS_TABS,
                   [_fetch(GQL % ("QwE456", "UserByScreenName"), body)])
        self.assertEqual(res["label"], "Captured 0 bookmarks")

    def test_duplicates_deduplicated(self):
        body = _timeline(["777"])
        res = _run("/i/history", BOOKMARKS_TABS,
                   [_fetch(GQL % ("A1", "HistoryTimeline"), body),
                    _fetch(GQL % ("A1", "HistoryTimeline"), body)])
        self.assertEqual(res["label"], "Captured 1 bookmarks")

    def test_legacy_bookmarks_route(self):
        body = _timeline(["888", "889", "890"])
        res = _run("/i/bookmarks", [],  # no tabs needed on legacy route
                   [_fetch(GQL % ("L1", "Bookmarks"), body)])
        self.assertEqual(res["label"], "Captured 3 bookmarks")

    def test_download_jsonl_format(self):
        body = _timeline(["901", "902"])
        res = _run("/i/history", BOOKMARKS_TABS,
                   [_fetch(GQL % ("D1", "HistoryTimeline"), body)])
        lines = [ln for ln in (res["blob"] or "").split("\n") if ln.strip()]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            obj = json.loads(ln)
            self.assertEqual(set(obj.keys()),
                             {"status_id", "entry_id", "sort_index",
                              "captured_at", "page", "cursor", "raw"})
            self.assertTrue(obj["status_id"])
            self.assertEqual(obj["entry_id"], "tweet-" + obj["status_id"])
            self.assertIn("entryId", obj["raw"])

    def test_spa_bookmarks_to_likes_stops(self):
        body = _timeline(["951"])
        body2 = _timeline(["952"])
        res = _run("/i/history", BOOKMARKS_TABS,
                   [_fetch(GQL % ("S1", "HistoryTimeline"), body),
                    {"transport": "fetch",
                     "url": GQL % ("S1", "HistoryTimeline"),
                     "body": body2, "setTabs": LIKES_TABS}])
        self.assertEqual(res["label"], "Captured 1 bookmarks")

    def test_spa_likes_to_bookmarks_resumes(self):
        body = _timeline(["961"])
        body2 = _timeline(["962"])
        res = _run("/i/history", LIKES_TABS,
                   [_fetch(GQL % ("R1", "HistoryTimeline"), body),
                    {"transport": "fetch",
                     "url": GQL % ("R1", "HistoryTimeline"),
                     "body": body2, "setTabs": BOOKMARKS_TABS}])
        self.assertEqual(res["label"], "Captured 1 bookmarks")


class CollectorStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SRC, encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_interception_covers_fetch_and_xhr(self):
        self.assertIn("window.fetch", self.src)
        self.assertIn("XMLHttpRequest", self.src)
        self.assertIn(".clone()", self.src)
        self.assertIn("responseText", self.src)

    def test_no_literal_bookmarks_op_required(self):
        self.assertNotIn("Bookmarks\\b", self.src)
        self.assertIn("GRAPHQL_RE", self.src)
        self.assertIn("opNameFromUrl", self.src)

    def test_passive_no_credentials(self):
        for token in ("auth_token", "ct0", "localStorage",
                      "sessionStorage", "document.cookie", "password"):
            self.assertNotIn(token, self.src.lower())

    def test_diagnostics_prefix_and_fields(self):
        self.assertIn("[X Bookmark Collector]", self.src)
        for field in ("transport", "status", "jsonOk", "tweetLike",
                      "fetch", "xhr"):
            self.assertIn(field, self.src)


if __name__ == "__main__":
    unittest.main()

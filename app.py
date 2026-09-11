"""Local bookmark library web app (stdlib http.server, zero dependencies).

Run:  python app.py [--kb data] [--port 8765] [--no-browser]

Read-only pipeline UI with local-only mutations of library.json:
  GET  /                      -> web/index.html (single-page UI)
  GET  /api/state             -> snapshot (counts, categories, ai status)
  GET  /api/bookmarks         -> cards (filters: q, cat, intent, review,
                                  unsorted, sort, page)
  POST /api/bookmarks/toggle  -> {id, category, on}   (library.json only)
  POST /api/bookmarks/intent  -> {id, intent}        (library.json only)
  POST /api/bookmarks/tags    -> {id, tags}          (library.json only)
  POST /api/categories        -> {name, description}         (create)
  POST /api/categories/rename -> {from, to}
  POST /api/categories/merge  -> {target, sources}
  POST /api/categories/delete -> {name}
  POST /api/reanalyze         -> {ids}   (classify_subset, local heuristic/AI)
  GET  /api/export?format=csv|json|md       (download)
  GET  /api/backup                        (zip download)

NOTE: unlike Stage-1 xb.py, this app NEVER talks to X and never mutates
anything except data/<kb>/library.json (plus optional re-analysis).
"""
from __future__ import annotations
import argparse
import json
import os
import re as _re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from xbookmark import store, util, ai
from xbookmark import library as LIB
from xbookmark import search as SEARCH
from xbookmark import exporter as EXP
from xbookmark import classify as CLS
from xbookmark import demo as DEMO
from xbookmark import normalize as NZ
from xbookmark import categories as CAT

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")

# Client-disconnect errors: the browser closed, timed out, or cancelled
# the request. These must never produce a noisy secondary traceback and
# must never trigger a second error response on a dead socket.
_CLIENT_DISCONNECTS = (BrokenPipeError, ConnectionResetError,
                       ConnectionAbortedError)

# Cache for expensive duplicate detection: /api/state is called on every
# sidebar refresh, so recomputing Jaccard pairs each time is wasteful.
_DUP_CACHE = {"kb": None, "at": 0.0, "value": None}
_DUP_TTL_S = 60.0

# Cache for the in-memory search index: rebuild only when the underlying
# files changed (bookmarks.json / library.json mtime), not on every call.
_SEARCH_CACHE = {"kb": None, "sig": None, "searcher": None}

def _dup_count(kb):
    """Cached duplicate-group count (TTL above). Never raises."""
    import time as _t
    now = _t.time()
    if (_DUP_CACHE["kb"] == kb and _DUP_CACHE["value"] is not None
            and now - _DUP_CACHE["at"] < _DUP_TTL_S):
        return _DUP_CACHE["value"]
    try:
        from xbookmark import duplicates as DUP
        n = len(DUP.find_groups(kb))
    except Exception:
        n = 0
    _DUP_CACHE.update(kb=kb, at=now, value=n)
    return n


def _search_sig(kb):
    try:
        sig = []
        for name in ("bookmarks.json", "library.json"):
            p = store.kb_path(kb, name)
            try:
                sig.append(os.path.getmtime(p))
            except OSError:
                sig.append(-1)
        return tuple(sig)
    except Exception:
        return None


SUGGESTIONS = {
    "read_later": ["watch later", "long read", "weekend"],
    "must_read": ["core", "keep forever"],
    "try_this": ["experiment", "tool to try"],
    "reference": ["docs", "cheatsheet"],
}

_x_url_re = _re.compile(r"https?://(www\.)?(twitter|x)\.com/"
                        r"(?P<user>[A-Za-z0-9_]{1,20})")


def _link_out(b):
    """True if every URL in the bookmark points at x.com/twitter.com."""
    urls = [(u.get("expanded") or u.get("short") or "")
            for u in (b.get("urls") or [])]
    urls = [u for u in urls if u]
    return bool(urls) and all(_x_url_re.match(u) for u in urls)


def _avatar_seed(handle):
    return (handle or "x").lower()[:2]


def _card(kb, lib, tid, b):
    ov = lib.record(tid)
    media = b.get("media") or []
    thumb = ""
    if media and isinstance(media[0], dict):
        thumb = media[0].get("url") or ""
    handle = (b.get("author_handle") or "unknown").lstrip("@")
    urls = [(u.get("expanded") or u.get("short") or "")
            for u in (b.get("urls") or [])]
    conf = ov.get("confidence")
    try:
        needs_review = bool(lib.review_reason(tid))
    except Exception:
        needs_review = False
        review_why = ""
    else:
        try:
            review_why = lib.review_reason(tid)
        except Exception:
            review_why = ""
    return {
        "id": tid,
        "text": b.get("text") or "",
        "author": b.get("author_name") or "",
        "handle": handle,
        "avatar": "https://unavatar.io/twitter/%s?fallback=false" % handle,
        "date": b.get("created_at") or "",
        "thumb": thumb,
        "domains": b.get("domains") or [],
        "hashtags": b.get("hashtags") or [],
        "mentions": b.get("mentions") or [],
        "links": urls,
        "link_out": _link_out(b),
        "status": b.get("status") or "available",
        "categories": ov.get("categories") or [],
        "tags": ov.get("tags") or [],
        "intent": ov.get("intent") or "",
        "confidence": conf,
        "confidence_level": ov.get("confidence_level")
        or LIB.confidence_level(conf),
        "reason": ov.get("reason") or "",
        "alternatives": ov.get("alternatives") or [],
        "reviewed": bool(ov.get("reviewed")),
        "manual": bool(ov.get("manual")),
        "needs_review": needs_review,
        "review_reason": review_why,
        "x_url": "https://x.com/%s/status/%s" % (handle, tid),
    }


def _suggestions(intent):
    return SUGGESTIONS.get(intent, [])


class Handler(BaseHTTPRequestHandler):
    server_version = "XBLocal/2.0"
    kb = "data"          # set by main()
    lib = None           # Library instance (request-time bound)
    searcher = None
    quiet = False

    def log_message(self, fmt, *args):
        if not Handler.quiet:
            super().log_message(fmt, *args)

    def _json(self, obj, code=200):
        # Safe write: if the browser already disconnected, swallow the
        # disconnect error instead of raising a noisy secondary traceback.
        # Never attempt a second error response on a dead socket.
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            body = b'{"error":"encode failed"}'
            code = 500
        try:
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECTS:
            pass

    def _send_bytes(self, data, ctype, disposition=None):
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except _CLIENT_DISCONNECTS:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _lib(self):
        if Handler.lib is None or Handler.lib.kb != Handler.kb:
            Handler.lib = LIB.Library(Handler.kb)
        return Handler.lib

    def _search(self):
        # Cached index: rebuild only when bookmarks/library changed.
        sig = _search_sig(Handler.kb)
        if (_SEARCH_CACHE["kb"] == Handler.kb
                and _SEARCH_CACHE["sig"] == sig
                and _SEARCH_CACHE["searcher"] is not None):
            return _SEARCH_CACHE["searcher"]
        s = SEARCH.Search(Handler.kb)
        _SEARCH_CACHE.update(kb=Handler.kb, sig=sig, searcher=s)
        Handler.searcher = s
        return s

    @staticmethod
    def invalidate_caches():
        _SEARCH_CACHE.update(kb=None, sig=None, searcher=None)
        Handler.searcher = None

    # ---- routing ------------------------------------------------- #
    def do_GET(self):
        p = urlparse(self.path)
        path = unquote(p.path)
        qs = parse_qs(p.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path, qs)
            if path == "/" or not os.path.splitext(path)[1]:
                return self._serve_file("index.html")
            return self._serve_file(path.lstrip("/"))
        except _CLIENT_DISCONNECTS:
            # Browser closed / timed out / cancelled: stay silent, keep
            # serving. Never try a second response on a dead socket.
            pass
        except Exception as e:
            try:
                return self._json({"error": str(e)}, 500)
            except _CLIENT_DISCONNECTS:
                pass

    def _serve_file(self, rel):
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(os.path.normpath(WEB)):
            return self._json({"error": "forbidden"}, 403)
        if not os.path.exists(full):
            return self._json({"error": "not found: %s" % rel}, 404)
        ctype = {"html": "text/html; charset=utf-8",
                 "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8",
                 "svg": "image/svg+xml", "png": "image/png",
                 "ico": "image/x-icon"}.get(
                     os.path.splitext(full)[1].lstrip("."),
                     "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        return self._send_bytes(data, ctype)

    def _api_get(self, path, qs):
        lib = self._lib()
        if path == "/api/state":
            counts = {c["name"]: 0 for c in lib.category_list()}
            for tid in lib.bookmarks:
                for c in lib.categories_for(tid):
                    if c in counts:
                        counts[c] += 1
            dup_n = _dup_count(Handler.kb)
            books = store.load_bookmarks(Handler.kb)
            return self._json({
                "kb": Handler.kb,
                "total": len(books),
                "categories": lib.category_list(),
                "counts": counts,
                "unsorted": lib.unsorted_count(),
                "review": len(lib.review_ids()),
                "confidence": lib.confidence_counts(),
                "duplicates": dup_n,
                "intents": {i: lib.intent_count(i) for i in LIB.INTENTS},
                "intent_labels": dict(LIB.INTENT_LABELS),
                "suggestions": SUGGESTIONS,
                "ai": ai.status(),
            })
        if path == "/api/discover":
            proposal, err = CAT.get_proposal(Handler.kb)
            return self._json({"ok": proposal is not None,
                               "proposal": proposal, "error": err})
        if path == "/api/bookmarks":
            return self._bookmarks(qs)
        if path == "/api/duplicates":
            from xbookmark import duplicates as DUP
            groups = DUP.find_groups(Handler.kb)
            return self._json({"total": len(groups), "groups": groups[:200]})
        if path == "/api/export":
            return self._export(qs)
        if path == "/api/backup":
            data = EXP.backup_zip(Handler.kb)
            return self._send_bytes(
                data, "application/zip",
                'attachment; filename="xbookmarks-backup.zip"')
        return self._json({"error": "unknown endpoint"}, 404)

    def _bookmarks(self, qs):
        lib = self._lib()
        books = store.load_bookmarks(Handler.kb)
        q = (qs.get("q") or [""])[0]
        cat = (qs.get("cat") or [""])[0]
        intent = (qs.get("intent") or [""])[0]
        mode = (qs.get("mode") or [""])[0]     # review | unsorted | duplicates
        author = (qs.get("author") or [""])[0].lower().lstrip("@")
        domain = (qs.get("domain") or [""])[0].lower()
        conf = (qs.get("conf") or [""])[0].upper()  # HIGH|MEDIUM|LOW
        only_unsorted = (qs.get("unsorted") or [""])[0] in ("1", "true")
        only_review = (qs.get("needs_review") or [""])[0] in ("1", "true")
        sort = (qs.get("sort") or ["date_desc"])[0]
        page = int((qs.get("page") or ["1"])[0])
        per = min(100, max(10, int((qs.get("per") or ["30"])[0])))
        ids = self._search().query(q)
        review_ids = set(lib.review_ids())

        def keep(tid):
            if tid not in books:
                return False
            cats = lib.categories_for(tid)
            ov = lib.record(tid)
            if mode == "review" and tid not in review_ids:
                return False
            if mode == "unsorted" and cats:
                return False
            if only_unsorted and cats:
                return False
            if only_review and tid not in review_ids:
                return False
            if cat:
                if cat == "Unsorted / Review":
                    if cats:
                        return False
                elif cat not in cats:
                    return False
            if intent and ov.get("intent") != intent:
                return False
            if author:
                b = books.get(tid) or {}
                hay = ((b.get("author_name") or "") + " " +
                       (b.get("author_handle") or "")).lower()
                if author not in hay:
                    return False
            if domain:
                b = books.get(tid) or {}
                doms = " ".join(b.get("domains") or []).lower()
                urls = " ".join([(u.get("expanded") or u.get("short") or "")
                                 for u in (b.get("urls") or [])]).lower()
                if domain not in doms and domain not in urls:
                    return False
            if conf in ("HIGH", "MEDIUM", "LOW"):
                lvl = ov.get("confidence_level") or LIB.confidence_level(
                    ov.get("confidence"))
                if lvl != conf:
                    return False
            return True

        ids = [t for t in ids if keep(t)]
        rev = sort.endswith("_asc")
        key = sort.replace("_asc", "").replace("_desc", "")

        def sort_key(tid):
            b = books.get(tid) or {}
            if key == "author":
                return (b.get("author_name") or "").lower()
            if key == "confidence":
                ov = lib.record(tid)
                c = ov.get("confidence")
                return c if c is not None else -1.0
            return (b.get("created_at") or "")

        ids.sort(key=sort_key, reverse=not rev)
        total = len(ids)
        start = (page - 1) * per
        cards = [_card(Handler.kb, lib, t, books[t])
                 for t in ids[start:start + per]]
        return self._json({"total": total, "page": page, "per": per,
                           "cards": cards})

    def _export(self, qs):
        fmt = (qs.get("format") or ["json"])[0]
        mime = {"csv": "text/csv; charset=utf-8",
                "json": "application/json; charset=utf-8",
                "md": "text/markdown; charset=utf-8"}.get(fmt)
        if not mime:
            return self._json({"error": "bad format"}, 400)
        if fmt == "csv":
            data = EXP.to_csv(Handler.kb).encode("utf-8")
        elif fmt == "md":
            data = EXP.to_markdown(Handler.kb).encode("utf-8")
        else:
            data = EXP.to_json(Handler.kb).encode("utf-8")
        return self._send_bytes(
            data, mime, 'attachment; filename="bookmarks.%s"' % fmt)

    # ---- POST routes --------------------------------------------- #
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
        except _CLIENT_DISCONNECTS:
            return
        except Exception:
            body = {}
        try:
            return self._api_post(path, body)
        except _CLIENT_DISCONNECTS:
            # Browser closed / timed out / cancelled after the work was
            # done (or while waiting): the file writes already happened,
            # so just stay silent and keep serving. Never send a second
            # error response on a dead socket.
            pass
        except SystemExit as e:
            try:
                return self._json({"error": str(e)}, 400)
            except _CLIENT_DISCONNECTS:
                pass
        except Exception as e:
            try:
                return self._json({"error": str(e)}, 500)
            except _CLIENT_DISCONNECTS:
                pass

    def _api_post(self, path, body):
        lib = self._lib()
        if path == "/api/bookmarks/toggle":
            rec = lib.toggle_category(str(body.get("id")),
                                      str(body.get("category") or ""),
                                      on=body.get("on"))
            Handler.invalidate_caches()
            return self._json({"ok": True, "record": rec})
        if path == "/api/bookmarks/intent":
            rec = lib.set_bookmark(str(body.get("id")),
                                   intent=body.get("intent"))
            Handler.invalidate_caches()
            return self._json({"ok": True, "record": rec})
        if path == "/api/bookmarks/tags":
            rec = lib.set_bookmark(str(body.get("id")),
                                   tags=body.get("tags") or [])
            Handler.invalidate_caches()
            return self._json({"ok": True, "record": rec})
        if path == "/api/categories":
            created = lib.add_category(str(body.get("name") or ""),
                                       str(body.get("description") or ""))
            Handler.invalidate_caches()
            return self._json({"ok": True, "created": created})
        if path == "/api/categories/rename":
            lib.rename_category(str(body.get("from") or ""),
                                str(body.get("to") or ""))
            Handler.invalidate_caches()
            return self._json({"ok": True})
        if path == "/api/categories/merge":
            lib.merge_category(str(body.get("target") or ""),
                               *[str(s) for s in
                                 (body.get("sources") or [])])
            Handler.invalidate_caches()
            return self._json({"ok": True})
        if path == "/api/categories/delete":
            ok = lib.delete_category(str(body.get("name") or ""))
            Handler.invalidate_caches()
            return self._json({"ok": True, "deleted": ok})
        if path == "/api/reanalyze":
            ids = [str(t) for t in (body.get("ids") or [])]
            engine = body.get("engine") or "heuristic"
            res = CLS.classify_subset(Handler.kb, ids, engine=engine)
            Handler.invalidate_caches()
            return self._json({"ok": True, "result": res})
        if path == "/api/classify-all":
            engine = body.get("engine") or "heuristic"
            # default to heuristic when AI is offline so one click works
            if engine == "ai" and not ai.status().get("available"):
                engine = "heuristic"
            res = CLS.classify_all(
                Handler.kb, engine=engine, model=body.get("model"),
                batch_size=int(body.get("batch_size") or CLS.BATCH),
                force=bool(body.get("force")),
                limit=body.get("limit"))
            Handler.invalidate_caches()
            return self._json({"ok": True, "result": res})
        if path == "/api/bookmarks/categories":
            rec = lib.set_bookmark(str(body.get("id")),
                                   categories=body.get("categories") or [])
            Handler.invalidate_caches()
            return self._json({"ok": True, "record": rec})
        if path == "/api/review":
            ids = [str(t) for t in (body.get("ids") or [])]
            if body.get("id"):
                ids = [str(body.get("id"))] + ids
            out = []
            for tid in ids:
                kw = {}
                if body.get("categories") is not None:
                    kw["categories"] = body.get("categories") or []
                if body.get("intent") is not None:
                    kw["intent"] = body.get("intent")
                kw["reviewed"] = True
                if body.get("done") is False:
                    kw["reviewed"] = False
                out.append(lib.set_bookmark(tid, **kw))
            Handler.invalidate_caches()
            return self._json({"ok": True, "updated": len(out)})
        if path == "/api/discover":
            # Proposal is written to category_structure.json INSIDE
            # discover_structure, before we send the response — so even if
            # the browser cancels mid-request, the proposal is preserved.
            res = CAT.discover_structure(
                Handler.kb,
                engine=body.get("engine") or "ai",
                model=body.get("model"),
                min_categories=int(body.get("min_categories") or 8),
                max_categories=int(body.get("max_categories") or 20))
            Handler.invalidate_caches()
            return self._json({"ok": True, "proposal": res})
        if path == "/api/discover/accept":
            cats = body.get("categories") or []
            res = CAT.accept_structure(Handler.kb, cats)
            Handler.invalidate_caches()
            return self._json({"ok": True, **res})
        if path == "/api/demo":
            n = int(body.get("n") or 200)
            DEMO.run(Handler.kb, n=n)
            NZ.normalize_all(Handler.kb)
            Handler.invalidate_caches()
            return self._json({"ok": True, "imported": n})
        return self._json({"error": "unknown endpoint"}, 404)


def main():
    ap = argparse.ArgumentParser(description="Local X bookmarks library")
    ap.add_argument("--kb", default="data")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    Handler.kb = args.kb
    Handler.quiet = args.quiet
    store.ensure_kb(args.kb)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % args.port
    print("x-bookmark-organizer local UI: %s  (kb=%s)" % (url, args.kb))
    print("Ctrl+C to stop. Read-only toward X; writes library.json only.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
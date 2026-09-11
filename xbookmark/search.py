"""Global local search over the whole bookmark collection.

Searches: tweet text, author name/username, URLs, domains, hashtags,
mentions, category names, intent (+labels), tags. Offline, in-memory,
fast. Multiple terms = AND. Handles @handle, #tag, domain strings and
field filters (tag:, domain:, from:).
"""
import re

from . import library as LIB
from . import store

_TOKEN = re.compile(r"[a-z0-9_]+")
_INTENT_LABELS = {k: v.lower() for k, v in LIB.INTENT_LABELS.items()}


def _norm(s):
    return (s or "").strip().lower()


class Search:
    """In-memory search index over bookmarks + library overlay."""

    def __init__(self, kb):
        self.kb = kb
        self.docs = {}     # tweet_id -> haystack string + fields
        self.meta = {}
        self.rebuild()

    def rebuild(self):
        self.docs = {}
        self.meta = {}
        books = store.load_bookmarks(self.kb)
        try:
            lib = LIB.Library(self.kb)
        except Exception:
            lib = None
        for tid, b in books.items():
            tid = str(tid)
            urls = [u.get("expanded") or u.get("short") or ""
                    for u in (b.get("urls") or [])]
            cats, tags, intent = [], [], ""
            if lib is not None:
                try:
                    ov = lib.record(tid)
                    cats = ov.get("categories") or []
                    tags = ov.get("tags") or []
                    intent = ov.get("intent") or ""
                except Exception:
                    pass
            intent_label = LIB.INTENT_LABELS.get(intent, "")
            mentions = list(b.get("mentions") or [])
            hashtags = list(b.get("hashtags") or [])
            handle = _norm(b.get("author_handle")).lstrip("@")
            hay = " ".join([
                b.get("text") or "",
                b.get("author_name") or "",
                handle, "@" + handle,
                " ".join("#" + h for h in hashtags),
                " ".join(hashtags),
                " ".join("@" + m.lstrip("@") for m in mentions),
                " ".join(mentions),
                " ".join(urls),
                " ".join(b.get("domains") or []),
                " ".join(cats),
                " ".join(tags),
                intent, intent_label,
                intent.replace("_", " "),
            ]).lower()
            self.docs[tid] = hay
            self.meta[tid] = {
                "text": b.get("text") or "",
                "author": b.get("author_name") or "",
                "handle": handle,
                "tags": [t.lower() for t in hashtags],
                "domains": [d.lower() for d in (b.get("domains") or [])],
                "urls": [u.lower() for u in urls if u],
            }

    # ---- querying ------------------------------------------------ #
    def query(self, q, ids=None, limit=5000):
        """Return matching tweet_ids, best-first (most term hits first).

        Tokens are ANDed across the full haystack. "tag:foo",
        "domain:foo.com" and "from:handle" act as extra field filters.
        Empty query returns the whole scope (up to limit).
        """
        q = (q or "").strip().lower()
        scope = list(ids) if ids is not None else list(self.docs)
        if not q:
            return scope[:limit]
        fields = []
        for m in re.finditer(r"(tag|domain|from):([a-z0-9_.\-@#]+)", q):
            fields.append((m.group(1), m.group(2).lstrip("@#")))
        q_wo_fields = re.sub(r"(tag|domain|from):([a-z0-9_.\-@#]+)", " ", q)
        tokens = [t for t in _TOKEN.findall(q_wo_fields) if t]
        # keep single-char intent initials out; require len>=2 unless the
        # raw query is very short (e.g. "AI").
        out = []
        for tid in scope:
            hay = self.docs.get(tid)
            if hay is None:
                continue
            if tokens and not all(t in hay for t in tokens):
                continue
            m = self.meta.get(tid) or {}
            ok = True
            for kind, val in fields:
                if kind == "tag" and val not in m.get("tags", []):
                    ok = False
                elif kind == "domain" and not any(
                        val in dom for dom in m.get("domains", [])):
                    ok = False
                elif kind == "from" and val.lstrip("@") != m.get("handle"):
                    ok = False
                if not ok:
                    break
            if ok:
                out.append(tid)
                if len(out) >= limit:
                    break
        return out

    def snippets(self, tid, q, width=80):
        """A short hit-context string for UI display (or '')."""
        text = (self.meta.get(tid) or {}).get("text", "")
        q = (q or "").strip().lower()
        if not text or not q:
            return text[:width]
        tokens = _TOKEN.findall(q)
        low = text.lower()
        for t in tokens:
            i = low.find(t)
            if i >= 0:
                start = max(0, i - 20)
                return (("..." if start else "") + text[start:i + width])
        return text[:width]

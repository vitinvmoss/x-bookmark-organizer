"""Cheap local analysis: entities, keywords, authors, clusters (Siftly
rawjson-extractor.ts ideas, zero AI calls). Output -> analysis.json."""
from __future__ import annotations
import collections
from . import normalize as N
from . import store, util

STOP = set(("the a an and or for with from that this have has are was were "
            "will would there their what when where which while with about "
            "into your you its his her she him they them our out not but all "
            "can just like get got more most only over such than then too very "
            "http https tco com www rt via amp").split())


def analyze(kb: str, top_n: int = 60) -> dict:
    bookmarks = store.load_bookmarks(kb)
    items = list(bookmarks.values())
    domains = collections.Counter()
    hashtags = collections.Counter()
    mentions = collections.Counter()
    authors = collections.Counter()
    words = collections.Counter()
    media_n = reply_n = quote_n = thread_n = 0
    for b in items:
        for d in b.get("domains") or []:
            domains[d] += 1
        for h in b.get("hashtags") or []:
            hashtags[h] += 1
        for m in b.get("mentions") or []:
            mentions[m] += 1
        authors[b.get("author_handle") or "unknown"] += 1
        text = "%s %s" % (b.get("text") or "", b.get("quoted_text") or "")
        for w in N.tokenize(text):
            if w not in STOP and len(w) <= 32:
                words[w] += 1
        if b.get("has_media"):
            media_n += 1
        if (b.get("text") or "").startswith("@"):
            reply_n += 1
    result = {
        "total": len(items),
        "generated_at": util.utcnow_iso(),
        "top_domains": domains.most_common(top_n),
        "top_hashtags": hashtags.most_common(top_n),
        "top_mentions": mentions.most_common(top_n),
        "top_authors": authors.most_common(top_n),
        "top_keywords": words.most_common(200),
        "stats": {"with_media": media_n, "replies": reply_n},
        "existing_folders": util.read_json(
            store.kb_path(kb, "folders.json"), []),
    }
    util.write_json(store.kb_path(kb, "analysis.json"), result)
    store.log_change(kb, {"kind": "analyze", "total": len(items)})
    return result

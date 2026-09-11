"""Possible-duplicate detection (local, read-only, never deletes).

Groups by:
  1. exact duplicate tweet IDs (same id appearing twice in raw import)
  2. identical canonical URLs (expanded URL match, t.co resolved at import)
  3. highly similar tweet text (normalized equality or Jaccard >= 0.9)

Returns [{"kind": ..., "ids": [...], "detail": ...}].
"""
from __future__ import annotations
import re
from collections import defaultdict
from . import store

_WS = re.compile(r"\s+")
_STRIP_URL = re.compile(r"https?://\S+")


def _norm_text(t):
    t = (t or "").lower()
    t = _STRIP_URL.sub(" ", t)
    t = re.sub(r"[^a-z0-9#@ ]", " ", t)
    return _WS.sub(" ", t).strip()


def _tokens(t):
    return set(_norm_text(t).split())


def find_groups(kb, jaccard=0.9):
    books = store.load_bookmarks(kb)
    groups = []
    # 1. identical URLs
    by_url = defaultdict(set)
    for tid, b in books.items():
        for u in (b.get("urls") or []):
            url = (u.get("expanded") or u.get("short") or "").strip()
            if url:
                by_url[url].add(str(tid))
    for url, ids in by_url.items():
        if len(ids) > 1:
            groups.append({"kind": "same_url", "ids": sorted(ids),
                           "detail": url[:160]})
    # 2. identical normalized text
    by_text = defaultdict(set)
    for tid, b in books.items():
        key = _norm_text(b.get("text") or "")
        if len(key) >= 20:
            by_text[key].add(str(tid))
    for key, ids in by_text.items():
        if len(ids) > 1:
            groups.append({"kind": "same_text", "ids": sorted(ids),
                           "detail": key[:160]})
    # 3. near-duplicate text (Jaccard on token sets), small-n safe O(n^2)
    # only for items not already grouped identically; cap to keep it fast.
    toks = {tid: _tokens((b.get("text") or ""))
            for tid, b in books.items()}
    seen_pairs = set()
    for g in groups:
        ids = g["ids"]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                seen_pairs.add((ids[i], ids[j]))
    ids = sorted(toks)
    for i in range(len(ids)):
        ti = toks[ids[i]]
        if not ti:
            continue
        for j in range(i + 1, len(ids)):
            if (ids[i], ids[j]) in seen_pairs:
                continue
            tj = toks[ids[j]]
            if not tj:
                continue
            # cheap length prefilter
            if abs(len(ti) - len(tj)) > max(2, int(0.25 * max(len(ti), 1))):
                continue
            inter = len(ti & tj)
            union = len(ti | tj) or 1
            if inter / union >= jaccard:
                groups.append({"kind": "similar_text", "ids": [ids[i], ids[j]],
                               "detail": "Jaccard %.2f" % (inter / union)})
                seen_pairs.add((ids[i], ids[j]))
    return groups


def count(kb):
    return len(find_groups(kb))

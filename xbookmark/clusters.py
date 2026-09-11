"""Stage 3 / Part 1 - whole-library LOCAL clustering pre-pass (no AI).

Before any AI call we group the ENTIRE local bookmark collection offline:

  * repeated keywords   (token document-frequency window filters)
  * hashtags            (entity tags)
  * domains / URLs      (host split into meaningful parts, e.g. openai)
  * mentions / authors  (context only - never category seeds; per spec no
                         categories may be based on a single person)
  * existing user tags  (surfaced to the AI as context)
  * tweet type          (post / reply / quote / media counts)
  * semantic similarity (practical stand-in: greedy Jaccard token-overlap
                         merging of keyword document sets)

Zero network, zero AI, stdlib only. Output feeds
categories.discover_structure(), which asks the configured model to name and
describe the clusters (with an offline heuristic fallback).
"""
from __future__ import annotations
import collections
from . import normalize as N
from .analyze import STOP

TLD_PARTS = {"com", "org", "net", "www", "http", "https", "html", "php",
             "htm", "asp", "aspx", "jsp"}


def _domain_parts(dom):
    for p in (dom or "").lower().split("."):
        if len(p) >= 3 and p not in TLD_PARTS:
            yield ("d", p)


def _doc_tokens(b):
    """Signal tokens for one bookmark: words, #hashtags, domain parts."""
    toks = set()
    text = "%s %s" % (b.get("text") or "", b.get("quoted_text") or "")
    for w in N.tokenize(text):
        if len(w) >= 3 and w not in STOP:
            toks.add(("w", w))
    for h in b.get("hashtags") or []:
        h = str(h or "").strip().lstrip("#").lower()
        if h:
            toks.add(("h", h))
    for d in b.get("domains") or []:
        toks.update(_domain_parts(d))
    return toks


def tweet_type(b):
    if (b.get("text") or "").startswith("@"):
        return "reply"
    if b.get("quoted_text"):
        return "quote"
    if b.get("has_media"):
        return "media"
    return "post"


def render_tokens(tokens):
    bits = []
    for kind, tok in tokens:
        if kind == "h":
            bits.append("#" + tok)
        else:
            bits.append(tok)
    return ", ".join(bits)


def _examples(by_tid, doc_ids, limit=3):
    """Prefer short informative texts, keep order stable."""
    scored = []
    for tid in doc_ids:
        text = " ".join((by_tid.get(tid, {}).get("text") or "").split())
        if text:
            scored.append((0 if 30 <= len(text) <= 180 else 1,
                           len(text), tid, text))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [t[:160] for _, _, _, t in scored[:limit]]


def build(bookmarks, min_df_abs=3, min_df_frac=0.008, max_df_frac=0.85,
          keep_top=48, max_clusters=24, merge_jaccard=0.30):
    """Cluster the whole collection offline.

    Returns (clusters, signals):
      clusters: biggest-first list of
        {"tokens": [(kind, tok)...], "count": int,
         "doc_ids": [...], "examples": [...]}
      signals:  {"total", "unclustered", "tweet_types", "distinct_authors",
                 "top_domains", "top_hashtags", "top_keywords",
                 "top_mentions", "top_authors"}
    """
    docs = []                      # (tid, token set)
    df = collections.Counter()
    domains = collections.Counter()
    hashtags = collections.Counter()
    mentions = collections.Counter()
    authors = collections.Counter()
    words = collections.Counter()
    types = collections.Counter()
    for tid, b in bookmarks.items():
        toks = _doc_tokens(b)
        docs.append((tid, toks))
        for t in toks:
            df[t] += 1
        for d in b.get("domains") or []:
            domains[d] += 1
        for h in b.get("hashtags") or []:
            hashtags[str(h or "").lower()] += 1
        for m in b.get("mentions") or []:
            mentions[str(m or "").lower()] += 1
        handle = (b.get("author_handle") or "unknown").lstrip("@").lower()
        authors[handle] += 1
        text = "%s %s" % (b.get("text") or "", b.get("quoted_text") or "")
        for w in N.tokenize(text):
            words[w] += 1
        types[tweet_type(b)] += 1

    n = len(docs)
    lo = max(1, min(min_df_abs, max(2, max(1, n) // 3)))
    hi = max(lo + 1, int(max(1, n) * max_df_frac))
    signal = [t for t, c in df.most_common(keep_top * 4)
              if lo <= c <= hi][:keep_top]

    # greedy merge: tokens whose document sets overlap become one cluster
    clusters = []
    for tok in signal:
        dset = {tid for tid, toks in docs if tok in toks}
        if not dset:
            continue
        best, best_j = None, 0.0
        for c in clusters:
            inter = len(dset & c["_docs"])
            union = len(dset | c["_docs"])
            j = inter / union if union else 0.0
            if j > best_j:
                best_j, best = j, c
        if best is not None and best_j >= merge_jaccard \
                and len(best["tokens"]) < 8:
            best["_docs"] |= dset
            best["tokens"].append(tok)
        else:
            clusters.append({"tokens": [tok], "_docs": set(dset)})

    clusters.sort(key=lambda c: len(c["_docs"]), reverse=True)
    keep = clusters[:max_clusters]

    # assign every doc to its best-fitting kept cluster (single membership
    # for count estimates; a bookmark may span several topics later at
    # classification time)
    by_tid = bookmarks
    counts = {id(c): 0 for c in keep}
    hits = {id(c): [] for c in keep}
    unclustered = 0
    for tid, toks in docs:
        best, best_ov = None, 0
        for c in keep:
            ov = len(toks & set(c["tokens"]))
            if ov > best_ov:
                best_ov, best = ov, c
        need = 2 if (best is not None and len(best["tokens"]) >= 4) else 1
        if best is None or best_ov < need:
            unclustered += 1
            continue
        counts[id(best)] += 1
        hits[id(best)].append(tid)

    out = []
    for c in keep:
        doc_ids = hits[id(c)]
        out.append({"tokens": list(c["tokens"]),
                    "count": counts[id(c)],
                    "doc_ids": doc_ids,
                    "examples": _examples(by_tid, doc_ids)})
    signals = {
        "total": n,
        "unclustered": unclustered,
        "tweet_types": dict(types),
        "distinct_authors": len(authors),
        "top_domains": domains.most_common(25),
        "top_hashtags": hashtags.most_common(25),
        "top_mentions": mentions.most_common(15),
        "top_authors": authors.most_common(10),
        "top_keywords": words.most_common(80),
    }
    return out, signals

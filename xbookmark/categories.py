"""Stage 3 / Part 1 - whole-library category discovery.

Flow: cheap LOCAL clustering (clusters.py, no AI) over the entire collection
-> the configured model names/describes the major clusters (heuristic
fallback when offline) -> user reviews/edits in the UI -> explicit
"Accept Category Structure" installs it into library.json.

No hard-coded topic names anywhere; categories always come from the user's
own data. category_structure.json = proposal; library.json = accepted truth.

No X calls, no X writes. Every change is logged to change_log.jsonl.
"""
from __future__ import annotations
import threading
import time

from . import ai as AI
from . import clusters as CL
from . import store, util

_LOCK = threading.Lock()

SYSTEM = (
    "You design a bookmark category structure for one person's saved "
    "posts. You are given clusters computed offline from their ACTUAL "
    "collection. Return ONLY JSON: "
    '{"categories": [{"name": str, "description": str, "count": int, '
    '"examples": [str], "confidence": 0.0-1.0}]}. Rules: '
    "propose 8-15 broad useful categories for the whole collection "
    "(merge small themes, never dozens of tiny folders); base them on "
    "recurring themes across the whole collection; avoid generic names "
    "like Interesting, Miscellaneous, Useful, Other, General Saves unless "
    "truly unavoidable; do "
    "not create categories that would contain only a handful of bookmarks "
    "unless they are a genuinely important theme; never base a category "
    "solely on one person or one website/domain; merge similar topics "
    "instead of splitting them; write names and descriptions in clear "
    "English; the count for each category must be a realistic estimate of "
    "how many of the person's bookmarks belong there (also usable as "
    "estimated_count); keep the example "
    "texts short and use representative bookmark IDs/URLs/texts you were "
    "shown. Use the exact counts and examples from the input "
    "clusters where they fit, and never invent bookmark texts you were "
    "not shown.")


def heuristic(analysis, max_categories, min_size):
    cats = []

    def add(name, desc, est, examples, conf):
        cats.append({"name": name, "description": desc,
                     "expected_count": est, "examples": examples,
                     "confidence": conf})

    for dom, cnt in (analysis.get("top_domains") or [])[:8]:
        if cnt >= min_size and len(cats) < max_categories - 1:
            add("Links: " + dom, "Bookmarks linking to " + dom + ".",
                cnt, ["domain:" + dom], 0.55)
    for tag, cnt in (analysis.get("top_hashtags") or [])[:6]:
        if cnt >= min_size and len(cats) < max_categories - 1:
            add("Topic: #" + tag, "Bookmarks tagged #" + tag + ".",
                cnt, ["#" + tag], 0.55)
    for kw, cnt in (analysis.get("top_keywords") or [])[:12]:
        if cnt >= max(min_size, 8) and len(cats) < max_categories - 1:
            nm = "Theme: " + kw.capitalize()
            if any(c["name"] == nm for c in cats):
                continue
            add(nm, "Bookmarks about " + kw + ".", cnt,
                ["keyword:" + kw], 0.5)
    if not cats:
        add("General Saves", "General collection.",
            analysis.get("total", 1), ["mixed"], 0.4)
    add("Unsorted / Review", "Ambiguous items.", 0,
        ["low confidence"], 1.0)
    core = [c for c in cats if c["name"] != "Unsorted / Review"]
    core = core[:max(1, max_categories - 1)]
    tail = [c for c in cats if c["name"] == "Unsorted / Review"]
    return core + tail

def finalize(raw, max_categories):
    seen = []
    for c in raw or []:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        dup = False
        for x in seen:
            if x["name"].lower() == name.lower():
                dup = True
        if dup:
            continue
        try:
            conf = float(c.get("confidence") or 0.5)
        except Exception:
            conf = 0.5
        seen.append({"name": name,
                     "description": str(c.get("description") or "")[:300],
                     "expected_count": int(c.get("expected_count") or 0),
                     "examples": [str(x)[:120] for x in
                                  (c.get("examples") or [])[:5]],
                     "confidence": max(0.0, min(1.0, conf))})
    has_unsorted = False
    for c in seen:
        if c["name"] == "Unsorted / Review":
            has_unsorted = True
    if not has_unsorted:
        seen.append({"name": "Unsorted / Review",
                     "description": "Ambiguous items.", "expected_count": 0,
                     "examples": [], "confidence": 1.0})
    core = [c for c in seen if c["name"] != "Unsorted / Review"]
    core = core[:max(1, max_categories - 1)]
    tail = [c for c in seen if c["name"] == "Unsorted / Review"]
    return core + tail
def context(analysis, max_categories, min_size):
    lines = ["Total: %d" % analysis.get("total", 0)]
    doms = ", ".join("%s(%d)" % (d, c) for d, c in
                     (analysis.get("top_domains") or [])[:30])
    lines.append("Domains: " + doms)
    tags = ", ".join("#%s(%d)" % (h, c) for h, c in
                     (analysis.get("top_hashtags") or [])[:30])
    lines.append("Hashtags: " + tags)
    kws = ", ".join("%s(%d)" % (w, c) for w, c in
                    (analysis.get("top_keywords") or [])[:80])
    lines.append("Keywords: " + kws)
    lines.append("Want max %d cats, min size %d."
                 % (max_categories, min_size))
    return "\n".join(lines)


def discover(kb, max_categories=20, min_size=5, engine="ai", model=None):
    store.ensure_kb(kb)
    analysis = util.read_json(store.kb_path(kb, "analysis.json"), None)
    if not analysis:
        from . import analyze as AZ
        analysis = AZ.analyze(kb)
    cats = None
    used = "heuristic"
    if engine == "ai":
        try:
            data = AI.chat_json(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user",
                  "content": context(analysis, max_categories, min_size)}],
                max_tokens=3000, model=model)
            raw = data.get("categories") if isinstance(data, dict) else data
            if isinstance(raw, list) and raw:
                cats = finalize(raw, max_categories)
                used = "ai"
        except Exception as e:
            print("AI failed (%s); heuristic fallback." % e)
    if cats is None:
        cats = finalize(heuristic(analysis, max_categories, min_size),
                        max_categories)
    payload = {"engine": used, "max_categories": max_categories,
               "min_size": min_size, "generated_at": util.utcnow_iso(),
               "categories": cats}
    util.write_json(store.kb_path(kb, "categories.json"), payload)
    store.log_change(kb, {"kind": "categories", "engine": used,
                          "count": len(cats)})
    return payload


# ===================================================================== #
# Stage 3 / Part 1: whole-library "Discover Categories" workflow.       #
# ===================================================================== #

DEFAULT_MIN_CATEGORIES = 8
DEFAULT_MAX_CATEGORIES = 15

GENERIC_NAMES = {"interesting", "miscellaneous", "misc", "useful",
                 "other", "others", "general", "general saves", "stuff",
                 "random", "various", "everything", "uncategorized",
                 "unsorted", "review"}

ERROR_KINDS = ("succeeded", "fallback_transient", "not_configured",
               "invalid_output", "model_unavailable",
               "insufficient_signal")

# Minimum number of strictly-valid proposals required before anything is
# rendered. Fewer than this -> an explicit error state, never bad rows.
MIN_VALID_PROPOSALS = 3


def _norm_key(name):
    return (name or "").strip().lower()


def _recount(cats, by_name):
    """Replace AI-estimated counts with cluster-observed counts when a
    category was built from a known cluster (sum of multi-merged counts
    otherwise kept as-is)."""
    for c in cats:
        est = c.get("_est")
        if est is not None:
            c["count"] = est
            c.pop("_est", None)
    return cats


def _enrich_proposal_item(c):
    """Normalize one proposal item to the required contract.

    Required: name, description, estimated_count, representative bookmark
    IDs or URLs (via examples + representative_ids), confidence.
    Compat aliases kept: count == estimated_count, examples.
    """
    name = str((c or {}).get("name") or "").strip()
    desc = str((c or {}).get("description") or "").strip()[:300]
    # count aliases
    cnt = (c or {}).get("count", None)
    if cnt is None:
        cnt = (c or {}).get("estimated_count",
              (c or {}).get("expected_count", 0))
    try:
        cnt = int(cnt or 0)
    except (TypeError, ValueError):
        cnt = 0
    cnt = max(0, cnt)
    try:
        conf = float((c or {}).get("confidence") or 0.5)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    examples = [str(x).strip()[:160] for x in
                ((c or {}).get("examples") or [])[:5]
                if str(x or "").strip()]
    rep_ids = [str(x).strip()[:120] for x in
               ((c or {}).get("representative_ids")
                or (c or {}).get("representative_bookmark_ids") or [])[:5]
               if str(x or "").strip()]
    rep_urls = [str(x).strip()[:300] for x in
                ((c or {}).get("representative_urls") or [])[:5]
                if str(x or "").strip()]
    return {"name": name, "description": desc,
            "count": cnt, "estimated_count": cnt,
            "expected_count": cnt,
            "examples": examples,
            "representative_ids": rep_ids or examples[:3],
            "representative_urls": rep_urls,
            "confidence": conf}


def _norm_name(name):
    """Normalized key for near-duplicate detection.

    Lowercases, treats '&' as 'and', drops connector words (and/the/of)
    and punctuation, and collapses whitespace — so 'AI & ML',
    'AI and ML' and 'ai-ml' all collide. Falls back to the raw lowercased
    name when normalization would erase everything (e.g. non-Latin
    scripts).
    """
    import re as _re
    s = (name or "").strip().lower().replace("&", " and ")
    toks = [w for w in _re.sub(r"[^0-9a-z]+", " ", s).split()
            if w not in ("and", "the", "of")]
    return " ".join(toks) or (name or "").strip().lower()


def _norm_text(text):
    """Normalized bookmark text for example-grounding checks."""
    return " ".join((text or "").lower().split())


def _bookmark_search_texts(books):
    """Normalized (text + quoted_text) per bookmark id, for grounding."""
    out = {}
    for tid, b in (books or {}).items():
        try:
            t = _norm_text("%s %s" % (b.get("text") or "",
                                      b.get("quoted_text") or ""))
        except Exception:
            t = ""
        if t:
            out[str(tid)] = t
    return out


def _bookmark_url_set(books):
    """Normalized set of every URL actually present in the collection."""
    out = set()
    for b in (books or {}).values():
        try:
            for u in (b.get("urls") or []):
                if not isinstance(u, dict):
                    continue
                link = ((u.get("expanded") or u.get("short")) or "").strip()
                if link:
                    out.add(link.lower())
        except Exception:
            continue
    return out


def _strict_validate(raw, books, total, max_categories):
    """Strictly validate proposals; reject invalid ones BEFORE rendering.

    Returns (valid, rejected):
      valid:    list of normalized proposal dicts (name, description,
                estimated_count/count/expected_count, examples,
                representative_ids/representative_urls, confidence).
      rejected: list of {"name": ..., "reasons": [...]} for the error
                state (names + reasons only — never bookmark contents).

    Rejection rules:
      - blank or over-long category names
      - blank descriptions
      - generic names (Other, Miscellaneous, Interesting, Random,
        Useful, ...)
      - confidence exactly 0.5 unless explicitly justified via
        confidence_justified=true or a non-empty confidence_reason
      - missing/unparseable confidence
      - proposals with no examples (no grounded example text and no
        real representative IDs/URLs)
      - representative IDs/URLs that do not belong to this collection
        (unknown IDs dropped; examples kept only when grounded in a
        real bookmark or tied to real IDs)
      - estimated count of zero (clamped into [1, collection size])
      - duplicate or near-duplicate names ("AI & ML" vs "AI and ML")

    "Unsorted / Review" is a computed bucket, not a proposal, and is
    skipped here (the caller appends it only when items genuinely went
    unclassified).
    """
    try:
        hard_max = max(1, int(max_categories or DEFAULT_MAX_CATEGORIES))
    except (TypeError, ValueError):
        hard_max = DEFAULT_MAX_CATEGORIES
    try:
        total_n = max(1, int(total or 0))
    except (TypeError, ValueError):
        total_n = 1
    by_id = books or {}
    search_texts = _bookmark_search_texts(by_id)
    url_set = _bookmark_url_set(by_id)
    valid = []
    rejected = []
    seen_keys = set()

    def _reject(name, reasons):
        rejected.append({"name": (name or "")[:60], "reasons": reasons})

    for c in (raw or []):
        if not isinstance(c, dict):
            _reject("", ["not an object"])
            continue
        name = str(c.get("name") or "").strip()
        if not name:
            _reject("", ["blank category name"])
            continue
        if len(name) > 60:
            _reject(name, ["name too long"])
            continue
        if name.lower() == "unsorted / review":
            continue
        reasons = []
        if _norm_name(name) in GENERIC_NAMES or \
                name.strip().lower() in GENERIC_NAMES:
            reasons.append("generic name")
        desc = str(c.get("description") or "").strip()
        if not desc:
            reasons.append("blank description")
        # count aliases -> int, clamped; zero is rejected
        cnt = c.get("count", None)
        if cnt is None:
            cnt = c.get("estimated_count",
                        c.get("expected_count", 0))
        try:
            cnt = int(cnt or 0)
        except (TypeError, ValueError):
            cnt = 0
        if cnt <= 0:
            reasons.append("zero estimated count")
        elif cnt > total_n:
            cnt = total_n
        # confidence: present, parseable, and never a bare 0.5
        justified = c.get("confidence_justified") is True or bool(
            str(c.get("confidence_reason") or "").strip())
        try:
            conf = float(c.get("confidence"))
        except (TypeError, ValueError):
            conf = None
        if conf is None:
            reasons.append("missing confidence")
        else:
            conf = max(0.0, min(1.0, conf))
            if abs(conf - 0.5) < 1e-9 and not justified:
                reasons.append("unjustified 50% confidence")
        # grounding: representative IDs/URLs must belong to this
        # collection; examples must be real bookmark text (or tied to
        # real IDs).
        rep_ids = []
        for x in ((c.get("representative_ids") or
                   c.get("representative_bookmark_ids")) or [])[:8]:
            sx = str(x or "").strip()
            if sx and sx in by_id:
                rep_ids.append(sx[:120])
        rep_urls = []
        for x in (c.get("representative_urls") or [])[:8]:
            sx = str(x or "").strip()
            if sx and sx.lower() in url_set:
                rep_urls.append(sx[:300])
        raw_examples = [str(x).strip()[:200] for x in
                        (c.get("examples") or [])[:8]
                        if str(x or "").strip()]
        kept_examples = []
        if rep_ids:
            # IDs are real: examples are display text for those IDs.
            kept_examples = raw_examples[:5]
        else:
            for ex in raw_examples:
                nx = _norm_text(ex)
                if nx and any(nx in t for t in search_texts.values()):
                    kept_examples.append(ex)
                    if len(kept_examples) >= 5:
                        break
        if not rep_ids and not kept_examples and not rep_urls:
            reasons.append("no grounded examples")
        # near-duplicate names
        key = _norm_name(name)
        if key in seen_keys:
            reasons.append("duplicate name")
        if reasons:
            _reject(name, reasons)
            continue
        seen_keys.add(key)
        valid.append({"name": name,
                      "description": desc[:300],
                      "count": cnt, "estimated_count": cnt,
                      "expected_count": cnt,
                      "examples": kept_examples,
                      "representative_ids": rep_ids[:5],
                      "representative_urls": rep_urls[:5],
                      "confidence": conf})
    return valid[:hard_max], rejected


def _structure_heuristic(clusters, signals, min_categories, max_categories,
                         books=None):
    """Offline naming of the clusters - no AI, deterministic.

    Targets 8-15 broad categories for large collections: tiny clusters
    below a collection-scaled floor are merged/dropped instead of becoming
    their own folders. Never invents generic Interesting/Misc buckets
    unless the collection has no stronger signal.

    Returns CORE proposals only (no Unsorted tail - the caller appends
    that bucket only when items genuinely went unclassified).

    Every item carries real cluster evidence: a non-empty data-derived
    name, a non-empty description, the observed cluster count (>0),
    real example bookmark texts, real representative bookmark IDs, and
    a varied evidence-based confidence that is never exactly 0.5.
    Clusters with no assigned bookmarks are skipped, never padded.
    """
    total = int((signals or {}).get("total", 0) or 0)
    books = books or {}
    try:
        min_c = max(1, int(min_categories or DEFAULT_MIN_CATEGORIES))
    except (TypeError, ValueError):
        min_c = DEFAULT_MIN_CATEGORIES
    try:
        max_c = max(1, int(max_categories or DEFAULT_MAX_CATEGORIES))
    except (TypeError, ValueError):
        max_c = DEFAULT_MAX_CATEGORIES
    # For real-world collections (>=50) cap broad proposals at 15 even when
    # the caller asked for more; tiny libraries keep the caller's max.
    target_max = min(max_c, 15) if total >= 50 else max_c
    target_max = max(1, target_max)
    # Collection-scaled floor: drops dozens-of-tiny-folders behaviour.
    try:
        req_min = int(min_categories or 5)
    except (TypeError, ValueError):
        req_min = 5
    floor = max(req_min if total < 50 else 5, total // 70 if total else 4, 4)
    cats = []
    used_words = set()
    used_names = set()

    def build_name(tokens):
        """Data-derived name: hashtags/domains first, then keywords.

        Never blank (returns "" when there is nothing to name, so the
        caller skips the cluster instead of emitting a fake row).
        """
        order = {"h": 0, "d": 1}
        try:
            ranked = sorted(tokens or [],
                            key=lambda t: (order.get(t[0], 2),
                                           str(t[1]).lower()))
        except Exception:
            return ""
        # First pass: skip words already used by earlier categories so
        # names stay distinguishable; second pass allows reuse rather
        # than emitting nothing.
        for allow_reuse in (False, True):
            parts, seen = [], set()
            for kind, tok in ranked:
                try:
                    kind_s, tok_s = kind, str(tok or "").strip()
                except Exception:
                    continue
                if not tok_s:
                    continue
                low = tok_s.lower()
                if low in seen:
                    continue
                if not allow_reuse and low in used_words:
                    continue
                seen.add(low)
                parts.append(("#" + tok_s) if kind_s == "h" else tok_s)
                if len(parts) >= 3:
                    break
            name = " ".join(parts).strip().title()
            if name:
                return name
        return ""

    def confidence_for(cnt):
        """Evidence-based confidence: grows with cluster share of the
        collection. Always >0.55, varied across proposals, never
        exactly 0.5."""
        share = min(1.0, cnt / max(10.0, (total or 1) / 8.0))
        return round(0.55 + 0.30 * share, 2)

    def cluster_urls(doc_ids):
        urls = []
        for tid in (doc_ids or [])[:5]:
            try:
                b = books.get(str(tid)) or {}
            except Exception:
                b = {}
            for u in (b.get("urls") or []):
                link = ((u.get("expanded") or u.get("short")) or "").strip()
                if link:
                    urls.append(link[:300])
                    break
            if len(urls) >= 3:
                break
        return urls

    ordered = sorted(clusters or [], key=lambda c: -int(c.get("count", 0) or 0))

    def consider(c):
        cnt = int(c.get("count", 0) or 0)
        doc_ids = [str(t) for t in (c.get("doc_ids") or [])[:5]]
        if cnt <= 0 or not doc_ids:
            return
        name = build_name(c.get("tokens") or [])
        if not name:
            return
        key = _norm_name(name)
        if key in used_names:
            # disambiguate with the cluster's lead token
            lead = ""
            try:
                lead = str((c.get("tokens") or [[None, ""]])[0][1])
            except Exception:
                lead = ""
            name = ("%s (%s)" % (name, lead)).strip()[:60]
            key = _norm_name(name)
            if key in used_names:
                return
        if name.strip().lower() in GENERIC_NAMES or \
                key in GENERIC_NAMES:
            return
        examples = [str(x)[:200] for x in (c.get("examples") or [])[:5]
                    if str(x or "").strip()]
        if not examples:
            # fall back to real bookmark texts for these IDs
            for tid in doc_ids:
                try:
                    t = " ".join(
                        str((books.get(tid) or {}).get("text") or "")
                        .split())[:200]
                except Exception:
                    t = ""
                if t:
                    examples.append(t)
                if len(examples) >= 3:
                    break
        if not examples:
            return
        topic = CL.render_tokens(c.get("tokens") or [])
        desc = ("Bookmarks about %s (%d of %d bookmarks). "
                "Example: %s" % (topic, cnt, total, examples[0][:140]))
        used_names.add(key)
        for w in key.split(" "):
            if w:
                used_words.add(w)
        cats.append({"name": name, "description": desc[:300],
                     "count": cnt, "estimated_count": cnt,
                     "expected_count": cnt,
                     "examples": examples,
                     "representative_ids": doc_ids,
                     "representative_urls": cluster_urls(doc_ids),
                     "confidence": confidence_for(cnt)})

    # Pass 1: broad clusters at/above the floor.
    for c in ordered:
        if len(cats) >= target_max:
            break
        if int(c.get("count", 0) or 0) < floor:
            continue
        consider(c)
    # Pass 2: fill up to min_c with the next-biggest real clusters
    # (still theme-based, never generic padding) so small libraries
    # aren't empty.
    if len(cats) < min(min_c, target_max):
        for c in ordered:
            if len(cats) >= min(min_c, target_max):
                break
            if any(str(t) in (x.get("representative_ids") or [])
                   for x in cats for t in (c.get("doc_ids") or [])[:1]):
                continue
            consider(c)
    cats.sort(key=lambda c: -c["count"])
    return cats[:target_max]


def _structure_context(clusters, signals, user_tags, min_categories,
                       max_categories):
    def fmt_pairs(pairs, limit, fmt):
        return ", ".join(fmt(x, c) for x, c in pairs[:limit])

    lines = ["Total bookmarks: %d" % signals.get("total", 0)]
    lines.append("Distinct authors: %d" % signals.get("distinct_authors", 0))
    tt = signals.get("tweet_types") or {}
    lines.append("Tweet types: " + ", ".join(
        "%s=%d" % (k, v) for k, v in sorted(tt.items())))
    lines.append("Clusters (computed offline, biggest first; tokens are "
                 "keywords unless marked #tag or d:domain-part):")
    for c in clusters:
        toks = " ".join(("#" + t) if k == "h" else
                        ("d:" + t if k == "d" else t)
                        for k, t in c["tokens"])
        lines.append("  - %s | bookmarks in cluster: %d | example: %s"
                     % (toks, c["count"],
                        (c["examples"][0][:110] if c["examples"]
                         else "(none)")))
    if user_tags:
        lines.append("User's own tags seen in the collection: " +
                     fmt_pairs(user_tags, 25, lambda t, c: "#%s(%d)" % (t, c)))
    lines.append("Design %d-%d categories. Return ONLY the JSON object "
                 "described in the system message." %
                 (min_categories, max_categories))
    return "\n".join(lines)


def _validate_structure(raw, min_categories, max_categories):
    """Dedupe/clamp the AI's proposal into 8-15 broad categories.

    Hard cap (max) always applies; the min is a soft floor - tiny real
    libraries must not be padded with meaningless padding buckets.
    Generic Interesting/Misc/Useful/Other names are dropped unless they
    are the only signal. Every item is normalized to the required
    contract (name, description, estimated_count, representative
    IDs/URLs, confidence).
    """
    seen = []
    for c in (raw or []):
        name = str((c or {}).get("name") or "").strip()
        if not name or len(name) > 60:
            continue
        if name.lower() == "unsorted / review":
            continue
        item = _enrich_proposal_item(c)
        if not item["name"]:
            continue
        if any(x["name"].lower() == item["name"].lower() for x in seen):
            continue
        seen.append(item)
    # Drop generic buckets when stronger themes exist.
    strong = [c for c in seen if c["name"].strip().lower()
              not in GENERIC_NAMES]
    if strong:
        seen = strong
    seen.sort(key=lambda x: -x["count"])
    try:
        hard_max = max(1, int(max_categories or DEFAULT_MAX_CATEGORIES))
    except (TypeError, ValueError):
        hard_max = DEFAULT_MAX_CATEGORIES
    if len(seen) > hard_max:
        seen = seen[:hard_max]
    # Broad-quality guard: for non-tiny collections prefer items with
    # real estimated counts over empty placeholders.
    if len(seen) > 6:
        with_counts = [c for c in seen if c["count"] > 0]
        if with_counts:
            seen = with_counts[:hard_max] or seen[:1]
    tail = {"name": "Unsorted / Review",
            "description": "Ambiguous items the classifier is unsure "
                           "about.",
            "count": 0, "estimated_count": 0, "expected_count": 0,
            "examples": [], "representative_ids": [],
            "representative_urls": [], "confidence": 1.0}
    return seen + [tail]


# ===================================================================== #
# Two-stage hosted discovery (Gemini default).
#
# The full collection is NEVER sent as one oversized prompt. Each
# bookmark is reduced to one compact line (ID | author | URL |
# truncated text) and only a bounded sample of those lines is sent:
#
#   Stage A: compact evidence + offline cluster stats -> the model
#            identifies recurring themes/topics (small JSON output).
#   Stage B: the Stage-A themes + compact evidence for the cited
#            bookmarks -> the model proposes 8-15 broad local folders,
#            each with name, description, estimated_count,
#            representative IDs/URLs, examples, and confidence.
#
# Every proposal is then strictly validated (see _strict_validate):
# IDs/URLs/examples must belong to THIS collection.
# ===================================================================== #

STAGE_A_SYSTEM = (
    "You analyze one person's saved-post collection. You are shown "
    "offline keyword clusters plus compact bookmark lines "
    "(ID | @author | url | truncated text). Identify the recurring "
    "themes/topics across the WHOLE collection. Return ONLY JSON: "
    '{"themes": [{"theme": str (short topic label), '
    '"description": str (one sentence), '
    '"evidence_ids": [str] (real bookmark IDs shown to you, 2-8 per '
    "theme), "
    '"approx_count": int (rough share of the collection)]}. Rules: '
    "list 8-15 themes; base them on recurring topics, never on one "
    "person or one website; use ONLY bookmark IDs you were actually "
    "shown; never invent IDs, URLs, or bookmark texts.")

STAGE_B_SYSTEM = (
    "You design a bookmark category structure (LOCAL folders, not "
    "website folders) for one person's saved posts, from the themes "
    "identified in stage A plus the cited evidence bookmarks. Return "
    "ONLY JSON: "
    '{"categories": [{"name": str (meaningful folder name, <=60 '
    "chars), "
    '"description": str (one clear sentence, non-empty), '
    '"estimated_count": int (>0, realistic share of the collection), '
    '"representative_ids": [str] (real bookmark IDs shown to you), '
    '"representative_urls": [str] (real URLs shown to you, may be []), '
    '"examples": [str] (short real bookmark texts shown to you), '
    '"confidence": 0.0-1.0 (varied per category; NEVER use exactly '
    '0.5 unless you also set "confidence_reason" explaining why)]}. '
    "Rules: propose 8-15 broad useful categories (merge small themes, "
    "never dozens of tiny folders); avoid generic names like "
    "Interesting, Miscellaneous, Useful, Other, Random, General Saves; "
    "never base a category solely on one person or one website/domain; "
    "merge similar topics instead of splitting them; every category "
    "needs at least one real representative ID or real example text "
    "you were shown; never invent bookmark IDs, URLs, or texts.")

_COMPACT_TEXT_CHARS = 120
_STAGE_A_MAX_LINES = 240
_STAGE_A_PER_CLUSTER = 10
_STAGE_B_MAX_LINES = 200


def _compact_line(tid, book):
    """One compact line per bookmark: ID | author | URL | truncated text.

    Full bookmark contents are never emitted — text is truncated to
    _COMPACT_TEXT_CHARS.
    """
    import re as _re
    tid_s = str(tid or "").strip()
    b = book or {}
    author = str(b.get("author_handle") or b.get("author_name") or
                 "unknown").strip().lstrip("@") or "unknown"
    url = ""
    try:
        for u in (b.get("urls") or []):
            if not isinstance(u, dict):
                continue
            link = ((u.get("expanded") or u.get("short")) or "").strip()
            if link:
                url = link
                break
    except Exception:
        url = ""
    if not url:
        digits = _re.sub(r"\D", "", tid_s)
        url = "https://x.com/i/status/%s" % (digits or "0")
    text = " ".join(str(b.get("text") or "").split())
    if len(text) > _COMPACT_TEXT_CHARS:
        text = text[:_COMPACT_TEXT_CHARS].rstrip() + "…"
    return "%s | @%s | %s | %s" % (tid_s, author[:30], url[:200], text)


def _compact_lines(books, ids, max_lines):
    lines = []
    for tid in (ids or [])[:max_lines]:
        if str(tid) in (books or {}):
            lines.append(_compact_line(tid, books[str(tid)]))
    return lines


def _stage_a_context(clusters, signals, books, user_tags, min_categories,
                     max_categories):
    total = int((signals or {}).get("total", 0) or 0)
    lines = ["Total bookmarks: %d (offline pass left %d unclustered)."
             % (total, int((signals or {}).get("unclustered", 0) or 0))]
    lines.append("Offline keyword clusters, biggest first "
                 "(tokens: #tag, d:domain-part, or keyword):")
    sampled = []
    for c in (clusters or [])[:24]:
        toks = " ".join(("#" + t) if k == "h" else
                        ("d:" + t if k == "d" else t)
                        for k, t in (c.get("tokens") or []))
        ids = [str(t) for t in (c.get("doc_ids") or [])
               [:_STAGE_A_PER_CLUSTER]]
        sampled.extend([i for i in ids if i not in sampled])
        lines.append("  - %s | bookmarks in cluster: %d | ids: %s"
                     % (toks, int(c.get("count", 0) or 0),
                        ",".join(ids) or "(none)"))
    if user_tags:
        lines.append("User's own tags seen in the collection: " +
                     ", ".join("#%s(%d)" % (t, n)
                               for t, n in user_tags[:25]))
    lines.append("Sample bookmarks (ID | @author | url | text, truncated):")
    for ln in _compact_lines(books, sampled, _STAGE_A_MAX_LINES):
        lines.append("  " + ln)
    lines.append("Identify %d-%d recurring themes across the whole "
                 "collection. Return ONLY the stage-A JSON object."
                 % (min_categories, max_categories))
    return "\n".join(lines)


def _clean_themes(raw, books):
    """Validate Stage-A output. Keeps themes with real evidence IDs;
    returns [] when nothing usable came back."""
    out = []
    for t in (raw or []):
        if not isinstance(t, dict):
            continue
        name = str(t.get("theme") or t.get("name") or "").strip()
        if not name:
            continue
        desc = str(t.get("description") or "").strip()[:300]
        ev = []
        for x in ((t.get("evidence_ids") or
                   t.get("representative_ids")) or [])[:20]:
            sx = str(x or "").strip()
            if sx and sx in (books or {}) and sx not in ev:
                ev.append(sx)
        try:
            approx = max(0, int(t.get("approx_count") or 0))
        except (TypeError, ValueError):
            approx = 0
        out.append({"theme": name[:80], "description": desc,
                    "evidence_ids": ev, "approx_count": approx})
    if not out or not any(t["evidence_ids"] for t in out):
        return []
    return out


def _stage_b_context(themes, books, signals, min_categories,
                     max_categories):
    total = int((signals or {}).get("total", 0) or 0)
    lines = ["Collection size: %d bookmarks." % total]
    lines.append("Recurring themes identified in stage A:")
    evidence = []
    for t in themes:
        lines.append("- %s: %s (rough share: %d bookmarks; evidence "
                     "IDs: %s)" % (t["theme"], t["description"] or "(no "
                                   "description)",
                                   t["approx_count"],
                                   ",".join(t["evidence_ids"])))
        evidence.extend([i for i in t["evidence_ids"] if i not in evidence])
    lines.append("Evidence bookmarks (ID | @author | url | text, "
                 "truncated) — use ONLY these IDs/URLs/texts:")
    for ln in _compact_lines(books, evidence, _STAGE_B_MAX_LINES):
        lines.append("  " + ln)
    lines.append("Propose %d-%d broad LOCAL folder categories from these "
                 "themes. Return ONLY the stage-B JSON object."
                 % (min_categories, max_categories))
    return "\n".join(lines)


def _provider_generate_json(provider, messages, model=None):
    """Ask the llm provider layer for structured JSON.

    ``provider`` is a provider adapter (preferred) or a provider name;
    categories.py never knows how a provider performs its HTTP call.
    """
    gen = getattr(provider, "generate_structured_json", None)
    if callable(gen):
        return gen(messages, model=model)
    from . import llm as _LLM
    return _LLM.chat_json(provider, messages, model=model)


def _run_two_stage(provider, model, clusters, signals, books, user_tags,
                   min_categories, max_categories):
    """Run Stage A (themes) then Stage B (proposals). Returns
    (raw_proposals, stage_info). Raises ValueError with an 'invalid
    structured output' message when either stage is unusable, or
    propagates the provider exception (after its one controlled retry
    inside llm.py) so the caller reports a provider-error state."""
    a_context = _stage_a_context(clusters, signals, books, user_tags,
                                 min_categories, max_categories)
    a_data = _provider_generate_json(
        provider,
        [{"role": "system", "content": STAGE_A_SYSTEM},
         {"role": "user", "content": a_context}],
        model=model)
    a_raw = a_data.get("themes") if isinstance(a_data, dict) else a_data
    themes = _clean_themes(a_raw, books)
    if not themes:
        raise ValueError("provider returned invalid structured output "
                         "(stage A: no usable themes with real evidence)")
    b_context = _stage_b_context(themes, books, signals, min_categories,
                                 max_categories)
    b_data = _provider_generate_json(
        provider,
        [{"role": "system", "content": STAGE_B_SYSTEM},
         {"role": "user", "content": b_context}],
        model=model)
    raw = b_data.get("categories") if isinstance(b_data, dict) else b_data
    if not isinstance(raw, list) or not raw:
        raise ValueError("provider returned invalid structured output "
                         "(stage B: empty categories)")
    return raw, {"stage_a_themes": len(themes)}


def _classify_discovery_error(engine, exc, model_name=""):
    """Map a discovery failure to (ai_error, error_kind, diagnostics).

    error_kind is one of: not_configured, model_unavailable,
    invalid_output, fallback_transient, insufficient_signal. Safe: no
    prompts, contents, headers, cookies, or secrets — only
    provider/model/status/text<=500 plus retry count.
    """
    from . import llm as _LLM
    prov = (engine or "gemini").lower()
    if prov not in ("gemini", "groq", "openrouter", "cerebras",
                    "heuristic", "ai", "auto"):
        prov = "gemini"
    label = _LLM.PROVIDER_LABELS.get(prov, "AI")
    key_env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
               "openrouter": "OPENROUTER_API_KEY",
               "cerebras": "CEREBRAS_API_KEY"}.get(prov, "an API key")
    retries = int(getattr(exc, "_llm_retries", 0) or 0)
    # Retry counts recorded inside llm._gemini_call live in LAST_DIAGNOSTICS
    try:
        if not retries and isinstance(_LLM.LAST_DIAGNOSTICS, dict):
            retries = int(_LLM.LAST_DIAGNOSTICS.get("retries", 0) or 0)
    except Exception:
        pass
    msg = str(exc or "")
    if "not configured" in msg.lower() or "api key not configured" in msg.lower():
        diag = {"provider": prov, "model": model_name or "",
                "http_status": None, "error": _LLM._redact_secrets(msg)[:500],
                "retries": retries}
        return ("%s not configured; configure %s to use cloud AI"
                % (prov, key_env), "not_configured", diag)
    try:
        if _LLM.is_model_unavailable(exc):
            diag = _LLM.describe_error(prov, model_name, exc, retries)
            return (_LLM._safe_error(exc, prov), "model_unavailable", diag)
    except Exception:
        pass
    if "malformed json" in msg.lower() or "invalid structured" in msg.lower() \
            or "empty ai response" in msg.lower() \
            or "empty candidates" in msg.lower():
        try:
            diag = _LLM.describe_error(prov, model_name, exc, retries)
        except Exception:
            diag = {"provider": prov, "model": model_name or "",
                    "http_status": None,
                    "error": "provider returned invalid structured output",
                    "retries": retries}
        safe = msg if ("malformed" in msg.lower()
                       or "strictly valid" in msg.lower()) else \
            "provider returned invalid structured output"
        return ("%s returned invalid structured output (%s)"
                % (label, safe[:300]), "invalid_output", diag)
    try:
        safe = _LLM._safe_error(exc, prov)
    except Exception:
        safe = "%s" % str(exc)[:200]
    try:
        diag = _LLM.describe_error(prov, model_name, exc, retries)
    except Exception:
        diag = {"provider": prov, "model": model_name or "",
                "http_status": getattr(exc, "code", None),
                "error": safe[:500], "retries": retries}
    return (safe, "fallback_transient", diag)


def _attempt_record(provider, model, diag, kind, success, started):
    """Safe per-attempt diagnostics (never prompts/contents/headers/keys).

    Retains only the contract fields the UI/diagnostics need:
    provider, model, http_status, error_kind, error_message_truncated,
    retry_count, duration_ms, success.
    """
    diag = diag or {}
    return {
        "provider": str(provider or ""),
        "model": str(model or ""),
        "http_status": diag.get("http_status"),
        "error_kind": str(kind or ""),
        "error_message_truncated": str(diag.get("error") or "")[:500],
        "retry_count": int(diag.get("retries") or 0),
        "duration_ms": int(max(0.0, time.time() - started) * 1000),
        "success": bool(success),
    }


def _aggregate_chain_failure(attempts, chain, last_error, last_kind, last_diag):
    """Compose the explicit provider-error state for a failed chain.

    Returns (ai_error, error_kind, diagnostics). ``diagnostics`` keeps a
    legacy {provider,model,http_status,error,retries} shape pointing at
    the first failed provider so existing consumers keep working.
    """
    from . import llm as _LLM
    lines = []
    for a in attempts or []:
        label = _LLM.PROVIDER_LABELS.get(a.get("provider"),
                                         a.get("provider") or "AI")
        status = a.get("http_status")
        kind = a.get("error_kind") or ""
        if a.get("success"):
            desc = "success"
        elif status:
            desc = "HTTP %s" % status
        elif kind == "invalid_output":
            desc = "invalid structured output"
        elif kind == "model_unavailable":
            desc = "model unavailable"
        elif kind == "not_configured":
            desc = "not configured"
        elif a.get("error_message_truncated"):
            desc = "error"
        else:
            desc = "failed"
        lines.append("%s - %s" % (label, desc))
    if not attempts:
        ai_error = ("no AI provider is configured; set an API key "
                    "(GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY) "
                    "or choose the offline heuristic explicitly")
        return (ai_error, "not_configured",
                {"provider": "none", "model": "", "http_status": None,
                 "error": ai_error[:500], "retries": 0})
    attempted = ", ".join(lines)
    ai_error = ("AI discovery failed after trying: %s. No AI proposals "
                "were generated." % attempted)
    detail = str(last_error or "").strip()
    if detail:
        ai_error += " Last error: %s" % detail[:300]
    # error_kind: prefer the most actionable terminal category.
    kinds = [a.get("error_kind") for a in attempts if a.get("error_kind")]
    if kinds and all(k == "not_configured" for k in kinds):
        error_kind = "not_configured"
    elif "invalid_output" in kinds and last_kind == "invalid_output":
        error_kind = "invalid_output"
    elif last_kind:
        error_kind = last_kind
    elif "invalid_output" in kinds:
        error_kind = "invalid_output"
    else:
        error_kind = "fallback_transient"
    base = dict(last_diag or {})
    first = attempts[0]
    diag = {"provider": first.get("provider"),
            "model": first.get("model") or "",
            "http_status": first.get("http_status"),
            "error": (first.get("error_message_truncated")
                      or last_error or "")[:500],
            "retries": first.get("retry_count", 0)}
    if not diag["error"] and base:
        diag["error"] = str(base.get("error") or "")[:500]
    return (ai_error, error_kind, diag)


def discover_structure(kb, engine="ai", model=None, min_categories=8,
                       max_categories=15):
    """Whole-library discovery: LOCAL clustering pre-pass, then the
    requested engine proposes folders.

    Hosted engines (gemini/groq/openrouter/cerebras) run a TWO-STAGE process:
    Stage A identifies recurring themes from compact per-bookmark
    evidence (ID/author/URL/truncated text — never full contents in one
    oversized prompt); Stage B turns those themes into 8-15 folder
    proposals. engine="ai" keeps the legacy single-call bridge.
    engine="heuristic" runs the offline generator explicitly.

    Discovery NEVER classifies or permanently assigns bookmarks: it
    only proposes folders for user approval. A successful run
    overwrites (never duplicates) the previous proposal file;
    library.json is untouched until Accept.

    Failure semantics (never fake output):
      - hosted failure (503/429/5xx/timeout/...) or invalid AI output
        or fewer than MIN_VALID_PROPOSALS strictly-valid proposals ->
        an explicit provider-error payload with categories == [].
        NO heuristic categories are generated or displayed by default;
        the user may explicitly request them via engine="heuristic"
        (server: POST /api/discover/heuristic) or retry the hosted
        engine (server: POST /api/discover/retry) from the stored
        collection without re-upload.
      - a failed run never clobbers a previously stored good proposal.
    """
    with _LOCK:
        store.ensure_kb(kb)
        books = store.load_bookmarks(kb)
        if not books:
            raise SystemExit(
                "no bookmarks in %s - run 'demo' or 'import' first" % kb)
        try:
            min_categories = max(1, min(int(min_categories or 8), 30))
        except (TypeError, ValueError):
            min_categories = 8
        try:
            max_categories = max(1, min(int(max_categories or 15), 30))
        except (TypeError, ValueError):
            max_categories = 15
        clusters, signals = CL.build(books, max_clusters=max(24, max_categories + 4))
        if not clusters:
            raise SystemExit("no usable signal in the collection")
        user_tags = []
        lib = util.read_json(store.kb_path(kb, "library.json"), None)
        if lib:
            for rec in (lib.get("bookmarks") or {}).values():
                for t in (rec.get("tags") or []):
                    user_tags.append((str(t), 1))
        merged = {}
        for t, c in user_tags:
            merged[t.lower()] = merged.get(t.lower(), 0) + c
        user_tags = sorted(merged.items(), key=lambda x: -x[1])
        total = int(signals.get("total", 0) or len(books))
        unsorted = int(signals.get("unclustered", 0) or 0)
        cats = None
        used = "heuristic"
        ai_error = ""
        error_kind = "fallback_transient"
        diagnostics = {"provider": engine, "model": model or "",
                       "http_status": None, "error": "", "retries": 0}
        status_label = "heuristic"
        rejected = []
        stage_info = {}
        attempts = []
        fallback_chain = []
        successful_provider = ""
        hosted = {"ai", "gemini", "groq", "openrouter", "cerebras", "auto"}

        def _success_diag(prov, mdl):
            try:
                from . import llm as _LLMD
                _r = int(_LLMD.LAST_DIAGNOSTICS.get("retries", 0) or 0)
            except Exception:
                _r = 0
            return {"provider": prov, "model": mdl, "http_status": 200,
                    "error": "", "retries": _r}

        def _fail(exc, prov, mdl):
            try:
                from . import llm as _LLM2b
                return _classify_discovery_error(prov, exc, mdl)
            except Exception:
                return ("%s" % str(exc)[:200], "fallback_transient",
                        {"provider": prov, "model": mdl,
                         "http_status": getattr(exc, "code", None),
                         "error": ("%s" % str(exc)[:500]), "retries": 0})

        def _check_valid(valid, rej, where):
            if len(valid) < MIN_VALID_PROPOSALS:
                why = "; ".join(
                    "%s (%s)" % (r["name"] or "(blank)",
                                 ", ".join(r["reasons"][:3]))
                    for r in rej[:6]) or "no proposals returned"
                raise ValueError(
                    "provider returned invalid structured output "
                    "(%s: only %d of %d strictly valid: %s)"
                    % (where, len(valid),
                       len(valid) + len(rej), why[:300]))

        if engine in hosted:
            from . import llm as _LLM0
            if engine == "ai":
                # legacy path (OpenCode/Ollama bridge) — kept for
                # backwards compatibility with existing flows/tests.
                fallback_chain = ["ai"]
                try:
                    data = AI.chat_json(
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _structure_context(
                             clusters, signals, user_tags, min_categories,
                             max_categories)}],
                        max_tokens=3000, model=model)
                    _prov = "ai"
                    _model = model or ""
                    raw = data.get("categories") \
                        if isinstance(data, dict) else data
                    if not isinstance(raw, list) or not raw:
                        raise ValueError(
                            "provider returned invalid structured output "
                            "(empty categories)")
                    valid, rejected = _strict_validate(
                        raw, books, total, max_categories)
                    _check_valid(valid, rejected, "validation")
                    cats = valid
                    used = "ai"
                    error_kind = "succeeded"
                    ai_error = ""
                    diagnostics = {"provider": "ai", "model": _model,
                                   "http_status": None, "error": "",
                                   "retries": 0}
                    status_label = "ai"
                    successful_provider = "ai"
                    attempts.append(_attempt_record(
                        "ai", _model, diagnostics, "succeeded", True,
                        time.time()))
                except Exception as e:
                    _m = model or ""
                    ai_error, error_kind, diagnostics = _fail(e, "ai", _m)
                    attempts.append(_attempt_record(
                        "ai", _m, diagnostics, error_kind, False, time.time()))
                    print("AI discovery failed "
                          "(provider=ai model=%s status=%s retries=%s: %s); "
                          "no proposals generated."
                          % (diagnostics.get("model"),
                             diagnostics.get("http_status"),
                             diagnostics.get("retries"),
                             (ai_error or "")[:200]))
                    cats = None
            else:
                # Explicit provider (gemini/groq/openrouter/cerebras) -> exactly
                # that provider. auto -> configured providers in
                # LLM_FALLBACK_PROVIDERS order, then heuristic only when
                # LLM_ALLOW_HEURISTIC_FALLBACK is enabled.
                chain = _LLM0.resolve_chain(engine, model=model)
                fallback_chain = [p.name for p in chain]
                last_error = ""
                last_kind = "fallback_transient"
                last_diag = None
                if not chain:
                    ai_error = ("no AI provider is configured for auto "
                                "mode; set an API key or choose the offline "
                                "heuristic explicitly")
                    error_kind = "not_configured"
                    diagnostics = {"provider": "none", "model": "",
                                   "http_status": None,
                                   "error": ai_error[:500], "retries": 0}
                    cats = None
                for adapter in chain:
                    prov = adapter.name
                    if prov == "heuristic":
                        mdl = ""
                    elif engine in _LLM0.HOSTED_PROVIDERS and model:
                        mdl = model
                    else:
                        mdl = adapter.model
                    started = time.time()
                    try:
                        if prov == "heuristic":
                            raw = _structure_heuristic(
                                clusters, signals, min_categories,
                                max_categories, books)
                            this_stage = {}
                        else:
                            raw, this_stage = _run_two_stage(
                                adapter, mdl, clusters, signals, books,
                                user_tags, min_categories, max_categories)
                        valid, rejected = _strict_validate(
                            raw, books, total, max_categories)
                        _check_valid(valid, rejected, "validation")
                    except Exception as e:
                        ai_err, kind, diag = _classify_discovery_error(
                            prov, e, mdl)
                        attempts.append(_attempt_record(
                            prov, mdl, diag, kind, False, started))
                        last_error, last_kind, last_diag = ai_err, kind, diag
                        continue
                    # Success: this provider produced valid structured
                    # output. Report exactly which one succeeded.
                    cats = valid
                    used = prov
                    error_kind = "succeeded"
                    ai_error = ""
                    stage_info = this_stage or {}
                    diagnostics = _success_diag(prov, mdl)
                    status_label = prov
                    successful_provider = prov
                    attempts.append(_attempt_record(
                        prov, mdl, diagnostics, "succeeded", True, started))
                    break
                if cats is None:
                    ai_error, error_kind, diagnostics = \
                        _aggregate_chain_failure(
                            attempts, fallback_chain, last_error, last_kind,
                            last_diag)
                    try:
                        print("AI discovery failed (chain=%s): no proposals "
                              "generated. %s"
                              % ("->".join(fallback_chain or ["none"]),
                                 (ai_error or "")[:200]))
                    except Exception:
                        print("AI discovery failed; no proposals generated.")
        elif engine == "heuristic":
            # Explicit offline run only (never automatic, never labeled
            # as AI-generated).
            fallback_chain = ["heuristic"]
            _h_started = time.time()
            raw_core = _structure_heuristic(clusters, signals,
                                            min_categories, max_categories,
                                            books)
            valid, rejected = _strict_validate(raw_core, books, total,
                                               max_categories)
            if len(valid) < MIN_VALID_PROPOSALS:
                why = "; ".join(
                    "%s (%s)" % (r["name"] or "(blank)",
                                 ", ".join(r["reasons"][:3]))
                    for r in rejected[:6]) or "no usable clusters"
                ai_error = ("heuristic proposal produced only %d "
                            "strictly-valid categories: %s"
                            % (len(valid), why[:300]))
                error_kind = "insufficient_signal"
                diagnostics = {"provider": "heuristic", "model": "",
                               "http_status": None,
                               "error": ai_error[:500], "retries": 0}
                status_label = "heuristic (insufficient signal)"
                attempts.append(_attempt_record(
                    "heuristic", "", diagnostics, "insufficient_signal",
                    False, _h_started))
                cats = None
            else:
                cats = valid
                used = "heuristic"
                error_kind = "succeeded"
                status_label = "heuristic"
                successful_provider = "heuristic"
                diagnostics = {"provider": "heuristic", "model": "",
                               "http_status": None, "error": "",
                               "retries": 0}
                attempts.append(_attempt_record(
                    "heuristic", "", diagnostics, "succeeded", True,
                    _h_started))
        else:
            ai_error = "unknown discovery engine: %s" % (engine,)
            error_kind = "invalid_output"
            diagnostics = {"provider": str(engine), "model": model or "",
                           "http_status": None, "error": ai_error[:500],
                           "retries": 0}
            status_label = "error"
            cats = None
        successful_model = ""
        for _a in attempts:
            if _a.get("success"):
                successful_model = _a.get("model") or ""
                break
        try:
            from . import llm as _LLM3
            _primary3, _ = _LLM3.resolve_run_provider(engine) \
                if engine in ("ai", "gemini", "groq", "openrouter",
                              "cerebras", "heuristic", "auto") \
                else ("heuristic", None)
            _model3 = successful_model or (
                model or (_LLM3.model_for(_primary3)
                          if _primary3 != "heuristic" else ""))
        except Exception:
            _primary3, _model3 = engine, (successful_model or model or "")
        try:
            from . import llm as _LLM4
            _chain_labels = [_LLM4.PROVIDER_LABELS.get(n, n)
                             for n in (fallback_chain or [])]
        except Exception:
            _chain_labels = list(fallback_chain or [])
        # "Unsorted / Review" is a computed bucket, not a proposal: only
        # include it when items genuinely went unclassified (never as a
        # zero-count placeholder).
        tail = []
        if unsorted > 0:
            tail = [{"name": "Unsorted / Review",
                     "description": ("Items the offline pass could not "
                                     "place (%d unclassified; review "
                                     "manually)." % unsorted),
                     "count": unsorted, "estimated_count": unsorted,
                     "expected_count": unsorted,
                     "examples": [], "representative_ids": [],
                     "representative_urls": [], "confidence": 1.0}]
        failed = cats is None
        cats_out = [] if failed else (cats + tail)
        if failed:
            # Provider-error state: no proposals, explicit retry/heuristic
            # actions available. "engine" keeps the legacy offline label
            # for backwards compatibility; "provider_used" is the
            # truthful machine-readable field ("none" = nothing shown).
            used = "heuristic"
            provider_used = "none"
            is_fallback = engine in hosted
            status_label = ("%s failed" % engine) if engine in hosted \
                else status_label
        else:
            provider_used = used
            is_fallback = False
        payload = {"version": 3, "engine": used, "generated_at":
                   util.utcnow_iso(),
                   "engine_requested": engine,
                   "provider_requested": engine,
                   "model_requested": _model3,
                   "provider_used": provider_used,
                   "provider_actually_used": provider_used,
                   "mode": provider_used,
                   "fallback": is_fallback,
                   "fallback_chain": list(fallback_chain or []),
                   "fallback_chain_labels": _chain_labels,
                   "attempts": attempts,
                   "successful_provider": (successful_provider
                                           if not failed else "none"),
                   "model_used": successful_model,
                   "heuristic_fallback_used": bool(
                       not failed and successful_provider == "heuristic"
                       and engine == "auto"),
                   "heuristic_available": bool(failed and engine in hosted),
                   "ai_error": ai_error,
                   "error_kind": "succeeded" if not failed else error_kind,
                   "diagnostics": diagnostics,
                   "status_label": status_label,
                   "min_categories": min_categories,
                   "max_categories": max_categories,
                   "collection_size": total,
                   "stage_a_themes": stage_info.get("stage_a_themes", 0),
                   "rejected_count": len(rejected),
                   "rejected": [{"name": r["name"],
                                 "reasons": r["reasons"][:4]}
                                for r in rejected[:12]],
                   "signal_summary": {
                       "unclustered": unsorted,
                       "tweet_types": signals.get("tweet_types") or {},
                       "top_hashtags": signals.get("top_hashtags") or [],
                       "top_domains": signals.get("top_domains") or []},
                   "categories": cats_out}
        if failed:
            # Never clobber a previously stored good proposal with an
            # error state; write the error payload only when there is
            # nothing good to preserve.
            try:
                prev = util.read_json(
                    store.kb_path(kb, "category_structure.json"), None)
            except Exception:
                prev = None
            if not (isinstance(prev, dict) and prev.get("categories")):
                util.write_json(store.kb_path(kb, "category_structure.json"),
                                payload)
        else:
            util.write_json(store.kb_path(kb, "category_structure.json"),
                            payload)
        store.log_change(kb, {"kind": "category_discovery", "engine": used,
                              "count": len(cats_out),
                              "collection_size": payload["collection_size"]})
        return payload


def get_proposal(kb):
    """Return (proposal, None) or (None, error-string). Read-only.

    A stored provider-error state (no categories, but ai_error) is
    surfaced as its error text so the UI can show the failure state
    instead of a stale or fake proposal.
    """
    p = util.read_json(store.kb_path(kb, "category_structure.json"), None)
    if not p:
        return None, "no proposal yet - run Discover Categories first"
    if p.get("categories"):
        return p, None
    if p.get("ai_error"):
        return None, p["ai_error"]
    return None, "no proposal yet - run Discover Categories first"


def accept_structure(kb, categories, source="user"):
    """Install the reviewed/edited structure as the working library.

    categories: final list of {name, description} from the review UI.
    Renames/merges/deletes are just edits of this list before accepting.
    "Unsorted / Review" stays a computed state - never stored as a
    category."""
    store.ensure_kb(kb)
    from . import library as LIB
    clean = []
    for c in (categories or []):
        name = str((c or {}).get("name") or "").strip()
        if not name or len(name) > 60:
            continue
        if name.lower() == "unsorted / review":
            continue
        clean.append({"name": name,
                      "description": str((c or {}).get("description") or
                                        "").strip()[:300]})
    if not clean:
        raise ValueError("at least one real category is required")
    existing = util.read_json(store.kb_path(kb, "category_structure.json"),
                              None) or {}
    updated = dict(existing)
    updated["accepted_at"] = util.utcnow_iso()
    updated["accepted"] = True
    updated["categories"] = [
        {**c,
         "accepted_name": c["name"],
         "accepted_description": c["description"]} for c in clean]
    util.write_json(store.kb_path(kb, "category_structure.json"), updated)
    lib = LIB.Library(kb)
    keep = {_norm_key(c["name"]) for c in clean}
    for key in [k for k in list(lib.categories) if k not in keep]:
        lib.categories.pop(key)
    for c in clean:
        key = _norm_key(c["name"])
        if key in lib.categories:
            lib.categories[key]["description"] = c["description"]
            lib.categories[key]["source"] = source
        else:
            lib.categories[key] = {"name": c["name"], "source": source,
                                   "description": c["description"],
                                   "created_at": util.utcnow_iso()}
    for rec in lib.bookmarks.values():
        rec["categories"] = [c for c in rec["categories"]
                             if _norm_key(c) in keep]
    lib.save()
    # legacy mirror (dry-run/plan-writes still reads categories.json)
    legacy = {
        "version": 3, "engine": "accepted",
        "generated_at": util.utcnow_iso(),
        "categories": [
            {"name": c["name"], "description": c["description"],
             "expected_count": 0, "examples": [], "confidence": 1.0}
            for c in clean]}
    util.write_json(store.kb_path(kb, "categories.json"), legacy)
    store.log_change(kb, {"kind": "category_structure_accepted",
                          "count": len(clean),
                          "names": [c["name"] for c in clean]})
    return {"accepted": True, "count": len(clean),
            "names": [c["name"] for c in clean]}


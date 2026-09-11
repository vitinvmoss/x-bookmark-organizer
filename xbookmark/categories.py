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
    "prefer broad useful categories; avoid dozens of tiny categories; do "
    "not create categories that would contain only a handful of bookmarks "
    "unless they are a genuinely important theme; never base a category "
    "solely on one person or one website/domain; merge similar topics "
    "instead of splitting them; write names and descriptions in clear "
    "English; the count for each category must be a realistic estimate of "
    "how many of the person's bookmarks belong there; keep the example "
    "texts short. Use the exact counts and examples from the input "
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
DEFAULT_MAX_CATEGORIES = 20


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


def _structure_heuristic(clusters, signals, min_categories, max_categories):
    """Offline naming of the clusters - no AI, deterministic."""
    cats = []
    used_words = set()

    def take_name(tokens):
        nice = []
        used_local = set()
        for kind, tok in tokens:
            if tok.lower() in used_local:
                continue
            used_local.add(tok.lower())
            if kind == "h":
                nice.append("#" + tok)
            else:
                if tok not in used_words:
                    used_words.add(tok)
                nice.append(tok)
        return " ".join(nice[:3]).title() or "General Saves"

    for c in clusters:
        if len(cats) >= max_categories:
            break
        name = take_name(c["tokens"])
        if any(c["name"].lower() == name.lower() for c in cats):
            name = name + " (" + CL.render_tokens(c["tokens"][:1]).title() + ")"
        cats.append({"name": name,
                     "description": "Bookmarks matching: " +
                     CL.render_tokens(c["tokens"]) + ".",
                     "count": c["count"],
                     "examples": list(c["examples"]),
                     "confidence": 0.5})
    if len(cats) < min_categories and clusters:
        cats.append({"name": "General Saves",
                     "description": "Everything that did not fit a "
                                    "stronger cluster.",
                     "count": signals.get("unclustered", 0),
                     "examples": [], "confidence": 0.4})
    cats.sort(key=lambda c: -c["count"])
    cats = cats[:max_categories]
    cats.append({"name": "Unsorted / Review",
                 "description": "Ambiguous items the classifier is "
                                "unsure about.",
                 "count": 0, "examples": [], "confidence": 1.0})
    return cats


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
    """Dedupe/clamp the AI's proposal. Hard caps (max, <=6 core) always
    apply; the min is a soft floor - tiny real libraries must not be
    padded with meaningless padding buckets."""
    seen = []
    for c in (raw or []):
        name = str((c or {}).get("name") or "").strip()
        if not name or len(name) > 60:
            continue
        try:
            conf = float(c.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.5
        try:
            count = int(c.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        examples = [str(x).strip()[:160] for x in (c.get("examples") or [])[:3]
                    if str(x or "").strip()]
        item = {"name": name,
                "description": str(c.get("description") or "").strip()[:300],
                "count": max(0, count),
                "examples": examples,
                "confidence": max(0.0, min(1.0, conf))}
        if any(x["name"].lower() == name.lower() for x in seen):
            continue
        if name.lower() == "unsorted / review":
            continue
        seen.append(item)
    seen.sort(key=lambda x: -x["count"])
    hard_max = max(1, max_categories)
    if len(seen) > hard_max:
        seen = seen[:hard_max]
    if len(seen) > 6:
        seen = [c for c in seen[:hard_max] if c["count"] > 0] or seen[:1]
    tail = {"name": "Unsorted / Review",
            "description": "Ambiguous items the classifier is unsure "
                           "about.",
            "count": 0, "examples": [], "confidence": 1.0}
    return seen + [tail]


def discover_structure(kb, engine="ai", model=None, min_categories=8,
                       max_categories=20):
    """Whole-library discovery: LOCAL clustering pre-pass, then the
    configured model names/describes the major clusters (heuristic
    fallback). Proposal -> category_structure.json. Nothing is applied to
    the working library until the user accepts."""
    with _LOCK:
        store.ensure_kb(kb)
        books = store.load_bookmarks(kb)
        if not books:
            raise SystemExit(
                "no bookmarks in %s - run 'demo' or 'import' first" % kb)
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
        cats = None
        used = "heuristic"
        ai_error = ""
        hosted = {"ai", "gemini", "groq", "openrouter"}
        if engine in hosted:
            try:
                from . import llm as _LLM
                if engine == "ai":
                    # legacy path (OpenCode/Ollama bridge) — kept for
                    # backwards compatibility with existing flows/tests.
                    data = AI.chat_json(
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _structure_context(
                             clusters, signals, user_tags, min_categories,
                             max_categories)}],
                        max_tokens=3000, model=model)
                    _prov = "ai"
                else:
                    _prov = engine
                    _model = model or _LLM.model_for(_prov)
                    data = _LLM.chat_json(
                        _prov,
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": _structure_context(
                             clusters, signals, user_tags, min_categories,
                             max_categories)}],
                        model=_model)
                raw = data.get("categories") if isinstance(data, dict) \
                    else data
                if isinstance(raw, list) and raw:
                    cats = _validate_structure(raw, min_categories,
                                               max_categories)
                    used = _prov
                else:
                    ai_error = "empty AI response; heuristic fallback."
            except Exception as e:
                try:
                    from . import llm as _LLM2
                    ai_error = _LLM2._safe_error(e)
                except Exception:
                    ai_error = "%s" % str(e)[:200]
                print("AI discovery failed (%s); heuristic fallback."
                      % ai_error)
        if cats is None:
            cats = _validate_structure(
                _structure_heuristic(clusters, signals, min_categories,
                                     max_categories),
                min_categories, max_categories)
        unsorted = signals.get("unclustered", 0)
        try:
            from . import llm as _LLM3
            _primary3, _ = _LLM3.resolve_run_provider(engine) \
                if engine in ("ai", "gemini", "groq", "openrouter",
                              "heuristic") else ("heuristic", None)
            _model3 = model or (_LLM3.model_for(_primary3)
                                if _primary3 != "heuristic" else "")
        except Exception:
            _primary3, _model3 = engine, (model or "")
        payload = {"version": 3, "engine": used, "generated_at":
                   util.utcnow_iso(),
                   "engine_requested": engine,
                   "provider_requested": engine,
                   "model_requested": _model3,
                   "provider_used": used,
                   "provider_actually_used": used,
                   "mode": used,
                   "fallback": (engine in ("ai", "gemini", "groq",
                                           "openrouter")
                                and used == "heuristic"),
                   "ai_error": ai_error,
                   "min_categories": min_categories,
                   "max_categories": max_categories,
                   "collection_size": signals.get("total", len(books)),
                   "signal_summary": {
                       "unclustered": unsorted,
                       "tweet_types": signals.get("tweet_types") or {},
                       "top_hashtags": signals.get("top_hashtags") or [],
                       "top_domains": signals.get("top_domains") or []},
                   "categories": cats}
        util.write_json(store.kb_path(kb, "category_structure.json"),
                        payload)
        store.log_change(kb, {"kind": "category_discovery", "engine": used,
                              "count": len(cats),
                              "collection_size": payload["collection_size"]})
        return payload


def get_proposal(kb):
    """Return (proposal, None) or (None, error-string). Read-only."""
    p = util.read_json(store.kb_path(kb, "category_structure.json"), None)
    if not p or not p.get("categories"):
        return None, "no proposal yet - run Discover Categories first"
    return p, None


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


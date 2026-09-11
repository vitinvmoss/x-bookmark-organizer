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
                 "other", "general", "general saves", "stuff"}

ERROR_KINDS = ("succeeded", "fallback_transient", "not_configured",
               "invalid_output", "model_unavailable")


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


def _structure_heuristic(clusters, signals, min_categories, max_categories,
                         books=None):
    """Offline naming of the clusters - no AI, deterministic.

    Targets 8-15 broad categories for large collections: tiny clusters
    below a collection-scaled floor are merged/dropped instead of becoming
    their own folders. Never invents generic Interesting/Misc buckets
    unless the collection has no stronger signal.
    """
    total = int((signals or {}).get("total", 0) or 0)
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

    ordered = sorted(clusters or [], key=lambda c: -int(c.get("count", 0) or 0))
    # Pass 1: broad clusters at/above the floor.
    for c in ordered:
        if len(cats) >= target_max:
            break
        cnt = int(c.get("count", 0) or 0)
        if cnt < floor:
            continue
        name = take_name(c.get("tokens") or [])
        if any(x["name"].lower() == name.lower() for x in cats):
            name = name + " (" + CL.render_tokens(
                (c.get("tokens") or [])[:1]).title() + ")"
        if name.strip().lower() in GENERIC_NAMES:
            continue
        doc_ids = [str(t) for t in (c.get("doc_ids") or [])[:5]]
        ex = list(c.get("examples") or [])
        item = {"name": name,
                "description": "Bookmarks about " +
                CL.render_tokens(c.get("tokens") or []) +
                " (%d bookmarks)." % cnt,
                "count": cnt, "estimated_count": cnt,
                "expected_count": cnt,
                "examples": ex,
                "representative_ids": doc_ids,
                "representative_urls": [],
                "confidence": 0.5}
        cats.append(_enrich_proposal_item(item))
    # Pass 2: fill up to min_c with the next-biggest real clusters (still
    # theme-based, never generic padding) so small libraries aren't empty.
    if len(cats) < min(min_c, target_max):
        for c in ordered:
            if len(cats) >= min(min_c, target_max):
                break
            cnt = int(c.get("count", 0) or 0)
            if any(str(t) in (x.get("representative_ids") or [])
                   for x in cats for t in (c.get("doc_ids") or [])[:1]):
                continue
            name = take_name(c.get("tokens") or [])
            if any(x["name"].lower() == name.lower() for x in cats):
                continue
            if name.strip().lower() in GENERIC_NAMES:
                continue
            doc_ids = [str(t) for t in (c.get("doc_ids") or [])[:5]]
            item = {"name": name,
                    "description": "Bookmarks about " +
                    CL.render_tokens(c.get("tokens") or []) + ".",
                    "count": cnt, "estimated_count": cnt,
                    "expected_count": cnt,
                    "examples": list(c.get("examples") or []),
                    "representative_ids": doc_ids,
                    "representative_urls": [],
                    "confidence": 0.45}
            cats.append(_enrich_proposal_item(item))
    cats.sort(key=lambda c: -c["count"])
    cats = cats[:target_max]
    # Only as a last resort (no stronger signal at all) use a generic
    # bucket — never alongside real themes.
    if not cats and ordered:
        c0 = ordered[0]
        doc_ids = [str(t) for t in (c0.get("doc_ids") or [])[:5]]
        cats.append(_enrich_proposal_item({
            "name": "General Saves",
            "description": "Everything that did not fit a stronger cluster.",
            "count": int(signals.get("unclustered", total) or 0),
            "examples": list(c0.get("examples") or []),
            "representative_ids": doc_ids, "confidence": 0.4}))
    cats.append({"name": "Unsorted / Review",
                 "description": "Ambiguous items the classifier is "
                                "unsure about.",
                 "count": 0, "estimated_count": 0, "expected_count": 0,
                 "examples": [], "representative_ids": [],
                 "representative_urls": [], "confidence": 1.0})
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


def _classify_discovery_error(engine, exc, model_name=""):
    """Map a discovery failure to (ai_error, error_kind, diagnostics).

    error_kind is one of: not_configured, model_unavailable,
    invalid_output, fallback_transient. Safe: no prompts, contents,
    headers, cookies, or secrets — only provider/model/status/text<=500
    plus retry count.
    """
    from . import llm as _LLM
    prov = engine if engine in ("gemini", "groq", "openrouter") else "gemini"
    if engine == "ai":
        prov = "ai"
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
                % (prov, {"gemini": "GEMINI_API_KEY",
                          "groq": "GROQ_API_KEY",
                          "openrouter": "OPENROUTER_API_KEY"}.get(prov, "key")),
                "not_configured", diag)
    try:
        if _LLM.is_model_unavailable(exc):
            diag = _LLM.describe_error(prov, model_name, exc, retries)
            return (_LLM._safe_error(exc), "model_unavailable", diag)
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
        safe = msg if "malformed" in msg.lower() else \
            "provider returned invalid structured output"
        return ("Gemini returned invalid structured output (%s)"
                % safe[:200], "invalid_output", diag)
    try:
        safe = _LLM._safe_error(exc)
    except Exception:
        safe = "%s" % str(exc)[:200]
    try:
        diag = _LLM.describe_error(prov, model_name, exc, retries)
    except Exception:
        diag = {"provider": prov, "model": model_name or "",
                "http_status": getattr(exc, "code", None),
                "error": safe[:500], "retries": retries}
    return (safe, "fallback_transient", diag)


def discover_structure(kb, engine="ai", model=None, min_categories=8,
                       max_categories=15):
    """Whole-library discovery: LOCAL clustering pre-pass, then the
    configured model names/describes the major clusters (heuristic
    fallback). Proposal -> category_structure.json. Nothing is applied to
    the working library until the user accepts.

    Discovery NEVER classifies or permanently assigns bookmarks: it only
    proposes folders for user approval. Overwrites (never duplicates)
    the previous proposal file; library.json is untouched until Accept.
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
        cats = None
        used = "heuristic"
        ai_error = ""
        error_kind = "fallback_transient"
        diagnostics = {"provider": engine, "model": model or "",
                       "http_status": None, "error": "", "retries": 0}
        status_label = "heuristic"
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
                    _model = model or ""
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
                    error_kind = "succeeded"
                    ai_error = ""
                    try:
                        _diag_retries = int(
                            _LLM.LAST_DIAGNOSTICS.get("retries", 0) or 0)
                    except Exception:
                        _diag_retries = 0
                    diagnostics = {"provider": _prov, "model": _model,
                                   "http_status": 200, "error": "",
                                   "retries": _diag_retries}
                    status_label = _prov
                else:
                    ai_error = ("Gemini returned invalid structured output "
                                "(empty categories); heuristic fallback.")
                    error_kind = "invalid_output"
                    diagnostics = {"provider": _prov, "model": _model,
                                   "http_status": 200,
                                   "error": ai_error[:500], "retries": 0}
            except Exception as e:
                _m = ""
                try:
                    from . import llm as _LLM2
                    _m = model or _LLM2.model_for(engine) \
                        if engine != "ai" else (model or "")
                except Exception:
                    _m = model or ""
                try:
                    from . import llm as _LLM2b
                    ai_error, error_kind, diagnostics = \
                        _classify_discovery_error(engine, e, _m)
                except Exception:
                    ai_error = "%s" % str(e)[:200]
                    error_kind = "fallback_transient"
                # Safe single-line log: diagnostics only, never prompts.
                try:
                    print("AI discovery failed "
                          "(provider=%s model=%s status=%s retries=%s: %s); "
                          "heuristic fallback."
                          % (diagnostics.get("provider"),
                             diagnostics.get("model"),
                             diagnostics.get("http_status"),
                             diagnostics.get("retries"),
                             (ai_error or "")[:200]))
                except Exception:
                    print("AI discovery failed; heuristic fallback.")
        if cats is None:
            cats = _validate_structure(
                _structure_heuristic(clusters, signals, min_categories,
                                     max_categories, books),
                min_categories, max_categories)
            if engine in hosted and engine != "heuristic":
                status_label = "heuristic (fallback)"
            else:
                status_label = "heuristic"
                if not ai_error:
                    error_kind = "succeeded"
                    diagnostics = {"provider": "heuristic", "model": "",
                                   "http_status": None, "error": "",
                                   "retries": 0}
        else:
            status_label = used
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
                   "error_kind": error_kind if used == "heuristic"
                   else "succeeded",
                   "diagnostics": diagnostics,
                   "status_label": status_label,
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


"""Batched classification: each bookmark -> {tweet_id, category,
confidence, reason}. Cheap pre-pass (domain/hashtag/author) + AI for the
rest. Low confidence -> Unsorted / Review."""
from __future__ import annotations
from . import ai as AI
from . import store, util

BATCH = 20

HOSTED_ENGINES = {"ai", "gemini", "groq", "openrouter"}


def _hosted_chat_json(engine, messages, model=None, max_tokens=4000):
    """Dispatch hosted engines (ai legacy + gemini/groq/openrouter).

    Returns (parsed_json, provider_used). Raises on total failure so the
    caller can heuristic-fill the batch. Secondary fallback only when its
    key is configured (see llm.resolve_run_provider).
    """
    from . import llm as LLM
    eng = (engine or "ai").lower()
    if eng in ("gemini", "groq", "openrouter"):
        primary, secondary = LLM.resolve_run_provider(eng)
        last_err = RuntimeError("no hosted provider configured")
        for prov in [p for p in (primary, secondary) if p and p != "heuristic"]:
            try:
                data = LLM.chat_json(
                    prov, messages,
                    model=model or LLM.model_for(prov))
                return data, prov
            except Exception as e:
                last_err = e
                continue
        raise last_err
    return AI.chat_json(messages, max_tokens=max_tokens,
                        model=model), "ai"


def _cheap_guess(text, hashtags, domains, author, cats):
    hay = ("%s %s %s %s" % (text, " ".join(hashtags), " ".join(domains),
                            author)).lower()
    names = [c["name"] for c in cats
             if c["name"] != "Unsorted / Review"]
    best = None
    for name in names:
        for tok in name.lower().replace("/", " ").replace(":", " ").split():
            if len(tok) >= 4 and tok in hay:
                best = name
                break
        if best:
            break
    return best


def classify(kb, engine="ai", model=None,
             confidence_threshold=0.45, batch_size=BATCH) -> dict:
    store.ensure_kb(kb)
    bookmarks = store.load_bookmarks(kb)
    cats_doc = util.read_json(store.kb_path(kb, "categories.json"), None)
    if not cats_doc:
        raise SystemExit("run 'categories' first")
    cats = cats_doc["categories"]
    names = [c["name"] for c in cats]
    out_path = store.kb_path(kb, "classifications.jsonl")
    try:
        open(out_path, "w", encoding="utf-8").close()
    except FileNotFoundError:
        pass
    done = amb = 0
    items = list(bookmarks.values())
    with open(out_path, "w", encoding="utf-8") as fh:
        import json
        for i in range(0, len(items), batch_size):
            chunk = items[i:i + batch_size]
            results = [None] * len(chunk)
            need_idx, need_payload = [], []
            for j, b in enumerate(chunk):
                guess = _cheap_guess(b.get("text", "")[:500],
                                     b.get("hashtags") or [],
                                     b.get("domains") or [],
                                     b.get("author_handle") or "", cats)
                if guess and engine != "ai":
                    results[j] = {"tweet_id": b["tweet_id"],
                                  "category": guess, "confidence": 0.6,
                                  "reason": "heuristic keyword/domain match"}
                else:
                    need_idx.append(j)
                    need_payload.append({
                        "tweet_id": b["tweet_id"],
                        "author": b.get("author_handle"),
                        "text": (b.get("text") or "")[:600],
                        "hashtags": (b.get("hashtags") or [])[:8],
                        "domains": (b.get("domains") or [])[:4]})
            if need_payload and engine == "ai":
                prompt = ("Categories: %s\nAssign each item one category. "
                          "Return ONLY JSON array of {tweet_id, category, "
                          "confidence, reason}.\nItems:\n%s"
                          % (" | ".join(names),
                             "\n".join("- %s" % (p,) for p in need_payload)))
                try:
                    arr = AI.chat_json(
                        [{"role": "system",
                          "content": "You classify bookmarks. JSON only."},
                         {"role": "user", "content": prompt}],
                        max_tokens=3000, model=model)
                    if isinstance(arr, dict):
                        arr = arr.get("results") or arr.get("items") or []
                    for r in (arr or []):
                        for j in need_idx:
                            if chunk[j]["tweet_id"] == str(r.get("tweet_id")):
                                cat = str(r.get("category") or "")
                                if cat not in names:
                                    cat = "Unsorted / Review"
                                try:
                                    conf = float(r.get("confidence") or 0)
                                except Exception:
                                    conf = 0.4
                                if conf < confidence_threshold:
                                    cat = "Unsorted / Review"
                                results[j] = {
                                    "tweet_id": chunk[j]["tweet_id"],
                                    "category": cat, "confidence": conf,
                                    "reason": str(r.get("reason") or "")[:200]}
                except Exception as e:
                    print("AI classify batch failed (%s); heuristic fill."
                          % e)
            for j, b in enumerate(chunk):
                if results[j] is None:
                    guess = _cheap_guess(b.get("text", "")[:500],
                                         b.get("hashtags") or [],
                                         b.get("domains") or [],
                                         b.get("author_handle") or "", cats)
                    results[j] = {
                        "tweet_id": b["tweet_id"],
                        "category": guess or "Unsorted / Review",
                        "confidence": 0.55 if guess else 0.3,
                        "reason": "fallback: %s" % (
                            "keyword match" if guess else "no signal")}
                if results[j]["category"] == "Unsorted / Review":
                    amb += 1
                fh.write(json.dumps(results[j], ensure_ascii=False,
                                    sort_keys=True) + "\n")
                done += 1
    store.log_change(kb, {"kind": "classify", "engine": engine,
                          "classified": done, "unsorted": amb})
    return {"classified": done, "unsorted": amb}


def classify_subset(kb, tweet_ids, engine="ai", model=None,
                    confidence_threshold=0.45, batch_size=BATCH) -> dict:
    """Re-classify only the given bookmarks, merging into
    classifications.jsonl (keeps untouched rows; used by the UI's
    'Re-analyze selected' button so a partial run never wipes history)."""
    store.ensure_kb(kb)
    wanted = [str(t) for t in (tweet_ids or [])]
    if not wanted:
        return {"classified": 0, "unsorted": 0}
    cats_doc = util.read_json(store.kb_path(kb, "categories.json"), None)
    if not cats_doc:
        raise SystemExit("run 'categories' first")
    cats = cats_doc["categories"]
    names = [c["name"] for c in cats]
    bookmarks = store.load_bookmarks(kb)
    keep = []          # rows for ids NOT being re-run
    re_run = []        # bookmark dicts being re-run
    seen_keep = set()
    for row in util.iter_jsonl(store.kb_path(kb, "classifications.jsonl")):
        tid = str(row.get("tweet_id"))
        if tid in wanted:
            continue
        keep.append(row)
        seen_keep.add(tid)
    for t in wanted:
        b = bookmarks.get(t)
        if b is not None and t not in seen_keep:
            re_run.append(b)
    done = amb = 0
    out = []
    items = re_run
    with open(store.kb_path(kb, "classifications.jsonl"), "w",
              encoding="utf-8") as fh:
        import json
        for i in range(0, len(items), batch_size):
            chunk = items[i:i + batch_size]
            results = [None] * len(chunk)
            need_idx, need_payload = [], []
            for j, b in enumerate(chunk):
                guess = _cheap_guess(b.get("text", "")[:500],
                                     b.get("hashtags") or [],
                                     b.get("domains") or [],
                                     b.get("author_handle") or "", cats)
                if guess and engine != "ai":
                    results[j] = {"tweet_id": b["tweet_id"],
                                  "category": guess, "confidence": 0.6,
                                  "reason": "heuristic keyword/domain match"}
                else:
                    need_idx.append(j)
                    need_payload.append({
                        "tweet_id": b["tweet_id"],
                        "author": b.get("author_handle"),
                        "text": (b.get("text") or "")[:600],
                        "hashtags": (b.get("hashtags") or [])[:8],
                        "domains": (b.get("domains") or [])[:4]})
            if need_payload and engine == "ai":
                prompt = ("Categories: %s\nAssign each item one category. "
                          "Return ONLY JSON array of {tweet_id, category, "
                          "confidence, reason}.\nItems:\n%s"
                          % (" | ".join(names),
                             "\n".join("- %s" % (p,) for p in need_payload)))
                try:
                    arr = AI.chat_json(
                        [{"role": "system",
                          "content": "You classify bookmarks. JSON only."},
                         {"role": "user", "content": prompt}],
                        max_tokens=3000, model=model)
                    if isinstance(arr, dict):
                        arr = arr.get("results") or arr.get("items") or []
                    for r in (arr or []):
                        for j in need_idx:
                            if chunk[j]["tweet_id"] == str(r.get("tweet_id")):
                                cat = str(r.get("category") or "")
                                if cat not in names:
                                    cat = "Unsorted / Review"
                                try:
                                    conf = float(r.get("confidence") or 0)
                                except Exception:
                                    conf = 0.4
                                if conf < confidence_threshold:
                                    cat = "Unsorted / Review"
                                results[j] = {
                                    "tweet_id": chunk[j]["tweet_id"],
                                    "category": cat, "confidence": conf,
                                    "reason": str(r.get("reason") or "")[:200]}
                except Exception as e:
                    print("AI classify batch failed (%s); heuristic fill."
                          % e)
            for j, b in enumerate(chunk):
                if results[j] is None:
                    guess = _cheap_guess(b.get("text", "")[:500],
                                         b.get("hashtags") or [],
                                         b.get("domains") or [],
                                         b.get("author_handle") or "", cats)
                    results[j] = {
                        "tweet_id": b["tweet_id"],
                        "category": guess or "Unsorted / Review",
                        "confidence": 0.55 if guess else 0.3,
                        "reason": "fallback: %s" % (
                            "keyword match" if guess else "no signal")}
                if results[j]["category"] == "Unsorted / Review":
                    amb += 1
                out.append(results[j])
                done += 1
    with open(store.kb_path(kb, "classifications.jsonl"), "w",
              encoding="utf-8") as fh:
        for row in keep + out:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True)
                     + "\n")
    store.log_change(kb, {"kind": "classify_subset", "engine": engine,
                          "classified": done, "unsorted": amb})
    return {"classified": done, "unsorted": amb}


# ===================================================================== #
# Classify All: whole-collection organization against the ACCEPTED      #
# category structure (library.json). Multi-topic, resumable, preserves  #
# manual corrections.                                                   #
# ===================================================================== #

def confidence_level(conf):
    if conf is None:
        return "LOW"
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return "LOW"
    if c >= 0.85:
        return "HIGH"
    if c >= 0.60:
        return "MEDIUM"
    return "LOW"


INTENTS = ("read_later", "must_read", "try_this", "reference",
           "inspiration", "news", "entertainment")


def _multi_guess(text, hashtags, domains, author, cats):
    """Heuristic multi-topic guess: every accepted category sharing a
    keyword/hashtag/domain token with the bookmark. Returns
    (matches, alternatives)."""
    hay = ("%s %s %s %s" % (text, " ".join(hashtags), " ".join(domains),
                            author)).lower()
    scored = []
    for c in cats:
        name = c["name"] if isinstance(c, dict) else str(c)
        if name == "Unsorted / Review":
            continue
        toks = [t for t in name.lower().replace("/", " ").replace("&", " ")
                .replace(":", " ").replace("-", " ").split()
                if len(t) >= 4]
        hits = sum(1 for tok in toks if tok in hay)
        # hashtag carry-over: e.g. category "#Crypto ..." matches #crypto
        for h in (hashtags or []):
            if h.lower() in name.lower():
                hits += 1
        for d in (domains or []):
            part = d.split(".")[0]
            if len(part) >= 4 and part in name.lower():
                hits += 1
        if hits:
            scored.append((hits, name))
    scored.sort(reverse=True)
    matches = [n for _, n in scored[:2] if scored]
    alts = [n for _, n in scored[2:4]]
    return matches, alts


def _classify_all_prompt(names, chunk_payload, corrections):
    lines = [
        "You organize one person's saved X posts. TOPIC = what the post is "
        "about. INTENT = why they saved it (optional, one of: Read Later, "
        "Must Read, Try This, Reference, Inspiration, News, Entertainment, "
        'or null when unclear). Never confuse the two.',
        "Accepted TOPIC categories: %s" % " | ".join(names),
        "A bookmark may belong to 0-2 topic categories. "
        "Use [] when nothing fits (do NOT force).",
        "Return ONLY a JSON array, one object per item: "
        '{"tweet_id": str, "categories": [str], "confidence": 0.0-1.0, '
        '"reason": str (<=140 chars), "alternatives": [str], '
        '"intent": str|null}. Categories must come from the accepted list; '
        "unknown names are dropped.",
    ]
    if corrections:
        lines.append("Previous manual corrections (follow the same taste):")
        for r in corrections[-8:]:
            lines.append("- %s -> %s (intent %s -> %s)" % (
                r.get("bookmark_id"),
                ",".join(r.get("new_categories") or []),
                r.get("old_intent"), r.get("new_intent")))
    lines.append("Items:")
    for p in chunk_payload:
        lines.append("- %s" % (p,))
    return "\n".join(lines)


def classify_all(kb, engine="ai", model=None, batch_size=BATCH,
                 force=False, limit=None) -> dict:
    """Classify the whole collection against accepted library categories.

    Resumable: skips records that already have an automatic classification
    (confidence set + categories) and ALWAYS skips manually corrected
    records (manual/reviewed), unless force=True. Never duplicates work:
    each bookmark is written once per run.
    """
    import json
    from . import library as LIB
    store.ensure_kb(kb)
    lib = LIB.Library(kb)
    names = [c["name"] for c in lib.category_list()]
    if not names:
        raise SystemExit("accept a category structure first")
    name_set = set(names)
    books = store.load_bookmarks(kb)
    todo = []
    skipped = 0
    for tid in books:
        rec = lib.record(tid)
        if not force and rec.get("manual"):
            skipped += 1
            continue
        if not force and rec.get("reviewed") and rec.get("categories"):
            skipped += 1
            continue
        if not force and rec.get("classified_at"):
            skipped += 1  # already processed (even if unsorted) -> resume
            continue
        if not force and rec.get("confidence") is not None and \
                rec.get("categories"):
            skipped += 1  # already classified -> resume without redo
            continue
        todo.append(tid)
    if limit:
        todo = todo[:limit]
    try:
        from . import feedback as FB
        corrections = FB.load_all(kb, limit=40)
    except Exception:
        corrections = []
    cats_for_guess = [{"name": n} for n in names]
    done = unsorted = 0
    batches = 0
    out_rows = []
    ai_ok = 0
    ai_fail = 0
    heuristic_fills = 0
    ai_errors: list = []
    providers_used: set = set()
    for i in range(0, len(todo), batch_size):
        chunk_ids = todo[i:i + batch_size]
        chunk = [books[t] for t in chunk_ids if t in books]
        results = [None] * len(chunk)
        batch_used_ai = False
        provider_used_this_batch = None
        if engine in HOSTED_ENGINES:
            payload = [{
                "tweet_id": b.get("tweet_id"),
                "author": b.get("author_handle"),
                "text": (b.get("text") or "")[:600],
                "hashtags": (b.get("hashtags") or [])[:8],
                "domains": (b.get("domains") or [])[:4],
                "mentions": (b.get("mentions") or [])[:8]} for b in chunk]
            try:
                # Single attempt per batch: no uncontrolled retries. Any
                # failure (HTTP 502, timeout, bad JSON) falls through to
                # heuristic fill so one bad batch never fails the run.
                from . import llm as _LLM
                arr, _prov = _hosted_chat_json(
                    engine,
                    [{"role": "system",
                      "content": "You classify bookmarks. JSON only."},
                     {"role": "user",
                      "content": _LLM.classify_prompt(
                          names, payload, corrections)}],
                    model=model)
                provider_used_this_batch = _prov
                if isinstance(arr, dict):
                    arr = arr.get("results") or arr.get("items") or []
                by_id = {str(r.get("tweet_id")): r for r in (arr or [])
                         if isinstance(r, dict)}
                for j, b in enumerate(chunk):
                    r = by_id.get(str(b.get("tweet_id")))
                    if not r:
                        continue
                    cats = [c for c in (r.get("categories") or [])
                            if c in name_set][:2]
                    try:
                        conf = float(r.get("confidence") or 0)
                    except (TypeError, ValueError):
                        conf = 0.4
                    conf = max(0.0, min(1.0, conf))
                    if r.get("review_required") is True and conf >= 0.60:
                        conf = 0.55  # honor model-flagged uncertainty
                    intent = r.get("intent")
                    if isinstance(intent, str):
                        key = intent.strip().lower().replace(" ", "_")
                        intent = key if key in INTENTS else None
                    else:
                        intent = None
                    alts = [a for a in (r.get("alternatives") or [])
                            if a in name_set][:3]
                    results[j] = {
                        "tweet_id": str(b.get("tweet_id")),
                        "categories": cats, "confidence": conf,
                        "confidence_level": confidence_level(conf),
                        "reason": str(r.get("reason") or "")[:200],
                        "alternatives": alts, "intent": intent}
                if any(v is not None for v in results):
                    batch_used_ai = True
                    ai_ok += 1
                    if provider_used_this_batch:
                        providers_used.add(provider_used_this_batch)
            except Exception as e:
                # Graceful per-batch fallback: record the error, keep
                # going with heuristic fill. Never fail the whole run.
                # Sanitize: never log keys/headers/raw bodies.
                ai_fail += 1
                from . import llm as _LLM2
                msg = _LLM2._safe_error(e)
                ai_errors.append("batch %d: %s" % (batches + 1, msg))
                print("AI classify-all batch failed (%s); heuristic fill."
                      % msg)
        for j, b in enumerate(chunk):
            if results[j] is None:
                heuristic_fills += 1
                if engine in HOSTED_ENGINES:
                    # AI unavailable/missing row: heuristic fill so the run
                    # still completes offline.
                    m, a = _multi_guess(
                        (b.get("text") or "")[:600], b.get("hashtags") or [],
                        b.get("domains") or [], b.get("author_handle") or "",
                        cats_for_guess)
                    results[j] = {
                        "tweet_id": str(b.get("tweet_id")),
                        "categories": m,
                        "confidence": 0.65 if m else 0.3,
                        "confidence_level": confidence_level(
                            0.65 if m else 0.3),
                        "reason": "heuristic keyword/domain match" if m
                        else "no signal",
                        "alternatives": a, "intent": None}
                else:
                    m, a = _multi_guess(
                        (b.get("text") or "")[:600], b.get("hashtags") or [],
                        b.get("domains") or [], b.get("author_handle") or "",
                        cats_for_guess)
                    results[j] = {
                        "tweet_id": str(b.get("tweet_id")),
                        "categories": m,
                        "confidence": 0.65 if m else 0.3,
                        "confidence_level": confidence_level(
                            0.65 if m else 0.3),
                        "reason": "heuristic keyword/domain match" if m
                        else "no signal",
                        "alternatives": a, "intent": None}
        for r in results:
            tid = r["tweet_id"]
            lib.set_bookmark(
                tid, categories=r["categories"],
                confidence=r["confidence"],
                reviewed=False, save=False,
                reason=r["reason"], alternatives=r["alternatives"],
                manual=False, classified_at=__import__(
                    "xbookmark.util", fromlist=["utcnow_iso"]).utcnow_iso(),
                _log_feedback=False)
            if r.get("intent"):
                # intent is optional/uncertain: only set when the model
                # actually suggested one; never overwrite a manual intent.
                cur = lib.record(tid).get("intent")
                if not cur:
                    lib.set_bookmark(tid, intent=r["intent"], save=False,
                                     _log_feedback=False)
            lib.bookmarks[tid]["confidence_level"] = r["confidence_level"]
            if not r["categories"]:
                unsorted += 1
            out_rows.append(r)
            done += 1
        batches += 1
        lib.save()  # persist per batch -> resumable if interrupted
    if out_rows:
        with open(store.kb_path(kb, "classifications.jsonl"), "a",
                  encoding="utf-8") as fh:
            for r in out_rows:
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True)
                         + "\n")
    store.log_change(kb, {"kind": "classify_all", "engine": engine,
                          "classified": done, "skipped": skipped,
                          "unsorted": unsorted, "batches": batches,
                          "mode": _classify_mode(engine, ai_ok,
                                                 heuristic_fills)})
    from . import llm as _LLM3
    _primary, _ = _LLM3.resolve_run_provider(engine) \
        if engine in HOSTED_ENGINES else (engine, None)
    _used = sorted(providers_used)[0] if providers_used else (
        "heuristic" if heuristic_fills and not ai_ok else _primary)
    _mode = _classify_mode(engine, ai_ok, heuristic_fills)
    return {"classified": done, "skipped": skipped, "unsorted": unsorted,
            "batches": batches, "total": len(books),
            "engine_requested": engine,
            "provider_requested": engine,
            "model_requested": (model or _LLM3.model_for(_primary))
            if engine in HOSTED_ENGINES and engine != "ai" else (model or ""),
            "provider_used": _used,
            "provider_actually_used": _used,
            "mode": _mode if _mode in (
                "gemini", "groq", "openrouter", "heuristic", "mixed",
                "ai") else "heuristic",
            "successful_batches": ai_ok, "failed_batches": ai_fail,
            "fallback_batches": heuristic_fills,
            "ai_batches": ai_ok, "ai_failures": ai_fail,
            "heuristic_fills": heuristic_fills,
            "ai_errors": ai_errors[:5],
            "errors": ai_errors[:5],
            "resumable": True,
            "note": _classify_note(engine, ai_ok, heuristic_fills,
                                   ai_errors)}


def _classify_mode(engine, ai_ok, heuristic_fills):
    if engine not in HOSTED_ENGINES:
        return "heuristic"
    if engine in ("gemini", "groq", "openrouter"):
        if ai_ok and heuristic_fills:
            return "mixed"
        if ai_ok:
            return engine
        return "heuristic"
    if ai_ok and heuristic_fills:
        return "mixed"
    if ai_ok:
        return "ai"
    return "heuristic"


def _classify_note(engine, ai_ok, heuristic_fills, ai_errors):
    if engine not in HOSTED_ENGINES:
        return "heuristic run (offline, deterministic)."
    if ai_ok and not heuristic_fills:
        return "AI run: all batches classified by AI."
    if ai_ok:
        return ("mixed run: AI classified %d batch(es), heuristic filled "
                "the rest." % ai_ok)
    err = (" Last error: %s" % ai_errors[-1]) if ai_errors else ""
    return ("heuristic fallback: AI unavailable, all results heuristic.%s"
            % err)

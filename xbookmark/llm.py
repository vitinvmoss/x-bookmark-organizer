"""Hosted LLM provider layer: gemini (default) / groq / openrouter / heuristic.

All requests are server-side via stdlib urllib (never expose keys to the
browser). Compact structured JSON prompts, batched, bounded timeouts, at
most ONE controlled retry, no uncontrolled retries.

Every run reports: provider_requested, model_requested, provider_used,
mode, successful/failed/fallback batches, safe error messages, resumable.

Secrets are never logged; errors are sanitized.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.1-8b-instruct:free"

PROVIDERS = ("gemini", "groq", "openrouter", "heuristic")


def _bounded_timeout(raw, default=25):
    try:
        t = int(raw or default)
    except (TypeError, ValueError):
        t = default
    return max(5, min(t, 60))


def llm_timeout():
    return _bounded_timeout(os.environ.get("GEMINI_TIMEOUT"), 25)


def provider_default():
    p = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    return p if p in PROVIDERS else "gemini"


def model_for(provider):
    provider = (provider or "").lower()
    if provider == "gemini":
        return os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    if provider == "groq":
        return os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
    return ""


def configured_provider_status():
    """Safe config report: which providers have keys, no secrets."""
    prov = provider_default()
    return {
        "default_provider": prov,
        "default_model": model_for(prov) if prov != "heuristic" else "",
        "gemini": {"configured": bool(os.environ.get("GEMINI_API_KEY")),
                   "model": os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL},
        "groq": {"configured": bool(os.environ.get("GROQ_API_KEY")),
                 "model": os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL},
        "openrouter": {"configured": bool(os.environ.get("OPENROUTER_API_KEY")),
                       "model": os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL},
        "timeout_s": llm_timeout(),
    }


def _safe_error(exc):
    """Map provider failures to safe, user-facing messages (no keys)."""
    import re as _re
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code == 400:
            return "provider rejected the request (HTTP 400, bad request)"
        if code == 401:
            return "invalid API key (HTTP 401)"
        if code == 403:
            return "API key lacks permission (HTTP 403)"
        if code == 429:
            return "rate limited / quota exhausted (HTTP 429)"
        if 500 <= code <= 599:
            return "provider server error (HTTP %d)" % code
        return "provider HTTP error %d" % code
    name = type(exc).__name__
    msg = str(exc)[:160]
    if "timed out" in msg.lower() or "timeout" in name.lower() or "Timeout" in name:
        return "provider request timed out"
    if isinstance(exc, (ConnectionError, OSError)) or "urlopen" in name.lower() \
            or "URLError" in name:
        return "connection to provider failed"
    if isinstance(exc, (json.JSONDecodeError, ValueError)) and "json" in msg.lower():
        return "provider returned malformed JSON"
    if "quota" in msg.lower() or "exhaust" in msg.lower():
        return "provider quota exhausted"
    # redact anything key-like before cleaning; never leak raw bodies/keys
    msg = _re.sub(r"(?i)(bearer\s+[A-Za-z0-9._\-~+/=]+)", "Bearer [redacted]",
                  msg)
    msg = _re.sub(r"(?i)\b(sk-[A-Za-z0-9\-_]{4,}|xox[bpas]-[A-Za-z0-9\-]+)",
                  "[redacted]", msg)
    msg = _re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)\S+", r"\1[redacted]", msg)
    clean = "".join(c for c in msg if c.isalnum() or c in " .,:;()-_%[]")
    return "%s: %s" % (name, clean[:140] or "request failed")


def _post_json(url, payload, headers, timeout):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _gemini_call(prompt_text, model, timeout):
    key = os.environ.get("GEMINI_API_KEY") or ""
    if not key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s"
           ":generateContent" % model)
    # key passed as query param by urllib to avoid header logging; stdlib
    # only — never sent to browser. Use header instead to keep URL clean:
    url += "?key=" + key if False else ""
    # NOTE: key must travel server-side only; attach as x-goog-api-key header
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4000,
                             "responseMimeType": "application/json"},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    last = None
    for _attempt in range(2):  # 1 initial + 1 controlled retry max
        try:
            data = _post_json(url, payload, headers, timeout)
            cands = (data.get("candidates") or [])
            parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if not text.strip():
                # block/empty → treat as malformed
                raise ValueError("provider returned malformed JSON (empty candidates)")
            return text
        except urllib.error.HTTPError as e:
            # retry only transient 429/5xx, once
            if e.code in (429,) or 500 <= e.code <= 599:
                last = e
                time.sleep(1.0)
                continue
            raise
        except (TimeoutError, OSError) as e:
            last = e
            time.sleep(1.0)
            continue
    raise last


def _openai_compat_call(base, key, model, messages, timeout):
    if not key:
        raise RuntimeError("API key not configured")
    payload = {"model": model, "messages": messages, "temperature": 0.2,
               "max_tokens": 4000}
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + key}
    last = None
    for _attempt in range(2):
        try:
            data = _post_json(base.rstrip("/") + "/chat/completions",
                              payload, headers, timeout)
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code in (429,) or 500 <= e.code <= 599:
                last = e
                time.sleep(1.0)
                continue
            raise
        except (TimeoutError, OSError, KeyError, IndexError) as e:
            last = e
            if isinstance(e, (KeyError, IndexError)):
                raise ValueError("provider returned malformed JSON (bad chat envelope)")
            time.sleep(1.0)
            continue
    raise last


def chat_text(provider, messages, model=None, timeout=None):
    """Single hosted call returning raw text. Raises with safe errors."""
    provider = (provider or "gemini").lower()
    timeout = timeout or llm_timeout()
    user_text = "\n\n".join(
        m.get("content", "") for m in (messages or []) if isinstance(m, dict))
    system = next((m.get("content", "") for m in (messages or [])
                   if isinstance(m, dict) and m.get("role") == "system"), "")
    if provider == "gemini":
        full = (system + "\n\n" + user_text).strip()
        return _gemini_call(full, model or model_for("gemini"), timeout)
    if provider == "groq":
        return _openai_compat_call("https://api.groq.com/openai/v1",
                                   os.environ.get("GROQ_API_KEY") or "",
                                   model or model_for("groq"), messages, timeout)
    if provider == "openrouter":
        return _openai_compat_call("https://openrouter.ai/api/v1",
                                   os.environ.get("OPENROUTER_API_KEY") or "",
                                   model or model_for("openrouter"), messages, timeout)
    raise RuntimeError("unknown provider: %s" % provider)


def parse_json_array_or_obj(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    if not starts:
        raise ValueError("provider returned malformed JSON (no object/array)")
    start = min(starts)
    ends = [e for e in (t.rfind("}"), t.rfind("]")) if e >= 0]
    end = max(ends) + 1
    return json.loads(t[start:end])


def chat_json(provider, messages, model=None, timeout=None):
    text = chat_text(provider, messages, model=model, timeout=timeout)
    try:
        return parse_json_array_or_obj(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError("provider returned malformed JSON (%s)" % str(e)[:120])


def classify_prompt(names, chunk_payload, corrections=None):
    lines = [
        "You organize one person's saved X posts. TOPIC = what the post is "
        "about. INTENT = why they saved it (optional, one of: read_later, "
        "must_read, try_this, reference, inspiration, news, entertainment, "
        'or null when unclear). Never confuse the two.',
        "Accepted TOPIC categories: %s" % " | ".join(names),
        "A bookmark may belong to 0-2 topic categories. Use [] when nothing "
        "fits (do NOT force).",
        'Return ONLY a JSON array, one object per item: {"tweet_id": str, '
        '"categories": [str], "intent": str|null, "confidence": 0.0-1.0, '
        '"reason": str (<=140 chars), "alternatives": [str], '
        '"review_required": bool}. Categories must come from the accepted '
        "list; unknown names are dropped. Set review_required=true when "
        "confidence < 0.60 or nothing fits.",
    ]
    if corrections:
        lines.append("Previous manual corrections (follow the same taste):")
        for r in corrections[-8:]:
            lines.append("- %s -> %s" % (r.get("bookmark_id"),
                                         ",".join(r.get("new_categories") or [])))
    lines.append("Items:")
    for p in chunk_payload:
        lines.append("- %s" % (p,))
    return "\n".join(lines)


def discover_prompt(context_text):
    return context_text


def resolve_run_provider(requested):
    """Return (primary, secondary_or_None). Secondary only when its key set."""
    requested = (requested or provider_default()).lower()
    if requested == "ai":  # legacy alias → default hosted
        requested = "gemini" if provider_default() == "gemini" else provider_default()
    if requested not in ("gemini", "groq", "openrouter", "heuristic"):
        requested = "gemini"
    if requested == "heuristic":
        return "heuristic", None
    # optional second hosted fallback only when its key configured
    secondary = None
    for cand, env in (("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
                      ("openrouter", "OPENROUTER_API_KEY")):
        if cand != requested and os.environ.get(env):
            secondary = cand
            break
    return requested, secondary

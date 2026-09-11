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

# Retryable transient statuses: exactly one controlled retry, short bounded
# backoff. Anything else (400/401/403/404) fails fast with an actionable
# message — never silently retried, never silently switched providers.
RETRYABLE_STATUS = (429, 500, 502, 503, 504)
RETRY_BACKOFF_S = 0.8

# Carries the safe diagnostics of the most recent hosted call for
# categories.py / server.py to label results without logging secrets.
LAST_DIAGNOSTICS: dict = {}


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


def _redact_secrets(text):
    """Redact anything key-like plus the actual configured key values."""
    import re as _re
    s = str(text or "")
    # Never let a real configured key value appear in logs/errors.
    for env in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                "XBO_AI_KEY"):
        try:
            v = os.environ.get(env) or ""
        except Exception:
            v = ""
        if v and len(v) >= 4 and v in s:
            s = s.replace(v, "[redacted]")
    s = _re.sub(r"(?i)(bearer\s+[A-Za-z0-9._\-~+/=]+)", "Bearer [redacted]",
                s)
    s = _re.sub(r"(?i)\b(sk-[A-Za-z0-9\-_]{4,}|xox[bpas]-[A-Za-z0-9\-]+)",
                "[redacted]", s)
    s = _re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)\S+", r"\1[redacted]", s)
    s = _re.sub(r"(?i)(x-goog-api-key\s*[:=]\s*)\S+", r"\1[redacted]", s)
    s = _re.sub(r"(?i)([?&]key\s*=\s*)[^&\s]+", r"\1[redacted]", s)
    return s


def _read_http_body(exc, limit=500):
    """Safely extract provider error text (truncated, redacted).

    Only the provider's error payload — never prompts, bookmark contents,
    headers, cookies, or secrets.
    """
    try:
        raw = exc.read() if hasattr(exc, "read") else b""
    except Exception:
        return ""
    try:
        if isinstance(raw, bytes):
            txt = raw.decode("utf-8", errors="ignore")
        else:
            txt = str(raw or "")
    except Exception:
        return ""
    txt = _redact_secrets(txt)
    # keep it one line-ish and bounded
    txt = " ".join(txt.split())
    return txt[:limit]


def _safe_error(exc):
    """Map provider failures to safe, user-facing messages (no keys)."""
    import re as _re
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        body = _read_http_body(exc)
        # Model-not-found is actionable — tell the operator exactly what
        # to check instead of silently producing poor categories.
        low = body.lower()
        if code in (400, 404) and (
                "not found" in low or "not_found" in low
                or "unknown model" in low or "invalid model" in low
                or "model_not_found" in low or "unsupported model" in low):
            base = ("model unavailable (HTTP %d). Check GEMINI_MODEL "
                    "(current default 'gemini-3.8-flash') and the "
                    "provider dashboard" % code)
            if body:
                return "%s: %s" % (base, body[:200])
            return base
        if code == 400:
            base = "provider rejected the request (HTTP 400, bad request)"
        elif code == 401:
            base = "invalid API key (HTTP 401)"
        elif code == 403:
            base = "API key lacks permission (HTTP 403)"
        elif code == 429:
            base = "rate limited / quota exhausted (HTTP 429)"
        elif code == 404:
            base = ("model or endpoint not found (HTTP 404). Check "
                    "GEMINI_MODEL (default 'gemini-3.8-flash')")
        elif 500 <= code <= 599:
            base = "provider server error (HTTP %d)" % code
        else:
            base = "provider HTTP error %d" % code
        if body:
            # provider error text only, truncated, already redacted
            return "%s: %s" % (base, body[:200])
        return base
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
    msg = _redact_secrets(msg)
    clean = "".join(c for c in msg if c.isalnum() or c in " .,:;()-_%[]")
    return "%s: %s" % (name, clean[:140] or "request failed")


def gemini_request_info(model=None, timeout=None):
    """Safe request facts for diagnostics (no key, no prompt, no headers).

    Returns the exact endpoint URL (key-free), model name, and timeout so
    operators can verify what was called without exposing secrets.
    """
    m = model or model_for("gemini")
    t = timeout or llm_timeout()
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s"
           ":generateContent" % m)
    return {"provider": "gemini", "model": m, "url": url, "timeout_s": t}


def describe_error(provider, model, exc, retries):
    """Safe diagnostics dict: provider, model, HTTP status, error text<=500,
    retry count. No prompts, contents, headers, cookies, or secrets."""
    status = getattr(exc, "code", None) if isinstance(
        exc, urllib.error.HTTPError) else None
    if isinstance(exc, urllib.error.HTTPError):
        err_text = _read_http_body(exc, limit=500)
        if not err_text:
            # fall back to the mapped safe message (already redacted)
            try:
                err_text = _safe_error(exc)[:500]
            except Exception:
                err_text = "provider HTTP error %s" % (status,)
    else:
        try:
            err_text = _redact_secrets(str(exc))[:500]
        except Exception:
            err_text = "request failed"
        # belt-and-suspenders: strip anything that looks like a secret
        err_text = " ".join(str(err_text).split())[:500]
    # final guarantee: configured key values never appear
    err_text = _redact_secrets(err_text)[:500]
    info = {"provider": (provider or "gemini"),
            "model": (model or model_for(provider or "gemini")),
            "http_status": status,
            "error": err_text,
            "retries": int(retries or 0)}
    try:
        LAST_DIAGNOSTICS.clear()
        LAST_DIAGNOSTICS.update(info)
    except Exception:
        pass
    return dict(info)


def is_model_unavailable(exc):
    """True when the provider response clearly proves a bad model name."""
    if not isinstance(exc, urllib.error.HTTPError):
        return False
    if exc.code not in (400, 404):
        return False
    try:
        body = _read_http_body(exc, limit=500).lower()
    except Exception:
        body = ""
    keys = ("not found", "not_found", "unknown model", "invalid model",
            "model_not_found", "unsupported model")
    return any(k in body for k in keys)


def is_invalid_output_error(exc):
    msg = str(exc or "").lower()
    return "malformed json" in msg or "invalid structured" in msg


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
    # NOTE: key must travel server-side only; attach as x-goog-api-key header
    # (never in the URL, never to the browser). Only bookmark text snippets
    # are sent, and only to this configured Gemini endpoint.
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8000,
                             "responseMimeType": "application/json"},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    last = None
    retries = 0
    for _attempt in range(2):  # 1 initial + 1 controlled retry max
        try:
            try:
                data = _post_json(url, payload, headers, timeout)
            except ValueError as ve:
                # JSON decode of a 200 response -> invalid structured output
                raise ValueError(
                    "provider returned malformed JSON (%s)"
                    % _redact_secrets(str(ve))[:120])
            if not isinstance(data, dict):
                raise ValueError(
                    "provider returned malformed JSON (bad envelope)")
            # API-level error delivered as 200 JSON: surface safely.
            if data.get("error"):
                try:
                    em = json.dumps(data["error"])[:500]
                except Exception:
                    em = str(data.get("error"))[:500]
                raise ValueError("provider returned malformed JSON "
                                 "(api error: %s)" % _redact_secrets(em)[:200])
            cands = (data.get("candidates") or [])
            parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if not text.strip():
                # block/empty → treat as malformed (invalid structured output)
                raise ValueError("provider returned malformed JSON (empty candidates)")
            describe_error("gemini", model, RuntimeError("ok"), retries) \
                if False else None
            try:
                LAST_DIAGNOSTICS.clear()
                LAST_DIAGNOSTICS.update(
                    {"provider": "gemini", "model": model,
                     "http_status": 200, "error": "", "retries": retries})
            except Exception:
                pass
            return text
        except urllib.error.HTTPError as e:
            # retry only transient 429/500/502/503/504, exactly once
            if e.code in RETRYABLE_STATUS and _attempt == 0:
                last = e
                retries = 1
                time.sleep(RETRY_BACKOFF_S)
                continue
            try:
                describe_error("gemini", model, e, retries)
            except Exception:
                pass
            # tag retry count for callers that inspect the exception
            try:
                e._llm_retries = retries  # type: ignore[attr-defined]
            except Exception:
                pass
            raise
        except (TimeoutError, OSError) as e:
            # URLError wraps OSError; retry once on transient IO/timeout
            if "URLError" in type(e).__name__ or isinstance(
                    e, (TimeoutError, ConnectionError, OSError)):
                if _attempt == 0:
                    last = e
                    retries = 1
                    time.sleep(RETRY_BACKOFF_S)
                    continue
            try:
                describe_error("gemini", model, e, retries)
            except Exception:
                pass
            try:
                e._llm_retries = retries  # type: ignore[attr-defined]
            except Exception:
                pass
            raise
    try:
        describe_error("gemini", model, last, retries)
    except Exception:
        pass
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
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, AttributeError):
                raise ValueError("provider returned malformed JSON (bad chat envelope)")
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_STATUS and _attempt == 0:
                last = e
                time.sleep(RETRY_BACKOFF_S)
                continue
            raise
        except (TimeoutError, OSError) as e:
            if _attempt == 0:
                last = e
                time.sleep(RETRY_BACKOFF_S)
                continue
            raise
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
        # Never include prompts/bookmark text — only the safe parser hint.
        detail = _redact_secrets(str(e))[:120]
        detail = "".join(
            c for c in detail if c.isalnum() or c in " .,:;()-_%[]")[:120]
        raise ValueError("provider returned malformed JSON (%s)" % detail)


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

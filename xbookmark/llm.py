"""Hosted LLM provider layer: gemini / groq / openrouter / cerebras / heuristic.

Providers are isolated behind a small adapter interface (see the
``Provider`` classes near the bottom of this module) so callers such as
``categories.py`` never need to know how an individual HTTP API works.
The layer also owns the provider preference/fallback chain:

  LLM_PROVIDER=auto|gemini|groq|openrouter|cerebras|heuristic
  LLM_FALLBACK_PROVIDERS=gemini,groq,openrouter,cerebras
  LLM_ALLOW_HEURISTIC_FALLBACK=false

When mode is ``auto`` the configured hosted providers are tried in the
deterministic ``LLM_FALLBACK_PROVIDERS`` order; a provider with no API key
is never attempted; transient failures and invalid structured output move
on to the next provider; the offline heuristic runs only when it is the
explicitly enabled final fallback.

All requests are server-side via stdlib urllib (never expose keys to the
browser). Compact structured JSON prompts, batched, bounded timeouts, at
most ONE controlled retry per provider, no uncontrolled retries.

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
DEFAULT_OPENROUTER_MODEL = "openai/gpt-oss-20b:free"
DEFAULT_CEREBRAS_MODEL = "llama-3.3-70b"

PROVIDERS = ("gemini", "groq", "openrouter", "cerebras", "heuristic")
HOSTED_PROVIDERS = ("gemini", "groq", "openrouter", "cerebras")
VALID_MODES = ("auto", "gemini", "groq", "openrouter", "cerebras", "heuristic")
DEFAULT_FALLBACK_ORDER = ("gemini", "groq", "openrouter", "cerebras")

KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
           "openrouter": "OPENROUTER_API_KEY", "cerebras": "CEREBRAS_API_KEY"}
MODEL_ENV = {"gemini": "GEMINI_MODEL", "groq": "GROQ_MODEL",
             "openrouter": "OPENROUTER_MODEL", "cerebras": "CEREBRAS_MODEL"}
DEFAULT_MODEL = {"gemini": DEFAULT_GEMINI_MODEL, "groq": DEFAULT_GROQ_MODEL,
                 "openrouter": DEFAULT_OPENROUTER_MODEL,
                 "cerebras": DEFAULT_CEREBRAS_MODEL}
PROVIDER_LABELS = {"gemini": "Gemini", "groq": "Groq",
                   "openrouter": "OpenRouter", "cerebras": "Cerebras",
                   "heuristic": "Heuristic"}

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
    p = (os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
    if p == "ai":  # legacy alias -> hosted auto chain
        return "auto"
    return p if p in VALID_MODES else "auto"


def normalize_mode(raw):
    """Coerce an arbitrary engine/mode string to a supported mode."""
    p = (raw or "").strip().lower()
    if p == "ai":  # legacy alias
        return "auto"
    if p in VALID_MODES:
        return p
    return provider_default()


def model_for(provider):
    provider = (provider or "").lower()
    if provider == "gemini":
        return os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    if provider == "groq":
        return os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL
    if provider == "openrouter":
        return os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
    if provider == "cerebras":
        return os.environ.get("CEREBRAS_MODEL") or DEFAULT_CEREBRAS_MODEL
    return ""


def parse_fallback_order():
    """LLM_FALLBACK_PROVIDERS as a deduped list of hosted providers.

    Unknown/blank entries are dropped; when nothing usable is configured
    the deterministic default order (gemini, groq, openrouter) applies.
    """
    raw = os.environ.get("LLM_FALLBACK_PROVIDERS")
    if not raw or not str(raw).strip():
        return list(DEFAULT_FALLBACK_ORDER)
    out = []
    for part in str(raw).split(","):
        p = part.strip().lower()
        if p in HOSTED_PROVIDERS and p not in out:
            out.append(p)
    return out or list(DEFAULT_FALLBACK_ORDER)


def allow_heuristic_fallback():
    v = (os.environ.get("LLM_ALLOW_HEURISTIC_FALLBACK") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def provider_is_configured(name):
    env = KEY_ENV.get((name or "").lower())
    return bool(env and os.environ.get(env))


def configured_hosted_providers():
    """Configured hosted providers in fallback order (no heuristic)."""
    return [p for p in parse_fallback_order() if provider_is_configured(p)]


def primary_hosted_provider():
    chain = configured_hosted_providers()
    return chain[0] if chain else ""


def fallback_chain_labels(mode=None):
    """Human-readable fallback chain for the UI (no secrets)."""
    return [PROVIDER_LABELS.get(n, n) for n in resolve_chain_names(mode)]


def configured_provider_status():
    """Safe config report: which providers have keys, no secrets."""
    prov = provider_default()
    return {
        "mode": prov,
        "default_provider": prov,
        "default_model": model_for(prov) if prov in HOSTED_PROVIDERS else "",
        "fallback_order": parse_fallback_order(),
        "fallback_chain": resolve_chain_names(prov)
        if prov == "auto" else [prov],
        "fallback_chain_labels": fallback_chain_labels(prov),
        "heuristic_fallback_enabled": allow_heuristic_fallback(),
        "gemini": {"configured": bool(os.environ.get("GEMINI_API_KEY")),
                   "model": os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL},
        "groq": {"configured": bool(os.environ.get("GROQ_API_KEY")),
                 "model": os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL},
        "openrouter": {"configured": bool(os.environ.get("OPENROUTER_API_KEY")),
                       "model": os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL},
        "cerebras": {"configured": bool(os.environ.get("CEREBRAS_API_KEY")),
                     "model": os.environ.get("CEREBRAS_MODEL") or DEFAULT_CEREBRAS_MODEL},
        "timeout_s": llm_timeout(),
    }


def _redact_secrets(text):
    """Redact anything key-like plus the actual configured key values."""
    import re as _re
    s = str(text or "")
    # Never let a real configured key value appear in logs/errors.
    for env in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                "CEREBRAS_API_KEY", "XBO_AI_KEY"):
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


def _safe_error(exc, provider="gemini"):
    """Map provider failures to safe, user-facing messages (no keys)."""
    import re as _re
    model_env = MODEL_ENV.get((provider or "gemini").lower(), "GEMINI_MODEL")
    default_model = DEFAULT_MODEL.get(
        (provider or "gemini").lower(), DEFAULT_GEMINI_MODEL)
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
            base = ("model unavailable (HTTP %d). Check %s "
                    "(current default '%s') and the "
                    "provider dashboard" % (code, model_env, default_model))
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
                    "%s (default '%s')" % (model_env, default_model))
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
                err_text = _safe_error(exc, provider)[:500]
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
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32000,
                             "responseMimeType": "application/json"},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": key,
               "User-Agent": "x-bookmark-organizer/1.0 (+https://x-bookmark-organizer.onrender.com)"}
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


def _mentions_response_format(exc, limit=500):
    """True when a provider 400 is specifically about JSON response_format.

    Used to downgrade one step (structured JSON -> plain JSON instruction)
    for models that do not support ``response_format``; this is a
    capability adjustment, not an uncontrolled retry.
    """
    try:
        body = _read_http_body(exc, limit=limit).lower()
    except Exception:
        body = ""
    return ("response_format" in body or "json_object" in body
            or "json mode" in body or "structured output" in body)


def _openai_compat_call(base, key, model, messages, timeout,
                        provider="openai", extra_headers=None,
                        json_mode=True):
    """OpenAI-compatible chat completions (Groq / OpenRouter).

    Requests structured JSON via ``response_format`` when supported. If
    the selected model rejects that field (400/403 naming response_format /
    json_object), it is dropped once and the plain prompt instruction is
    used — never an unbounded retry loop. One controlled retry for
    transient 429/500/502/503/504/timeouts.
    """
    if not key:
        raise RuntimeError("API key not configured")
    payload = {"model": model, "messages": messages, "temperature": 0.2,
               "max_tokens": 4000}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json",
               "Authorization": "Bearer " + key,
               "User-Agent": "x-bookmark-organizer/1.0 (+https://x-bookmark-organizer.onrender.com)"}
    for k, v in (extra_headers or {}).items():
        if v:
            headers[k] = v
    last = None
    retries = 0
    response_format_supported = bool(json_mode)
    for _attempt in range(2):  # 1 initial + 1 controlled retry max
        try:
            try:
                data = _post_json(base.rstrip("/") + "/chat/completions",
                                  payload, headers, timeout)
            except ValueError as ve:
                raise ValueError(
                    "provider returned malformed JSON (%s)"
                    % _redact_secrets(str(ve))[:120])
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, AttributeError):
                raise ValueError(
                    "provider returned malformed JSON (bad chat envelope)")
            if not str(text or "").strip():
                raise ValueError(
                    "provider returned malformed JSON (empty chat content)")
            try:
                LAST_DIAGNOSTICS.clear()
                LAST_DIAGNOSTICS.update(
                    {"provider": provider, "model": model,
                     "http_status": 200, "error": "", "retries": retries})
            except Exception:
                pass
            return text
        except urllib.error.HTTPError as e:
            # Capability downgrade: model cannot do response_format.
            if (response_format_supported and e.code in (400, 403)
                    and _mentions_response_format(e)):
                response_format_supported = False
                payload.pop("response_format", None)
                retries = 1
                continue
            if e.code in RETRYABLE_STATUS and _attempt == 0:
                last = e
                retries = 1
                time.sleep(RETRY_BACKOFF_S)
                continue
            try:
                describe_error(provider, model, e, retries)
            except Exception:
                pass
            try:
                e._llm_retries = retries  # type: ignore[attr-defined]
            except Exception:
                pass
            raise
        except (TimeoutError, OSError) as e:
            if _attempt == 0:
                last = e
                retries = 1
                time.sleep(RETRY_BACKOFF_S)
                continue
            try:
                describe_error(provider, model, e, retries)
            except Exception:
                pass
            try:
                e._llm_retries = retries  # type: ignore[attr-defined]
            except Exception:
                pass
            raise
    try:
        describe_error(provider, model, last, retries)
    except Exception:
        pass
    raise last


def _openrouter_headers():
    """OpenRouter attribution headers from env (never secrets)."""
    extra = {}
    site = (os.environ.get("OPENROUTER_SITE_URL") or "").strip()
    app_name = (os.environ.get("OPENROUTER_APP_NAME") or "").strip()
    if site:
        extra["HTTP-Referer"] = site
    if app_name:
        extra["X-Title"] = app_name
    return extra


_MODEL_LIST_ENDPOINTS = {
    "groq": "https://api.groq.com/openai/v1/models",
    "cerebras": "https://api.cerebras.ai/v1/models",
}

_MODEL_LIST_KEYS = {
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
}

_USER_AGENT = ("x-bookmark-organizer/1.0 "
               "(+https://x-bookmark-organizer.onrender.com)")


def list_models(provider):
    """Return a list of model ID strings for the given provider.

    Read-only diagnostic helper.  Never returns raw responses, headers,
    or API keys — only a flat list of model ID strings.  Errors are
    surfaced via ``safe_error`` (no secrets leaked).
    """
    provider = (provider or "").lower()
    if provider not in _MODEL_LIST_ENDPOINTS:
        return None, "unsupported provider: %s" % provider
    key = os.environ.get(_MODEL_LIST_KEYS[provider]) or ""
    if not key:
        return None, "%s API key not configured" % PROVIDER_LABELS.get(
            provider, provider)
    url = _MODEL_LIST_ENDPOINTS[provider]
    headers = {
        "Authorization": "Bearer " + key,
        "User-Agent": _USER_AGENT,
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=llm_timeout()) as resp:
            body = json.load(resp)
        models = [m["id"] for m in (body.get("data") or [])
                  if isinstance(m, dict) and m.get("id")]
        return models, None
    except Exception as exc:
        return None, _safe_error(exc, provider)


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
                                   model or model_for("groq"), messages, timeout,
                                   provider="groq")
    if provider == "openrouter":
        return _openai_compat_call("https://openrouter.ai/api/v1",
                                   os.environ.get("OPENROUTER_API_KEY") or "",
                                   model or model_for("openrouter"), messages,
                                   timeout, provider="openrouter",
                                   extra_headers=_openrouter_headers())
    if provider == "cerebras":
        return _openai_compat_call("https://api.cerebras.ai/v1",
                                   os.environ.get("CEREBRAS_API_KEY") or "",
                                   model or model_for("cerebras"), messages,
                                   timeout, provider="cerebras")
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
    """Return (primary, secondary_or_None).

    Kept for the legacy classification path. Explicit providers get an
    optional second hosted provider only when its key is configured;
    ``auto`` resolves to the first configured hosted provider in the
    fallback order.
    """
    requested = (requested or provider_default()).lower()
    if requested == "ai":  # legacy alias → default hosted
        requested = provider_default()
    if requested == "auto":
        hosted = [c for c in resolve_chain_names("auto")
                  if c in HOSTED_PROVIDERS]
        requested = hosted[0] if hosted else "heuristic"
    if requested not in ("gemini", "groq", "openrouter", "cerebras", "heuristic"):
        requested = "gemini"
    if requested == "heuristic":
        return "heuristic", None
    # optional second hosted fallback only when its key configured
    secondary = None
    for cand, env in (("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
                       ("openrouter", "OPENROUTER_API_KEY"),
                       ("cerebras", "CEREBRAS_API_KEY")):
        if cand != requested and os.environ.get(env):
            secondary = cand
            break
    return requested, secondary


# ===================================================================== #
# Provider adapter interface.                                            #
#                                                                        #
# Each adapter owns its provider's configuration facts and the one      #
# call primitive the discovery layer needs (structured JSON).           #
# categories.py never constructs URLs, headers, or provider payloads.    #
# ===================================================================== #

class ProviderError(RuntimeError):
    """Safe provider failure carrying no secrets."""

    def __init__(self, message, provider=None, model=None,
                 kind="fallback_transient", http_status=None):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.kind = kind
        self.http_status = http_status


class Provider:
    name = ""
    key_env = ""
    model_env = ""
    default_model = ""

    def __init__(self, model=None, timeout=None):
        self.model = model or model_for(self.name)
        self.timeout = timeout or llm_timeout()

    def is_configured(self):
        if not self.key_env:
            return True
        return bool(os.environ.get(self.key_env))

    def configured_model(self):
        return model_for(self.name)

    def generate_text(self, messages, model=None, timeout=None):
        return chat_text(self.name, messages, model=model or self.model,
                         timeout=timeout or self.timeout)

    def generate_structured_json(self, messages, model=None, timeout=None):
        return chat_json(self.name, messages, model=model or self.model,
                         timeout=timeout or self.timeout)

    def describe_error(self, exc, retries=0):
        return describe_error(self.name, self.model, exc, retries)

    def safe_error(self, exc):
        return _safe_error(exc, self.name)


class GeminiProvider(Provider):
    name = "gemini"
    key_env = "GEMINI_API_KEY"
    model_env = "GEMINI_MODEL"
    default_model = DEFAULT_GEMINI_MODEL


class GroqProvider(Provider):
    name = "groq"
    key_env = "GROQ_API_KEY"
    model_env = "GROQ_MODEL"
    default_model = DEFAULT_GROQ_MODEL


class OpenRouterProvider(Provider):
    name = "openrouter"
    key_env = "OPENROUTER_API_KEY"
    model_env = "OPENROUTER_MODEL"
    default_model = DEFAULT_OPENROUTER_MODEL


class CerebrasProvider(Provider):
    name = "cerebras"
    key_env = "CEREBRAS_API_KEY"
    model_env = "CEREBRAS_MODEL"
    default_model = DEFAULT_CEREBRAS_MODEL


class HeuristicProvider(Provider):
    name = "heuristic"
    key_env = ""
    default_model = ""

    def is_configured(self):
        return True

    def generate_structured_json(self, messages, model=None, timeout=None):
        raise ProviderError(
            "heuristic provider is offline (no hosted model)",
            provider=self.name, kind="not_supported")


_PROVIDER_CLASSES = {
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
    "cerebras": CerebrasProvider,
    "heuristic": HeuristicProvider,
}


def get_provider(name, model=None, timeout=None):
    """Factory for a provider adapter by name."""
    cls = _PROVIDER_CLASSES.get((name or "").lower())
    if cls is None:
        raise ValueError("unknown provider: %s" % (name,))
    return cls(model=model, timeout=timeout)


def resolve_chain(mode=None, model=None, timeout=None):
    """Ordered provider adapters to attempt for a run.

    - explicit gemini/groq/openrouter -> exactly that provider (no
      cross-provider fallback; callers asked for it by name)
    - heuristic -> the offline provider only
    - auto -> configured hosted providers in LLM_FALLBACK_PROVIDERS
      order, then heuristic ONLY when LLM_ALLOW_HEURISTIC_FALLBACK is
      explicitly enabled. Providers without a key are never included.
    """
    m = normalize_mode(mode)
    if m == "heuristic":
        return [get_provider("heuristic", timeout=timeout)]
    if m in HOSTED_PROVIDERS:
        return [get_provider(m, model=model, timeout=timeout)]
    chain = []
    for p in parse_fallback_order():
        if provider_is_configured(p):
            chain.append(get_provider(p, model=None, timeout=timeout))
    if allow_heuristic_fallback():
        chain.append(get_provider("heuristic", timeout=timeout))
    return chain


def resolve_chain_names(mode=None):
    return [p.name for p in resolve_chain(mode)]


# ===================================================================== #
# Small, safe connection test (never sends the bookmark collection).     #
# ===================================================================== #

CONNECTION_TEST_MESSAGES = [
    {"role": "system",
     "content": "You are a connectivity test. Reply with JSON only."},
    {"role": "user", "content": 'Reply ONLY {"ok": true}. No other text.'},
]


def test_provider_connection(provider_name, model=None, timeout=None):
    """Test one provider with a tiny harmless prompt. Safe dict only."""
    prov = get_provider(provider_name, model=model, timeout=timeout)
    label = PROVIDER_LABELS.get(prov.name, prov.name)
    base = {"provider": prov.name, "provider_label": label,
            "model": model or prov.configured_model() or prov.model}
    if not prov.is_configured():
        base.update({"success": False, "configured": False,
                     "http_status": None,
                     "error": "%s is not configured. Add the %s API key."
                     % (label, label)})
        return base
    if prov.name == "heuristic":
        base.update({"success": True, "configured": True,
                     "http_status": None, "error": "",
                     "duration_ms": 0, "offline": True})
        return base
    t0 = time.time()
    try:
        txt = prov.generate_text(
            CONNECTION_TEST_MESSAGES, timeout=min(timeout or 20, 20))
        data = parse_json_array_or_obj(txt)
        ok = bool(data.get("ok")) if isinstance(data, dict) else False
        base.update({"success": ok, "configured": True, "http_status": 200,
                     "error": "" if ok else "unexpected reply",
                     "duration_ms": int((time.time() - t0) * 1000)})
        return base
    except Exception as e:
        status = getattr(e, "code", None) if isinstance(
            e, urllib.error.HTTPError) else None
        base.update({"success": False, "configured": True,
                     "http_status": status,
                     "error": _safe_error(e, prov.name),
                     "duration_ms": int((time.time() - t0) * 1000)})
        return base


def test_connection(mode=None, model=None):
    """Test the selected mode's chain in order with a tiny prompt.

    Auto mode reports each attempted provider and stops at the first
    success, e.g. Gemini -> 503, Groq -> OK, OpenRouter -> not attempted.
    """
    m = normalize_mode(mode)
    result = {"mode": m, "fallback_chain": fallback_chain_labels(m),
              "attempts": [], "success": False, "provider": None}
    if m == "heuristic":
        r = test_provider_connection("heuristic")
        result["attempts"] = [r]
        result["success"] = True
        result["provider"] = "heuristic"
        return result
    chain = resolve_chain(m, model=model)

    def _not_attempted(p):
        label = PROVIDER_LABELS.get(p.name, p.name)
        return {"provider": p.name, "provider_label": label,
                "model": p.configured_model() or p.model,
                "configured": p.is_configured(), "success": False,
                "attempted": False, "http_status": None, "error": "",
                "duration_ms": 0,
                "skipped_reason": "not attempted (earlier provider "
                                  "succeeded)"}

    for idx, prov in enumerate(chain):
        if prov.name == "heuristic":
            r = test_provider_connection("heuristic")
            r["attempted"] = True
            result["attempts"].append(r)
            result["success"] = True
            result["provider"] = "heuristic"
            break
        r = test_provider_connection(prov.name, model=model, timeout=20)
        r["attempted"] = True
        result["attempts"].append(r)
        if r.get("success"):
            result["success"] = True
            result["provider"] = prov.name
            for later in chain[idx + 1:]:
                result["attempts"].append(_not_attempted(later))
            break
    return result

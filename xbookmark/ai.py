"""FREE AI bridge: OpenCode providers -> OpenAI-compatible chat.

Priority:
  XBO_AI_BACKEND / XBO_AI_BASE / XBO_AI_KEY / XBO_AI_MODEL env
  -> %USERPROFILE%/.config/opencode/config.json providers (first with
     baseURL+apiKey+models, e.g. your apinex free models, agentrouter,
     gorouter, openrouter)
  -> Ollama http://localhost:11434 (model llama3.1)
  -> None (caller falls back to offline heuristic)

Only sends truncated text snippets, never the whole DB.
"""
from __future__ import annotations
import json
import os
import urllib.request

TIMEOUT = int(os.environ.get("XBO_AI_TIMEOUT", "30"))
# Hard cap so a hung free endpoint can never freeze Classify All / Discover.
TIMEOUT = max(5, min(TIMEOUT, 60))


def _opencode_config_providers():
    try:
        cfg_path = os.path.join(os.path.expanduser("~"), ".config",
                                "opencode", "config.json")
        if not os.path.exists(cfg_path):
            return {}
        with open(cfg_path, encoding="utf-8") as fh:
            return json.load(fh).get("provider", {}) or {}
    except Exception:
        return {}


def resolve_endpoint(explicit_model: str | None = None) -> dict | None:
    if os.environ.get("XBO_AI_BACKEND", "").lower() == "ollama":
        return {"kind": "ollama",
                "base": os.environ.get("XBO_AI_BASE",
                                       "http://localhost:11434"),
                "model": (explicit_model or os.environ.get(
                    "XBO_AI_MODEL") or "llama3.1"),
                "key": ""}
    base = os.environ.get("XBO_AI_BASE")
    key = os.environ.get("XBO_AI_KEY")
    model = explicit_model or os.environ.get("XBO_AI_MODEL")
    if base and model:
        return {"kind": "openai", "base": base.rstrip("/"),
                "model": model, "key": key or ""}
    for _name, prov in (_opencode_config_providers().items()):
        opts = (prov or {}).get("options", {}) or {}
        models = (prov or {}).get("models", {}) or {}
        b, k = opts.get("baseURL"), opts.get("apiKey")
        if b and k and models:
            first = next(iter(models.keys()))
            short = first.split("/")[-1]
            return {"kind": "openai", "base": b.rstrip("/"),
                    "model": first if "/" in first else short,
                    "key": k, "provider": _name}
    # Ollama probe (fast fail)
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=3) as r:
            if r.status == 200:
                return {"kind": "ollama",
                        "base": "http://localhost:11434",
                        "model": explicit_model or os.environ.get(
                            "XBO_AI_MODEL") or "llama3.1", "key": ""}
    except Exception:
        pass
    return None


def status() -> dict:
    ep = resolve_endpoint()
    if not ep:
        return {"available": False,
                "hint": "set XBO_AI_BASE/XBO_AI_KEY/XBO_AI_MODEL, run "
                        "'ollama serve', or add a provider to "
                        "~/.config/opencode/config.json"}
    # never leak full key
    ep = dict(ep)
    if ep.get("key"):
        ep["key"] = ep["key"][:4] + "..." + ep["key"][-2:]
    return {"available": True, "endpoint": ep}


def _post_openai(base: str, key: str, model: str, messages,
                 max_tokens: int = 2000) -> str:
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": 0.2,
                       "max_tokens": max_tokens}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError("unexpected chat response: %s" % str(data)[:400]) \
            from e


def _post_ollama(base: str, model: str, messages,
                 max_tokens: int = 2000) -> str:
    url = base.rstrip("/") + "/api/chat"
    body = json.dumps({"model": model, "messages": messages,
                       "options": {"temperature": 0.2,
                                   "num_predict": max_tokens},
                       "stream": False}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    msg = (data.get("message") or {}).get("content", "")
    if not msg:
        raise RuntimeError("empty ollama response")
    return msg


def chat(messages, max_tokens: int = 2000,
         model: str | None = None) -> str:
    ep = resolve_endpoint(model)
    if not ep:
        raise RuntimeError("no AI endpoint configured (see ai-status)")
    if ep["kind"] == "ollama":
        return _post_ollama(ep["base"], ep["model"], messages,
                            max_tokens=max_tokens)
    return _post_openai(ep["base"], ep.get("key", ""), ep["model"],
                        messages, max_tokens=max_tokens)


def chat_json(messages, max_tokens: int = 3000,
              model: str | None = None):
    """Chat + strip fencing + parse JSON (object or array)."""
    text = chat(messages, max_tokens=max_tokens, model=model)
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0] or [0])
    end_candidates = [t.rfind("}"), t.rfind("]")]
    end = max([e for e in end_candidates if e >= 0] or [len(t) - 1]) + 1
    return json.loads(t[start:end])

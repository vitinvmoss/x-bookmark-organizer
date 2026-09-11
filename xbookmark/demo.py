"""Demo data generator (synthetic bookmarks, offline testing)."""
from __future__ import annotations
import random
from . import collect, store

TOPICS = [
    ("LLM prompting tricks", "ai", "openai.com"),
    ("Bitcoin halving analysis", "crypto", "coinbase.com"),
    ("Rust async patterns", "coding", "github.com"),
    ("Pitch deck teardown", "startups", "ycombinator.com"),
    ("Figma auto-layout guide", "design", "figma.com"),
    ("Zone 2 cardio protocol", "health", "youtube.com"),
]


def _entry(tid, i, title, tag, dom):
    text = "%s #%s demo %d https://%s/x/%d" % (title, tag, i, dom, i)
    result = {"__typename": "Tweet", "rest_id": tid}
    result["legacy"] = {"id_str": tid, "full_text": text,
                        "created_at": "Mon May 12 09:00:00 +0000 2026",
                        "entities": {"hashtags": [{"text": tag}],
                                     "urls": [{"url": "https://t.co/x",
                                               "expanded_url": "https://%s/x/%d" % (dom, i),
                                               "display_url": dom}],
                                     "user_mentions": []},
                        "favorite_count": i, "retweet_count": 0}
    result["core"] = {"user_results": {"result": {
        "rest_id": "u1",
        "legacy": {"screen_name": "demo_user%d" % (i % 25),
                   "name": "Demo User"}}}}
    result["views"] = {"count": i}
    entry = {"entryId": "tweet-" + tid, "sortIndex": str(i)}
    entry["content"] = {"itemContent": {"tweet_results": {"result": result}}}
    return entry


def make_envelopes(n=200, seed=7):
    out = []
    for i in range(n):
        title, tag, dom = TOPICS[i % len(TOPICS)]
        tid = str(1900000000000000000 + i)
        entry = _entry(tid, i, title, tag, dom)
        out.append({"status_id": tid, "entry_id": "tweet-" + tid,
                    "sort_index": str(i),
                    "captured_at": "2026-09-10T00:00:00Z",
                    "page": i // 50, "cursor": "", "raw": entry})
    return out


def run(kb, n=200, seed=7):
    store.ensure_kb(kb)
    envs = make_envelopes(n, seed)
    stats = collect.import_envelopes(kb, envs, source="demo")
    from . import normalize as NZ
    nstats = NZ.normalize_all(kb)
    return {"import": stats, "normalize": nstats}

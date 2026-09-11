"""Shared constants + stdlib helpers. Windows-safe (no POSIX-only APIs)."""
from __future__ import annotations
import json, os, random, re, tempfile, time

BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")

OPERATIONS = ["Bookmarks", "BookmarkFoldersSlice", "BookmarkFolderTimeline",
              "createBookmarkFolder", "bookmarkTweetToFolder",
              "RemoveTweetFromBookmarkFolder", "EditBookmarkFolder"]

FALLBACK_FEATURES = {
    "graphql_timeline_v2_bookmark_timeline": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

BASE_DELAY_S = 2.75
MAX_RETRIES = 5
MAX_CONSECUTIVE_EMPTY = 5

def utcnow_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def ensure_dir(p: str) -> str:
    os.makedirs(p, exist_ok=True)
    return p

def atomic_write_text(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)),
                               prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def read_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

def write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

def append_jsonl(path: str, obj) -> None:
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")

def iter_jsonl(path: str):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)

_slug_re = re.compile(r"[^a-z0-9]+")
def slugify(name: str) -> str:
    s = (name or "").strip().lower().replace("&", " and ")
    s = _slug_re.sub("-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "category"

def jittered_sleep(base: float = BASE_DELAY_S, spread: float = 0.5,
                   seed=None, sleep_fn=time.sleep) -> float:
    rng = random.Random(seed) if seed is not None else random
    d = rng.uniform(base - spread / 2.0, base + spread / 2.0)
    sleep_fn(max(0.2, d))
    return d

def backoff_delay(retry: int, base: float = 2.0, cap: float = 120.0, seed=None) -> float:
    rng = random.Random(f"{seed}-{retry}") if seed is not None else random
    return rng.uniform(0, min(cap, base * (2 ** retry)))

"""Normalize raw envelopes -> bookmarks.json (Siftly parser.ts, ported)."""
from __future__ import annotations
import hashlib
import re
import urllib.parse
from . import parser as P
from . import store, util

_WORD = re.compile(r"[a-z0-9][a-z0-9-]{2,}")


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
        return host.lower().lstrip("www.")[4:] if host.lower().startswith("www.") else host.lower()
    except Exception:
        return ""


def content_hash(text: str, author: str) -> str:
    h = hashlib.sha256()
    h.update((author or "").lower().encode("utf-8", "ignore"))
    h.update(b"\x00")
    h.update((" ".join((text or "").split())).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def normalize_envelope(env: dict):
    raw = env.get("raw") or {}
    # xarchive shim entries carry display fields under raw.xarchive
    if isinstance(raw, dict) and isinstance(raw.get("xarchive"), dict):
        xa = raw["xarchive"]
        tid = str(env.get("status_id") or "")
        text = str(xa.get("full_text") or "")
        author = xa.get("author") or {}
        urls = [{"short": None, "expanded": u, "display": None}
                for u in (xa.get("urls") or [])] if isinstance(
                    xa.get("urls"), list) else []
        tags = [str(x).lower() for x in (xa.get("hashtags") or [])]
        return {"tweet_id": tid, "status": xa.get("status") or "available",
                "text": text, "created_at": xa.get("created_at"),
                "author_handle": str(author.get("screen_name") or "unknown"),
                "author_name": str(author.get("name") or "Unknown"),
                "hashtags": tags, "mentions": [], "urls": urls,
                "domains": sorted({_domain(u.get("expanded") or "")
                                   for u in urls if u.get("expanded")} - {""}),
                "media": xa.get("media") or [],
                "has_media": bool(xa.get("media")),
                "existing_folders": xa.get("folders") or [],
                "existing_folder_ids": xa.get("folder_ids") or []}
    entry = raw if isinstance(raw, dict) and raw.get("entryId") else None
    if entry is None:
        return None
    if str(entry.get("entryId", "")).startswith("cursor-"):
        return None
    tweet = P.extract_tweet(entry)
    if not tweet:
        return None
    text = tweet.get("full_text") or ""
    author = tweet.get("author") or {}
    handle = str(author.get("screen_name") or "unknown")
    urls = tweet.get("urls") or []
    domains = sorted({_domain(u.get("expanded") or "") for u in urls
                      if u.get("expanded")} - {""})
    media = tweet.get("media") or []
    mentions = [str(m.get("screen_name") or "").lower()
                for m in (tweet.get("mentions") or [])
                if m.get("screen_name")]
    return {"tweet_id": str(tweet.get("tweet_id")),
            "status": tweet.get("status", "available"),
            "text": text, "created_at": tweet.get("created_at"),
            "author_handle": handle,
            "author_name": str(author.get("name") or "Unknown"),
            "hashtags": tweet.get("hashtags") or [], "mentions": mentions,
            "urls": urls, "domains": domains, "media": media,
            "has_media": bool(media),
            "quoted_text": ((tweet.get("quoted") or {}).get("full_text") or ""),
            "existing_folders": [], "existing_folder_ids": []}


def tokenize(text: str):
    return _WORD.findall((text or "").lower())


def normalize_all(kb: str) -> dict:
    store.ensure_kb(kb)
    bookmarks = store.load_bookmarks(kb)
    n_new = n_upd = n_skip = 0
    for env in util.iter_jsonl(store.kb_path(kb, "raw.jsonl")):
        if not env.get("status_id"):
            continue
        norm = normalize_envelope(env)
        if not norm or not norm.get("tweet_id"):
            continue
        tid = str(norm["tweet_id"])
        norm["content_hash"] = content_hash(norm.get("text", ""),
                                            norm.get("author_handle", ""))
        prev = bookmarks.get(tid)
        if prev is None:
            norm["first_seen_at"] = util.utcnow_iso()
            norm["last_seen_at"] = norm["first_seen_at"]
            bookmarks[tid] = norm
            n_new += 1
        elif prev.get("content_hash") != norm["content_hash"]:
            norm["first_seen_at"] = prev.get("first_seen_at")
            norm["last_seen_at"] = util.utcnow_iso()
            # preserve folder knowledge
            norm["existing_folders"] = prev.get("existing_folders") or norm.get(
                "existing_folders")
            norm["existing_folder_ids"] = prev.get("existing_folder_ids") or []
            bookmarks[tid] = norm
            n_upd += 1
        else:
            n_skip += 1
    store.save_bookmarks(kb, bookmarks)
    store.log_change(kb, {"kind": "normalize", "new": n_new,
                          "updated": n_upd, "unchanged": n_skip,
                          "total": len(bookmarks)})
    return {"new": n_new, "updated": n_upd, "unchanged": n_skip,
            "total": len(bookmarks)}

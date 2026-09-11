"""Single-tweet extraction (xarchive parser.js extractTweet, ported)."""
from __future__ import annotations
from typing import Any, Dict, Optional


def unwrap_result(result: Dict[str, Any]) -> Dict[str, Any]:
    if (isinstance(result, dict)
            and result.get("__typename") == "TweetWithVisibilityResults"
            and isinstance(result.get("tweet"), dict)):
        return result["tweet"]
    return result


def _unavailable(tweet_id: str, sort_index, reason: str):
    return {"tweet_id": tweet_id, "sort_index": sort_index,
            "status": "unavailable", "unavailable_reason": reason,
            "folders": []}


def extract_tweet(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    content = (entry or {}).get("content")
    item = content.get("itemContent") if isinstance(content, dict) else None
    if not isinstance(item, dict):
        return None
    sort_index = entry.get("sortIndex")
    tw_results = item.get("tweet_results")
    result = tw_results.get("result") if isinstance(tw_results, dict) else None
    tweet_id = str((entry.get("entryId") or "").replace("tweet-", ""))
    if not result:
        return _unavailable(tweet_id, sort_index, "No tweet data returned")
    if result.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
        text = ""
        try:
            text = result.get("tombstone", {}).get("text", {}).get("text", "")
        except Exception:
            text = ""
        return _unavailable(tweet_id, sort_index,
                            text or result.get("__typename"))
    tw = unwrap_result(result)
    legacy = tw.get("legacy", {}) or {}
    core = tw.get("core", {}).get("user_results", {}).get("result", {}) or {}
    user_legacy = core.get("legacy", {}) or {}
    note = ((tw.get("note_tweet") or {}).get("note_tweet_results") or {}).get(
        "result", {}) or {}
    full_text = note.get("text") or legacy.get("full_text") or ""
    entities = legacy.get("entities", {}) or {}
    urls = [{"short": u.get("url"), "expanded": u.get("expanded_url"),
             "display": u.get("display_url")}
            for u in (entities.get("urls", []) or [])]
    tags = [str(h.get("text", "")).lower()
            for h in (entities.get("hashtags", []) or []) if h.get("text")]
    mentions = [{"screen_name": m.get("screen_name"), "id": m.get("id_str")}
                for m in (entities.get("user_mentions", []) or [])]
    media = []
    msrc = (legacy.get("extended_entities") or legacy.get("entities") or {})
    for m in (msrc.get("media") or []):
        vids = sorted(
            [{"bitrate": v.get("bitrate", 0), "url": v.get("url")}
             for v in ((m.get("video_info") or {}).get("variants") or [])
             if v.get("content_type") == "video/mp4"],
            key=lambda v: v["bitrate"], reverse=True)
        media.append({"type": m.get("type"), "url": m.get("media_url_https"),
                      "alt": m.get("ext_alt_text"), "video_variants": vids})
    quoted = None
    if isinstance(tw.get("quoted_status_result"), dict):
        q = unwrap_result(tw["quoted_status_result"].get("result") or {})
        if q.get("__typename") in ("TweetTombstone", "TweetUnavailable"):
            quoted = {"status": "unavailable"}
        elif q.get("rest_id") or q.get("legacy"):
            ql = q.get("legacy", {}) or {}
            qc = q.get("core", {}).get("user_results", {}).get(
                "result", {}) or {}
            qul = qc.get("legacy", {}) or {}
            qn = ((q.get("note_tweet") or {}).get("note_tweet_results")
                  or {}).get("result", {}) or {}
            quoted = {"tweet_id": q.get("rest_id") or ql.get("id_str"),
                      "full_text": qn.get("text") or ql.get("full_text"),
                      "author": {"screen_name": qul.get("screen_name"),
                                 "name": qul.get("name")}}
    views = tw.get("views") or {}
    return {
        "tweet_id": tw.get("rest_id") or legacy.get("id_str") or tweet_id,
        "sort_index": sort_index, "status": "available",
        "full_text": full_text, "created_at": legacy.get("created_at"),
        "conversation_id": legacy.get("conversation_id_str"),
        "in_reply_to": legacy.get("in_reply_to_status_id_str"),
        "author": {"screen_name": user_legacy.get("screen_name"),
                   "name": user_legacy.get("name"),
                   "id": core.get("rest_id")},
        "hashtags": tags, "mentions": mentions, "urls": urls,
        "media": media, "quoted": quoted,
        "metrics": {"likes": legacy.get("favorite_count"),
                    "retweets": legacy.get("retweet_count"),
                    "replies": legacy.get("reply_count"),
                    "quotes": legacy.get("quote_count"),
                    "views": views.get("count")},
        "lang": legacy.get("lang"),
    }

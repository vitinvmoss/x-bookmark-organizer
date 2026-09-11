"""Page-level parsing: instructions + folder list."""
from __future__ import annotations
from typing import Any, Dict, List, Optional
from .tweet_extract import extract_tweet


def page_instructions(data: Dict[str, Any]) -> Optional[list]:
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(inner, dict):
        return None
    for key in ("bookmark_timeline_v2", "bookmark_timeline",
                "bookmark_collection_timeline"):
        tl = inner.get(key)
        if isinstance(tl, dict) and isinstance(tl.get("timeline"), dict):
            ins = tl["timeline"].get("instructions")
            if ins is not None:
                return ins
    return None


def parse_bookmarks_page(data: Dict[str, Any]):
    ins = page_instructions(data)
    if not isinstance(ins, list):
        return [], None, False
    tweets: List[Dict[str, Any]] = []
    bottom = None
    for instruction in ins:
        if not isinstance(instruction, dict):
            return [], None, False
        itype = instruction.get("type")
        if itype == "TimelineTerminateTimeline":
            continue
        if itype == "TimelineReplaceEntry":
            entries = [instruction.get("entry")]
        elif itype == "TimelineAddEntries":
            entries = instruction.get("entries")
        else:
            return [], None, False
        if not isinstance(entries, list):
            return [], None, False
        for entry in entries:
            if not isinstance(entry, dict):
                return [], None, False
            eid = entry.get("entryId")
            if not isinstance(eid, str):
                return [], None, False
            if eid.startswith("tweet-"):
                one = extract_tweet(entry)
                if not one:
                    return [], None, False
                tweets.append(one)
            elif eid.startswith("cursor-bottom-"):
                content = entry.get("content") or {}
                val = content.get("value")
                if not isinstance(val, str) or not val:
                    return [], None, False
                bottom = val
            elif eid.startswith("cursor-top-"):
                continue
            else:
                return [], None, False
    return tweets, bottom, True


def parse_folder_list(data: Dict[str, Any]):
    try:
        node = data["data"]["viewer"]["user_results"]["result"]
        sl = node["bookmark_collections_slice"]
        items = sl.get("items", [])
        folders = [{"id": it.get("id") or it.get("collection_id"),
                    "name": it.get("name")} for it in items]
        cursor = (sl.get("slice_info") or {}).get("next_cursor")
        valid = (cursor is None or isinstance(cursor, str)) and all(
            isinstance(f.get("id"), str) and f["id"]
            and isinstance(f.get("name"), str) for f in folders)
        return folders, cursor, valid
    except Exception:
        return [], None, False

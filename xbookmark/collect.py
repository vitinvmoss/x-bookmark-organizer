"""Import stage: userscript JSONL, xarchive JSON, raw GraphQL pages.

Dedupes by tweet_id. Never deletes. Appends to change log.
"""
from __future__ import annotations
import json
import os
from . import parser as P
from . import store, util


def _raw_envelope(status_id: str, entry_id: str, raw: dict,
                  page: int = 0, cursor: str = "") -> dict:
    return {"status_id": str(status_id), "entry_id": entry_id,
            "sort_index": "", "captured_at": util.utcnow_iso(),
            "page": page, "cursor": cursor, "raw": raw}


def load_userscript_fixture(path: str):
    """SaveBox collector.user.js shape: {status_id,entry_id,raw,...} per line,
    or raw GraphQL responses (one per line). Yields envelopes."""
    out = []
    text = open(path, encoding="utf-8").read().splitlines()
    for line in text:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and obj.get("status_id") and "raw" in obj:
            out.append(obj)
        elif isinstance(obj, dict) and "data" in obj:
            tweets, bottom, valid = P.parse_bookmarks_page(obj)
            if not valid:
                raise ValueError("unrecognized GraphQL page in fixture")
            for t in tweets:
                entry = {"entryId": "tweet-%s" % t["tweet_id"],
                         "content": {"itemContent": {"tweet_results": {
                             "result": t.get("_raw_result", {})}}}}
                out.append(_raw_envelope(t["tweet_id"], entry["entryId"], entry))
            if bottom:
                out.append(_raw_envelope("", "cursor-bottom-x",
                                         {"entryId": "cursor-bottom-x"}))
        else:
            raise ValueError("unknown fixture line shape: %s" % str(obj)[:120])
    return out


def load_xarchive_file(path: str):
    """xarchive cumulative/combined JSON -> envelopes (best effort)."""
    data = json.load(open(path, encoding="utf-8"))
    return load_xarchive_file_from_data(data)


def load_xarchive_file_from_data(data):
    """xarchive dict (already parsed) -> (envelopes, folders)."""
    marks = data.get("bookmarks") if isinstance(data, dict) else data
    if not isinstance(marks, list):
        raise ValueError("xarchive file has no bookmarks list")
    out = []
    for b in marks:
        tid = str(b.get("tweet_id") or b.get("id") or "")
        if not tid:
            continue
        raw_entry = {"entryId": "tweet-%s" % tid,
                     "content": {"itemContent": {"tweet_results": {
                         "result": b.get("raw_result") or {}}}},
                     "xarchive": {k: b.get(k) for k in
                                  ("full_text", "author", "created_at",
                                   "urls", "hashtags", "media", "folders",
                                   "folder_ids", "status") if k in b}}
        out.append(_raw_envelope(tid, "tweet-%s" % tid, raw_entry))
    folders = []
    for f in (data.get("folders") if isinstance(data, dict) else None) or []:
        if isinstance(f, dict) and f.get("id") and f.get("name"):
            folders.append({"id": str(f["id"]), "name": str(f["name"])})
    return out, folders


def import_envelopes(kb: str, envelopes, source: str = "fixture") -> dict:
    store.ensure_kb(kb)
    raw_path = store.kb_path(kb, "raw.jsonl")
    seen = set()
    for row in util.iter_jsonl(raw_path):
        if row.get("status_id"):
            seen.add(str(row["status_id"]))
    added = dups = cursors = 0
    with open(raw_path, "a", encoding="utf-8") as fh:
        for env in envelopes:
            sid = str(env.get("status_id") or "")
            if not sid:
                cursors += 1
                fh.write(json.dumps(env, ensure_ascii=False,
                                    sort_keys=True) + "\n")
                continue
            if sid in seen:
                dups += 1
                continue
            seen.add(sid)
            fh.write(json.dumps(env, ensure_ascii=False,
                                sort_keys=True) + "\n")
            added += 1
    store.log_change(kb, {"kind": "import", "source": source,
                          "added": added, "duplicates": dups,
                          "cursor_lines": cursors})
    return {"added": added, "duplicates": dups, "cursor_lines": cursors,
            "total_seen": len(seen)}

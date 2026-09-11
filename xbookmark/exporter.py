"""Export the library as JSON / CSV / Markdown, plus a ZIP full backup.

Everything is stdlib and in-memory: the HTTP layer (app.py) calls these
and streams the returned bytes as a download. Files are also mirrored
to data/<kb>/exports/ so users can grab them directly.
"""
from __future__ import annotations
import csv, io, json, os, zipfile
from . import store, util

CORE_FILES = ["bookmarks.json", "library.json", "categories.json",
              "category_structure.json", "feedback.jsonl",
              "classifications.jsonl", "analysis.json", "session.json",
              "change_log.jsonl"]


def _stamp():
    return util.utcnow_iso().replace(":", "").replace("-", "").split(".")[0]


def x_url(bookmark):
    handle = (bookmark.get("author_handle") or "").lstrip("@") or "i"
    return "https://x.com/%s/status/%s" % (handle, bookmark.get("tweet_id"))


def rows(kb, ids=None):
    """Joined view: normalized bookmark + library overlay, ordered by id."""
    lib = util.read_json(store.kb_path(kb, "library.json"), {}) or {}
    marks = lib.get("bookmarks") or {}
    books = store.load_bookmarks(kb)
    out = []
    for tid in sorted(books, key=lambda x: (len(x), x)):
        if ids is not None and tid not in ids:
            continue
        b = books[tid]
        ov = marks.get(tid) or {}
        media = b.get("media") or []
        thumb = ""
        if media and isinstance(media[0], dict):
            thumb = media[0].get("url") or ""
        out.append({
            "tweet_id": tid,
            "text": b.get("text") or "",
            "author_name": b.get("author_name") or "",
            "author_handle": (b.get("author_handle") or "").lstrip("@"),
            "created_at": b.get("created_at") or "",
            "url": x_url(b),
            "domains": ", ".join(b.get("domains") or []),
            "hashtags": ", ".join(b.get("hashtags") or []),
            "thumbnail": thumb,
            "categories": ov.get("categories") or [],
            "tags": ov.get("tags") or [],
            "intent": ov.get("intent") or "",
            "confidence": ov.get("confidence"),
            "reviewed": bool(ov.get("reviewed")),
            "status": b.get("status") or "available",
        })
    return out


def to_json(kb, ids=None):
    data = rows(kb, ids)
    return json.dumps({"exported_at": util.utcnow_iso(),
                       "count": len(data),
                       "bookmarks": data},
                      ensure_ascii=False, indent=2)


def to_csv(kb, ids=None):
    data = rows(kb, ids)
    buf = io.StringIO()
    cols = ["tweet_id", "created_at", "author_name", "author_handle",
            "text", "categories", "tags", "intent", "confidence",
            "reviewed", "domains", "hashtags", "url"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in data:
        r = dict(r)
        r["categories"] = " | ".join(r["categories"])
        r["tags"] = " | ".join(r["tags"])
        r["reviewed"] = "yes" if r["reviewed"] else "no"
        w.writerow(r)
    return buf.getvalue()


def to_markdown(kb, ids=None):
    data = rows(kb, ids)
    lines = ["# X Bookmarks export", "",
             "_Generated %s - %d bookmarks_" % (util.utcnow_iso(), len(data)),
             ""]
    groups = {}
    for r in data:
        key = " / ".join(r["categories"]) if r["categories"] else "Unsorted"
        groups.setdefault(key, []).append(r)
    for key in sorted(groups):
        lines += ["## %s (%d)" % (key, len(groups[key])), ""]
        for r in groups[key]:
            who = "@%s" % r["author_handle"] if r["author_handle"] else ""
            lines.append("- **%s** %s - %s" % (r["author_name"] or who,
                                               who, r["created_at"]))
            for ln in (r["text"] or "").splitlines():
                if ln.strip():
                    lines.append("  > %s" % ln.strip())
            lines.append("  [open on X](%s)" % r["url"])
            if r["intent"]:
                lines.append("  - intent: `%s`" % r["intent"])
            lines.append("")
    return "\n".join(lines)


def backup_zip(kb, arcdir=None):
    """ZIP of the core knowledge-base files (excludes raw.jsonl/exports)."""
    if not arcdir:
        arcdir = os.path.basename(os.path.normpath(kb)) or "kb"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in CORE_FILES:
            p = store.kb_path(kb, name)
            if os.path.exists(p):
                z.write(p, arcname="%s/%s" % (arcdir, name))
    return buf.getvalue()


def save_export(kb, fmt, ids=None):
    """Write an export file into data/<kb>/exports/ and return its path."""
    util.ensure_dir(store.kb_path(kb, "exports"))
    ext = {"json": "json", "csv": "csv", "md": "md"}[fmt]
    path = store.kb_path(kb, os.path.join(
        "exports", "bookmarks-%s.%s" % (_stamp(), ext)))
    if fmt == "json":
        text = to_json(kb, ids)
    elif fmt == "csv":
        text = to_csv(kb, ids)
    else:
        text = to_markdown(kb, ids)
    util.atomic_write_text(path, text)
    return path
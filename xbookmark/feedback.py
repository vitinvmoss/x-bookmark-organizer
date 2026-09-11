"""Manual-correction feedback log (feedback.jsonl).

Each record: {bookmark_id, old_categories, new_categories, old_intent,
new_intent, at, reason}. Written on every manual category/intent edit so
future AI classification can use relevant corrections as context.
No ML training - just prompt context.
"""
from __future__ import annotations
from . import store, util


def log_correction(kb, bookmark_id, old, new, reason=""):
    rec = {
        "bookmark_id": str(bookmark_id),
        "old_categories": list((old or {}).get("categories") or []),
        "new_categories": list((new or {}).get("categories") or []),
        "old_intent": (old or {}).get("intent"),
        "new_intent": (new or {}).get("intent"),
        "at": util.utcnow_iso(),
    }
    if reason:
        rec["reason"] = str(reason)[:300]
    util.append_jsonl(store.kb_path(kb, "feedback.jsonl"), rec)
    store.log_change(kb, {"kind": "feedback", "id": rec["bookmark_id"],
                          "old": rec["old_categories"],
                          "new": rec["new_categories"]})
    return rec


def load_all(kb, limit=500):
    rows = list(util.iter_jsonl(store.kb_path(kb, "feedback.jsonl")))
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def relevant(kb, categories, limit=8):
    """Corrections mentioning any of these categories (prompt context)."""
    names = {(c or "").strip().lower() for c in (categories or []) if c}
    out = []
    for r in load_all(kb):
        pool = {(c or "").strip().lower()
                for c in (r.get("old_categories") or []) +
                (r.get("new_categories") or [])}
        if names & pool:
            out.append(r)
    return out[-limit:] if limit else out

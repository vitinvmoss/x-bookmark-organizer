"""Dry-run proposal: folders + counts + low-confidence list. No X writes."""
from __future__ import annotations
import collections
from . import store, util


def build(kb, confidence_threshold=0.45) -> dict:
    store.ensure_kb(kb)
    bookmarks = store.load_bookmarks(kb)
    cats_doc = util.read_json(store.kb_path(kb, "categories.json"), None)
    if not cats_doc:
        raise SystemExit("run 'categories' first")
    counts: dict = collections.Counter()
    low = []
    mapping = {}
    for row in util.iter_jsonl(store.kb_path(kb, "classifications.jsonl")):
        mapping[str(row.get("tweet_id"))] = row
    for tid in bookmarks:
        row = mapping.get(tid)
        cat = (row or {}).get("category") or "Unsorted / Review"
        conf = float((row or {}).get("confidence") or 0)
        counts[cat] += 1
        if conf < confidence_threshold or not row:
            low.append({"tweet_id": tid, "category": cat,
                        "confidence": conf})
    cats = []
    for c in cats_doc["categories"]:
        cats.append({**c, "assigned_count": counts.get(c["name"], 0)})
    existing = util.read_json(store.kb_path(kb, "folders.json"), [])
    proposal = {
        "generated_at": util.utcnow_iso(), "dry_run": True,
        "total_bookmarks": len(bookmarks),
        "classified": len(mapping), "unclassified": len(bookmarks) - len(mapping),
        "proposed_folders": cats,
        "counts": dict(counts),
        "low_confidence_count": len(low),
        "low_confidence_sample": low[:50],
        "existing_folders": existing,
        "note": "REVIEW ONLY. Nothing was written to X. Run plan-writes "
                "then apply --approve to create folders.",
    }
    util.write_json(store.kb_path(kb, "proposal.json"), proposal)
    store.log_change(kb, {"kind": "proposal", "counts": dict(counts)})
    return proposal


def render_text(proposal: dict) -> str:
    lines = ["=== DRY-RUN PROPOSAL (nothing written to X) ===",
             "Total: %d | Classified: %d | Low-confidence: %d"
             % (proposal["total_bookmarks"], proposal["classified"],
                proposal["low_confidence_count"]), ""]
    lines.append("Proposed folders:")
    for c in proposal["proposed_folders"]:
        lines.append("  - %s : %d  (%s)" % (c["name"], c["assigned_count"],
                                            c.get("description", "")[:90]))
    if proposal.get("existing_folders"):
        lines.append("")
        lines.append("Existing X folders (preserved, ADD-ONLY):")
        for f in proposal["existing_folders"]:
            lines.append("  = %s (%s)" % (f.get("name"), f.get("id")))
    lines.append("")
    lines.append("Review data/proposal.json, then run plan-writes.")
    return "\n".join(lines)

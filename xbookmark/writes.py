"""Write planning + finalize stub (full apply in apply_phase.py)."""
from __future__ import annotations
from . import store, util


def plan(kb, preserve_existing=True) -> dict:
    store.ensure_kb(kb)
    proposal = util.read_json(store.kb_path(kb, "proposal.json"), None)
    if not proposal:
        raise SystemExit("run 'propose' first")
    existing = {f.get("name"): f for f in
                util.read_json(store.kb_path(kb, "folders.json"), [])}
    mapping = {}
    for row in util.iter_jsonl(store.kb_path(kb, "classifications.jsonl")):
        mapping[str(row.get("tweet_id"))] = row
    bookmarks = store.load_bookmarks(kb)
    ops = []
    for c in proposal["proposed_folders"]:
        name = c["name"]
        if name in existing and preserve_existing:
            ops.append({"op": "keep_folder", "name": name,
                        "folder_id": existing[name].get("id"),
                        "status": "done", "note": "preserved"})
        else:
            ops.append({"op": "create_folder", "name": name,
                        "description": c.get("description", ""),
                        "status": "pending"})
    for tid, b in bookmarks.items():
        row = mapping.get(tid, {})
        cat = row.get("category") or "Unsorted / Review"
        cur = set(b.get("existing_folder_ids") or [])
        ops.append({"op": "add_to_folder", "tweet_id": tid,
                    "category": cat, "from_folder_ids": sorted(cur),
                    "status": "pending"})
    payload = {"generated_at": util.utcnow_iso(),
               "preserve_existing": True, "ops": ops}
    payload["summary"] = {
        "create_folder": sum(1 for o in ops if o["op"] == "create_folder"),
        "add_to_folder": sum(1 for o in ops if o["op"] == "add_to_folder"),
        "keep_folder": sum(1 for o in ops if o["op"] == "keep_folder")}
    util.write_json(store.kb_path(kb, "writes.json"), payload)
    store.log_change(kb, {"kind": "plan-writes", **payload["summary"]})
    return payload

"""Apply phase: create folders -> add -> verify (ADD-ONLY default)."""
from __future__ import annotations
import time as _time
from . import discovery  # noqa: F401  (re-export for CLI query-ids)
from . import requests as R
from . import store, util


def _session_or_die(kb, need_ops):
    sess = store.load_session(kb)
    missing = [op for op in need_ops if not sess.get("queryId_" + op)]
    if missing:
        raise SystemExit("missing query IDs %s; run query-ids --discover "
                         "or --add <url>" % (missing,))
    if not sess.get("cookie_header") or not sess.get("ct0"):
        raise SystemExit("no cookie_header/ct0 in session.json")
    return sess


def apply_writes(kb, approve=False, reorganize=False, sleep_fn=None):
    snooze = sleep_fn or _time.sleep
    if not approve:
        return {"applied": False,
                "message": "DRY RUN: pass --approve to write to X."}
    store.ensure_kb(kb)
    payload = util.read_json(store.kb_path(kb, "writes.json"), None)
    if not payload:
        raise SystemExit("run plan-writes first")
    need = ["createBookmarkFolder", "bookmarkTweetToFolder"]
    if reorganize:
        need.append("RemoveTweetFromBookmarkFolder")
    sess = _session_or_die(kb, need)
    cookie, ct0 = sess["cookie_header"], sess["ct0"]
    name_to_id = {}
    done = failed = skipped = 0
    for op in payload["ops"]:
        if op.get("status") == "done":
            if op.get("folder_id"):
                name_to_id[op["name"]] = op["folder_id"]
            skipped += 1
            continue
        try:
            if op["op"] == "create_folder":
                st, data, _ = R.graphql_post(
                    sess["queryId_createBookmarkFolder"],
                    "createBookmarkFolder", {"name": op["name"]},
                    sess, ct0, cookie)
                node = (data.get("data") or {}).get(
                    "create_bookmark_folder") or {}
                fid = node.get("collection_id") or data.get("id")
                if st == 200 and fid:
                    op.update(status="done", folder_id=str(fid))
                    name_to_id[op["name"]] = str(fid)
                    done += 1
                    store.log_change(kb, {"kind": "folder_created",
                                          "name": op["name"],
                                          "folder_id": str(fid)})
                else:
                    raise RuntimeError("create http=%s" % st)
            elif op["op"] == "add_to_folder":
                fid = name_to_id.get(op["category"])
                if not fid:
                    raise RuntimeError("no folder id yet")
                st, _d, _h = R.graphql_post(
                    sess["queryId_bookmarkTweetToFolder"],
                    "bookmarkTweetToFolder",
                    {"tweet_id": op["tweet_id"], "collection_id": fid},
                    sess, ct0, cookie)
                if st != 200:
                    raise RuntimeError("add http=%s" % st)
                op.update(status="done", folder_id=fid)
                done += 1
                store.log_change(kb, {"kind": "added_to_folder",
                                      "tweet_id": op["tweet_id"],
                                      "folder_id": fid})
                if reorganize:
                    for old in op.get("from_folder_ids") or []:
                        if old == fid:
                            continue
                        st2, _, _ = R.graphql_post(
                            sess["queryId_RemoveTweetFromBookmarkFolder"],
                            "RemoveTweetFromBookmarkFolder",
                            {"tweet_id": op["tweet_id"],
                             "collection_id": old},
                            sess, ct0, cookie)
                        store.log_change(
                            kb, {"kind": "removed_from_folder",
                                 "tweet_id": op["tweet_id"],
                                 "folder_id": old, "http": st2})
        except Exception as e:
            op.update(status="failed", error=str(e)[:300])
            failed += 1
            store.log_change(kb, {"kind": "op_failed", "op": op.get("op"),
                                  "error": str(e)[:300]})
        util.write_json(store.kb_path(kb, "writes.json"), payload)
        snooze(1.0)
    return {"applied": True, "done": done, "failed": failed,
            "skipped": skipped, "reorganize": reorganize}

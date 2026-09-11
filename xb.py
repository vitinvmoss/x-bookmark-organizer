#!/usr/bin/env python3
"""xb.py CLI part 1: collect/normalize/analyze/categories/classify."""
from __future__ import annotations
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xbookmark import store, util  # noqa: E402


def cmd_demo(a):
    from xbookmark import demo
    print(json.dumps(demo.run(a.kb, n=a.n, seed=a.seed), indent=2))


def cmd_import(a):
    from xbookmark import collect
    fmt = (a.format or "auto").lower()
    path = os.path.abspath(os.path.expanduser(a.fixture))
    if fmt == "auto":
        try:
            head = open(path, encoding="utf-8").read(2000)
            if '"bookmarks"' in head and '"tweet_id"' in head:
                fmt = "xarchive"
            else:
                fmt = "userscript"
        except Exception:
            fmt = "userscript"
    if fmt == "xarchive":
        envs, folders = collect.load_xarchive_file(path)
        stats = collect.import_envelopes(a.kb, envs, source="xarchive")
        if folders:
            util.write_json(store.kb_path(a.kb, "folders.json"), folders)
    else:
        envs = collect.load_userscript_fixture(path)
        stats = collect.import_envelopes(a.kb, envs, source="userscript")
    print(json.dumps(stats, indent=2))


def cmd_normalize(a):
    from xbookmark import normalize as NZ
    print(json.dumps(NZ.normalize_all(a.kb), indent=2))


def cmd_analyze(a):
    from xbookmark import analyze as AZ
    r = AZ.analyze(a.kb)
    print("total=%d media=%d" % (r["total"], r["stats"]["with_media"]))
    print("top domains:", r["top_domains"][:10])
    print("top hashtags:", r["top_hashtags"][:10])
    print("top keywords:", r["top_keywords"][:15])


def cmd_categories(a):
    from xbookmark import categories as CZ
    payload = CZ.discover(a.kb, max_categories=a.max_categories,
                          min_size=a.min_size, engine=a.engine,
                          model=a.model)
    print("engine=%s categories=%d" % (payload["engine"],
                                       len(payload["categories"])))
    for c in payload["categories"]:
        print("  - %s" % c["name"])


def cmd_discover_categories(a):
    """Stage 3: whole-library discovery + review/accept printout."""
    from xbookmark import categories as CZ
    payload = CZ.discover_structure(
        a.kb, engine=a.engine, model=a.model,
        min_categories=a.min_categories,
        max_categories=a.max_categories)
    print("engine=%s collection=%d categories=%d" % (
        payload["engine"], payload["collection_size"],
        len(payload["categories"])))
    for c in payload["categories"]:
        print("  %-4d%%  %-30s count=%-6s %s" % (
            round((c.get("confidence") or 0) * 100), c["name"][:30],
            c.get("count", 0), (c.get("description") or "")[:80]))
        for ex in (c.get("examples") or [])[:3]:
            print("          e.g. %s" % ex[:100])
    if a.accept:
        res = CZ.accept_structure(a.kb, payload["categories"])
        print("accepted structure: %s" % ", ".join(res["names"]))
    else:
        print("review %s, then re-run with --accept "
              "(or use the web UI)." % ("category_structure.json",))


def cmd_classify(a):
    from xbookmark import classify as CL
    print(json.dumps(CL.classify(a.kb, engine=a.engine, model=a.model,
                                 confidence_threshold=a.confidence),
                     indent=2))


def cmd_classify_all(a):
    from xbookmark import classify as CL
    print(json.dumps(CL.classify_all(
        a.kb, engine=a.engine, model=a.model,
        batch_size=a.batch_size, force=a.force,
        limit=a.limit), indent=2))


def cmd_duplicates(a):
    from xbookmark import duplicates as DU
    groups = DU.find_groups(a.kb)
    print("groups=%d" % len(groups))
    for g in groups[:50]:
        print("  %-12s %s  %s" % (g["kind"], ",".join(g["ids"][:6]),
                                  (g.get("detail") or "")[:100]))


def cmd_search(a):
    from xbookmark import search as SE
    s = SE.Search(a.kb)
    ids = s.query(a.q, limit=a.limit)
    print("query=%r hits=%d" % (a.q, len(ids)))
    for tid in ids[:30]:
        print("  %s  %s" % (tid, s.snippets(tid, a.q)[:120]))
def cmd_propose(a):
    from xbookmark import propose as PZ
    proposal = PZ.build(a.kb, confidence_threshold=a.confidence)
    print(PZ.render_text(proposal))


def cmd_plan(a):
    from xbookmark import writes as WZ
    payload = WZ.plan(a.kb, preserve_existing=not a.no_preserve)
    print(json.dumps(payload["summary"], indent=2))


def cmd_apply(a):
    from xbookmark import apply_phase as AP
    res = AP.apply_writes(a.kb, approve=a.approve, reorganize=a.reorganize)
    print(json.dumps(res, indent=2))
    if not a.approve:
        print("DRY RUN: nothing written. Re-run with --approve.")


def cmd_query_ids(a):
    from xbookmark import discovery as DZ
    sess = store.load_session(a.kb)
    if a.add:
        for url in a.add:
            sess.update(DZ.discover_ids_from_url_path(url.strip()))
        store.save_session(a.kb, sess)
    if a.discover:
        found = DZ.scrape_bundles()
        sess.update({"queryId_" + k: v for k, v in found.items()})
        sess["discovery_at"] = util.utcnow_iso()
        store.save_session(a.kb, sess)
        print("discovered: %s" % json.dumps(found, indent=2))
    if a.show or (not a.add and not a.discover):
        qids = {k: v for k, v in sess.items() if k.startswith("queryId_")}
        print(json.dumps(qids or {"note": "no IDs yet"}, indent=2))
def cmd_dryrun(a):
    from xbookmark import normalize as NZ, analyze as AZ
    from xbookmark import categories as CZ, classify as CL
    from xbookmark import propose as PZ
    NZ.normalize_all(a.kb)
    AZ.analyze(a.kb)
    payload = CZ.discover(a.kb, max_categories=a.max_categories,
                          min_size=a.min_size, engine=a.engine,
                          model=a.model)
    print("categories (%s): %d" % (payload["engine"],
                                   len(payload["categories"])))
    print(json.dumps(CL.classify(a.kb, engine=a.engine, model=a.model,
                                 confidence_threshold=a.confidence),
                     indent=2))
    print(PZ.render_text(PZ.build(a.kb, confidence_threshold=a.confidence)))


def cmd_ai_status(_a):
    from xbookmark import ai as AI
    print(json.dumps(AI.status(), indent=2))

def build_parser():
    p = argparse.ArgumentParser(prog="xb.py")
    p.add_argument("--kb", default="data")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("demo")
    s.add_argument("--n", type=int, default=200)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("import")
    s.add_argument("--fixture", required=True)
    s.add_argument("--format", default="auto")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("normalize")
    s.set_defaults(fn=cmd_normalize)
    s = sub.add_parser("analyze")
    s.set_defaults(fn=cmd_analyze)
    for name, fn in (("categories", cmd_categories),
                     ("classify", cmd_classify), ("dry-run", cmd_dryrun)):
        s = sub.add_parser(name)
        s.add_argument("--engine", default="ai")
        s.add_argument("--model", default=None)
        s.add_argument("--max-categories", type=int, default=20)
        s.add_argument("--min-size", type=int, default=5)
        s.add_argument("--confidence", type=float, default=0.45)
        s.set_defaults(fn=fn)
    s = sub.add_parser("propose")
    s.add_argument("--confidence", type=float, default=0.45)
    s.set_defaults(fn=cmd_propose)
    s = sub.add_parser("discover-categories")
    s.add_argument("--engine", default="ai")
    s.add_argument("--model", default=None)
    s.add_argument("--min-categories", type=int, default=8)
    s.add_argument("--max-categories", type=int, default=20)
    s.add_argument("--accept", action="store_true",
                   help="skip review and accept the discovered structure")
    s.set_defaults(fn=cmd_discover_categories)
    s = sub.add_parser("classify-all")
    s.add_argument("--engine", default="heuristic")
    s.add_argument("--model", default=None)
    s.add_argument("--batch-size", type=int, default=20)
    s.add_argument("--force", action="store_true")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(fn=cmd_classify_all)
    s = sub.add_parser("duplicates")
    s.set_defaults(fn=cmd_duplicates)
    s = sub.add_parser("search")
    s.add_argument("q", nargs="?", default="")
    s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_search)
    s = sub.add_parser("plan-writes")
    s.add_argument("--no-preserve", action="store_true")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("apply")
    s.add_argument("--approve", action="store_true")
    s.add_argument("--reorganize", action="store_true")
    s.set_defaults(fn=cmd_apply)
    s = sub.add_parser("ai-status")
    s.set_defaults(fn=cmd_ai_status)
    s = sub.add_parser("query-ids")
    s.add_argument("--discover", action="store_true")
    s.add_argument("--show", action="store_true")
    s.add_argument("--add", action="append", default=[])
    s.set_defaults(fn=cmd_query_ids)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    store.ensure_kb(a.kb)
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


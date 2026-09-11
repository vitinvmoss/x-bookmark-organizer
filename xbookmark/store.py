"""Local store layout + session (query IDs, folders, change log).

data/<kb>/
  raw.jsonl            imported raw envelopes (userscript / xarchive / graphql)
  bookmarks.json       normalized bookmarks keyed by tweet_id
  folders.json         known X folders [{id,name}]
  folder_members.json  {folder_id: [tweet_id]}
  analysis.json        cheap local stats + clusters
  categories.json      discovered categories
  category_structure.json  Stage-3 whole-library discovery proposal/accepted
  classifications.jsonl {tweet_id,category,confidence,reason}
  proposal.json        dry-run proposal (folders + counts + changes)
  writes.json          planned write ops
  change_log.jsonl     every proposed + completed change
  session.json         query IDs, features, captured_at (never secrets)
"""
from __future__ import annotations
import os
from . import util

FILES = ["raw.jsonl", "bookmarks.json", "folders.json",
         "folder_members.json", "analysis.json", "categories.json",
         "category_structure.json", "library.json", "feedback.jsonl",
         "classifications.jsonl", "proposal.json", "writes.json",
         "change_log.jsonl", "session.json"]


def kb_path(kb: str, name: str) -> str:
    return os.path.join(os.path.abspath(os.path.expanduser(kb)), name)


def ensure_kb(kb: str) -> str:
    root = os.path.abspath(os.path.expanduser(kb))
    util.ensure_dir(root)
    return root


def load_bookmarks(kb: str) -> dict:
    return util.read_json(kb_path(kb, "bookmarks.json"), {}) or {}


def save_bookmarks(kb: str, data: dict) -> None:
    util.write_json(kb_path(kb, "bookmarks.json"), data)


def log_change(kb: str, event: dict) -> None:
    rec = dict(event)
    rec.setdefault("at", util.utcnow_iso())
    util.append_jsonl(kb_path(kb, "change_log.jsonl"), rec)


def load_session(kb: str) -> dict:
    return util.read_json(kb_path(kb, "session.json"), {}) or {}


def save_session(kb: str, sess: dict) -> None:
    util.write_json(kb_path(kb, "session.json"), sess)

"""Tiny SQLite helper (stdlib sqlite3) for server-side metadata.

The bookmark library itself stays in JSON KB files under DATA_DIR (which on
Render is a persistent disk mount, e.g. /var/data). This SQLite DB tracks
operational metadata co-located with the data dir:

  uploads(id, at, filename, imported, duplicates, skipped, invalid)
  app_log(id, at, kind, detail)   # no bookmark text, no secrets

Single file: <DATA_DIR>/meta.db. Survives restarts when DATA_DIR is on a
persistent disk; on free ephemeral storage it resets (documented).
"""
from __future__ import annotations
import os
import sqlite3

from . import util


def data_dir():
    d = (os.environ.get("DATA_DIR") or os.environ.get("KB_DIR")
         or "data").strip() or "data"
    util.ensure_dir(os.path.abspath(os.path.expanduser(d)))
    return os.path.abspath(os.path.expanduser(d))


def kb_root():
    # KB files live directly in DATA_DIR (back-compat: data/<kb> layout
    # still works locally when KB="data").
    return data_dir()


def db_path():
    return os.path.join(data_dir(), "meta.db")


def connect():
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = connect()
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS uploads(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            filename TEXT NOT NULL DEFAULT '',
            imported INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            invalid INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS app_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')""")
        con.commit()
    finally:
        con.close()


def log_upload(filename, imported, duplicates, skipped, invalid):
    init_db()
    con = connect()
    try:
        con.execute("INSERT INTO uploads(at,filename,imported,duplicates,"
                    "skipped,invalid) VALUES(?,?,?,?,?,?)",
                    (util.utcnow_iso(), str(filename or "")[:200],
                     int(imported), int(duplicates), int(skipped),
                     int(invalid)))
        con.commit()
    finally:
        con.close()


def recent_uploads(limit=20):
    try:
        init_db()
        con = connect()
        try:
            rows = con.execute("SELECT at,filename,imported,duplicates,"
                               "skipped,invalid FROM uploads ORDER BY id DESC"
                               " LIMIT ?", (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except Exception:
        return []


def log_event(kind, detail=""):
    try:
        init_db()
        con = connect()
        try:
            con.execute("INSERT INTO app_log(at,kind,detail) VALUES(?,?,?)",
                        (util.utcnow_iso(), str(kind or "")[:80],
                         str(detail or "")[:500]))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass

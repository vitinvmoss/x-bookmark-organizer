"""Production web service (Flask) for x-bookmark-organizer.

Local dev still works via app.py (stdlib, zero deps). This module is the
publicly deployable target (Render): auth, CSRF, mobile upload, hosted LLM
(Gemini default), SQLite metadata, health endpoint.

Start (production):  gunicorn server:app --bind 0.0.0.0:$PORT --workers 2
Dev:                  python server.py
"""
from __future__ import annotations
import csv
import functools
import io
import json
import os
import re
import urllib.parse

from flask import (Flask, jsonify, request, session, redirect, url_for,
                   render_template, send_file, g)

from xbookmark import store, util
from xbookmark import library as LIB
from xbookmark import search as SEARCH
from xbookmark import exporter as EXP
from xbookmark import classify as CLS
from xbookmark import demo as DEMO
from xbookmark import normalize as NZ
from xbookmark import categories as CAT
from xbookmark import auth as AUTH
from xbookmark import db as DB
from xbookmark import llm as LLM
from xbookmark import ai as LEGACY_AI

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_ENGINES = {"gemini", "groq", "openrouter", "heuristic", "ai"}

REVIEW_EXPLAINER = ("Needs Review contains bookmarks whose classification is "
                    "uncertain, usually because confidence is below 0.60, or "
                    "because the bookmark has not been classified yet.")

_x_url_re = re.compile(r"https?://(www\.)?(twitter|x)\.com/"
                       r"(?P<user>[A-Za-z0-9_]{1,20})")


def kb():
    return DB.kb_root()


def create_app():
    app = Flask(__name__, template_folder=os.path.join(HERE, "templates"))
    secret = AUTH.session_secret()
    if not secret:
        if AUTH.is_production():
            raise RuntimeError(
                "SESSION_SECRET must be set in production")
        secret = "dev-only-insecure-secret-change-me"
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=AUTH.is_production(),
        PERMANENT_SESSION_LIFETIME=14 * 24 * 3600,
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    )
    DB.init_db()
    store.ensure_kb(kb())

    # ---------- helpers ---------- #
    def logged_in():
        return bool(session.get("user"))

    def login_required(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            if not logged_in():
                if request.path.startswith("/api/"):
                    return jsonify({"error": "login required"}), 401
                return redirect(url_for("login"))
            return fn(*a, **k)
        return wrapper

    def check_csrf():
        """CSRF for state-changing browser requests (JSON header or form)."""
        if request.method == "GET":
            return True
        token = request.headers.get("X-CSRF-Token") or \
            request.headers.get("X-CSRFToken") or \
            (request.form.get("csrf_token") if request.form else None)
        if not token and request.is_json:
            try:
                token = (request.get_json(silent=True) or {}).get("csrf_token")
            except Exception:
                token = None
        if AUTH.valid_csrf(session, token):
            return True
        # also accept JSON body field for fetch clients
        return False

    def require_csrf(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            if not check_csrf():
                return jsonify({"error": "CSRF check failed"}), 403
            return fn(*a, **k)
        return wrapper

    def safe_outbound_url(u):
        try:
            u = (u or "").strip()
            if not u:
                return ""
            p = urllib.parse.urlparse(u)
            if p.scheme not in ("http", "https"):
                return ""
            if not p.netloc or len(u) > 2000:
                return ""
            return u
        except Exception:
            return ""

    def card(lib, tid, b):
        ov = lib.record(tid)
        media = b.get("media") or []
        thumb = ""
        if media and isinstance(media[0], dict):
            thumb = safe_outbound_url(media[0].get("url") or "")
        handle = (b.get("author_handle") or "unknown").lstrip("@")
        # URL-safe handle: X handles are [A-Za-z0-9_]{1,15}; anything else
        # (e.g. injected markup) must never reach href attributes.
        url_handle = re.sub(r"[^A-Za-z0-9_]", "", handle)[:20] or "unknown"
        urls = [safe_outbound_url(u.get("expanded") or u.get("short") or "")
                for u in (b.get("urls") or [])]
        urls = [u for u in urls if u]
        conf = ov.get("confidence")
        try:
            why = lib.review_reason(tid)
            needs = bool(why)
        except Exception:
            needs, why = False, ""
        avatar = "https://unavatar.io/twitter/%s?fallback=false" % (
            urllib.parse.quote(handle[:20] or "x"))
        return {
            "id": tid,
            "text": b.get("text") or "",
            "author": b.get("author_name") or "",
            "handle": handle,
            "avatar": avatar,
            "date": b.get("created_at") or "",
            "thumb": thumb,
            "domains": b.get("domains") or [],
            "hashtags": b.get("hashtags") or [],
            "mentions": b.get("mentions") or [],
            "links": urls,
            "link_out": bool(urls) and all(_x_url_re.match(u) for u in urls),
            "status": b.get("status") or "available",
            "categories": ov.get("categories") or [],
            "tags": ov.get("tags") or [],
            "intent": ov.get("intent") or "",
            "confidence": conf,
            "confidence_level": ov.get("confidence_level")
            or LIB.confidence_level(conf),
            "reason": ov.get("reason") or "",
            "alternatives": ov.get("alternatives") or [],
            "reviewed": bool(ov.get("reviewed")),
            "manual": bool(ov.get("manual")),
            "needs_review": needs,
            "review_reason": why,
            "x_url": "https://x.com/%s/status/%s" % (
                url_handle, re.sub(r"\D", "", str(tid)) or "0"),
            "author_url": "https://x.com/%s" % url_handle,
        }

    def engine_for(body):
        eng = str((body or {}).get("engine") or
                  LLM.provider_default() or "heuristic").lower()
        if eng not in ALLOWED_ENGINES:
            eng = "heuristic"
        return eng

    def ensure_cloud_opt_in(engine, body):
        """Hosted engines require explicit opt-in + configured key."""
        if engine in ("heuristic",):
            return None
        if engine == "ai":
            engine = "gemini"
        if not ((body or {}).get("cloud_ok") is True):
            return jsonify({"error": "cloud AI requires opt-in "
                                     "(tick 'Use cloud AI' — bookmark text "
                                     "is sent to the hosted provider)"}), 400
        key_env = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY",
                   "openrouter": "OPENROUTER_API_KEY"}.get(engine, "")
        if key_env and not os.environ.get(key_env):
            return jsonify({"error": "%s not configured; using heuristic "
                                     "instead" % engine,
                            "fallback": "heuristic"}), 400
        return None

    # ---------- public ---------- #
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "x-bookmark-organizer",
                        "time": util.utcnow_iso()})

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "x-bookmark-organizer",
                        "time": util.utcnow_iso()})

    @app.get("/api/csrf")
    def csrf():
        return jsonify({"csrf_token": AUTH.ensure_csrf_token(session)})

    @app.get("/login")
    def login():
        if logged_in():
            return redirect(url_for("index"))
        AUTH.ensure_csrf_token(session)
        return render_template("login.html",
                               csrf_token=session.get("csrf_token", ""))

    @app.post("/login")
    def login_post():
        if not check_csrf():
            return render_template(
                "login.html", csrf_token=AUTH.ensure_csrf_token(session),
                error="CSRF check failed. Reload and try again."), 403
        user = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        if AUTH.verify_credentials(user, pw):
            session.clear()
            session["user"] = AUTH.app_username()
            session.permanent = True
            AUTH.ensure_csrf_token(session)
            DB.log_event("login", "ok")
            nxt = (request.form.get("next") or "/").strip() or "/"
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = "/"
            return redirect(nxt)
        DB.log_event("login", "failed")
        return render_template(
            "login.html", csrf_token=AUTH.ensure_csrf_token(session),
            error="Invalid username or password."), 401

    @app.post("/logout")
    def logout():
        if not check_csrf():
            return jsonify({"error": "CSRF check failed"}), 403
        session.clear()
        if request.is_json or "application/json" in (
                request.headers.get("Accept") or ""):
            return jsonify({"ok": True})
        return redirect(url_for("login"))

    # ---------- protected pages ---------- #
    @app.get("/")
    @login_required
    def index():
        AUTH.ensure_csrf_token(session)
        return render_template(
            "app.html", csrf_token=session.get("csrf_token", ""),
            user=session.get("user", ""),
            review_explainer=REVIEW_EXPLAINER,
            llm_default=LLM.provider_default())

    @app.get("/upload")
    @login_required
    def upload_page():
        AUTH.ensure_csrf_token(session)
        return render_template(
            "upload.html", csrf_token=session.get("csrf_token", ""),
            user=session.get("user", ""),
            max_mb=MAX_UPLOAD_BYTES // (1024 * 1024))

    # ---------- protected APIs ---------- #
    @app.get("/api/state")
    @login_required
    def api_state():
        lib = LIB.Library(kb())
        counts = {c["name"]: 0 for c in lib.category_list()}
        for tid in lib.bookmarks:
            for c in lib.categories_for(tid):
                if c in counts:
                    counts[c] += 1
        try:
            from xbookmark import duplicates as DUP
            dup_n = len(DUP.find_groups(kb()))
        except Exception:
            dup_n = 0
        books = store.load_bookmarks(kb())
        legacy = LEGACY_AI.status()
        cfg = LLM.configured_provider_status()
        return jsonify({
            "kb": "default",
            "total": len(books),
            "categories": lib.category_list(),
            "counts": counts,
            "unsorted": lib.unsorted_count(),
            "review": len(lib.review_ids()),
            "review_explainer": REVIEW_EXPLAINER,
            "confidence": lib.confidence_counts(),
            "duplicates": dup_n,
            "intents": {i: lib.intent_count(i) for i in LIB.INTENTS},
            "intent_labels": dict(LIB.INTENT_LABELS),
            "ai": legacy,
            "llm": cfg,
            "cloud_default": False,
            "uploads": DB.recent_uploads(5),
        })

    @app.get("/api/ai-status")
    @login_required
    def api_ai_status():
        # safe: configured flags + models only, never keys. The legacy
        # ai.status() masks keys as first4...last2; strip even that.
        legacy = LEGACY_AI.status()
        try:
            ep = dict(legacy.get("endpoint") or {})
            ep.pop("key", None)
            legacy = dict(legacy, endpoint=ep)
        except Exception:
            pass
        return jsonify({"ok": True, "llm": LLM.configured_provider_status(),
                        "legacy": legacy,
                        "cloud_default": False})

    @app.post("/api/ai-test")
    @login_required
    @require_csrf
    def api_ai_test():
        body = request.get_json(silent=True) or {}
        provider = str(body.get("provider") or LLM.provider_default()
                       ).lower()
        if provider not in ("gemini", "groq", "openrouter"):
            return jsonify({"ok": False,
                            "error": "unknown provider"}), 400
        cfg = LLM.configured_provider_status()
        if not cfg.get(provider, {}).get("configured"):
            return jsonify({"ok": False, "provider": provider,
                            "configured": False,
                            "error": "%s API key not configured" % provider,
                            "model": cfg[provider]["model"]}), 200
        model = body.get("model") or cfg[provider]["model"]
        try:
            txt = LLM.chat_text(
                provider,
                [{"role": "system", "content": "Reply with JSON only."},
                 {"role": "user",
                  "content": 'Reply ONLY {"ok": true}. No other text.'}],
                model=model, timeout=20)
            data = LLM.parse_json_array_or_obj(txt)
            ok = bool(data.get("ok")) if isinstance(data, dict) else False
            return jsonify({"ok": ok, "provider": provider,
                            "model": model, "configured": True,
                            "message": "connection OK" if ok
                            else "unexpected reply"})
        except Exception as e:
            return jsonify({"ok": False, "provider": provider,
                            "model": model, "configured": True,
                            "error": LLM._safe_error(e)}), 200

    @app.get("/api/bookmarks")
    @login_required
    def api_bookmarks():
        lib = LIB.Library(kb())
        books = store.load_bookmarks(kb())
        q = request.args.get("q", "")
        cat = request.args.get("cat", "")
        intent = request.args.get("intent", "")
        mode = request.args.get("mode", "")
        author = request.args.get("author", "").lower().lstrip("@")
        domain = request.args.get("domain", "").lower()
        conf = request.args.get("conf", "").upper()
        only_unsorted = request.args.get("unsorted") in ("1", "true")
        only_review = request.args.get("needs_review") in ("1", "true")
        sort = request.args.get("sort", "date_desc")
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1
        try:
            per = min(100, max(10, int(request.args.get("per", "30"))))
        except ValueError:
            per = 30
        ids = SEARCH.Search(kb()).query(q)
        review_ids = set(lib.review_ids())

        def keep(tid):
            if tid not in books:
                return False
            cats = lib.categories_for(tid)
            ov = lib.record(tid)
            if mode == "review" and tid not in review_ids:
                return False
            if mode == "unsorted" and cats:
                return False
            if only_unsorted and cats:
                return False
            if only_review and tid not in review_ids:
                return False
            if cat:
                if cat == "Unsorted / Review":
                    if cats:
                        return False
                elif cat not in cats:
                    return False
            if intent and ov.get("intent") != intent:
                return False
            if author:
                b = books.get(tid) or {}
                hay = ((b.get("author_name") or "") + " " +
                       (b.get("author_handle") or "")).lower()
                if author not in hay:
                    return False
            if domain:
                b = books.get(tid) or {}
                doms = " ".join(b.get("domains") or []).lower()
                urls = " ".join([(u.get("expanded") or u.get("short") or "")
                                 for u in (b.get("urls") or [])]).lower()
                if domain not in doms and domain not in urls:
                    return False
            if conf in ("HIGH", "MEDIUM", "LOW"):
                lvl = ov.get("confidence_level") or LIB.confidence_level(
                    ov.get("confidence"))
                if lvl != conf:
                    return False
            return True

        ids = [t for t in ids if keep(t)]
        rev = sort.endswith("_asc")
        key = sort.replace("_asc", "").replace("_desc", "")

        def sort_key(tid):
            b = books.get(tid) or {}
            if key == "author":
                return (b.get("author_name") or "").lower()
            if key == "confidence":
                ov = lib.record(tid)
                c = ov.get("confidence")
                return c if c is not None else -1.0
            return (b.get("created_at") or "")

        ids.sort(key=sort_key, reverse=not rev)
        total = len(ids)
        start = (page - 1) * per
        cards = [card(lib, t, books[t]) for t in ids[start:start + per]]
        return jsonify({"total": total, "page": page, "per": per,
                        "cards": cards})

    @app.get("/api/duplicates")
    @login_required
    def api_duplicates():
        from xbookmark import duplicates as DUP
        groups = DUP.find_groups(kb())
        return jsonify({"total": len(groups), "groups": groups[:200]})

    @app.get("/api/discover")
    @login_required
    def api_discover_get():
        proposal, err = CAT.get_proposal(kb())
        return jsonify({"ok": proposal is not None,
                        "proposal": proposal, "error": err})

    @app.get("/api/export")
    @login_required
    def api_export():
        fmt = (request.args.get("format") or "json").lower()
        if fmt not in ("csv", "json", "md"):
            return jsonify({"error": "bad format"}), 400
        if fmt == "csv":
            data = EXP.to_csv(kb()).encode("utf-8")
            mime = "text/csv; charset=utf-8"
        elif fmt == "md":
            data = EXP.to_markdown(kb()).encode("utf-8")
            mime = "text/markdown; charset=utf-8"
        else:
            data = EXP.to_json(kb()).encode("utf-8")
            mime = "application/json; charset=utf-8"
        return send_file(io.BytesIO(data), mimetype=mime,
                         as_attachment=True,
                         download_name="bookmarks.%s" % fmt)

    @app.get("/api/backup")
    @login_required
    def api_backup():
        data = EXP.backup_zip(kb())
        return send_file(io.BytesIO(data), mimetype="application/zip",
                         as_attachment=True,
                         download_name="xbookmarks-backup.zip")

    @app.get("/api/uploads")
    @login_required
    def api_uploads():
        return jsonify({"uploads": DB.recent_uploads(20)})

    # ----- import / upload ----- #
    def _import_envelope_list(envelopes, source, filename):
        from xbookmark import collect as COL
        stats = COL.import_envelopes(kb(), envelopes, source=source)
        norm = NZ.normalize_all(kb())
        DB.log_upload(filename, stats.get("added", 0),
                      stats.get("duplicates", 0), 0, 0)
        return {"added": stats.get("added", 0),
                "duplicates": stats.get("duplicates", 0),
                "invalid": 0, "skipped": norm.get("unchanged", 0),
                "total": norm.get("total", 0), "source": source}

    def _direct_upsert(items, filename):
        """Upsert simple bookmark dicts with tweet_id/URL/hash dedup."""
        books = store.load_bookmarks(kb())
        by_url = {}
        for tid, b in books.items():
            for u in (b.get("urls") or []):
                url = (u.get("expanded") or u.get("short") or "").strip()
                if url:
                    by_url[url] = tid
        hashes = {b.get("content_hash") for b in books.values()
                  if b.get("content_hash")}
        imported = dups = invalid = 0
        for it in items:
            tid = str(it.get("tweet_id") or it.get("id") or "").strip()
            url = str(it.get("url") or it.get("x_url") or "").strip()
            if not tid and url:
                m = re.search(r"/status/(\d+)", url)
                if m:
                    tid = m.group(1)
            text = str(it.get("text") or it.get("full_text") or "")
            if not tid or not text.strip():
                invalid += 1
                continue
            if tid in books or (url and url in by_url):
                dups += 1
                continue
            ch = NZ.content_hash(
                text, str(it.get("author_handle") or "unknown"))
            if ch in hashes:
                dups += 1
                continue
            handle = str(it.get("author_handle") or it.get("author") or
                         "unknown").lstrip("@") or "unknown"
            url_handle = re.sub(r"[^A-Za-z0-9_]", "", handle)[:20] \
                or "unknown"
            urls = []
            if url:
                xurl = safe_outbound_url(url) or \
                    "https://x.com/%s/status/%s" % (
                        url_handle, re.sub(r"\D", "", tid) or "0")
                urls = [{"short": None, "expanded": xurl, "display": None}]
            books[tid] = {
                "tweet_id": tid, "status": "available", "text": text,
                "created_at": it.get("created_at") or "",
                "author_handle": handle,
                "author_name": str(it.get("author_name") or handle),
                "hashtags": [str(h).lower().lstrip("#")
                             for h in (it.get("hashtags") or [])][:16],
                "mentions": [], "urls": urls,
                "domains": [],
                "media": [], "has_media": False, "content_hash": ch,
                "first_seen_at": util.utcnow_iso(),
                "last_seen_at": util.utcnow_iso(),
                "existing_folders": [], "existing_folder_ids": []}
            try:
                dom = urllib.parse.urlparse(urls[0]["expanded"]).hostname \
                    or "" if urls else ""
                books[tid]["domains"] = [dom.lower().lstrip("www.")] \
                    if dom else []
            except Exception:
                pass
            hashes.add(ch)
            if url:
                by_url[url] = tid
            imported += 1
        store.save_bookmarks(kb(), books)
        store.log_change(kb(), {"kind": "import", "source": "direct",
                                "added": imported, "duplicates": dups,
                                "invalid": invalid})
        DB.log_upload(filename, imported, dups, 0, invalid)
        return {"added": imported, "duplicates": dups, "invalid": invalid,
                "skipped": 0, "total": len(books), "source": "direct"}

    @app.post("/api/import")
    @login_required
    @require_csrf
    def api_import():
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "no file uploaded"}), 400
        raw = f.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return jsonify({"error": "file too large (max %d MB)" %
                                     (MAX_UPLOAD_BYTES // (1024 * 1024))}), 413
        name = f.filename or "upload"
        lower = name.lower()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            DB.log_upload(name, 0, 0, 0, 1)
            return jsonify({"error": "file must be UTF-8 text"}), 400
        try:
            if lower.endswith(".jsonl") or lower.endswith(".ndjson"):
                return jsonify(_import_jsonl_text(text, name))
            if lower.endswith(".csv"):
                return jsonify(_import_csv_text(text, name))
            if lower.endswith(".json"):
                return jsonify(_import_json_text(text, name))
            # sniff
            s = text.lstrip()[:1]
            if s == "[" or s == "{":
                return jsonify(_import_json_text(text, name))
            return jsonify(_import_jsonl_text(text, name))
        except ValueError as e:
            DB.log_upload(name, 0, 0, 0, 1)
            return jsonify({"error": str(e)[:300]}), 400

    def _import_jsonl_text(text, name):
        from xbookmark import collect as COL
        from xbookmark import parser as P
        envelopes = []
        invalid = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if isinstance(obj, dict) and obj.get("status_id") and \
                    "raw" in obj:
                envelopes.append(obj)
            elif isinstance(obj, dict) and "data" in obj:
                try:
                    tweets, _bottom, valid = P.parse_bookmarks_page(obj)
                except Exception:
                    invalid += 1
                    continue
                if not valid:
                    invalid += 1
                    continue
                for t in tweets:
                    entry = {"entryId": "tweet-%s" % t["tweet_id"],
                             "content": {"itemContent": {"tweet_results": {
                                 "result": t.get("_raw_result", {})}}}}
                    envelopes.append(COL._raw_envelope(
                        t["tweet_id"], entry["entryId"], entry))
            else:
                invalid += 1
        if not envelopes and invalid:
            raise ValueError("no valid bookmark lines found "
                             "(invalid=%d)" % invalid)
        res = _import_envelope_list(envelopes, "mobile-upload", name)
        res["invalid"] = invalid
        return {"ok": True, "imported": res["added"],
                "duplicates": res["duplicates"], "invalid": invalid,
                "skipped": res["skipped"], "total": res["total"],
                "filename": name}

    def _import_json_text(text, name):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("invalid JSON file")
        from xbookmark import collect as COL
        # xarchive shape
        if isinstance(data, dict) and isinstance(
                data.get("bookmarks"), list):
            envs, folders = COL.load_xarchive_file_from_data(data)
            if folders:
                util.write_json(store.kb_path(kb(), "folders.json"),
                                folders)
            res = _import_envelope_list(envs, "xarchive-upload", name)
            return {"ok": True, "imported": res["added"],
                    "duplicates": res["duplicates"], "invalid": 0,
                    "skipped": res["skipped"], "total": res["total"],
                    "filename": name}
        # list of envelopes
        if isinstance(data, list) and data and isinstance(data[0], dict) \
                and data[0].get("status_id") and "raw" in data[0]:
            res = _import_envelope_list(data, "mobile-upload", name)
            return {"ok": True, "imported": res["added"],
                    "duplicates": res["duplicates"], "invalid": 0,
                    "skipped": res["skipped"], "total": res["total"],
                    "filename": name}
        # simple list of tweet dicts
        if isinstance(data, list):
            res = _direct_upsert(
                [d for d in data if isinstance(d, dict)], name)
            return {"ok": True, "imported": res["added"],
                    "duplicates": res["duplicates"],
                    "invalid": res["invalid"], "skipped": 0,
                    "total": res["total"], "filename": name}
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            res = _direct_upsert(
                [d for d in data["items"] if isinstance(d, dict)], name)
            return {"ok": True, "imported": res["added"],
                    "duplicates": res["duplicates"],
                    "invalid": res["invalid"], "skipped": 0,
                    "total": res["total"], "filename": name}
        raise ValueError("unrecognized JSON structure")

    def _import_csv_text(text, name):
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
        except Exception:
            raise ValueError("invalid CSV file")
        if not rows:
            raise ValueError("empty CSV file")
        items = []
        for r in rows:
            items.append({
                "tweet_id": r.get("tweet_id") or r.get("id") or "",
                "url": r.get("url") or r.get("x_url") or "",
                "text": r.get("text") or r.get("full_text") or "",
                "author_handle": r.get("author_handle") or r.get("author")
                or "unknown",
                "author_name": r.get("author_name") or "",
                "created_at": r.get("created_at") or "",
                "hashtags": [h.strip() for h in
                             (r.get("hashtags") or "").replace(
                                 ";", ",").split(",") if h.strip()],
            })
        res = _direct_upsert(items, name)
        return {"ok": True, "imported": res["added"],
                "duplicates": res["duplicates"], "invalid": res["invalid"],
                "skipped": 0, "total": res["total"], "filename": name}

    # ----- mutations ----- #
    def _body():
        try:
            return request.get_json(force=True, silent=True) or {}
        except Exception:
            return {}

    @app.post("/api/bookmarks/toggle")
    @login_required
    @require_csrf
    def api_toggle():
        body = _body()
        lib = LIB.Library(kb())
        try:
            rec = lib.toggle_category(str(body.get("id")),
                                      str(body.get("category") or ""),
                                      on=body.get("on"))
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)[:200]}), 400
        return jsonify({"ok": True, "record": rec})

    @app.post("/api/bookmarks/intent")
    @login_required
    @require_csrf
    def api_intent():
        body = _body()
        lib = LIB.Library(kb())
        rec = lib.set_bookmark(str(body.get("id")),
                               intent=body.get("intent"))
        return jsonify({"ok": True, "record": rec})

    @app.post("/api/bookmarks/tags")
    @login_required
    @require_csrf
    def api_tags():
        body = _body()
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            return jsonify({"error": "tags must be a list"}), 400
        tags = [str(t)[:60] for t in tags][:30]
        lib = LIB.Library(kb())
        rec = lib.set_bookmark(str(body.get("id")), tags=tags)
        return jsonify({"ok": True, "record": rec})

    @app.post("/api/bookmarks/categories")
    @login_required
    @require_csrf
    def api_set_cats():
        body = _body()
        lib = LIB.Library(kb())
        rec = lib.set_bookmark(str(body.get("id")),
                               categories=body.get("categories") or [])
        return jsonify({"ok": True, "record": rec})

    @app.post("/api/categories")
    @login_required
    @require_csrf
    def api_cat_create():
        body = _body()
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 60:
            return jsonify({"error": "invalid category name"}), 400
        lib = LIB.Library(kb())
        try:
            created = lib.add_category(
                name, str(body.get("description") or ""))
        except ValueError as e:
            return jsonify({"error": str(e)[:200]}), 400
        lib.save()
        return jsonify({"ok": True, "created": created})

    @app.post("/api/categories/rename")
    @login_required
    @require_csrf
    def api_cat_rename():
        body = _body()
        lib = LIB.Library(kb())
        try:
            lib.rename_category(str(body.get("from") or ""),
                                str(body.get("to") or ""))
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)[:200]}), 400
        return jsonify({"ok": True})

    @app.post("/api/categories/merge")
    @login_required
    @require_csrf
    def api_cat_merge():
        body = _body()
        lib = LIB.Library(kb())
        try:
            lib.merge_category(str(body.get("target") or ""),
                               *[str(s) for s in
                                 (body.get("sources") or [])])
        except (KeyError, ValueError) as e:
            return jsonify({"error": str(e)[:200]}), 400
        return jsonify({"ok": True})

    @app.post("/api/categories/delete")
    @login_required
    @require_csrf
    def api_cat_delete():
        body = _body()
        lib = LIB.Library(kb())
        ok = lib.delete_category(str(body.get("name") or ""))
        return jsonify({"ok": True, "deleted": ok})

    @app.post("/api/reanalyze")
    @login_required
    @require_csrf
    def api_reanalyze():
        body = _body()
        ids = [str(t) for t in (body.get("ids") or [])][:200]
        engine = engine_for(body)
        gate = ensure_cloud_opt_in(engine, body)
        eff = "heuristic" if gate else engine
        res = CLS.classify_subset(kb(), ids, engine=eff,
                                  model=body.get("model"))
        if gate:
            res = dict(res, cloud_gate="heuristic fallback (opt-in "
                                       "required)")
        return jsonify({"ok": True, "result": res})

    @app.post("/api/classify-all")
    @login_required
    @require_csrf
    def api_classify_all():
        body = _body()
        engine = engine_for(body)
        gate = ensure_cloud_opt_in(engine, body)
        eff = "heuristic" if gate else engine
        if eff == "ai" and not LEGACY_AI.status().get("available") \
                and not LLM.configured_provider_status().get(
                    "gemini", {}).get("configured"):
            eff = "heuristic"
        try:
            batch_size = int(body.get("batch_size") or CLS.BATCH)
        except (TypeError, ValueError):
            batch_size = CLS.BATCH
        batch_size = max(1, min(batch_size, 50))
        try:
            res = CLS.classify_all(
                kb(), engine=eff, model=body.get("model"),
                batch_size=batch_size, force=bool(body.get("force")),
                limit=body.get("limit"))
        except SystemExit as e:
            return jsonify({"error": str(e)[:300]}), 400
        if gate:
            res = dict(res, cloud_gate="heuristic fallback (opt-in "
                                       "required)")
        res.setdefault("resumable", True)
        DB.log_event("classify_all", "mode=%s done=%s" % (
            res.get("mode"), res.get("classified")))
        return jsonify({"ok": True, "result": res})

    @app.post("/api/review")
    @login_required
    @require_csrf
    def api_review():
        body = _body()
        lib = LIB.Library(kb())
        ids = [str(t) for t in (body.get("ids") or [])][:500]
        if body.get("id"):
            ids = [str(body.get("id"))] + ids
        if not ids:
            return jsonify({"error": "no ids"}), 400
        out = 0
        for tid in ids:
            kw = {}
            if body.get("categories") is not None:
                cats = body.get("categories") or []
                if not isinstance(cats, list):
                    return jsonify(
                        {"error": "categories must be a list"}), 400
                kw["categories"] = cats
            if body.get("intent") is not None:
                kw["intent"] = body.get("intent")
            kw["reviewed"] = False if body.get("done") is False else True
            lib.set_bookmark(tid, **kw)
            out += 1
        return jsonify({"ok": True, "updated": out})

    @app.post("/api/discover")
    @login_required
    @require_csrf
    def api_discover():
        body = _body()
        engine = engine_for(body)
        gate = ensure_cloud_opt_in(engine, body)
        eff = "heuristic" if gate else engine
        try:
            min_c = int(body.get("min_categories") or 8)
            max_c = int(body.get("max_categories") or 20)
        except (TypeError, ValueError):
            min_c, max_c = 8, 20
        try:
            res = CAT.discover_structure(
                kb(), engine=eff, model=body.get("model"),
                min_categories=max(1, min(min_c, 30)),
                max_categories=max(1, min(max_c, 30)))
        except SystemExit as e:
            return jsonify({"error": str(e)[:300]}), 400
        if gate:
            res = dict(res, cloud_gate="heuristic fallback (opt-in "
                                       "required)")
        return jsonify({"ok": True, "proposal": res})

    @app.post("/api/discover/accept")
    @login_required
    @require_csrf
    def api_discover_accept():
        body = _body()
        try:
            res = CAT.accept_structure(kb(), body.get("categories") or [])
        except ValueError as e:
            return jsonify({"error": str(e)[:200]}), 400
        return jsonify({"ok": True, **res})

    @app.post("/api/demo")
    @login_required
    @require_csrf
    def api_demo():
        body = _body()
        try:
            n = max(1, min(int(body.get("n") or 200), 2000))
        except (TypeError, ValueError):
            n = 200
        DEMO.run(kb(), n=n)
        NZ.normalize_all(kb())
        return jsonify({"ok": True, "imported": n})

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify({"error": "file too large"}), 413

    return app


app = create_app()


def main():
    port = int(os.environ.get("PORT", "8765"))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()

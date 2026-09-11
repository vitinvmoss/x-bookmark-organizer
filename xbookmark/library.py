"""Library: user-editable state layered on top of bookmarks.json.

bookmarks.json stays the immutable source of tweet content (keyed by
tweet_id). library.json adds the user's categorical view atop it:

    library.json = {
      "version": 1,
      "categories": [{"name": "AI & LLMs", "source": "ai"|"user",
                      "description": "...", "created_at": "..."}],
      "bookmarks": {
        "<tweet_id>": {"categories": [...], "tags": [...],
                       "intent": null|"read_later"|"must_read"|
                                 "try_this"|"reference",
                       "confidence": 0-1, "reviewed": bool}
      }
    }

On first load it migrates existing categories.json + classifications.jsonl.
"Unsorted / Review" is COMPUTED at query time (no topic categories OR low
confidence); it is never stored as a category.
"""
from __future__ import annotations
from . import store, util

VERSION = 1
INTENTS = ("read_later", "must_read", "try_this", "reference",
           "inspiration", "news", "entertainment")
INTENT_LABELS = {"read_later": "Read Later", "must_read": "Must Read",
                 "try_this": "Try This", "reference": "Reference",
                 "inspiration": "Inspiration", "news": "News",
                 "entertainment": "Entertainment"}
UNSORTED_SENTINEL = "Unsorted / Review"


def confidence_level(conf):
    """HIGH >= 0.85, MEDIUM 0.60-0.84, LOW < 0.60 (None -> LOW)."""
    if conf is None:
        return "LOW"
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return "LOW"
    if c >= 0.85:
        return "HIGH"
    if c >= 0.60:
        return "MEDIUM"
    return "LOW"


def intent_label(k):
    return INTENT_LABELS.get(k, k or "")


def _norm(name):
    return (name or "").strip().lower()


class Library:
    def __init__(self, kb):
        self.kb = kb
        self.path = store.kb_path(kb, "library.json")
        self.categories = {}   # lowercase name -> record
        self.bookmarks = {}    # tweet_id -> record
        self.load()

    # ------------------------------------------------------------------
    def load(self):
        data = util.read_json(self.path)
        if data:
            self._load_from(data)
            return
        self.seed()

    def _load_from(self, data):
        for c in (data.get("categories", []) or []):
            if c.get("name"):
                self.categories[_norm(c["name"])] = {
                    "name": c["name"], "source": c.get("source", "user"),
                    "description": c.get("description", ""),
                    "created_at": c.get("created_at", "")}
        for tid, rec in (data.get("bookmarks", {}) or {}).items():
            self.bookmarks[str(tid)] = self._clean_record(rec)

    def _clean_record(self, rec):
        rec = rec or {}
        cats = []
        for c in (rec.get("categories") or []):
            if c and _norm(c) in self.categories and c not in cats:
                cats.append(c)
        intent = rec.get("intent") or None
        if intent not in INTENTS:
            intent = None
        conf = rec.get("confidence")
        if conf is not None:
            try:
                conf = float(conf)
            except (TypeError, ValueError):
                conf = None
        alts = [str(a) for a in (rec.get("alternatives") or [])
                if str(a or "").strip()][:5]
        return {"categories": cats,
                "tags": list(dict.fromkeys(rec.get("tags") or [])),
                "intent": intent, "confidence": conf,
                "confidence_level": rec.get("confidence_level")
                or confidence_level(conf),
                "reason": str(rec.get("reason") or "")[:500],
                "alternatives": alts,
                "reviewed": bool(rec.get("reviewed")),
                "manual": bool(rec.get("manual")),
                "classified_at": rec.get("classified_at") or ""}
    def seed(self):
        """One-time migration from categories.json + classifications.jsonl."""
        cats_doc = util.read_json(store.kb_path(self.kb, "categories.json"))
        for c in (cats_doc.get("categories", []) if cats_doc else []) or []:
            name = (c.get("name") or "").strip()
            if not name or _norm(name) == _norm(UNSORTED_SENTINEL):
                continue
            self.add_category(name, c.get("description", ""), source="ai")
        for row in util.iter_jsonl(store.kb_path(
                self.kb, "classifications.jsonl")):
            tid = str(row.get("tweet_id") or "")
            if not tid:
                continue
            rec = self.bookmarks.get(tid)
            if rec is None:
                rec = {"categories": [], "tags": [], "intent": None,
                       "confidence": None, "confidence_level": "LOW",
                       "reason": "", "alternatives": [], "reviewed": False,
                       "manual": False, "classified_at": ""}
                self.bookmarks[tid] = rec
            cat = row.get("category") or ""
            if cat and _norm(cat) != _norm(UNSORTED_SENTINEL):
                if _norm(cat) in self.categories and cat not in rec["categories"]:
                    rec["categories"].append(cat)
            conf = row.get("confidence")
            if conf is not None:
                try:
                    rec["confidence"] = float(conf)
                except (TypeError, ValueError):
                    pass
        self.save()
        store.log_change(self.kb, {"kind": "library_seed",
                                   "categories": len(self.categories),
                                   "bookmarks": len(self.bookmarks)})

    def save(self):
        util.write_json(self.path, {
            "version": VERSION,
            "categories": [dict(c) for c in self.categories.values()],
            "bookmarks": {tid: dict(rec)
                          for tid, rec in self.bookmarks.items()}})

    # --- category CRUD --------------------------------------- #
    def add_category(self, name, description="", source="user"):
        name = (name or "").strip()
        if not name:
            raise ValueError("category name required")
        key = _norm(name)
        if key not in self.categories:
            self.categories[key] = {"name": name, "source": source,
                                    "description": description or "",
                                    "created_at": util.utcnow_iso()}
            return True
        return False

    def rename_category(self, name, new_name):
        new_name = (new_name or '').strip()
        if not new_name:
            raise ValueError('new name required')
        key = _norm(name)
        if key not in self.categories:
            raise KeyError('unknown category: %s' % name)
        if _norm(new_name) in self.categories and _norm(new_name) != key:
            return self.merge_category(new_name, name)
        rec = self.categories.pop(key)
        rec['name'] = new_name
        self.categories[_norm(new_name)] = rec
        for b in self.bookmarks.values():
            b['categories'] = [new_name if _norm(c) == key else c
                               for c in b['categories']]
        self.save()
        return True

    def merge_category(self, target, *sources):
        target = (target or '').strip()
        tk = _norm(target)
        if tk not in self.categories:
            raise KeyError('target category missing')
        for src in sources:
            sk = _norm(src)
            if sk == tk or sk not in self.categories:
                continue
            self.categories.pop(sk)
            for b in self.bookmarks.values():
                cats = b['categories']
                b['categories'] = list(dict.fromkeys(
                    [(target if _norm(c) == sk else c) for c in cats]))
        self.save()
        return True

    def delete_category(self, name):
        key = _norm(name)
        if key not in self.categories:
            return False
        self.categories.pop(key)
        for b in self.bookmarks.values():
            b['categories'] = [c for c in b['categories']
                               if _norm(c) != key]
        self.save()
        return True

    def category_list(self):
        return [dict(self.categories[k]) for k in self.categories]

    # ---- bookmark assignment ------------------------------- #
    def _rec(self, tid):
        tid = str(tid)
        if tid not in self.bookmarks:
            self.bookmarks[tid] = self._clean_record(None)
        return self.bookmarks[tid]

    def set_bookmark(self, tid, categories=None, tags=None, intent=None,
                     confidence=None, reviewed=None, save=True,
                     reason=None, alternatives=None, manual=None,
                     classified_at=None, _log_feedback=True):
        rec = self._rec(tid)
        old = {"categories": list(rec["categories"]),
               "intent": rec["intent"]}
        if categories is not None:
            cleaned = []
            for c in categories:
                c = (c or '').strip()
                if c and _norm(c) in self.categories and c not in cleaned:
                    cleaned.append(c)
            rec['categories'] = cleaned
        if tags is not None:
            rec['tags'] = list(dict.fromkeys(
                [t.strip() for t in tags if (t or '').strip()]))
        if intent is not None:
            rec['intent'] = intent if intent in INTENTS else None
        if confidence is not None:
            try:
                rec['confidence'] = max(0.0, min(1.0, float(confidence)))
            except (TypeError, ValueError):
                pass
            rec['confidence_level'] = confidence_level(rec['confidence'])
        if reviewed is not None:
            rec['reviewed'] = bool(reviewed)
        if reason is not None:
            rec['reason'] = str(reason or "")[:500]
        if alternatives is not None:
            rec['alternatives'] = [str(a) for a in alternatives
                                   if str(a or "").strip()][:5]
        if manual is not None:
            rec['manual'] = bool(manual)
        if classified_at is not None:
            rec['classified_at'] = classified_at
        if save:
            self.save()
        # manual corrections take precedence: any explicit category/intent
        # edit from the UI marks the record manual+reviewed and is logged
        # to feedback.jsonl for future AI context.
        if _log_feedback and (categories is not None or intent is not None):
            new = {"categories": list(rec["categories"]),
                   "intent": rec["intent"]}
            if new != old:
                try:
                    from . import feedback as FB
                    FB.log_correction(self.kb, str(tid), old, new)
                except Exception:
                    pass
                rec['manual'] = True
                rec['reviewed'] = True
                if save:
                    self.save()
        return rec

    def toggle_category(self, tid, name, on=None):
        name = (name or '').strip()
        rec = self._rec(tid)
        old = {"categories": list(rec["categories"]),
               "intent": rec["intent"]}
        present = name in rec['categories']
        if on is None:
            on = not present
        if on and name not in rec['categories']:
            rec['categories'].append(name)
        elif not on and name in rec['categories']:
            rec['categories'].remove(name)
        rec['reviewed'] = True
        rec['manual'] = True
        self.save()
        new = {"categories": list(rec["categories"]),
               "intent": rec["intent"]}
        if new != old:
            try:
                from . import feedback as FB
                FB.log_correction(self.kb, str(tid), old, new)
            except Exception:
                pass
        return rec

    def record(self, tid):
        return dict(self._rec(tid))

    def categories_for(self, tid):
        return list(self._rec(tid)['categories'])

    def unsorted_count(self):
        try:
            from . import store as _store
            all_ids = set(_store.load_bookmarks(self.kb))
        except Exception:
            all_ids = set(self.bookmarks)
        n = 0
        for tid in all_ids:
            r = self.bookmarks.get(str(tid))
            if not r or not r.get("categories"):
                n += 1
        return n

    def review_ids(self, threshold=0.60):
        """Bookmarks needing review: uncertain AND not yet reviewed.

        Marking a bookmark reviewed removes it here (it stays visible
        under All). Unsorted (no topic category), low confidence
        (< threshold, None counts as low), or never classified all need
        review until a human accepts them.
        """
        out = []
        try:
            from . import store as _store
            all_ids = [str(t) for t in _store.load_bookmarks(self.kb)]
        except Exception:
            all_ids = list(self.bookmarks)
        for tid in all_ids:
            r = self.bookmarks.get(str(tid))
            if r is None:
                out.append(str(tid))  # never classified yet
                continue
            if r.get("reviewed"):
                continue
            if not r.get("categories"):
                out.append(tid)
                continue
            conf = r.get("confidence")
            if conf is None:
                out.append(tid)
                continue
            try:
                if float(conf) < threshold:
                    out.append(tid)
            except (TypeError, ValueError):
                out.append(tid)
        return out

    def review_reason(self, tid, threshold=0.60):
        """Human-readable reason why tid needs review ('' if not)."""
        r = self.bookmarks.get(str(tid))
        if r is None:
            return "not classified yet"
        if r.get("reviewed"):
            return ""
        if not r.get("categories"):
            if r.get("confidence") is None:
                return "not classified yet"
            return "no topic category (Unsorted)"
        conf = r.get("confidence")
        if conf is None:
            return "no confidence score yet"
        try:
            if float(conf) < threshold:
                return "low confidence %.2f < %.2f" % (float(conf),
                                                       threshold)
        except (TypeError, ValueError):
            return "low confidence"
        return ""

    def confidence_counts(self):
        out = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for r in self.bookmarks.values():
            out[confidence_level(r.get("confidence"))] += 1
        return out

    def intent_count(self, intent):
        return sum(1 for r in self.bookmarks.values()
                   if r['intent'] == intent)
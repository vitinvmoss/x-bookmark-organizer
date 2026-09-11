"""GraphQL request layer (xarchive api.js, ported). No hard-coded IDs."""
from __future__ import annotations
import json
import urllib.parse
import urllib.request
from .util import BEARER, FALLBACK_FEATURES


def assert_no_delete(operation: str) -> None:
    if "delete" in operation.lower() and "bookmark" in operation.lower():
        raise RuntimeError("REFUSED: DeleteBookmark is never allowed.")


def build_headers(session: dict, ct0: str) -> dict:
    h = {"Authorization": session.get("authorization") or ("Bearer " + BEARER),
         "X-Csrf-Token": ct0, "X-Twitter-Active-User": "yes",
         "X-Twitter-Auth-Type": "OAuth2Session",
         "X-Twitter-Client-Language": "en",
         "Content-Type": "application/json",
         "Referer": "https://x.com/i/bookmarks",
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if session.get("x-client-uuid"):
        h["X-Client-Uuid"] = session["x-client-uuid"]
    if session.get("x-client-transaction-id"):
        h["X-Client-Transaction-Id"] = session["x-client-transaction-id"]
    return h


def features_for(session: dict, operation: str) -> dict:
    feats = dict(FALLBACK_FEATURES)
    cap = session.get("features_" + operation) or session.get("features")
    if cap:
        try:
            parsed = json.loads(cap) if isinstance(cap, str) else cap
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(v, bool):
                        feats[k] = v
        except Exception:
            pass
    return feats


def graphql_url(qid: str, op: str, variables: dict, session: dict) -> str:
    params = urllib.parse.urlencode(
        {"variables": json.dumps(variables, separators=(",", ":")),
         "features": json.dumps(features_for(session, op),
                                separators=(",", ":"))})
    return ("https://x.com/i/api/graphql/%s/%s?%s"
            % (urllib.parse.quote(qid), op, params))


def _call(method: str, qid: str, op: str, variables: dict, session: dict,
          ct0: str, cookie: str, timeout=30):
    assert_no_delete(op)
    headers = build_headers(session, ct0)
    headers["Cookie"] = cookie
    if method == "GET":
        url = graphql_url(qid, op, variables, session)
        req = urllib.request.Request(url, headers=headers, method="GET")
    else:
        url = ("https://x.com/i/api/graphql/%s/%s"
               % (urllib.parse.quote(qid), op))
        body = json.dumps({"variables": variables,
                           "features": features_for(session, op)}).encode()
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r), dict(r.headers.items())
    except Exception as e:
        import urllib.error
        if isinstance(e, urllib.error.HTTPError):
            try:
                text = e.read().decode("utf-8", "ignore")[:500]
            except Exception:
                text = ""
            return e.code, {"_http_error": text}, {}
        return -1, {"_network_error": str(e)}, {}


def graphql_get(qid, op, variables, session, ct0, cookie, timeout=30):
    return _call("GET", qid, op, variables, session, ct0, cookie, timeout)


def graphql_post(qid, op, variables, session, ct0, cookie, timeout=30):
    return _call("POST", qid, op, variables, session, ct0, cookie, timeout)

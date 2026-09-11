"""Query-ID discovery (strict, xarchive-style)."""
from __future__ import annotations
import re
import urllib.parse
import urllib.request
from .util import OPERATIONS

PAIR_QO = re.compile(
    r'queryId"\s*:\s*"([A-Za-z0-9_-]+)"\s*,\s*"operationName"\s*:\s*"([^"]+)"')
PAIR_OQ = re.compile(
    r'operationName"\s*:\s*"([^"]+)"\s*,\s*"queryId"\s*:\s*"([A-Za-z0-9_-]+)"')


def discover_ids_in_text(js):
    ids = {}
    text = js or ""
    for m in PAIR_QO.finditer(text):
        if m.group(2) in OPERATIONS:
            ids[m.group(2)] = m.group(1)
    for m in PAIR_OQ.finditer(text):
        if m.group(1) in OPERATIONS:
            ids[m.group(1)] = m.group(2)
    return ids

def discover_ids_from_url_path(url):
    import re as _re
    m = _re.search(r"/graphql/([^/]+)/([A-Za-z]+)", url or "")
    if not m or m.group(2) not in OPERATIONS:
        return {}
    return {m.group(2): m.group(1)}


def scrape_bundles(timeout=25):
    import urllib.parse as _up
    import urllib.request as _ur
    found = {}

    def get(url):
        try:
            req = _ur.Request(url, headers={"User-Agent": "xbo"})
            with _ur.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            return None

    for page in ("https://x.com", "https://x.com/i/bookmarks"):
        html = get(page)
        if not html:
            continue
        urls = sorted(set(_re.findall(r"https://[^\s\"'<>]+\.js", html)))
        good = []
        for u in urls:
            try:
                host = _up.urlparse(u).hostname or ""
            except Exception:
                continue
            if host == "abs.twimg.com" or host == "x.com":
                good.append(u)
        for u in good[:30]:
            js = get(u)
            if js:
                for k, v in discover_ids_in_text(js).items():
                    found[k] = v
    return found

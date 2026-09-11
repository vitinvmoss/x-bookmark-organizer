// ==UserScript==
// @name         X Bookmarks Collector (x-bookmark-organizer fallback)
// @namespace    https://github.com/x-bookmark-organizer
// @version      1.1.0
// @description  Passive X bookmark exporter. Reads Bookmarks GraphQL responses as you scroll; downloads bookmarks_raw.jsonl for: python xb.py import --fixture bookmarks_raw.jsonl. Works on /i/bookmarks and the Bookmarks tab of /i/history.
// @match        https://x.com/*
// @match        https://twitter.com/*
// @run-at       document-start
// @grant        none
// @noframes
// ==/UserScript==
// Adapted from SaveBox assets/collector.user.js (same envelope contract).
(function () {
  "use strict";
  var RE = /\/i\/api\/graphql\/[^/]+\/Bookmarks\b/;
  var captured = [], seen = {}, page = 0;
  function iso() { return new Date().toISOString().replace(/\.\d{3}Z$/, "Z"); }
  function instr(g) {
    if (!g || typeof g !== "object") return [];
    var d = g.data || g;
    var keys = ["bookmark_timeline_v2", "bookmark_timeline", "bookmarks"];
    for (var i = 0; i < keys.length; i++) {
      var tl = d[keys[i]];
      if (tl && tl.timeline && tl.timeline.instructions) {
        return tl.timeline.instructions;
      }
    }
    return [];
  }
  function ingest(data) {
    var ins = instr(data), tweets = 0, cursor = "";
    ins.forEach(function (n) {
      (n.entries || []).forEach(function (e) {
        if (!e || !e.entryId) return;
        if (e.entryId.indexOf("tweet-") === 0) {
          var id = e.entryId.replace("tweet-", "");
          if (!seen[id]) {
            seen[id] = 1; tweets++;
            captured.push({ captured_at: iso(), cursor: cursor,
              entry_id: e.entryId, page: page, raw: e,
              sort_index: e.sortIndex || "", status_id: id });
          }
        } else if (e.entryId.indexOf("cursor-bottom-") === 0) {
          cursor = (e.content && e.content.value) || cursor;
        }
      });
    });
    if (tweets) { page++; update(); }
  }
  var f = window.fetch;
  window.fetch = function () {
    var u = "";
    try { u = typeof arguments[0] === "string" ? arguments[0] : arguments[0].url || ""; }
    catch (e) {}
    var p = f.apply(this, arguments);
    if (u && RE.test(u)) {
      p.then(function (r) {
        try {
          r.clone().json().then(function (d) {
            try { ingest(d); } catch (e) {}
          }).catch(function () {});
        } catch (e) {}
      }).catch(function () {});
    }
    return p;
  };
  function count() {
    var n = 0;
    for (var k in seen) { n++; }
    return n;
  }
  var box = null, label = null;
  function update() { if (label) label.textContent = "Captured " + count() + " bookmarks"; }
  function locText() {
    try { return (location.pathname || "") + (location.search || "") + (location.hash || ""); }
    catch (e) { return ""; }
  }
  function isLegacyRoute() {
    try { return location.pathname.indexOf("/i/bookmarks") === 0; }
    catch (e) { return false; }
  }
  function isHistoryRoute() {
    try { return location.pathname.indexOf("/i/history") === 0; }
    catch (e) { return false; }
  }
  function domBookmarksSelected() {
    // Returns true = Bookmarks tab active, false = another tab active,
    // null = unknown. Uses generic ARIA signals only (no X-specific
    // class names, testids, or markup assumptions).
    try {
      var tabs = document.querySelectorAll('[role="tab"]');
      for (var i = 0; i < tabs.length; i++) {
        if (tabs[i].getAttribute("aria-selected") === "true") {
          var name = tabs[i].getAttribute("aria-label") || tabs[i].textContent || "";
          if (/bookmark/i.test(name)) return true;
          if (name.replace(/\s/g, "")) return false;
        }
      }
      var cur = document.querySelectorAll('a[aria-current="page"], [aria-selected="true"]');
      for (var j = 0; j < cur.length; j++) {
        var t = cur[j].getAttribute("aria-label") || cur[j].textContent || "";
        if (/bookmark/i.test(t)) return true;
      }
    } catch (e) {}
    return null;
  }
  function isBookmarksView() {
    // Legacy route is always the bookmarks view (no tabs involved).
    if (isLegacyRoute()) return true;
    if (!isHistoryRoute()) return false;
    // On /i/history the pathname prefix itself is "history", so only
    // inspect what follows it (subpath + query + hash) for tab signals.
    var rest = locText().slice("/i/history".length);
    if (/bookmark/i.test(rest)) return true;
    if (/like/i.test(rest)) return false;
    var dom = domBookmarksSelected();
    if (dom !== null) return dom;
    // Fail open on a bare /i/history with unreadable tabs so the
    // Download button still appears reliably; capture itself remains
    // gated on Bookmarks GraphQL responses (see RE above), so other
    // tabs simply capture nothing.
    return true;
  }
  function refresh() {
    if (!box) { init(); return; }
    try { box.style.display = isBookmarksView() ? "" : "none"; } catch (e) {}
  }
  function download() {
    var blob = new Blob([captured.map(function (o) {
      return JSON.stringify(o);
    }).join("\n") + "\n"], { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "bookmarks_raw.jsonl";
    a.click();
  }
  function init() {
    if (box) { refresh(); return; }
    if (!isBookmarksView()) return;
    if (!document.body) return;
    box = document.createElement("div");
    box.style.cssText = "position:fixed;z-index:999999;bottom:16px;right:16px;background:#15202b;color:#fff;padding:10px;border-radius:10px;font:13px sans-serif";
    label = document.createElement("div");
    var b = document.createElement("button");
    b.textContent = "Download bookmarks_raw.jsonl";
    b.onclick = download;
    box.appendChild(label); box.appendChild(b);
    document.body.appendChild(box); update();
    refresh();
  }
  // X is an SPA: tab switches and in-app navigation do not reload the
  // page, so re-check the current view on history changes, tab
  // attribute flips, and a periodic fallback.
  try {
    var _ps = history.pushState, _rs = history.replaceState;
    history.pushState = function () {
      var r = _ps.apply(this, arguments);
      setTimeout(refresh, 0);
      return r;
    };
    history.replaceState = function () {
      var r = _rs.apply(this, arguments);
      setTimeout(refresh, 0);
      return r;
    };
  } catch (e) {}
  window.addEventListener("popstate", function () { setTimeout(refresh, 0); });
  window.addEventListener("hashchange", function () { setTimeout(refresh, 0); });
  var _mt = null;
  function watchTabs() {
    try {
      var target = document.documentElement || document.body;
      if (!target || typeof MutationObserver !== "function") return;
      var mo = new MutationObserver(function () {
        if (_mt) return;
        _mt = setTimeout(function () { _mt = null; refresh(); }, 300);
      });
      mo.observe(target, { childList: true, subtree: true,
        attributes: true, attributeFilter: ["aria-selected", "aria-current"] });
    } catch (e) {}
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { watchTabs(); init(); });
  } else { watchTabs(); init(); }
  setInterval(refresh, 1500);
})();

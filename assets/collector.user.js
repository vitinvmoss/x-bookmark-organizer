// ==UserScript==
// @name         X Bookmarks Collector (x-bookmark-organizer fallback)
// @namespace    https://github.com/x-bookmark-organizer
// @version      1.0.0
// @description  Passive X bookmark exporter. Reads Bookmarks GraphQL responses as you scroll; downloads bookmarks_raw.jsonl for: python xb.py import --fixture bookmarks_raw.jsonl
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
    if (box || location.pathname.indexOf("/i/bookmarks") !== 0) return;
    box = document.createElement("div");
    box.style.cssText = "position:fixed;z-index:999999;bottom:16px;right:16px;background:#15202b;color:#fff;padding:10px;border-radius:10px;font:13px sans-serif";
    label = document.createElement("div");
    var b = document.createElement("button");
    b.textContent = "Download bookmarks_raw.jsonl";
    b.onclick = download;
    box.appendChild(label); box.appendChild(b);
    document.body.appendChild(box); update();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else { init(); }
  setInterval(function () {
    if (!box) init();
  }, 1500);
})();

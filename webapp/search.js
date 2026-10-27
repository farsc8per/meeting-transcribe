// search.js — the search box on index.html.
//
// Self-contained on purpose: index.html's own <script> owns the recording controls and
// the 2s /api/state poll, and nothing in here may touch either. No globals leave this
// IIFE except what the page already defines.
(function () {
  "use strict";

  var DEBOUNCE = 250;      // fast enough to feel live, slow enough not to query per keystroke
  var LIMIT = 30;

  var box = document.getElementById("sq");
  var kindbar = document.getElementById("skinds");
  var out = document.getElementById("sresults");
  if (!box || !out) return;

  var kind = "";
  var timer = null;
  var seq = 0;             // responses can land out of order; only the newest may draw

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // textContent, never innerHTML — see note below
    return e;
  }

  // H:MM:SS. Seconds into the call, so a transcript hit says where to scrub to.
  function hms(t) {
    var n = Math.max(0, Math.floor(Number(t) || 0));
    var h = Math.floor(n / 3600), m = Math.floor(n / 60) % 60, s = n % 60;
    return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
  }

  var KIND_LABEL = {
    summary: "summary", transcript_segment: "transcript", transcript: "transcript",
    recap: "recap", memory: "memory", note: "note"
  };

  function line(text) {
    out.innerHTML = "";
    out.appendChild(el("div", "empty", text));
    out.hidden = false;
  }

  function draw(r) {
    out.innerHTML = "";
    if (r.error) {
      var e = el("div", "err", r.error);
      out.appendChild(e);
      out.hidden = false;
      return;
    }
    if (!r.hits.length) {
      out.appendChild(el("div", "empty", "No matches for “" + r.q + "”."));
      out.hidden = false;
      return;
    }
    out.appendChild(el("div", "note", r.count + (r.count === 1 ? " match" : " matches")));
    r.hits.forEach(function (h) { out.appendChild(hitRow(h)); });
    out.hidden = false;
  }

  function hitRow(h) {
    var d = el("div", "hit");
    var top = el("div", "top");
    top.appendChild(el("div", "name", h.title || h.path || "(untitled)"));
    top.appendChild(el("span", "tag", KIND_LABEL[h.kind] || h.kind || "doc"));
    if (h.customer) top.appendChild(el("span", "tag cust", h.customer));
    if (h.date) top.appendChild(el("span", "meta", h.date));
    d.appendChild(top);

    // Second line: who said it and when, for a transcript segment; otherwise where it
    // lives, which is the only useful handle on a recap/memory/note hit.
    var sub = [];
    if (h.kind === "transcript_segment") {
      if (h.speaker) sub.push(h.speaker);
      if (h.t_start != null) sub.push(hms(h.t_start));
    } else if (h.path) {
      sub.push(h.path);
    }
    if (sub.length) d.appendChild(el("div", "meta", sub.join("  ·  ")));

    // THE ONE innerHTML IN THIS FILE, and it is deliberate: search.py escapes the source
    // text FIRST and only then wraps the matched terms in <mark>…</mark>, so the string
    // is already HTML-safe and the <mark> is the only live markup in it. Every other
    // field above — title, path, speaker, customer, date, kind — is raw text from disk
    // and goes in through textContent.
    var snip = el("div", "snip");
    snip.innerHTML = h.snippet || "";
    d.appendChild(snip);

    var dest = destination(h);
    if (dest) {
      d.className = "hit go";
      d.tabIndex = 0;
      d.setAttribute("role", "link");
      d.onclick = dest;
      d.onkeydown = function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); dest(); }
      };
    }
    return d;
  }

  // Where a hit goes is decided by its KIND, because the kind is what guarantees the
  // file is there. A `summary` hit IS a summary.md, and /api/summary now resolves pre-mt
  // meeting folders too, so every one of them opens. A `transcript_segment` hit IS a row
  // out of transcript.json, which is exactly what /review needs. A legacy pasted
  // `transcript` has neither guarantee — 94 of the 98 sit next to a summary.md, so it
  // opens that, and the other four land on the modal's own "not summarised yet" line
  // rather than a broken page. Everything else (recap, memory, note) has no page in this
  // app and must not look clickable: a dead hover is worse than a plain row.
  function destination(h) {
    if (!h.session) return null;
    var sid = h.session;
    if (h.kind === "transcript_segment") {
      return function () { location.href = "/review?session=" + encodeURIComponent(sid); };
    }
    if (h.kind === "summary" || h.kind === "transcript") {
      if (typeof window.showSummary !== "function") return null;
      return function () { window.showSummary(sid, h.title || sid); };
    }
    return null;
  }

  function run() {
    var q = box.value.trim();
    if (!q) { out.hidden = true; out.innerHTML = ""; return; }
    var mine = ++seq;
    var url = "/api/search?q=" + encodeURIComponent(q) +
              "&limit=" + LIMIT + (kind ? "&kind=" + encodeURIComponent(kind) : "");
    fetch(url)
      .then(function (r) {
        // A 404 here means a long-running `mt web` that predates the search route —
        // it serves this file from disk but has no /api/search. Say so plainly.
        if (!r.ok) { var e = new Error("http " + r.status); e.status = r.status; throw e; }
        return r.json();
      })
      .then(function (r) { if (mine === seq) draw(r); })
      .catch(function (e) {
        if (mine !== seq) return;
        line(e && e.status === 404
          ? "Search isn\u2019t in this server yet \u2014 restart mt web."
          : "Search is not answering.");
      });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(run, DEBOUNCE);
  }

  box.addEventListener("input", schedule);
  box.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { ev.preventDefault(); clearTimeout(timer); run(); }
  });

  if (kindbar) {
    kindbar.addEventListener("click", function (ev) {
      var b = ev.target.closest("button[data-kind]");
      if (!b) return;
      kind = b.getAttribute("data-kind") || "";
      Array.prototype.forEach.call(kindbar.querySelectorAll("button"), function (x) {
        x.className = (x === b) ? "on" : "";
      });
      clearTimeout(timer);
      run();
    });
  }
})();

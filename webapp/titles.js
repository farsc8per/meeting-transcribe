// titles.js — review drafted titles (R4.4c), one at a time, keyboard first.
//
// Enter saves: unchanged = accept the draft, changed = your edit. Alt+S skips (the item
// keeps its fallback label). Every decision is one POST and is final here; a title can
// always be changed later in the item's edit sheet. Drafts arrive 25 at a time.
(function () {
"use strict";

var $ = function (s) { return document.querySelector(s); };
var api = function (p, opts) { return fetch(p, opts).then(function (r) { return r.json(); }); };
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

var QUEUE = [], SEEN = {}, TOTAL = 0, DONE = 0, MAX = 120, busy = false, loading = false;

function fetchMore() {
  if (loading) return Promise.resolve();
  loading = true;
  return api("/api/tracking/titles?limit=25").then(function (r) {
    loading = false;
    if (!r.ready) {
      $("#card").innerHTML = "";
      $("#card").appendChild(el("div", "empty",
        "Titles are not on this database yet — run the R4.4c migration first."));
      return;
    }
    TOTAL = r.total + r.done;
    DONE = r.done;
    (r.drafts || []).forEach(function (d) {
      if (!SEEN[d.draft_id]) { SEEN[d.draft_id] = 1; QUEUE.push(d); }
    });
  }).catch(function () { loading = false; });
}

function progress() {
  var left = TOTAL - DONE;
  $("#progress").textContent = TOTAL ? DONE + " of " + TOTAL + " reviewed · " + left
    + " to go" : "";
}

function show() {
  progress();
  var box = $("#card");
  box.innerHTML = "";
  var d = QUEUE[0];
  if (!d) {
    box.appendChild(el("div", "empty", TOTAL ? "All drafted titles reviewed. Nice."
                                              : "No drafted titles waiting."));
    return;
  }
  if (QUEUE.length < 6) fetchMore();

  var c = el("div", "card ttl-card");
  var meta = el("div", "ttl-meta");
  meta.appendChild(el("span", "tag", "#" + d.id));
  if (d.customer) meta.appendChild(el("span", "tag cust", d.customer));
  if (d.project) meta.appendChild(el("span", "tag track", d.project));
  if (d.parent_id) meta.appendChild(el("span", "tag", "↳ #" + d.parent_id));
  meta.appendChild(el("span", "tag", d.status));
  if (d.owner) meta.appendChild(el("span", "tag", d.owner));
  if (d.due_date) meta.appendChild(el("span", "tag", "due " + d.due_date));
  var open = el("a", "ttl-open", "open item ↗");
  open.href = "/tracking?open=" + d.id;
  open.target = "mt-tracking";
  meta.appendChild(open);
  c.appendChild(meta);

  var row = el("div", "ttl-row");
  var inp = el("input", "ttl-in");
  inp.value = d.draft;
  inp.maxLength = MAX;
  inp.spellcheck = true;
  var count = el("span", "ttl-count");
  function recount() {
    var n = inp.value.trim().length;
    count.textContent = n + " / " + MAX;
    count.classList.toggle("long", n > 70);
    save.textContent = inp.value.trim() === d.draft ? "Accept" : "Save edit";
  }
  var save = el("button", "primary", "Accept");
  var skip = el("button", "tap", "Skip");
  row.appendChild(inp);
  row.appendChild(count);
  row.appendChild(save);
  row.appendChild(skip);
  c.appendChild(row);
  var err = el("div", "err");
  err.hidden = true;
  c.appendChild(err);

  c.appendChild(el("div", "ttl-lbl", "Description"));
  c.appendChild(el("div", "ttl-desc", d.description || ""));
  box.appendChild(c);

  inp.addEventListener("input", recount);
  inp.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); decide(d, inp.value, err); }
    if (e.altKey && e.code === "KeyS") { e.preventDefault(); decide(d, null, err); }
  });
  save.addEventListener("click", function () { decide(d, inp.value, err); });
  skip.addEventListener("click", function () { decide(d, null, err); });
  recount();
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

function decide(d, value, err) {
  if (busy) return;
  var t = value == null ? null : value.trim().replace(/\s+/g, " ");
  if (t === "") { err.textContent = "Empty — use Skip instead."; err.hidden = false; return; }
  busy = true;
  var body = t == null ? { id: d.draft_id, decision: "skip" }
    : t === d.draft ? { id: d.draft_id, decision: "accept" }
    : { id: d.draft_id, decision: "edit", final: t };
  api("/api/tracking/title/decide", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then(function (r) {
    busy = false;
    if (!r.ok) { err.textContent = r.why || "save failed"; err.hidden = false; return; }
    try { new BroadcastChannel("mt-tracking").postMessage({ type: "changed" }); } catch (e) {}
    QUEUE.shift();
    DONE++;
    var last = $("#last");
    last.textContent = "#" + d.id + "  " + (r.decision === "skip" ? "skipped"
      : (r.decision === "edit" ? "✎ " : "✓ ") + r.title);
    last.hidden = false;
    show();
  }).catch(function (e) { busy = false; err.textContent = String(e); err.hidden = false; });
}

fetchMore().then(show);
})();

// live.js — live updates for the tracking list and the Gantt (R4.4b).
//
// One small engine, used by both pages:
//   * Between mt windows: after a write, a page calls live.notify(); every other window
//     hears it on BroadcastChannel('mt-tracking') and checks the feed at once.
//   * From outside mt (assistant, scripts): a VISIBLE page asks /api/tracking/changes every
//     15 s. Hidden tabs never ask; becoming visible asks once.
//   * Pause live updates (Oct 9 — repaints have caused trouble before): a checkbox,
//     remembered per window. While paused nothing on screen changes; a badge counts what
//     is waiting and Apply brings it in.
//   * The page also pauses ITSELF while you are mid-action (busy() says so — an edit sheet
//     open, an input focused, a tooltip up) and applies the queue when you finish.
//   * Above the size cap the page is not patched automatically; the badge says
//     "changes available — refresh" instead.
//
// The page supplies apply(ids, linksOrProjectsMoved, full): patch the rows by id, or
// refetch when `full` (over the cap, or the feed overflowed).
(function () {
"use strict";

var POLL_MS = 15000;
var CAP = 300;

window.mtLive = function (opts) {
  var last = opts.since == null ? -1 : opts.since;
  var sig = null;
  var queue = {};                 // item id -> 1
  var sigMoved = false, overflow = false;
  var paused = false;
  var inflight = false;
  var timer = null;
  var ch = null;
  try { ch = new BroadcastChannel("mt-tracking"); } catch (e) { ch = null; }
  try { paused = sessionStorage.getItem("mt.live.paused." + opts.key) === "1"; } catch (e) {}

  // ---- controls: [ ] Pause live updates   [3 updates · Apply]
  var box = document.createElement("span");
  box.className = "live-ctl";
  var lab = document.createElement("label");
  lab.title = "Stop this window changing under you. Updates wait in the badge until Apply.";
  var cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = paused;
  lab.appendChild(cb);
  lab.appendChild(document.createTextNode(" Pause live updates"));
  box.appendChild(lab);
  var badge = document.createElement("button");
  badge.className = "live-badge";
  badge.hidden = true;
  box.appendChild(badge);
  if (opts.mount) opts.mount.appendChild(box);

  cb.addEventListener("change", function () {
    paused = cb.checked;
    try { sessionStorage.setItem("mt.live.paused." + opts.key, paused ? "1" : "0"); }
    catch (e) {}
    flush();
  });
  badge.addEventListener("click", function () { apply(true); });

  function pending() { return Object.keys(queue).length; }
  function overCap() { return (opts.rows ? opts.rows() : 0) > CAP || overflow; }

  function show() {
    var n = pending();
    var any = n || sigMoved || overflow;
    badge.hidden = !any;
    if (!any) return;
    if (overCap()) {
      badge.textContent = "changes available — refresh";
    } else {
      var what = n ? n + " update" + (n === 1 ? "" : "s") : "links changed";
      badge.textContent = what + " · Apply";
    }
    badge.title = paused ? "Paused — nothing changes until you click"
      : "Waiting until you finish what you are doing";
  }

  function apply(force) {
    var ids = Object.keys(queue).map(Number);
    var moved = sigMoved, full = overCap();
    if (!ids.length && !moved && !overflow) { show(); return; }
    if (!force && (paused || (opts.busy && opts.busy()) || full)) { show(); return; }
    queue = {};
    sigMoved = false;
    overflow = false;
    show();
    opts.apply(ids, moved, full);
  }
  function flush() { apply(false); }

  function poll() {
    if (inflight || document.hidden) return;
    inflight = true;
    fetch("/api/tracking/changes?since=" + last).then(function (r) { return r.json(); })
      .then(function (r) {
        inflight = false;
        if (!r || !r.ready) return;
        if (last >= 0) {
          (r.ids || []).forEach(function (id) { queue[id] = 1; });
          if (r.more) overflow = true;
          if (sig !== null && r.sig !== sig) sigMoved = true;
        }
        last = r.last;
        sig = r.sig;
        flush();
      }).catch(function () { inflight = false; });
  }

  function schedule() {
    clearInterval(timer);
    timer = setInterval(function () {
      if (!document.hidden) poll();
    }, POLL_MS);
  }

  if (ch) {
    ch.addEventListener("message", function (ev) {
      if ((ev.data || {}).type === "changed") setTimeout(poll, 150);
    });
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });
  // Mid-action pauses end without any event to hear, so look again every 1.5 s — only
  // when something is actually queued, and with no network.
  setInterval(function () { if (pending() || sigMoved || overflow) flush(); }, 1500);

  poll();
  schedule();

  return {
    // This window wrote: tell the others. (A BroadcastChannel never hears itself.)
    notify: function () { if (ch) ch.postMessage({ type: "changed" }); },
    // Start from a known event id (the Gantt gets one with its data).
    since: function (n) { if (n != null && n > last) last = n; },
    poll: poll,
    paused: function () { return paused; }
  };
};
})();

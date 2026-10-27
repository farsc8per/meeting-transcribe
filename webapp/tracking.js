// tracking.js — the action-items page.
//
// Vanilla, no dependencies, no build step, same as every other page here. Data is
// rendered with createElement + textContent; there is no innerHTML on anything that
// came out of the DB, because descriptions and owner strings are transcript-derived.
//
// The one idea worth knowing: every controlled field is a <select> built from
// /api/tracking/meta, never a hardcoded list and never a free-text box. That is what
// stops a new outlier spelling. A row that already holds a legacy value gets that value
// injected as an extra, clearly-marked option so the row stays editable without being
// forced into a cleanup — tracking.py accepts an unchanged value and refuses a changed
// one that is not canonical. (Oct 2026: no legacy values are left, so the filters no
// longer offer them and the Needs cleanup tab is gone; the Hygiene page catches drift.)
//
// R4.1 added: server-side sort that keeps families together, nesting shown as an indent,
// a project sidebar with roll-ups, quick views, owners grouped by contact with a Mine
// toggle, and click-to-edit due dates.
(function () {
"use strict";

var $ = function (s) { return document.querySelector(s); };
var LIVE = null;            // R4.4b live updates (live.js), started in boot()
var api = function (p, opts) {
  return fetch(p, opts).then(function (r) { return r.json(); }).then(function (r) {
    // Every write on this page goes through here, so this is the one place that tells
    // the other mt windows (the Gantt, a second list) to look at the change feed.
    if (LIVE && opts && opts.method === "POST" && p.indexOf("/api/tracking/") === 0
        && r && !r.error && r.ok !== false) {
      LIVE.notify();
    }
    return r;
  });
};
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

var META = { vocab: {}, customers: [], owners: {}, projects: [], no_project: {} };
var CONTACTS = [];
var ITEMS = [];
var TAB = "items";
var seq = 0;

// Selected ids, as a set. Deliberately cleared on every load(): if changing a filter kept
// the selection, you could bulk-write rows you can no longer see. Select-all does not
// reload, so the normal "filter down, select all, apply" flow is unaffected.
var SEL = Object.create(null);
function selCount() { return Object.keys(SEL).length; }

var FIELDS = ["status", "priority", "report_scope"];
var LABEL = { status: "Status", priority: "Priority", report_scope: "Report scope",
              project_id: "Project", parent_id: "Sub-item of" };

// ------------------------------------------------------------------ boot

function boot() {
  Promise.all([
    api("/api/tracking/meta"),
    api("/api/contacts?limit=1000").catch(function () { return { contacts: [] }; })
  ]).then(function (r) {
    var meta = r[0] || {};
    if (meta.error) return fail(meta.error);
    META = meta;
    CONTACTS = (r[1] && r[1].contacts) || [];
    loadSort();
    fillFilters();
    wire();
    load();
    listen();
    startLive();
    var want = +(new URLSearchParams(location.search).get("open") || 0);
    if (want) openById(want);
  }).catch(function (e) { fail(String(e)); });
}

// ------------------------------------------------------------------ the Gantt window (R4.4)

// Its own named window, so a second click brings the same one back rather than stacking
// windows. Scope is whatever the list is filtered to: a project wins over an account.
function openGantt() {
  var p = new URLSearchParams();
  var proj = $("#tproj").value, cust = $("#tcust").value;
  if (proj) p.set("project", proj);
  if (cust && cust !== "none" && (!proj || proj === "none")) p.set("customer", cust);
  if (!$("#thide").checked) p.set("hide_done", "0");
  var w = window.open("/tracking/gantt?" + p.toString(), "mt-gantt",
                      "width=1500,height=900");
  if (w) w.focus();
}

// openById() lives with the sequence chips (R4.3) below.

// Live updates (R4.4b). Changed rows on this page are patched in place by id — scroll,
// selection and focus are kept, the list is never rebuilt for an incoming change. A link
// or project change (chips, steps, roll-ups everywhere) or a big batch refreshes the page
// instead, in place. Changes to rows not on this page are counted, not fetched.
function startLive() {
  var mount = el("span", "livebar");
  $("#filters").insertBefore(mount, $("#tgantt"));
  var off = el("button", "live-badge off");
  off.hidden = true;
  off.addEventListener("click", function () { off.hidden = true; load("refresh"); });
  LIVE = window.mtLive({
    key: "list", mount: mount,
    rows: function () { return TAB === "items" ? ITEMS.length : 0; },
    busy: function () {
      if (sheetEl) return true;
      var a = document.activeElement;
      return !!(a && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== "checkbox");
    },
    apply: function (ids, moved, full) {
      if (TAB !== "items") {
        refreshMeta();
        // R4.8: open project panels refetch (live updates are already held while a
        // field in them has focus, so nothing changes under the cursor).
        (META.projects || []).forEach(function (p) { if (OPENPROJ[p.id]) reloadPanel(p); });
        return;
      }
      if (moved || full) { load("refresh"); refreshMeta(); return; }
      var here = {};
      ITEMS.forEach(function (it, i) { here[it.id] = i; });
      var mine = ids.filter(function (id) { return id in here; });
      var elsewhere = ids.length - mine.length;
      if (elsewhere) {
        off.textContent = elsewhere + " change" + (elsewhere === 1 ? "" : "s")
          + " not on this page · Refresh";
        off.hidden = false;
      }
      refreshMeta();
      if (!mine.length) return;
      api("/api/tracking/items?limit=500&q=" + encodeURIComponent(
          mine.map(function (id) { return "#" + id; }).join(" ")))
        .then(function (r) {
          (r.items || []).forEach(function (fresh) {
            var i = here[fresh.id];
            if (i == null) return;
            var old = ITEMS[i];
            fresh.depth = old.depth;          // position comes from THIS view's order
            fresh.kids = old.kids;
            if (old.step != null) fresh.step = old.step;
            ITEMS[i] = fresh;
            var node = document.querySelector('#list .row[data-id="' + fresh.id + '"]');
            if (node) node.replaceWith(rowFor(fresh));
          });
        });
    }
  });
  mount.appendChild(off);
}

// Same-origin windows talk over BroadcastChannel — no server work. The Gantt asks this
// page to open an item (this page owns editing) and waits for "opened"; if no list
// window answers, it opens one with ?open=.
var CH = null;
function listen() {
  try { CH = new BroadcastChannel("mt-tracking"); } catch (e) { return; }
  CH.addEventListener("message", function (ev) {
    var m = ev.data || {};
    if (m.type === "open" && m.id) {
      CH.postMessage({ type: "opened", id: m.id });
      try { window.focus(); } catch (e) {}
      openById(m.id);
    }
  });
}

function fail(msg) {
  var box = $("#list");
  box.innerHTML = "";
  box.appendChild(el("div", "err", msg));
}

// Rebuilt from META every time it changes (after any save), so it must be idempotent:
// each select is emptied back to its "any" option and its current value restored.
function refill(sel, build) {
  var keep = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  Array.prototype.slice.call(sel.querySelectorAll("optgroup")).forEach(function (g) {
    g.remove();
  });
  build(sel);
  sel.value = keep;
  if (sel.value !== keep) sel.value = "";
}

function fillFilters() {
  refill($("#tcust"), function (cust) {
    META.customers.forEach(function (c) {
      cust.appendChild(optionFor(c.short_name || "", c.short_name || c.name));
    });
    cust.appendChild(optionFor("none", "— no account —"));
  });

  fillProjectSelect();

  refill($("#tstatus"), function (sel) { fillVocabSelect(sel, "status"); });
  refill($("#tprio"), function (sel) { fillVocabSelect(sel, "priority"); });
  refill($("#tscope"), function (sel) { fillVocabSelect(sel, "report_scope"); });

  // Owners, grouped: one entry per linked person however many spellings their rows once
  // had, then the raw spellings still on unlinked rows so those stay reachable. Counts
  // are live / all.
  refill($("#towner"), function (own) {
    var o = META.owners || {};
    var n = function (x) { return "  (" + x.live + " / " + x.n + ")"; };
    if ((o.people || []).length) {
      var g = el("optgroup");
      g.label = "People";
      o.people.forEach(function (p) {
        g.appendChild(optionFor("c:" + p.id, p.name + n(p)));
      });
      own.appendChild(g);
    }
    if ((o.tail || []).length) {
      var t = el("optgroup");
      t.label = "Not linked to a contact";
      o.tail.forEach(function (p) {
        t.appendChild(optionFor("s:" + p.owner, (p.owner || "(blank)") + n(p)));
      });
      own.appendChild(t);
    }
  });

  refill($("#tdep"), function (sel) {
    (META.vocab.dep_filters || []).forEach(function (d) {
      sel.appendChild(optionFor(d.key, d.label));
    });
  });
  $("#tdep").hidden = !META.vocab.r42;

  refill($("#tdue"), function (sel) {
    (META.vocab.due_filters || []).forEach(function (d) {
      sel.appendChild(optionFor(d.key, d.label));
    });
  });
}

// The Project filter: grouped by account, A–Z, open counts; narrowed to the account picked
// in the Account filter (Oct 13).
function fillProjectSelect() {
  refill($("#tproj"), function (sel) {
    projOptions(sel, $("#tcust").value || null);
    sel.appendChild(optionFor("none", "— no project —"));
  });
}

function optionFor(value, text) {
  var o = el("option", null, text);
  o.value = value;
  return o;
}

function fillVocabSelect(sel, field) {
  (META.vocab[field] || []).forEach(function (v) {
    sel.appendChild(optionFor(v, v));
  });
}

// ------------------------------------------------------------------ sort

// Remembered per browser. The server owns the order (it is paged), so this is only the
// choice; an unknown key falls back to the server default.
var SORT = { key: "id", dir: "desc" };

function loadSort() {
  try {
    var v = JSON.parse(localStorage.getItem("mt.tracking.sort") || "null");
    if (v && sortDef(v.key)) SORT = { key: v.key, dir: v.dir === "asc" ? "asc" : "desc" };
  } catch (e) {}
}

function saveSort() {
  try { localStorage.setItem("mt.tracking.sort", JSON.stringify(SORT)); } catch (e) {}
}

function sortDef(key) {
  return (META.vocab.sorts || []).filter(function (x) { return x.key === key; })[0];
}

// Natural direction first (soonest due, highest priority, newest); a second click flips.
// Undated rows stay at the bottom either way — that is the server's rule.
var ARROW = {
  due: { asc: "soonest first", desc: "latest first" },
  start: { asc: "soonest first", desc: "latest first" },
  priority: { asc: "critical first", desc: "low first" },
  status: { asc: "open → done", desc: "done → open" },
  title: { asc: "A → Z", desc: "Z → A" },
  updated: { asc: "oldest first", desc: "newest first" },
  id: { asc: "oldest first", desc: "newest first" }
};

function renderSortbar() {
  var bar = $("#sortbar");
  bar.innerHTML = "";
  bar.appendChild(el("span", "lbl", "Sort"));
  (META.vocab.sorts || []).forEach(function (d) {
    var on = SORT.key === d.key;
    var b = el("button", on ? "on" : "", d.label + (on ? (SORT.dir === "asc" ? " ▲" : " ▼") : ""));
    b.title = on ? (ARROW[d.key] || {})[SORT.dir] + " — click to reverse"
                 : "sort by " + d.label.toLowerCase();
    b.addEventListener("click", function () {
      SORT = on ? { key: d.key, dir: SORT.dir === "asc" ? "desc" : "asc" }
                : { key: d.key, dir: d.dir };
      saveSort();
      load("sort");
    });
    bar.appendChild(b);
  });
  var how = (ARROW[SORT.key] || {})[SORT.dir];
  if (how) bar.appendChild(el("span", "note", how + "  ·  sub-items stay under their parent"));
}

// ------------------------------------------------------------------ quick views

// Each is a combination of the visible controls. Applying one clears status / priority /
// scope / owner / Mine / due and sets its own; account, project and search are kept, so
// "Due this week" inside the ACM POS project works. Clicking the active one clears it.
// "Ready to start" joins these once predecessors exist (R4.3).
var QUICK = [
  { key: "mine", label: "Mine", set: { mine: true } },
  { key: "myover", label: "My overdue", set: { mine: true, due: "overdue" } },
  { key: "week", label: "Due this week", set: { due: "week" } },
  { key: "waiting", label: "Waiting on others", set: { status: "waiting" },
    title: "status = waiting: you need someone else to come back to you" },
  { key: "ready", label: "Ready to start", set: { dep: "ready" },
    title: "open items whose predecessors are all finished (or marked met anyway)" }
];
var QFIELDS = { status: "#tstatus", priority: "#tprio", scope: "#tscope",
                owner: "#towner", due: "#tdue", dep: "#tdep" };

function quickActive(q) {
  if ($("#tmine").checked !== !!q.set.mine) return false;
  return Object.keys(QFIELDS).every(function (k) {
    return $(QFIELDS[k]).value === (q.set[k] || "");
  });
}

function applyQuick(q) {
  var off = quickActive(q);
  Object.keys(QFIELDS).forEach(function (k) {
    $(QFIELDS[k]).value = off ? "" : (q.set[k] || "");
  });
  $("#tmine").checked = off ? false : !!q.set.mine;
  syncMine();
  load();
}

function renderQuick() {
  var box = $("#quick");
  box.innerHTML = "";
  box.appendChild(el("span", "lbl", "Quick views"));
  QUICK.forEach(function (q) {
    if (q.set.dep && !META.vocab.r42) return;
    var b = el("button", quickActive(q) ? "on" : "", q.label);
    if (q.title) b.title = q.title;
    b.addEventListener("click", function () { applyQuick(q); });
    box.appendChild(b);
  });
  if (META.title_drafts) {
    var tl = el("a", "ttl-link", "✎ " + META.title_drafts + " title"
                + (META.title_drafts === 1 ? "" : "s") + " to review");
    tl.href = "/tracking/titles";
    box.appendChild(tl);
  }
}

// Mine and a specific owner would AND together into nothing, so Mine parks the owner
// picker rather than letting the two fight.
function syncMine() {
  var on = $("#tmine").checked;
  if (on) $("#towner").value = "";
  $("#towner").disabled = on;
}

function wire() {
  var t = null;
  $("#tq").addEventListener("input", function () {
    clearTimeout(t);
    t = setTimeout(load, 250);
  });
  $("#tcust").addEventListener("change", function () { fillProjectSelect(); load(); });
  ["#tproj", "#tstatus", "#tprio", "#tscope", "#towner", "#tdue", "#tdep",
   "#thide"].forEach(function (s) {
    $(s).addEventListener("change", load);
  });
  // Oct 13: "sometimes I forget that I had search terms." An × in the box (and Esc)
  // clears the search; Clear filters resets everything; filters that are on are outlined.
  $("#tqx").addEventListener("click", function () { $("#tq").value = ""; load(); $("#tq").focus(); });
  $("#tq").addEventListener("keydown", function (e) {
    if (e.key === "Escape" && $("#tq").value) { e.preventDefault(); $("#tq").value = ""; load(); }
  });
  $("#tclear").addEventListener("click", clearFilters);
  $("#tmine").addEventListener("change", function () { syncMine(); load(); });
  $("#tnew").addEventListener("click", function () { openSheet(null); });
  $("#tgantt").addEventListener("click", openGantt);

  Array.prototype.forEach.call($("#tabs").children, function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call($("#tabs").children, function (x) {
        x.classList.remove("on");
      });
      b.classList.add("on");
      TAB = b.dataset.tab;
      $("#filters").hidden = (TAB !== "items");
      $("#quick").hidden = (TAB !== "items");
      $("#sortbar").hidden = (TAB !== "items");
      $("#projside").hidden = (TAB !== "items");
      $("#trkbody").classList.toggle("noside", TAB !== "items");
      if (TAB !== "items") {
        SEL = Object.create(null);          // selections belong to the items list only
        $("#selbar").hidden = true;
        $("#bulkbar").hidden = true;
        showPagers(false);
      }
      if (TAB === "items") load(); else renderTab();
    });
  });
}

// ------------------------------------------------------------------ list

// Paged on the SERVER: a row is ~2 KB, so the whole list is 1.5 MB. pager.js holds the
// page size (20/50/100/All, remembered) and the position; the server returns one page and
// the true total it came from.
var PG = null;
var TOTAL = 0;
var STEPS = false;           // the server grouped this view into sequence steps

function pager() {
  if (PG) return PG;
  PG = window.mtPager("tracking", function () { load("page"); });
  var list = $("#list");
  list.parentNode.insertBefore(PG.mount(), list);
  list.parentNode.insertBefore(PG.mount(), list.nextSibling);
  return PG;
}

function showPagers(on) {
  document.querySelectorAll(".pager").forEach(function (b) { b.hidden = !on; });
}

// The filters, as query params. One function, used by the page request AND by "select
// all matching", so the two can never disagree about which rows a bulk write touches.
function filterParams() {
  var p = new URLSearchParams();
  var q = $("#tq").value.trim();
  if (q) p.set("q", q);
  [["customer", "#tcust"], ["project", "#tproj"], ["status", "#tstatus"],
   ["priority", "#tprio"], ["scope", "#tscope"], ["owner", "#towner"],
   ["due", "#tdue"], ["dep", "#tdep"]].forEach(function (pair) {
    var n = $(pair[1]);
    if (n.value && !n.disabled) p.set(pair[0], n.value);
  });
  if ($("#tmine").checked) p.set("mine", "1");
  if ($("#thide").checked) p.set("hide_done", "1");
  p.set("sort", SORT.key);
  p.set("dir", SORT.dir);
  return p;
}

// mode:
//   "page"     the pager moved. Filters are unchanged, so the selection survives —
//              the rows you ticked on page 1 still match and are still what you chose.
//   "refresh"  same filters, after a write. Stay on this page.
//   "sort"     same rows, new order. Back to page 1; the selection survives, because
//              the set of matching rows did not change.
//   anything   (including the Event a <select> passes) — the filters changed. Back to
//              page 1, and the selection is cleared: a selection must never include rows
//              the current filters no longer show.
function load(mode) {
  if (TAB !== "items") return;
  var pg = pager();
  showPagers(true);
  if (mode !== "page" && mode !== "refresh") pg.reset();
  if (mode !== "page" && mode !== "sort") SEL = Object.create(null);
  renderQuick();
  renderSortbar();
  renderSide();
  markFilters();
  var mine = ++seq;
  var p = filterParams();
  p.set("limit", String(pg.limit || 2000));
  p.set("offset", String(pg.offset));
  api("/api/tracking/items?" + p.toString()).then(function (r) {
    if (mine !== seq) return;                 // a slower earlier request must not draw
    if (r.error) return fail(r.error);
    ITEMS = r.items || [];
    STEPS = !!r.steps;
    TOTAL = r.total != null ? r.total : ITEMS.length;
    if (r.sort && (r.sort !== SORT.key || r.dir !== SORT.dir)) {
      SORT = { key: r.sort, dir: r.dir };    // the server fell back; show what it did
      renderSortbar();
    }
    pg.setTotal(TOTAL);
    renderStats(r.stats || {});
    renderList();
  });
}

var FILTER_IDS = ["#tcust", "#tproj", "#tstatus", "#tprio", "#tscope", "#towner", "#tdue", "#tdep"];
function activeFilters() {
  var n = 0;
  if ($("#tq").value.trim()) n++;
  FILTER_IDS.forEach(function (s) { if ($(s).value && !$(s).disabled) n++; });
  if ($("#tmine").checked) n++;
  if (!$("#thide").checked) n++;
  return n;
}
function markFilters() {
  $("#tq").classList.toggle("on", !!$("#tq").value.trim());
  $("#tqx").hidden = !$("#tq").value;
  FILTER_IDS.forEach(function (s) { $(s).classList.toggle("on", !!$(s).value); });
  $("#tminewrap").classList.toggle("on", $("#tmine").checked);
  $("#thidewrap").classList.toggle("on", !$("#thide").checked);
  var n = activeFilters();
  $("#tclear").hidden = !n;
  $("#tclear").textContent = "Clear filters (" + n + ")";
}
function clearFilters() {
  $("#tq").value = "";
  FILTER_IDS.forEach(function (s) { $(s).value = ""; });
  $("#tmine").checked = false;
  $("#thide").checked = true;
  syncMine();
  fillProjectSelect();
  load();
}

function renderStats(s) {
  var box = $("#stats");
  box.innerHTML = "";
  var cells = [
    ["total", s.total], ["open", s.open], ["overdue", s.overdue],
    ["untriaged", s.unset_scope], ["owner spellings", s.distinct_owners]
  ];
  cells.forEach(function (c) {
    var flag = c[0] === "overdue" && c[1] > 0;
    var d = el("div", flag ? "stat flag" : "stat");
    d.appendChild(el("b", null, String(c[1] == null ? "\u2014" : c[1])));
    d.appendChild(document.createTextNode(c[0]));
    box.appendChild(d);
  });
}

function renderList() {
  var box = $("#list");
  box.innerHTML = "";
  var hid = $("#thide").checked && !$("#tstatus").value;
  $("#count").textContent = TOTAL
    ? TOTAL + " item" + (TOTAL === 1 ? "" : "s") + " match"
      + (hid ? "  ·  finished items hidden" : "")
    : "";
  if (!ITEMS.length) {
    box.appendChild(el("div", "empty", "nothing matches those filters"));
    renderSel();
    return;
  }
  // Inside a project the list reads as a sequence: a header each time the step changes
  // (top-level rows only — sub-items stay under their parent whatever their step).
  var lastStep = null;
  ITEMS.forEach(function (it) {
    if (STEPS && !it.depth && it.step !== lastStep) {
      lastStep = it.step;
      box.appendChild(el("div", "stephead", "Step " + it.step
        + (it.step === 1 ? " — nothing before these" : "")));
    }
    box.appendChild(rowFor(it));
  });
  renderSel();
}

// After any write: counts, owner groups and project roll-ups may all have moved.
function refreshMeta() {
  api("/api/tracking/meta").then(function (m) {
    if (!m.error) {
      META = m;
      fillFilters();
    }
    load("refresh");
  });
}

// ------------------------------------------------------------------ project sidebar

// ------------------------------------------------------------------ project lists (Oct 13)

// Oct 13: the project lists (left pane, Projects tab, the Project dropdowns) "can
// get pretty large and difficult to manage". One sort + one text match, used by all of
// them; each place remembers its own choices.
var PSORTS = [["name", "A–Z"], ["account", "Account"], ["open", "Most open"],
              ["overdue", "Overdue first"], ["newest", "Newest"]];
function pstore(k, v) {
  try {
    if (v === undefined) return JSON.parse(localStorage.getItem("mt.tracking." + k) || "null");
    localStorage.setItem("mt.tracking." + k, JSON.stringify(v));
  } catch (e) { return null; }
}
function custName(short) {
  var c = (META.customers || []).filter(function (x) { return x.short_name === short; })[0];
  return c ? c.name : (short || "");
}
function projSort(list, key) {
  var byName = function (a, b) { return (a.name || "").localeCompare(b.name || ""); };
  var out = list.slice();
  out.sort(function (a, b) {
    if (key === "account") {
      var ca = a.customer ? custName(a.customer) : "\uffff", cb = b.customer ? custName(b.customer) : "\uffff";
      return ca.localeCompare(cb) || byName(a, b);
    }
    if (key === "open") return (b.live || 0) - (a.live || 0) || byName(a, b);
    if (key === "overdue") return (b.overdue || 0) - (a.overdue || 0) || (b.live || 0) - (a.live || 0) || byName(a, b);
    if (key === "newest") return (b.id || 0) - (a.id || 0);
    return byName(a, b);
  });
  return out;
}
function projMatch(p, q) {
  q = (q || "").trim().toLowerCase();
  if (!q) return true;
  // Name, account (name / short / code) and #id — not the notes: a short query like "ace"
  // sits inside everyday words ("replace", "surface") and would match half the list.
  return [p.name, p.customer, custName(p.customer), p.customer ? codeForCustomer(p.customer) : "",
          "#" + p.id].some(function (s) { return (s || "").toLowerCase().indexOf(q) >= 0; });
}
// Options for a Project <select>: one optgroup per account (A–Z), projects A–Z inside with
// their open count; projects with no account last. `only` limits to one account's short
// name ("none" = no account).
function projOptions(sel, only) {
  var groups = {};
  (META.projects || []).forEach(function (p) {
    if (only && (only === "none" ? p.customer : p.customer !== only)) return;
    var k = p.customer || "";
    (groups[k] = groups[k] || []).push(p);
  });
  Object.keys(groups).sort(function (a, b) {
    if (!a) return 1;
    if (!b) return -1;
    return custName(a).localeCompare(custName(b));
  }).forEach(function (k) {
    var g = el("optgroup");
    g.label = k ? custName(k) : "No account";
    projSort(groups[k], "name").forEach(function (p) {
      g.appendChild(optionFor(String(p.id), p.name + "  (" + (p.live || 0) + " open)"));
    });
    sel.appendChild(g);
  });
}

// Every active project with its roll-up. Clicking one is the same as picking it in the
// Project filter — the two are one control drawn twice. The span is due dates only
// until R4.2 gives items a start and an end.
// Oct 13: a filter box, a sort and "Hide finished" sit above the list. They are built
// once and kept (typing must not lose focus when the list redraws); the project picked
// in the Project filter always stays visible, filtered or not.
var SIDE = pstore("side") || { q: "", sort: "name", hide: false };
function sideControls(box) {
  var c = box.querySelector(".side-ctl");
  if (c) return c;
  box.innerHTML = "";
  box.appendChild(el("div", "lbl", "Projects"));
  c = el("div", "side-ctl");
  var q = el("input");
  q.type = "search";
  q.placeholder = "Filter projects…";
  q.value = SIDE.q || "";
  q.addEventListener("input", function () { SIDE.q = q.value; pstore("side", SIDE); renderSide(); });
  var srt = el("select");
  PSORTS.forEach(function (s) { srt.appendChild(optionFor(s[0], s[1])); });
  srt.value = SIDE.sort || "name";
  srt.addEventListener("change", function () { SIDE.sort = srt.value; pstore("side", SIDE); renderSide(); });
  var hl = el("label");
  var hc = el("input");
  hc.type = "checkbox";
  hc.checked = !!SIDE.hide;
  hc.addEventListener("change", function () { SIDE.hide = hc.checked; pstore("side", SIDE); renderSide(); });
  hl.appendChild(hc);
  hl.appendChild(document.createTextNode(" Hide finished"));
  hl.title = "Hide projects with nothing open";
  c.appendChild(q);
  c.appendChild(srt);
  c.appendChild(hl);
  box.appendChild(c);
  box.appendChild(el("div", "side-list"));
  return c;
}

function renderSide() {
  var outer = $("#projside");
  sideControls(outer);
  var box = outer.querySelector(".side-list");
  box.innerHTML = "";
  var cur = $("#tproj").value;

  function entry(value, name, roll, cust) {
    var b = el("button", "proj" + (cur === value ? " on" : "")
                         + (value && roll && !roll.live ? " quiet" : ""));
    var top = el("div", "pn");
    top.appendChild(el("span", "pname", name));
    if (cust) top.appendChild(el("span", "tag cust", cust));
    b.appendChild(top);
    if (roll) {
      var line = el("div", "pr");
      line.appendChild(el("span", null, roll.live + " open · " + roll.done + " done"));
      if (roll.overdue) line.appendChild(el("span", "od", roll.overdue + " overdue"));
      b.appendChild(line);
      var span = spanText(roll.due_min, roll.due_max);
      if (span) b.appendChild(el("div", "pr span", span));
    }
    b.addEventListener("click", function () {
      var want = cur === value ? "" : value;
      // The Project dropdown follows the Account filter; picking a project from another
      // account here clears that filter rather than silently matching nothing.
      var p = (META.projects || []).filter(function (x) { return String(x.id) === want; })[0];
      var acct = $("#tcust").value;
      if (p && acct && (acct === "none" ? p.customer : p.customer !== acct)) {
        $("#tcust").value = "";
        fillProjectSelect();
      }
      $("#tproj").value = want;
      load();
    });
    box.appendChild(b);
  }

  entry("", "All items", null, null);
  var all = META.projects || [];
  var shown = projSort(all.filter(function (p) {
    if (String(p.id) === cur) return true;
    if (SIDE.hide && !p.live) return false;
    return projMatch(p, SIDE.q);
  }), SIDE.sort);
  var lastAcct = null;
  shown.forEach(function (p) {
    if (SIDE.sort === "account" && (p.customer || "") !== lastAcct) {
      lastAcct = p.customer || "";
      box.appendChild(el("div", "side-grp", lastAcct ? custName(lastAcct) : "No account"));
    }
    entry(String(p.id), p.name, p, SIDE.sort === "account" ? null : p.customer);
  });
  if (shown.length < all.length) {
    box.appendChild(el("div", "side-n", "showing " + shown.length + " of " + all.length));
  }
  if (!SIDE.q) entry("none", "No project", META.no_project || null, null);
}

var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
           "Dec"];
function shortDate(d) {
  if (!d || d.length < 10) return "";
  var y = d.slice(0, 4), m = MON[+d.slice(5, 7) - 1], day = +d.slice(8, 10);
  var thisYear = String(new Date().getFullYear());
  return m + " " + day + (y === thisYear ? "" : " " + y);
}
function spanText(a, b) {
  if (!a) return "";
  return a === b ? "due " + shortDate(a) : "due " + shortDate(a) + " → " + shortDate(b);
}

// ------------------------------------------------------------------ bulk

function renderSel() {
  var bar = $("#selbar");
  bar.innerHTML = "";
  bar.hidden = !ITEMS.length;
  if (!ITEMS.length) { renderBulk(); return; }

  var all = ITEMS.every(function (i) { return SEL[i.id]; });
  var l = el("label");
  var cb = el("input");
  cb.type = "checkbox";
  cb.checked = all;
  cb.addEventListener("change", function () {
    ITEMS.forEach(function (i) {
      if (cb.checked) SEL[i.id] = 1; else delete SEL[i.id];
    });
    renderList();
  });
  l.appendChild(cb);
  l.appendChild(document.createTextNode("Select all " + ITEMS.length + " on this page"));
  bar.appendChild(l);

  // Rows on other pages are real rows the filter matches, so "all matching" is offered
  // explicitly rather than implied. It fetches ids only — a few KB, not 1.5 MB.
  if (TOTAL > ITEMS.length) {
    var all2 = el("button", "tap", "Select all " + TOTAL + " matching");
    all2.addEventListener("click", function () {
      all2.disabled = true;
      var p = filterParams();
      p.set("limit", "1");
      p.set("ids", "1");
      api("/api/tracking/items?" + p.toString()).then(function (r) {
        (r.ids || []).forEach(function (id) { SEL[id] = 1; });
        renderList();
      });
    });
    bar.appendChild(all2);
  }

  if (selCount()) {
    var clear = el("button", "tap", "Clear");
    clear.addEventListener("click", function () {
      SEL = Object.create(null);
      renderList();
    });
    bar.appendChild(clear);
  }
  renderBulk();
}

function renderBulk() {
  var bar = $("#bulkbar");
  bar.innerHTML = "";
  var n = selCount();
  bar.hidden = !n;
  if (!n) return;

  // With paging, a selection can include rows that are not on screen. Say so, every time,
  // so "Apply to 180" is never a surprise when only 20 rows are visible.
  var onPage = ITEMS.filter(function (i) { return SEL[i.id]; }).length;
  bar.appendChild(el("b", null, n + " selected"));
  if (onPage < n) {
    bar.appendChild(el("span", "note", "(" + (n - onPage) + " on other pages)"));
  }
  bar.appendChild(document.createTextNode("set"));

  var field = el("select");
  ["status", "priority", "report_scope", "project_id", "parent_id"].forEach(function (f) {
    field.appendChild(optionFor(f, LABEL[f]));
  });
  bar.appendChild(field);
  bar.appendChild(document.createTextNode("to"));

  // Canonical values only. A bulk write is an explicit choice about these exact rows, so
  // the "unchanged legacy value" allowance that single-row edits get does not apply —
  // tracking.py refuses a legacy value here and the picker never offers one.
  // Project and Sub-item of go through the same shape rules as the edit sheet, row by row.
  var value = el("select");
  var parentIn = el("input");
  parentIn.type = "text";
  parentIn.placeholder = "#id — empty = top level";
  parentIn.className = "pid";
  function fillValue() {
    value.innerHTML = "";
    var f = field.value;
    value.hidden = f === "parent_id";
    parentIn.hidden = f !== "parent_id";
    if (f === "project_id") {
      (META.projects || []).forEach(function (p) {
        value.appendChild(optionFor(String(p.id), p.name));
      });
      value.appendChild(optionFor("", "— no project —"));
      return;
    }
    (META.vocab[f] || []).forEach(function (v) {
      value.appendChild(optionFor(v, v));
    });
    if (f === "status") value.value = "done";
  }
  field.addEventListener("change", fillValue);
  fillValue();
  bar.appendChild(value);
  bar.appendChild(parentIn);

  var err = el("span", "grow");
  bar.appendChild(err);

  var apply = el("button", "primary", "Apply to " + n);
  apply.addEventListener("click", function () {
    doBulk(field.value, field.value === "parent_id" ? parentIn.value.trim() : value.value,
           apply, err);
  });
  // tracking.py caps a bulk write at 500 and would refuse anyway; say it here, before the
  // confirm dialog, rather than after.
  if (n > BULK_MAX) {
    apply.disabled = true;
    err.textContent = BULK_MAX + " is the most in one go — narrow the filter, or apply "
      + "in slices.";
  }
  bar.appendChild(apply);
}

var BULK_MAX = 500;

function doBulk(field, value, btn, err) {
  var ids = Object.keys(SEL).map(Number);
  if (!ids.length) return;
  var what = LABEL[field].toLowerCase();
  var shown = value;
  if (field === "project_id") {
    var pr = (META.projects || []).filter(function (p) { return String(p.id) === value; })[0];
    shown = pr ? pr.name : "no project";
  } else if (field === "parent_id") {
    shown = value ? "#" + value.replace(/^#/, "") : "top level (no parent)";
  }
  var note = field === "project_id"
    ? "\n\nA parent's sub-items move with it. A sub-item whose parent is not moving is "
      + "skipped (it follows its parent) — detach it first with Sub-item of → empty."
    : field === "parent_id" && value
    ? "\n\nEach item takes that parent's project. Items that have sub-items of their own "
      + "are skipped — nesting stops at one level."
    : "";
  var off = ids.length - ITEMS.filter(function (i) { return SEL[i.id]; }).length;
  if (!window.confirm((off > 0 ? off + " of these are on other pages.\n\n" : "") +
        "Set " + what + " = \u201c" + shown + "\u201d on " + ids.length
        + " item" + (ids.length === 1 ? "" : "s") + "?" + note + "\n\n"
        + "This writes to the database and cannot be undone from this page.")) {
    return;
  }
  var fields = {};
  fields[field] = value;
  btn.disabled = true;
  err.textContent = "";
  api("/api/tracking/bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids: ids, fields: fields })
  }).then(function (r) {
    btn.disabled = false;
    if (!r.ok) { err.textContent = r.why || "bulk update failed"; return; }
    var carried = (r.carried || []).length;
    toast(r.changed.length + " updated"
      + (carried ? ", " + carried + " sub-item" + (carried === 1 ? "" : "s") + " moved with "
         + "their parent" : "")
      + (r.skipped.length ? ", " + r.skipped.length + " skipped" : ""));
    // Skipped rows say why — "follows its parent's project" is something to act on, not
    // a number to shrug at. Kept on screen until the next action.
    var real = r.skipped.filter(function (x) { return x.why !== "already there"; });
    if (real.length) {
      var box = $("#bulknote");
      box.innerHTML = "";
      box.hidden = false;
      box.appendChild(el("b", null, real.length + " not moved:"));
      var ul = el("ul");
      real.slice(0, 12).forEach(function (x) { ul.appendChild(el("li", null, "#" + x.id + " — " + x.why)); });
      if (real.length > 12) ul.appendChild(el("li", null, "… and " + (real.length - 12) + " more"));
      box.appendChild(ul);
      var x = el("button", "tap", "Dismiss");
      x.addEventListener("click", function () { box.hidden = true; });
      box.appendChild(x);
    }
    SEL = Object.create(null);
    refreshMeta();
  }).catch(function (e) {
    btn.disabled = false;
    err.textContent = String(e);
  });
}

function toast(msg) {
  var t = el("div", "toast float", msg);
  document.body.appendChild(t);
  setTimeout(function () { t.remove(); }, 3400);
}

var DONE = { done: 1, closed: 1, completed: 1, wont_do: 1 };

function rowFor(it) {
  var row = el("div", "row");
  // Nesting, the published tracker's convention: indent under the parent, and a ↳ #N
  // chip. depth comes from the server and counts only within this view — a child whose
  // parent the filters hid sits at the left edge but still says whose child it is.
  if (it.depth) {
    row.classList.add("nested");
    row.style.marginLeft = Math.min(it.depth, 3) * 26 + "px";
  }

  var pick = el("input", "pick");
  pick.type = "checkbox";
  pick.checked = !!SEL[it.id];
  // The row itself opens the edit sheet, so ticking a box must not also open it.
  pick.addEventListener("click", function (e) { e.stopPropagation(); });
  pick.addEventListener("change", function () {
    if (pick.checked) SEL[it.id] = 1; else delete SEL[it.id];
    row.classList.toggle("sel", pick.checked);
    renderSel();
  });
  row.appendChild(pick);
  if (pick.checked) row.classList.add("sel");

  row.appendChild(dueCell(it));

  var mid = el("div", "mid");
  // R4.4c: the title is the headline; the description's start sits under it, one line,
  // so the detail is a glance away. Untitled rows show the description as before.
  if (it.title) {
    mid.appendChild(el("div", "name titled", it.title));
    mid.appendChild(el("div", "excerpt", (it.description || "").replace(/\s+/g, " ")
                                              .slice(0, 220)));
  } else {
    mid.appendChild(el("div", "name", (it.description || "").slice(0, 160)));
  }
  var sub = el("div", "sub");
  if (it.parent_id) {
    var up = el("button", "chip", "↳ #" + it.parent_id);
    up.title = "sub-item of #" + it.parent_id + " — click to open it";
    up.addEventListener("click", function (e) {
      e.stopPropagation();
      api("/api/tracking/item?id=" + it.parent_id).then(function (r) {
        if (r.item) openSheet(r.item);
      });
    });
    sub.appendChild(up);
  }
  var bits = ["#" + it.id];
  if (it.start_date || it.end_date) {
    bits.push("plan " + shortDate(it.start_date || "") + " → " + shortDate(it.end_date || ""));
  }
  if (it.owner) bits.push(it.owner);
  if ((it.with || []).length) {
    bits.push("with " + it.with.map(function (p) { return p.name; }).join(", "));
  }
  if (it.source) bits.push(it.source.slice(0, 60));
  sub.appendChild(document.createTextNode(bits.join("  ·  ")));
  mid.appendChild(sub);
  var seq = seqChips(it);
  if (seq) mid.appendChild(seq);
  row.appendChild(mid);
  row.dataset.id = it.id;
  var linked = [].concat((it.after || []).map(function (x) { return x.id; }),
                         (it.unblocks || []).map(function (x) { return x.id; }));
  if (linked.length) {
    row.addEventListener("mouseenter", function () { hiRows(linked, true); });
    row.addEventListener("mouseleave", function () { hiRows(linked, false); });
  }

  var right = el("div", "right");
  if (it.kids) {
    var k = el("span", "tag kids", it.kids + " sub");
    k.title = it.kids + " sub-item" + (it.kids === 1 ? "" : "s") + " in this view, listed below";
    right.appendChild(k);
  }
  if (it.promoted_to_project_id) {
    right.appendChild(el("span", "tag type", "→ project #" + it.promoted_to_project_id));
  }
  if (it.item_type && it.item_type !== "task") {
    right.appendChild(el("span", "tag type", it.item_type === "milestone" ? "◆ milestone"
                                                                         : it.item_type));
  }
  if (it.customer) right.appendChild(el("span", "tag cust", it.customer));
  right.appendChild(el("span", "tag " + (DONE[it.status] ? "ok" : "todo"), it.status));
  if (it.priority && it.priority !== "normal") {
    right.appendChild(el("span", "tag", it.priority));
  }
  if (it.report_scope && it.report_scope !== "unset") {
    right.appendChild(el("span", "tag", it.report_scope));
  }
  if (it.legacy_fields && it.legacy_fields.length) {
    var w = el("span", "tag unconf", "legacy");
    w.title = "legacy value in: " + it.legacy_fields.join(", ");
    right.appendChild(w);
  }
  if (isOverdue(it)) {
    var o = el("span", "tag unconf", "overdue");
    right.appendChild(o);
  }
  row.appendChild(right);

  // The sheet works on the row as it is NOW, not as the list drew it: a fresh read gets
  // the current rev (fewer needless conflicts) and the full sub-item count.
  row.addEventListener("click", function () {
    api("/api/tracking/item?id=" + it.id).then(function (r) {
      openSheet(r.item || it);
    }).catch(function () { openSheet(it); });
  });
  return row;
}

// Click a due date to change it in place; no edit sheet. The picker saves when you pick
// (or leave the field), Escape puts it back. Typing a date fires `change` for every
// partial year on the way to 2026 (0002, 0020, 0202 ...), so only a plausible year saves
// and the save waits a beat for the typing to finish.
function dueCell(it) {
  var cell = el("div", "when due" + (it.due_date ? "" : " empty"));
  cell.textContent = it.due_date || "+ due";
  cell.title = it.due_date ? "due " + it.due_date + " — click to change"
                           : "no due date — click to set one";
  cell.addEventListener("click", function (e) {
    e.stopPropagation();
    if (cell.querySelector("input")) return;
    var was = it.due_date || "";
    var inp = el("input");
    inp.type = "date";
    inp.value = was;
    cell.textContent = "";
    cell.classList.add("editing");
    cell.appendChild(inp);
    inp.addEventListener("click", function (ev) { ev.stopPropagation(); });
    var t = null, done = false;
    function restore() {
      done = true;
      clearTimeout(t);
      cell.classList.remove("editing");
      cell.textContent = it.due_date || "+ due";
    }
    function plausible(v) {
      return v === "" || (/^\d{4}-\d{2}-\d{2}$/.test(v) && +v.slice(0, 4) >= 2000
                          && +v.slice(0, 4) <= 2100);
    }
    function commit() {
      if (done) return;
      var v = inp.value;
      if (v === was || !plausible(v)) return restore();
      done = true;
      clearTimeout(t);
      inp.disabled = true;
      api("/api/tracking/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: it.id, fields: { due_date: v }, rev: it.rev })
      }).then(function (r) {
        if (r.conflict) {
          restore();
          toast("#" + it.id + " changed since the list loaded — refreshed, try again");
          refreshMeta();
          return;
        }
        if (!r.ok) {
          restore();
          toast("Not saved — " + (r.why || "save failed"));
          return;
        }
        toast("#" + it.id + (v ? " due " + v : " due date cleared"));
        refreshMeta();
      }).catch(function (err) { restore(); toast("Not saved — " + err); });
    }
    inp.addEventListener("change", function () {
      clearTimeout(t);
      if (plausible(inp.value)) t = setTimeout(commit, 700);
    });
    inp.addEventListener("blur", commit);
    inp.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.stopPropagation(); restore(); }
      if (ev.key === "Enter") commit();
    });
    inp.focus();
    try { inp.showPicker(); } catch (err) {}
  });
  return cell;
}

// ------------------------------------------------------------------ sequence (R4.3)

function openById(id) {
  api("/api/tracking/item?id=" + id).then(function (r) { if (r.item) openSheet(r.item); });
}

function idChip(x, cls) {
  var b = el("button", "chip" + (cls ? " " + cls : "") + (x.done ? " done" : "")
                     + (x.met ? " met" : ""), "#" + x.id + (x.met ? " ✓" : ""));
  b.title = x.done ? "#" + x.id + " is finished" : x.met ? "#" + x.id + " is still open, "
    + "but this link is marked met anyway" : "open #" + x.id;
  b.addEventListener("click", function (e) { e.stopPropagation(); openById(x.id); });
  return b;
}

// after #12 #15 · → unblocks #20 · waits on #12 | ready · ⚠ conflict
//
// Waiting on an unfinished predecessor is the NORMAL state of a planned sequence, so it is
// a quiet grey "waits on", not an alert (Oct 13: red "blocked by" "looks like it's an
// alert we need to take care of"). Red stays for real problems: a status set to blocked (an
// outside blocker) and a ⚠ date conflict. Green "ready" = every predecessor finished.
function seqChips(it) {
  var a = it.after || [], u = it.unblocks || [], bl = it.blocked_by || [],
      cf = it.conflicts || [];
  if (!a.length && !u.length) return null;
  var line = el("div", "seq");
  if (a.length) {
    line.appendChild(el("span", "lbl", "after"));
    a.forEach(function (x) { line.appendChild(idChip(x)); });
  }
  if (u.length) {
    line.appendChild(el("span", "lbl", "→ unblocks"));
    u.forEach(function (x) { line.appendChild(idChip(x)); });
  }
  if (bl.length && !DONE[it.status]) {
    var t = el("span", "tag wait", "waits on " + bl.map(function (i) { return "#" + i; })
                                                  .join(" "));
    t.title = "a predecessor is still open — normal for a sequence. If work can go ahead "
      + "anyway, mark the link met anyway in the item";
    line.appendChild(t);
  } else if (a.length && it.status === "open") {
    var rd = el("span", "tag ready", "ready");
    rd.title = "every predecessor is finished (or marked met anyway) — this can start";
    line.appendChild(rd);
  }
  cf.forEach(function (c) {
    var w = el("button", "tag cfl", "⚠ conflict");
    w.title = "#" + c.pred + " finishes " + c.need + " but this starts " + c.has
      + " — click to see what Push dependents would move";
    w.addEventListener("click", function (e) { e.stopPropagation(); openPush(c.pred); });
    line.appendChild(w);
  });
  return line;
}

function hiRows(ids, on) {
  ids.forEach(function (id) {
    var r = document.querySelector('#list .row[data-id="' + id + '"]');
    if (r) r.classList.toggle("hl", on);
  });
}

// The edit sheet's Sequence section. Links save as you add them — they are their own
// rows (action_item_deps), not fields of this item, so they do not wait for Save.
function seqSection(it) {
  var wrap = el("div", "seqbox");
  wrap.appendChild(el("div", "sub", "Sequence"));
  var box = el("div");
  wrap.appendChild(box);

  function post(body, after) {
    return api("/api/tracking/dep", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) { toast(r.why || "not saved"); return; }
      if (after) after(r);
      draw();
      load("refresh");
    });
  }

  function linkRow(x, kind) {
    var row = el("div", "lnk" + (x.done ? " done" : ""));
    row.appendChild(idChip(x));
    var when = x.end_date || x.due_date;
    row.appendChild(el("span", "p", (x.title || "") + "  ·  " + x.status
      + (when ? "  ·  " + (x.end_date ? "ends " : "due ") + when : "")
      + (x.lag ? "  ·  +" + x.lag + " working day" + (x.lag === 1 ? "" : "s") : "")));
    if (kind === "after" && !x.done) {
      var met = el("button", "tap r", x.met ? "Un-met" : "Met anyway");
      met.title = x.met ? "count this predecessor again"
        : "work here can go ahead while #" + x.id + " finishes its last bits";
      met.addEventListener("click", function () {
        var note = x.met ? "" : window.prompt("Why can this go ahead? (optional)", "");
        if (note === null) return;
        post({ met: x.dep, on: !x.met, note: note || "" });
      });
      row.appendChild(met);
    }
    var rm = el("button", "tap r", "Remove");
    rm.addEventListener("click", function () { post({ remove: x.dep }); });
    row.appendChild(rm);
    return row;
  }

  function adder(label, mk) {
    var row = el("div", "seqadd");
    var i = el("input");
    i.type = "text";
    i.placeholder = "#id";
    var lag = el("input");
    lag.type = "number";
    lag.min = "0";
    lag.placeholder = "lag";
    lag.title = "working days to wait after the predecessor finishes (optional)";
    var b = el("button", "tap", label);
    b.addEventListener("click", function () {
      var v = i.value.trim();
      if (!v) return;
      post({ add: mk(v, lag.value) });
    });
    i.addEventListener("keydown", function (e) { if (e.key === "Enter") b.click(); });
    row.appendChild(i);
    row.appendChild(lag);
    row.appendChild(b);
    return row;
  }

  function draw() {
    api("/api/tracking/sequence?id=" + it.id).then(function (r) {
      box.innerHTML = "";
      if (!r.ok) { box.appendChild(el("div", "err", r.why)); return; }
      box.appendChild(el("div", "hint", "Comes after — these finish before this starts:"));
      (r.after || []).forEach(function (x) { box.appendChild(linkRow(x, "after")); });
      box.appendChild(adder("Add predecessor", function (v, lag) {
        return { pred: v, succ: it.id, lag: lag || 0 };
      }));
      box.appendChild(el("div", "hint", "Unblocks — these wait for this one:"));
      (r.unblocks || []).forEach(function (x) { box.appendChild(linkRow(x, "unblocks")); });
      box.appendChild(adder("Add successor", function (v, lag) {
        return { pred: it.id, succ: v, lag: lag || 0 };
      }));
      (r.conflicts || []).forEach(function (c) {
        box.appendChild(el("div", "err", "⚠ #" + c.pred + " finishes " + c.need
          + " but this starts " + c.has + "."));
      });
      if ((r.unblocks || []).length) {
        var push = el("button", "", "Push dependents…");
        push.title = "Move the items that wait on this one so they start after it finishes";
        push.addEventListener("click", function () { openPush(it.id); });
        box.appendChild(push);
      }
    });
  }
  draw();
  return wrap;
}

// Push dependents: a preview of every date that would move, applied as one batch with
// one Undo. Never automatic (decision 3). Due dates never move — an item with
// only a due date is listed as held, with the day it would need.
function openPush(predId) {
  if (guarded(function () { openPush(predId); })) return;
  var sh = plainSheet("Push dependents of #" + predId);
  sh.body.appendChild(el("div", "empty", "working out what would move…"));
  api("/api/tracking/push/preview?id=" + predId).then(function (r) {
    sh.body.innerHTML = "";
    if (!r.ok) { sh.body.appendChild(el("div", "err", r.why)); return; }
    var s = r.summary;
    var head = s.moves ? s.moves + (s.moves === 1 ? " item moves" : " items move") + ", by up to "
        + s.max_days + " working day" + (s.max_days === 1 ? "" : "s")
        + (s.new_month ? "; " + s.new_month + " cross into a new month" : "")
        + (s.past_due ? "; " + s.past_due + " would end after the due date" : "") + "."
      : "No planned dates need to move.";
    sh.body.appendChild(el("div", "note", head));
    var ul = el("ul", "plan");
    r.moves.forEach(function (m) {
      var t = "#" + m.id + " " + m.title + " — ";
      if (m.start_date[0]) t += "start " + m.start_date[0] + " → " + m.start_date[1];
      if (m.end_date[0]) t += (m.start_date[0] ? ", " : "") + "end " + m.end_date[0] + " → "
        + m.end_date[1];
      if (m.past_due) t += "  (after its due date " + m.due_date + ")";
      ul.appendChild(el("li", m.past_due ? "late" : null, t));
    });
    if (r.moves.length) sh.body.appendChild(ul);
    if (r.held.length) {
      var w = el("div", "err");
      w.appendChild(el("div", null, "Not moved — these have only a due date, which is a "
        + "commitment. Change it by hand if it really moves, or give the item a plan:"));
      var hl = el("ul");
      r.held.forEach(function (h) {
        hl.appendChild(el("li", null, "#" + h.id + " " + h.title + " — due " + h.due_date
          + ", would need " + h.need + " (" + h.days + " working day"
          + (h.days === 1 ? "" : "s") + " later)"));
      });
      w.appendChild(hl);
      sh.body.appendChild(w);
    }
    var err = el("div", "err");
    err.hidden = true;
    sh.body.appendChild(err);
    sh.foot.appendChild(el("span", "grow"));
    if (!r.moves.length) return;
    var go = el("button", "primary", "Move " + r.moves.length);
    go.addEventListener("click", function () {
      go.disabled = true;
      api("/api/tracking/push", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: predId, sha: r.sha })
      }).then(function (x) {
        go.disabled = false;
        if (!x.ok) { err.textContent = x.why; err.hidden = false; return; }
        closeSheet();
        undoToast(x.moved.length + " item" + (x.moved.length === 1 ? "" : "s") + " moved",
                  x.undo);
        refreshMeta();
      });
    });
    sh.foot.appendChild(go);
  });
}

function undoToast(msg, undo) {
  var t = el("div", "toast float", msg);
  var b = el("button", null, "Undo");
  b.addEventListener("click", function () {
    b.disabled = true;
    api("/api/tracking/push", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ undo: undo })
    }).then(function (x) {
      t.remove();
      toast(x.ok ? "Put back." : "Not undone — " + x.why);
      refreshMeta();
    });
  });
  t.appendChild(b);
  document.body.appendChild(t);
  setTimeout(function () { t.remove(); }, 12000);
}

function isOverdue(it) {
  if (!it.due_date || DONE[it.status]) return false;
  return it.due_date < new Date().toISOString().slice(0, 10);
}

// ------------------------------------------------------------------ other tabs

function renderTab() {
  var box = $("#list");
  box.innerHTML = "";
  $("#count").textContent = "";
  if (TAB === "projects") return renderProjects(box);
}

var PROJ_ARCHIVED = false;

// ------------------------------------------------------------------ quick add in the popups

// Oct 13: "being able to create subitems within the popups … in the Project popup,
// I can make new items (either tasks or stories) quickly with titles. Then if I'm in an
// item, I can quickly add subitems … I might want an option to pull the same account and
// owner down into the new item."
//
// One section, two homes: the project sheet ("Items in this project") and the item sheet
// ("Sub-items"). A list of what is already there (click to open), then a quick-add row —
// Task / Story / Milestone, a title pre-filled with the account code, an owner — and the
// carry-down options: Same account (both), Same owner (item sheet; projects have no owner).
// The options are remembered per browser. Inputs here carry no data-k, so typing in them
// never counts as an unsaved edit of the sheet itself.
function qaPref(k, dflt) {
  try { var v = localStorage.getItem("mt.tracking.qa." + k); return v == null ? dflt : v === "1"; }
  catch (e) { return dflt; }
}
function qaSetPref(k, on) {
  try { localStorage.setItem("mt.tracking.qa." + k, on ? "1" : "0"); } catch (e) {}
}
function custById(id) {
  return (META.customers || []).filter(function (c) { return String(c.id) === String(id); })[0] || null;
}

// cfg: {kind: "project" | "item", heading, load: fn → Promise<[items]>,
//       base: {project_id?, parent_id?}, customer_id, owner, owner_contact_id}
function quickSection(cfg) {
  var wrap = el("div", "qa");
  var head = el("div", "sub", cfg.heading);
  wrap.appendChild(head);
  var list = el("div", "qa-list");
  wrap.appendChild(list);

  function draw() {
    cfg.load().then(function (items) {
      list.innerHTML = "";
      head.textContent = cfg.heading + (items.length ? " (" + items.length + ")" : "");
      items.forEach(function (it) {
        var line = el("button", "qa-item" + (DONE[it.status] ? " done" : ""));
        line.type = "button";
        line.appendChild(el("span", "gid", "#" + it.id));
        line.appendChild(el("span", "tag", it.item_type || "task"));
        line.appendChild(el("span", "qa-t", it.title || it.label || (it.description || "").slice(0, 90)));
        line.appendChild(el("span", "tag", it.status));
        line.title = "Open #" + it.id;
        line.addEventListener("click", function () { openById(it.id); });
        list.appendChild(line);
      });
      if (!items.length) list.appendChild(el("div", "hint", "Nothing here yet."));
    });
  }

  var cust = custById(cfg.customer_id);
  var row = el("div", "qa-row");
  var type = el("select");
  (META.vocab.item_type || ["task", "story"]).forEach(function (t) {
    type.appendChild(optionFor(t, t.charAt(0).toUpperCase() + t.slice(1)));
  });
  type.value = (function () {
    try { return localStorage.getItem("mt.tracking.qa.type") || "task"; } catch (e) { return "task"; }
  })();
  type.addEventListener("change", function () {
    try { localStorage.setItem("mt.tracking.qa.type", type.value); } catch (e) {}
  });
  var t = el("input", "qa-title");
  t.maxLength = META.vocab.title_max || 120;
  var own = el("input");
  own.setAttribute("list", ownersList().id);
  own.placeholder = "owner";
  var add = el("button", "primary", cfg.kind === "item" ? "+ Add sub-item" : "+ Add item");
  row.appendChild(type);
  row.appendChild(t);
  row.appendChild(own);
  row.appendChild(add);
  wrap.appendChild(row);

  var opts = el("div", "qa-opts");
  var sameAcct = null, sameOwner = null;
  if (cust) {
    var la = el("label");
    sameAcct = el("input");
    sameAcct.type = "checkbox";
    sameAcct.checked = qaPref("acct", true);
    la.appendChild(sameAcct);
    la.appendChild(document.createTextNode(" Same account (" + codeForCustomer(cust.short_name) + ")"));
    opts.appendChild(la);
  }
  if (cfg.kind === "item" && (cfg.owner || cfg.owner_contact_id)) {
    var lo = el("label");
    sameOwner = el("input");
    sameOwner.type = "checkbox";
    sameOwner.checked = qaPref("owner", true);
    lo.appendChild(sameOwner);
    lo.appendChild(document.createTextNode(" Same owner (" + (cfg.owner || "linked contact") + ")"));
    opts.appendChild(lo);
  }
  wrap.appendChild(opts);
  var msg = el("div", "err");
  msg.hidden = true;
  wrap.appendChild(msg);

  function prefix() {
    return (sameAcct && sameAcct.checked ? codeForCustomer(cust.short_name)
            : META.vocab.internal_code || "INT") + " - ";
  }
  function syncOpts() {
    // Keep the pre-fill in step with "Same account" while nothing has been typed yet.
    if (!t.value || /^[A-Z]{3} - $/.test(t.value)) t.value = prefix();
    t.placeholder = prefix() + "Short title summary";
    if (sameOwner) {
      own.disabled = sameOwner.checked;
      if (sameOwner.checked) own.value = cfg.owner || "";
    }
  }
  if (sameAcct) sameAcct.addEventListener("change", function () { qaSetPref("acct", sameAcct.checked); syncOpts(); });
  if (sameOwner) sameOwner.addEventListener("change", function () {
    qaSetPref("owner", sameOwner.checked);
    if (!sameOwner.checked) own.value = "";
    syncOpts();
  });
  syncOpts();

  function go() {
    msg.hidden = true;
    var title = t.value.trim();
    if (!title || /^[A-Z]{3} -$/.test(title)) { msg.textContent = "Give it a title first."; msg.hidden = false; t.focus(); return; }
    var f = Object.assign({ title: title, description: title, status: "open",
                            item_type: type.value }, cfg.base);
    if (sameAcct && sameAcct.checked) f.customer_id = String(cfg.customer_id);
    if (sameOwner && sameOwner.checked) {
      if (cfg.owner_contact_id) f.owner_contact_id = String(cfg.owner_contact_id);
      else f.owner = cfg.owner;
    } else if (own.value.trim()) {
      var c = contactByName(own.value, cust && cust.short_name);
      if (c && !c.ambiguous) f.owner_contact_id = String(c.id);
      else f.owner = own.value.trim();
    }
    add.disabled = true;
    api("/api/tracking/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: null, fields: f })
    }).then(function (r) {
      add.disabled = false;
      if (!r.ok) { msg.textContent = r.why || "not added"; msg.hidden = false; return; }
      toast("Added #" + r.item.id + (cfg.kind === "item" ? " under #" + cfg.base.parent_id : ""));
      t.value = "";
      syncOpts();
      t.focus();
      draw();
    }).catch(function (e) { add.disabled = false; msg.textContent = String(e); msg.hidden = false; });
  }
  add.addEventListener("click", go);
  t.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } });
  draw();
  return wrap;
}

// ------------------------------------------------------------------ project workspace (R4.8)

// Oct 13: "on the project page if when I click on the project it showed me all the
// items in there that I can do quick edits or adds right from the project." A click on a
// project card opens its items under it; each row edits in place — title, status, owner,
// start, end, due — saved field by field through /api/tracking/save with the row's rev
// (the same refusals and conflict check as the edit sheet). A quick-add row at the bottom
// creates items in this project (or under a row, via + sub). The project's own fields
// moved behind the card's Edit button.
var OPENPROJ = {};                       // project id → {showDone, parent}

function codeForCustomer(short) {
  return (META.vocab.account_codes || {})[short] || META.vocab.internal_code || "INT";
}
// Phase 7 — the people an item is "with" (the owner is the first person; these are the
// rest). Saved as you add / remove them — they are their own rows (item_people), not
// fields of the item, so they never wait for Save. A typed name links only when it is
// exactly one person (contactByName), the same never-guess rule as the owner.
function withSection(it) {
  var wrap = el("div", "withsec");
  wrap.appendChild(el("div", "sub", "With"));
  var chips = el("div", "withchips");
  wrap.appendChild(chips);
  var row = el("div", "withadd");
  var inp = el("input");
  inp.placeholder = "Add a person…";
  inp.setAttribute("list", ownersList().id);
  var add = el("button", "tap", "Add");
  var msg = el("div", "hint");
  row.appendChild(inp);
  row.appendChild(add);
  wrap.appendChild(row);
  wrap.appendChild(msg);
  function draw(list) {
    chips.innerHTML = "";
    if (!list.length) chips.appendChild(el("span", "hint", "Nobody else yet."));
    list.forEach(function (p) {
      var c = el("span", "tag withchip", p.name + " ");
      var x = el("button", "chipx", "×");
      x.title = "Remove " + p.name;
      x.addEventListener("click", function () {
        api("/api/tracking/people", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ item: it.id, remove: p.pid }) }).then(refresh);
      });
      c.appendChild(x);
      chips.appendChild(c);
    });
  }
  function refresh() {
    api("/api/tracking/item?id=" + it.id).then(function (r) {
      if (r.item) { it.with = r.item.with || []; draw(it.with); }
    });
  }
  function go() {
    msg.textContent = "";
    var c = contactByName(inp.value, it.customer);
    if (!c) { msg.textContent = "No contact is called exactly that — add them on the Contacts page first."; return; }
    if (c.ambiguous) { msg.textContent = c.ambiguous + " contacts share that name — use the Owners review or Contacts page to pick."; return; }
    api("/api/tracking/people", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item: it.id, add: c.id }) }).then(function (r) {
      if (!r.ok) { msg.textContent = r.why || "not added"; return; }
      inp.value = "";
      refresh();
    });
  }
  add.addEventListener("click", go);
  inp.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } });
  draw(it.with || []);
  return wrap;
}

// The contact a typed owner name means — never a guess. Some internal people have one
// contact row per account (one contact carried on two accounts), so: exactly one live
// match → that one; several → the one on this item's account; otherwise {ambiguous}
// and the name is saved as plain text, linked to nobody (Person Verification rule).
function contactByName(name, custShort) {
  var n = (name || "").trim().toLowerCase();
  if (!n) return null;
  var hits = CONTACTS.filter(function (c) {
    return (c.name || "").trim().toLowerCase() === n && !c.merged_into && !c.archived_at;
  });
  if (hits.length === 1) return hits[0];
  var mine = hits.filter(function (c) { return custShort && c.customer === custShort; });
  if (mine.length === 1) return mine[0];
  return hits.length ? { ambiguous: hits.length } : null;
}
function ownersList() {
  var dl = $("#pws-owners");
  if (dl) return dl;
  dl = el("datalist");
  dl.id = "pws-owners";
  CONTACTS.forEach(function (c) { dl.appendChild(optionFor(c.name, c.customer || "")); });
  document.body.appendChild(dl);
  return dl;
}

function projectPanel(p, after) {
  var st = OPENPROJ[p.id] || (OPENPROJ[p.id] = { showDone: false, parent: null });
  var panel = el("div", "pws");
  panel.dataset.pid = p.id;
  after.parentNode.insertBefore(panel, after.nextSibling);
  panel.appendChild(el("div", "empty", "loading…"));
  var q = "/api/tracking/items?project=" + p.id + "&limit=500&sort=due&dir=asc"
    + (st.showDone ? "" : "&hide_done=1");
  api(q).then(function (r) {
    panel.innerHTML = "";
    if (r.error) { panel.appendChild(el("div", "err", r.error)); return; }
    drawPanel(p, panel, r.items || [], st);
  });
  return panel;
}

function drawPanel(p, panel, items, st) {
  var bar = el("div", "pws-bar");
  var live = items.filter(function (i) { return !DONE[i.status]; }).length;
  bar.appendChild(el("span", "pws-n", live + " open" + (st.showDone ? " · "
    + (items.length - live) + " done shown" : "")));
  var sd = el("label", "pws-sd");
  var cb = el("input");
  cb.type = "checkbox";
  cb.checked = st.showDone;
  cb.addEventListener("change", function () { st.showDone = cb.checked; reloadPanel(p); });
  sd.appendChild(cb);
  sd.appendChild(document.createTextNode(" Show done"));
  bar.appendChild(sd);
  var g = el("button", "tap", "Gantt ↗");
  g.addEventListener("click", function () {
    var w = window.open("/tracking/gantt?project=" + p.id, "mt-gantt", "width=1500,height=900");
    if (w) w.focus();
  });
  bar.appendChild(g);
  panel.appendChild(bar);

  var head = el("div", "pws-row pws-head");
  ["", "Title", "Status", "Owner", "Start", "End", "Due", ""].forEach(function (h) {
    head.appendChild(el("span", null, h));
  });
  panel.appendChild(head);
  if (!items.length) panel.appendChild(el("div", "empty", "No items in this project yet — add the first below."));
  var lastStep = null;
  items.forEach(function (it) {
    if (it.step != null && !it.depth && it.step !== lastStep && items.some(function (x) { return x.step > 1; })) {
      lastStep = it.step;
      panel.appendChild(el("div", "pws-step", "Step " + it.step));
    }
    panel.appendChild(pwsRow(p, it, st));
  });
  panel.appendChild(quickAdd(p, st));
}

function reloadPanel(p) {
  var old = document.querySelector('.pws[data-pid="' + p.id + '"]');
  if (!old) return;
  var anchor = old.previousSibling;
  old.remove();
  projectPanel(p, anchor);
}

function pwsRow(p, it, st) {
  var row = el("div", "pws-row" + (DONE[it.status] ? " done" : "") + (it.depth ? " sub" : ""));
  row.dataset.id = it.id;
  var idc = el("span", "pws-id", (it.depth ? "↳ " : "") + "#" + it.id);
  row.appendChild(idc);

  var tcell = el("span", "pws-t");
  var title = el("input", "pws-in title");
  title.value = it.title || "";
  title.maxLength = META.vocab.title_max || 120;
  title.placeholder = it.title ? "" : codeForCustomer(it.customer) + " - "
    + ((it.description || "").replace(/\s+/g, " ").slice(0, 70));
  tcell.appendChild(title);
  var ex = el("div", "pws-ex", (it.description || "").replace(/\s+/g, " ").slice(0, 200));
  ex.title = "the description — open the item for all of it";
  tcell.appendChild(ex);
  row.appendChild(tcell);

  var stat = el("select", "pws-in");
  fillVocabSelect(stat, "status");
  if (!(META.vocab.status || []).some(function (v) { return v === it.status; })) {
    stat.appendChild(optionFor(it.status, it.status));
  }
  stat.value = it.status;
  row.appendChild(stat);

  var own = el("input", "pws-in");
  own.value = it.owner || "";
  own.setAttribute("list", ownersList().id);
  own.placeholder = "owner";
  var ocell = el("span", "pws-own");
  ocell.appendChild(own);
  // An owner typed as plain text that matches exactly one contact (or exactly one on this
  // item's account) can be linked in one click — retyping the same name changes nothing,
  // so it never would be otherwise. Ambiguous names get no button (never guess a person).
  var cand = (!it.owner_contact_id && it.owner) ? contactByName(it.owner, it.customer) : null;
  if (cand && !cand.ambiguous) {
    var lk = el("button", "pws-link", "link to contact");
    lk.type = "button";
    lk.title = "Link “" + it.owner + "” to the contact " + cand.name
      + (cand.customer ? " (" + cand.customer + ")" : "") + " — Mine and the owner pickers then count it";
    lk.addEventListener("click", function () {
      lk.remove();
      save({ owner_contact_id: String(cand.id) }, own, function () {});
    });
    ocell.appendChild(lk);
  }
  if ((it.with || []).length) {
    ocell.appendChild(el("span", "pws-with", "with " + it.with.map(function (p) { return p.name; }).join(", ")));
  }
  row.appendChild(ocell);

  var dates = ["start_date", "end_date", "due_date"].map(function (k) {
    var d = el("input", "pws-in date");
    d.type = "date";
    d.value = it[k] || "";
    row.appendChild(d);
    return [k, d];
  });

  var acts = el("span", "pws-acts");
  if (!it.depth) {
    var sub = el("button", "tap", "+ sub");
    sub.title = "Quick-add a sub-item under #" + it.id;
    sub.addEventListener("click", function () {
      st.parent = { id: it.id, t: it.title || "#" + it.id };
      var qa = row.parentNode.querySelector(".pws-add");
      if (qa) qa.replaceWith(quickAdd(p, st));
      var t = row.parentNode.querySelector(".pws-add .title");
      if (t) t.focus();
    });
    acts.appendChild(sub);
  }
  var op = el("button", "tap", "open");
  op.title = "The full edit sheet (description, sequence, links)";
  op.addEventListener("click", function () { openById(it.id); });
  acts.appendChild(op);
  row.appendChild(acts);

  var msg = el("div", "pws-msg");
  msg.hidden = true;
  row.appendChild(msg);

  // Saves on one row run one after another: Tab from title to owner fires two changes
  // a few ms apart, and the second must carry the rev the first one produced — sent
  // together, the second would be refused as "changed elsewhere".
  var chain = Promise.resolve();
  function save(fields, input, revert) {
    chain = chain.then(function () { return saveNow(fields, input, revert); });
  }
  function saveNow(fields, input, revert) {
    msg.hidden = true;
    row.classList.add("saving");
    return api("/api/tracking/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: it.id, fields: fields, rev: it.rev })
    }).then(function (r) {
      row.classList.remove("saving");
      if (r.ok && r.item) {
        Object.assign(it, r.item);
        flash(input);
        if ("status" in fields) row.classList.toggle("done", !!DONE[it.status]);
        return;
      }
      if (r.conflict) {
        msg.innerHTML = "";
        msg.appendChild(document.createTextNode("Changed elsewhere since this loaded. "));
        var mine = el("button", "tap", "Keep mine");
        mine.addEventListener("click", function () {
          it.rev = r.conflict.rev;
          save(fields, input, revert);
        });
        var theirs = el("button", "tap", "Take theirs");
        theirs.addEventListener("click", function () { reloadPanel(p); });
        msg.appendChild(mine);
        msg.appendChild(theirs);
        msg.hidden = false;
        return;
      }
      msg.textContent = r.why || "not saved";
      msg.hidden = false;
      revert();
    }).catch(function (e) {
      row.classList.remove("saving");
      msg.textContent = String(e);
      msg.hidden = false;
      revert();
    });
  }
  function bind(input, key, toFields) {
    var was = input.value;
    var go = function () {
      if (input.value === was) return;
      var v = input.value, prev = was;
      was = v;
      save(toFields(v), input, function () { input.value = prev; was = prev; });
    };
    input.addEventListener("change", go);
    if (input.tagName === "INPUT" && input.type !== "date") {
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { input.value = was; input.blur(); }
      });
    }
  }
  bind(title, "title", function (v) { return { title: v }; });
  bind(stat, "status", function (v) { return { status: v }; });
  bind(own, "owner", function (v) {
    var c = contactByName(v, it.customer);
    if (c && c.ambiguous) {
      setTimeout(function () {
        msg.textContent = c.ambiguous + " contacts are named “" + v + "” — saved as plain "
          + "text, linked to nobody. Pick the right one in the full sheet (open).";
        msg.hidden = false;
      }, 50);
    }
    return c && !c.ambiguous ? { owner_contact_id: String(c.id) }
                             : { owner_contact_id: "", owner: v };
  });
  dates.forEach(function (kd) {
    bind(kd[1], kd[0], function (v) { var f = {}; f[kd[0]] = v; return f; });
  });
  return row;
}

function flash(input) {
  input.classList.add("saved");
  setTimeout(function () { input.classList.remove("saved"); }, 900);
}

// The quick-add row: title (pre-filled with the account code), owner, dates. Lands in
// this project; with a parent picked by "+ sub", as its sub-item. Enter adds and leaves
// the title ready for the next one, so a list of items goes in fast. The description
// starts as the title — the full sheet is where the detail goes.
function quickAdd(p, st) {
  var code = codeForCustomer(p.customer) + " - ";
  var row = el("div", "pws-row pws-add");
  row.appendChild(el("span", "pws-id", st.parent ? "↳ new" : "new"));
  var tcell = el("span", "pws-t");
  var t = el("input", "pws-in title");
  t.value = code;
  t.maxLength = META.vocab.title_max || 120;
  t.placeholder = code + "Short title summary";
  tcell.appendChild(t);
  if (st.parent) {
    var chip = el("div", "pws-ex");
    chip.appendChild(document.createTextNode("under #" + st.parent.id + " " + st.parent.t + " "));
    var x = el("button", "tap", "×");
    x.title = "Add at the top level instead";
    x.addEventListener("click", function () { st.parent = null; row.replaceWith(quickAdd(p, st)); });
    chip.appendChild(x);
    tcell.appendChild(chip);
  }
  row.appendChild(tcell);
  var typ = el("select", "pws-in");
  (META.vocab.item_type || ["task", "story"]).forEach(function (v) {
    typ.appendChild(optionFor(v, v.charAt(0).toUpperCase() + v.slice(1)));
  });
  try { typ.value = localStorage.getItem("mt.tracking.qa.type") || "task"; } catch (e) {}
  typ.addEventListener("change", function () {
    try { localStorage.setItem("mt.tracking.qa.type", typ.value); } catch (e) {}
  });
  row.appendChild(typ);
  var own = el("input", "pws-in");
  own.setAttribute("list", ownersList().id);
  own.placeholder = "owner";
  row.appendChild(own);
  var ds = ["start_date", "end_date", "due_date"].map(function (k) {
    var d = el("input", "pws-in date");
    d.type = "date";
    row.appendChild(d);
    return [k, d];
  });
  var acts = el("span", "pws-acts");
  var add = el("button", "primary", "Add");
  acts.appendChild(add);
  row.appendChild(acts);
  var msg = el("div", "pws-msg");
  msg.hidden = true;
  row.appendChild(msg);

  function go() {
    var title = t.value.trim();
    if (!title || title === code.trim()) { msg.textContent = "Give it a title first."; msg.hidden = false; return; }
    var f = { title: title, description: title, project_id: String(p.id), status: "open",
              item_type: typ.value };
    if (p.customer_id != null) f.customer_id = String(p.customer_id);
    if (st.parent) f.parent_id = String(st.parent.id);
    var c = contactByName(own.value, p.customer);
    if (c && !c.ambiguous) f.owner_contact_id = String(c.id);
    else if (own.value.trim()) f.owner = own.value.trim();
    ds.forEach(function (kd) { if (kd[1].value) f[kd[0]] = kd[1].value; });
    add.disabled = true;
    api("/api/tracking/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: null, fields: f })
    }).then(function (r) {
      add.disabled = false;
      if (!r.ok) { msg.textContent = r.why || "not added"; msg.hidden = false; return; }
      toast("Added #" + r.item.id);
      reloadPanel(p);
      setTimeout(function () {
        var nt = document.querySelector('.pws[data-pid="' + p.id + '"] .pws-add .title');
        if (nt) { nt.focus(); nt.setSelectionRange(nt.value.length, nt.value.length); }
      }, 400);
    });
  }
  add.addEventListener("click", go);
  t.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } });
  return row;
}


function renderProjects(box) {
  var bar = el("div", "selbar");
  var add = el("button", "primary", "+ New project");
  add.addEventListener("click", function () { openProject(null); });
  bar.appendChild(add);
  var tog = el("button", "tap", PROJ_ARCHIVED ? "Show active" : "Show archived");
  tog.addEventListener("click", function () {
    PROJ_ARCHIVED = !PROJ_ARCHIVED;
    refreshProjects();
  });
  bar.appendChild(tog);
  box.appendChild(bar);

  // Oct 13: filter, account, status and sort — remembered. The controls are built once
  // per render and only the cards below redraw as you type.
  var PT = pstore("ptab") || { q: "", acct: "", status: "", sort: "name" };
  var save = function () { pstore("ptab", PT); drawCards(); };
  var fq = el("input");
  fq.type = "search";
  fq.placeholder = "Filter projects…";
  fq.value = PT.q || "";
  fq.addEventListener("input", function () { PT.q = fq.value; save(); });
  var fa = el("select");
  fa.appendChild(optionFor("", "Every account"));
  (META.customers || []).forEach(function (c) { fa.appendChild(optionFor(c.short_name, c.name)); });
  fa.appendChild(optionFor("none", "— no account —"));
  fa.value = PT.acct || "";
  fa.addEventListener("change", function () { PT.acct = fa.value; save(); });
  var fs = el("select");
  fs.appendChild(optionFor("", "Any status"));
  ((META.vocab.project || {}).status || []).forEach(function (v) { fs.appendChild(optionFor(v, v)); });
  fs.value = PT.status || "";
  fs.addEventListener("change", function () { PT.status = fs.value; save(); });
  var fo = el("select");
  PSORTS.forEach(function (s) { fo.appendChild(optionFor(s[0], "Sort: " + s[1])); });
  fo.value = PT.sort || "name";
  fo.addEventListener("change", function () { PT.sort = fo.value; save(); });
  var ctl = el("div", "ptab-ctl");
  [fq, fa, fs, fo].forEach(function (x) { ctl.appendChild(x); });
  box.appendChild(ctl);
  var pl = el("div", "rows");
  box.appendChild(pl);

  var list = META.projects || [];
  if (!list.length) {
    box.appendChild(el("div", "empty",
      PROJ_ARCHIVED ? "Nothing archived." : "No projects yet."));
    return;
  }
  function drawCards() {
  pl.innerHTML = "";
  fq.classList.toggle("on", !!PT.q); fa.classList.toggle("on", !!PT.acct); fs.classList.toggle("on", !!PT.status);
  var shown = projSort(list.filter(function (p) {
    if (PT.acct && (PT.acct === "none" ? p.customer : p.customer !== PT.acct)) return false;
    if (PT.status && p.status !== PT.status) return false;
    return projMatch(p, PT.q);
  }), PT.sort);
  if (!shown.length) pl.appendChild(el("div", "empty", "No projects match — clear the filters above."));
  if (shown.length < list.length) pl.appendChild(el("div", "side-n", "showing " + shown.length + " of " + list.length));
  shown.forEach(function (p) {
    var row = el("div", "row");
    row.appendChild(el("div", "when", p.medium || "—"));
    var mid = el("div", "mid");
    mid.appendChild(el("div", "name", p.name || ""));
    mid.appendChild(el("div", "sub", (p.notes || "").slice(0, 140)));
    row.appendChild(mid);
    var right = el("div", "right");
    if (p.customer) right.appendChild(el("span", "tag cust", p.customer));
    right.appendChild(el("span", "tag", p.type || "untyped"));
    right.appendChild(el("span", "tag " + (p.status === "active" ? "todo" : "ok"),
                         p.status || "—"));
    if (p.priority && p.priority !== "normal") {
      right.appendChild(el("span", "tag", p.priority));
    }
    right.appendChild(el("span", "tag", p.item_count + " items"));
    if (p.origin_item_id) right.appendChild(el("span", "tag type", "from #" + p.origin_item_id));
    if (p.legacy_fields && p.legacy_fields.length) {
      var w = el("span", "tag unconf", "legacy");
      w.title = "legacy value in: " + p.legacy_fields.join(", ");
      right.appendChild(w);
    }
    if (p.rail_url) {
      var a = el("a", "tag", "open");
      a.href = p.rail_url;
      a.target = "_blank";
      a.rel = "noopener";
      a.addEventListener("click", function (e) { e.stopPropagation(); });
      right.appendChild(a);
    }
    // R4.8: the card opens its items; the project's own fields are behind Edit.
    var ed = el("button", "tap", "Edit");
    ed.title = "Edit the project itself (name, type, tracker link, status…)";
    ed.addEventListener("click", function (e) { e.stopPropagation(); openProject(p); });
    right.appendChild(ed);
    row.appendChild(right);
    row.classList.add("pcard");
    if (OPENPROJ[p.id]) row.classList.add("open");
    row.title = "Click to show this project's items";
    row.addEventListener("click", function (e) {
      if (e.target.closest("a, button")) return;
      if (OPENPROJ[p.id]) {
        delete OPENPROJ[p.id];
        row.classList.remove("open");
        var pnl = document.querySelector('.pws[data-pid="' + p.id + '"]');
        if (pnl) pnl.remove();
      } else {
        OPENPROJ[p.id] = { showDone: false, parent: null };
        row.classList.add("open");
        projectPanel(p, row);
      }
    });
    pl.appendChild(row);
    if (OPENPROJ[p.id]) projectPanel(p, row);
  });
  }
  drawCards();
}

function refreshProjects() {
  api("/api/tracking/projects" + (PROJ_ARCHIVED ? "?archived=1" : ""))
    .then(function (r) {
      META.projects = r.projects || [];
      if (TAB === "projects") renderTab();
    });
}

function openProject(p) {
  if (guarded(function () { openProject(p); })) return;
  closeSheet();
  var isNew = !p;
  p = p || { status: "active" };
  var pv = (META.vocab && META.vocab.project) || {};
  var help = pv.field_help || {};

  var modal = el("div", "modal");
  var sheet = el("div", "sheet wide");
  var head = el("div", "head");
  head.appendChild(el("b", null, isNew ? "New project" : "Project #" + p.id));
  var g = el("button", null, "");
  head.appendChild(g);
  var x = el("button", null, "Close");
  x.addEventListener("click", function () { requestClose(); });
  head.appendChild(x);
  sheet.appendChild(head);

  var form = el("div", "form");
  var applyGuide = function (on) {
    form.classList.toggle("guide", on);
    g.textContent = on ? "Hide field guide" : "Field guide";
  };
  g.addEventListener("click", function () {
    var on = !form.classList.contains("guide");
    setGuide(on);
    applyGuide(on);
  });

  function ph(l, key) {
    var t = help[key];
    if (!t) return l;
    var d = el("div", "hint");
    t.split(/`([^`]+)`/).forEach(function (part, i) {
      if (i % 2) d.appendChild(el("code", null, part));
      else if (part) d.appendChild(document.createTextNode(part));
    });
    l.appendChild(d);
    return l;
  }
  function pick(key, label, val, vals) {
    var l = labelled(label);
    var sel = el("select");
    sel.dataset.k = key;
    if (key === "priority") sel.appendChild(optionFor("", "—"));
    (vals || []).forEach(function (v) { sel.appendChild(optionFor(v, v)); });
    if (val && (vals || []).indexOf(val) === -1) {
      sel.appendChild(optionFor(val, val + "  (legacy — pick a replacement)"));
    }
    sel.value = val || (key === "priority" ? "" : (vals || [])[0] || "");
    l.appendChild(sel);
    return ph(l, key);
  }

  var nameL = labelled("Name");
  var ni = el("input");
  ni.type = "text";
  ni.value = p.name || "";
  ni.dataset.k = "name";
  nameL.appendChild(ni);
  form.appendChild(ph(nameL, "name"));

  var grid = el("div", "grid");
  grid.appendChild(pick("type", "Type of work", p.type, pv.type));
  grid.appendChild(selectField("customer_id", "Account", p.customer_id,
    META.customers.map(function (c) { return [String(c.id), c.short_name || c.name]; }),
    "— none —"));
  grid.appendChild(pick("status", "Status", p.status, pv.status));
  grid.appendChild(pick("priority", "Priority", p.priority, pv.priority));
  form.appendChild(grid);

  var urlL = labelled("Tracker link");
  var ui = el("input");
  ui.type = "text";
  ui.value = p.rail_url || "";
  ui.dataset.k = "rail_url";
  ui.placeholder = "https://…";
  urlL.appendChild(ui);
  form.appendChild(ph(urlL, "rail_url"));

  var notesL = labelled("Notes");
  var ta = el("textarea", "notes");
  ta.value = p.notes || "";
  ta.dataset.k = "notes";
  notesL.appendChild(ta);
  form.appendChild(ph(notesL, "notes"));
  if (!isNew) {
    form.appendChild(quickSection({
      kind: "project", heading: "Items in this project",
      load: function () {
        return api("/api/tracking/items?project=" + p.id + "&limit=500&sort=due&dir=asc&hide_done=1")
          .then(function (r) { return (r.items || []).filter(function (x) { return !x.parent_id; }); });
      },
      base: { project_id: String(p.id) },
      customer_id: p.customer_id
    }));
  }

  var err = el("div", "err");
  err.hidden = true;
  form.appendChild(err);
  applyGuide(guideOn());
  sheet.appendChild(form);

  var foot = el("div", "foot");
  foot.appendChild(el("span", "idline grow", isNew ? "" :
    (p.item_count || 0) + " action item" + (p.item_count === 1 ? "" : "s") + " attached"));
  if (!isNew) {
    var arch = el("button", p.archived_at ? "primary" : "",
                  p.archived_at ? "Restore" : "Archive");
    arch.addEventListener("click", function () {
      if (!p.archived_at && !window.confirm(
            "Archive “" + p.name + "”?\n\n"
            + (p.item_count || 0) + " action item(s) point at it. They keep working and "
            + "keep their link — the project is only hidden from the list and the "
            + "picker. Nothing is deleted.")) return;
      arch.disabled = true;
      api("/api/tracking/project/archive", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: p.id, archived: !p.archived_at })
      }).then(function (r) {
        arch.disabled = false;
        if (!r.ok) { err.textContent = r.why || "failed"; err.hidden = false; return; }
        closeSheet();
        toast(p.archived_at ? "Restored." : "Archived.");
        refreshProjects();
      });
    });
    foot.appendChild(arch);
  }
  if (!isNew && META.vocab.r42 && !p.archived_at && !p.demoted_to_item_id) {
    var dem = el("button", "", "Demote to item…");
    dem.title = "Turn this project back into an item; its items become sub-items. "
      + "Previewed first.";
    dem.addEventListener("click", function () { openDemote(p); });
    foot.appendChild(dem);
  }
  var save = el("button", "primary", isNew ? "Create" : "Save");
  save.addEventListener("click", function () {
    var fields = {};
    form.querySelectorAll("[data-k]").forEach(function (n) {
      if (n.disabled) return;
      fields[n.dataset.k] = n.value;
    });
    if (!isNew) {
      Object.keys(fields).forEach(function (k) {
        var was = p[k] == null ? "" : String(p[k]);
        if (String(fields[k]) === was) delete fields[k];
      });
      if (!Object.keys(fields).length) { closeSheet(); return; }
    }
    save.disabled = true;
    err.hidden = true;
    api("/api/tracking/project/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: isNew ? null : p.id, fields: fields })
    }).then(function (r) {
      save.disabled = false;
      if (!r.ok) { err.textContent = r.why || "save failed"; err.hidden = false; return; }
      closeSheet();
      toast(isNew ? "Project created." : "Saved.");
      refreshProjects();
    });
  });
  foot.appendChild(save);
  sheet.appendChild(foot);
  var hsave = el("button", "primary hsave", isNew ? "Create" : "Save");
  hsave.title = "Save and close (⌘S)";
  hsave.addEventListener("click", function () { save.click(); });
  head.insertBefore(hsave, x);
  guardSheet(sheet, form, function () { save.click(); });

  modal.appendChild(sheet);
  modal.addEventListener("click", function (e) { if (e.target === modal) requestClose(); });
  document.body.appendChild(modal);
  sheetEl = modal;
  document.addEventListener("keydown", onEsc);
}

// ------------------------------------------------------------------ edit sheet

var sheetEl = null;

function openSheet(it) {
  if (guarded(function () { openSheet(it); })) return;
  closeSheet();
  var isNew = !it;
  it = it || { status: "open", priority: "normal", report_scope: "unset" };

  var modal = el("div", "modal");
  var sheet = el("div", "sheet wide");

  var head = el("div", "head");
  head.appendChild(el("b", null, isNew ? "New action item" : "Item #" + it.id));

  var guide = el("button", null, "");
  head.appendChild(guide);
  var x = el("button", null, "Close");
  x.addEventListener("click", function () { requestClose(); });
  head.appendChild(x);
  sheet.appendChild(head);

  var form = el("div", "form");
  var applyGuide = function (on) {
    form.classList.toggle("guide", on);
    guide.textContent = on ? "Hide field guide" : "Field guide";
  };
  guide.addEventListener("click", function () {
    var on = !form.classList.contains("guide");
    setGuide(on);
    applyGuide(on);
  });
  if (META.vocab.r44c) form.appendChild(titleField(it));
  var descL = area("description", "Description", it.description || "");
  form.appendChild(descL);
  if (!isNew) form.appendChild(entryRow(descL.querySelector("textarea")));

  var grid = el("div", "grid");
  grid.appendChild(ownerField(it));
  grid.appendChild(selectField("customer_id", "Account", it.customer_id,
    META.customers.map(function (c) {
      return [String(c.id), c.short_name || c.name];
    }), "— none —"));
  grid.appendChild(vocabField("status", it));
  grid.appendChild(vocabField("priority", it));
  grid.appendChild(vocabField("report_scope", it));
  grid.appendChild(dateField("due_date", "Due date", it.due_date || ""));
  var projL = selectField("project_id", "Project", it.project_id,
    META.projects.map(function (p) { return [String(p.id), p.name]; }), "— none —");
  grid.appendChild(projL);
  grid.appendChild(parentField(it, projL));
  if (META.vocab.r42) {
    // R4.2: the plan (start → end) next to the commitment (due), and the Jira-facing
    // type. Only once the DB is migrated; before that the columns do not exist.
    var tl = labelled("Type");
    var ts = el("select");
    ts.dataset.k = "item_type";
    (META.vocab.item_type || ["task"]).forEach(function (t) { ts.appendChild(optionFor(t, t)); });
    ts.value = it.item_type || "task";
    tl.appendChild(ts);
    grid.appendChild(hint(tl, "item_type"));
    grid.appendChild(dateField("start_date", "Start (plan)", it.start_date || ""));
    grid.appendChild(dateField("end_date", "End (plan)", it.end_date || ""));
  }
  form.appendChild(grid);
  if (!isNew && META.vocab.p7) form.appendChild(withSection(it));

  form.appendChild(text("source", "Source", it.source || ""));

  if (!isNew) form.appendChild(linkSection(it));
  if (!isNew && META.vocab.r42) form.appendChild(seqSection(it));
  if (!isNew && !it.promoted_to_project_id) {
    if (it.parent_id) {
      // Project → item → sub-item, one level only (R4.2's depth guard).
      var one = el("div", "note");
      one.appendChild(document.createTextNode("This is a sub-item of #" + it.parent_id
        + ". Sub-items go one level deep — add more from "));
      var up = el("button", "chip", "#" + it.parent_id);
      up.addEventListener("click", function () { openById(it.parent_id); });
      one.appendChild(up);
      one.appendChild(document.createTextNode("."));
      form.appendChild(one);
    } else {
      form.appendChild(quickSection({
        kind: "item", heading: "Sub-items",
        load: function () {
          return api("/api/tracking/children?id=" + it.id).then(function (r) { return r.items || []; });
        },
        base: { parent_id: String(it.id) },
        customer_id: it.customer_id, owner: it.owner, owner_contact_id: it.owner_contact_id
      }));
    }
  }

  if (it.promoted_to_project_id) {
    var pr = (META.projects || []).filter(function (p) {
      return p.id === it.promoted_to_project_id;
    })[0];
    form.appendChild(el("div", "note", "Promoted to project #" + it.promoted_to_project_id
      + (pr ? " “" + pr.name + "”" : "") + ". This item is kept, closed, as its history; "
      + "its work now lives in the project. Demoting the project restores it."));
  }
  if (it.legacy_fields && it.legacy_fields.length) {
    form.appendChild(el("div", "note", "This row holds a value that is being retired ("
      + it.legacy_fields.join(", ") + "). Leaving it alone is fine — saving other changes "
      + "will not touch it. Changing it means picking a current value."));
  }

  var err = el("div", "err");
  err.hidden = true;
  form.appendChild(err);
  applyGuide(guideOn());
  sheet.appendChild(form);

  var foot = el("div", "foot");
  var idline = el("span", "idline grow", isNew ? "" :
    "created " + (it.created_at || "?") + (it.updated_by ? "  ·  last by " + it.updated_by : ""));
  foot.appendChild(idline);
  if (!isNew && META.vocab.r42 && !it.parent_id && !it.promoted_to_project_id) {
    var prom = el("button", "", "Promote to project…");
    prom.title = "Make this item a project; its sub-items become the project's items. "
      + "You see exactly what changes before anything is written.";
    prom.addEventListener("click", function () { openPromote(it); });
    foot.appendChild(prom);
  }
  var save = el("button", "primary", isNew ? "Create" : "Save");
  save.addEventListener("click", function () { doSave(it, isNew, form, err, save); });
  foot.appendChild(save);
  sheet.appendChild(foot);
  // Save up top as well (the head is sticky), so a long sheet never needs a scroll to
  // save. Save saves and closes, same as the one at the bottom.
  var hsave = el("button", "primary hsave", isNew ? "Create" : "Save");
  hsave.title = "Save and close (⌘S)";
  hsave.addEventListener("click", function () { save.click(); });
  head.insertBefore(hsave, x);
  guardSheet(sheet, form, function () { save.click(); });

  modal.appendChild(sheet);
  modal.addEventListener("click", function (e) { if (e.target === modal) requestClose(); });
  document.body.appendChild(modal);
  sheetEl = modal;
  document.addEventListener("keydown", onEsc);
}

// "Sub-item of #N". Nesting stops at one level and a sub-item lives in its parent's
// project — tracking.py refuses anything else with a sentence that says why. While a
// parent is set the Project picker is parked, because the parent decides it.
function parentField(it, projL) {
  var l = labelled("Sub-item of");
  var i = el("input");
  i.type = "text";
  i.placeholder = "#id, or empty for top level";
  i.value = it.parent_id ? "#" + it.parent_id : "";
  i.dataset.k = "parent_id";
  i.dataset.was = it.parent_id ? String(it.parent_id) : "";
  l.appendChild(i);
  hint(l, "parent_id");
  var note = el("div", "hint pick-meaning");
  l.appendChild(note);
  var proj = projL.querySelector("select");
  function sync() {
    var on = !!i.value.replace(/[#\s]/g, "");
    proj.disabled = on;
    note.textContent = on ? "A sub-item follows its parent's project."
      : (it.child_count ? "Has " + it.child_count + " sub-item"
         + (it.child_count === 1 ? "" : "s") + " — a project change carries them along."
         : "");
  }
  i.addEventListener("input", sync);
  sync();
  return l;
}

// ------------------------------------------------------------------ promote / demote

function plainSheet(title) {
  closeSheet();
  var modal = el("div", "modal");
  var sheet = el("div", "sheet wide");
  var head = el("div", "head");
  head.appendChild(el("b", null, title));
  var x = el("button", null, "Close");
  x.addEventListener("click", function () { requestClose(); });
  head.appendChild(x);
  sheet.appendChild(head);
  var body = el("div", "form");
  sheet.appendChild(body);
  var foot = el("div", "foot");
  sheet.appendChild(foot);
  modal.appendChild(sheet);
  modal.addEventListener("click", function (e) { if (e.target === modal) requestClose(); });
  document.body.appendChild(modal);
  sheetEl = modal;
  document.addEventListener("keydown", onEsc);
  return { body: body, foot: foot };
}

function bullet(ul, text) { ul.appendChild(el("li", null, text)); }

// Promote: the server computes the plan and a fingerprint of it; Promote sends the
// fingerprint back and is refused if anything moved since this sheet was drawn.
function openPromote(it) {
  if (guarded(function () { openPromote(it); })) return;
  var sh = plainSheet("Promote #" + it.id + " to a project");
  sh.body.appendChild(el("div", "empty", "working out what would change…"));
  api("/api/tracking/promote/preview?id=" + it.id).then(function (r) {
    sh.body.innerHTML = "";
    if (!r.ok) { sh.body.appendChild(el("div", "err", r.why)); return; }
    var s = r.summary, pl = r.plan;
    var d = r.item.description || "";
    sh.body.appendChild(el("div", "note", d.length > 300 ? d.slice(0, 300) + "…" : d));

    var nl = labelled("Project name");
    var ni = el("input");
    ni.type = "text";
    ni.value = pl.project.name;
    nl.appendChild(ni);
    nl.appendChild(el("div", "hint", "Suggested from the description — change it freely."));
    sh.body.appendChild(nl);

    var tl = labelled("Type of work");
    var ts = el("select");
    ((META.vocab.project || {}).type || ["other"]).forEach(function (t) {
      ts.appendChild(optionFor(t, t));
    });
    ts.value = "other";
    tl.appendChild(ts);
    sh.body.appendChild(tl);

    var ul = el("ul", "plan");
    bullet(ul, "A new " + pl.project.status + " project"
      + (r.item.customer ? " in " + r.item.customer : "")
      + (pl.project.target_date ? ", target " + pl.project.target_date : "")
      + (pl.project.priority ? ", priority " + pl.project.priority : "") + ".");
    bullet(ul, (s.children_live + s.children_done) + " sub-item"
      + (s.children_live + s.children_done === 1 ? "" : "s") + " (" + s.children_live
      + " live, " + s.children_done + " finished) become items in the project.");
    if (s.deeper) {
      bullet(ul, s.deeper + " row" + (s.deeper === 1 ? "" : "s") + " below them come along"
        + " and stay sub-items of their own item"
        + (s.flatten ? "; " + s.flatten + " that would sit three deep move up one level" : "")
        + ".");
    }
    if (s.links) bullet(ul, s.links + " Jira/Monday link" + (s.links === 1 ? " is" : "s are")
      + " copied to the project (and kept on the item).");
    bullet(ul, "#" + it.id + " is kept, closed, pointing at the project — searching #"
      + it.id + " still finds it. Demote undoes this.");
    sh.body.appendChild(ul);

    if ((r.similar || []).length) {
      var w = el("div", "err");
      w.appendChild(el("div", null, "Projects already in this account — check this isn't "
        + "one of them. Nothing is merged either way:"));
      var ul2 = el("ul");
      r.similar.forEach(function (p) {
        bullet(ul2, "#" + p.id + " " + p.name + (p.archived ? " (archived)" : "")
          + (p.shares.length ? "  — shares: " + p.shares.join(", ") : ""));
      });
      w.appendChild(ul2);
      sh.body.appendChild(w);
    }
    var err = el("div", "err");
    err.hidden = true;
    sh.body.appendChild(err);

    var go = el("button", "primary", "Promote");
    go.addEventListener("click", function () {
      if (!ni.value.trim()) { err.textContent = "a project needs a name"; err.hidden = false; return; }
      go.disabled = true;
      api("/api/tracking/promote", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: it.id, sha: r.sha, name: ni.value.trim(), type: ts.value })
      }).then(function (x) {
        go.disabled = false;
        if (!x.ok) { err.textContent = x.why; err.hidden = false; return; }
        closeSheet();
        toast("Project #" + x.project.id + " “" + x.project.name + "” — "
          + x.project.moved + " item" + (x.project.moved === 1 ? "" : "s") + " moved");
        $("#tproj").value = "";
        refreshMeta();
      });
    });
    sh.foot.appendChild(el("span", "grow"));
    sh.foot.appendChild(go);
  });
}

function openDemote(p) {
  if (guarded(function () { openDemote(p); })) return;
  var sh = plainSheet("Demote project #" + p.id + " to an item");
  sh.body.appendChild(el("div", "empty", "working out what would change…"));
  api("/api/tracking/demote/preview?id=" + p.id).then(function (r) {
    sh.body.innerHTML = "";
    if (!r.ok) { sh.body.appendChild(el("div", "err", r.why)); return; }
    var s = r.summary, pl = r.plan;
    var ul = el("ul", "plan");
    bullet(ul, pl.origin
      ? "#" + pl.origin.id + " — the item this project was promoted from — is reopened as “"
        + pl.origin.status + "”."
      : "A new item is created from the project's name and notes.");
    bullet(ul, s.top + " item" + (s.top === 1 ? "" : "s") + " become its sub-items.");
    bullet(ul, "The project is archived (not deleted) and points at the item.");
    sh.body.appendChild(ul);

    var picks = {};
    if ((r.needs_choice || []).length) {
      sh.body.appendChild(el("div", "note", s.subs + " item" + (s.subs === 1 ? " is" : "s are")
        + " already a sub-item. Under the new item they would sit three deep, which isn't "
        + "allowed — choose for each:"));
      r.needs_choice.forEach(function (n) {
        var row = el("label", "f");
        row.appendChild(el("span", null, "#" + n.id + " (under #" + n.parent_id + ") — "
          + n.description));
        var sel = el("select");
        sel.appendChild(optionFor("flatten", "Flatten: a sub-item of the new item"));
        (r.other_projects || []).forEach(function (o) {
          sel.appendChild(optionFor(String(o.id), "Move to project: " + o.name));
        });
        picks[n.id] = sel;
        row.appendChild(sel);
        sh.body.appendChild(row);
      });
    }
    var err = el("div", "err");
    err.hidden = true;
    sh.body.appendChild(err);
    var go = el("button", "primary", "Demote");
    go.addEventListener("click", function () {
      var choices = {};
      Object.keys(picks).forEach(function (k) { choices[k] = picks[k].value; });
      go.disabled = true;
      api("/api/tracking/demote", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: p.id, sha: r.sha, choices: choices })
      }).then(function (x) {
        go.disabled = false;
        if (!x.ok) { err.textContent = x.why; err.hidden = false; return; }
        closeSheet();
        toast("Now item #" + x.item.id + " with " + x.item.moved + " sub-item"
          + (x.item.moved === 1 ? "" : "s"));
        if ($("#tproj").value === String(p.id)) $("#tproj").value = "";
        api("/api/tracking/meta").then(function (m) {
          if (!m.error) { META = m; fillFilters(); }
          if (TAB === "projects") refreshProjects(); else load("refresh");
        });
      });
    });
    sh.foot.appendChild(el("span", "grow"));
    sh.foot.appendChild(go);
  });
}

// ------------------------------------------------------------------ unsaved edits

// Oct 13: "if I accidentally click out of the popup I lose my edits." Every way
// out of an edit sheet a person can take — Close, Esc, a click outside, or opening
// another sheet over it (an #id chip, Promote, Push, a click from the Gantt) — goes
// through requestClose(), which asks first when the form has unsaved edits:
// Save · Discard · Keep editing. closeSheet() itself stays the unconditional close used
// after a successful save.
//
// "Unsaved" = the user has typed or picked something (a trusted input/change event) AND
// the form's values differ from when it opened — so a field filled in asynchronously, or
// an edit typed and then put back, never nags.
var GUARD = null;          // {form, base, touched, save: fn, prompt: el} for the open sheet
var AFTER = null;          // what to do once the sheet is closed (open the next one)

function formValues(form) {
  var v = {};
  form.querySelectorAll("[data-k]").forEach(function (n) { v[n.dataset.k] = n.value; });
  return JSON.stringify(v);
}

function guardSheet(sheet, form, saveFn) {
  var g = { form: form, base: formValues(form), touched: false, save: saveFn };
  var mark = function (e) { if (e.isTrusted) g.touched = true; };
  form.addEventListener("input", mark);
  form.addEventListener("change", mark);
  var bar = el("div", "unsaved");
  bar.hidden = true;
  bar.appendChild(el("span", "grow", "You have unsaved changes."));
  var s = el("button", "primary", "Save");
  var d = el("button", null, "Discard");
  var k = el("button", null, "Keep editing");
  s.addEventListener("click", function () { bar.hidden = true; g.save(); });
  d.addEventListener("click", function () { closeSheet(); });
  k.addEventListener("click", function () { bar.hidden = true; AFTER = null; });
  bar.appendChild(s);
  bar.appendChild(d);
  bar.appendChild(k);
  // Inside the sticky head, so it is on screen however far the sheet is scrolled.
  sheet.querySelector(".head").appendChild(bar);
  g.prompt = bar;
  GUARD = g;
}

function isDirty() {
  return !!(sheetEl && GUARD && GUARD.touched
            && formValues(GUARD.form) !== GUARD.base);
}

// Close, unless there are unsaved edits — then ask. `next` runs once the sheet is gone
// (after Save succeeds, or on Discard); Keep editing drops it.
function requestClose(next) {
  if (!isDirty()) {
    closeSheet();
    if (next) next();
    return;
  }
  AFTER = next || null;
  GUARD.prompt.hidden = false;
  GUARD.prompt.querySelector("button.primary").focus();
}

// For anything that opens a sheet: true = it asked first, and will reopen `again` itself.
function guarded(again) {
  if (!isDirty()) return false;
  requestClose(again);
  return true;
}

function onEsc(e) {
  if (e.key === "Escape") {
    if (GUARD && GUARD.prompt && !GUARD.prompt.hidden) {   // Esc on the prompt = keep editing
      GUARD.prompt.hidden = true;
      AFTER = null;
      return;
    }
    requestClose();
  }
  if ((e.metaKey || e.ctrlKey) && (e.key === "s" || e.key === "S") && GUARD) {
    e.preventDefault();                                     // not the browser's Save Page
    GUARD.save();
  }
}

function closeSheet() {
  if (sheetEl) sheetEl.remove();
  sheetEl = null;
  GUARD = null;
  document.removeEventListener("keydown", onEsc);
  var next = AFTER;
  AFTER = null;
  if (next) next();
}

function labelled(text) {
  var l = el("label", "f");
  l.appendChild(el("span", null, text));
  return l;
}

// The field guide. Hidden or shown by a class on the form rather than by rebuilding it,
// so toggling never loses what you have typed. Preference is remembered per browser;
// storage is wrapped because it throws in a private window.
function hint(l, field) {
  var t = (META.vocab.field_help || {})[field];
  if (!t) return l;
  var d = el("div", "hint");
  // FIELD_HELP is our own static text, so `backticks` become <code>. Still built with
  // createElement rather than innerHTML — the habit holds even where the input is ours,
  // because the next person to edit this will not check where the string came from.
  t.split(/`([^`]+)`/).forEach(function (part, i) {
    if (i % 2) d.appendChild(el("code", null, part));
    else if (part) d.appendChild(document.createTextNode(part));
  });
  l.appendChild(d);
  return l;
}

function guideOn() {
  try {
    return localStorage.getItem("mt.tracking.guide") !== "0";
  } catch (e) { return true; }
}

function setGuide(on) {
  try { localStorage.setItem("mt.tracking.guide", on ? "1" : "0"); } catch (e) {}
}

function text(key, label, val) {
  var l = labelled(label);
  var i = el("input");
  i.type = "text";
  i.value = val || "";
  i.dataset.k = key;
  l.appendChild(i);
  return hint(l, key);
}

// R4.4c — one line, counted against the limit the server enforces.
function titleField(it) {
  var l = labelled("Title");
  var max = META.vocab.title_max || 120;
  var i = el("input", "titlein");
  i.value = it.title || "";
  i.maxLength = max;
  i.dataset.k = "title";
  // The Oct 13 convention: "XXX - Short title summary". The placeholder shows the code for
  // the account picked in this sheet, and follows the Account picker as it changes.
  var codeFor = function (cid) {
    var c = (META.customers || []).filter(function (x) { return String(x.id) === cid; })[0];
    return ((META.vocab.account_codes || {})[c && c.short_name]
            || META.vocab.internal_code || "INT");
  };
  i.placeholder = codeFor(String(it.customer_id || "")) + " - Short title summary";
  setTimeout(function () {
    var acct = i.closest(".form") && i.closest(".form").querySelector('[data-k="customer_id"]');
    if (acct) acct.addEventListener("change", function () {
      i.placeholder = codeFor(acct.value) + " - Short title summary";
    });
  }, 0);
  var n = el("span", "titlecount");
  var upd = function () {
    var k = i.value.trim().length;
    n.textContent = k + " / " + max;
    n.classList.toggle("long", k > 70);
  };
  i.addEventListener("input", upd);
  upd();
  l.appendChild(i);
  l.appendChild(n);
  return hint(l, "title");
}

// "+ Add entry": a running log at the end of the description, one stamped paragraph per
// update — `[2026-10-26 14:05 CT — Alex] `. Same shape the hand-written entries already
// use, so a reader (or an assistant) can split the history on the brackets. It only
// edits the textarea; Save writes it, through the same rev-guarded save as any edit.
// Kept outside the <label>: a button inside one becomes the label's control.
function entryRow(t) {
  var row = el("div", "entryrow");
  var b = el("button", null, "+ Add entry");
  b.type = "button";
  b.title = "Start a new dated entry at the end of the description";
  b.addEventListener("click", function () {
    var body = t.value.replace(/\s+$/, "");
    t.value = (body ? body + "\n\n" : "") + entryStamp();
    if (GUARD && GUARD.form.contains(t)) GUARD.touched = true;
    t.focus();
    t.setSelectionRange(t.value.length, t.value.length);
    t.scrollTop = t.scrollHeight;
  });
  row.appendChild(b);
  row.appendChild(el("span", "sub",
    "Stamps date, time and your name. Say where it happened; end with NEXT: if there is one."));
  return row;
}

function entryStamp() {
  var d = new Date();
  var p = function (n) { return (n < 10 ? "0" : "") + n; };
  var zone = "";
  try {
    zone = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
      .formatToParts(d).filter(function (x) { return x.type === "timeZoneName"; })[0].value;
  } catch (e) { zone = ""; }
  // CDT/CST → CT, and so on: the season is noise in a log line.
  zone = zone.replace(/^([ECMP])[SD]T$/, "$1T");
  var who = META.vocab.entry_author || "";
  return "[" + d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
    + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + (zone ? " " + zone : "")
    + (who ? " — " + who : "") + "] ";
}

function area(key, label, val) {
  var l = labelled(label);
  var t = el("textarea", "notes");
  t.value = val || "";
  t.dataset.k = key;
  l.appendChild(t);
  return hint(l, key);
}

function dateField(key, label, val) {
  var l = labelled(label);
  var i = el("input");
  i.type = "date";
  i.value = val || "";
  i.dataset.k = key;
  l.appendChild(i);
  return hint(l, key);
}

function selectField(key, label, val, pairs, blank) {
  var l = labelled(label);
  var s = el("select");
  s.dataset.k = key;
  s.appendChild(optionFor("", blank || "—"));
  pairs.forEach(function (p) { s.appendChild(optionFor(p[0], p[1])); });
  s.value = val == null ? "" : String(val);
  l.appendChild(s);
  return hint(l, key);
}

// A controlled field. The current value is injected as an extra option when it is not
// canonical, so the row renders truthfully and stays editable — without that, opening an
// old row would silently show the first option and saving would rewrite it.
function vocabField(field, it) {
  var l = labelled(LABEL[field]);
  var s = el("select");
  s.dataset.k = field;
  var vals = META.vocab[field] || [];
  vals.forEach(function (v) { s.appendChild(optionFor(v, v)); });
  var cur = it[field];
  if (cur && vals.indexOf(cur) === -1) {
    s.appendChild(optionFor(cur, cur + "  (legacy — pick a replacement)"));
  }
  s.value = cur || vals[0] || "";
  l.appendChild(s);
  hint(l, field);
  if (field === "report_scope") {
    // Two different things: what the FIELD does (static, above) and what the value you
    // currently have selected means (live, here). Both earn their space on this one.
    var meaning = el("div", "hint pick-meaning");
    var draw = function () {
      var m = (META.vocab.scope_help || {})[s.value];
      meaning.textContent = m ? s.value + " — " + m : "";
    };
    s.addEventListener("change", draw);
    draw();
    l.appendChild(meaning);
  }
  return l;
}

// Owner is the 237-spelling field. The picker is the easy path and can only produce a
// canonical contact name; the free-text box stays for people who are not contacts yet,
// and is disabled while a contact is selected so the two cannot disagree.
function ownerField(it) {
  var l = labelled("Owner");
  var s = el("select");
  s.dataset.k = "owner_contact_id";
  s.appendChild(optionFor("", "— free text below —"));
  CONTACTS.forEach(function (c) {
    s.appendChild(optionFor(String(c.id), c.name + (c.customer ? "  (" + c.customer + ")" : "")));
  });
  s.value = it.owner_contact_id == null ? "" : String(it.owner_contact_id);
  l.appendChild(s);

  var i = el("input");
  i.type = "text";
  i.value = it.owner || "";
  i.dataset.k = "owner";
  i.placeholder = "name as free text";
  l.appendChild(i);

  var sync = function () {
    i.disabled = !!s.value;
    i.title = s.value ? "the selected contact's name is used" : "";
  };
  s.addEventListener("change", sync);
  sync();
  return hint(l, "owner");
}

// Linked files. The DB has been pointing at local markdown for months — in prose, inside
// `source` — so this both shows real links and offers the paths already buried in this
// row's own text, rather than making the user retype what is already written down.
function linkSection(it) {
  var wrap = el("div");
  wrap.appendChild(el("div", "sub", "Linked files"));
  var box = el("div");
  wrap.appendChild(box);

  function draw() {
    api("/api/tracking/links?table=action_items&id=" + it.id).then(function (r) {
      box.innerHTML = "";
      if (r.error) { box.appendChild(el("div", "err", r.error)); return; }
      var files = (r.links || []).filter(function (l) { return l.system === "file"; });
      var other = (r.links || []).filter(function (l) { return l.system !== "file"; });

      files.forEach(function (l) {
        var row = el("div", "lnk");
        row.appendChild(el("span", "tag r", l.role));
        var a = el("a", "p", l.external_item_id);
        a.href = "#";
        a.addEventListener("click", function (e) {
          e.preventDefault();
          openLinkedFile(l.external_container_id || "workspace", l.external_item_id);
        });
        row.appendChild(a);
        var rm = el("button", "tap r", "Unlink");
        rm.addEventListener("click", function () {
          rm.disabled = true;
          api("/api/tracking/link", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ remove: l.id })
          }).then(function (x) {
            if (!x.ok) { rm.disabled = false; return; }
            draw();
          });
        });
        row.appendChild(rm);
        box.appendChild(row);
      });

      (r.suggest || []).forEach(function (sg) {
        var row = el("div", "lnk sug");
        row.appendChild(el("span", "tag r", sg.role));
        row.appendChild(el("span", "p", sg.path));
        var add = el("button", "tap r", "Link this");
        add.title = "found in this item's own source/description text";
        add.addEventListener("click", function () {
          add.disabled = true;
          api("/api/tracking/link", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ table: "action_items", id: it.id,
                                   path: sg.path, role: sg.role })
          }).then(function (x) {
            if (!x.ok) { add.disabled = false; return; }
            draw();
          });
        });
        row.appendChild(add);
        box.appendChild(row);
      });

      if (other.length) {
        box.appendChild(el("div", "hint", other.length + " link(s) to other systems: "
          + other.map(function (l) { return l.system + ":" + l.external_item_id; })
                 .join(", ") + " — managed where they were created."));
      }
      if (!files.length && !(r.suggest || []).length) {
        box.appendChild(el("div", "hint",
          "No files linked, and no path found in this item's text. Link one from the "
          + "Files page, or paste a workspace path into Source and reopen."));
      }
    });
  }
  draw();
  return wrap;
}

// Opening a linked file borrows the Files page editor rather than duplicating it: its
// script is loaded once, then mtOpenFile() is called. One editor, one set of save guards.
var _filesJs = null;
function openLinkedFile(root, path) {
  function go() {
    api("/api/file?root=" + encodeURIComponent(root)
        + "&path=" + encodeURIComponent(path)).then(function (r) {
      if (!r.ok) { window.alert(r.error || "could not open"); return; }
      window.mtOpenFile({ root: r.root, path: r.path,
                          name: r.path.split("/").pop(), bytes: r.bytes });
    });
  }
  if (window.mtOpenFile) return go();
  if (!_filesJs) {
    _filesJs = new Promise(function (res, rej) {
      var sc = document.createElement("script");
      sc.src = "/static/files.js";
      sc.onload = res;
      sc.onerror = rej;
      document.head.appendChild(sc);
    });
  }
  _filesJs.then(go);
}

// The row moved since this sheet opened (R4.2): someone else's change is shown, and
// nothing is overwritten until the user picks. Keep mine re-sends against the new rev;
// take theirs reopens the sheet on the current row.
function showConflict(it, fields, err, btn, r) {
  err.innerHTML = "";
  err.hidden = false;
  err.appendChild(el("div", null, r.why));
  var ul = el("ul");
  (r.conflict.changes || []).forEach(function (c) {
    ul.appendChild(el("li", null, c.field + ": " + (c.old == null ? "—" : c.old) + " → "
      + (c.new == null ? "—" : c.new) + "  (" + (c.by || "another writer") + ", " + c.at + ")"));
  });
  err.appendChild(ul);
  var keep = el("button", "tap", "Keep mine");
  keep.addEventListener("click", function () {
    sendSave(it, false, fields, err, btn, r.conflict.rev);
  });
  var take = el("button", "tap", "Take theirs");
  take.addEventListener("click", function () {
    // Taking theirs IS discarding mine — no unsaved-edits prompt on the way.
    closeSheet();
    if (r.conflict.item) openSheet(r.conflict.item);
  });
  err.appendChild(keep);
  err.appendChild(take);
}

function doSave(it, isNew, form, err, btn) {
  var fields = {};
  form.querySelectorAll("[data-k]").forEach(function (n) {
    if (n.disabled) return;
    fields[n.dataset.k] = n.value;
  });
  // An unchanged legacy value is allowed through by tracking.py, but sending a field the
  // user never touched invites that. Only send what actually differs on an edit.
  if ("parent_id" in fields) fields.parent_id = fields.parent_id.replace(/[#\s]/g, "");
  if (!isNew) {
    Object.keys(fields).forEach(function (k) {
      var was = it[k] == null ? "" : String(it[k]);
      if (String(fields[k]) === was) delete fields[k];
    });
    if (!Object.keys(fields).length) { closeSheet(); return; }
    if ("project_id" in fields && it.child_count && !window.confirm(
          "#" + it.id + " has " + it.child_count + " sub-item"
          + (it.child_count === 1 ? "" : "s") + ". Changing its project moves "
          + (it.child_count === 1 ? "it" : "them") + " too.\n\nGo ahead?")) {
      return;
    }
  }
  sendSave(it, isNew, fields, err, btn, it.rev);
}

function sendSave(it, isNew, fields, err, btn, rev) {
  btn.disabled = true;
  err.hidden = true;
  api("/api/tracking/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: isNew ? null : it.id, fields: fields,
                           rev: isNew || rev == null ? null : rev })
  }).then(function (r) {
    btn.disabled = false;
    if (r.conflict) return showConflict(it, fields, err, btn, r);
    if (!r.ok) {
      err.textContent = r.why || "save failed";
      err.hidden = false;
      return;
    }
    if (r.item && r.item.carried && r.item.carried.length) {
      toast(r.item.carried.length + " sub-item" + (r.item.carried.length === 1 ? "" : "s")
        + " moved with it");
    }
    closeSheet();
    refreshMeta();
  }).catch(function (e) {
    btn.disabled = false;
    err.textContent = String(e);
    err.hidden = false;
  });
}

boot();
})();

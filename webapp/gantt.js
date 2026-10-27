// gantt.js — the Gantt window (R4.4, read-only).
//
// Vanilla, no dependencies, hand-built SVG — the house rule, and decision 4 (Oct 9).
// Everything from the DB goes in through textContent, never innerHTML.
//
// Layout: one scroller holding a sticky time header and a body row; the label column is
// HTML (position: sticky, left) so long titles ellipsize and stay readable while the
// timeline scrolls under them. The timeline itself is one SVG per tier.
//
// What is drawn, per item (rules from TRACKING-PLAN.md § R4):
//   a bar      start → end (end falls back to due) — the PLAN
//   ◆ filled   the due date when there is no plan yet — the COMMITMENT, alone
//   ◇ hollow   the due date sitting on top of a planned bar
//   red ring   overdue, or a plan that ends after its due date
//   ◆ larger   item_type = milestone
//   arrows     finish-to-start links, right-angle elbows; dashed = met anyway,
//              red = sequence conflict
// Clicking a row opens that item in the list window (BroadcastChannel 'mt-tracking').
(function () {
"use strict";

var NS = "http://www.w3.org/2000/svg";
var $ = function (s) { return document.querySelector(s); };
var api = function (p) { return fetch(p).then(function (r) { return r.json(); }); };
var post = function (p, body) {
  return fetch(p, { method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body) }).then(function (r) { return r.json(); });
};
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function sv(tag, attrs, text) {
  var e = document.createElementNS(NS, tag);
  // var() is reliable in CSS, not in presentation attributes — those go through style.
  for (var k in attrs) {
    if (attrs[k] == null) continue;
    if (typeof attrs[k] === "string" && attrs[k].indexOf("var(") >= 0) {
      e.style.setProperty(k, attrs[k]);
    } else {
      e.setAttribute(k, attrs[k]);
    }
  }
  if (text != null) e.textContent = text;
  return e;
}
function store(k, v) {
  try {
    if (v === undefined) return localStorage.getItem("mt.gantt." + k);
    localStorage.setItem("mt.gantt." + k, v);
  } catch (e) { return null; }
}

// ------------------------------------------------------------------ state

var Q = new URLSearchParams(location.search);
var S = {
  project: Q.get("project") || "",
  customer: Q.get("customer") || "",
  hide: Q.get("hide_done") !== "0",
  zoom: store("zoom") || "",            // "" = pick from the span
  by: store("by") || "status"
};
var META = { customers: [], projects: [] };
var DATA = null;
var LIVE = null;                        // R4.4b, started with the first data
var FIRST = true;                       // scroll to today on the first draw only

var PPD = { day: 30, week: 11, month: 3.6 };   // pixels per day
var LABELW = 380, ROWH = 28, GRPH = 34, HEADH = 46;
// The name column: auto-fitted to the longest title unless dragged (then remembered).
var LABEL_SET = +(store("labelw") || 0);
if (LABEL_SET) LABELW = LABEL_SET;
var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
           "Dec"];

var CH = null;
try { CH = new BroadcastChannel("mt-tracking"); } catch (e) { CH = null; }

// ------------------------------------------------------------------ dates

function dn(s) {                      // ISO date -> day number (UTC, no DST surprises)
  return Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) / 864e5;
}
function iso(n) { return new Date(n * 864e5).toISOString().slice(0, 10); }
function dow(n) { return new Date(n * 864e5).getUTCDay(); }   // 0 = Sunday
function shortDate(s) {
  if (!s) return "";
  var y = s.slice(0, 4), lbl = MON[+s.slice(5, 7) - 1] + " " + (+s.slice(8, 10));
  return y === String(new Date().getFullYear()) ? lbl : lbl + " " + y;
}

// ------------------------------------------------------------------ colour

function cssVar(name) { return "var(--g-" + name + ")"; }
var STATUS_COL = { open: "open", waiting: "waiting", blocked: "blocked" };
var WHO_LABEL = { me: "Me", shared: "Shared", them: "Them", none: "Unassigned" };

// Colour follows the entity, never its rank: an account keeps its slot (by customer id)
// whatever else is on screen.
function custSlot(short) {
  var ids = META.customers.slice().sort(function (a, b) { return a.id - b.id; });
  for (var i = 0; i < ids.length; i++) {
    if (ids[i].short_name === short) return "c" + Math.min(i + 1, 8);
  }
  return "done";
}
function isDone(r) {
  return r.status === "done" || r.status === "wont_do" || r.status === "closed"
      || r.status === "completed";
}
function colourFor(r) {
  if (isDone(r)) return cssVar("done");
  if (S.by === "cust") return cssVar(custSlot(r.cust));
  if (S.by === "who") return cssVar(r.who || "none");
  // The item's own status. A derived block (a predecessor still open) is NOT painted
  // red: in a planned chain nearly every successor is "blocked" in that sense, and a
  // wall of red would say nothing. It shows as a chip on the label instead.
  return cssVar(STATUS_COL[r.status] || "open");
}

// ------------------------------------------------------------------ boot

function boot() {
  Promise.all([api("/api/tracking/meta"), fetchData()]).then(function (r) {
    META = r[0] || META;
    fillScope();
    wire();
    take(r[1]);
  }).catch(function (e) { fail(String(e)); });
}

function fetchData() {
  var p = new URLSearchParams();
  if (S.project) p.set("project", S.project);
  if (S.customer) p.set("customer", S.customer);
  p.set("hide_done", S.hide ? "1" : "0");
  return api("/api/tracking/gantt?" + p.toString());
}

// `keep` = an update, not a new view: the redraw keeps both scroll positions.
function reload(keep) {
  fetchData().then(function (d) { take(d, keep === true); })
    .catch(function (e) { fail(String(e)); });
}

function take(d, keep) {
  DATA = d;
  if (d.error) return fail(d.error);
  (d.rows || []).forEach(function (r) { r.conflictSrv = r.conflict; });
  d.linksSrv = d.links || [];             // R4.5b: links on screen = these + staged edits
  if (FIRST_TAKE) { FIRST_TAKE = false; restorePending(); }
  reapplyPending();
  $("#gname").textContent = d.title || "";
  document.title = "mt — gantt · " + (d.title || "");
  startLive(d);
  render(null, keep);
}

// Live updates (R4.4b). The Gantt's whole projection is a few KB, so an update refetches
// it and redraws with the scroll kept — but never while a tooltip is up (that is the
// "repaint under the cursor" case) and never while paused. Above the cap (a cut view)
// the badge asks for a refresh instead.
function startLive(d) {
  if (LIVE) { LIVE.since(d.last_event); return; }
  LIVE = window.mtLive({
    key: "gantt", since: d.last_event || -1, mount: $(".gctl"),
    rows: function () { return DATA && DATA.cut ? Infinity : (DATA ? DATA.rows.length : 0); },
    // R4.5: also held while a drag is under way or edits are staged in the tray.
    busy: function () { return !$("#gtip").hidden || !!DRAG || pendingCount() > 0; },
    apply: function () { reload(true); }
  });
}

function fail(msg) {
  var box = $("#gscroll");
  box.innerHTML = "";
  box.appendChild(el("div", "err", msg));
}

// The scope picker: every project, "No project", and every account — so the window can
// move on without going back to the list.
function fillScope() {
  // Oct 13: grouped by account (A–Z), each group led by its whole-account view, then its
  // projects A–Z with their open count; projects with no account, and "No project", last.
  var sel = $("#gscope");
  sel.innerHTML = "";
  var custs = (META.customers || []).slice().sort(function (a, b) { return a.name.localeCompare(b.name); });
  var byCust = {};
  (META.projects || []).forEach(function (p) {
    (byCust[p.customer || ""] = byCust[p.customer || ""] || []).push(p);
  });
  function projOpt(p) {
    var o = el("option", null, "   " + p.name + "  (" + (p.live || 0) + " open)");
    o.value = "p:" + p.id;
    return o;
  }
  var byName = function (a, b) { return (a.name || "").localeCompare(b.name || ""); };
  custs.forEach(function (c) {
    var g = el("optgroup");
    g.label = c.name;
    var all = el("option", null, "All of " + c.name);
    all.value = "c:" + c.short_name;
    g.appendChild(all);
    (byCust[c.short_name] || []).sort(byName).forEach(function (p) { g.appendChild(projOpt(p)); });
    sel.appendChild(g);
  });
  var other = el("optgroup");
  other.label = "No account";
  (byCust[""] || []).sort(byName).forEach(function (p) { other.appendChild(projOpt(p)); });
  var none = el("option", null, "No project (every account)");
  none.value = "p:none";
  other.appendChild(none);
  sel.appendChild(other);
  sel.value = S.project ? "p:" + S.project : (S.customer ? "c:" + S.customer : "");
}

function wire() {
  $("#gscope").addEventListener("change", function () {
    var v = this.value;
    S.project = v.indexOf("p:") === 0 ? v.slice(2) : "";
    S.customer = v.indexOf("c:") === 0 ? v.slice(2) : "";
    var p = new URLSearchParams();
    if (S.project) p.set("project", S.project);
    if (S.customer) p.set("customer", S.customer);
    if (!S.hide) p.set("hide_done", "0");
    history.replaceState(null, "", "/tracking/gantt?" + p.toString());
    FIRST = true;
    // Staged edits are kept per scope (every stage persists under its own scope's key);
    // the new scope loads its own.
    PEND = {};
    HIST = [];
    FIRST_TAKE = true;
    reload();
  });
  document.querySelectorAll("#gzoom button").forEach(function (b) {
    b.addEventListener("click", function () {
      var keep = centreDay();
      S.zoom = b.dataset.z;
      store("zoom", S.zoom);
      render(keep);
    });
  });
  $("#gby").value = S.by;
  $("#gby").addEventListener("change", function () {
    S.by = this.value;
    store("by", S.by);
    render(centreDay());
  });
  $("#ghide").checked = S.hide;
  $("#ghide").addEventListener("change", function () {
    S.hide = this.checked;
    reload();
  });
  $("#gtoday").addEventListener("click", function () { scrollToDay(dn(DATA.today), 1 / 3); });
  $("#greload").addEventListener("click", function () { reload(true); });
  $("#gpng").addEventListener("click", function () { exportSlide("png"); });
  $("#gsvg").addEventListener("click", function () { exportSlide("svg"); });
  window.addEventListener("keydown", function (e) {
    if (e.target && /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
    if (e.key === "t") scrollToDay(dn(DATA.today), 1 / 3);
    if (e.key === "Escape" && DRAG) cancelDrag();
    if (e.key === "Escape") closeLinkMenu();
    if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z") && !e.shiftKey) {
      e.preventDefault();
      undoLast();
    }
  });
}

// ------------------------------------------------------------------ geometry

var G = null;     // the current layout: from, ppd, width, line list, y by id

function zoomFor(d) {
  if (S.zoom) return S.zoom;
  var span = dn(d.range.to) - dn(d.range.from);
  return span <= 60 ? "day" : span <= 240 ? "week" : "month";
}

// `o` overrides the screen layout — the slide export uses it: {zoom, ppd, from, to,
// rowh, grph, maxH}. Lines that would pass maxH are left off and counted in `omitted`.
function layout(d, o) {
  o = o || {};
  var z = o.zoom || zoomFor(d), ppd = o.ppd || PPD[z];
  var rowh = o.rowh || ROWH, grph = o.grph || GRPH;
  // Room on the right for the last date label, at every zoom.
  var from = o.from != null ? o.from : dn(d.range.from);
  var to = o.to != null ? o.to : dn(d.range.to) + Math.ceil(150 / ppd);
  var lines = [], y = 0, byId = {}, omitted = 0;
  var rowsByGroup = {};
  d.rows.forEach(function (r) {
    (rowsByGroup[String(r.g)] = rowsByGroup[String(r.g)] || []).push(r);
  });
  d.groups.forEach(function (g) {
    var rs = rowsByGroup[String(g.id)] || [];
    if (!rs.length) return;
    if (o.maxH && y + grph + rowh > o.maxH) { omitted += rs.length; return; }
    lines.push({ kind: "grp", g: g, y: y, h: grph });
    y += grph;
    rs.forEach(function (r) {
      if (o.maxH && y + rowh > o.maxH) { omitted++; return; }
      lines.push({ kind: "row", r: r, y: y, h: rowh });
      byId[r.id] = { r: r, cy: y + rowh / 2, line: lines.length - 1 };
      y += rowh;
    });
  });
  return { zoom: z, ppd: ppd, from: from, to: to, days: to - from + 1, rowh: rowh,
           width: Math.round((to - from + 1) * ppd), height: Math.max(y, o.maxH ? y : 120),
           lines: lines, byId: byId, omitted: omitted };
}

function X(s) { return (dn(s) - G.from) * G.ppd; }
function Xn(n) { return (n - G.from) * G.ppd; }

// The plan bar of a row, or null. Mirrors tracking._begin/_finish: a start with no end
// runs to the due date; an end with no start is a one-day bar.
function barOf(r) {
  if (r.type === "milestone" || (!r.start && !r.end)) return null;
  var b = r.start || r.end, e = r.end || r.due || b;
  if (e < b) e = b;
  return { b: b, e: e };
}
function markDay(r) {                     // where a milestone / due-only diamond sits
  return r.type === "milestone" ? (r.end || r.due || r.start) : r.due;
}
// Where an arrow leaves (the finish) and lands (the begin).
function finishX(r) {
  var b = barOf(r);
  if (b) return X(b.e) + G.ppd;
  var m = markDay(r);
  return m ? X(m) + G.ppd / 2 + 7 : null;
}
function beginX(r) {
  var b = barOf(r);
  if (b) return X(b.b);
  var m = markDay(r);
  return m ? X(m) + G.ppd / 2 - 7 : null;
}

// ------------------------------------------------------------------ render

function centreDay() {
  if (!G) return null;
  var sc = $("#gscroll");
  return G.from + (sc.scrollLeft + (sc.clientWidth - LABELW) / 2) / G.ppd;
}
function scrollToDay(n, frac) {
  if (!G) return;
  var sc = $("#gscroll");
  sc.scrollLeft = Math.max(0, Xn(n) - (sc.clientWidth - LABELW) * frac);
}

function render(keepCentre, keepScroll) {
  var d = DATA;
  if (!d || !d.range) return;
  var sc = $("#gscroll");
  var was = { left: sc.scrollLeft, top: sc.scrollTop };
  G = layout(d);
  refreshConflicts();
  renderTray();
  sc.innerHTML = "";
  document.querySelectorAll("#gzoom button").forEach(function (b) {
    b.classList.toggle("on", b.dataset.z === G.zoom);
  });
  renderLegend();
  renderNote();
  if (!d.rows.length) {
    sc.appendChild(el("div", "empty", S.hide
      ? "Nothing open here. Untick Hide done to see finished work."
      : "No items in this view."));
    return;
  }

  var head = el("div", "ghead");
  var corner = el("div", "gcorner", d.rows.length + " item" + (d.rows.length === 1 ? "" : "s")
                  + (d.cut ? " of " + d.total : ""));
  corner.style.width = LABELW + "px";
  corner.style.height = HEADH + "px";
  head.appendChild(corner);
  head.appendChild(renderAxis());
  sc.appendChild(head);

  var body = el("div", "gbody");
  var labels = renderLabels();
  body.appendChild(labels);
  body.appendChild(renderChart());
  sc.appendChild(body);
  resizer(labels, corner);
  // R4.4c: unless you have dragged it, the name column fits the longest title.
  // Not while edits are staged: the ● marker on an edited row widens its label, and a
  // refit would shift the whole chart sideways under the mouse. Refits after Save/Discard.
  if (!LABEL_SET && !pendingCount()) {
    var fit = fitWidth(labels);
    if (Math.abs(fit - LABELW) > 4) { LABELW = fit; return render(keepCentre, keepScroll); }
  }

  if (keepScroll) {
    sc.scrollLeft = was.left;
    sc.scrollTop = was.top;
  } else if (keepCentre != null) {
    sc.scrollLeft = Math.max(0, Xn(keepCentre) - (sc.clientWidth - LABELW) / 2);
  } else if (FIRST) {
    FIRST = false;
    scrollToDay(dn(d.today), 1 / 3);
  }
}

function renderNote() {
  var d = DATA, n = $("#gnote"), msgs = [];
  var planned = d.rows.some(function (r) { return r.start || r.end; });
  if (pendingCount()) {
    // While anything is staged the banner keeps its place (different words) — if it
    // vanished when the first plan was drawn, the chart would jump up under the mouse.
    msgs.push("Unsaved changes are in the tray — nothing is written until you press Save.");
  } else if (d.rows.length && !planned) {
    msgs.push("No plan dates in this view yet — every item shows its due date as ◆. "
      + "Drag across a row to draw its plan, or set Start / End in its edit sheet.");
  }
  if (d.cut) {
    msgs.push("Showing the first " + d.rows.length + " of " + d.total
      + " — pick a single project to see the rest.");
  }
  n.textContent = msgs.join("  ");
  n.hidden = !msgs.length;
}

// Two tiers. Day: months over day numbers. Week: months over Monday dates.
// Month: years over month names.
function renderAxis() {
  var s = sv("svg", { width: G.width, height: HEADH, "aria-hidden": "true" });
  var mid = 22;
  s.appendChild(sv("line", { x1: 0, x2: G.width, y1: mid, y2: mid,
                             stroke: "var(--line)" }));
  var lastTop = null;
  for (var n = G.from; n <= G.to; n++) {
    var s_ = iso(n), x = Xn(n), day = +s_.slice(8, 10), m = +s_.slice(5, 7);
    var topKey = G.zoom === "month" ? s_.slice(0, 4) : s_.slice(0, 7);
    if (topKey !== lastTop) {
      if (lastTop !== null) {
        s.appendChild(sv("line", { x1: x, x2: x, y1: 0, y2: HEADH, stroke: "var(--line-2)" }));
      }
      s.appendChild(sv("text", { x: x + 6, y: 15, "font-size": 11.5, "font-weight": 600,
        fill: "var(--ink-2)" }, G.zoom === "month" ? s_.slice(0, 4)
        : MON[m - 1] + " " + s_.slice(0, 4)));
      lastTop = topKey;
    }
    if (G.zoom === "day") {
      var we = dow(n) === 0 || dow(n) === 6;
      s.appendChild(sv("text", { x: x + G.ppd / 2, y: 38, "font-size": 10.5,
        "text-anchor": "middle", fill: we ? "var(--ink-3)" : "var(--ink-2)",
        "font-family": "var(--m)" }, String(day)));
    } else if (G.zoom === "week" && dow(n) === 1) {
      s.appendChild(sv("line", { x1: x, x2: x, y1: mid, y2: HEADH, stroke: "var(--line)" }));
      s.appendChild(sv("text", { x: x + 4, y: 38, "font-size": 10.5, fill: "var(--ink-2)",
        "font-family": "var(--m)" }, String(day)));
    } else if (G.zoom === "month" && day === 1) {
      s.appendChild(sv("line", { x1: x, x2: x, y1: mid, y2: HEADH, stroke: "var(--line)" }));
      s.appendChild(sv("text", { x: x + 4, y: 38, "font-size": 10.5, fill: "var(--ink-2)" },
        MON[m - 1]));
    }
  }
  var t = Xn(dn(DATA.today)) + G.ppd / 2;
  s.appendChild(sv("rect", { x: t - 19, y: mid + 3, width: 38, height: 16, rx: 8,
                             fill: "var(--g-today)" }));
  s.appendChild(sv("text", { x: t, y: mid + 15, "font-size": 10.5, "font-weight": 600,
    "text-anchor": "middle", fill: "var(--surface)" }, "Today"));
  return s;
}

function renderLabels() {
  var box = el("div", "glabels");
  box.style.width = LABELW + "px";
  G.lines.forEach(function (ln) {
    var row = el("div", "gl" + (ln.kind === "grp" ? " grp" : ""));
    row.style.height = ln.h + "px";
    if (ln.kind === "grp") {
      var g = ln.g;
      row.appendChild(el("span", "gt", g.name));
      row.appendChild(el("span", "gn", g.live + " open"
        + (g.overdue ? " · " + g.overdue + " overdue" : "")));
    } else {
      var r = ln.r;
      if (isDone(r)) row.classList.add("done");
      if (PEND[r.id]) {
        row.classList.add("pend");
        row.title = "Unsaved change — see the tray";
      }
      row.style.paddingLeft = (10 + 16 * r.depth) + "px";
      var sw = el("span", "sw");
      sw.style.background = colourFor(r);
      row.appendChild(sw);
      row.appendChild(el("span", "gid", (r.depth ? "↳ " : "") + "#" + r.id));
      row.appendChild(el("span", "gt", r.t));
      // Status in words on every row: colour never carries it alone (and light-mode
      // yellow is under 3:1 on white).
      // Waiting on a predecessor is not flagged here — the arrow already says it, and a
      // chip on every successor read like an alert (Oct 13). Green "ready" when
      // every predecessor is finished; red only for a real blocked status, below.
      if (r.ready) row.appendChild(el("span", "gs ready", "ready"));
      else row.appendChild(el("span", "gs " + r.status, r.status.replace("_", " ")));
      if (r.overdue) row.appendChild(el("span", "gs od", "overdue"));
      if (r.conflict) row.appendChild(el("span", "gs cf", "⚠ conflict"));
      row.dataset.id = r.id;
      hook(row, r);
    }
    box.appendChild(row);
  });
  return box;
}

// ------------------------------------------------------------------ name column width

// Natural width of the widest row: padding + fixed bits + the title's full text width.
function fitWidth(labels) {
  var w = 0;
  // The title cell is flex:1, so it always fills the column and its own width says
  // nothing about the text. Measure with every cell at its natural width instead.
  labels.classList.add("measuring");
  labels.querySelectorAll(".gl").forEach(function (row) {
    var cs = getComputedStyle(row), need = parseFloat(cs.paddingLeft)
      + parseFloat(cs.paddingRight), kids = row.children, gap = 6;
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i];
      need += k.offsetWidth + gap;
    }
    w = Math.max(w, need);
  });
  labels.classList.remove("measuring");
  return Math.round(Math.max(240, Math.min(w + 4, window.innerWidth * 0.45)));
}

function resizer(labels, corner) {
  var h = el("div", "gresize");
  h.title = "Drag to resize · double-click to fit the longest title";
  labels.appendChild(h);
  h.addEventListener("dblclick", function () {
    LABEL_SET = 0;
    store("labelw", "0");
    LABELW = fitWidth(labels);
    render(null, true);
  });
  h.addEventListener("mousedown", function (e) {
    e.preventDefault();
    var x0 = e.clientX, w0 = LABELW;
    function move(ev) {
      LABELW = Math.max(200, Math.min(window.innerWidth * 0.8, w0 + ev.clientX - x0));
      labels.style.width = corner.style.width = LABELW + "px";
    }
    function up() {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      LABEL_SET = Math.round(LABELW);
      store("labelw", String(LABEL_SET));
      render(null, true);
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

function diamond(cx, cy, r, attrs) {
  return sv("path", Object.assign({
    d: "M" + cx + " " + (cy - r) + "L" + (cx + r) + " " + cy + "L" + cx + " " + (cy + r)
       + "L" + (cx - r) + " " + cy + "Z" }, attrs));
}

function renderChart() {
  var s = sv("svg", { width: G.width, height: G.height, role: "img",
                      "aria-label": "Gantt chart, " + DATA.rows.length + " items" });
  var defs = sv("defs");
  [["a", "var(--g-link)"], ["ac", "var(--g-crit)"], ["l", "var(--accent)"]].forEach(function (m) {
    var mk = sv("marker", { id: "gm-" + m[0], viewBox: "0 0 8 8", refX: 7, refY: 4,
      markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
      markerUnits: "userSpaceOnUse" });
    mk.appendChild(sv("path", { d: "M0 0L8 4L0 8Z", fill: m[1] }));
    defs.appendChild(mk);
  });
  s.appendChild(defs);

  // Ground: group bands, weekends, gridlines, today.
  var ground = sv("g");
  G.lines.forEach(function (ln) {
    if (ln.kind === "grp") {
      ground.appendChild(sv("rect", { x: 0, y: ln.y, width: G.width, height: ln.h,
                                      fill: "var(--g-group)" }));
    }
  });
  if (G.zoom === "day") {           // at week zoom the bands turn into stripes
    for (var n = G.from; n <= G.to; n++) {
      if (dow(n) === 6) {
        ground.appendChild(sv("rect", { x: Xn(n), y: 0, width: G.ppd * 2, height: G.height,
                                        fill: "var(--g-weekend)", opacity: 0.8 }));
      }
    }
  }
  for (var m = G.from; m <= G.to; m++) {
    var d_ = iso(m);
    var grid = G.zoom === "month" ? d_.slice(8) === "01" : dow(m) === 1;
    if (grid) {
      ground.appendChild(sv("line", { x1: Xn(m), x2: Xn(m), y1: 0, y2: G.height,
                                      stroke: "var(--g-grid)" }));
    }
  }
  var hi = sv("rect", { x: 0, y: 0, width: G.width, height: G.rowh, fill: "var(--g-rowhi)",
                        opacity: 0 });
  ground.appendChild(hi);
  var tx = Xn(dn(DATA.today)) + G.ppd / 2;
  ground.appendChild(sv("line", { x1: tx, x2: tx, y1: 0, y2: G.height,
    stroke: "var(--g-today)", "stroke-width": 2 }));
  s.appendChild(ground);
  G.hi = hi;

  // Links under the marks, so an arrowhead never hides a diamond.
  G.linksG = sv("g", { fill: "none", "stroke-width": 1.5 });
  drawLinks();
  s.appendChild(G.linksG);

  var marks = sv("g");
  G.rowG = {};
  G.lineOf = {};
  // Link dots get their own TOP layer (appended below, after the arrow click paths):
  // a dot sits right where its arrow starts, and must win the press.
  if (!G.export) { G.connG = sv("g"); G.connOf = {}; }
  G.lines.forEach(function (ln) {
    if (ln.kind === "grp") return drawGroup(marks, ln);
    G.lineOf[ln.r.id] = ln;
    G.rowG[ln.r.id] = drawRow(marks, ln);
  });
  s.appendChild(marks);
  if (!G.export) {
    // Above the bars: a wide invisible stroke along each arrow, so an arrow can be
    // clicked (its menu: met anyway / remove / push), and the rubber band for drawing.
    G.linkHitG = sv("g", { fill: "none" });
    s.appendChild(G.linkHitG);
    s.appendChild(G.connG);
    G.bandG = sv("g");
    s.appendChild(G.bandG);
    drawLinks();
    G.svg = s;
    s.addEventListener("pointerdown", onDown);
    s.addEventListener("pointermove", onMove);
    s.addEventListener("pointerup", onUp);
    s.addEventListener("pointercancel", function () { cancelDrag(); });
  }
  return s;
}

// FS arrows. Conflicts are worked out HERE from the dates on screen (same rule as
// tracking._conflict: predecessor's finish + lag in working days after the successor's
// begin), so a drag shows a new conflict — or clears one — before anything is saved.
function drawLinks() {
  var links = G.linksG, hits = G.linkHitG;
  while (links.firstChild) links.removeChild(links.firstChild);
  if (hits) while (hits.firstChild) hits.removeChild(hits.firstChild);
  (DATA.links || []).forEach(function (l) {
    var p = G.byId[l.pred], q = G.byId[l.succ];
    if (!p || !q) return;
    var sx = finishX(p.r), ex = beginX(q.r);
    if (sx == null || ex == null) return;
    var sy = p.cy, ey = q.cy, d, bad = linkConflict(l);
    if (ex - sx >= 14) {
      d = "M" + sx + " " + sy + "H" + (sx + 6) + "V" + ey + "H" + ex;
    } else {                          // backwards: drop to the row edge and come round
      var yb = sy + (ey > sy ? G.rowh / 2 : -G.rowh / 2);
      d = "M" + sx + " " + sy + "H" + (sx + 6) + "V" + yb + "H" + (ex - 8) + "V" + ey
          + "H" + ex;
    }
    var staged = !G.export && (l.staged || l.id < 0);
    links.appendChild(sv("path", { d: d,
      stroke: bad ? "var(--g-crit)" : staged ? "var(--accent)" : "var(--g-link)",
      "stroke-width": staged ? 2 : null,
      "stroke-dasharray": l.met ? "4 3" : null, opacity: l.done ? 0.45 : 1,
      "marker-end": "url(#gm-" + (bad ? "ac" : staged ? "l" : "a") + ")" }));
    if (hits) {
      var h = sv("path", { d: d, stroke: "transparent", "stroke-width": 10,
                           "pointer-events": "stroke", "data-role": "linkhit",
                           "data-link": l.id });
      h.addEventListener("click", function (ev) {
        ev.stopPropagation();
        if (JUST_DRAGGED) return;                 // the end of a draw across it
        openLinkMenu(l, ev);
      });
      hits.appendChild(h);
    }
  });
}

function wdAdd(n, k) {               // day number + k working days (Mon–Fri)
  var step = k >= 0 ? 1 : -1, left = Math.abs(k || 0);
  while (left) { n += step; if (dow(n) !== 0 && dow(n) !== 6) left--; }
  return n;
}
function linkConflict(l) {
  var p = G.byId[l.pred], q = G.byId[l.succ];
  if (!p || !q) return !!l.conflict;
  p = p.r; q = q.r;
  if (l.met || isDone(p) || isDone(q)) return false;
  var f = p.end || p.due, b = q.start || q.end || q.due;
  if (!f || !b) return false;
  return wdAdd(dn(f), l.lag || 0) > dn(b);
}
// A row's ⚠: an in-view link in conflict now, or (off-chart) what the server said.
function refreshConflicts() {
  (DATA.rows || []).forEach(function (r) {
    var inView = (DATA.links || []).some(function (l) {
      return l.succ === r.id && linkConflict(l);
    });
    r.conflict = inView || (r.conflictSrv && r.ext_after.length > 0);
  });
}

// The project's span (derived: earliest begin → latest finish of its rows) as a thin
// bracket in the lower half of the group row; its target date as a hollow accent ◇ on
// the same line, labelled in the upper half so the words never sit on the bar.
// The span is worked out HERE from the rows on screen — staged drags included — with the
// server's rule (begin = start, else end, else due; finish = end, else due, else the day
// it was finished), so the bracket moves with the bars instead of waiting for Save.
function groupSpan(g) {
  var lo = null, hi = null;
  (DATA.rows || []).forEach(function (r) {
    if (r.g !== g.id) return;
    var b = r.start || r.end || r.due, f = [r.end || r.due, r.done_at].filter(Boolean);
    if (b && (!lo || b < lo)) lo = b;
    f.forEach(function (x) { if (!hi || x > hi) hi = x; });
  });
  return { start: lo, end: hi && lo && hi < lo ? lo : hi };
}

function drawGroup(into, ln) {
  var g = Object.assign({}, ln.g, groupSpan(ln.g)), by = ln.y + ln.h - 11;
  if (g.start && g.end) {
    var x1 = X(g.start), x2 = X(g.end) + G.ppd, w = Math.max(x2 - x1, 4);
    into.appendChild(sv("rect", { x: x1, y: by - 2, width: w, height: 4, rx: 2,
                                  fill: "var(--g-sum)" }));
    if (w >= 16) {
      [x1, x2].forEach(function (x, i) {
        into.appendChild(sv("path", { d: "M" + x + " " + (by - 2) + "v8l" + (i ? -5 : 5)
          + " -8Z", fill: "var(--g-sum)" }));
      });
    }
    if (ln.h >= 28) into.appendChild(sv("text", { x: x1, y: ln.y + 13, "font-size": 10.5,
      fill: "var(--ink-3)", "font-family": "var(--m)" },
      g.start === g.end ? shortDate(g.start)
        : shortDate(g.start) + " → " + shortDate(g.end)));
  }
  if (g.target) {
    var tx = X(g.target) + G.ppd / 2;
    into.appendChild(diamond(tx, by, 6, { fill: "var(--surface)", stroke: "var(--g-today)",
                                          "stroke-width": 2 }));
    if (ln.h >= 28) into.appendChild(sv("text", { x: tx + 9, y: ln.y + 13, "font-size": 10.5,
      "font-weight": 600, fill: "var(--g-today)" }, "target " + shortDate(g.target)));
  }
}

function drawRow(into, ln) {
  var r = ln.r, cy = ln.y + ln.h / 2, col = colourFor(r), done = isDone(r);
  // Marks scale with the row (screen rows are 28 px → a 14 px bar; a slide may go to 48).
  var k = Math.max(0.75, Math.min(1.6, G.rowh / ROWH)), bh = Math.round(14 * k);
  var edited = !G.export && !!PEND[r.id];
  var g = sv("g", { class: "hit", "data-id": r.id });
  // The hit area is the whole row, so a thin month-zoom bar is still easy to point at.
  // R4.5: on screen it is also where you DRAW a plan (drag across an empty stretch).
  g.appendChild(sv("rect", { x: 0, y: ln.y, width: G.width, height: ln.h, fill: "none",
                             "pointer-events": "all", "data-role": "row" }));
  var right = null;
  var bar = barOf(r);
  if (bar) {
    var x1 = X(bar.b) + 1, x2 = X(bar.e) + G.ppd - 1;
    g.appendChild(sv("rect", { x: x1, y: cy - bh / 2, width: Math.max(x2 - x1, 4), height: bh,
      rx: 4, fill: col, "data-role": "bar",
      stroke: edited ? "var(--accent)" : r.status === "wont_do" ? "var(--ink-3)" : null,
      "stroke-width": edited ? 2 : null,
      "stroke-dasharray": r.status === "wont_do" ? "3 2" : null }));
    if (!G.export) {                  // grab the ends to change start / end
      var hw = Math.min(8, Math.max(4, (x2 - x1) / 3));
      g.appendChild(sv("rect", { x: x1 - 3, y: cy - bh / 2 - 2, width: hw + 3,
        height: bh + 4, fill: "none", "pointer-events": "all", "data-role": "edge-l" }));
      g.appendChild(sv("rect", { x: x2 - hw, y: cy - bh / 2 - 2, width: hw + 3,
        height: bh + 4, fill: "none", "pointer-events": "all", "data-role": "edge-r" }));
    }
    right = x2;
  }
  var md = markDay(r);
  if (md) {
    var cx = X(md) + G.ppd / 2;
    var late = !done && (r.overdue || (bar && r.due && bar.e > r.due));
    var ring = edited ? "var(--accent)" : null;
    if (r.type === "milestone") {
      g.appendChild(diamond(cx, cy, 8 * k, { fill: col, stroke: ring || (late
        ? "var(--g-crit)" : "var(--surface)"), "stroke-width": 2, "data-role": "ms" }));
    } else if (bar) {               // the commitment, on top of the plan
      g.appendChild(diamond(cx, cy, 6 * k, { fill: "var(--surface)",
        stroke: late ? "var(--g-crit)" : "var(--ink-2)", "stroke-width": 2,
        "data-role": "due" }));
    } else {                        // due date only, no plan yet
      g.appendChild(diamond(cx, cy, 6.5 * k, { fill: col,
        stroke: ring || (late ? "var(--g-crit)" : "var(--surface)"), "stroke-width": 2,
        "data-role": "due" }));
      if (late) {
        g.appendChild(diamond(cx, cy, 10 * k, { fill: "none", stroke: "var(--g-crit)",
                                            "stroke-width": 1.5 }));
      }
    }
    right = Math.max(right || 0, cx + 10 * k);
  }
  // R4.5b: the link dot just past the finish — drag it onto the item that comes after.
  // Shown on hover only (CSS), but always there to grab.
  var fx = !G.export && G.connG ? finishX(r) : null;
  if (G.connOf && G.connOf[r.id]) { G.connOf[r.id].remove(); delete G.connOf[r.id]; }
  if (fx != null) {
    var w = sv("g", { class: "gconnw", "data-id": r.id });
    w.appendChild(sv("circle", { cx: fx + 9, cy: cy, r: 5, class: "gconn",
      "data-role": "link", fill: "var(--accent)", stroke: "var(--surface)",
      "stroke-width": 1.5 }));
    w.addEventListener("mouseenter", function () { w.classList.add("on"); });
    w.addEventListener("mouseleave", function () { w.classList.remove("on"); });
    G.connG.appendChild(w);
    G.connOf[r.id] = w;
  }
  // A quiet date beside the last mark — the chart should read without hovering.
  var tag = md ? shortDate(md) : (bar ? shortDate(bar.e) : "");
  if (tag && right != null) {
    g.appendChild(sv("text", { x: right + (fx != null ? 17 : 5), y: cy + 4,
      "font-size": 10.5, fill: "var(--ink-3)", "font-family": "var(--m)",
      "pointer-events": "none" }, tag));
  }
  if (!bar && !md) {
    g.appendChild(sv("text", { x: 8, y: cy + 4, "font-size": 10.5, fill: "var(--ink-3)",
      "font-style": "italic", "pointer-events": "none" },
      G.export ? "no dates" : "no dates — drag across this row to plan it"));
  }
  hook(g, r);
  into.appendChild(g);
  return g;
}

// ------------------------------------------------------------------ hover + open

function linked(r) {
  var ids = [r.id];
  (DATA.links || []).forEach(function (l) {
    if (l.pred === r.id) ids.push(l.succ);
    if (l.succ === r.id) ids.push(l.pred);
  });
  return ids;
}

function hook(node, r) {
  node.addEventListener("mouseenter", function () { highlight(r, true); });
  node.addEventListener("mouseleave", function () { highlight(r, false); hideTip(); });
  node.addEventListener("mousemove", function (e) { if (!DRAG) showTip(r, e); });
  node.addEventListener("click", function () {
    if (JUST_DRAGGED) return;         // the end of a drag is not a click
    openInList(r.id);
  });
}

function highlight(r, on) {
  var ids = on ? linked(r) : [];
  document.querySelectorAll(".gl.hi").forEach(function (n) { n.classList.remove("hi"); });
  ids.forEach(function (id) {
    var n = document.querySelector('.gl[data-id="' + id + '"]');
    if (n) n.classList.add("hi");
  });
  if (G && G.connOf && G.connOf[r.id]) G.connOf[r.id].classList.toggle("on", on);
  var b = G && G.byId[r.id];
  if (G && G.hi && b) {
    G.hi.setAttribute("y", b.cy - G.rowh / 2);
    G.hi.setAttribute("opacity", on ? 1 : 0);
  }
}

function showTip(r, e) {
  var t = $("#gtip");
  t.innerHTML = "";
  var h = el("div");
  h.appendChild(el("b", null, "#" + r.id + " "));
  h.appendChild(document.createTextNode(r.t));
  t.appendChild(h);
  var who = r.owner ? r.owner + " (" + WHO_LABEL[r.who] + ")" : "no owner";
  if ((r.with || []).length) who += " · with " + r.with.join(", ");
  if (r.desc && r.desc !== r.t) {
    t.appendChild(el("div", "d", r.desc + (r.desc.length >= 360 ? "…" : "")));
  }
  t.appendChild(el("div", "m", r.status + (r.blocked ? " · waits on #" + r.blocked_by.join(" #")
    : r.ready ? " · ready (predecessors finished)" : "")
    + " · " + r.type + " · " + who + (r.cust ? " · " + r.cust : "")));
  var dates = [];
  if (r.start) dates.push("start " + shortDate(r.start));
  if (r.end) dates.push("end " + shortDate(r.end));
  if (r.due) dates.push("due " + shortDate(r.due));
  if (r.done_at) dates.push("finished " + shortDate(r.done_at));
  t.appendChild(el("div", "m", dates.length ? dates.join(" · ") : "no dates"));
  if (r.step) t.appendChild(el("div", "m", "step " + r.step));
  if (r.overdue) t.appendChild(el("div", "w", "Overdue"));
  if (r.conflict) t.appendChild(el("div", "w", "⚠ starts before a predecessor finishes"));
  if (r.ext_after.length) {
    t.appendChild(el("div", "m", "also after #" + r.ext_after.join(" #") + " (not on this chart)"));
  }
  if (r.ext_unblocks.length) {
    t.appendChild(el("div", "m", "also unblocks #" + r.ext_unblocks.join(" #")
      + " (not on this chart)"));
  }
  t.appendChild(el("div", "m", "click to open in the list · drag to plan"));
  t.hidden = false;
  var x = e.clientX + 14, y = e.clientY + 14;
  var w = t.offsetWidth, hh = t.offsetHeight;
  if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
  if (y + hh > window.innerHeight - 8) y = e.clientY - hh - 14;
  t.style.left = x + "px";
  t.style.top = y + "px";
}
function hideTip() { $("#gtip").hidden = true; }

// The list window owns editing. Ask it to open the item; if no list window answers
// within a moment, open one.
var pendingOpen = null;
if (CH) {
  CH.addEventListener("message", function (ev) {
    var m = ev.data || {};
    if (m.type === "opened" && pendingOpen && m.id === pendingOpen.id) {
      clearTimeout(pendingOpen.timer);
      pendingOpen = null;
    }
  });
}
function openInList(id) {
  if (pendingOpen) clearTimeout(pendingOpen.timer);
  var fallback = function () {
    pendingOpen = null;
    window.open("/tracking?open=" + id, "mt-tracking");
  };
  if (!CH) return fallback();
  pendingOpen = { id: id, timer: setTimeout(fallback, 600) };
  CH.postMessage({ type: "open", id: id });
  try { if (window.opener && !window.opener.closed) window.opener.focus(); } catch (e) {}
}

// ------------------------------------------------------------------ R4.5: drag to plan

// Gestures (all snap to whole days; nothing is written until Save):
//   drag across an empty stretch of a row  → draw a plan (start → end)
//   drag a bar                             → move the plan
//   drag a bar's end                       → change start or end
//   drag a ◆                               → move the due date (a milestone: its day)
// A press that moves less than 4 px is a click, and still opens the item in the list.
//
// Staged edits live in PEND (id → {orig, cur, rev, stale?}) and in localStorage, so a
// reload or a crash does not lose them. HIST is the undo stack, one entry per gesture.
var DRAG = null, JUST_DRAGGED = false, FIRST_TAKE = true;
// PEND: id → {orig, cur, rev, t, stale?}   staged date edits
// LINKOPS: [{op:"add", tmp, pred, succ, met} | {op:"remove", id, pred, succ, met}
//           | {op:"met", id, pred, succ, met}]   staged link edits (R4.5b)
// HIST: the undo stack, one entry per gesture:
//   {id, before}  a date edit · {kind:"link", op, dropped:[]}  · {kind:"group", entries}
var PEND = {}, HIST = [], LINKOPS = [], TMP = -1, TRAYNOTE = "";

function scopeKey() { return "pend:" + (S.project ? "p" + S.project : "c" + S.customer); }
function pendingCount() { return Object.keys(PEND).length + LINKOPS.length; }
function rowById(id) {
  for (var i = 0; DATA && i < DATA.rows.length; i++) if (DATA.rows[i].id === id) return DATA.rows[i];
  return null;
}
function datesOf(r) { return { start: r.start || null, end: r.end || null, due: r.due || null }; }
function same(a, b) { return a.start === b.start && a.end === b.end && a.due === b.due; }
function setDates(r, d) { r.start = d.start; r.end = d.end; r.due = d.due; }

function persist() {
  store(scopeKey(), JSON.stringify({ pend: PEND, hist: HIST, links: LINKOPS, tmp: TMP }));
  leaseSoon();
}
function restorePending() {
  try {
    var v = JSON.parse(store(scopeKey()) || "null");
    if (v && v.pend) {
      PEND = v.pend; HIST = v.hist || []; LINKOPS = v.links || []; TMP = v.tmp || -1;
    }
  } catch (e) { PEND = {}; HIST = []; LINKOPS = []; }
}
// After any fetch: put the staged dates back on the fresh rows, and rebuild the links on
// screen = the server's links with the staged link edits applied. A row whose rev moved,
// or a staged edit to a link that has gone, is marked stale; Save waits until it is
// resolved.
function reapplyPending() {
  Object.keys(PEND).forEach(function (k) {
    var p = PEND[k], r = rowById(+k);
    if (!r) return;
    if (r.rev !== p.rev) { p.stale = true; p.theirs = datesOf(r); p.theirRev = r.rev; }
    setDates(r, p.cur);
  });
  applyLinkOps();
}
function srvLink(id) {
  return (DATA.linksSrv || []).filter(function (l) { return l.id === id; })[0] || null;
}
function applyLinkOps() {
  var out = (DATA.linksSrv || []).map(function (l) { return Object.assign({}, l); });
  LINKOPS.forEach(function (o) {
    if (o.op === "add") {
      var p = rowById(o.pred);
      out.push({ id: o.tmp, pred: o.pred, succ: o.succ, lag: 0, met: !!o.met,
                 done: !!(p && isDone(p)), staged: true });
    } else if (o.op === "remove") {
      out = out.filter(function (l) { return l.id !== o.id; });
    } else if (o.op === "met") {
      out.forEach(function (l) { if (l.id === o.id) { l.met = o.met; l.staged = true; } });
    }
  });
  DATA.links = out;
}
function opStale(o) { return o.op !== "add" && !srvLink(o.id); }

function dayAt(clientX) {
  var rect = G.svg.getBoundingClientRect();
  return G.from + Math.floor((clientX - rect.left) / G.ppd);
}
function svgPoint(e) {
  var rect = G.svg.getBoundingClientRect();
  return { x: e.clientX - rect.left, y: e.clientY - rect.top };
}
function rowAt(clientX, clientY) {
  var t = document.elementFromPoint(clientX, clientY);
  var g = t && t.closest && t.closest("g[data-id]");
  return g ? +g.getAttribute("data-id") : null;
}

function onDown(e) {
  if (e.button !== 0) return;
  // Ask what is under the pointer NOW rather than trusting e.target: right after a
  // redraw (every drop re-renders) Chrome can hand the press to a stale hit-test
  // target — the row background instead of the bar just drawn on it.
  var t = document.elementFromPoint(e.clientX, e.clientY) || e.target;
  var role = t.getAttribute && t.getAttribute("data-role");
  var gEl = t.closest && t.closest("g[data-id]");
  var id = gEl ? +gEl.getAttribute("data-id") : null;
  if (role === "linkhit") {
    // An arrow runs along its successor's row. A CLICK on it opens its menu (the path's
    // click handler); a DRAG draws on whatever row is underneath, as if it were not there.
    var y = svgPoint(e).y;
    var ln = G.lines.filter(function (l) { return l.kind === "row" && y >= l.y && y < l.y + l.h; })[0];
    if (!ln) return;
    id = ln.r.id;
    role = "row";
  }
  if (id == null) return;
  var r = G.byId[id] && G.byId[id].r;
  if (!r) return;
  closeLinkMenu();
  DRAG = { id: id, r: r, role: role || "row", x0: e.clientX,
           y0: e.clientY, d0: dayAt(e.clientX), before: datesOf(r), bar: barOf(r),
           moved: false, pid: e.pointerId };
  // No pointer capture yet: a plain click must still land on the row (and open it).
}

function onMove(e) {
  if (!DRAG) return;
  var dx = e.clientX - DRAG.x0, dy = e.clientY - DRAG.y0;
  if (!DRAG.moved && Math.abs(dx) < 4 && Math.abs(dy) < 4) return;
  if (!DRAG.moved) {
    DRAG.moved = true;
    hideTip();
    document.body.classList.add("gdragging");
    try { G.svg.setPointerCapture(DRAG.pid); } catch (x) {}
  }
  if (DRAG.role === "link") return linkBand(e);
  var dd = Math.round(dx / G.ppd), r = DRAG.r, b = DRAG.bar, B = DRAG.before;
  var role = DRAG.role;
  if ((role === "bar" || role === "edge-l" || role === "edge-r") && !b) role = "row";
  if (role === "bar") {
    r.start = iso(dn(b.b) + dd);
    r.end = iso(dn(b.e) + dd);
  } else if (role === "edge-l") {
    r.start = iso(Math.min(dn(b.b) + dd, dn(b.e)));
    r.end = b.e;
  } else if (role === "edge-r") {
    r.end = iso(Math.max(dn(b.e) + dd, dn(b.b)));
    r.start = b.b;
  } else if (role === "due") {
    r.due = iso(dn(B.due) + dd);
  } else if (role === "ms") {
    if (B.end) { r.start = r.end = iso(dn(B.end) + dd); }
    else if (B.due) { r.due = iso(dn(B.due) + dd); }
    else if (B.start) { r.start = r.end = iso(dn(B.start) + dd); }
  } else {                                     // draw a plan
    var d1 = dayAt(e.clientX);
    if (r.type === "milestone") { r.start = r.end = iso(d1); }
    else { r.start = iso(Math.min(DRAG.d0, d1)); r.end = iso(Math.max(DRAG.d0, d1)); }
  }
  redrawRow(DRAG.id);
  drawLinks();
  dragTag(e, r, role);
}

// Dragging from a row's dot: a rubber band from its finish to the pointer, and the row
// under the pointer lit up as the would-be successor.
function linkBand(e) {
  var p = G.byId[DRAG.id], sx = finishX(p.r), pt = svgPoint(e);
  var band = G.bandG;
  while (band.firstChild) band.removeChild(band.firstChild);
  band.appendChild(sv("path", { d: "M" + (sx + 9) + " " + p.cy + "L" + pt.x + " " + pt.y,
    stroke: "var(--accent)", "stroke-width": 2, "stroke-dasharray": "5 3", fill: "none",
    "marker-end": "url(#gm-l)", "pointer-events": "none" }));
  var tgt = rowAt(e.clientX, e.clientY), t = $("#gdragtag");
  if (tgt && tgt !== DRAG.id && G.byId[tgt]) {
    G.hi.setAttribute("y", G.byId[tgt].cy - G.rowh / 2);
    G.hi.setAttribute("opacity", 1);
    t.textContent = "#" + tgt + " comes after #" + DRAG.id;
  } else {
    G.hi.setAttribute("opacity", 0);
    t.textContent = "drop on the item that comes after #" + DRAG.id;
  }
  t.hidden = false;
  t.style.left = (e.clientX + 14) + "px";
  t.style.top = (e.clientY - 30) + "px";
}

function onUp(e) {
  if (!DRAG) return;
  var d = DRAG;
  DRAG = null;
  document.body.classList.remove("gdragging");
  $("#gdragtag").hidden = true;
  if (!d.moved) return;                         // a click — hook()'s handler opens it
  JUST_DRAGGED = true;
  setTimeout(function () { JUST_DRAGGED = false; }, 0);
  if (d.role === "link") {
    var tgt = rowAt(e.clientX, e.clientY);
    if (tgt && tgt !== d.id) addLink(d.id, tgt);
    render(null, true);
    return;
  }
  if (same(datesOf(d.r), d.before)) return;
  stageDates(d.id, d.before);
  HIST.push({ id: d.id, before: d.before });
  persist();
  render(null, true);
}

function cancelDrag() {
  if (!DRAG) return;
  setDates(DRAG.r, DRAG.before);
  var id = DRAG.id;
  DRAG = null;
  document.body.classList.remove("gdragging");
  $("#gdragtag").hidden = true;
  if (G.bandG) while (G.bandG.firstChild) G.bandG.removeChild(G.bandG.firstChild);
  redrawRow(id);
  drawLinks();
}

function redrawRow(id) {
  var old = G.rowG[id], ln = G.lineOf[id];
  if (!old || !ln) return;
  var tmp = sv("g");
  var g = drawRow(tmp, ln);
  old.parentNode.replaceChild(g, old);
  G.rowG[id] = g;
}

// The live read-out beside the pointer: what the dates will be, and the length.
function dragTag(e, r, role) {
  var t = $("#gdragtag");
  var txt = role === "due" ? "due " + shortDate(r.due)
    : r.type === "milestone" ? shortDate(r.end || r.due || r.start)
    : shortDate(r.start) + " → " + shortDate(r.end) + "  ·  "
      + (wdCount(dn(r.start), dn(r.end))) + " working day"
      + (wdCount(dn(r.start), dn(r.end)) === 1 ? "" : "s");
  if (r.due && role !== "due" && r.end && r.end > r.due) txt += "  ·  ends after due";
  t.textContent = txt;
  t.hidden = false;
  t.style.left = (e.clientX + 14) + "px";
  t.style.top = (e.clientY - 30) + "px";
}
function wdCount(a, b) {                        // working days a..b inclusive
  var n = 0;
  for (var d = a; d <= b; d++) if (dow(d) !== 0 && dow(d) !== 6) n++;
  return n;
}
function wdBetween(a, b) {                      // = tracking.wd_between, on day numbers
  var sign = b >= a ? 1 : -1, lo = Math.min(a, b), hi = Math.max(a, b), n = 0;
  for (var d = lo + 1; d <= hi; d++) if (dow(d) !== 0 && dow(d) !== 6) n++;
  return sign * n;
}

// Book-keeping only — the caller pushes the undo entry and persists.
function stageDates(id, before) {
  var r = rowById(id);
  if (!PEND[id]) PEND[id] = { orig: before, cur: datesOf(r), rev: r.rev, t: r.t };
  PEND[id].cur = datesOf(r);
  if (same(PEND[id].orig, PEND[id].cur)) delete PEND[id];
}
function unstageDates(id, before) {
  var r = rowById(id);
  if (r) setDates(r, before);
  if (PEND[id]) {
    PEND[id].cur = before;
    if (same(PEND[id].orig, before)) delete PEND[id];
  }
}

// ---- links (R4.5b) — staged like dates; one Save carries both

function reaches(from, to) {                     // along the links on screen
  var stack = [from], seen = {};
  while (stack.length) {
    var n = stack.pop();
    if (n === to) return true;
    if (seen[n]) continue;
    seen[n] = 1;
    DATA.links.forEach(function (l) { if (l.pred === n) stack.push(l.succ); });
  }
  return false;
}

function addLink(pred, succ) {
  if (DATA.links.some(function (l) { return l.pred === pred && l.succ === succ; })) {
    return trayNote("#" + succ + " already comes after #" + pred + ".");
  }
  if (reaches(succ, pred)) {
    return trayNote("#" + succ + " already leads to #" + pred
                    + " — linking them this way would make a loop.");
  }
  // Re-adding a link whose removal is staged just cancels the removal.
  var rm = LINKOPS.filter(function (o) {
    return o.op === "remove" && o.pred === pred && o.succ === succ;
  })[0];
  if (rm) {
    LINKOPS.splice(LINKOPS.indexOf(rm), 1);
    HIST.push({ kind: "unlink-undo", op: rm });
  } else {
    var op = { op: "add", tmp: TMP--, pred: pred, succ: succ, met: false };
    LINKOPS.push(op);
    HIST.push({ kind: "link", op: op, dropped: [] });
  }
  applyLinkOps();
  persist();
}

function removeLink(l) {
  if (l.id < 0) {                                // a link drawn here and not saved yet
    var a = LINKOPS.filter(function (o) { return o.tmp === l.id; })[0];
    if (a) {
      LINKOPS.splice(LINKOPS.indexOf(a), 1);
      HIST.push({ kind: "unadd", op: a });
    }
  } else {
    var dropped = LINKOPS.filter(function (o) { return o.op === "met" && o.id === l.id; });
    dropped.forEach(function (o) { LINKOPS.splice(LINKOPS.indexOf(o), 1); });
    var s0 = srvLink(l.id) || l;
    var op = { op: "remove", id: l.id, pred: s0.pred, succ: s0.succ, met: !!s0.met };
    LINKOPS.push(op);
    HIST.push({ kind: "link", op: op, dropped: dropped });
  }
  applyLinkOps();
  persist();
  render(null, true);
}

function toggleMet(l) {
  if (l.id < 0) {
    var a = LINKOPS.filter(function (o) { return o.tmp === l.id; })[0];
    if (a) { a.met = !a.met; HIST.push({ kind: "addmet", op: a }); }
  } else {
    var m = LINKOPS.filter(function (o) { return o.op === "met" && o.id === l.id; })[0];
    if (m) {                                     // back to what the server has
      LINKOPS.splice(LINKOPS.indexOf(m), 1);
      HIST.push({ kind: "unmet", op: m });
    } else {
      var op = { op: "met", id: l.id, pred: l.pred, succ: l.succ, met: !l.met };
      LINKOPS.push(op);
      HIST.push({ kind: "link", op: op, dropped: [] });
    }
  }
  applyLinkOps();
  persist();
  render(null, true);
}

function undoEntry(h) {
  if (h.kind === "group") {
    h.entries.slice().reverse().forEach(undoEntry);
  } else if (h.kind === "link") {
    var i = LINKOPS.indexOf(h.op);
    if (i < 0) i = LINKOPS.findIndex(function (o) { return JSON.stringify(o) === JSON.stringify(h.op); });
    if (i >= 0) LINKOPS.splice(i, 1);
    (h.dropped || []).forEach(function (o) { LINKOPS.push(o); });
  } else if (h.kind === "unadd" || h.kind === "unlink-undo" || h.kind === "unmet") {
    LINKOPS.push(h.op);
  } else if (h.kind === "addmet") {
    var a = LINKOPS.filter(function (o) { return o.tmp === h.op.tmp; })[0];
    if (a) a.met = !a.met;
  } else {
    unstageDates(h.id, h.before);
  }
}

function undoLast() {
  var h = HIST.pop();
  if (!h) return;
  undoEntry(h);
  applyLinkOps();
  persist();
  render(null, true);
}
function undoItem(id) {
  var p = PEND[id], r = rowById(+id);
  if (!p) return;
  if (r) setDates(r, p.stale ? p.theirs : p.orig);
  delete PEND[id];
  HIST = HIST.filter(function (h) { return h.kind || String(h.id) !== String(id); });
  persist();
  render(null, true);
}
function undoLinkOp(o) {
  LINKOPS.splice(LINKOPS.indexOf(o), 1);
  HIST = HIST.filter(function (h) { return h.op !== o; });
  applyLinkOps();
  persist();
  render(null, true);
}
function keepMine(id) {                         // re-base my edit on their newer version
  var p = PEND[id];
  if (!p || !p.stale) return;
  p.orig = p.theirs;
  p.rev = p.theirRev;
  delete p.stale; delete p.theirs; delete p.theirRev;
  if (same(p.orig, p.cur)) delete PEND[id];
  persist();
  render(null, true);
}
function discardAll() {
  if (!window.confirm("Discard " + pendingCount() + " unsaved change"
                      + (pendingCount() === 1 ? "" : "s") + "?")) return;
  Object.keys(PEND).forEach(function (k) {
    var r = rowById(+k);
    if (r) setDates(r, PEND[k].stale ? PEND[k].theirs : PEND[k].orig);
  });
  PEND = {};
  HIST = [];
  LINKOPS = [];
  TRAYNOTE = "";
  applyLinkOps();
  persist();
  render(null, true);
}

// ---- Push dependents, on the chart

// The same walk as tracking.push_preview, but over what is ON SCREEN — staged dates and
// links included — and the result is staged in the tray like any drag (one undo).
// Due dates never move; an item with only a due date is reported as held.
function pushFrom(predId) {
  var links = DATA.links.filter(function (l) { return !l.met; });
  var succs = {}, into = {};
  links.forEach(function (l) {
    (succs[l.pred] = succs[l.pred] || []).push(l);
    (into[l.succ] = into[l.succ] || []).push(l);
  });
  var reach = {}, stack = [predId];
  while (stack.length) {
    (succs[stack.pop()] || []).forEach(function (l) {
      var s_ = rowById(l.succ);
      if (s_ && !isDone(s_) && !reach[l.succ]) { reach[l.succ] = 1; stack.push(l.succ); }
    });
  }
  var ids = Object.keys(reach).map(Number), indeg = {};
  ids.forEach(function (i) {
    indeg[i] = (into[i] || []).filter(function (l) { return reach[l.pred]; }).length;
  });
  var order = [], ready = ids.filter(function (i) { return !indeg[i]; }).sort(function (a, b) { return a - b; });
  while (ready.length) {
    var i = ready.shift();
    order.push(i);
    (succs[i] || []).forEach(function (l) {
      if (l.succ in indeg && --indeg[l.succ] === 0) ready.push(l.succ);
    });
  }
  var est = {}, entries = [], held = [];
  order.forEach(function (i) {
    var it = rowById(i), need = null;
    (into[i] || []).forEach(function (l) {
      var p = rowById(l.pred);
      if (!p || isDone(p)) return;
      var pf = est[p.id] || p.end || p.due;
      if (!pf) return;
      var n = iso(wdAdd(dn(pf), l.lag || 0));
      if (!need || n > need) need = n;
    });
    var begin = it.start || it.end || it.due;
    if (!need || !begin || need <= begin) { est[i] = it.end || it.due; return; }
    var before = datesOf(it);
    if (it.start) {
      var delta = wdBetween(dn(it.start), dn(need));
      it.start = need;
      if (it.end) it.end = iso(wdAdd(dn(it.end), delta));
      est[i] = it.end || need;
    } else if (it.end) {
      it.end = need;
      est[i] = need;
    } else {
      held.push("#" + i + " (due " + shortDate(it.due) + ") would need " + shortDate(need));
      est[i] = need;
      return;
    }
    stageDates(i, before);
    entries.push({ id: i, before: before });
  });
  if (entries.length) HIST.push({ kind: "group", entries: entries });
  var r0 = rowById(predId), off = r0 && r0.ext_unblocks.length;
  TRAYNOTE = (entries.length ? "Pushed " + entries.length + " item" + (entries.length === 1 ? "" : "s")
              + " after #" + predId + ". " : "Nothing needed to move. ")
    + (held.length ? "Held — due dates never move by themselves: " + held.join("; ") + ". " : "")
    + (off ? "Not on this chart, so not pushed: #" + r0.ext_unblocks.join(" #") + "." : "");
  persist();
  render(null, true);
}
function trayNote(msg) {
  TRAYNOTE = msg;
  renderTray();
}

// ---- the arrow menu

function openLinkMenu(l, e) {
  closeLinkMenu();
  var m = el("div", "glinkmenu");
  m.id = "glinkmenu";
  var p = rowById(l.pred), q = rowById(l.succ);
  var head = el("div", "glm-h");
  head.appendChild(el("b", null, "#" + l.pred + " → #" + l.succ));
  head.appendChild(el("div", "glm-t", (p ? p.t : "") + "  →  " + (q ? q.t : "")));
  var state = [];
  if (l.met) state.push("met anyway");
  if (linkConflict(l)) state.push("⚠ #" + l.succ + " starts before #" + l.pred + " finishes");
  if (l.staged || l.id < 0) state.push("unsaved");
  if (state.length) head.appendChild(el("div", "glm-s", state.join(" · ")));
  m.appendChild(head);
  function act(label, fn, cls) {
    var b = el("button", "tap" + (cls ? " " + cls : ""), label);
    b.addEventListener("click", function (ev) { ev.stopPropagation(); closeLinkMenu(); fn(); });
    m.appendChild(b);
  }
  if (linkConflict(l)) act("Push dependents of #" + l.pred, function () { pushFrom(l.pred); }, "primary");
  act(l.met ? "Undo met anyway" : "Mark met anyway", function () { toggleMet(l); });
  act("Remove link", function () { removeLink(l); }, "danger");
  document.body.appendChild(m);
  var x = Math.min(e.clientX + 8, window.innerWidth - m.offsetWidth - 8);
  var y = Math.min(e.clientY + 8, window.innerHeight - m.offsetHeight - 8);
  m.style.left = x + "px";
  m.style.top = y + "px";
}
function closeLinkMenu() {
  var m = $("#glinkmenu");
  if (m) m.remove();
}
document.addEventListener("mousedown", function (e) {
  var m = $("#glinkmenu");
  if (m && !m.contains(e.target)) closeLinkMenu();
});

// ---- save

function saveAll() {
  var err = $("#gtray .err");
  var changes = Object.keys(PEND).map(function (k) {
    var p = PEND[k], c = { id: +k, rev: p.rev };
    if (p.orig.start !== p.cur.start) c.start_date = p.cur.start || "";
    if (p.orig.end !== p.cur.end) c.end_date = p.cur.end || "";
    if (p.orig.due !== p.cur.due) c.due_date = p.cur.due || "";
    return c;
  });
  var links = LINKOPS.map(function (o) {
    return o.op === "add" ? { op: "add", pred: o.pred, succ: o.succ, met: !!o.met }
      : o.op === "remove" ? { op: "remove", id: o.id }
      : { op: "met", id: o.id, met: !!o.met };
  });
  if (!changes.length && !links.length) return;
  var btn = $("#gtray .save");
  btn.disabled = true;
  post("/api/tracking/plan", { changes: changes, links: links }).then(function (r) {
    btn.disabled = false;
    if (!r.ok) { err.textContent = r.why || "save failed"; err.hidden = false; return; }
    PEND = {};
    HIST = [];
    LINKOPS = [];
    TRAYNOTE = "";
    persist();
    if (LIVE) LIVE.notify();
    savedToast(r.saved, { changes: r.undo, links: r.undo_links || [] });
    reload(true);
  }).catch(function (e) {
    btn.disabled = false;
    err.textContent = String(e);
    err.hidden = false;
  });
}

function savedToast(n, undo) {
  var t = el("div", "gtoast");
  t.appendChild(document.createTextNode("Saved " + n + " change" + (n === 1 ? "" : "s") + ". "));
  var u = el("button", "tap", "Undo");
  u.addEventListener("click", function () {
    u.disabled = true;
    post("/api/tracking/plan", { undo: undo }).then(function (r) {
      t.remove();
      if (!r.ok) return window.alert(r.why || "undo failed");
      if (LIVE) LIVE.notify();
      reload(true);
    });
  });
  t.appendChild(u);
  document.body.appendChild(t);
  setTimeout(function () { t.remove(); }, 9000);
}

// ---- soft leases (R4.5b)

// While anything is staged, this window holds a lease on the items involved, so the assistant
// (`tracking.py --leases`) sees "unsaved Gantt edits" and warns instead of writing over
// them. Renewed every 4 minutes (expiry is 10), released on Save / Discard / close.
var HOLDER = (function () {
  try {
    var h = sessionStorage.getItem("mt.gantt.holder");
    if (!h) {
      h = "gantt-" + Math.random().toString(36).slice(2, 10);
      sessionStorage.setItem("mt.gantt.holder", h);
    }
    return h;
  } catch (e) { return "gantt-" + Math.random().toString(36).slice(2, 10); }
})();
var LEASED = [], leaseTimer = null;
function leaseIds() {
  var ids = {};
  Object.keys(PEND).forEach(function (k) { ids[k] = 1; });
  LINKOPS.forEach(function (o) { if (o.pred) ids[o.pred] = 1; if (o.succ) ids[o.succ] = 1; });
  return Object.keys(ids).map(Number).sort(function (a, b) { return a - b; });
}
function leaseNow() {
  var ids = leaseIds();
  if (!ids.length && !LEASED.length) return;
  LEASED = ids;
  post("/api/tracking/lease", { holder: HOLDER, ids: ids, label: "Gantt" }).catch(function () {});
}
function leaseSoon() {
  clearTimeout(leaseTimer);
  leaseTimer = setTimeout(leaseNow, 800);
}
setInterval(function () { if (leaseIds().length) leaseNow(); }, 4 * 60 * 1000);
window.addEventListener("pagehide", function () {
  if (!LEASED.length || !navigator.sendBeacon) return;
  navigator.sendBeacon("/api/tracking/lease", JSON.stringify({ holder: HOLDER, ids: [] }));
});

// ---- the tray

function renderTray() {
  var box = $("#gtray");
  box.innerHTML = "";
  var ids = Object.keys(PEND);
  var n = pendingCount();
  box.hidden = !n && !TRAYNOTE;
  if (box.hidden) return;
  var head = el("div", "gth");
  head.appendChild(el("b", null, n ? n + " unsaved change" + (n === 1 ? "" : "s") : "Gantt"));
  box.appendChild(head);
  if (TRAYNOTE) {
    var note = el("div", "gtn", TRAYNOTE);
    var x = el("button", "tap", "×");
    x.addEventListener("click", function () { TRAYNOTE = ""; renderTray(); });
    note.appendChild(x);
    box.appendChild(note);
  }
  var list = el("div", "gtl");
  ids.forEach(function (k) {
    var p = PEND[k], r = rowById(+k);
    var line = el("div", "gti" + (p.stale ? " stale" : ""));
    var top = el("div", "gtit");
    top.appendChild(el("span", "gid", "#" + k));
    top.appendChild(el("span", "gtt", r ? r.t : (p.t || "")));
    var u = el("button", "tap", "↶");
    u.title = "Undo this item's changes";
    u.addEventListener("click", function () { undoItem(k); });
    top.appendChild(u);
    line.appendChild(top);
    var bits = [];
    [["start", "start"], ["end", "end"], ["due", "due"]].forEach(function (f) {
      if (p.orig[f[0]] !== p.cur[f[0]]) {
        bits.push(f[1] + " " + (p.orig[f[0]] ? shortDate(p.orig[f[0]]) : "—") + " → "
                  + (p.cur[f[0]] ? shortDate(p.cur[f[0]]) : "—"));
      }
    });
    line.appendChild(el("div", "gtd", bits.join("  ·  ")));
    if (p.stale) {
      var w = el("div", "gtw", "Changed elsewhere since you moved it. ");
      var km = el("button", "tap", "Keep mine");
      km.addEventListener("click", function () { keepMine(k); });
      var tt = el("button", "tap", "Take theirs");
      tt.addEventListener("click", function () { undoItem(k); });
      w.appendChild(km);
      w.appendChild(tt);
      line.appendChild(w);
    }
    list.appendChild(line);
  });
  LINKOPS.forEach(function (o) {
    var stale = opStale(o);
    var line = el("div", "gti link" + (stale ? " stale" : ""));
    var top = el("div", "gtit");
    top.appendChild(el("span", "gid", "#" + o.pred + " → #" + o.succ));
    top.appendChild(el("span", "gtt", o.op === "add" ? "link added" + (o.met ? " (met anyway)" : "")
      : o.op === "remove" ? "link removed"
      : o.met ? "marked met anyway" : "met anyway undone"));
    var u = el("button", "tap", "↶");
    u.title = "Undo this link change";
    u.addEventListener("click", function () { undoLinkOp(o); });
    top.appendChild(u);
    line.appendChild(top);
    if (stale) line.appendChild(el("div", "gtw", "That link is gone on the server — undo this one."));
    list.appendChild(line);
  });
  box.appendChild(list);
  var err = el("div", "err");
  err.hidden = true;
  box.appendChild(err);
  if (!n) return;
  var foot = el("div", "gtf");
  var un = el("button", "tap", "Undo");
  un.title = "Undo the last change (⌘Z)";
  un.disabled = !HIST.length;
  un.addEventListener("click", undoLast);
  var dis = el("button", "tap", "Discard");
  dis.addEventListener("click", discardAll);
  var sv_ = el("button", "primary save", "Save");
  sv_.disabled = ids.some(function (k) { return PEND[k].stale; }) || LINKOPS.some(opStale);
  if (sv_.disabled) sv_.title = "Resolve the items marked first";
  sv_.addEventListener("click", saveAll);
  foot.appendChild(un);
  foot.appendChild(dis);
  foot.appendChild(sv_);
  box.appendChild(foot);
}

window.addEventListener("beforeunload", function (e) {
  if (pendingCount()) { e.preventDefault(); e.returnValue = ""; }
});

// ------------------------------------------------------------------ legend

function legendKey(box, swatch, label) {
  var k = el("span", "k");
  k.appendChild(swatch);
  k.appendChild(document.createTextNode(label));
  box.appendChild(k);
}
function chip(fill) {
  var s = sv("svg", { width: 18, height: 10 });
  s.appendChild(sv("rect", { x: 0, y: 1, width: 18, height: 8, rx: 3, fill: fill }));
  return s;
}
function glyph(draw) {
  var s = sv("svg", { width: 22, height: 16 });
  draw(s);
  return s;
}

function renderLegend() {
  var box = $("#glegend");
  box.innerHTML = "";
  if (S.by === "status") {
    [["open", "Open"], ["waiting", "Waiting"], ["blocked", "Blocked"], ["done", "Done"]]
      .forEach(function (p) { legendKey(box, chip(cssVar(p[0])), p[1]); });
  } else if (S.by === "who") {
    ["me", "shared", "them", "none"].forEach(function (k) {
      legendKey(box, chip(cssVar(k)), WHO_LABEL[k]);
    });
  } else {
    var seen = {};
    (DATA.rows || []).forEach(function (r) { if (r.cust) seen[r.cust] = 1; });
    META.customers.slice().sort(function (a, b) { return a.id - b.id; })
      .forEach(function (c) {
        if (seen[c.short_name]) legendKey(box, chip(cssVar(custSlot(c.short_name))), c.name);
      });
    legendKey(box, chip(cssVar("done")), "Done");
  }
  box.appendChild(el("span", "sep"));
  legendKey(box, chip("var(--ink-3)"), "plan");
  legendKey(box, glyph(function (s) {
    s.appendChild(diamond(11, 8, 5.5, { fill: "var(--ink-3)" }));
  }), "due (no plan yet)");
  legendKey(box, glyph(function (s) {
    s.appendChild(diamond(11, 8, 5, { fill: "var(--surface)", stroke: "var(--ink-2)",
                                      "stroke-width": 2 }));
  }), "due, on a plan");
  legendKey(box, glyph(function (s) {
    s.appendChild(diamond(11, 8, 5, { fill: "var(--ink-3)", stroke: "var(--g-crit)",
                                      "stroke-width": 2 }));
  }), "overdue / plan past due");
  legendKey(box, glyph(function (s) {
    s.appendChild(sv("path", { d: "M1 8H17", stroke: "var(--g-link)", "stroke-width": 1.5 }));
    s.appendChild(sv("path", { d: "M14 4L21 8L14 12Z", fill: "var(--g-link)" }));
  }), "comes after");
  legendKey(box, glyph(function (s) {
    s.appendChild(sv("path", { d: "M1 8H17", stroke: "var(--g-link)", "stroke-width": 1.5,
                               "stroke-dasharray": "4 3" }));
  }), "met anyway");
  box.appendChild(el("span", "sep"));
  box.appendChild(el("span", "ghint", "Drag to plan: across an empty row to draw · a bar to "
    + "move · its ends to resize · a ◆ to move a due date · a row's dot onto another item "
    + "to link them · click an arrow for its options. Saved only when you press Save."));
}

// ------------------------------------------------------------------ export for a slide

// The current view (scope, colour mode, Hide done), redrawn LIGHT at 1920×1080 whatever
// the screen theme — a projector and a white slide want the light palette. The span is
// fitted to the dates actually on the chart, and rows shrink to fit (down to 15 px); past
// that, rows are left off and the slide says how many.
//
// PNG is the one for Google Slides (it does not accept SVG). SVG is for PowerPoint /
// Keynote, or for editing.
var SW = 1920, SH = 1080, SPAD = 56, SLW = 600, STOP = 172;   // SLW fitted per export

// Light values of every token, read from the stylesheet's own top-level :root and .gnt
// rules (the dark ones live inside @media and are skipped), so the export can never drift
// from app.css.
function lightTokens() {
  var t = {};
  Array.prototype.forEach.call(document.styleSheets, function (sh) {
    var rules;
    try { rules = sh.cssRules; } catch (e) { return; }
    Array.prototype.forEach.call(rules || [], function (r) {
      if (r.type !== 1 || (r.selectorText !== ":root" && r.selectorText !== ".gnt")) return;
      for (var i = 0; i < r.style.length; i++) {
        var k = r.style[i];
        if (k.indexOf("--") === 0) t[k.slice(2)] = r.style.getPropertyValue(k).trim();
      }
    });
  });
  return t;
}

function slideSpan(d) {
  var lo = dn(d.today), hi = lo;
  d.rows.forEach(function (r) {
    [r.start, r.end, r.due, r.done_at].forEach(function (s) {
      if (!s) return;
      lo = Math.min(lo, dn(s));
      hi = Math.max(hi, dn(s));
    });
  });
  d.groups.forEach(function (g) { if (g.target) hi = Math.max(hi, dn(g.target)); });
  return { from: lo - 4, to: hi + Math.max(10, Math.ceil((hi - lo) * 0.12)) };
}

function trunc(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }

function exportSlide(kind) {
  if (!DATA || !DATA.rows.length) return;
  var d = DATA, saved = G;
  var span = slideSpan(d);
  var ppd = (SW - SPAD * 2 - SLW) / (span.to - span.from + 1);
  var zoom = ppd >= 18 ? "day" : ppd >= 4.5 ? "week" : "month";
  var avail = SH - STOP - HEADH - 44;
  // The name column widens for full titles (R4.4c), within reason.
  var longest = d.rows.reduce(function (m, r) {
    return Math.max(m, (r.t || "").length + 8 + 16 * (r.depth || 0) / 8);
  }, 0);
  SLW = Math.round(Math.max(460, Math.min(780, longest * 8.6 + 170)));
  var groups = d.groups.filter(function (g) {
    return d.rows.some(function (r) { return r.g === g.id; });
  }).length;
  var rowh = Math.max(15, Math.min(48, Math.floor(avail / (d.rows.length + groups * 1.25))));
  var grph = Math.round(rowh * 1.25);
  G = layout(d, { zoom: zoom, ppd: ppd, from: span.from, to: span.to, rowh: rowh,
                  grph: grph, maxH: avail });
  G.export = true;                      // no drag handles, no "edited" outlines
  G.width = SW - SPAD * 2 - SLW;
  var lw = G;

  var root = sv("svg", { xmlns: NS, width: SW, height: SH, viewBox: "0 0 " + SW + " " + SH,
                         "font-family": "-apple-system, 'Segoe UI', system-ui, sans-serif" });
  root.appendChild(sv("rect", { x: 0, y: 0, width: SW, height: SH, fill: "var(--surface)" }));
  root.appendChild(sv("text", { x: SPAD, y: SPAD + 22, "font-size": 34, "font-weight": 700,
                                fill: "var(--ink)" }, d.title || "Gantt"));
  var shown = d.rows.length - G.omitted;
  root.appendChild(sv("text", { x: SPAD, y: SPAD + 56, "font-size": 17, fill: "var(--ink-2)" },
    shortDate(iso(span.from + 4)) + " – " + shortDate(iso(span.to)) + "  ·  " + shown
    + " item" + (shown === 1 ? "" : "s") + (S.hide ? " open" : "") + "  ·  as of "
    + shortDate(d.today) + (d.today.slice(0, 4) === String(new Date().getFullYear())
      ? " " + d.today.slice(0, 4) : "")));
  root.appendChild(slideLegend(SPAD, SPAD + 88));

  var axis = renderAxis();
  axis.setAttribute("x", SPAD + SLW);
  axis.setAttribute("y", STOP);
  root.appendChild(axis);
  var chart = renderChart();
  chart.setAttribute("x", SPAD + SLW);
  chart.setAttribute("y", STOP + HEADH);
  chart.setAttribute("height", lw.height);
  root.appendChild(chart);
  root.appendChild(slideLabels(SPAD, STOP + HEADH, SLW));
  root.appendChild(sv("line", { x1: SPAD, x2: SW - SPAD, y1: STOP + HEADH, y2: STOP + HEADH,
                                stroke: "var(--line-2)" }));
  root.appendChild(sv("line", { x1: SPAD + SLW, x2: SPAD + SLW, y1: STOP,
                                y2: STOP + HEADH + lw.height, stroke: "var(--line-2)" }));
  if (G.omitted) {
    root.appendChild(sv("text", { x: SPAD, y: SH - 22, "font-size": 15, fill: "var(--ink-3)" },
      "+ " + G.omitted + " more item" + (G.omitted === 1 ? "" : "s")
      + " not shown — export a single project to fit everything."));
  }
  G = saved;

  var tok = lightTokens();
  var str = new XMLSerializer().serializeToString(root)
    .replace(/var\(--m\)/g, "ui-monospace, Menlo, monospace")
    .replace(/var\(--([a-z0-9-]+)\)/gi, function (m, k) { return tok[k] || "#000"; });
  var name = "gantt-" + (d.title || "view").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") + "-" + d.today;
  if (kind === "svg") return download(new Blob([str], { type: "image/svg+xml" }), name + ".svg");
  var img = new Image();
  var url = URL.createObjectURL(new Blob([str], { type: "image/svg+xml" }));
  img.onload = function () {
    var c = document.createElement("canvas");
    c.width = SW;
    c.height = SH;
    c.getContext("2d").drawImage(img, 0, 0);
    URL.revokeObjectURL(url);
    c.toBlob(function (b) { download(b, name + ".png"); }, "image/png");
  };
  img.src = url;
}

function download(blob, name) {
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// The label column, as SVG text (the screen's is HTML). Status in words at the right
// edge, as on screen — colour never carries it alone.
function slideLabels(x0, y0, w) {
  var g = sv("g", { transform: "translate(" + x0 + "," + y0 + ")" });
  var fs = Math.max(11, Math.min(19, G.rowh * 0.5));
  G.lines.forEach(function (ln) {
    var cy = ln.y + ln.h / 2 + fs * 0.35;
    if (ln.kind === "grp") {
      g.appendChild(sv("rect", { x: 0, y: ln.y, width: w, height: ln.h,
                                 fill: "var(--g-group)" }));
      g.appendChild(sv("text", { x: 10, y: cy, "font-size": fs + 1, "font-weight": 700,
                                 fill: "var(--ink)" }, trunc(ln.g.name, 52)));
      return;
    }
    var r = ln.r, ind = 10 + 18 * r.depth;
    g.appendChild(sv("rect", { x: ind, y: ln.y + ln.h / 2 - 4, width: 8, height: 8, rx: 2,
                               fill: colourFor(r) }));
    // A derived block reads "waits on #N" in muted ink — not red, for the same reason
    // the bars are not painted red for it.
    var st = r.overdue ? "overdue" : r.status === "blocked" ? "blocked"
      : r.blocked && r.status === "open" ? "waits on #" + r.blocked_by.join(" #")
      : r.status.replace("_", " ");
    var room = Math.floor((w - ind - 24 - 130) / (fs * 0.56));
    g.appendChild(sv("text", { x: ind + 16, y: cy, "font-size": fs,
      fill: isDone(r) ? "var(--ink-3)" : "var(--ink)" },
      trunc("#" + r.id + "  " + r.t, room)));
    g.appendChild(sv("text", { x: w - 12, y: cy, "font-size": fs - 1, "text-anchor": "end",
      fill: r.overdue || r.status === "blocked" ? "var(--rec)" : "var(--ink-3)" }, st));
  });
  return g;
}

function slideLegend(x, y) {
  var g = sv("g", { transform: "translate(" + x + "," + y + ")" });
  var cx = 0;
  function key(draw, label) {
    draw(cx);
    var t = sv("text", { x: cx + 26, y: 13, "font-size": 15, fill: "var(--ink-2)" }, label);
    g.appendChild(t);
    cx += 26 + label.length * 8 + 26;
  }
  function swatch(fill) {
    return function (x0) {
      g.appendChild(sv("rect", { x: x0, y: 2, width: 20, height: 12, rx: 3, fill: fill }));
    };
  }
  var keys = S.by === "who" ? ["me", "shared", "them", "none"].map(function (k) {
    return [cssVar(k), WHO_LABEL[k]];
  }) : S.by === "cust" ? META.customers.filter(function (c) {
    return DATA.rows.some(function (r) { return r.cust === c.short_name; });
  }).map(function (c) { return [cssVar(custSlot(c.short_name)), c.name]; })
    : [["var(--g-open)", "Open"], ["var(--g-waiting)", "Waiting"],
       ["var(--g-blocked)", "Blocked"]];
  if (!S.hide) keys.push(["var(--g-done)", "Done"]);
  keys.forEach(function (k) { key(swatch(k[0]), k[1]); });
  key(function (x0) {
    g.appendChild(diamond(x0 + 10, 8, 6, { fill: "var(--ink-3)" }));
  }, "due");
  key(function (x0) {
    g.appendChild(diamond(x0 + 10, 8, 6, { fill: "var(--surface)", stroke: "var(--ink-2)",
                                           "stroke-width": 2 }));
  }, "due, on a plan");
  key(function (x0) {
    g.appendChild(diamond(x0 + 10, 8, 6, { fill: "var(--ink-3)", stroke: "var(--g-crit)",
                                           "stroke-width": 2 }));
  }, "overdue");
  key(function (x0) {
    g.appendChild(sv("path", { d: "M" + x0 + " 8H" + (x0 + 16), stroke: "var(--g-link)",
                               "stroke-width": 2 }));
    g.appendChild(sv("path", { d: "M" + (x0 + 14) + " 3L" + (x0 + 22) + " 8L" + (x0 + 14)
                               + " 13Z", fill: "var(--g-link)" }));
  }, "comes after");
  return g;
}

boot();
})();

// hygiene.js — the /hygiene page (Phase 3a: the Data area).
//
// Vanilla, no dependencies, no build step. Everything from the DB is rendered with
// textContent — descriptions are transcript-derived.
//
// The one idea worth knowing: an approve sends back the group's `sha`, the hash of
// exactly the rows this page drew. hygiene.py recomputes the group and refuses if the
// hash moved, so the user can only ever approve what they were shown. Excluded rows are sent
// by key; everything else in the group is applied, in one transaction, after a backup.
(function () {
"use strict";

var $ = function (s) { return document.querySelector(s); };
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function api(p, body) {
  var opts = body ? { method: "POST", headers: { "Content-Type": "application/json" },
                      body: JSON.stringify(body) } : undefined;
  return fetch(p, opts).then(function (r) {
    return r.text().then(function (t) {
      try { return JSON.parse(t); } catch (e) {
        return { ok: false, why: r.status === 404
          ? "The running mt web is older than this page. Restart mt web, then try again."
          : "Unexpected reply from mt web (HTTP " + r.status + ")." };
      }
    });
  }, function () { return { ok: false, why: "Cannot reach mt web — is it still running?" }; });
}

var GROUPS = [];
var EXCL = Object.create(null);     // group key -> {row key: 1}
var CHOICE = Object.create(null);   // group key -> chosen value (decide groups)
var OPEN = Object.create(null);     // group key -> rows expanded
var BUSY = false;

var KIND = {
  promote: ["Promote to project", "fix"],
  fix:    ["Fix", "fix"],
  decide: ["Needs your call", "decide"],
  link:   ["Link files", "link"],
  watch:  ["For awareness", "watch"],
  locked: ["Phase 7", "locked"],
  review: ["Review", "decide"]
};

// ------------------------------------------------------------------ tabs

function setArea(area) {
  document.querySelectorAll("#hytabs button").forEach(function (b) {
    b.classList.toggle("on", b.dataset.area === area);
  });
  $("#area-data").hidden = area !== "data";
  $("#area-memory").hidden = area !== "memory";
  try { localStorage.setItem("hy.area", area); } catch (e) {}
}

// ------------------------------------------------------------------ load

function load() {
  api("/api/hygiene/data").then(function (d) {
    var box = $("#groups");
    box.innerHTML = "";
    $("#hyfail").innerHTML = "";
    if (d.error || d.ok === false) {
      $("#hyfail").appendChild(el("div", "err", d.error || d.why));
      return;
    }
    GROUPS = d.groups || [];
    $("#nData").textContent = d.open ? String(d.open) : "";
    render(box);
  });
  loadLog();
}

function render(box) {
  var live = GROUPS.filter(function (g) { return !g.skipped; });
  var skipped = GROUPS.filter(function (g) { return g.skipped; });
  var open = live.filter(function (g) { return g.actionable; }).length;
  box.appendChild(el("div", "count", open
    ? open + (open === 1 ? " group needs" : " groups need") + " a decision"
      + (skipped.length ? " · " + skipped.length + " skipped" : "")
    : "Nothing needs a decision right now."
      + (skipped.length ? " (" + skipped.length + " skipped)" : "")));
  live.forEach(function (g) { box.appendChild(card(g)); });
  if (skipped.length) {
    var dd = el("details", "hyskipped");
    dd.appendChild(el("summary", null, "Skipped (" + skipped.length + ") — a card comes back "
      + "only if NEW rows appear, showing just those"));
    skipped.forEach(function (g) { dd.appendChild(card(g)); });
    box.appendChild(dd);
  }
}

// ------------------------------------------------------------------ a group

function included(g) {
  var ex = EXCL[g.key] || {};
  return g.rows.filter(function (r) { return !ex[r.key]; });
}

function card(g) {
  var k = KIND[g.kind] || [g.kind, "watch"];
  var c = el("div", "hyg k-" + k[1] + (g.kind === "promote" ? " optin" : "")
                    + (g.skipped ? " skipped" : ""));
  var top = el("div", "hygtop");
  top.appendChild(el("span", "tag hyk " + k[1], k[0]));
  top.appendChild(el("b", "hygtitle", g.title));
  top.appendChild(el("span", "hygn", g.count + (g.count === 1 ? " row" : " rows")));
  c.appendChild(top);
  c.appendChild(el("div", "hygwhy", g.why));

  if (g.kind === "locked") return c;
  // A review card (R4.4c titles) is worked on its own page; here it is a count and a door.
  if (g.kind === "review") {
    var go = el("a", "tap primary hyggo", "Review " + g.count + " →");
    go.href = g.href;
    c.appendChild(go);
    return c;
  }

  // Promotions are opt-in, one row at a time (R4.2: "per-row approvals, never bulk"):
  // every row starts unticked, so the button reads "Approve 0" until you choose.
  if (g.kind === "promote" && !EXCL[g.key]) {
    EXCL[g.key] = Object.create(null);
    g.rows.forEach(function (r) { EXCL[g.key][r.key] = 1; });
    OPEN[g.key] = true;
  }

  if (g.kind === "decide") {
    var pick = el("div", "hypick");
    pick.appendChild(el("span", null, "Becomes:"));
    var s = el("select");
    s.appendChild(new Option("— choose —", ""));
    // A choice is a plain value, or {value, label} when the value is an id (a project).
    (g.choices || []).forEach(function (v) {
      s.appendChild(typeof v === "object" ? new Option(v.label, v.value) : new Option(v, v));
    });
    s.value = CHOICE[g.key] || "";
    s.addEventListener("change", function () { CHOICE[g.key] = s.value; refresh(c, g); });
    pick.appendChild(s);
    c.appendChild(pick);
  }

  var list = el("div", "hyrows");
  c.appendChild(list);
  var foot = el("div", "hygfoot");
  c.appendChild(foot);
  c._list = list;
  c._foot = foot;
  refresh(c, g);
  return c;
}

function refresh(c, g) {
  var list = c._list, foot = c._foot;
  list.innerHTML = "";
  foot.innerHTML = "";
  var act = g.actionable;
  var rows = g.rows;
  var show = OPEN[g.key] ? rows : rows.slice(0, 3);
  show.forEach(function (r) { list.appendChild(rowFor(g, r, act, c)); });
  if (rows.length > 3) {
    var more = el("button", "tap", OPEN[g.key] ? "Show fewer"
      : "Show all " + rows.length + (act ? " (to exclude rows)" : ""));
    more.addEventListener("click", function () { OPEN[g.key] = !OPEN[g.key]; refresh(c, g); });
    foot.appendChild(more);
  }
  if (g.rows_truncated) {
    foot.appendChild(el("span", "note", "+" + g.rows_truncated + " more not shown"));
  }
  if (!act) return;
  var n = included(g).length;
  var ex = rows.length - n;
  foot.appendChild(el("span", "grow"));
  if (g.kind === "promote") {
    foot.appendChild(el("span", "note", n ? n + " of " + rows.length + " ticked"
      : "Tick the items that should become projects"));
  } else if (ex) foot.appendChild(el("span", "note", ex + " excluded"));
  if (g.skipped) {
    var un = el("button", null, "Unskip");
    un.addEventListener("click", function () {
      api("/api/hygiene/unskip", { key: g.key }).then(done);
    });
    foot.appendChild(un);
  } else {
    var sk = el("button", null, "Skip for now");
    sk.title = "Hide these rows. The card comes back only if new rows appear — editing a "
      + "skipped row does not bring it back";
    sk.addEventListener("click", function () {
      api("/api/hygiene/skip", { key: g.key, sha: g.sha }).then(done);
    });
    foot.appendChild(sk);
  }
  var go = el("button", "primary", "Approve " + n + (n === 1 ? " row…" : " rows…"));
  go.disabled = !n || (g.kind === "decide" && !CHOICE[g.key]);
  if (g.kind === "decide" && !CHOICE[g.key]) go.title = "Choose what the value becomes first";
  go.addEventListener("click", function () { approve(g); });
  foot.appendChild(go);
}

function rowFor(g, r, act, c) {
  var row = el("div", "hyrow");
  if (act) {
    var box = el("input");
    box.type = "checkbox";
    box.checked = !(EXCL[g.key] || {})[r.key];
    box.title = "Untick to leave this row out";
    box.addEventListener("change", function () {
      var ex = EXCL[g.key] || (EXCL[g.key] = Object.create(null));
      if (box.checked) delete ex[r.key]; else ex[r.key] = 1;
      refresh(c, g);
    });
    row.appendChild(box);
    if (!box.checked) row.classList.add("off");
    // The whole row is the target, not just the 15px box — except its own buttons.
    row.addEventListener("click", function (e) {
      if (e.target === box || e.target.closest("button")) return;
      box.checked = !box.checked;
      box.dispatchEvent(new Event("change"));
    });
  }
  var mid = el("div", "hymid");
  var head = el("div", "hylabel");
  head.appendChild(el("span", "hyid", "#" + r.id));
  head.appendChild(document.createTextNode(" " + (r.label || "")));
  mid.appendChild(head);
  (r.lines || []).forEach(function (t) { mid.appendChild(el("div", "hych", t)); });
  if (r.changes) {
    r.changes.forEach(function (ch) {
      var after = g.kind === "decide" ? choiceLabel(g, CHOICE[g.key]) || "?"
        : (ch.after_label != null ? ch.after_label : ch.after);
      if (ch.before_label != null) ch = Object.assign({}, ch, { before: ch.before_label });
      var line = el("div", "hych");
      line.appendChild(el("span", "f", (ch.field_label || ch.field) + ": "));
      line.appendChild(el("span", "b", ch.before == null ? "(empty)" : String(ch.before)));
      line.appendChild(el("span", "arr", " → "));
      line.appendChild(el("span", "a", after == null ? "(empty)" : String(after)));
      mid.appendChild(line);
    });
  }
  if (r.link) {
    var l = el("div", "hych");
    l.appendChild(el("span", "f", "link " + r.link.role + ": "));
    l.appendChild(el("span", "a", r.link.path));
    mid.appendChild(l);
  }
  if (r.note) mid.appendChild(el("div", "hych warn", r.note));
  row.appendChild(mid);
  if (r.link && !r.note) {
    var o = el("button", "tap", "Open");
    o.title = "Preview the file before linking it";
    o.addEventListener("click", function () { openFile("workspace", r.link.path); });
    row.appendChild(o);
  }
  // Oct 16: a suggestion the user has decided against goes away for good — not "for now".
  // It is logged, so Hygiene → log → "Suggest again" brings it back.
  if (g.kind === "promote") {
    var kp = el("button", "tap", "Keep as an item");
    kp.title = "Don't suggest making #" + r.id + " a project again (undo from the log)";
    kp.addEventListener("click", function (e) {
      e.stopPropagation();
      kp.disabled = true;
      api("/api/hygiene/keep", { key: r.key }).then(function (res) {
        if (!res.ok) { kp.disabled = false; window.alert(res.why || "not saved"); return; }
        load();
        loadLog();
      });
    });
    row.appendChild(kp);
  }
  return row;
}

// ------------------------------------------------------------------ approve / undo

function choiceLabel(g, v) {
  var hit = (g.choices || []).filter(function (c) {
    return typeof c === "object" ? c.value === v : c === v;
  })[0];
  return hit == null ? v : (typeof hit === "object" ? hit.label : hit);
}

function approve(g) {
  if (BUSY || document.querySelector(".modal.dlg")) return;
  var inc = included(g);
  var ex = g.rows.length - inc.length;
  var what = g.kind === "link" ? "Add a file link to " + inc.length + " action items"
    : g.kind === "promote" ? "Make " + inc.length + " item" + (inc.length === 1 ? "" : "s")
      + " into projects: " + inc.map(function (r) { return "#" + r.id; }).join(", ")
    : g.kind === "decide" ? (g.choices && typeof g.choices[0] === "object"
        ? "project → “" + choiceLabel(g, CHOICE[g.key]) + "” for the whole family"
        : g.title.split("=").pop().trim() + " → “" + CHOICE[g.key] + "”")
    : changeSummary(inc);
  confirmDialog({
    title: "Apply this fix?",
    lead: g.title,
    lines: [["Change", what], ["Rows", inc.length + (ex ? " (" + ex + " left out)" : "")],
            ["First", "a full backup of the database"],
            ["Undo", "from History, as long as none of these rows is edited in between"]],
    warn: g.kind === "decide" ? ["You chose the target value. Nothing maps it automatically."]
      : [],
    yes: "Yes, apply " + inc.length + (inc.length === 1 ? " row" : " rows")
  }, function () {
    BUSY = true;
    api("/api/hygiene/apply", { key: g.key, sha: g.sha,
      exclude: Object.keys(EXCL[g.key] || {}), choice: CHOICE[g.key] || "" })
      .then(function (r) {
        BUSY = false;
        var box = $("#hydone");
        box.innerHTML = "";
        if (!r.ok) { box.appendChild(el("div", "err", r.why)); return load(); }
        delete EXCL[g.key]; delete CHOICE[g.key]; delete OPEN[g.key];
        var b = el("div", "hyok");
        b.appendChild(el("span", null, "Applied " + r.n + (r.n === 1 ? " row" : " rows")
          + " — " + g.title + ". Backup: " + String(r.backup || "").split("/").pop()));
        if (r.warning) box.appendChild(el("div", "err", r.warning));
        var u = el("button", null, "Undo");
        u.addEventListener("click", function () { undo(r.log_id, g.title); });
        b.appendChild(u);
        box.appendChild(b);
        load();
      });
  });
}

// One line per field, from the rows actually being applied. A field whose rows go to
// different values (each weekly row to its own channel id) says so, rather than naming
// one row's value as if it were everyone's.
function changeSummary(rows) {
  var by = {}, order = [];
  rows.forEach(function (r) {
    (r.changes || []).forEach(function (ch) {
      var f = ch.field_label || ch.field;
      if (!by[f]) { by[f] = {}; order.push(f); }
      by[f][String(ch.after_label != null ? ch.after_label : ch.after)] = 1;
    });
  });
  return order.map(function (f) {
    var vals = Object.keys(by[f]);
    return vals.length === 1 ? f + " → “" + vals[0] + "”"
      : f + " → each row's own value (" + vals.length + " different — see the list)";
  }).join(", ");
}

function undo(id, title) {
  if (BUSY || document.querySelector(".modal.dlg")) return;
  confirmDialog({
    title: "Undo this fix?", lead: title,
    lines: [["What", "puts back exactly the values this fix changed"],
            ["Refused if", "any of those rows was edited since — nothing is half-undone"]],
    yes: "Yes, undo"
  }, function () {
    BUSY = true;
    api("/api/hygiene/undo", { log_id: id }).then(function (r) {
      BUSY = false;
      var box = $("#hydone");
      box.innerHTML = "";
      box.appendChild(el("div", r.ok ? "hyok" : "err",
        r.ok ? "Undone — " + r.n + " changes put back." : r.why));
      load();
    });
  });
}

function done(r) {
  if (r && r.ok === false) {
    $("#hydone").innerHTML = "";
    $("#hydone").appendChild(el("div", "err", r.why));
  }
  load();
}

function loadLog() {
  api("/api/hygiene/log").then(function (d) {
    var box = $("#log");
    box.innerHTML = "";
    var rows = (d && d.log) || [];
    if (!rows.length) {
      box.appendChild(el("div", "empty", "nothing applied yet"));
      return;
    }
    rows.forEach(function (e) {
      var row = el("div", "row");
      row.appendChild(el("div", "when", String(e.applied_at || "").slice(0, 16)));
      var mid = el("div", "mid");
      mid.appendChild(el("div", "name", e.title || e.group_key));
      mid.appendChild(el("div", "sub", String(e.group_key || "").indexOf("keep:") === 0
        ? "decision · by " + (e.applied_by || "?") + " · no data changed"
        : e.n + " changes · by " + (e.applied_by || "?")
          + (e.choice ? " · → " + e.choice : "")
          + " · backup " + String(e.backup || "").split("/").pop()));
      row.appendChild(mid);
      var right = el("div", "right");
      if (e.undone_at) {
        right.appendChild(el("span", "tag", "undone " + String(e.undone_at).slice(0, 16)));
      } else {
        var keepE = String(e.group_key || "").indexOf("keep:") === 0;
        var u = el("button", "tap", keepE ? "Suggest again" : "Undo");
        if (keepE) u.title = "Let Hygiene suggest promoting this item again";
        u.addEventListener("click", function () { undo(e.id, e.title); });
        right.appendChild(u);
      }
      row.appendChild(right);
      box.appendChild(row);
    });
  });
}

// ------------------------------------------------------------------ dialogs + files

function confirmDialog(o, onYes) {
  var wrap = el("div", "modal dlg");
  wrap.style.zIndex = "90";
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, o.title));
  sheet.appendChild(head);
  var f = el("div", "form");
  f.appendChild(el("p", "dlglead", o.lead));
  var grid = el("div", "keys");
  (o.lines || []).forEach(function (l) {
    grid.appendChild(el("b", null, l[0]));
    grid.appendChild(el("span", null, l[1]));
  });
  f.appendChild(grid);
  (o.warn || []).forEach(function (w) { f.appendChild(el("div", "warnbox", w)); });
  var acts = el("div", "dlgacts");
  var no = el("button", null, "Cancel");
  var yes = el("button", "primary", o.yes || "Yes");
  acts.appendChild(no);
  acts.appendChild(yes);
  f.appendChild(acts);
  sheet.appendChild(f);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);
  var close = function () { wrap.remove(); document.removeEventListener("keydown", key, true); };
  var key = function (e) { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", key, true);
  no.addEventListener("click", close);
  wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
  yes.addEventListener("click", function () { close(); onYes(); });
  no.focus();
}

var _filesJs = null;
function openFile(root, path) {
  function go() {
    api("/api/file?root=" + encodeURIComponent(root) + "&path=" + encodeURIComponent(path))
      .then(function (r) {
        if (!r.ok) { window.alert(r.error || r.why || "could not open"); return; }
        window.mtOpenFile({ root: r.root, path: r.path,
                            name: r.path.split("/").pop(), bytes: r.bytes });
      });
  }
  if (window.mtOpenFile) return go();
  if (!_filesJs) {
    _filesJs = new Promise(function (res, rej) {
      var md = document.createElement("script");
      md.src = "/static/md.js";
      md.onload = function () {
        var sc = document.createElement("script");
        sc.src = "/static/files.js";
        sc.onload = res;
        sc.onerror = rej;
        document.head.appendChild(sc);
      };
      md.onerror = rej;
      document.head.appendChild(md);
    });
  }
  _filesJs.then(go);
}

// ================================================================== Assistant memory (3b)
//
// A different area with different handling: ONE proposal at a time, no bulk approve. The
// review sheet shows the complete after-state the tool computed (hygiene_memory.plan), and
// approving sends that plan's `sha` — if any memory file moved since, it's refused.

var MSTATE = { pending: "Waiting for you", revised: "Revised — review again",
  revise: "Sent back to your assistant", applied: "Applied", rejected: "Rejected",
  undone: "Undone" };

function loadMemory() {
  api("/api/hygiene/memory").then(function (d) {
    if (d.off) {                     // no "memory_dir" in config.json: the area doesn't exist
      document.querySelector('#hytabs [data-area="memory"]').hidden = true;
      setArea("data");
      return;
    }
    $("#memfail").innerHTML = "";
    if (d.error || d.ok === false) {
      $("#memfail").appendChild(el("div", "err", d.error || d.why));
    }
    $("#nMem").textContent = d.open ? String(d.open) : "";
    renderProposals(d.proposals || []);
    renderFindings(d.findings || [], d.stats || {});
  });
  loadMemLog();
}

function renderProposals(props) {
  var box = $("#proposals");
  box.innerHTML = "";
  var open = props.filter(function (p) { return p.status === "pending" || p.status === "revised"; });
  var back = props.filter(function (p) { return p.status === "revise"; });
  var closed = props.filter(function (p) {
    return ["applied", "rejected", "undone"].indexOf(p.status) >= 0; });
  if (!open.length && !back.length) {
    box.appendChild(el("div", "empty", "No proposals waiting. Copy a request from Findings to "
      + "your assistant, and its proposal shows up here."));
  }
  open.concat(back).forEach(function (p) { box.appendChild(propCard(p)); });
  if (closed.length) {
    var dd = el("details", "hyskipped");
    dd.appendChild(el("summary", null, "Done (" + closed.length + ")"));
    closed.forEach(function (p) { dd.appendChild(propCard(p)); });
    box.appendChild(dd);
  }
}

function propCard(p) {
  var c = el("div", "hyg k-mem" + (p.status === "revise" ? " skipped" : ""));
  var top = el("div", "hygtop");
  top.appendChild(el("span", "tag hyk mem", p.kind));
  top.appendChild(el("b", "hygtitle", p.title));
  top.appendChild(el("span", "hygn", MSTATE[p.status] || p.status));
  c.appendChild(top);
  c.appendChild(el("div", "hygwhy", p.reason));
  var files = el("div", "hych");
  files.textContent = (p.files || []).join(" · ") + "   (" + p.id + ")";
  c.appendChild(files);
  if (p.notes) c.appendChild(el("div", "warnbox", "Your notes: " + p.notes));
  var foot = el("div", "hygfoot");
  foot.appendChild(el("span", "grow"));
  var rv = el("button", p.status === "pending" || p.status === "revised" ? "primary" : null,
    p.status === "pending" || p.status === "revised" ? "Review…" : "View");
  rv.addEventListener("click", function () { review(p.id); });
  foot.appendChild(rv);
  c.appendChild(foot);
  return c;
}

function review(pid) {
  if (document.querySelector(".modal.hymem")) return;
  api("/api/hygiene/memory-plan?id=" + encodeURIComponent(pid)).then(function (pl) {
    if (!pl.ok) { window.alert(pl.why || "could not load the proposal"); return; }
    openReview(pl);
  });
}

function openReview(pl) {
  var P = pl.proposal;
  var live = P.status === "pending" || P.status === "revised";
  var wrap = el("div", "modal hymem");
  var sheet = el("div", "sheet wide hysheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, "Memory proposal · " + P.kind + " · " + P.id));
  var x = el("button", null, "Close");
  head.appendChild(x);
  sheet.appendChild(head);
  var body = el("div", "form hyrev");
  sheet.appendChild(body);
  body.appendChild(el("div", "hybanner", "Changes here alter how your assistant works in every "
    + "session. Read the right-hand side: it's exactly what the files will say."));
  body.appendChild(el("h3", "hyh3", P.title));
  body.appendChild(el("p", "hygwhy", P.reason));

  var s = pl.summary || {};
  body.appendChild(el("div", "count", [
    s.written + " file" + (s.written === 1 ? "" : "s") + " written",
    s.created ? s.created + " new" : "", s.archived ? s.archived + " moved to archive/" : "",
    s.links ? s.links + " links repointed" : "", s.index ? "MEMORY.md updated" : ""
  ].filter(Boolean).join(" · ")));

  (pl.errors || []).forEach(function (e) { body.appendChild(el("div", "err", e)); });
  (pl.flags || []).forEach(function (f) {
    body.appendChild(el("div", "warnbox", "Person check: " + f
      + " — confirm this is right before approving."));
  });
  (pl.warnings || []).forEach(function (w) { body.appendChild(el("div", "warnbox", w)); });

  var quotes = [];
  if ((P.conflicts || []).length) {
    var cb = el("div", "hyconf");
    cb.appendChild(el("b", null, "The contradiction"));
    P.conflicts.forEach(function (c) {
      [["A", c.a], ["B", c.b]].forEach(function (pair) {
        if (!pair[1]) return;
        if (pair[1].quote) quotes.push(pair[1].quote);
        var q = el("div", "hyq");
        q.appendChild(el("span", "f", pair[0] + " · " + (pair[1].file || "") + ": "));
        q.appendChild(el("span", null, "“" + (pair[1].quote || "") + "”"));
        cb.appendChild(q);
      });
      if (c.resolution) cb.appendChild(el("div", "hyres", "Resolution: " + c.resolution));
    });
    body.appendChild(cb);
  }

  (pl.files || []).forEach(function (f) { body.appendChild(fileBlock(pl, f, quotes, live)); });

  var hist = el("details", "hyhist");
  hist.appendChild(el("summary", null, "Proposal history"));
  (P.history || []).forEach(function (h) {
    hist.appendChild(el("div", "hych", (h.at || "") + " · " + (h.by || "") + " · " + h.what
      + (h.notes ? " — " + h.notes : "") + (h.summary ? " — " + h.summary : "")));
  });
  body.appendChild(hist);

  var box = el("div");
  body.appendChild(box);
  var acts = el("div", "dlgacts hyacts");
  if (live) {
    var sb = el("button", null, "Send back to your assistant…");
    sb.addEventListener("click", function () { sendBack(P.id, box); });
    var rj = el("button", null, "Reject…");
    rj.addEventListener("click", function () { rejectIt(P.id, close); });
    var ap = el("button", "primary", "Approve…");
    ap.disabled = !!(pl.errors || []).length;
    if (ap.disabled) ap.title = "Fix the errors above first (send it back to your assistant)";
    ap.addEventListener("click", function () { approveMem(pl, close); });
    acts.appendChild(sb); acts.appendChild(rj); acts.appendChild(el("span", "grow"));
    acts.appendChild(ap);
  }
  body.appendChild(acts);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);
  function close() {
    wrap.remove();
    document.removeEventListener("keydown", key, true);
  }
  function key(e) {
    if (e.key === "Escape" && !document.querySelector(".modal.dlg")) { e.stopPropagation(); close(); }
  }
  document.addEventListener("keydown", key, true);
  x.addEventListener("click", close);
  wrap._close = close;
}

function fileBlock(pl, f, quotes, live) {
  var b = el("div", "hyfile");
  var h = el("div", "hyfhead");
  var what = { modify: "changed", create: "new file", move: "moved to " + (f.to || "archive/") }[f.action]
    || f.action;
  if (f.history) what = "new: the whole old file, verbatim, from " + f.from;
  h.appendChild(el("b", null, f.path));
  h.appendChild(el("span", "tag", what));
  if (f.links) h.appendChild(el("span", "tag", f.links + " links repointed"));
  if (f.reworded) {
    var rw = el("span", "tag", f.reworded + (f.reworded === 1 ? " line reworded" : " lines reworded"));
    rw.title = "Wording changed; the old and new line are side by side in the diff below";
    h.appendChild(rw);
  }
  if (f.bytes_before != null) {
    h.appendChild(el("span", "hygn", Math.round(f.bytes_before / 1024 * 10) / 10 + " KB → "
      + Math.round(f.bytes_after / 1024 * 10) / 10 + " KB"));
  }
  var op = el("button", "tap", "Open current");
  op.addEventListener("click", function () { openFile("memory", f.from || f.path); });
  if (f.action !== "create" || f.history) h.appendChild(op);
  b.appendChild(h);
  if (f.lost && f.lost.length) {
    var ld = el("details", "hylost");
    ld.open = true;
    ld.appendChild(el("summary", null, f.lost.length + " line(s) leave your assistant's memory "
      + "entirely"));
    f.lost.slice(0, 60).forEach(function (l) { ld.appendChild(el("div", "hych b", l)); });
    b.appendChild(ld);
  }
  if (f.rows && f.rows.length) b.appendChild(diffTable(f.rows, quotes));
  if (f.editable && live) {
    var eb = el("button", "tap", "Edit the proposed text");
    var area = null;
    eb.addEventListener("click", function () {
      if (area) return;
      area = el("textarea", "fmdesc hyedit");
      area.value = f.content || "";
      area.rows = 16;
      var save = el("button", "primary", "Save my edit");
      var out = el("div");
      save.addEventListener("click", function () {
        api("/api/hygiene/mem-edit", { id: pl.proposal.id, path: f.path, content: area.value })
          .then(function (r) {
            if (!r.ok) { out.innerHTML = ""; out.appendChild(el("div", "err", r.why)); return; }
            var m = document.querySelector(".modal.hymem");
            if (m && m._close) m._close();
            review(pl.proposal.id);
          });
      });
      b.appendChild(area); b.appendChild(save); b.appendChild(out);
      area.focus();
    });
    b.appendChild(eb);
  }
  return b;
}

function diffTable(rows, quotes) {
  var t = el("div", "hydiff");
  var hd = el("div", "hyrowd hdr");
  hd.appendChild(el("div", "c", "Now"));
  hd.appendChild(el("div", "c", "After approving"));
  t.appendChild(hd);
  rows.forEach(function (r) {
    if (r.t === "skip") {
      t.appendChild(el("div", "hyskip", "… " + r.n + " unchanged lines …"));
      return;
    }
    var row = el("div", "hyrowd " + r.t);
    var L = el("div", "c l" + (r.l == null ? " none" : ""), r.l == null ? "" : r.l);
    var R = el("div", "c r" + (r.r == null ? " none" : ""), r.r == null ? "" : r.r);
    if (r.l && quotes.some(function (q) { return q && r.l.indexOf(q) >= 0; })) L.classList.add("hl");
    row.appendChild(L); row.appendChild(R);
    t.appendChild(row);
  });
  return t;
}

function approveMem(pl, closeSheet) {
  if (document.querySelector(".modal.dlg")) return;
  var s = pl.summary || {};
  var warn = (pl.flags || []).map(function (f) { return "Person check: " + f; })
    .concat(pl.warnings || []);
  confirmDialog({
    title: "Apply this memory change?",
    lead: pl.proposal.title,
    lines: [["Files", s.written + " written" + (s.created ? " (" + s.created + " new)" : "")
              + (s.archived ? ", " + s.archived + " moved to archive/" : "")],
            ["Links", s.links ? s.links + " repointed" : "none change"],
            ["First", "a full copy of the memory folder, plus a before-copy of each file"],
            ["Undo", "from History, as long as none of these files is edited in between"]],
    warn: warn,
    yes: "Yes, apply it"
  }, function () {
    api("/api/hygiene/mem-apply", { id: pl.proposal.id, sha: pl.sha }).then(function (r) {
      closeSheet();
      var box = $("#memdone");
      box.innerHTML = "";
      if (!r.ok) { box.appendChild(el("div", "err", r.why)); return loadMemory(); }
      var b = el("div", "hyok");
      b.appendChild(el("span", null, "Applied — " + pl.proposal.title + ". "
        + (r.modified.length + r.created.length) + " written, " + r.moved.length
        + " archived."));
      var u = el("button", null, "Undo");
      u.addEventListener("click", function () { undoMem(r.log_id, pl.proposal.title); });
      b.appendChild(u);
      box.appendChild(b);
      loadMemory();
    });
  });
}

function undoMem(id, title) {
  if (document.querySelector(".modal.dlg")) return;
  confirmDialog({
    title: "Undo this memory change?", lead: title,
    lines: [["What", "every file goes back to exactly what it was; archived files come back"],
            ["Refused if", "any of those files was edited since — nothing is half-undone"]],
    yes: "Yes, undo"
  }, function () {
    api("/api/hygiene/mem-undo", { log_id: id }).then(function (r) {
      var box = $("#memdone");
      box.innerHTML = "";
      box.appendChild(el("div", r.ok ? "hyok" : "err", r.ok ? "Undone." : r.why));
      loadMemory();
    });
  });
}

function sendBack(pid, box) {
  if (document.querySelector(".modal.dlg")) return;
  notesDialog("Send back to your assistant", "Your assistant revises the proposal from these "
    + "notes and it "
    + "comes back here for review. Nothing changes in memory.", true, function (notes) {
    api("/api/hygiene/mem-sendback", { id: pid, notes: notes }).then(function (r) {
      box.innerHTML = "";
      if (!r.ok) { box.appendChild(el("div", "err", r.why)); return; }
      box.appendChild(pasteBox("Sent back. Paste this to your assistant:", r.line));
      loadMemory();
    });
  });
}

function rejectIt(pid, closeSheet) {
  notesDialog("Reject this proposal", "Nothing changes in memory. The proposal is kept, "
    + "marked rejected, with your note.", false, function (notes) {
    api("/api/hygiene/mem-reject", { id: pid, notes: notes }).then(function (r) {
      closeSheet();
      if (!r.ok) { $("#memdone").innerHTML = ""; $("#memdone").appendChild(el("div", "err", r.why)); }
      loadMemory();
    });
  });
}

function notesDialog(title, hint, required, onSend) {
  var wrap = el("div", "modal dlg");
  wrap.style.zIndex = "95";
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, title));
  sheet.appendChild(head);
  var f = el("div", "form");
  f.appendChild(el("p", "dlglead", hint));
  var ta = el("textarea", "fmdesc");
  ta.rows = 6;
  ta.placeholder = required ? "What should your assistant change?" : "Why (optional)";
  f.appendChild(ta);
  var out = el("div");
  f.appendChild(out);
  var acts = el("div", "dlgacts");
  var no = el("button", null, "Cancel");
  var yes = el("button", "primary", required ? "Send back" : "Reject");
  acts.appendChild(no); acts.appendChild(yes);
  f.appendChild(acts);
  sheet.appendChild(f);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);
  var close = function () { wrap.remove(); document.removeEventListener("keydown", key, true); };
  var key = function (e) { if (e.key === "Escape") { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", key, true);
  no.addEventListener("click", close);
  wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
  yes.addEventListener("click", function () {
    var v = ta.value.trim();
    if (required && !v) {
      out.innerHTML = "";
      out.appendChild(el("div", "err", "Write what you want changed — the notes are the point."));
      return;
    }
    close(); onSend(v);
  });
  ta.focus();
}

function pasteBox(title, line) {
  var b = el("div", "hyok hypaste");
  b.appendChild(el("b", null, title));
  var pre = el("pre", "wline", line);
  b.appendChild(pre);
  var cp = el("button", null, "Copy");
  cp.addEventListener("click", function () { copyText(line, cp); });
  b.appendChild(cp);
  return b;
}

function copyText(text, btn) {
  var done = function () { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
  } else { fallbackCopy(text); done(); }
}
function fallbackCopy(text) {
  var t = el("textarea");
  t.value = text; document.body.appendChild(t); t.select();
  try { document.execCommand("copy"); } catch (e) {}
  t.remove();
}

function renderFindings(groups, stats) {
  var box = $("#findings");
  box.innerHTML = "";
  if (stats.files) {
    box.appendChild(el("div", "count", stats.files + " live memory files · "
      + Math.round((stats.total_bytes || 0) / 1024) + " KB · MEMORY.md "
      + Math.round((stats.index_bytes || 0) / 1024 * 10) / 10 + " KB (loads every session)"));
  }
  if (!groups.length) { box.appendChild(el("div", "empty", "nothing found")); return; }
  groups.forEach(function (g) {
    var c = el("div", "hyg k-watch");
    var top = el("div", "hygtop");
    top.appendChild(el("span", "tag hyk watch", "Finding"));
    top.appendChild(el("b", "hygtitle", g.title));
    top.appendChild(el("span", "hygn", g.count + (g.count === 1 ? " item" : " items")));
    c.appendChild(top);
    c.appendChild(el("div", "hygwhy", g.why));
    var list = el("div", "hyrows");
    var drawn = false;
    function draw(all) {
      list.innerHTML = "";
      (all ? g.rows : g.rows.slice(0, 3)).forEach(function (r) {
        var row = el("div", "hyrow");
        var mid = el("div", "hymid");
        var lab = el("div", "hylabel");
        lab.appendChild(el("span", "hyid", r.path));
        lab.appendChild(document.createTextNode("  " + (r.label || "")));
        mid.appendChild(lab);
        row.appendChild(mid);
        var o = el("button", "tap", "Open");
        o.addEventListener("click", function () { openFile("memory", r.path); });
        row.appendChild(o);
        list.appendChild(row);
      });
    }
    draw(false);
    c.appendChild(list);
    var foot = el("div", "hygfoot");
    if (g.rows.length > 3) {
      var more = el("button", "tap", "Show all " + g.rows.length);
      more.addEventListener("click", function () {
        drawn = !drawn; draw(drawn); more.textContent = drawn ? "Show fewer" : "Show all " + g.rows.length;
      });
      foot.appendChild(more);
    }
    foot.appendChild(el("span", "grow"));
    var ask = el("button", null, "Copy request for your assistant");
    ask.title = g.ask;
    ask.addEventListener("click", function () { copyText(g.ask, ask); });
    foot.appendChild(ask);
    c.appendChild(foot);
    box.appendChild(c);
  });
}

function loadMemLog() {
  api("/api/hygiene/memory-log").then(function (d) {
    var box = $("#memlog");
    box.innerHTML = "";
    var rows = (d && d.log) || [];
    if (!rows.length) { box.appendChild(el("div", "empty", "nothing applied yet")); return; }
    rows.forEach(function (e) {
      var row = el("div", "row");
      row.appendChild(el("div", "when", String(e.applied_at || "").slice(0, 16)));
      var mid = el("div", "mid");
      mid.appendChild(el("div", "name", e.title || e.group_key));
      mid.appendChild(el("div", "sub", (e.choice || "") + " · " + e.n + " files · "
        + e.group_key + " · by " + (e.applied_by || "?")));
      row.appendChild(mid);
      var right = el("div", "right");
      if (e.undone_at) right.appendChild(el("span", "tag", "undone " + String(e.undone_at).slice(0, 16)));
      else {
        var u = el("button", "tap", "Undo");
        u.addEventListener("click", function () { undoMem(e.id, e.title); });
        right.appendChild(u);
      }
      row.appendChild(right);
      box.appendChild(row);
    });
  });
}

// ------------------------------------------------------------------ boot

document.querySelectorAll("#hytabs button").forEach(function (b) {
  b.addEventListener("click", function () { setArea(b.dataset.area); });
});
var saved = null;
try { saved = localStorage.getItem("hy.area"); } catch (e) {}
setArea(saved === "memory" ? "memory" : "data");
load();
loadMemory();
})();

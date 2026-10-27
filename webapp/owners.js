// owners.js — Phase 7b: decide owner spellings one at a time.
//
// Person Verification is a hard rule, so this page offers and never chooses: each name
// in the spelling gets its candidate contacts (same name first, then similar names, the
// item's account first), a search over every contact, and "+ New person". Link applies
// the decision to every unlinked row with this spelling (backed up, logged, undoable from
// Hygiene → log) and remembers it for future rows. "Not a person" keeps the text — for a
// team, an account, or the assistant.
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
function post(p, body) {
  return api(p, { method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(body) });
}

var Q = [], CONTACTS = [], CUSTOMERS = [], I = 0, DONE = 0, SKIPPED = {};

function load() {
  return Promise.all([api("/api/hygiene/owners"), api("/api/contacts?limit=2000"),
                      api("/api/tracking/meta")]).then(function (r) {
    if (!r[0].ready) {
      $("#card").innerHTML = "";
      $("#card").appendChild(el("div", "empty",
        "Owner review needs the Phase 7 migration (tracking.py --migrate-p7)."));
      return false;
    }
    Q = (r[0].items || []).filter(function (it) { return !SKIPPED[it.key]; });
    CONTACTS = (r[1].contacts || []).filter(function (c) { return !c.merged_into && !c.archived_at; });
    CUSTOMERS = (r[2].customers || []);
    return true;
  });
}

function custShort(id) {
  var c = CUSTOMERS.filter(function (x) { return x.id === id; })[0];
  return c ? c.short_name : "";
}
function who(c) {
  var bits = [];
  if (c.role) bits.push(c.role);
  var acct = c.customer || custShort(c.customer_id);
  if (acct) bits.push(acct); else if (c.company) bits.push(c.company);
  return bits.join(" · ");
}

function progress() {
  var rows = Q.reduce(function (n, it) { return n + it.count; }, 0);
  $("#progress").textContent = Q.length ? Q.length + " spelling" + (Q.length === 1 ? "" : "s")
    + " · " + rows + " rows to decide" + (DONE ? " · " + DONE + " decided this session" : "")
    : "";
}

function show() {
  progress();
  var box = $("#card");
  box.innerHTML = "";
  var it = Q[0];
  if (!it) {
    box.appendChild(el("div", "empty", DONE ? "Every owner spelling is decided. Nice."
                                             : "No owner spellings waiting."));
    return;
  }
  var c = el("div", "card own-card");
  c.appendChild(el("div", "own-spell", it.example));
  c.appendChild(el("div", "own-meta", it.count + " row" + (it.count === 1 ? "" : "s")
    + " (" + it.live + " open) · " + it.accounts.join(", ")));
  var why = el("ul", "own-why");
  it.reasons.forEach(function (r) { why.appendChild(el("li", null, r)); });
  c.appendChild(why);

  var rows = el("div", "own-rows");
  it.rows.forEach(function (r) {
    var a = el("a", "own-row" + (r.live ? "" : " done"));
    a.href = "/tracking?open=" + r.id;
    a.target = "mt-tracking";
    a.appendChild(el("span", "gid", "#" + r.id));
    a.appendChild(el("span", "own-t", r.label));
    if (r.account) a.appendChild(el("span", "tag cust", r.account));
    a.appendChild(el("span", "tag", r.status));
    rows.appendChild(a);
  });
  if (it.count > it.rows.length) rows.appendChild(el("div", "hint", "… and " + (it.count - it.rows.length) + " more"));
  c.appendChild(rows);

  // One slot per name: owner first, then "with". Each slot holds the chosen contact id.
  var slots = [];
  var slotBox = el("div", "own-slots");
  function addSlot(role, name, candidates) {
    var s = { role: role, name: name, chosen: null };
    var w = el("div", "own-slot");
    var h = el("div", "own-slot-h");
    h.appendChild(el("b", null, role === "owner" ? "Owner" : "With"));
    h.appendChild(el("span", null, name ? " — written as “" + name + "”" : ""));
    if (role === "with") {
      var rm = el("button", "tap", "remove");
      rm.addEventListener("click", function () { slots.splice(slots.indexOf(s), 1); w.remove(); sync(); });
      h.appendChild(rm);
    }
    w.appendChild(h);
    var opts = el("div", "own-opts");
    function option(cand) {
      var l = el("label", "own-opt");
      var r = el("input");
      r.type = "radio";
      r.name = "slot" + slots.length + "-" + Math.random().toString(36).slice(2, 6);
      r.addEventListener("change", function () {
        opts.querySelectorAll("label").forEach(function (x) { x.classList.remove("on"); });
        l.classList.add("on");
        s.chosen = cand.id;
        sync();
      });
      l.appendChild(r);
      l.appendChild(el("span", "own-n", cand.name));
      l.appendChild(el("span", "own-w", who(cand)));
      if (cand.verified === false || cand.verification_status === "needs_confirmation") {
        l.appendChild(el("span", "tag todo", "unverified"));
      }
      if (cand.why) l.appendChild(el("span", "own-why2", cand.why));
      opts.appendChild(l);
      return r;
    }
    (candidates || []).forEach(option);
    if (!(candidates || []).length) opts.appendChild(el("div", "hint", "No contact looks like this name."));
    w.appendChild(opts);

    // Search every contact, or add a new person.
    var tools = el("div", "own-tools");
    var q = el("input");
    q.placeholder = "Search all contacts…";
    q.setAttribute("list", "own-all");
    var useQ = el("button", "tap", "Use");
    useQ.addEventListener("click", function () {
      var hit = CONTACTS.filter(function (x) { return x.name.toLowerCase() === q.value.trim().toLowerCase(); });
      if (hit.length !== 1) { alert(hit.length ? "More than one contact has that name — pick one from the list above (search shows them)." : "No contact with exactly that name."); return; }
      option(Object.assign({ why: "searched" }, hit[0])).click();
      q.value = "";
    });
    var nw = el("button", "tap", "+ New person");
    nw.addEventListener("click", function () { newPerson(name, function (cand) { option(cand).click(); }); });
    tools.appendChild(q);
    tools.appendChild(useQ);
    tools.appendChild(nw);
    w.appendChild(tools);
    slotBox.appendChild(w);
    slots.push(s);
  }
  it.names.forEach(function (n) { addSlot(n.role, n.name, n.candidates); });
  if (!it.names.length) addSlot("owner", "", []);
  c.appendChild(slotBox);
  var addWith = el("button", "tap", "+ add a “with” person");
  addWith.addEventListener("click", function () { addSlot("with", "", []); });
  c.appendChild(addWith);

  var err = el("div", "err");
  err.hidden = true;
  var acts = el("div", "own-acts");
  var link = el("button", "primary", "Link — applies to " + it.count + " row" + (it.count === 1 ? "" : "s"));
  var np = el("button", "tap", "Not a person — keep as text");
  np.title = "A team, an account, your assistant… — the text stays, it stops asking";
  var sk = el("button", "tap", "Skip for now");
  acts.appendChild(link);
  acts.appendChild(np);
  acts.appendChild(sk);
  c.appendChild(acts);
  c.appendChild(err);
  box.appendChild(c);

  function sync() {
    var ok = slots.length && slots[0].role === "owner" && slots[0].chosen
      && slots.every(function (s) { return s.chosen; });
    link.disabled = !ok;
    link.title = ok ? "" : "Choose a contact for every name (or remove a “with” you don't want)";
  }
  sync();
  function after(r, label) {
    if (!r.ok) { err.textContent = r.why || "not saved"; err.hidden = false; return; }
    DONE++;
    var last = $("#last");
    last.textContent = "“" + it.example + "” → " + label
      + (r.linked != null ? " · " + r.linked + " row" + (r.linked === 1 ? "" : "s") + " linked" : "")
      + (r.left && r.left.length ? " · " + r.left.length + " still need a pick (shown again)" : "");
    last.hidden = false;
    Q.shift();
    load().then(function (ok) { if (ok) show(); });
  }
  link.addEventListener("click", function () {
    link.disabled = true;
    var ids = slots.map(function (s) { return s.chosen; });
    post("/api/hygiene/owners/decide", { key: it.key, kind: "people", owner_id: ids[0],
      with_ids: ids.slice(1) }).then(function (r) {
      var names = ids.map(function (i) {
        var c2 = CONTACTS.filter(function (x) { return x.id === i; })[0];
        return c2 ? c2.name : "#" + i;
      });
      after(r, names[0] + (names.length > 1 ? " · with " + names.slice(1).join(", ") : ""));
    });
  });
  np.addEventListener("click", function () {
    post("/api/hygiene/owners/decide", { key: it.key, kind: "not_person" })
      .then(function (r) { after(r, "not a person (text kept)"); });
  });
  sk.addEventListener("click", function () {
    SKIPPED[it.key] = 1;
    Q.shift();
    show();
  });
}

// "+ New person": name, role, company, account. Saved verified (the user is the one saying
// who this is), with the source noting it came from the owner review.
function newPerson(seed, done) {
  var m = el("div", "modal");
  var s = el("div", "sheet");
  var h = el("div", "head");
  h.appendChild(el("b", null, "New person"));
  s.appendChild(h);
  var f = el("div", "form");
  function field(label, input) {
    var l = el("label", "f");
    l.appendChild(el("span", null, label));
    l.appendChild(input);
    f.appendChild(l);
    return input;
  }
  var n = field("Full name", el("input"));
  n.value = seed || "";
  var r = field("Title / role", el("input"));
  var co = field("Company (your company, a reseller, a partner…)", el("input"));
  var ac = field("Account (customer side)", el("select"));
  ac.appendChild(new Option("— none —", ""));
  CUSTOMERS.forEach(function (c) { ac.appendChild(new Option(c.name, String(c.id))); });
  var err = el("div", "err");
  err.hidden = true;
  f.appendChild(err);
  s.appendChild(f);
  var foot = el("div", "foot");
  var cancel = el("button", "tap", "Cancel");
  var save = el("button", "primary", "Add person");
  foot.appendChild(cancel);
  foot.appendChild(save);
  s.appendChild(foot);
  m.appendChild(s);
  document.body.appendChild(m);
  n.focus();
  cancel.addEventListener("click", function () { m.remove(); });
  save.addEventListener("click", function () {
    if (n.value.trim().split(/\s+/).length < 2 && !confirm("Only one name — save anyway?")) return;
    var fields = { name: n.value.trim(), role: r.value.trim(), company: co.value.trim(),
                   verification_status: "verified",
                   source: "mt owner review " + new Date().toISOString().slice(0, 10) };
    if (ac.value) fields.customer_id = ac.value;
    post("/api/contacts/save", { id: null, fields: fields }).then(function (res) {
      var c = res.contact || res.item;
      if (!res.ok || !c) { err.textContent = res.why || "not saved"; err.hidden = false; return; }
      CONTACTS.push(c);
      fillAll();
      m.remove();
      done(Object.assign({ why: "new" }, c));
    });
  });
}

function fillAll() {
  var dl = $("#own-all");
  if (!dl) { dl = el("datalist"); dl.id = "own-all"; document.body.appendChild(dl); }
  dl.innerHTML = "";
  CONTACTS.forEach(function (c) { dl.appendChild(new Option(c.name, c.name)); });
}

load().then(function (ok) { if (ok) { fillAll(); show(); } });
})();

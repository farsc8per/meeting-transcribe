// contacts.js — everything behind /contacts.
//
// ~274 people. That is small enough to fetch the whole list once and filter it in the
// browser, so typing is instant and the server is not asked a question per keystroke.
// The only per-request calls are the ones that need the DB: one person's card (with the
// meetings they turn up in), the duplicate groups, and the writes.
//
// ESCAPING, the one rule that matters here: `snippet` on a "where I've seen them" row
// arrives already HTML-escaped from search.py with <mark> wrapped around the matched
// terms, so it goes in through innerHTML and is the ONLY innerHTML of live content in
// this file. Every other value — name, email, role, phone, company, aliases, notes,
// source, slack id, title, path, customer, dates — is raw text out of the DB or off
// disk and goes in through textContent. Getting that backwards is an XSS hole.
//
// Nothing on this page blocks anything. Verification state is shown, never enforced:
// naming a speaker in `mt` stays free-form.
(function () {
  "use strict";

  var DEBOUNCE = 120;          // purely a render throttle — no request is made per keystroke
  var LIMIT = 1000;

  // Seeded from the schema note in CONTACTS-CONTRACT.md, then overwritten by whatever the
  // data actually says, so a renamed or added account fixes itself on load.
  var CUSTOMERS = {};   // customer id -> short name, from the customers table via /api/contacts

  var all = [];                // every contact, fetched once
  var stats = {};
  var groups = null;           // duplicate groups, fetched when that tab is first opened
  var groupsErr = null;
  var voices = null;           // enrolled voiceprints no contact row answers to
  var voicesErr = null;
  var showDismissed = false;
  var tab = "all";
  var q = "", fCust = "", fStatus = "";
  var listErr = null;
  var timer = null;
  var toast = null;            // {text, undo}

  var listEl = document.getElementById("list");
  var countEl = document.getElementById("count");

  // Paged in the BROWSER: the whole list (~240 KB) is fetched once and filtered here by
  // design, so paging only has to limit what gets DRAWN. One pager serves every
  // person-list tab; the duplicate and voice worklists render groups and hide it.
  var PG = window.mtPager("contacts", function () { draw(); });
  listEl.parentNode.insertBefore(PG.mount(), listEl);
  listEl.parentNode.insertBefore(PG.mount(), listEl.nextSibling);
  function showPagers(on) {
    Array.prototype.forEach.call(document.querySelectorAll(".pager"),
      function (b) { b.hidden = !on; });
  }
  var FIELD_HELP = {};
  var statsEl = document.getElementById("stats");
  var tabsEl = document.getElementById("tabs");
  var qBox = document.getElementById("cq");
  var custSel = document.getElementById("ccust");
  var statSel = document.getElementById("cstatus");
  var newBtn = document.getElementById("cnew");
  if (!listEl) return;

  // ── little helpers ─────────────────────────────────────────────────────────
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;     // textContent, always — see the header note
    return e;
  }
  function j(r) {
    if (!r.ok) { var e = new Error("http " + r.status); e.status = r.status; throw e; }
    return r.json();
  }
  function post(url, body) {
    return fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                       body: JSON.stringify(body)})
      .then(j)
      .catch(function (e) {
        return {ok: false, why: e && e.status === 404
          ? "Contacts isn’t in this server yet — restart mt web."
          : "Contacts is not answering."};
      });
  }
  function has(v) { return v != null && String(v).trim() !== ""; }
  function txt(v) { return v == null ? "" : String(v); }
  function low(v) { return txt(v).trim().toLowerCase(); }
  function byId(id) {
    for (var i = 0; i < all.length; i++) if (all[i].id === id) return all[i];
    return null;
  }

  // ── dismissed pairs ────────────────────────────────────────────────────────
  // "These two are genuinely different people" is a ruling, and it lives in the DB
  // (contacts.not_duplicate_of), not in this browser. The assistant reads the same table,
  // so a pair the user has ruled on stops being raised there too — which matters because
  // four of the twelve groups are one person deliberately carried once per account and
  // would otherwise come back forever. Nothing here is read from localStorage; an old
  // key left over from the first cut of this page is simply ignored.
  function pairsOf(g) {
    var out = [], r = g.rows || [];
    for (var i = 0; i < r.length; i++)
      for (var k = i + 1; k < r.length; k++)
        out.push([Math.min(r[i].id, r[k].id), Math.max(r[i].id, r[k].id)]);
    return out;
  }
  function groupDismissed(g) { return !!g.dismissed; }

  // ── data ───────────────────────────────────────────────────────────────────
  function learn() {
    all.forEach(function (c) {
      if (c.customer_id && c.customer) CUSTOMERS[c.customer_id] = c.customer;
    });
  }

  function recount() {
    var s = {total: 0, verified: 0, needs_confirmation: 0, missing_email: 0,
             missing_role: 0, with_voiceprint: 0,
             duplicate_groups: stats.duplicate_groups};
    all.forEach(function (c) {
      s.total++;
      if (c.verification_status === "needs_confirmation") s.needs_confirmation++;
      else s.verified++;
      if (!has(c.email)) s.missing_email++;
      if (!has(c.role)) s.missing_role++;
      if (c.has_voiceprint) s.with_voiceprint++;
    });
    stats = s;
  }

  // Fold a row the server just handed back into the local copy, rather than refetching
  // 274 rows to learn about one. update_contact() returns the stored row; `customer` and
  // `has_voiceprint` are list_contacts() extras that it is not contracted to include, so
  // they are re-derived instead of being allowed to go undefined.
  function apply(row) {
    if (!row || !row.id) return;
    var cur = byId(row.id);
    var merged = cur ? Object.assign(cur, row) : Object.assign({}, row);
    if (merged.customer == null)
      merged.customer = merged.customer_id ? (CUSTOMERS[merged.customer_id] || null) : null;
    if (merged.has_voiceprint == null) merged.has_voiceprint = false;
    if (!cur) all.push(merged);
    all.sort(function (a, b) {
      return low(a.name) < low(b.name) ? -1 : low(a.name) > low(b.name) ? 1 : 0;
    });
    recount();
    drawStats();
  }

  function reload(then) {
    fetch("/api/contacts?limit=" + LIMIT).then(j).then(function (r) {
      if (r.error) { listErr = r.error; all = []; stats = {}; }
      else {
        listErr = null;
        all = r.contacts || [];
        stats = r.stats || {};
        FIELD_HELP = r.field_help || {};
        Object.keys(r.accounts || {}).forEach(function (k) { CUSTOMERS[k] = r.accounts[k]; });
        learn();
        fillCustomers();
      }
      drawStats(); draw();
      if (then) then();
    }).catch(function (e) {
      listErr = e && e.status === 404
        ? "Contacts isn’t in this server yet — restart mt web."
        : "Contacts is not answering.";
      all = []; drawStats(); draw();
      if (then) then();
    });
  }

  function loadDupes(force) {
    if (groups !== null && !force) { draw(); return; }
    groups = null; groupsErr = null;
    draw();
    // ?dismissed=1 because the page needs the ruled-on groups for its "show N
    // dismissed" toggle. The header chip still counts only the open ones, which is what
    // stats() reports.
    fetch("/api/contacts/duplicates?dismissed=1").then(j).then(function (r) {
      if (r.error) { groupsErr = r.error; groups = []; }
      else {
        groups = r.groups || [];
        stats.duplicate_groups = groups.filter(function (g) { return !g.dismissed; }).length;
        drawStats();
      }
      draw();
    }).catch(function () {
      groupsErr = "Contacts is not answering."; groups = []; draw();
    });
  }

  // ── voices with no contact ─────────────────────────────────────────────────
  // Enrolling a voice in `mt` and having a row in this table were, until now, two
  // unconnected facts — enrolling "Pat Placeholder" created nothing and said nothing. This
  // is the backlog that silence built up, and it is fetched at load so the header can
  // carry the count even when the tab is closed.
  var triageRows = null, triageErr = null;
  var archRows = null, archErr = null;

  function loadTriage() {
    fetch("/api/contacts/triage").then(j).then(function (r) {
      triageErr = r.error || null;
      triageRows = r.rows || [];
      drawStats();
      if (tab === "triage") draw();
    }).catch(function () {
      triageErr = "Contacts is not answering."; triageRows = [];
      if (tab === "triage") draw();
    });
  }

  function loadArchived() {
    fetch("/api/contacts?archived=archived&limit=" + LIMIT).then(j).then(function (r) {
      archErr = r.error || null;
      archRows = r.contacts || [];
      if (tab === "archived") draw();
    }).catch(function () {
      archErr = "Contacts is not answering."; archRows = [];
      if (tab === "archived") draw();
    });
  }

  function loadVoices(force) {
    if (voices !== null && !force) { draw(); return; }
    fetch("/api/contacts/unlinked").then(j).then(function (r) {
      if (r.error) { voicesErr = r.error; voices = []; }
      else { voicesErr = null; voices = r.unlinked || []; }
      drawStats();
      if (tab === "voices") draw();
    }).catch(function () {
      voicesErr = "Contacts is not answering."; voices = [];
      if (tab === "voices") draw();
    });
  }

  // ── header counts ──────────────────────────────────────────────────────────
  function chip(n, label, flag) {
    var d = el("div", flag && Number(n) > 0 ? "stat flag" : "stat");
    d.appendChild(el("b", null, String(n == null ? "—" : n)));
    d.appendChild(document.createTextNode(label));
    return d;
  }
  function drawStats() {
    statsEl.innerHTML = "";
    if (listErr) return;
    statsEl.appendChild(chip(stats.total != null ? stats.total : all.length, "people"));
    statsEl.appendChild(chip(stats.verified, "verified"));
    statsEl.appendChild(chip(stats.needs_confirmation, "need confirmation", true));
    statsEl.appendChild(chip(stats.duplicate_groups, "duplicate names", true));
    statsEl.appendChild(chip(stats.with_voiceprint, "voiceprints"));
    if (stats.triage) statsEl.appendChild(chip(stats.triage, "need a real name", true));
    if (stats.archived) statsEl.appendChild(chip(stats.archived, "archived"));
    statsEl.appendChild(chip(voices === null ? null : voices.length,
                             "voices with no contact", true));
    statsEl.appendChild(chip(stats.missing_email, "no email"));
  }

  // ── filters ────────────────────────────────────────────────────────────────
  function fillCustomers() {
    var seen = {}, withNone = false;
    all.forEach(function (c) { if (c.customer) seen[c.customer] = 1; else withNone = true; });
    var names = Object.keys(seen).sort();
    custSel.innerHTML = "";
    custSel.appendChild(new Option("Every account", ""));
    names.forEach(function (n) { custSel.appendChild(new Option(n, n)); });
    if (withNone) custSel.appendChild(new Option("— no account —", "none"));
    custSel.value = fCust;
  }

  function matches(c) {
    if (fCust === "none" ? !!c.customer : (fCust && c.customer !== fCust)) return false;
    if (fStatus && (c.verification_status || "verified") !== fStatus) return false;
    if (!q) return true;
    var hay = [c.name, c.email, c.role, c.company, c.aliases].map(low).join(" · ");
    return hay.indexOf(q) >= 0;
  }

  // ── the list ───────────────────────────────────────────────────────────────
  function setToast(text, undo) { toast = text ? {text: text, undo: undo} : null; }

  function toastRow() {
    var d = el("div", "toast");
    d.appendChild(el("span", null, toast.text));
    if (toast.undo) {
      var u = el("button", null, "Undo");
      u.onclick = function () { var f = toast.undo; setToast(null); f(); };
      d.appendChild(u);
    }
    var x = el("button", null, "Dismiss");
    x.onclick = function () { setToast(null); draw(); };
    d.appendChild(x);
    return d;
  }

  function drawTriage() {
    if (triageErr) { listEl.appendChild(el("div", "err", triageErr)); return; }
    if (triageRows === null) { listEl.appendChild(el("div", "empty", "loading…")); return; }
    countEl.textContent = triageRows.length
      ? triageRows.length + " row" + (triageRows.length === 1 ? "" : "s")
        + " that read as a placeholder rather than a person"
      : "";
    listEl.appendChild(el("div", "note",
      "A worklist, not a verdict. Plenty of these want a surname or an email rather than "
      + "archiving — open one and decide. Role being “TBD” is deliberately NOT a signal "
      + "here: that means the role is unfilled, not that the person is fictional."));
    if (!triageRows.length) {
      listEl.appendChild(el("div", "empty", "Nothing looks like a placeholder."));
      return;
    }
    PG.slice(triageRows).forEach(function (c) {
      var row = personRow(c, false);
      var who = row.querySelector(".who");
      if (who) {
        var why = el("div", "cx", (c.why || []).join("  ·  "));
        who.appendChild(why);
      }
      listEl.appendChild(row);
    });
  }

  function drawArchived() {
    if (archErr) { listEl.appendChild(el("div", "err", archErr)); return; }
    if (archRows === null) { listEl.appendChild(el("div", "empty", "loading…")); return; }
    countEl.textContent = archRows.length
      ? archRows.length + " archived" : "";
    listEl.appendChild(el("div", "note",
      "Hidden from the list, the pickers and the duplicate scan — but still here, and "
      + "still resolving by name, so nobody comes back as a stranger. Open one to restore it."));
    if (!archRows.length) {
      listEl.appendChild(el("div", "empty", "Nothing is archived."));
      return;
    }
    PG.slice(archRows).forEach(function (c) { listEl.appendChild(personRow(c, false)); });
  }

  function draw() {
    listEl.innerHTML = "";
    if (listErr) {
      countEl.textContent = "";
      listEl.appendChild(el("div", "err", listErr));
      return;
    }
    if (toast) listEl.appendChild(toastRow());
    if (tab === "dupes" || tab === "voices") showPagers(false);
    else showPagers(true);
    if (tab === "dupes") return drawDupes();
    if (tab === "voices") return drawVoices();
    if (tab === "triage") return drawTriage();
    if (tab === "archived") return drawArchived();

    var rows = all.filter(function (c) {
      if (tab === "queue" && (c.verification_status || "verified") !== "needs_confirmation")
        return false;
      return matches(c);
    });

    if (tab === "queue") {
      countEl.textContent = rows.length
        ? rows.length + (rows.length === 1 ? " person" : " people") + " to confirm"
          + "  ·  a flag, not a gate — nothing in mt is blocked either way"
        : "";
      if (!rows.length) {
        listEl.appendChild(el("div", "empty", (q || fCust)
          ? "Nothing left to confirm in this filter."
          : "Nothing to confirm — every contact is verified."));
        return;
      }
    } else {
      countEl.textContent = rows.length + (rows.length === 1 ? " person" : " people")
        + (rows.length !== all.length ? " of " + all.length : "");
      if (!rows.length) {
        listEl.appendChild(el("div", "empty", q
          ? "No matches for “" + qBox.value.trim() + "”."
          : "No contacts here."));
        return;
      }
    }
    PG.slice(rows).forEach(function (c) {
      listEl.appendChild(personRow(c, tab === "queue"));
    });
  }

  // Shown when a rename would do more than re-spell a label. Two cases, and the second
  // is the one worth catching: the voiceprint registry is keyed by the SLUG OF THE NAME,
  // so renaming here alone leaves an enrolled voice answering to the old spelling while
  // the contact answers to the new one.
  function renameGuard(imp, fields, proceed) {
    var wrap = el("div", "modal");
    wrap.style.zIndex = "70";
    var sheet = el("div", "sheet");
    var head = el("div", "head");
    head.appendChild(el("b", null, "Before renaming"));
    var x = el("button", null, "Cancel");
    x.onclick = function () { wrap.remove(); };
    head.appendChild(x);
    sheet.appendChild(head);

    var f = el("div", "form");
    f.appendChild(el("div", "note",
      "“" + imp.old + "”  →  “" + imp.new + "”"));

    var opts = { alias: false, vp: false };

    if (imp.collisions && imp.collisions.length) {
      var c0 = imp.collisions[0];
      f.appendChild(el("div", "err",
        "That name already belongs to " + (c0.name || ("#" + c0.id))
        + (c0.customer ? " (" + c0.customer + ")" : "")
        + ". Renaming would create a second row for one person — merging is almost "
        + "certainly what you want, and it keeps both rows' notes and aliases. "
        + "Merge lives on the Duplicate names tab."));
    }

    if (imp.voiceprint && imp.voiceprint.enrolled) {
      f.appendChild(el("div", "err",
        "“" + imp.old + "” has an enrolled voiceprint (" + imp.voiceprint.file + "). "
        + "The registry is keyed by the name, so renaming the contact on its own leaves "
        + "the voice answering to the old spelling — mt would keep writing “" + imp.old
        + "” onto transcripts."));

      var l1 = el("label", "f");
      var cb1 = document.createElement("input");
      cb1.type = "checkbox"; cb1.checked = true;
      l1.appendChild(cb1);
      l1.appendChild(document.createTextNode(" Rename the voiceprint too (runs mt rename)"));
      l1.style.flexDirection = "row";
      l1.style.alignItems = "center";
      l1.style.gap = "8px";
      f.appendChild(l1);
      f.appendChild(el("div", "hint",
        "Moves the label and the .npy; the vector, the sample count and the enrolment "
        + "history all stay. Past transcripts are NOT rewritten — that is a separate, "
        + "louder decision on the Voices page."));
      opts.vpBox = cb1;
    }

    var l2 = el("label", "f");
    var cb2 = document.createElement("input");
    cb2.type = "checkbox";
    cb2.checked = !!(imp.voiceprint && imp.voiceprint.enrolled);
    l2.appendChild(cb2);
    l2.appendChild(document.createTextNode(" Keep “" + imp.old + "” as an alias"));
    l2.style.flexDirection = "row";
    l2.style.alignItems = "center";
    l2.style.gap = "8px";
    f.appendChild(l2);
    f.appendChild(el("div", "hint",
      "Old spellings live on in past transcripts. An alias is what keeps those lookups "
      + "matching this person instead of quietly creating a new row."));

    var foot = el("div", "foot");
    var go = el("button", "primary", "Rename");
    go.onclick = function () {
      if (cb2.checked) {
        var lines = txt(fields.aliases).split(/\n/).map(function (t) { return t.trim(); })
          .filter(function (t) { return t; });
        if (lines.indexOf(imp.old) === -1) lines.push(imp.old);
        fields.aliases = lines.join("\n");
      }
      if (opts.vpBox && opts.vpBox.checked) {
        fields.__renameVoiceprint = true;
        fields.__oldName = imp.old;
      }
      wrap.remove();
      proceed();
    };
    foot.appendChild(go);
    var no = el("button", null, "Cancel");
    no.onclick = function () { wrap.remove(); };
    foot.appendChild(no);
    f.appendChild(foot);

    sheet.appendChild(f);
    wrap.appendChild(sheet);
    wrap.onclick = function (e) { if (e.target === wrap) wrap.remove(); };
    document.body.appendChild(wrap);
  }

  function personRow(c, inQueue) {
    var d = el("div", "person");
    var who = el("div", "who");
    who.appendChild(el("div", "nm", has(c.name) ? c.name : "(no name)"));
    var sub = [];
    if (has(c.role)) sub.push(c.role);
    if (has(c.company)) sub.push(c.company);
    if (sub.length) who.appendChild(el("div", "sub", sub.join("  ·  ")));
    var cx = [];
    if (has(c.email)) cx.push(c.email);
    if (has(c.phone)) cx.push(c.phone);
    if (cx.length) who.appendChild(el("div", "cx", cx.join("  ·  ")));
    d.appendChild(who);

    var right = el("div", "right");
    if (c.customer) right.appendChild(el("span", "tag cust", c.customer));
    if (c.has_voiceprint) right.appendChild(el("span", "tag voice", "voice"));
    if ((c.verification_status || "verified") === "needs_confirmation" && !inQueue)
      right.appendChild(el("span", "tag unconf", "confirm"));
    if (inQueue) {
      var v = el("button", "primary", "Verify");
      v.onclick = function (ev) { ev.stopPropagation(); verify(c, d, v); };
      right.appendChild(v);
    }
    d.appendChild(right);

    d.tabIndex = 0;
    d.setAttribute("role", "button");
    d.onclick = function () { openDetail(c.id); };
    d.onkeydown = function (ev) {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDetail(c.id); }
    };
    return d;
  }

  function verify(c, rowEl, btn) {
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    post("/api/contacts/status", {id: c.id, status: "verified"}).then(function (r) {
      if (!r.ok) {
        if (btn) { btn.disabled = false; btn.textContent = "Verify"; }
        setToast(r.why || "could not save", null);
        draw();
        return;
      }
      apply(r.contact);
      if (rowEl) rowEl.className = "person gone";
      setToast("Marked " + txt(c.name || "#" + c.id) + " verified.", function () {
        post("/api/contacts/status", {id: c.id, status: "needs_confirmation"})
          .then(function (r2) { if (r2.ok) apply(r2.contact); draw(); });
      });
      // Let the row fade before the list reflows under his cursor.
      setTimeout(draw, 260);
    });
  }

  // ── one person ─────────────────────────────────────────────────────────────
  function closeDetail() { var m = document.getElementById("cmodal"); if (m) m.remove(); }
  function closeSummary() { var m = document.getElementById("smodal"); if (m) m.remove(); }

  function openDetail(id) {
    closeDetail();
    var wrap = el("div", "modal"); wrap.id = "cmodal";
    var sheet = el("div", "sheet wide");
    var head = el("div", "head");
    var ttl = el("b", null, id ? "loading…" : "New contact");
    head.appendChild(ttl);
    var guide = el("button", null, guideOn() ? "Hide field guide" : "Field guide");
    guide.onclick = function () {
      var f = sheet.querySelector(".form");
      if (!f) return;
      var on = !f.classList.contains("guide");
      setGuide(on);
      f.classList.toggle("guide", on);
      guide.textContent = on ? "Hide field guide" : "Field guide";
    };
    head.appendChild(guide);
    var x = el("button", null, "Close"); x.onclick = closeDetail;
    head.appendChild(x);
    sheet.appendChild(head);
    var body = el("div");
    sheet.appendChild(body);
    wrap.appendChild(sheet);
    wrap.onclick = function (e) { if (e.target === wrap) closeDetail(); };
    document.body.appendChild(wrap);

    if (!id) return fillDetail(body, ttl, {}, [], null);
    fetch("/api/contacts/one?id=" + encodeURIComponent(id)).then(j).then(function (r) {
      if (r.error || !r.contact) {
        body.innerHTML = "";
        ttl.textContent = "Contact";
        var e = el("div", "err", r.error || "no such contact");
        e.style.margin = "16px 22px 0";
        body.appendChild(e);
        return;
      }
      fillDetail(body, ttl, r.contact, r.seen || [], id);
    }).catch(function () {
      // The card is still worth editing from what the list already knows.
      fillDetail(body, ttl, byId(id) || {}, [], id);
    });
  }

  function tfield(parent, label, key, val, ph) {
    var l = el("label", "f");
    l.appendChild(document.createTextNode(label));
    var i = document.createElement("input");
    i.type = "text";
    i.value = txt(val);                 // a property assignment, never parsed as HTML
    i.setAttribute("data-k", key);
    if (ph) i.placeholder = ph;
    l.appendChild(i);
    hintInto(l, key);
    parent.appendChild(l);
    return i;
  }
  function tarea(parent, label, key, val, cls, hint) {
    var l = el("label", "f");
    l.appendChild(document.createTextNode(label));
    var t = document.createElement("textarea");
    t.className = cls;
    t.value = txt(val);
    t.setAttribute("data-k", key);
    l.appendChild(t);
    hintInto(l, key);
    parent.appendChild(l);
    if (hint) {
      var h = el("div", "note", hint);
      h.style.margin = "5px 0 0";
      parent.appendChild(h);
    }
    return t;
  }
  function grid(parent) { var g = el("div", "grid"); parent.appendChild(g); return g; }

  // The field guide. Same shape as the tracking page: hidden by a class on the form
  // rather than by rebuilding it, so toggling mid-edit never loses what you typed.
  function hintInto(l, key) {
    var t = FIELD_HELP[key];
    if (!t) return l;
    var d = el("div", "hint");
    // FIELD_HELP is our own static text, so `backticks` become <code> and **bold**
    // becomes <strong>. Still built with createElement, never innerHTML.
    t.split(/`([^`]+)`|\*\*([^*]+)\*\*/).forEach(function (part, i) {
      if (part == null || part === "") return;
      if (i % 3 === 1) d.appendChild(el("code", null, part));
      else if (i % 3 === 2) d.appendChild(el("strong", null, part));
      else d.appendChild(document.createTextNode(part));
    });
    l.appendChild(d);
    return l;
  }
  function guideOn() {
    try { return localStorage.getItem("mt.contacts.guide") !== "0"; }
    catch (e) { return true; }
  }
  function setGuide(on) {
    try { localStorage.setItem("mt.contacts.guide", on ? "1" : "0"); } catch (e) {}
  }

  function fillDetail(body, ttl, c, seen, id) {
    body.innerHTML = "";
    ttl.textContent = id ? (has(c.name) ? c.name : "#" + id) : "New contact";

    var form = el("div", "form");
    if (guideOn()) form.classList.add("guide");
    body.appendChild(form);

    var g1 = grid(form);
    tfield(g1, "Name", "name", c.name);
    tfield(g1, "Role", "role", c.role, "Director of IT");
    var g2 = grid(form);
    tfield(g2, "Email", "email", c.email);
    tfield(g2, "Phone", "phone", c.phone);
    var g3 = grid(form);
    tfield(g3, "Company", "company", c.company, "Your company, a reseller, a partner…");

    var l = el("label", "f");
    l.appendChild(document.createTextNode("Account"));
    var sel = document.createElement("select");
    sel.setAttribute("data-k", "customer_id");
    sel.appendChild(new Option("— none —", ""));
    Object.keys(CUSTOMERS).sort(function (a, b) { return Number(a) - Number(b); })
      .forEach(function (k) { sel.appendChild(new Option(CUSTOMERS[k], k)); });
    sel.value = c.customer_id ? String(c.customer_id) : "";
    l.appendChild(sel);
    hintInto(l, "customer_id");
    g3.appendChild(l);

    var g4 = grid(form);
    tfield(g4, "Slack user ID", "slack_user_id", c.slack_user_id, "U01ABCDEF");
    tfield(g4, "Source", "source", c.source, "where this record came from");

    tarea(form, "Also known as", "aliases", c.aliases, "alias");
    var sp = el("div"); sp.style.height = "12px"; form.appendChild(sp);
    tarea(form, "Notes", "notes", c.notes, "notes");

    var g5 = grid(form);
    var l2 = el("label", "f");
    l2.appendChild(document.createTextNode("Verification"));
    var vs = document.createElement("select");
    vs.setAttribute("data-k", "verification_status");
    vs.appendChild(new Option("Verified", "verified"));
    vs.appendChild(new Option("Needs confirmation", "needs_confirmation"));
    vs.value = (c.verification_status === "needs_confirmation") ? "needs_confirmation" : "verified";
    l2.appendChild(vs);
    hintInto(l2, "verification_status");
    g5.appendChild(l2);
    var vp = el("div", "note", c.has_voiceprint
      ? "Voiceprint enrolled — mt can put this name on a track by itself."
      : "No voiceprint yet. One gets enrolled from a reviewed recording.");
    vp.style.alignSelf = "end";
    g5.appendChild(vp);

    var foot = el("div", "foot");
    var msg = el("span", "note", "");
    var save = el("button", "primary", id ? "Save changes" : "Create contact");
    save.onclick = function () {
      var fields = {};
      Array.prototype.forEach.call(form.querySelectorAll("[data-k]"), function (i) {
        fields[i.getAttribute("data-k")] = i.value;
      });
      if (!txt(fields.name).trim()) { msg.textContent = "A name is required."; return; }
      if (id) fields.id = id;
      // A rename is never just a rename when a voice is enrolled or the new name is
      // already taken. Ask the server what it would actually do before writing.
      if (id && txt(fields.name).trim() !== txt(c.name).trim()) {
        save.disabled = true;
        msg.textContent = "checking…";
        fetch("/api/contacts/rename-check?id=" + encodeURIComponent(id)
              + "&name=" + encodeURIComponent(txt(fields.name).trim()))
          .then(j).then(function (imp) {
            save.disabled = false;
            msg.textContent = "";
            if (imp && !imp.error && imp.changed
                && ((imp.collisions && imp.collisions.length)
                    || (imp.voiceprint && imp.voiceprint.enrolled))) {
              return renameGuard(imp, fields, doSave);
            }
            doSave();
          }).catch(function () { save.disabled = false; doSave(); });
        return;
      }
      doSave();

      function doSave() {
      save.disabled = true;
      msg.textContent = "saving…";
      post("/api/contacts/save", fields).then(function (r) {
        save.disabled = false;
        if (!r.ok) { msg.textContent = r.why || "could not save"; return; }
        apply(r.contact);
        closeDetail();
        setToast("Saved " + txt((r.contact && r.contact.name) || fields.name) + ".", null);
        groups = null;          // a rename can create a duplicate, or clear one
        if (fields.__renameVoiceprint) {
          post("/api/voiceprints/rename",
               { old: fields.__oldName, new: txt(fields.name).trim() })
            .then(function (v) {
              setToast(v && v.ok
                ? "Saved, and the voiceprint was renamed with it."
                : "Saved — but the voiceprint rename failed: "
                  + ((v && v.why) || "unknown"), null);
              draw();
            });
        }
        draw();
      });
      }
    };
    var cancel = el("button", null, "Cancel");
    cancel.onclick = closeDetail;
    foot.appendChild(save);
    foot.appendChild(cancel);

    if (id) {
      var isArch = has(c.archived_at);
      var arch = el("button", isArch ? "primary" : "", isArch ? "Restore" : "Forget for now");
      arch.style.marginLeft = "auto";
      arch.onclick = function () {
        if (!isArch && !window.confirm(
              "Archive " + txt(c.name) + "?\n\n"
              + "They stay in the database and their name still resolves, so they will "
              + "not come back as a stranger. They are only hidden from the list, the "
              + "pickers and the duplicate scan. You can restore them any time.")) return;
        arch.disabled = true;
        post("/api/contacts/archive", { id: id, archived: !isArch })
          .then(function (r) {
            arch.disabled = false;
            if (!r.ok) { msg.textContent = r.why || "could not archive"; return; }
            closeDetail();
            setToast(isArch
              ? txt(c.name) + " is back in the list."
              : txt(c.name) + " archived — find them on the Archived tab.", null);
            archRows = null; triageRows = null; groups = null;
            reload(function () { if (tab === "archived") loadArchived(); });
          });
      };
      foot.appendChild(arch);
      if (has(c.archived_reason)) {
        form.appendChild(el("div", "note", "Archived: " + c.archived_reason));
      }
    }

    foot.appendChild(msg);
    form.appendChild(foot);

    if (id) {
      var meta = ["#" + id];
      if (has(c.created_at)) meta.push("added " + c.created_at);
      if (has(c.updated_at)) meta.push("updated " + c.updated_at);
      body.appendChild(el("div", "idline", meta.join("  ·  ")));
      drawSeen(body, seen);
    }
  }

  // ── where I've seen them ───────────────────────────────────────────────────
  function drawSeen(body, seen) {
    var box = el("div", "seen");
    box.appendChild(el("h2", null, "Where I’ve seen them"));
    if (!seen.length) {
      box.appendChild(el("div", "empty",
        "No meeting mentions this name yet. If it gets transcribed a different way, "
        + "adding that spelling as an alias usually finds them."));
      body.appendChild(box);
      return;
    }
    var rows = el("div", "rows");
    seen.forEach(function (h) { rows.appendChild(seenRow(h)); });
    box.appendChild(rows);
    body.appendChild(box);
  }

  var KIND_LABEL = {
    summary: "summary", transcript_segment: "transcript", transcript: "transcript",
    recap: "recap", memory: "memory", note: "note"
  };

  function seenRow(h) {
    var d = el("div", "hit");
    var top = el("div", "top");
    top.appendChild(el("div", "name", h.title || h.path || "(untitled)"));
    top.appendChild(el("span", "tag", KIND_LABEL[h.kind] || h.kind || "doc"));
    if (h.customer) top.appendChild(el("span", "tag cust", h.customer));
    if (h.date) top.appendChild(el("span", "meta", h.date));
    d.appendChild(top);
    if (h.path) d.appendChild(el("div", "meta", h.path));

    // The one innerHTML of live content in this file, and it is deliberate: search.py
    // escapes the source text first and only then wraps matched terms in <mark>, so the
    // string is already HTML-safe. Everything above it is textContent.
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

  // Same rule as search.js: the KIND is what guarantees the destination exists. A
  // transcript segment is a row in transcript.json, which is what /review reads; a
  // summary is a summary.md, which the overlay reads. Anything else has no page in this
  // app and must not look clickable.
  function destination(h) {
    if (!h.session) return null;
    var sid = h.session;
    if (h.kind === "transcript_segment")
      return function () { location.href = "/review?session=" + encodeURIComponent(sid); };
    if (h.kind === "summary" || h.kind === "transcript")
      return function () { showSummary(sid, h.title || sid); };
    return null;
  }

  function showSummary(id, title) {
    closeSummary();
    var wrap = el("div", "modal"); wrap.id = "smodal";
    wrap.style.zIndex = "60";                 // above the contact card it was opened from
    var sheet = el("div", "sheet");
    var head = el("div", "head");
    head.appendChild(el("b", null, title || id));
    var x = el("button", null, "Close"); x.onclick = closeSummary;
    head.appendChild(x);
    sheet.appendChild(head);
    var body = el("div", "md", "loading…");
    sheet.appendChild(body);
    wrap.appendChild(sheet);
    wrap.onclick = function (e) { if (e.target === wrap) closeSummary(); };
    document.body.appendChild(wrap);
    fetch("/api/summary?id=" + encodeURIComponent(id)).then(j).then(function (r) {
      body.innerHTML = "";
      if (!r.ok) {
        var e = el("div", "err", (r.why || "No summary.") + (r.hint ? "\n" + r.hint : ""));
        e.style.whiteSpace = "pre-wrap";
        body.appendChild(e);
        return;
      }
      // mdToHtml escapes first and only then adds tags of its own — the same contract as
      // index.html, which renders the same files through the same function.
      body.innerHTML = mdToHtml(r.markdown);
    }).catch(function () {
      body.textContent = "Could not load that summary.";
    });
  }

  // ── duplicates ─────────────────────────────────────────────────────────────
  // Fields compared side by side. All-empty rows are dropped as noise, except the three
  // that decide whether two records are the same person in the first place.
  var CMP = [
    {k: "customer", label: "Account", always: true},
    {k: "role", label: "Role", always: true},
    {k: "email", label: "Email", always: true},
    {k: "phone", label: "Phone"},
    {k: "company", label: "Company"},
    {k: "slack_user_id", label: "Slack"},
    {k: "source", label: "Source"},
    {k: "aliases", label: "Aliases", long: true},
    {k: "notes", label: "Notes", long: true},
    {k: "verification_status", label: "Status"},
    {k: "created_at", label: "Added"}
  ];
  // What merge() copies across. verification_status, created_at and the ids are not in
  // here because merge fills EMPTY fields only and those are never empty.
  var FILLABLE = [
    {k: "email", label: "email"}, {k: "role", label: "role"},
    {k: "phone", label: "phone"}, {k: "company", label: "company"},
    {k: "customer_id", label: "account"}, {k: "slack_user_id", label: "Slack ID"},
    {k: "source", label: "source"}
  ];

  function completeness(r) {
    var n = 0;
    ["email", "role", "phone", "company", "customer_id", "slack_user_id", "notes", "aliases"]
      .forEach(function (k) { if (has(r[k])) n++; });
    return n;
  }
  // Default keeper: the fullest record, ties broken by the lowest id — the row the rest
  // of the workspace is most likely already pointing at.
  function defaultKeeper(g) {
    var best = g.rows[0];
    g.rows.forEach(function (r) {
      var d = completeness(r) - completeness(best);
      if (d > 0 || (d === 0 && r.id < best.id)) best = r;
    });
    return best.id;
  }

  function drawDupes() {
    countEl.textContent = "";
    if (groupsErr) { listEl.appendChild(el("div", "err", groupsErr)); return; }
    if (groups === null) { listEl.appendChild(el("div", "empty", "loading…")); return; }

    var hidden = groups.filter(groupDismissed);
    var live = showDismissed ? groups
                             : groups.filter(function (g) { return !groupDismissed(g); });
    var open = groups.length - hidden.length;

    countEl.textContent = open + (open === 1 ? " name is shared by more than one row"
                                             : " names are shared by more than one row")
      + (hidden.length ? "  ·  " + hidden.length + " dismissed as different people" : "")
      + "  ·  nothing merges until you click";

    if (!live.length) {
      listEl.appendChild(el("div", "empty", groups.length
        ? "All clear — every duplicate name has been merged or dismissed."
        : "No duplicate names."));
    } else {
      live.forEach(function (g) { listEl.appendChild(dupeCard(g)); });
    }
    if (hidden.length) {
      var t = el("button", null, showDismissed
        ? "Hide dismissed" : "Show " + hidden.length + " dismissed");
      t.style.alignSelf = "flex-start";
      t.onclick = function () { showDismissed = !showDismissed; draw(); };
      listEl.appendChild(t);
    }
  }

  function dupeCard(g) {
    var card = el("div", "dupe");
    var keepId = defaultKeeper(g);
    var hd = el("div", "hd");
    hd.appendChild(el("b", null, g.name));
    hd.appendChild(el("span", "note", g.rows.length + " rows share this name"));
    if (!g.same_account) hd.appendChild(el("span", "tag unconf", "two accounts"));
    if (groupDismissed(g)) hd.appendChild(el("span", "tag", "dismissed"));
    card.appendChild(hd);
    card.appendChild(accountHint(g));

    var below = el("div");
    card.appendChild(below);

    function render() {
      var old = card.querySelector("table");
      if (old) old.remove();
      below.innerHTML = "";

      var t = document.createElement("table");
      t.className = "dcmp";

      var pick = t.insertRow();
      pick.appendChild(el("th", null, ""));
      g.rows.forEach(function (r) {
        var td = el("td", "pick" + (r.id === keepId ? " keepcol" : ""));
        var lab = document.createElement("label");
        var radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "keep-" + g.rows[0].id;
        radio.checked = r.id === keepId;
        radio.onchange = function () { keepId = r.id; render(); };
        lab.appendChild(radio);
        lab.appendChild(document.createTextNode(
          r.id === keepId ? "Keeper · #" + r.id : "keep #" + r.id + " instead"));
        td.appendChild(lab);
        pick.appendChild(td);
      });

      CMP.forEach(function (f) {
        var vals = g.rows.map(function (r) { return txt(r[f.k]); });
        if (!f.always && !vals.some(function (v) { return has(v); })) return;
        var seen = {}, n = 0;
        vals.forEach(function (v) { if (has(v) && !seen[low(v)]) { seen[low(v)] = 1; n++; } });
        var differs = n > 1 || (n === 1 && vals.some(function (v) { return !has(v); }));
        var tr = t.insertRow();
        var th = el("th", differs ? "differs" : null, f.label);
        if (differs) th.title = "these rows disagree";
        tr.appendChild(th);
        g.rows.forEach(function (r, i) {
          var cls = (r.id === keepId ? "keepcol " : "") + (f.long ? "long " : "");
          if (!has(vals[i])) cls += "none";
          var td = el("td", cls.trim() || null);
          td.textContent = has(vals[i]) ? vals[i] : "—";   // raw DB text — textContent
          tr.appendChild(td);
        });
      });

      var vrow = t.insertRow();
      vrow.appendChild(el("th", null, "Voiceprint"));
      g.rows.forEach(function (r) {
        vrow.appendChild(el("td", r.id === keepId ? "keepcol" : null,
                             r.has_voiceprint ? "enrolled" : "—"));
      });

      card.insertBefore(t, below);

      var keep = g.rows.filter(function (r) { return r.id === keepId; })[0];
      g.rows.forEach(function (r) {
        if (r.id !== keepId) below.appendChild(planFor(keep, r));
      });

      var acts = el("div", "acts");
      g.rows.forEach(function (r) {
        if (r.id === keepId) return;
        var label = "Merge #" + r.id + " into #" + keepId;
        // Cross-account pairs are normally ONE person carried once per account, so merging
        // is the destructive choice there — it collapses them onto a single account and
        // loses the other. Style follows the correct action, not the shape of the widget.
        var b = el("button", g.same_account ? "primary" : "danger", label);
        arm(b, label, function () { doMerge(keepId, r.id); });
        acts.appendChild(b);
      });
      // The label has to describe THIS pair. "Different people" is simply untrue of
      // Alex Example on acme and Alex Example on globex — they are one person on two
      // accounts — and that wording led to three wrong merges on 2026-09-17.
      var keepLabel = g.same_account
        ? "Keep both — different people"
        : "Keep both — same person, one row per account";
      var dis = el("button", g.same_account ? "danger" : "primary", groupDismissed(g)
        ? "Bring this back" : keepLabel);
      dis.style.marginLeft = "auto";
      dis.onclick = function () { toggleDismiss(g, dis); };
      acts.appendChild(dis);
      below.appendChild(acts);
    }

    render();
    return card;
  }

  // The single most useful signal on this page. Two rows on DIFFERENT accounts are
  // almost always one person carried once per account on purpose, and merging those
  // destroys the per-account association. Two rows on the SAME account are almost
  // always a real duplicate. One line, stated plainly, not a redesign.
  function accountHint(g) {
    var accts = g.accounts || [];
    var t;
    if (!g.same_account) {
      t = "Different accounts (" + accts.join(" / ") + ") — usually one person "
        + "carried once per account on purpose. Dismissing is normally the right call; "
        + "merging would collapse them onto one account.";
    } else if (!accts.length) {
      t = "Neither row is tied to an account — compare the details below.";
    } else {
      t = "Both rows are on " + accts[0] + " — usually a real duplicate.";
    }
    var d = el("div", "note", t);
    d.style.margin = "4px 0 0";
    return d;
  }

  // Exactly what merge() will do, spelled out from the contract's own rules so he is
  // never guessing. Nothing here touches data — it is a description until a button is
  // clicked twice.
  function planFor(keep, drop) {
    var box = el("div", "plan");
    var head = el("div");
    head.appendChild(el("b", null, "Merging #" + drop.id + " into #" + keep.id + " will:"));
    box.appendChild(head);
    var ul = document.createElement("ul");

    var fills = 0;
    FILLABLE.forEach(function (f) {
      if (has(keep[f.k]) || !has(drop[f.k])) return;
      fills++;
      var li = document.createElement("li");
      li.appendChild(document.createTextNode("fill the empty " + f.label + " with "));
      li.appendChild(el("b", null, f.k === "customer_id"
        ? txt(drop.customer || drop.customer_id) : txt(drop[f.k])));
      ul.appendChild(li);
    });
    if (!fills) ul.appendChild(el("li", null,
      "change none of #" + keep.id + "’s own fields — a merge only fills blanks, "
      + "it never overwrites a value that is already there"));
    if (has(drop.notes)) ul.appendChild(el("li", null,
      "append #" + drop.id + "’s notes under a “--- merged from #" + drop.id
      + " ---” separator"));
    if (has(drop.aliases)) ul.appendChild(el("li", null,
      "add #" + drop.id + "’s aliases to the list"));
    ul.appendChild(el("li", null,
      "leave #" + drop.id + " in the database, tombstoned as merged into #" + keep.id
      + " — nothing is deleted, and the tombstone can be cleared in the data if this "
      + "turns out to be wrong"));
    box.appendChild(ul);
    return box;
  }

  // Two clicks, not a confirm() box: the plan above already says what happens, and a
  // second deliberate click is the difference between reviewing and misclicking.
  function arm(btn, label, fn) {
    var t = null;
    btn.onclick = function () {
      if (btn.getAttribute("data-armed") === "1") {
        clearTimeout(t);
        btn.setAttribute("data-armed", "");
        btn.className = "primary";
        btn.textContent = label;
        fn();
        return;
      }
      btn.setAttribute("data-armed", "1");
      btn.className = "confirming";
      btn.textContent = "Click again to merge";
      t = setTimeout(function () {
        btn.setAttribute("data-armed", "");
        btn.className = "primary";
        btn.textContent = label;
      }, 4000);
    };
  }

  function doMerge(keepId, dropId) {
    post("/api/contacts/merge", {keep_id: keepId, drop_id: dropId}).then(function (r) {
      if (!r.ok) { setToast(r.why || "merge failed", null); draw(); return; }
      setToast("Merged #" + dropId + " into "
        + txt((r.contact && r.contact.name) || "#" + keepId)
        + ". #" + dropId + " is tombstoned, not deleted.", null);
      reload(function () { loadDupes(true); });
    });
  }

  function toggleDismiss(g, btn) {
    var undo = groupDismissed(g);
    var url = undo ? "/api/contacts/undismiss" : "/api/contacts/dismiss";
    if (btn) { btn.disabled = true; btn.textContent = undo ? "restoring…" : "saving…"; }
    // One call per pair, in sequence. A two-row group is one ruling; a three-row group
    // is three, and doing them in order means a half-applied group cannot be created by
    // clicking fast.
    pairsOf(g).reduce(function (chain, pr) {
      return chain.then(function (errs) {
        return post(url, {a_id: pr[0], b_id: pr[1]}).then(function (r) {
          if (!r.ok) errs.push(r.why || "could not save");
          return errs;
        });
      });
    }, Promise.resolve([])).then(function (errs) {
      if (errs.length) { setToast(errs[0], null); draw(); return; }
      setToast(undo
        ? "“" + txt(g.name) + "” is back on the duplicate list."
        : "“" + txt(g.name) + "” recorded as different people — saved to the database, "
          + "so it stays ruled on. Nothing was merged or deleted.", null);
      reload(function () { loadDupes(true); });
    });
  }

  // The worklist itself. Same three choices the review page offers after an enrolment,
  // and for the same reason: a name that does not match a contact is one of three
  // different problems and only the user can say which.
  //
  //   a typo                 -> rename the voiceprint. Aliasing a typo entrenches it.
  //   the contact is wrong   -> fix the contact, alias the old spelling.
  //   nickname / legal name  -> alias. Both spellings are genuinely that person.
  var vpending = null;         // {old,new,list} — sessions a rename did not rewrite

  function drawVoices() {
    countEl.textContent = "";
    if (voicesErr) { listEl.appendChild(el("div", "err", voicesErr)); return; }
    if (voices === null) { listEl.appendChild(el("div", "empty", "loading…")); return; }
    if (vpending) listEl.appendChild(pendingCard());

    countEl.textContent = voices.length
      ? voices.length + (voices.length === 1 ? " enrolled voice has" : " enrolled voices have")
        + " no contact row" + "  ·  nothing is written until you click"
      : "";
    if (!voices.length) {
      listEl.appendChild(el("div", "empty",
        "Every enrolled voiceprint matches a contact. Enrolling a new voice in mt that "
        + "does not will show up here."));
      return;
    }
    voices.forEach(function (v) { listEl.appendChild(voiceCard(v)); });
  }

  function whoLine(c) {
    var bits = [];
    if (has(c.role)) bits.push(txt(c.role));
    if (has(c.company)) bits.push(txt(c.company));
    if (c.customer) bits.push(txt(c.customer));
    return bits.join("  ·  ");
  }

  function voiceCard(v) {
    var card = el("div", "vp");
    var hd = el("div", "hd");
    hd.appendChild(el("b", null, txt(v.name)));            // DB/disk text — textContent
    var meta = [];
    if (v.samples != null) meta.push(v.samples + (v.samples === 1 ? " sample" : " samples"));
    if (has(v.updated)) meta.push("updated " + txt(v.updated).slice(0, 10));
    if (meta.length) hd.appendChild(el("span", "note", meta.join("  ·  ")));
    hd.appendChild(el("span", "tag unconf", "no contact"));
    card.appendChild(hd);

    var close = v.close || [];
    if (close.length) {
      card.appendChild(el("div", "note", close.length === 1
        ? "One contact is close. It is a suggestion, not a match — nothing is assumed."
        : close.length + " contacts are close. All of them are shown rather than one "
          + "being picked."));
      close.forEach(function (h) { card.appendChild(candCard(v, h)); });
    } else {
      card.appendChild(el("div", "note",
        "No contact is close enough to suggest for this spelling."));
    }

    var acts = el("div", "acts");
    var b = el("button", close.length ? null : "primary",
      close.length ? "Different person — add “" + txt(v.name) + "” as a new contact"
                   : "Add “" + txt(v.name) + "” to contacts");
    b.onclick = function () { b.disabled = true; doCreate(v, b); };
    acts.appendChild(b);
    card.appendChild(acts);
    card.appendChild(el("div", "note",
      "A contact added here lands as “needs confirmation”, sourced to the enrolment — "
      + "you confirm it yourself once the spelling is checked."));
    return card;
  }

  function candCard(v, h) {
    var c = h.contact, d = el("div", "cand");
    d.appendChild(el("div", "cn", txt(c.name)));
    if (whoLine(c)) d.appendChild(el("div", "cr", whoLine(c)));
    d.appendChild(el("div", "cs", Number(h.score).toFixed(2) + " — " + txt(h.why)
      + (h.matched && h.matched !== low(c.name) ? "  ·  matched on “" + txt(h.matched) + "”" : "")));
    var acts = el("div", "acts");
    var r = el("button", "primary", "Yes — I misspelled it");
    r.onclick = function () { r.disabled = true; doRename(v, c, r); };
    var a = el("button", null, "Both names are right");
    a.onclick = function () { a.disabled = true; doAlias(v, c, a); };
    acts.appendChild(r); acts.appendChild(a);
    d.appendChild(acts);
    d.appendChild(el("div", "note",
      "“I misspelled it” re-spells the VOICEPRINT to “" + txt(c.name) + "” — the vector, "
      + "its samples and its history all move with it, and the contact is untouched. "
      + "“Both names are right” leaves the voiceprint alone and adds “" + txt(v.name)
      + "” to " + txt(c.name) + "’s spellings."));
    return d;
  }

  function refreshVoices() { voices = null; loadVoices(true); reload(null); }

  function doRename(v, c, btn) {
    post("/api/voiceprints/rename", {old: v.name, new: c.name}).then(function (r) {
      if (btn) btn.disabled = false;
      if (!r.ok) { setToast("Not renamed: " + (r.why || ""), null); draw(); return; }
      vpending = (r.pending_sessions || []).length
        ? {old: v.name, new: c.name, list: r.pending_sessions, detail: (r.detail || [])}
        : null;
      setToast("Voiceprint re-spelled “" + txt(v.name) + "” → “" + txt(c.name)
        + "”. The vector and its sample count moved with it.", null);
      refreshVoices();
    });
  }

  function doAlias(v, c, btn) {
    post("/api/contacts/alias", {id: c.id, alias: v.name}).then(function (r) {
      if (btn) btn.disabled = false;
      if (!r.ok) { setToast("Not saved: " + (r.why || ""), null); draw(); return; }
      var nm = txt((r.contact && r.contact.name) || c.name);
      setToast(r.already
        ? "“" + txt(v.name) + "” was already one of " + nm + "’s spellings."
        : "“" + txt(v.name) + "” added to " + nm + "’s spellings — the next lookup "
          + "will match it.", null);
      if (r.contact) apply(r.contact);
      refreshVoices();
    });
  }

  function doCreate(v, btn) {
    post("/api/contacts/from-enrollment", {name: v.name}).then(function (r) {
      if (btn) btn.disabled = false;
      if (!r.ok) { setToast("Not created: " + (r.why || ""), null); draw(); return; }
      setToast("Added " + txt((r.contact && r.contact.name) || v.name)
        + " as “needs confirmation”. Confirm them once you have checked the spelling.",
        null);
      refreshVoices();
    });
  }

  // Renaming the print moved a label. Rewriting sessions rewrites the transcripts the user
  // quotes to customers, so it stays a separate, deliberate second press — the same
  // blast-radius reveal `mt rename` does on the command line.
  function pendingCard() {
    var p = vpending, card = el("div", "vp");
    var hd = el("div", "hd");
    hd.appendChild(el("b", null, p.list.length + " transcript"
      + (p.list.length === 1 ? "" : "s") + " still say “" + txt(p.old) + "”"));
    hd.appendChild(el("span", "tag unconf", "all meetings"));
    card.appendChild(hd);
    card.appendChild(el("div", "note", "The voiceprint is renamed to “" + txt(p.new)
      + "”. These sessions were not touched — rewriting them re-renders transcript.md, "
      + "which is the file you quote."));
    p.list.forEach(function (x) { card.appendChild(el("div", "cs", txt(x))); });
    if (p.detail && p.detail.length)
      card.appendChild(el("pre", "detail", p.detail.join("\n")));
    var acts = el("div", "acts");
    var go = el("button", "danger", "Rewrite and re-render them too");
    go.onclick = function () {
      go.disabled = true;
      post("/api/voiceprints/rename", {old: p.old, new: p.new, sessions: true})
        .then(function (r) {
          vpending = null;
          setToast(r.ok ? (p.list.length + " session(s) rewritten.")
                        : ("Sessions not rewritten: " + (r.why || "")), null);
          refreshVoices();
        });
    };
    var no = el("button", null, "Leave the transcripts alone");
    no.onclick = function () { vpending = null; draw(); };
    acts.appendChild(go); acts.appendChild(no);
    card.appendChild(acts);
    return card;
  }

  // ── wiring ─────────────────────────────────────────────────────────────────
  tabsEl.addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-tab]");
    if (!b) return;
    tab = b.getAttribute("data-tab") || "all";
    PG.reset();                 // a different list — start it at page 1
    Array.prototype.forEach.call(tabsEl.querySelectorAll("button"), function (x) {
      x.className = (x === b) ? "on" : "";
    });
    var dupes = tab === "dupes" || tab === "voices" || tab === "triage";
    qBox.hidden = dupes;
    custSel.hidden = dupes;
    // The queue IS the status filter, so the control goes away — and its value goes with
    // it. Leaving "verified" set behind an invisible select is how a worklist shows up
    // empty for no visible reason.
    statSel.hidden = dupes || tab === "queue";
    if (statSel.hidden) { fStatus = ""; statSel.value = ""; }
    newBtn.hidden = dupes;
    setToast(null);
    if (tab === "dupes") loadDupes(false);
    else if (tab === "voices") loadVoices(false);
    else if (tab === "triage") loadTriage();
    else if (tab === "archived") loadArchived();
    else draw();
  });

  qBox.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      q = qBox.value.trim().toLowerCase(); PG.reset(); draw();
    }, DEBOUNCE);
  });
  custSel.addEventListener("change", function () { fCust = custSel.value; PG.reset(); draw(); });
  statSel.addEventListener("change", function () { fStatus = statSel.value; PG.reset(); draw(); });
  newBtn.addEventListener("click", function () { openDetail(null); });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (document.getElementById("smodal")) closeSummary();
    else closeDetail();
  });

  reload(null);
  loadVoices(false);
})();

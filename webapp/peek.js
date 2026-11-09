// peek.js — read an item without leaving the page you're on.
//
// The review pages (owners, titles, Hygiene) used to send "#764" to the Tracking window,
// which pulled you off the card you were deciding. mtPeek(id) opens a read-only sheet over
// the current page instead: title, account, status, dates, owner, project/parent, the
// description log and sub-items. The page underneath is never re-rendered, so whatever you
// had picked is still picked when you close it. "Open in Tracking ↗" is there for when you
// do want to edit — it opens a NEW tab, never this one.
//
// mtPeekLink(a, id) wires an existing link: a plain click peeks; ⌘/Ctrl/middle-click keep
// the browser's own "open in new tab".
(function () {
"use strict";

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function get(p) { return fetch(p).then(function (r) { return r.json(); }).catch(function () { return {}; }); }

var PROJECTS = null;   // id → name, fetched once per page
function projects() {
  if (PROJECTS) return Promise.resolve(PROJECTS);
  return get("/api/tracking/meta").then(function (m) {
    PROJECTS = {};
    (m.projects || []).forEach(function (p) { PROJECTS[p.id] = p.name || p.title || ("project #" + p.id); });
    return PROJECTS;
  });
}

var OPEN = null;   // only one peek at a time; opening another replaces it

function peek(id) {
  if (OPEN) OPEN.close();
  var wrap = el("div", "modal peek");
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, "#" + id));
  var full = el("a", "tap", "Open in Tracking ↗");
  full.href = "/tracking?open=" + id;
  full.target = "_blank";            // a new tab — this page stays where it is
  full.rel = "noopener";
  full.title = "Edit it there — opens a new tab, this page stays put";
  var x = el("button", "tap", "Close");
  var right = el("span", "peek-btns");   // one right-aligned group, whatever .head does to buttons
  right.appendChild(full);
  right.appendChild(x);
  head.appendChild(right);
  sheet.appendChild(head);
  var body = el("div", "peek-body");
  body.appendChild(el("div", "hint", "loading…"));
  sheet.appendChild(body);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);

  var prevFocus = document.activeElement;
  // Capture phase, so the page's own shortcuts don't fire while you're reading.
  function key(e) {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    e.stopPropagation();
  }
  function close() {
    wrap.remove();
    document.removeEventListener("keydown", key, true);
    OPEN = null;
    try { if (prevFocus && prevFocus.focus) prevFocus.focus(); } catch (e) {}
  }
  document.addEventListener("keydown", key, true);
  wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
  x.addEventListener("click", close);
  x.focus();
  OPEN = { close: close };

  Promise.all([get("/api/tracking/item?id=" + id), projects()]).then(function (r) {
    var it = r[0].item;
    body.innerHTML = "";
    if (!it) { body.appendChild(el("div", "err", r[0].error || "no such item")); return; }
    var extra = [];
    if (it.parent_id) extra.push(get("/api/tracking/item?id=" + it.parent_id));
    else extra.push(Promise.resolve(null));
    if (it.child_count) extra.push(get("/api/tracking/children?id=" + it.id));
    else extra.push(Promise.resolve(null));
    if (it.owner_contact_id) extra.push(get("/api/contacts/one?id=" + it.owner_contact_id));
    else extra.push(Promise.resolve(null));
    Promise.all(extra).then(function (x2) { draw(it, x2[0] && x2[0].item, x2[1] && x2[1].items, x2[2] && x2[2].contact); });
  });

  function draw(it, parent, kids, ownerC) {
    body.appendChild(el("div", "peek-title", it.title || (it.description || "").split(/(?<=\.)\s/)[0].slice(0, 140)));
    var tags = el("div", "peek-tags");
    if (it.customer) tags.appendChild(el("span", "tag cust", it.customer));
    tags.appendChild(el("span", "tag", it.status || "—"));
    if (it.item_type) tags.appendChild(el("span", "tag", it.item_type));
    if (it.priority) tags.appendChild(el("span", "tag", it.priority));
    body.appendChild(tags);

    var facts = el("dl", "peek-facts");
    function fact(k, v) {
      if (v == null || v === "") return;
      facts.appendChild(el("dt", null, k));
      var dd = el("dd");
      if (typeof v === "string") dd.textContent = v; else dd.appendChild(v);
      facts.appendChild(dd);
    }
    var own = it.owner || "";
    if (ownerC) own = ownerC.name + (ownerC.customer ? " · " + ownerC.customer : "")
      + (it.owner && it.owner !== ownerC.name ? "  (written as “" + it.owner + "”)" : "");
    fact("Owner", own || "—");
    if ((it.with || []).length) fact("With", it.with.map(function (p) { return p.name; }).join(", "));
    var dates = [];
    if (it.due_date) dates.push("due " + it.due_date);
    if (it.start_date) dates.push("start " + it.start_date);
    if (it.end_date) dates.push("end " + it.end_date);
    if (it.completed_at) dates.push("done " + String(it.completed_at).slice(0, 10));
    fact("Dates", dates.join(" · ") || "none set");
    if (it.project_id) fact("Project", PROJECTS[it.project_id] || "#" + it.project_id);
    if (parent) {
      var pa = el("a", null, "#" + parent.id + " " + (parent.title || ""));
      pa.href = "#";
      pa.addEventListener("click", function (e) { e.preventDefault(); peek(parent.id); });
      fact("Under", pa);
    }
    fact("Source", it.source);
    fact("Updated", (it.updated_at || "").slice(0, 16) + (it.updated_by ? " by " + it.updated_by : ""));
    body.appendChild(facts);

    // Description = a running log of paragraphs; the "[date — who]" lead of each is bolded.
    var log = el("div", "peek-desc");
    String(it.description || "").split(/\n\s*\n/).forEach(function (para) {
      if (!para.trim()) return;
      var p = el("p");
      var m = /^(\[[^\]]{4,80}\])\s*/.exec(para);
      if (m) { p.appendChild(el("b", null, m[1])); para = " " + para.slice(m[0].length); }
      p.appendChild(document.createTextNode(para));
      log.appendChild(p);
    });
    if (!log.childNodes.length) log.appendChild(el("p", "hint", "No description."));
    body.appendChild(el("div", "peek-h", "Description"));
    body.appendChild(log);

    if ((kids || []).length) {
      body.appendChild(el("div", "peek-h", "Sub-items (" + kids.length + ")"));
      var kl = el("div", "peek-kids");
      kids.forEach(function (k) {
        var a = el("a", "peek-kid");
        a.href = "#";
        a.appendChild(el("span", "gid", "#" + k.id));
        a.appendChild(el("span", "own-t", k.title || (k.description || "").slice(0, 100)));
        a.appendChild(el("span", "tag", k.status || ""));
        a.addEventListener("click", function (e) { e.preventDefault(); peek(k.id); });
        kl.appendChild(a);
      });
      body.appendChild(kl);
    }
  }
}

function link(a, id) {
  a.addEventListener("click", function (e) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;   // keep new-tab clicks
    e.preventDefault();
    e.stopPropagation();
    peek(id);
  });
  a.target = "_blank";   // if it does navigate (⌘-click), never in this tab
  a.title = a.title || "Click to read it here · ⌘-click opens it in Tracking";
}

window.mtPeek = peek;
window.mtPeekLink = link;
})();

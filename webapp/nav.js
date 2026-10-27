// nav.js — the shared header on every page: title + version, the date and Hygiene on the
// same line, and one menu row underneath that is identical everywhere (same links, same
// order, the current page highlighted), so nothing moves as you go from page to page.
// Clicking the version opens the About panel (/api/version). Also fills the recording-
// policy line under the Record button when a policy link is configured.
//
// Seeing the Hygiene count is the point (the owner, Sep 28 2026: "Having a way for me to
// see the hygiene may actually get me to do something"). One cheap GET per page load; a
// failure just leaves the link without a number.
(function () {
"use strict";
var header = document.querySelector("header");
if (!header) return;

var MENU = [["/", "Meetings"], ["/tracking", "Tracking"], ["/weekly", "Weekly"],
            ["/files", "Files"], ["/contacts", "Contacts"], ["/help", "Help"]];
var path = location.pathname;
var wantAbout = location.hash === "#about";   // read now: a page script may rewrite the hash
var wantFeedback = location.hash === "#feedback";
function current(href) {
  if (href === "/") return path === "/" || path === "/archive" || path === "/review";
  return path === href || path.indexOf(href + "/") === 0;
}
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

var css = el("style");
css.textContent =
  "header.hdr{display:block;padding:18px 0 0;border-bottom:1px solid var(--line)}" +
  ".hdr-top{display:flex;align-items:flex-start;gap:14px}" +
  ".hdr-brand{display:flex;flex-direction:column;line-height:1.15}" +
  ".hdr-ver{align-self:flex-start;margin-top:3px;padding:0;border:0;background:none;" +
    "font:12px var(--m);color:var(--ink-3);cursor:pointer}" +
  ".hdr-ver:hover{color:var(--accent);text-decoration:underline}" +
  ".hdr-right{margin-left:auto;display:flex;align-items:baseline;gap:14px;padding-top:4px}" +
  ".hdr-right .day{margin-left:0}" +
  ".hdr-menu{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}" +
  ".hdr-menu a{font-size:13px;color:var(--ink-2);text-decoration:none;padding:0 0 8px;" +
    "border-bottom:2px solid transparent}" +
  ".hdr-menu a:hover{color:var(--accent)}" +
  ".hdr-menu a.on{color:var(--accent);font-weight:600;border-bottom-color:var(--accent)}" +
  "dialog.about{border:1px solid var(--line);border-radius:10px;background:var(--surface);" +
    "color:var(--ink);padding:18px 20px;max-width:520px;width:calc(100% - 32px)}" +
  "dialog.about::backdrop{background:rgba(0,0,0,.3)}" +
  "dialog.about h3{margin:0 0 4px;font-size:16px}" +
  "dialog.about table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:13px;" +
    "margin-top:10px}" +
  "dialog.about td{padding:3px 8px 3px 0;vertical-align:top;overflow-wrap:anywhere}" +
  "dialog.about td:first-child{color:var(--ink-3);width:58%}" +
  "dialog.about code{font:12px var(--m)}" +
  "dialog.fb{max-width:600px}" +
  ".fb-kind{display:flex;gap:8px;margin:12px 0 10px}" +
  ".fb-kind button{padding:5px 14px;border-radius:999px}" +
  ".fb-kind button.on{background:var(--accent);border-color:var(--accent);color:#fff}" +
  "dialog.fb label{display:block;font-size:12px;color:var(--ink-3);margin:10px 0 4px}" +
  "dialog.fb input[type=text],dialog.fb textarea{width:100%;box-sizing:border-box;" +
    "font:14px var(--f);padding:8px 10px;border:1px solid var(--line-2);border-radius:7px;" +
    "background:var(--surface);color:var(--ink)}" +
  "dialog.fb textarea{min-height:130px;resize:vertical;line-height:1.5}" +
  "dialog.fb pre{font:12px var(--m);color:var(--ink-3);background:var(--surface-2);" +
    "padding:8px 10px;border-radius:7px;margin:6px 0 0;white-space:pre-wrap}" +
  ".fb-note{font-size:12px;color:var(--ink-3);margin-top:10px;line-height:1.5}" +
  ".fb-warn{margin-top:10px;padding:8px 10px;border-radius:7px;font-size:13px;" +
    "border:1px solid var(--warn);color:var(--ink)}" +
  ".fb-warn code{font:12px var(--m)}" +
  ".fb-foot{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px}" +
  ".fb-foot .grow{margin-right:auto;font-size:12px;color:var(--ink-3)}";
document.head.appendChild(css);

// ── header: [title / version] .......... [date] [Hygiene]   then the menu row
var h1 = header.querySelector("h1");
var day = header.querySelector(".day");
Array.prototype.slice.call(header.querySelectorAll("a.nav")).forEach(function (a) {
  a.remove();
});
header.classList.add("hdr");
var top = el("div", "hdr-top");
var brand = el("div", "hdr-brand");
if (h1) brand.appendChild(h1);
var ver = el("button", "hdr-ver", "version …");
ver.type = "button";
ver.title = "About this install";
brand.appendChild(ver);
top.appendChild(brand);
var right = el("div", "hdr-right");
if (day) right.appendChild(day);
var hy = el("a", "nav" + (current("/hygiene") ? " on" : ""), "Hygiene");
hy.href = "/hygiene";
right.appendChild(hy);
var fbLink = el("a", "nav", "Send feedback");
fbLink.href = "#feedback";
fbLink.title = "Report a problem or suggest an idea";
fbLink.addEventListener("click", function (e) { e.preventDefault(); feedback(); });
right.appendChild(fbLink);
top.appendChild(right);
var menu = el("nav", "hdr-menu");
MENU.forEach(function (m) {
  var a = el("a", current(m[0]) ? "on" : "", m[1]);
  a.href = m[0];
  menu.appendChild(a);
});
header.innerHTML = "";
header.appendChild(top);
header.appendChild(menu);

// ── version + About panel + the recording-policy link
var info = null;
function row(t, k, v) {
  var tr = el("tr");
  tr.appendChild(el("td", null, k));
  var td = el("td");
  if (v && v.nodeType) td.appendChild(v); else td.textContent = v == null ? "—" : String(v);
  tr.appendChild(td);
  t.appendChild(tr);
}
function about() {
  var d = el("dialog", "about");
  d.appendChild(el("h3", null, "mt " + (info ? info.version : "")));
  d.appendChild(el("div", "note", "One version for the whole tool. What's inside this install, " +
    "for bug reports:"));
  var t = el("table");
  if (info) {
    row(t, "Release", info.git);
    row(t, "Database schema", info.schema == null ? null : "v" + info.schema);
    row(t, "Python", info.python);
    Object.keys(info.packages || {}).forEach(function (k) { row(t, k, info.packages[k]); });
    (info.binaries || []).forEach(function (b) {
      row(t, b.name, b.built ? "built " + b.built : "not built");
    });
    var seen = {};
    (info.models || []).forEach(function (m) {
      var name = m.name.split("/")[0];          // a model folder's name says which model
      if (seen[name]) return;
      seen[name] = true;
      row(t, name, el("code", null, m.sha256 + (m.present ? "" : "  (missing)")));
    });
  } else {
    row(t, "Details", "unavailable — if you just updated, restart mt web (Ctrl-C, then " +
      "mt web): pages reload on their own, the server does not");
  }
  d.appendChild(t);
  var close = el("button", null, "Close");
  close.type = "button";
  close.style.marginTop = "14px";
  close.addEventListener("click", function () { d.close(); });
  d.appendChild(close);
  d.addEventListener("close", function () { d.remove(); });
  document.body.appendChild(d);
  d.showModal();
}
ver.addEventListener("click", about);

// ── Send feedback: a Problem or an Idea, with what is installed, as a GitHub issue
// (opened pre-filled for the person to submit) or as text to paste wherever they talk to
// whoever runs their install. Nothing is sent from here: GitHub's own page does that.
// Before either, the server reads the text for names and other private details from
// this user's own DB and config and lists them; the person edits, or sends anyway.
var KINDS = {
  problem: { label: "Problem", gh: "bug", title: "What went wrong, in one line",
             body: "What you did, what happened, and what you expected instead." },
  idea: { label: "Idea", gh: "enhancement", title: "The idea, in one line",
          body: "What you would like mt to do, and what it would save you." }
};
function diag(kind) {
  var v = info || {};
  var bits = ["mt " + (v.version || "?")];
  if (v.git) bits.push("release " + v.git);
  if (v.macos) bits.push("macOS " + v.macos + (v.arch ? " (" + v.arch + ")" : ""));
  if (v.python) bits.push("Python " + v.python);
  bits.push("page " + path);
  return KINDS[kind].label + " · " + bits.join(" · ");
}
function feedback() {
  var kind = "problem";
  var acked = "";                                  // the text the person said "send anyway" to
  var d = el("dialog", "about fb");
  d.appendChild(el("h3", null, "Send feedback"));
  var kinds = el("div", "fb-kind");
  var kb = {};
  Object.keys(KINDS).forEach(function (k) {
    var b = el("button", null, KINDS[k].label);
    b.type = "button";
    b.addEventListener("click", function () { setKind(k); });
    kb[k] = b;
    kinds.appendChild(b);
  });
  d.appendChild(kinds);
  d.appendChild(el("label", null, "Title"));
  var title = el("input");
  title.type = "text";
  title.maxLength = 120;
  d.appendChild(title);
  d.appendChild(el("label", null, "Details"));
  var body = el("textarea");
  d.appendChild(body);
  var incl = el("label");
  var cb = el("input");
  cb.type = "checkbox";
  cb.checked = true;
  incl.appendChild(cb);
  incl.appendChild(document.createTextNode(" Include version details"));
  d.appendChild(incl);
  var pre = el("pre");
  d.appendChild(pre);
  d.appendChild(el("div", "fb-note", "Leave out meeting content and the names of customers " +
    "and people. mt checks for the ones it knows before anything leaves this page."));
  var warn = el("div", "fb-warn");
  warn.hidden = true;
  d.appendChild(warn);
  var foot = el("div", "fb-foot");
  var status = el("span", "grow", "");
  foot.appendChild(status);
  var repo = info && info.feedback_repo;
  var gh = el("button", "primary", "Open on GitHub");
  gh.type = "button";
  gh.title = repo ? "Opens a new issue on github.com/" + repo + ", filled in, for you to submit"
                  : "No feedback_repo in config.json — use Copy";
  gh.disabled = !repo;
  var cp = el("button", repo ? null : "primary", "Copy");
  cp.type = "button";
  cp.title = "Copy the report, to paste into Slack, email or a chat";
  var cancel = el("button", null, "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", function () { d.close(); });
  foot.appendChild(cancel);
  foot.appendChild(cp);
  foot.appendChild(gh);
  d.appendChild(foot);

  function setKind(k) {
    kind = k;
    Object.keys(kb).forEach(function (x) { kb[x].classList.toggle("on", x === k); });
    title.placeholder = KINDS[k].title;
    body.placeholder = KINDS[k].body;
    pre.textContent = diag(k);
  }
  function text() {
    return body.value.trim() + (cb.checked ? "\n\n---\n" + diag(kind) : "");
  }
  function sig() { return title.value + "\n" + text(); }
  function reset() {
    if (acked && acked !== sig()) acked = "";
    warn.hidden = true;
    gh.textContent = "Open on GitHub";
    cp.textContent = "Copy";
    status.textContent = "";
  }
  [title, body].forEach(function (x) { x.addEventListener("input", reset); });
  cb.addEventListener("change", function () { pre.hidden = !cb.checked; reset(); });

  // Run the check; call go() when it is clean or the person already saw these hits.
  function checked(go, btn) {
    if (!title.value.trim()) { status.textContent = "Add a title first."; title.focus(); return; }
    if (acked && acked === sig()) { go(); return; }
    fetch("/api/feedback/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: title.value + "\n" + body.value })
    }).then(function (r) { return r.json(); }).then(function (r) {
      var hits = (r && r.hits) || [];
      if (!hits.length) { go(); return; }
      warn.innerHTML = "";
      warn.appendChild(document.createTextNode("This looks like it names something private: "));
      hits.forEach(function (h, i) {
        if (i) warn.appendChild(document.createTextNode(", "));
        warn.appendChild(el("code", null, h.text));
        warn.appendChild(document.createTextNode(" (" + h.why + ")"));
      });
      warn.appendChild(document.createTextNode(". Edit it out, or press the button again " +
        "to send it as it is."));
      warn.hidden = false;
      acked = sig();
      btn.textContent = btn === gh ? "Open on GitHub anyway" : "Copy anyway";
    }).catch(function () {
      status.textContent = "Could not check the text — restart mt web and try again.";
    });
  }
  gh.addEventListener("click", function () {
    checked(function () {
      var u = "https://github.com/" + repo + "/issues/new?title="
        + encodeURIComponent(title.value.trim()) + "&body=" + encodeURIComponent(text())
        + "&labels=" + KINDS[kind].gh;
      window.open(u, "_blank", "noopener");
      status.textContent = "Opened on GitHub — submit it there.";
    }, gh);
  });
  cp.addEventListener("click", function () {
    checked(function () {
      var s = KINDS[kind].label + ": " + title.value.trim() + "\n\n" + text();
      var done = function () { status.textContent = "Copied."; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(s).then(done, function () { fallbackCopy(s); done(); });
      } else { fallbackCopy(s); done(); }
    }, cp);
  });
  function fallbackCopy(s) {
    var t = el("textarea");
    t.value = s;
    d.appendChild(t);
    t.select();
    try { document.execCommand("copy"); } catch (e) { /* nothing more to try */ }
    t.remove();
  }

  setKind(kind);
  d.addEventListener("close", function () { d.remove(); });
  document.body.appendChild(d);
  d.showModal();
  title.focus();
}
fetch("/api/version").then(function (r) { return r.json(); }).then(function (v) {
  info = v;
  ver.textContent = "v" + v.version;
  var pn = document.getElementById("policynote");
  if (pn && v.policy_url) {
    var link = el("a", null, "your company's recording policy");
    link.href = v.policy_url;
    link.target = "_blank";
    link.rel = "noopener";
    pn.textContent = "Recording? Understand ";
    pn.appendChild(link);
    pn.appendChild(document.createTextNode("."));
  }
  if (wantAbout) about();                         // /#about links straight to the panel
  if (wantFeedback) feedback();
}).catch(function () {
  ver.textContent = "version ?";
  ver.title = "The server is older than this page — restart mt web";
});

// ── Hygiene open-count badge
fetch("/api/hygiene/counts").then(function (r) { return r.json(); }).then(function (c) {
  var d = c.data || 0, m = c.memory;
  var total = d + (m || 0);
  var nd = document.getElementById("nData");
  var nm = document.getElementById("nMem");
  if (nd) nd.textContent = d ? String(d) : "";
  if (nm) nm.textContent = m ? String(m) : "";
  if (!total) return;
  var b = el("span", "navbadge", m == null ? String(d) : d + " · " + m);
  b.title = "Open: Data " + d + (m == null ? "" : " · Assistant memory " + m);
  hy.appendChild(b);
}).catch(function () {});
})();

// files.js — browse and edit the workspace markdown, and the assistant's memory files.
//
// The editor is a full-width Edit/Preview TOGGLE, not a split pane.
// Preview runs the same mdToHtml() the summary modal uses, so what you see here is what
// every other surface will render.
//
// Three safety behaviours worth knowing, all enforced server-side in docs.py and merely
// surfaced here:
//   * the file's mtime is captured on open and sent back on save. If the file changed
//     underneath you — the assistant writes these too — the save is refused, not merged.
//   * every save keeps the previous bytes in a vault OUTSIDE the workspace, because
//     it is not version controlled and Drive syncs it in place.
//   * transcript.md and live.md cannot be opened at all.
//
// Memory files (phase 4) carry YAML front matter. The textarea holds the BODY only; the
// front matter is shown as fields, and a save sends the body plus the two fields you may
// edit (type, description). docs.py rebuilds the front matter from the bytes on disk, so
// nothing on this page can reformat, reorder or drop a front-matter line.
(function () {
"use strict";

var $ = function (s) { return document.querySelector(s); };
var api = function (p, opts) {
  return fetch(p, opts).then(function (r) { return r.json(); });
};
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

var FILES = [], KINDS = [], CUSTS = [];
var NAMES = null;                  // memory [[wiki-link]] name -> path, loaded on demand

var ROOT = "workspace";
try { if (localStorage.getItem("mt.files.root") === "memory") ROOT = "memory"; } catch (e) {}

// Paged in the BROWSER. The whole list is ~93 KB and already here; what costs is drawing
// 350 rows. Filters narrow FILES on the server, the pager slices what gets drawn.
var PG = null;
var seq = 0;
var sheetEl = null;

// ------------------------------------------------------------------ boot

function boot() {
  PG = window.mtPager("files", render);
  var list = $("#list");
  list.parentNode.insertBefore(PG.mount(), list);
  list.parentNode.insertBefore(PG.mount(), list.nextSibling);
  wire();
  setRoot(ROOT, true);
}

function setRoot(root, first) {
  ROOT = root === "memory" ? "memory" : "workspace";
  try { localStorage.setItem("mt.files.root", ROOT); } catch (e) {}
  Array.prototype.forEach.call($("#roots").children, function (b) {
    b.className = b.getAttribute("data-root") === ROOT ? "on" : "";
  });
  // Kinds differ per root and accounts only mean something in the workspace.
  KINDS = [];
  $("#fkind").innerHTML = '<option value="">Any kind</option>';
  $("#fcust").innerHTML = '<option value="">Every account</option>';
  $("#fcust").hidden = ROOT === "memory";
  $("#fq").placeholder = ROOT === "memory"
    ? "Search name, path or description…" : "Search path or filename…";
  if (!first) $("#fq").value = "";
  load();
}

function wire() {
  var t = null;
  $("#fq").addEventListener("input", function () {
    clearTimeout(t);
    t = setTimeout(load, 220);
  });
  $("#fkind").addEventListener("change", load);
  $("#fcust").addEventListener("change", load);
  Array.prototype.forEach.call($("#roots").children, function (b) {
    b.addEventListener("click", function () {
      if (b.getAttribute("data-root") !== ROOT) setRoot(b.getAttribute("data-root"));
    });
  });
}

function load() {
  var mine = ++seq;
  var p = new URLSearchParams();
  p.set("root", ROOT);
  var q = $("#fq").value.trim();
  if (q) p.set("q", q);
  if ($("#fkind").value) p.set("kind", $("#fkind").value);
  if (ROOT === "workspace" && $("#fcust").value) p.set("customer", $("#fcust").value);
  api("/api/files?" + p.toString()).then(function (r) {
    if (mine !== seq) return;
    if (r.error) { fail(r.error); return; }
    if (r.off) {                     // no "memory_dir" in config.json
      $('#roots [data-root="memory"]').hidden = true;
      setRoot("workspace");
      return;
    }
    FILES = r.files || [];
    if (r.names) NAMES = r.names;
    if (!KINDS.length) { KINDS = r.kinds || []; fillPickers(); }
    PG.reset();                       // new filter, back to page 1
    render();
  }).catch(function (e) { fail(String(e)); });
}

function fail(msg) {
  var box = $("#list");
  box.innerHTML = "";
  box.appendChild(el("div", "err", msg));
}

function fillPickers() {
  KINDS.forEach(function (k) {
    var o = el("option", null, k);
    o.value = k;
    $("#fkind").appendChild(o);
  });
  var seen = {};
  FILES.forEach(function (f) { if (f.customer) seen[f.customer] = 1; });
  Object.keys(seen).sort().forEach(function (c) {
    var o = el("option", null, c);
    o.value = c;
    $("#fcust").appendChild(o);
  });
}

// ------------------------------------------------------------------ list

function render() {
  var box = $("#list");
  box.innerHTML = "";
  $("#count").textContent = FILES.length
    ? FILES.length + " file" + (FILES.length === 1 ? "" : "s") : "";
  drawStats();
  var page = PG.slice(FILES);
  if (!FILES.length) {
    box.appendChild(el("div", "empty", "nothing matches those filters"));
    return;
  }
  page.forEach(function (f) { box.appendChild(rowFor(f)); });
}

function drawStats() {
  var box = $("#stats");
  box.innerHTML = "";
  var by = {};
  FILES.forEach(function (f) { by[f.kind] = (by[f.kind] || 0) + 1; });
  Object.keys(by).sort(function (a, b) { return by[b] - by[a]; }).forEach(function (k) {
    var d = el("div", "stat");
    d.appendChild(el("b", null, String(by[k])));
    d.appendChild(document.createTextNode(k));
    box.appendChild(d);
  });
}

function kb(n) {
  return n < 1024 ? n + " B" : Math.round(n / 1024) + " KB";
}

function rowFor(f) {
  var row = el("div", "row");
  row.appendChild(el("div", "when", f.date || ""));
  var mid = el("div", "mid");
  mid.appendChild(el("div", "name", f.root === "memory" && f.slug ? f.slug : f.name));
  mid.appendChild(el("div", "sub", f.root === "memory" && f.description
    ? f.description : f.path));
  row.appendChild(mid);
  var right = el("div", "right");
  if (f.customer) right.appendChild(el("span", "tag cust", f.customer));
  right.appendChild(el("span", "tag", f.kind));
  right.appendChild(el("span", "tag", kb(f.bytes)));
  row.appendChild(right);
  row.addEventListener("click", function () { openFile(f); });
  return row;
}

// ------------------------------------------------------------------ editor

function closeSheet() {
  if (sheetEl) sheetEl.remove();
  sheetEl = null;
  document.removeEventListener("keydown", onEsc);
}
function onEsc(e) { if (e.key === "Escape") maybeClose(); }

var DIRTY = false;
function maybeClose() {
  if (DIRTY && !window.confirm("You have unsaved changes. Close anyway?")) return;
  DIRTY = false;
  closeSheet();
}

function openFile(f) {
  closeSheet();
  var modal = el("div", "modal");
  var sheet = el("div", "sheet wide edit");
  var head = el("div", "head");
  var ttl = el("b", null, f.name);
  head.appendChild(ttl);

  var tabEdit = el("button", "on", "Edit");
  var tabPrev = el("button", null, "Preview");
  head.appendChild(tabEdit);
  head.appendChild(tabPrev);
  var hist = el("button", null, "History");
  head.appendChild(hist);
  var x = el("button", null, "Close");
  x.addEventListener("click", maybeClose);
  head.appendChild(x);
  sheet.appendChild(head);

  var body = el("div", "edbody");
  var mem = f.root === "memory";
  var fm = mem ? frontPanel() : null;
  if (fm) body.appendChild(fm.box);
  var ta = el("textarea", "editor");
  ta.spellcheck = false;
  var prev = el("div", "md");
  prev.hidden = true;
  body.appendChild(ta);
  body.appendChild(prev);
  sheet.appendChild(body);

  var foot = el("div", "foot");
  var pathline = el("span", "idline grow", f.path);
  foot.appendChild(pathline);
  var msg = el("span", "note", "");
  foot.appendChild(msg);
  var revert = el("button", null, "Revert");
  var save = el("button", "primary", "Save");
  foot.appendChild(revert);
  foot.appendChild(save);
  sheet.appendChild(foot);

  modal.appendChild(sheet);
  modal.addEventListener("click", function (e) { if (e.target === modal) maybeClose(); });
  document.body.appendChild(modal);
  sheetEl = modal;
  document.addEventListener("keydown", onEsc);

  var original = "", mtime = null;
  DIRTY = false;
  function recompute() {
    DIRTY = ta.value !== original || (fm ? fm.changed() : false);
    msg.textContent = DIRTY ? "unsaved" : "";
  }
  if (fm) fm.onChange(recompute);

  function setTab(edit) {
    ta.hidden = !edit;
    prev.hidden = edit;
    tabEdit.className = edit ? "on" : "";
    tabPrev.className = edit ? "" : "on";
    if (!edit) {
      prev.innerHTML = mdToHtml(ta.value);             // our own renderer, escapes first
      if (mem) wireWikiLinks(prev);
    }
  }
  tabEdit.addEventListener("click", function () { setTab(true); });
  tabPrev.addEventListener("click", function () { setTab(false); });

  ta.addEventListener("input", recompute);

  revert.addEventListener("click", function () {
    if (!DIRTY) return;
    if (!window.confirm("Throw away your unsaved changes?")) return;
    ta.value = original;
    if (fm) fm.reset();
    DIRTY = false;
    msg.textContent = "";
    if (prev.hidden === false) setTab(false);
  });

  save.addEventListener("click", function () {
    if (!DIRTY) { msg.textContent = "nothing changed"; return; }
    save.disabled = true;
    msg.textContent = "saving…";
    // Memory: body + explicit field edits only. The front matter itself never leaves disk.
    var payload = mem
      ? { root: f.root, path: f.path, body: ta.value, fields: fm.edits(), mtime: mtime }
      : { root: f.root, path: f.path, text: ta.value, mtime: mtime };
    api("/api/file/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      save.disabled = false;
      if (!r.ok) {
        msg.textContent = "";
        var e = el("div", "err", r.why || "could not save");
        if (r.stale) {
          var re = el("button", "tap", "Reload from disk");
          re.addEventListener("click", function () { closeSheet(); openFile(f); });
          e.appendChild(document.createTextNode("  "));
          e.appendChild(re);
        }
        body.insertBefore(e, body.firstChild);
        return;
      }
      original = ta.value;
      mtime = r.mtime;
      if (fm) refetchFront();
      DIRTY = false;
      msg.textContent = "saved";
      setTimeout(function () { if (msg.textContent === "saved") msg.textContent = ""; }, 2500);
      load();
    }).catch(function (e) {
      save.disabled = false;
      msg.textContent = String(e);
    });
  });

  hist.addEventListener("click", function () { showHistory(f, ta, function (t) {
    ta.value = t; recompute();
  }); });

  function fileUrl() {
    return "/api/file?root=" + encodeURIComponent(f.root)
      + "&path=" + encodeURIComponent(f.path);
  }
  // After a memory save the raw front matter shown in the panel is refreshed from disk,
  // so what you see is what was written — including a field edit's new quoting.
  function refetchFront() {
    api(fileUrl()).then(function (r) { if (r.ok) fm.fill(r); });
  }

  ta.value = "loading…";
  api("/api/file?root=" + encodeURIComponent(f.root)
      + "&path=" + encodeURIComponent(f.path)).then(function (r) {
    if (!r.ok) {
      ta.value = "";
      body.insertBefore(el("div", "err", r.error || "could not open"), body.firstChild);
      return;
    }
    original = mem ? (r.body || "") : r.text;
    mtime = r.mtime;
    ta.value = original;
    if (fm) fm.fill(r);
    ttl.textContent = mem && r.fields && r.fields.name ? r.fields.name : f.name;
    pathline.textContent = r.path + "  ·  " + kb(r.bytes);
  });
}

// The memory front-matter panel. Only `type` and `description` are editable: `name` is
// what [[wiki-links]] point at, so renaming it here would silently break every link to
// the file; the rest (originSessionId, modified, node_type) is bookkeeping the memory
// system owns. All of it is shown verbatim underneath, read-only.
function frontPanel() {
  var box = el("div", "form fm");
  var warn = el("div", "warnbox");
  warn.hidden = true;
  box.appendChild(warn);
  var grid = el("div", "fmgrid");
  box.appendChild(grid);

  var name = el("div", "fmname");
  var typ = el("select");
  // Two rows so a long hook is readable; it is still ONE line in the file — Enter and
  // pasted line breaks fold to spaces as you type.
  var desc = el("textarea", "fmdesc");
  desc.rows = 2;
  desc.spellcheck = true;
  desc.addEventListener("input", function () {
    if (/[\r\n]/.test(desc.value)) desc.value = desc.value.replace(/\s*[\r\n]+\s*/g, " ");
  });
  function field(label, ctl, hint) {
    var l = el("label");
    l.appendChild(el("span", "fmlab", label));
    l.appendChild(ctl);
    if (hint) l.appendChild(el("div", "hint", hint));
    grid.appendChild(l);
  }
  field("name", name, null);
  field("type", typ, null);
  field("description", desc, null);
  var guide = el("div", "hint fmguide",
    "name is read-only: it is what [[links]] in other memory files point at. description "
    + "is how recall decides whether this memory is relevant, so keep it a one-line hook. "
    + "Everything else in the front matter is kept byte-for-byte.");
  box.appendChild(guide);
  var raw = el("details", "fmraw");
  raw.appendChild(el("summary", null, "Front matter as stored (read-only)"));
  var pre = el("pre");
  raw.appendChild(pre);
  box.appendChild(raw);

  var orig = { type: "", description: "" };
  var cb = function () {};
  typ.addEventListener("change", function () { cb(); });
  desc.addEventListener("input", function () { cb(); });
  box.classList.add("guide");         // hints on: this panel is small and new

  return {
    box: box,
    onChange: function (f) { cb = f; },
    fill: function (r) {
      var fl = r.fields || {};
      var has = !!r.front;
      grid.hidden = !has;
      guide.hidden = !has;
      raw.hidden = !has;
      warn.hidden = !(r.index || !has);
      warn.textContent = r.index
        ? "This is the memory INDEX. One line per memory, in the form "
          + "\u201c- [Title](file.md) \u2014 hook\u201d: a hook, not a summary. It is loaded "
          + "into every session, so anything long here costs every conversation."
        : "This file has no front matter, so there are no fields to edit.";
      name.textContent = fl.name || "";
      typ.innerHTML = "";
      var types = (r.types || []).slice();
      if (fl.type && types.indexOf(fl.type) < 0) types.unshift(fl.type);
      types.forEach(function (t) {
        var o = el("option", null, t);
        o.value = t;
        typ.appendChild(o);
      });
      typ.value = fl.type || "";
      desc.value = fl.description || "";
      orig = { type: typ.value, description: desc.value };
      pre.textContent = r.front || "";
    },
    changed: function () {
      return typ.value !== orig.type || desc.value.trim() !== orig.description.trim();
    },
    edits: function () {
      var e = {};
      if (grid.hidden) return e;
      if (typ.value !== orig.type) e.type = typ.value;
      if (desc.value.trim() !== orig.description.trim()) e.description = desc.value.trim();
      return e;
    },
    reset: function () { typ.value = orig.type; desc.value = orig.description; }
  };
}

// [[wiki-links]] in a memory preview. md.js renders them inert; here each one is
// resolved against the memory names and either opens its target or is marked broken.
function wireWikiLinks(scope) {
  var links = scope.querySelectorAll("a.wl");
  if (!links.length) return;
  var go = function () {
    Array.prototype.forEach.call(links, function (a) {
      var target = NAMES && NAMES[a.getAttribute("data-wl")];
      if (!target) {
        a.classList.add("missing");
        a.title = "no memory file is named " + a.getAttribute("data-wl");
        a.addEventListener("click", function (e) { e.preventDefault(); });
        return;
      }
      a.title = target;
      a.addEventListener("click", function (e) {
        e.preventDefault();
        if (DIRTY && !window.confirm("You have unsaved changes. Leave this file?")) return;
        DIRTY = false;
        openFile({ root: "memory", path: target, name: target.split("/").pop() });
      });
    });
  };
  if (NAMES) { go(); return; }
  api("/api/files?root=memory&kind=index").then(function (r) {
    NAMES = r.names || {};
    go();
  }).catch(go);
}

// A prior version is offered as text to paste into the editor, not applied behind your
// back — restoring is then just a normal save, with its own version kept.
function showHistory(f, ta, apply) {
  var wrap = el("div", "modal");
  wrap.style.zIndex = "80";
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, "Earlier versions"));
  var x = el("button", null, "Close");
  x.addEventListener("click", function () { wrap.remove(); });
  head.appendChild(x);
  sheet.appendChild(head);
  var box = el("div", "form");
  box.appendChild(el("div", "hint",
    "Kept outside the workspace, so Google Drive never sees them and nothing here can "
    + "touch a file under cc. The last 20 saves of this file."
    + (f.root === "memory" ? " For a memory file this loads the BODY only; the front "
      + "matter stays as it is now." : "")));
  sheet.appendChild(box);
  wrap.appendChild(sheet);
  wrap.addEventListener("click", function (e) { if (e.target === wrap) wrap.remove(); });
  document.body.appendChild(wrap);

  api("/api/file/versions?root=" + encodeURIComponent(f.root)
      + "&path=" + encodeURIComponent(f.path)).then(function (r) {
    if (r.error) { box.appendChild(el("div", "err", r.error)); return; }
    var vs = r.versions || [];
    if (!vs.length) {
      box.appendChild(el("div", "empty", "No earlier versions yet — none have been saved "
        + "from here."));
      return;
    }
    vs.forEach(function (v) {
      var row = el("div", "row");
      row.appendChild(el("div", "when", v.stamp.slice(0, 8) + " " + v.stamp.slice(9, 15)));
      var mid = el("div", "mid");
      mid.appendChild(el("div", "sub", kb(v.bytes)));
      row.appendChild(mid);
      var right = el("div", "right");
      var use = el("button", "tap", "Load into editor");
      use.addEventListener("click", function () {
        api("/api/file/version?root=" + encodeURIComponent(f.root)
            + "&path=" + encodeURIComponent(f.path)
            + "&stamp=" + encodeURIComponent(v.stamp)).then(function (o) {
          if (!o.ok) return;
          apply(f.root === "memory" ? (o.body != null ? o.body : o.text) : o.text);
          wrap.remove();
        });
      });
      right.appendChild(use);
      row.appendChild(right);
      box.appendChild(row);
    });
  });
}

window.mtOpenFile = openFile;
// tracking.js loads this file only to borrow the editor. There is no #list there, so the
// browse page is only booted where it exists.
if (document.getElementById("list") && document.getElementById("fq")) boot();
})();

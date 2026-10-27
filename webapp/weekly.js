// weekly.js — the weekly-update board and editor (R2, phase 5).
//
// The board: one card per account for the chosen week, showing where each update is —
// no draft / draft / handed off / changed since hand-off / posted — and its RAG next to
// last week's, so a colour change is visible before it goes out.
//
// The editor is the same Edit/Preview TOGGLE as /files, with two
// more tabs showing EXACTLY what would be posted: the Jira comment and the Slack message,
// stripped the way weekly.py strips them. The page never strips anything itself — every
// preview is the server's answer, so what you see here is what the assistant (or, in phase 6,
// the tool) will post.
//
// RAG is two selects built from jira_fields.json. Saving with both set rewrites the RAG
// line in the file on the server and hands the rewritten text back, so the textarea
// always matches the disk.
(function () {
"use strict";

var $ = function (s) { return document.querySelector(s); };
// Never fails silently. A non-JSON answer comes back as {ok:false, why, error}, which every
// caller already prints. The likely one is a 404 from an mt web started before this page
// was updated: the page is read fresh from disk, the server's routes are not (Sep 24 2026 —
// "Send back" did nothing because the running server predated it).
var api = function (p, opts) {
  return fetch(p, opts).then(function (r) {
    return r.text().then(function (t) {
      try { return JSON.parse(t); } catch (e) {
        var why = r.status === 404
          ? "The running mt web is older than this page, so it does not know this action "
            + "yet. Restart mt web, then try again."
          : "mt web answered HTTP " + r.status + " instead of data.";
        return { ok: false, why: why, error: why };
      }
    });
  }, function () {
    var why = "Cannot reach mt web — is it still running?";
    return { ok: false, why: why, error: why };
  });
};
var post = function (p, body) {
  return api(p, { method: "POST", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(body || {}) });
};
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

var STATE_LABEL = {
  none: "No draft", draft: "Draft", handed_off: "Handed off",
  changed: "Changed since hand-off", posted: "Posted", error: "Error",
  sent_back: "Sent back to your assistant", revised: "Revised — review again",
  partial: "Posting stopped part-way"
};

var WEEK = null;                    // any date inside the week on screen
try { WEEK = sessionStorage.getItem("mt.weekly.week"); } catch (e) {}
var BOARD = null;
var sheetEl = null;
var DIRTY = false;
var CREDS = null;                   // /api/weekly/post-status — never a secret

// ------------------------------------------------------------------ dates

function iso(d) {
  return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-"
    + ("0" + d.getDate()).slice(-2);
}
function parseIso(s) {
  var p = String(s || "").split("-");
  return new Date(+p[0], +p[1] - 1, +p[2]);
}
function shift(s, days) {
  var d = parseIso(s);
  d.setDate(d.getDate() + days);
  return iso(d);
}
// DB times are UTC ("YYYY-MM-DD HH:MM:SS", SQLite's datetime('now')); hand-off times
// are ISO with an offset. Both are shown in local time.
function localTime(s) {
  if (!s) return "";
  var t = String(s);
  var d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(t) ? t : t.replace(" ", "T") + "Z");
  if (isNaN(d.getTime())) return t.slice(0, 16);
  return iso(d) + " " + ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
}
function nice(s, withYear) {
  var d = parseIso(s);
  var m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
           "Dec"][d.getMonth()];
  return m + " " + d.getDate() + (withYear ? " " + d.getFullYear() : "");
}

// ------------------------------------------------------------------ board

function boot() {
  $("#wprev").addEventListener("click", function () { go(shift(BOARD.week_of, -7)); });
  $("#wnext").addEventListener("click", function () { go(shift(BOARD.week_of, 7)); });
  $("#wnow").addEventListener("click", function () { go(""); });
  $("#wresync").addEventListener("click", resync);
  loadCreds();
  load();
}

function loadCreds() {
  return api("/api/weekly/post-status").then(function (r) {
    CREDS = r || null;
    var b = $("#wresync");
    var ok = CREDS && CREDS.jira && CREDS.jira.ok;
    b.disabled = !ok;
    b.title = ok ? "Re-read both RAG option lists from Jira"
                 : (CREDS && CREDS.jira && CREDS.jira.why) || "";
  });
}

// Resync only reads Jira and rewrites jira_fields.json, so it needs no confirm.
function resync() {
  var b = $("#wresync");
  b.disabled = true;
  var out = $("#wfail");
  post("/api/weekly/resync", {}).then(function (r) {
    b.disabled = false;
    out.innerHTML = "";
    if (!r.ok) { out.appendChild(el("div", "err", "Not resynced. " + (r.why || ""))); return; }
    var box = el("div", "okbox", "Jira options resynced — "
      + (r.changes && r.changes.length ? r.changes.length + " change"
         + (r.changes.length === 1 ? "" : "s") + ":" : "no changes."));
    (r.changes || []).forEach(function (c) { box.appendChild(el("div", "hint", c)); });
    load().then(function () { out.appendChild(box); });   // load() clears #wfail
  });
}

function go(week) {
  WEEK = week || null;
  try {
    if (WEEK) sessionStorage.setItem("mt.weekly.week", WEEK);
    else sessionStorage.removeItem("mt.weekly.week");
  } catch (e) {}
  load();
}

function load() {
  var p = api("/api/weekly/board" + (WEEK ? "?week=" + encodeURIComponent(WEEK) : ""))
    .then(function (r) {
      var fail = $("#wfail");
      fail.innerHTML = "";
      if (r.error) { fail.appendChild(el("div", "err", r.error)); return; }
      BOARD = r;
      if (r.fields_error) fail.appendChild(el("div", "warnbox", r.fields_error));
      render();
    }).catch(function (e) {
      $("#wfail").innerHTML = "";
      $("#wfail").appendChild(el("div", "err", String(e)));
    });
  api("/api/weekly/jira-fields").then(function (f) {
    $("#wcache").textContent = f.error ? "" : "Jira options cached "
      + (f.resynced_at || "?");
  });
  return p;
}

function ragChip(status, code, cls) {
  var c = el("span", "rag " + (status || "none") + (cls ? " " + cls : ""));
  c.appendChild(el("i"));
  c.appendChild(document.createTextNode(status ? status + (code ? " · " + code : "")
                                               : "RAG not set"));
  return c;
}

function render() {
  var b = BOARD;
  $("#wlabel").textContent = "Week of " + nice(b.week_of, true);
  $("#wrange").textContent = "  " + nice(b.monday) + " – " + nice(b.sunday)
    + (b.week_of === b.today_week ? "  · this week" : "");
  $("#wnow").disabled = b.week_of === b.today_week;
  var box = $("#board");
  box.innerHTML = "";
  if (!b.cards.length) {
    box.appendChild(el("div", "empty", "no active accounts"));
    return;
  }
  b.cards.forEach(function (c) { box.appendChild(cardFor(c)); });
}

function cardFor(c) {
  var card = el("div", "card wcard st-" + c.state);
  var top = el("div", "wtop");
  top.appendChild(el("b", null, c.name));
  top.appendChild(el("span", "tag wstate", STATE_LABEL[c.state] || c.state));
  if (c.test) top.appendChild(el("span", "tag wtest", "TEST"));
  card.appendChild(top);
  if (c.state === "error") {
    card.appendChild(el("div", "err", c.error || "could not read this account"));
    return card;
  }
  var rag = el("div", "wrag");
  rag.appendChild(ragChip(c.rag_status, c.rag_reason_code));
  var last = c.last || {};
  if (last.rag_status) {
    var changed = last.rag_status !== c.rag_status
      || last.rag_reason_code !== c.rag_reason_code;
    if (c.rag_status && changed) {
      rag.appendChild(el("span", "was chg", "was " + last.rag_status
        + (last.rag_reason_code ? " · " + last.rag_reason_code : "")));
    } else if (!c.rag_status) {
      rag.appendChild(el("span", "was", "last week " + last.rag_status
        + (last.rag_reason_code ? " · " + last.rag_reason_code : "")));
    }
  }
  card.appendChild(rag);

  var sub = el("div", "sub");
  if (c.path) sub.textContent = c.path.split("/").pop();
  else sub.textContent = c.jira + " · nothing written for this week yet";
  card.appendChild(sub);
  if (c.row) {
    var r = c.row;
    var line = "DB #" + r.id + " · posted " + localTime(r.posted_at)
      + (r.posted_by ? " by " + r.posted_by : "")
      + (r.edited_at ? " · corrected " + localTime(r.edited_at) : "");
    card.appendChild(el("div", "sub", line));
  }
  if (c.state !== "posted" && c.errors) {
    card.appendChild(el("div", "wflag", c.errors + " thing" + (c.errors === 1 ? "" : "s")
      + " to fix before it can be handed off"));
  }
  if (c.not_posted) card.appendChild(el("div", "wflag", "filename says NOT-POSTED"));

  var acts = el("div", "wacts");
  if (c.path) {
    var open = el("button", "primary", "Open");
    open.addEventListener("click", function () { openUpdate(c.path); });
    acts.appendChild(open);
  } else {
    var start = el("button", "primary", "Start draft");
    start.addEventListener("click", function () { startDraft(c, start); });
    acts.appendChild(start);
    if (last.path) {
      var lw = el("button", null, "Last week's");
      lw.addEventListener("click", function () { openUpdate(last.path); });
      acts.appendChild(lw);
    }
  }
  card.appendChild(acts);
  return card;
}

function startDraft(c, btn) {
  btn.disabled = true;
  post("/api/weekly/new", { customer: c.customer, week: BOARD.week_of }).then(function (r) {
    btn.disabled = false;
    if (!r.ok) {
      if (r.path) { openUpdate(r.path); return; }
      window.alert(r.why || "could not start a draft");
      return;
    }
    load();
    openUpdate(r.path);
  });
}

// ------------------------------------------------------------------ editor

function closeSheet() {
  if (sheetEl) sheetEl.remove();
  sheetEl = null;
  document.removeEventListener("keydown", onKey);
}
function maybeClose() {
  if (DIRTY && !window.confirm("You have unsaved changes. Close anyway?")) return;
  DIRTY = false;
  closeSheet();
  load();
}
var onKey = function () {};

function openUpdate(path) {
  closeSheet();
  var modal = el("div", "modal");
  var sheet = el("div", "sheet wide edit wkly");
  var head = el("div", "head");
  var ttl = el("b", null, path.split("/").pop());
  head.appendChild(ttl);
  var tabs = {};
  [["edit", "Edit"], ["prev", "Preview"], ["jira", "Jira post"], ["slack", "Slack post"]]
    .forEach(function (t) {
      var b = el("button", t[0] === "edit" ? "on" : null, t[1]);
      b.addEventListener("click", function () { setTab(t[0]); });
      tabs[t[0]] = b;
      head.appendChild(b);
    });
  var hist = el("button", "wright", "History");
  head.appendChild(hist);
  var x = el("button", null, "Close");
  x.addEventListener("click", maybeClose);
  head.appendChild(x);
  sheet.appendChild(head);

  var body = el("div", "edbody");
  var ragbox = el("div", "form fm guide wragbar");
  var status = el("select");
  var reason = el("select");
  var hint = el("div", "hint");
  var lastline = el("div", "hint");
  function field(label, ctl) {
    var l = el("label");
    l.appendChild(el("span", "fmlab", label));
    l.appendChild(ctl);
    ragbox.appendChild(l);
  }
  field("RAG status", status);
  field("Reason code", reason);
  ragbox.appendChild(hint);
  ragbox.appendChild(lastline);
  body.appendChild(ragbox);
  var checksBox = el("div", "wchecks");
  body.appendChild(checksBox);
  var errBox = el("div");
  body.appendChild(errBox);
  var handBox = el("div");            // the paste line; survives the reload that follows
  body.appendChild(handBox);
  var ta = el("textarea", "editor");
  ta.spellcheck = true;
  var prev = el("div", "md");
  var jiraView = el("div", "wpost");
  var slackView = el("div", "wpost");
  [prev, jiraView, slackView].forEach(function (v) { v.hidden = true; });
  body.appendChild(ta);
  body.appendChild(prev);
  body.appendChild(jiraView);
  body.appendChild(slackView);
  var posted = el("div", "wposted");
  body.appendChild(posted);
  sheet.appendChild(body);

  var foot = el("div", "foot");
  var pathline = el("span", "idline grow", path);
  foot.appendChild(pathline);
  var msg = el("span", "note", "");
  foot.appendChild(msg);
  var revert = el("button", null, "Revert");
  var save = el("button", "primary", "Save");
  var back = el("button", null, "Send back to your assistant…");
  var hand = el("button", null, "Hand off to your assistant…");
  var rec = el("button", null, "Record as posted…");
  var direct = el("button", "danger", "Post to Jira + Slack…");
  [revert, save, back, hand, rec, direct].forEach(function (b) { foot.appendChild(b); });
  sheet.appendChild(foot);

  modal.appendChild(sheet);
  modal.addEventListener("click", function (e) { if (e.target === modal) maybeClose(); });
  document.body.appendChild(modal);
  sheetEl = modal;
  onKey = function (e) {
    if (document.querySelector(".modal.dlg")) return;   // a dialog owns the keys
    if (e.key === "Escape") maybeClose();
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save.click(); }
  };
  document.addEventListener("keydown", onKey);

  var D = null;                     // the /api/weekly/one answer
  var LASTWEEK = null;              // last week's RAG, for the hand-off confirm
  var original = "", mtime = null, origRag = { s: "", c: "" };
  var LAST = { bodies: null, checks: [] };
  var tab = "edit";
  var pvTimer = null, pvSeq = 0;

  // ---- RAG selects
  function fillStatus(cur) {
    status.innerHTML = "";
    var o = el("option", null, "— choose —");
    o.value = "";
    status.appendChild(o);
    (D.rag || []).forEach(function (s) {
      var opt = el("option", null, (D.emoji[s] || "") + " " + s);
      opt.value = s;
      status.appendChild(opt);
    });
    status.value = cur || "";
  }
  // Reason codes are filtered by the pairing rule weekly.py enforces: GREEN goes with
  // Green and only Green. The server refuses a bad pair anyway; this just stops the page
  // from offering one.
  function fillReason(cur) {
    reason.innerHTML = "";
    var o = el("option", null, "— choose —");
    o.value = "";
    reason.appendChild(o);
    var st = status.value;
    (D.fields.reasons || []).forEach(function (r) {
      if (st === "Green" && !r.green) return;
      if (st && st !== "Green" && r.green) return;
      var opt = el("option", null, r.value);
      opt.value = r.value;
      reason.appendChild(opt);
    });
    // Green has exactly one valid code. Leaving Green, fall back to the last real reason
    // chosen on this file, so Amber -> Green -> Amber does not lose it.
    if (st === "Green") cur = "GREEN";
    else if (cur === "GREEN" || !cur) cur = lastReal;
    reason.value = cur || "";
    if (reason.value !== (cur || "")) reason.value = "";
    showHint();
  }
  function showHint() {
    var r = (D.fields.reasons || []).filter(function (x) {
      return x.value === reason.value;
    })[0];
    hint.textContent = r && r.help ? r.help : "";
  }
  var lastReal = "";
  status.addEventListener("change", function () { fillReason(reason.value); recompute(); });
  reason.addEventListener("change", function () {
    if (reason.value && reason.value !== "GREEN") lastReal = reason.value;
    showHint();
    recompute();
  });

  function ragChanged() {
    return status.value !== origRag.s || reason.value !== origRag.c;
  }
  function recompute() {
    DIRTY = ta.value !== original || ragChanged();
    if (DIRTY) handBox.innerHTML = "";
    msg.textContent = DIRTY ? "unsaved" : "";
    hand.disabled = DIRTY;
    rec.disabled = DIRTY;
    back.disabled = DIRTY;
    hand.title = rec.title = back.title = DIRTY
      ? "Save first — this reads the file on disk" : "";
    var dwhy = directWhy();
    direct.disabled = DIRTY || !!dwhy;
    direct.title = DIRTY ? "Save first — this reads the file on disk" : (dwhy || "");
    schedulePreview();
  }
  ta.addEventListener("input", recompute);

  // ---- the server's view of the unsaved text
  function schedulePreview() {
    clearTimeout(pvTimer);
    pvTimer = setTimeout(runPreview, 500);
  }
  function runPreview() {
    var mine = ++pvSeq;
    post("/api/weekly/preview", { path: path, text: ta.value }).then(function (r) {
      if (mine !== pvSeq || !r.ok) return;
      LAST = r;
      drawChecks(r.checks || []);
      if (tab === "jira" || tab === "slack") drawPost(tab);
    });
  }
  function drawChecks(list) {
    checksBox.innerHTML = "";
    // A RAG choice not yet saved is not in the text, so the text's RAG checks are moot
    // until Save writes it in.
    var pending = ragChanged() && status.value && reason.value;
    list.forEach(function (c) {
      if (pending && /RAG|reason code/.test(c.msg)) return;
      checksBox.appendChild(el("div", "wchk " + c.level, c.msg));
    });
    if (pending) {
      checksBox.appendChild(el("div", "wchk info", "RAG " + status.value + " · "
        + reason.value + " goes into the file when you save"));
    }
  }
  function drawPost(which) {
    var v = which === "jira" ? jiraView : slackView;
    var b = LAST.bodies || { jira: "", slack: "", jira_chars: 0, slack_chars: 0 };
    var txt = which === "jira" ? b.jira : b.slack;
    var n = which === "jira" ? b.jira_chars : b.slack_chars;
    v.innerHTML = "";
    var bar = el("div", "wpostbar");
    var where = which === "jira"
      ? "Comment on " + ((D.info && D.info.jira_key) || "the account epic")
      : "Message to " + (((D.info && D.info.slack_channel_name) || "") + " "
        + ((D.info && D.info.slack_channel) || "")).trim();
    bar.appendChild(el("b", null, where));
    var cnt = el("span", "tag" + (which === "slack" && n > 5000 ? " unconf" : ""),
                 n.toLocaleString() + " chars" + (which === "slack" ? " / 5,000" : ""));
    bar.appendChild(cnt);
    var raw = el("button", "tap", "Show raw");
    bar.appendChild(raw);
    v.appendChild(bar);
    v.appendChild(el("div", "hint", "Exactly what gets posted: the Note block and the "
      + "mentions footer are stripped. Rendered with the same renderer as Preview."));
    var md = el("div", "md");
    md.innerHTML = txt ? mdToHtml(txt) : "";
    if (!txt) md.appendChild(el("div", "empty", "nothing — this section is missing"));
    var pre = el("pre", "wraw");
    pre.textContent = txt;
    pre.hidden = true;
    raw.addEventListener("click", function () {
      pre.hidden = !pre.hidden;
      md.hidden = !pre.hidden;
      raw.textContent = pre.hidden ? "Show raw" : "Show rendered";
    });
    v.appendChild(md);
    v.appendChild(pre);
  }
  function setTab(t) {
    tab = t;
    Object.keys(tabs).forEach(function (k) { tabs[k].className = k === t ? "on" : ""; });
    ta.hidden = t !== "edit";
    prev.hidden = t !== "prev";
    jiraView.hidden = t !== "jira";
    slackView.hidden = t !== "slack";
    if (t === "prev") prev.innerHTML = mdToHtml(ta.value);
    if (t === "jira" || t === "slack") { drawPost(t); runPreview(); }
  }

  // ---- posted state
  function drawPosted() {
    posted.innerHTML = "";
    var r = D.row;
    var h = D.handoff;
    if (h && h.status === "revise") {
      var sb = el("div", "warnbox", "Sent back to your assistant " + localTime(h.requested_at)
        + " — waiting for its revision. Nothing will be posted from this.");
      sb.appendChild(el("pre", "wline", h.notes || ""));
      posted.appendChild(sb);
    }
    if (h && h.status === "revised") {
      var rv = el("div", h.file_changed === false ? "warnbox" : "okbox",
        "Your assistant revised this " + localTime(h.revised_at)
        + " — review it, then hand off."
        + (h.file_changed === false ? " (The file did not change.)" : ""));
      if (h.revised_summary) {
        rv.appendChild(el("div", "hint", "Assistant: " + h.revised_summary));
      }
      var yn = el("details");
      yn.appendChild(el("summary", null, "Your notes"));
      yn.appendChild(el("pre", "wline", h.notes || ""));
      rv.appendChild(yn);
      posted.appendChild(rv);
    }
    if (h && h.status === "posting") {
      var d = h.direct || {};
      var pb = el("div", "warnbox", d.jira_comment_id
        ? "Posting from mt stopped part-way: Jira comment " + d.jira_comment_id
          + (d.fields_set ? ", RAG fields set" : ", RAG fields NOT set")
          + (d.slack_ts ? ", Slack posted" : ", Slack NOT posted")
          + ", DB row not written. Finish posting picks up where it stopped — "
          + "nothing is posted twice."
        : "The last direct post got no answer from Jira, so the comment may or may not "
          + "be there. Look at the issue first — Finish posting lists any comment "
          + "already on it this week.");
      var fin = el("button", "tap", "Finish posting…");
      fin.addEventListener("click", function () { direct.click(); });
      pb.appendChild(el("div"));
      pb.appendChild(fin);
      posted.appendChild(pb);
    }
    if (h && h.status === "ready") {
      posted.appendChild(el("div", h.stale ? "warnbox" : "okbox", h.stale
        ? "Handed off " + localTime(h.ready_at)
          + " — but the file has changed since. Hand off again so your assistant posts "
          + "this version."
        : "Handed off to your assistant " + localTime(h.ready_at)
          + " — waiting to be posted."));
    }
    if (!r) return;
    var box = el("div", "okbox");
    box.appendChild(document.createTextNode("Posted — weekly_updates #" + r.id + ", "
      + localTime(r.posted_at) + (r.posted_by ? " by " + r.posted_by : "")
      + (r.edited_at ? ", corrected " + localTime(r.edited_at) : "")
      + ". "));
    if (D.jira_base && r.jira_comment_id && (r.jira_key || (D.info && D.info.jira_key))) {
      var a = el("a", null, "Jira comment " + r.jira_comment_id);
      a.href = D.jira_base + (r.jira_key || D.info.jira_key) + "?focusedCommentId="
        + encodeURIComponent(r.jira_comment_id);
      a.target = "_blank";
      a.rel = "noopener";
      box.appendChild(a);
    }
    if (r.slack_ts) box.appendChild(document.createTextNode("  · Slack ts " + r.slack_ts));
    if (!r.slack_ts && D.slack_copy && !handBox.querySelector(".slackcopy")) {
      var sc = manualSlack({ channel: D.slack_copy.channel || "",
                             channel_name: D.slack_copy.channel_name,
                             text: D.slack_copy.text,
                             why: "Slack is not posted yet — this stays here until the "
                                  + "Slack link is recorded." });
      sc.className += " slackcopy";
      posted.appendChild(box);
      posted.appendChild(sc);
      return;
    }
    if (!r.rag_status && r.rag_from_content) {
      box.appendChild(document.createTextNode("  · RAG recorded as "
        + r.rag_from_content.join(" / ") + " (from the row's text)"));
    }
    posted.appendChild(box);
  }

  // ---- load
  function fill(r) {
    D = r;
    original = r.text;
    mtime = r.mtime;
    ta.value = r.text;
    ttl.textContent = (r.name || r.customer) + " — week of " + nice(r.week_of, true);
    pathline.textContent = r.path;
    origRag = { s: r.info.rag_status || "", c: r.info.rag_reason_code || "" };
    if (origRag.c && origRag.c !== "GREEN") lastReal = origRag.c;
    fillStatus(origRag.s);
    fillReason(origRag.c);
    // Last week's RAG, when the file belongs to the week on the board. Matched on the
    // week, not the card's path: a draft started a moment ago is not on the board yet.
    var card = BOARD && BOARD.cards.filter(function (c) {
      return c.customer === r.customer;
    })[0];
    var inWeek = BOARD && r.week_of >= BOARD.monday && r.week_of <= BOARD.sunday;
    var last = card && card.last && inWeek ? card.last : null;
    LASTWEEK = last;
    lastline.textContent = last && last.rag_status
      ? "Last week: " + last.rag_status + (last.rag_reason_code
        ? " · " + last.rag_reason_code : "") : "";
    if (r.fields.error) errBox.appendChild(el("div", "warnbox", r.fields.error));
    LAST = { bodies: r.bodies, checks: r.checks };
    drawChecks(r.checks || []);
    drawPosted();
    DIRTY = false;
    recompute();
    msg.textContent = "";
  }
  function reload() {
    return api("/api/weekly/one?path=" + encodeURIComponent(path)).then(function (r) {
      if (!r.ok) {
        ta.value = "";
        errBox.innerHTML = "";
        errBox.appendChild(el("div", "err", r.error || "could not open"));
        return;
      }
      errBox.innerHTML = "";
      fill(r);
      if (tab !== "edit") setTab(tab);
    });
  }

  // ---- actions
  revert.addEventListener("click", function () {
    if (!DIRTY) return;
    if (!window.confirm("Throw away your unsaved changes?")) return;
    ta.value = original;
    fillStatus(origRag.s);
    fillReason(origRag.c);
    recompute();
    if (tab !== "edit") setTab(tab);
  });

  save.addEventListener("click", function () {
    if (!DIRTY) { msg.textContent = "nothing changed"; return; }
    var s = status.value, c = reason.value;
    if ((s && !c) || (!s && c)) {
      msg.textContent = "choose both RAG fields, or neither";
      return;
    }
    save.disabled = true;
    msg.textContent = "saving…";
    post("/api/weekly/save", { path: path, text: ta.value, mtime: mtime,
                               rag_status: s, rag_reason_code: c }).then(function (r) {
      save.disabled = false;
      errBox.innerHTML = "";
      if (!r.ok) {
        msg.textContent = "";
        var e = el("div", "err", r.why || "could not save");
        if (r.stale) {
          var re = el("button", "tap", "Reload from disk");
          re.addEventListener("click", function () {
            if (!window.confirm("Reloading throws away your unsaved text. Copy anything "
                                + "you need first. Reload?")) return;
            DIRTY = false;
            reload();
          });
          e.appendChild(document.createTextNode("  "));
          e.appendChild(re);
        }
        errBox.appendChild(e);
        return;
      }
      reload().then(function () {
        msg.textContent = r.unchanged ? "no change" : "saved";
        setTimeout(function () { if (msg.textContent === "saved") msg.textContent = ""; },
                   2500);
      });
    }).catch(function (e) {
      save.disabled = false;
      msg.textContent = String(e);
    });
  });

  function pasteLine(title, line) {
    var box = el("div", "okbox");
    box.appendChild(el("div", null, title + " Paste this to your assistant:"));
    box.appendChild(el("pre", "wline", line));
    var cp = el("button", "tap", "Copy");
    cp.addEventListener("click", function () {
      try {
        navigator.clipboard.writeText(line).then(function () { cp.textContent = "Copied"; });
      } catch (e2) { cp.textContent = "select and copy"; }
    });
    box.appendChild(cp);
    handBox.innerHTML = "";
    handBox.appendChild(box);
  }

  // ---- Path B: post from mt itself (phase 6)
  // A posted update's button corrects it in place instead.
  function isCorrection() {
    return !!(D && D.row && D.row.jira_comment_id
              && !(D.handoff && D.handoff.status === "posting"));
  }
  function directWhy() {
    direct.textContent = isCorrection() ? "Correct posted update…" : "Post to Jira + Slack…";
    if (!CREDS) return "checking credentials…";
    if (!CREDS.jira || !CREDS.jira.ok) return (CREDS.jira && CREDS.jira.why) || "no Jira credentials";
    if (D && D.row && !isCorrection() && !(D.handoff && D.handoff.status === "posting")) {
      return "already posted — weekly_updates #" + D.row.id;
    }
    if (D && (D.checks || []).some(function (c) { return c.level === "error"; })) {
      return "fix the checks marked in red first";
    }
    return null;
  }

  // Same shape as the hand-off confirm, but the Yes here reaches a customer channel and
  // the corporate RAG report immediately, with nobody in between — so it shows current
  // vs new RAG as Jira has it right now, the exact text, and anything that looks like
  // a double post. Cancel is the default.
  direct.addEventListener("click", function () {
    if (DIRTY || document.querySelector(".modal.dlg")) return;
    if (isCorrection()) { startCorrection(); return; }
    direct.disabled = true;
    errBox.innerHTML = "";
    post("/api/weekly/plan", { path: path }).then(function (pl) {
      direct.disabled = false;
      if (!pl.ok) {
        errBox.appendChild(el("div", "err", "Cannot post. " + (pl.why || "")));
        return;
      }
      function chg(x) {
        return x.changes ? (x.current || "not set") + "  →  " + x.new
                         : x.new + "  (unchanged)";
      }
      var lines = [
        ["Jira", "comment on " + pl.issue + " — " + pl.jira_chars.toLocaleString()
          + " chars"],
        ["Customer Health", chg(pl.health)],
        ["RAG Reason Code", chg(pl.reason)],
        ["Slack", (pl.slack_mode === "api" ? "message to " : "BY HAND — copy into ")
          + ((pl.slack_channel_name || "") + " " + (pl.slack_channel || "")).trim()
          + " — " + pl.slack_chars.toLocaleString() + " chars"]
      ];
      var warn = (pl.warn || []).slice();
      if (pl.health.changes || pl.reason.changes) {
        warn.unshift("RAG CHANGES in Jira — this moves the account in the corporate RAG "
          + "report.");
      }
      if (pl.resuming) {
        var done = pl.steps.filter(function (s) { return s.done; })
          .map(function (s) { return s.step.replace("_", " "); });
        warn.unshift("Finishing a post that stopped part-way. Already done: "
          + (done.join(", ") || "nothing") + " — those are skipped.");
      }
      confirmDialog({
        title: pl.resuming ? "Finish posting " + pl.account + "?"
                           : "Post " + pl.account + " now?",
        lead: "mt posts this straight away — your assistant is not involved:",
        lines: lines, warn: warn,
        extra: [["Exact Jira comment", pl.jira_body],
                ["Exact Slack message", pl.slack_text]],
        yes: pl.resuming ? "Yes — finish posting" : "Yes — post it", no: "Cancel"
      }, function () { runPost(pl); });
    });
  });

  function runPost(pl) {
    direct.disabled = true;
    handBox.innerHTML = "";
    var wait = el("div", "okbox", "Posting to " + pl.issue + "…");
    handBox.appendChild(wait);
    post("/api/weekly/post", { path: pl.path, sha256: pl.sha256 }).then(function (r) {
      direct.disabled = false;
      handBox.innerHTML = "";
      var box = el("div", r.ok ? "okbox" : "err", r.ok
        ? "Posted. " + (r.manual_slack ? "Jira is done — Slack is yours to post (below)."
                                       : "Jira, Slack and the DB row are all done.")
        : "Stopped. " + (r.why || ""));
      (r.steps || []).forEach(function (s) {
        box.appendChild(el("div", "hint", (s.ok ? "✓ " : "✗ ") + s.step.replace("_", " ")
          + " — " + s.detail));
      });
      (r.notes || []).forEach(function (n) { box.appendChild(el("div", "hint", n)); });
      handBox.appendChild(box);
      if (r.manual_slack) {
        var ms = manualSlack(r.manual_slack);
        ms.className += " slackcopy";
        handBox.appendChild(ms);
      }
      var np = r.path || path;
      if (np !== path) {
        // Filed into YYYY/MM/ — follow the file, keep this result on screen.
        var keep = Array.prototype.slice.call(handBox.childNodes);
        path = np;
        pathline.textContent = np;
        reload().then(function () {
          handBox.innerHTML = "";
          keep.forEach(function (k) { handBox.appendChild(k); });
        });
      } else {
        reload();
      }
      load();
    });
  }

  // Correct = edit the recorded comment + message in place. Shows a line diff of the
  // comment, RAG current -> file, and what happens to Slack; Cancel is the default.
  function startCorrection() {
    direct.disabled = true;
    errBox.innerHTML = "";
    post("/api/weekly/correct-plan", { path: path }).then(function (pl) {
      direct.disabled = false;
      if (!pl.ok) {
        errBox.appendChild(el("div", "err", "Cannot correct. " + (pl.why || "")));
        return;
      }
      function chg(x) {
        return x.changes ? (x.current || "not set") + "  →  " + x.new : x.new + "  (unchanged)";
      }
      var slackLine = pl.slack_mode === "api" && pl.slack_changes === false
          ? "unchanged — left alone"
        : pl.slack_mode === "api" ? "edit message " + pl.slack_ts + " in "
          + ((pl.slack_channel_name || "") + " " + (pl.slack_channel || "")).trim()
        : pl.slack_mode === "manual" ? "BY HAND — copy the corrected text over message "
          + pl.slack_ts
        : "left alone — no Slack message recorded";
      confirmDialog({
        title: "Correct the posted " + pl.account + " update?",
        lead: "mt edits what is already posted — same comment, same links:",
        lines: [
          ["Jira", pl.jira_changes ? "edit comment " + pl.comment_id + " on " + pl.issue
                                   : "comment text unchanged"],
          ["Customer Health", chg(pl.health)],
          ["RAG Reason Code", chg(pl.reason)],
          ["Slack", slackLine]
        ],
        warn: pl.warn || [],
        extra: [["What changes in the Jira comment", pl.diff],
                ["Corrected Jira comment", pl.jira_body],
                ["Corrected Slack message", pl.slack_text]],
        yes: "Yes — correct it", no: "Cancel"
      }, function () { runCorrection(pl); });
    });
  }

  function runCorrection(pl) {
    direct.disabled = true;
    handBox.innerHTML = "";
    handBox.appendChild(el("div", "okbox", "Correcting " + pl.issue + "…"));
    post("/api/weekly/correct", { path: pl.path, sha256: pl.sha256 }).then(function (r) {
      direct.disabled = false;
      handBox.innerHTML = "";
      var box = el("div", r.ok ? "okbox" : "err", r.ok ? "Corrected." : "Stopped. " + (r.why || ""));
      (r.steps || []).forEach(function (s) {
        box.appendChild(el("div", "hint", (s.ok ? "✓ " : "✗ ") + s.step.replace("_", " ")
          + " — " + s.detail));
      });
      handBox.appendChild(box);
      (r.notes || []).forEach(function (n) {
        handBox.appendChild(el("div", /OLD text|NOT UPDATED/.test(n) ? "warnbox" : "hint", n));
      });
      if (r.manual_slack) {
        var ms = manualSlack(r.manual_slack);
        ms.className += " slackcopy";
        handBox.appendChild(ms);
      }
      reload();
    });
  }

  function manualSlack(m) {
    var box = el("div", "warnbox");
    box.appendChild(el("div", null, "Post this to " + ((m.channel_name || "") + " "
      + m.channel).trim() + ", then use Record as posted with the message's link so the "
      + "row gets its Slack ts."));
    box.appendChild(el("pre", "wline", m.text));
    var cp = el("button", "tap", "Copy");
    cp.addEventListener("click", function () {
      try {
        navigator.clipboard.writeText(m.text).then(function () { cp.textContent = "Copied"; });
      } catch (e2) { cp.textContent = "select and copy"; }
    });
    box.appendChild(cp);
    box.appendChild(el("div", "hint", m.why || ""));
    return box;
  }

  // Hand off = "approved, post exactly this". It goes through a confirm that spells out
  // what will be posted where, because a misclick here reaches a customer channel and the
  // corporate RAG report. Cancel is the default button.
  hand.addEventListener("click", function () {
    if (DIRTY || document.querySelector(".modal.dlg")) return;
    var i = D.info || {};
    var b = LAST.bodies || D.bodies || {};
    var h = D.handoff || {};
    var lines = [
      ["Jira", "comment on " + (i.jira_key || "?") + " — "
        + (b.jira_chars || 0).toLocaleString() + " chars"],
      ["Customer Health", (D.emoji[i.rag_status] || "") + " " + (i.rag_status || "not set")],
      ["RAG Reason Code", i.rag_reason_code || "not set"],
      ["Slack", "message to " + ((i.slack_channel_name || "") + " "
        + (i.slack_channel || "")).trim() + " — " + (b.slack_chars || 0).toLocaleString()
        + " chars"]
    ];
    var warn = [];
    var lw = LASTWEEK;
    if (lw && lw.rag_status && (lw.rag_status !== i.rag_status
        || lw.rag_reason_code !== i.rag_reason_code)) {
      warn.push("RAG CHANGES from " + lw.rag_status + " · " + (lw.rag_reason_code || "?")
        + " to " + i.rag_status + " · " + i.rag_reason_code
        + " — this moves the account in the corporate RAG report.");
    }
    if (h.status === "revise") {
      warn.push("Your send-back notes are still open with your assistant. Handing off now "
        + "tells it to post THIS version instead of revising it.");
    }
    if (h.status === "revised" && h.file_changed === false) {
      warn.push("Your assistant marked this revised, but the file did not change.");
    }
    confirmDialog({
      title: "Hand off for posting?",
      lead: "Your assistant will post this exactly as saved:",
      lines: lines, warn: warn,
      yes: "Yes — hand off", no: "Cancel"
    }, function () {
      hand.disabled = true;
      post("/api/weekly/handoff", { path: path }).then(function (r) {
        hand.disabled = false;
        errBox.innerHTML = "";
        handBox.innerHTML = "";
        if (!r.ok) {
          errBox.appendChild(el("div", "err", "Not handed off. " + (r.why || "")));
          return;
        }
        pasteLine("Handed off — " + r.handoff + ".", r.line);
        reload();
      });
    });
  });

  // Send back = "revise this, do NOT post". Notes go in the hand-off file; the assistant
  // edits the draft and marks it revised, and the card says so.
  back.addEventListener("click", function () {
    if (DIRTY || document.querySelector(".modal.dlg")) return;
    notesDialog(D.handoff, function (notes, done) {
      post("/api/weekly/sendback", { path: path, notes: notes }).then(function (r) {
        if (!r.ok) { done(r.why || "not sent"); return; }
        done(null);
        errBox.innerHTML = "";
        pasteLine("Sent back — nothing will be posted.", r.line);
        reload();
      });
    });
  });

  rec.addEventListener("click", function () { recordForm(path, function () { reload(); }); });

  hist.addEventListener("click", function () {
    showHistory(path, function (t) { ta.value = t; recompute(); setTab("edit"); });
  });

  ta.value = "loading…";
  reload();
}

// A confirm that cannot be dismissed by a stray Enter: focus starts on Cancel, Escape
// and a click outside both cancel, and only the explicit Yes button proceeds.
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
  (o.extra || []).forEach(function (x) {
    var dd = el("details", "dlgtext");
    dd.appendChild(el("summary", null, x[0]));
    dd.appendChild(el("pre", "wline", x[1]));
    f.appendChild(dd);
  });
  var acts = el("div", "dlgacts");
  var no = el("button", null, o.no || "Cancel");
  var yes = el("button", "primary", o.yes || "Yes");
  acts.appendChild(no);
  acts.appendChild(yes);
  f.appendChild(acts);
  sheet.appendChild(f);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);
  var close = function () { wrap.remove(); document.removeEventListener("keydown", key, true); };
  var key = function (e) {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  };
  document.addEventListener("keydown", key, true);
  no.addEventListener("click", close);
  wrap.addEventListener("click", function (e) { if (e.target === wrap) close(); });
  yes.addEventListener("click", function () { close(); onYes(); });
  no.focus();
}

function notesDialog(prior, onSend) {
  var wrap = el("div", "modal dlg");
  wrap.style.zIndex = "90";
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, "Send back to your assistant"));
  sheet.appendChild(head);
  var f = el("div", "form fm guide");
  f.appendChild(el("div", "hint", "Your assistant revises the draft from these notes and "
    + "hands it back for your review. Nothing is posted. Anything you want it to answer, it "
    + "answers in the Note block."));
  var ta = el("textarea", "fmdesc");
  ta.rows = 7;
  ta.placeholder = "e.g. Tighten the summary to three sentences. Add the Orlando overage "
    + "decision to Open Items. Check the RAG — I think this is Amber now.";
  if (prior && prior.status === "revise" && prior.notes) ta.value = prior.notes;
  f.appendChild(ta);
  var out = el("div");
  f.appendChild(out);
  var acts = el("div", "dlgacts");
  var no = el("button", null, "Cancel");
  var yes = el("button", "primary", "Send back");
  acts.appendChild(no);
  acts.appendChild(yes);
  f.appendChild(acts);
  sheet.appendChild(f);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);
  var close = function () { wrap.remove(); document.removeEventListener("keydown", key, true); };
  var key = function (e) {
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  };
  document.addEventListener("keydown", key, true);
  no.addEventListener("click", close);
  yes.addEventListener("click", function () {
    if (!ta.value.trim()) { out.innerHTML = ""; out.appendChild(el("div", "err",
      "Write what you want changed first.")); return; }
    yes.disabled = true;
    onSend(ta.value, function (err) {
      yes.disabled = false;
      if (err) { out.innerHTML = ""; out.appendChild(el("div", "err", err)); return; }
      close();
    });
  });
  ta.focus();
}

// Record the DB row by hand — for an update posted outside the tool (the assistant
// normally runs `weekly.py --record` itself). Ids or URLs are both accepted; weekly.py
// extracts.
function recordForm(path, done) {
  var wrap = el("div", "modal");
  wrap.style.zIndex = "80";
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, "Record as posted"));
  var x = el("button", null, "Cancel");
  x.addEventListener("click", function () { wrap.remove(); });
  head.appendChild(x);
  sheet.appendChild(head);
  var f = el("div", "form fm guide");
  f.appendChild(el("div", "hint", "Writes the weekly_updates row — posting is not done "
    + "until it exists. Use this only once the update is really in Jira (and ideally "
    + "Slack). RAG is taken from the saved file."));
  function inp(label, ph) {
    var l = el("label");
    l.appendChild(el("span", "fmlab", label));
    var i = el("input");
    i.placeholder = ph;
    i.spellcheck = false;
    l.appendChild(i);
    f.appendChild(l);
    return i;
  }
  var jc = inp("Jira comment", "comment id, or the comment's link");
  var st = inp("Slack message", "ts like 1787933160.363229, or the message's link "
                                + "(optional — add later)");
  var out = el("div");
  f.appendChild(out);
  var go = el("button", "primary", "Record");
  f.appendChild(go);
  sheet.appendChild(f);
  wrap.appendChild(sheet);
  document.body.appendChild(wrap);
  jc.focus();
  go.addEventListener("click", function () {
    go.disabled = true;
    post("/api/weekly/record", { path: path, jira_comment: jc.value,
                                 slack_ts: st.value }).then(function (r) {
      go.disabled = false;
      out.innerHTML = "";
      if (!r.ok) { out.appendChild(el("div", "err", r.why || "not recorded")); return; }
      var ok = el("div", "okbox", "weekly_updates #" + r.id + " " + r.action + ".");
      (r.notes || []).forEach(function (n) { ok.appendChild(el("div", "hint", n)); });
      out.appendChild(ok);
      go.hidden = true;
      x.textContent = "Close";
      done();
    });
  });
}

// Earlier saves, from the same vault /files uses. Offered as text to load into the
// editor, never applied behind your back — restoring is then just a normal save.
function showHistory(path, apply) {
  var wrap = el("div", "modal");
  wrap.style.zIndex = "80";
  var sheet = el("div", "sheet");
  var head = el("div", "head");
  head.appendChild(el("b", null, "Earlier versions"));
  var x = el("button", null, "Close");
  x.addEventListener("click", function () { wrap.remove(); });
  head.appendChild(x);
  sheet.appendChild(head);
  var box = el("div", "form guide");
  box.appendChild(el("div", "hint", "Kept outside the workspace, so Google Drive never "
    + "sees them. The last 20 saves of this file."));
  sheet.appendChild(box);
  wrap.appendChild(sheet);
  wrap.addEventListener("click", function (e) { if (e.target === wrap) wrap.remove(); });
  document.body.appendChild(wrap);
  var q = "root=workspace&path=" + encodeURIComponent(path);
  api("/api/file/versions?" + q).then(function (r) {
    if (r.error) { box.appendChild(el("div", "err", r.error)); return; }
    var vs = r.versions || [];
    if (!vs.length) {
      box.appendChild(el("div", "empty", "No earlier versions yet."));
      return;
    }
    vs.forEach(function (v) {
      var row = el("div", "row");
      row.appendChild(el("div", "when", v.stamp.slice(0, 8) + " " + v.stamp.slice(9, 15)));
      row.appendChild(el("div", "mid"));
      var right = el("div", "right");
      var use = el("button", "tap", "Load into editor");
      use.addEventListener("click", function () {
        api("/api/file/version?" + q + "&stamp=" + encodeURIComponent(v.stamp))
          .then(function (o) {
            if (!o.ok) return;
            apply(o.text);
            wrap.remove();
          });
      });
      right.appendChild(use);
      row.appendChild(right);
      box.appendChild(row);
    });
  });
}

window.addEventListener("beforeunload", function (e) {
  if (DIRTY) { e.preventDefault(); e.returnValue = ""; }
});

boot();
})();

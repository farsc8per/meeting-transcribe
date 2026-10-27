// storage.js — the "Free up space" dialog, shared by the meeting list and the archive.
//
//   mtFreeSpace(sessionId, onDone)   // opens the dialog; onDone() after anything changed
//   mtBytes(n)                       // 1234567 → "1.2 MB"
//
// The server decides what is allowed (storage.py plan()) and re-checks it on apply, so
// this file only shows the answer: a disabled option carries its reason, never a guess.
(function () {
"use strict";

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

window.mtBytes = function (n) {
  n = n || 0;
  if (n < 1e6) return Math.max(0, Math.round(n / 1e3)) + " KB";
  if (n < 1e9) return (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + " MB";
  return (n / 1e9).toFixed(1) + " GB";
};

function post(path, body) {
  return fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                      body: JSON.stringify(body)}).then(function (r) { return r.json(); });
}
window.mtStoragePost = post;

function close() { var m = document.getElementById("fsmodal"); if (m) m.remove(); }

window.mtFreeSpace = async function (id, onDone) {
  close();
  var wrap = el("div", "modal"); wrap.id = "fsmodal";
  var sheet = el("div", "sheet fs");
  var head = el("div", "head");
  var ttl = el("b", null, "Free up space");
  head.appendChild(ttl);
  var x = el("button", null, "Cancel"); x.onclick = close;
  head.appendChild(x);
  sheet.appendChild(head);
  var body = el("div", "fsbody", "loading…");
  sheet.appendChild(body);
  wrap.appendChild(sheet);
  wrap.onclick = function (e) { if (e.target === wrap) close(); };
  document.body.appendChild(wrap);

  var p = await fetch("/api/storage/plan?id=" + encodeURIComponent(id))
                .then(function (r) { return r.json(); });
  body.textContent = "";
  if (!p.ok) { body.appendChild(el("div", "err", p.why || "could not load")); return; }
  ttl.textContent = "Free up space — " + p.title;
  var u = p.usage;
  body.appendChild(el("div", "fsuse",
    "Takes " + mtBytes(u.total) + ":  audio " + mtBytes(u.audio) + "  ·  working files " +
    mtBytes(u.rebuildable) + "  ·  transcript & notes " + mtBytes(u.text + u.other)));

  var opts = [
    {key: "trim", label: "Free working files", plan: p.trim,
     what: "The 16 kHz copies made while transcribing. Nothing is lost — re-processing " +
           "rebuilds them. Deleted straight away."},
    {key: "remove_audio", label: "Remove audio, keep the transcript", plan: p.remove_audio,
     what: "Keeps the transcript, summary, speaker names and your notes. You can no longer " +
           "play it back, re-process it, or teach mt a voice from it."},
    {key: "delete", label: "Delete the meeting", plan: p.delete,
     what: "Everything mt made for this meeting — for test recordings and meetings you " +
           "never need again."}
  ];
  var picked = null, go;
  var list = el("div", "fsopts");
  opts.forEach(function (o) {
    var lab = el("label", "fsopt" + (o.plan.ok ? "" : " off"));
    var r = el("input"); r.type = "radio"; r.name = "fs"; r.disabled = !o.plan.ok;
    r.onchange = function () { picked = o; go.disabled = false;
      go.textContent = o.key === "trim" ? "Free " + mtBytes(o.plan.bytes)
                     : o.key === "delete" ? "Delete meeting" : "Remove audio"; };
    lab.appendChild(r);
    var txt = el("div");
    var top = el("div", "fsl");
    top.appendChild(el("b", null, o.label));
    if (o.plan.bytes) top.appendChild(el("span", "fsb", mtBytes(o.plan.bytes)));
    txt.appendChild(top);
    txt.appendChild(el("div", "fsw", o.what));
    if (!o.plan.ok && o.plan.why) txt.appendChild(el("div", "fsno", o.plan.why));
    (o.plan.warn || []).forEach(function (w) { txt.appendChild(el("div", "fswarn", w)); });
    if (o.key === "delete" && o.plan.ok && (o.plan.kept || []).length)
      txt.appendChild(el("div", "fswarn", "Stays where it is (not made by mt): " +
                         o.plan.kept.join(", ")));
    lab.appendChild(txt);
    list.appendChild(lab);
  });
  body.appendChild(list);
  body.appendChild(el("div", "fsnote", "Removed audio and deleted meetings wait in the " +
    "trash for " + p.trash_days + " days — restore them from the Archive page — then " +
    "they are gone for good."));
  var foot = el("div", "fsfoot");
  var msg = el("span", "fsmsg");
  go = el("button", "stop", "Choose one"); go.disabled = true;
  go.onclick = async function () {
    if (!picked) return;
    go.disabled = true; msg.textContent = "working…";
    var r = await post("/api/storage/apply", {action: picked.key, sessions: [id]});
    if (!r.done || !r.done.length) {
      msg.textContent = (r.refused && r.refused[0] && r.refused[0].why) || r.why || "failed";
      go.disabled = false; return;
    }
    close();
    if (onDone) onDone(picked.key, r);
  };
  foot.appendChild(msg);
  foot.appendChild(go);
  body.appendChild(foot);
};

document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
})();

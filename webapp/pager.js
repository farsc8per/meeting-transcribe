// pager.js — one paging control, shared by every long list in the app.
//
//   var pg = mtPager("tracking", redraw);   // key = which list, for the remembered size
//   box.before(pg.mount());                  // mount() can be called more than once —
//   box.after(pg.mount());                   // top and bottom bars stay in step
//
// Two ways to drive it, because the lists differ:
//   * SERVER-paged (tracking — ~2 KB a row, 1.5 MB for everything): read pg.limit and
//     pg.offset into the request, then pg.setTotal(total) with what the server reports.
//   * CLIENT-paged (files, contacts — the whole list is already in memory and small; the
//     cost there is drawing hundreds of rows): pg.slice(allRows) returns just this page.
//
// Page size is 20 / 50 / 100 / All, default 20, remembered per list. Storage is wrapped
// because it throws in a private window; the pager still works, it just forgets.
(function () {
"use strict";

var SIZES = [20, 50, 100, 0];          // 0 = All

window.mtPager = function (key, onChange) {
  var skey = "mt.pagesize." + key;
  var size = 20;
  try {
    var v = parseInt(localStorage.getItem(skey), 10);
    if (SIZES.indexOf(v) !== -1) size = v;
  } catch (e) {}
  var page = 0, total = 0, bars = [];

  function pages() { return size ? Math.max(1, Math.ceil(total / size)) : 1; }
  function clamp() { if (page > pages() - 1) page = pages() - 1; if (page < 0) page = 0; }

  function btn(label, disabled, go) {
    var b = document.createElement("button");
    b.textContent = label;
    b.disabled = disabled;
    b.addEventListener("click", go);
    return b;
  }

  function drawOne(bar) {
    bar.innerHTML = "";
    // A list that fits on the smallest page needs no pager — "1–2 of 2 · Show 20" is
    // noise. Pages that hide the bar for their own reasons (a tab with no list) do it
    // after this runs, so they still win.
    bar.hidden = total <= SIZES[0];
    var from = total ? (size ? page * size + 1 : 1) : 0;
    var to = size ? Math.min(total, (page + 1) * size) : total;

    var info = document.createElement("span");
    info.className = "pg-info";
    info.textContent = total ? from + "–" + to + " of " + total : "0 of 0";
    bar.appendChild(info);

    var nav = document.createElement("span");
    nav.className = "pg-nav";
    nav.appendChild(btn("« First", page === 0, function () { go(0); }));
    nav.appendChild(btn("‹ Prev", page === 0, function () { go(page - 1); }));
    var at = document.createElement("span");
    at.className = "pg-at";
    at.textContent = "page " + (page + 1) + " of " + pages();
    nav.appendChild(at);
    nav.appendChild(btn("Next ›", page >= pages() - 1, function () { go(page + 1); }));
    nav.appendChild(btn("Last »", page >= pages() - 1, function () { go(pages() - 1); }));
    if (size && total > size) bar.appendChild(nav);

    var lab = document.createElement("label");
    lab.className = "pg-size";
    lab.appendChild(document.createTextNode("Show "));
    var sel = document.createElement("select");
    SIZES.forEach(function (n) {
      var o = document.createElement("option");
      o.value = String(n);
      o.textContent = n ? String(n) : "All";
      sel.appendChild(o);
    });
    sel.value = String(size);
    sel.addEventListener("change", function () {
      // Keep the first row you were looking at on screen rather than jumping to page 1.
      var first = size ? page * size : 0;
      size = parseInt(sel.value, 10) || 0;
      try { localStorage.setItem(skey, String(size)); } catch (e) {}
      page = size ? Math.floor(first / size) : 0;
      clamp();
      drawAll();
      if (onChange) onChange();
    });
    lab.appendChild(sel);
    lab.appendChild(document.createTextNode(" per page"));
    bar.appendChild(lab);
  }

  function drawAll() { bars.forEach(drawOne); }

  function go(p) {
    page = p;
    clamp();
    drawAll();
    if (onChange) onChange();
    // Paging from the bottom bar should land you at the top of the new page.
    var first = bars[0];
    if (first && first.getBoundingClientRect().top < 0) {
      first.scrollIntoView({ block: "start" });
    }
  }

  return {
    mount: function () {
      var bar = document.createElement("div");
      bar.className = "pager";
      bars.push(bar);
      drawOne(bar);
      return bar;
    },
    get limit() { return size || 0; },          // 0 = no limit (All)
    get offset() { return size ? page * size : 0; },
    get size() { return size; },
    reset: function () { page = 0; drawAll(); },
    setTotal: function (n) { total = Math.max(0, n | 0); clamp(); drawAll(); },
    slice: function (rows) {
      total = rows.length;
      clamp();
      drawAll();
      return size ? rows.slice(page * size, page * size + size) : rows;
    }
  };
};
})();

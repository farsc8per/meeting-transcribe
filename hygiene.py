"""hygiene.py — the /hygiene page, Data area (Phase 3a, TRACKING-PLAN.md).

Finds data that has drifted from the rules and proposes fixes in GROUPS. Nothing is ever
applied without the user approving that group on the page. The Memory area (3b) is a separate
module on purpose: the two have different care and handling, and must never share a queue.

How a group is applied, and why each step is there:

  1. The page sends back the group's `sha` — a hash of exactly the rows it showed. The
     group is recomputed now; if the hash differs, the data moved since he looked and the
     apply is refused. He approves what he saw, never something else.
  2. A gzip'd sqlite `.backup` of the whole DB, named `<db name>.bak-<stamp>-hygiene-…`.
     db-snapshot.sh prunes only its own dated `snapshots/` pattern, so these
     are never pruned from under an undo.
  3. One transaction. Every UPDATE is guarded on the value it expects (`WHERE col IS ?`);
     if any row moved, the whole group rolls back. `updated_by='hygiene-YYYYMMDD'`;
     `updated_at` is left alone — this fixes the spelling of a value, not the substance of
     an item (the Sep 22 2026 precedent).
  4. `hygiene_log` keeps every row before/after, so undo can put back exactly what changed
     — and refuses if anything was edited in the meantime.

Tables are added (CREATE TABLE IF NOT EXISTS), never altered or dropped. Nothing is
DELETEd, with one narrow exception inherited from tracking.remove_link: undoing a link
this module created removes that same link row.

CLI:  python3 hygiene.py            list groups
      python3 hygiene.py --log      show the log
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import tracking as TR  # noqa: E402  (same DB, same vocabularies, same link rules)

SAMPLE_MAX = 400          # rows sent to the page per group; the count is always the truth
DESC_MAX = 140

_SLACK_ID = re.compile(r"^[CDG][A-Z0-9]{8,}$")
_SLACK_ID_IN = re.compile(r"\(([CDG][A-Z0-9]{8,})\)")


# ------------------------------------------------------------------ plumbing

def _db() -> Path:
    return Path(TR.DB_PATH)


def _ro():
    return TR._connect()


def _rw():
    return TR._connect_w()


def _today() -> str:
    return dt.date.today().strftime("%Y%m%d")


def _by() -> str:
    return f"hygiene-{_today()}"


def _short(s, n=DESC_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _lab(r) -> str:
    """A row's label: its title (R4.4c) when it has one, else the description, shortened."""
    r = dict(r)
    return _short(r.get("title") or r.get("description"))


def _tcol(conn) -> str:
    """", title" when the column exists — so these queries also run on a pre-R4.4c DB."""
    return ", title" if "title" in _cols(conn, "action_items") else ""


def _sha(rows: list[dict]) -> str:
    """What the page saw: each row's key and its exact changes. Order-independent."""
    canon = sorted(json.dumps({"k": r["key"], "c": r.get("changes"), "l": r.get("link"),
                               "p": r.get("promote")}, sort_keys=True) for r in rows)
    return hashlib.sha256("\n".join(canon).encode()).hexdigest()[:16]


def ensure_tables(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS hygiene_log (
        id INTEGER PRIMARY KEY,
        area TEXT NOT NULL,
        group_key TEXT NOT NULL,
        title TEXT,
        choice TEXT,
        rows_json TEXT NOT NULL,
        n INTEGER NOT NULL,
        backup TEXT,
        applied_at TEXT NOT NULL,
        applied_by TEXT,
        undone_at TEXT,
        undone_by TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS hygiene_skip (
        area TEXT NOT NULL,
        group_key TEXT NOT NULL,
        sha TEXT NOT NULL,
        skipped_at TEXT NOT NULL,
        PRIMARY KEY (area, group_key))""")
    # Oct 16: a skip remembers WHICH rows were skipped, so it holds until a NEW row appears
    # (it used to break whenever any skipped row was edited — e.g. by titling it).
    if "row_keys" not in [r[1] for r in conn.execute("PRAGMA table_info(hygiene_skip)")]:
        conn.execute("ALTER TABLE hygiene_skip ADD COLUMN row_keys TEXT")


def _skips(conn) -> dict:
    """group key → {sha, rows: set of skipped row keys, or None for an old sha-only skip}."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(hygiene_skip)")]
        sel = "group_key, sha" + (", row_keys" if "row_keys" in cols else "")
        out = {}
        for r in conn.execute(f"SELECT {sel} FROM hygiene_skip WHERE area='data'"):
            rk = r["row_keys"] if "row_keys" in r.keys() else None
            out[r["group_key"]] = {"sha": r["sha"],
                                   "rows": set(json.loads(rk)) if rk else None}
        return out
    except sqlite3.OperationalError:
        return {}                 # table not created yet: nothing has been skipped


def _kept(conn) -> set:
    """Row keys the user decided to keep as they are — "Keep as an item" on a promote
    suggestion. Stored as hygiene_log entries (group_key keep:…), so the log lists them
    and its Undo ("Suggest again") reverses one."""
    out = set()
    try:
        for r in conn.execute("SELECT rows_json FROM hygiene_log WHERE area='data' AND "
                              "group_key LIKE 'keep:%' AND undone_at IS NULL"):
            for x in json.loads(r["rows_json"] or "[]"):
                if x.get("keep_key"):
                    out.add(x["keep_key"])
    except sqlite3.OperationalError:
        pass
    return out


def _shape(conn, groups: list[dict]) -> list[dict]:
    """Apply the user's standing decisions to freshly computed groups: rows kept as they are
    disappear for good; a skip hides exactly the rows that were skipped, and the card comes
    back only with rows that are new since (showing just those). Used by the page AND by
    apply, so the sha the page approves is the sha apply checks."""
    kept, skips = _kept(conn), _skips(conn)
    for g in groups:
        complete = len(g["rows"]) == g["count"]
        if kept and g["rows"]:
            g["rows"] = [r for r in g["rows"] if r["key"] not in kept]
            if complete:
                g["count"] = len(g["rows"])
        sk = skips.get(g["key"])
        g["skipped"] = False
        if g["actionable"] and sk:
            if sk["rows"] is None:                       # an old sha-only skip
                g["skipped"] = sk["sha"] == _sha(g["rows"])
            elif g["rows"]:
                new = [r for r in g["rows"] if r["key"] not in sk["rows"]]
                if new and len(new) < len(g["rows"]):
                    g["new_since_skip"] = len(new)
                    g["rows"] = new
                    if complete:
                        g["count"] = len(new)
                elif not new:
                    g["skipped"] = True
            else:                                        # a card with no rows: by count
                prev = max([int(k.split(":")[1]) for k in sk["rows"]
                            if k.startswith("count:")] or [0])
                g["skipped"] = g["count"] <= prev
        g["sha"] = _sha(g["rows"])
        g["actionable"] = g["actionable"] and g["count"] > 0
    return [g for g in groups if g["count"] > 0 or g["kind"] in ("watch", "locked")]


def _cols(conn, table) -> list[str]:
    return TR._cols(conn, table)


# ------------------------------------------------------------------ the checks
#
# Each check returns groups. A group:
#   key, title, why, kind, count, rows, choices?, locked?, action_note?
# kind:  "fix"    — a known target; approve applies it
#        "decide" — no default; the user picks the target from `choices`
#        "link"   — rows gain a file link (external_links), per-row
#        "watch"  — shown for awareness; fixed somewhere else, no apply here
#        "locked" — a later phase; shown so the size is visible, cannot be applied
#        "review" — worked on its own page (`href`); counts toward the badge, no apply here
# A row:  key, table, id, label, changes:[{field,before,after}]  or  link:{path,role}

def _vocab_groups(conn) -> list[dict]:
    out = []
    specs = [("action_items", TR.FIELD_VOCAB, TR.LEGACY, "Action items"),
             ("projects", TR.PROJECT_VOCAB, TR.PROJECT_LEGACY, "Projects")]
    for table, vocab, legacy, tlabel in specs:
        have = _cols(conn, table)
        label_col = "description" if table == "action_items" else "name"
        for field, allowed in vocab.items():
            if field not in have:
                continue
            q = (f"SELECT id, {label_col} AS label, {field} AS v, "
                 + ("priority" if "priority" in have else "NULL AS priority")
                 + f" FROM {table} WHERE COALESCE({field},'') <> '' ORDER BY id")
            bad: dict[str, list] = {}
            for r in conn.execute(q):
                if r["v"] not in allowed:
                    bad.setdefault(r["v"], []).append(r)
            for value, rs in sorted(bad.items()):
                target = (legacy.get(field) or {}).get(value)
                rows = []
                for r in rs:
                    ch = [{"field": field, "before": value, "after": target}]
                    # `low_priority` was a priority smuggled into status: moving it to
                    # `active` without setting priority=low would lose the meaning. The
                    # column may not exist yet (tracking adds it on first write); apply()
                    # adds it before anything is written, so the change is always carried.
                    if (table == "projects" and field == "status"
                            and value == "low_priority"):
                        ch.append({"field": "priority", "before": r["priority"],
                                   "after": "low"})
                    rows.append({"key": f"{table}:{r['id']}", "table": table,
                                 "id": r["id"], "label": _short(r["label"]),
                                 "changes": ch})
                g = {"key": f"vocab:{table}:{field}:{value}",
                     "title": f"{tlabel} — {field} = “{value}”",
                     "count": len(rows), "rows": rows}
                if target:
                    g.update(kind="fix",
                             why=f"“{value}” is a retired spelling. It becomes “{target}”"
                                 + (" and priority becomes “low”."
                                    if value == "low_priority" else "."))
                else:
                    g.update(kind="decide", choices=list(allowed),
                             why=f"“{value}” isn't one of the allowed values and there's "
                                 "no known mapping. Pick what it should be.")
                out.append(g)
    return out


def _slack_group(conn) -> list[dict]:
    if "slack_channel" not in _cols(conn, "weekly_updates"):
        return []
    try:
        import weekly as WK
        by_name = {}
        for short in WK._customers():
            name, cid = WK.channel_name(short), WK.channel_id(short)
            if name and cid:
                by_name[name.lower()] = cid
    except Exception:
        by_name = {}
    rows, stuck = [], []
    for r in conn.execute(
            "SELECT w.id, w.week_of, w.slack_channel AS v, c.short_name AS s "
            "FROM weekly_updates w LEFT JOIN customers c ON c.id = w.customer_id "
            "WHERE COALESCE(w.slack_channel,'') <> '' ORDER BY w.id"):
        v = r["v"].strip()
        if _SLACK_ID.match(v):
            continue
        m = _SLACK_ID_IN.search(v)
        cid = m.group(1) if m else by_name.get(v.split("(")[0].strip().lower())
        label = f"{r['s'] or '?'} · week of {r['week_of']}"
        row = {"key": f"weekly_updates:{r['id']}", "table": "weekly_updates",
               "id": r["id"], "label": label,
               "changes": [{"field": "slack_channel", "before": r["v"], "after": cid}]}
        (rows if cid else stuck).append(row)
    out = []
    if rows:
        out.append({"key": "slack:weekly_updates", "kind": "fix",
                    "title": "Weekly updates — Slack channel written as a name",
                    "why": "The rest of the table stores the raw channel id (C…), which is "
                           "what the Slack API and the posting code use. The id comes from "
                           "the id already in brackets, or the known channel map.",
                    "count": len(rows), "rows": rows})
    if stuck:
        for s in stuck:
            s.pop("changes", None)
        out.append({"key": "slack:unknown", "kind": "watch",
                    "title": "Weekly updates — Slack channel with no known id",
                    "why": "No id in the text and no match in the channel map. Add the "
                           "channel id to the account's slack_internal, then this becomes "
                           "a fix.", "count": len(stuck), "rows": stuck})
    return out


def _link_groups(conn) -> list[dict]:
    try:
        D = TR._docs()
    except Exception:
        return []
    have = {}
    for r in conn.execute("SELECT local_id, external_item_id FROM external_links "
                          "WHERE local_table='action_items' AND system='file'"):
        have.setdefault(r["local_id"], set()).add(r["external_item_id"])
    rows, missing = [], []
    for r in conn.execute(f"SELECT id, description, source{_tcol(conn)} FROM action_items "
                          f"ORDER BY id"):
        blob = " ".join(str(r[k] or "") for k in ("source", "description"))
        if "/" not in blob:
            continue
        found = []
        for m in TR._PATH_RE.finditer(blob):
            found.append((m.group(1), TR._role_for(m.group(1))))
        for m in TR._DIR_RE.finditer(blob):
            found.append((m.group(1) + "/summary.md", "source_meeting"))
        seen = set()
        for path, role in found:
            if path in seen or path in have.get(r["id"], ()):
                continue
            seen.add(path)
            row = {"key": f"action_items:{r['id']}:{path}", "table": "action_items",
                   "id": r["id"], "label": _lab(r),
                   "link": {"path": path, "role": role}}
            (rows if D.resolve("workspace", path) is not None else missing).append(row)
    out = []
    if rows:
        out.append({"key": "links:action_items", "kind": "link",
                    "title": "Action items — file named in the text, not linked",
                    "why": "These rows mention a file in their source or description. "
                           "Linking it adds an Open button on the tracking row. The text "
                           "itself is not changed.", "count": len(rows), "rows": rows})
    if missing:
        for m in missing:
            m["note"] = "file not found"
        out.append({"key": "links:missing", "kind": "watch",
                    "title": "Action items — file named in the text that doesn't exist",
                    "why": "The file was moved or renamed. Nothing to link; find the new "
                           "location and link it from the tracking row.",
                    "count": len(missing), "rows": missing})
    return out


def _rag_group(conn) -> list[dict]:
    if "rag_status" not in _cols(conn, "weekly_updates"):
        return []
    rs = conn.execute(
        "SELECT w.id, w.week_of, c.short_name AS s FROM weekly_updates w "
        "LEFT JOIN customers c ON c.id = w.customer_id "
        "WHERE COALESCE(w.rag_status,'') = '' ORDER BY w.id").fetchall()
    if not rs:
        return []
    return [{"key": "rag:missing", "kind": "watch",
             "title": "Weekly updates — no RAG status",
             "why": "Set it on the Weekly page; the RAG pick-list writes the file and the "
                    "row together.", "count": len(rs),
             "rows": [{"key": f"weekly_updates:{r['id']}", "table": "weekly_updates",
                       "id": r["id"], "label": f"{r['s'] or '?'} · week of {r['week_of']}"}
                      for r in rs]}]


def _owner_group(conn) -> list[dict]:
    """Phase 7. With its tables in (`tracking.py --migrate-p7`): a FIX group of the owners
    that resolve exactly — approve, per-row exclude, backed up, undoable — and a REVIEW
    card for everything else, worked one spelling at a time on /tracking/owners. Before
    the migration it stays the locked card it always was."""
    if "owner_contact_id" not in _cols(conn, "action_items"):
        return []
    if TR.p7_ready(conn):
        return _owner_groups_p7(conn)
    r = conn.execute(
        "SELECT count(DISTINCT owner) AS s, count(*) AS n FROM action_items "
        "WHERE owner_contact_id IS NULL AND COALESCE(owner,'') <> ''").fetchone()
    if not r or not r["n"]:
        return []
    return [{"key": "owners:phase7", "kind": "locked",
             "title": "Action items — owner not matched to a contact",
             "why": f"{r['s']} different spellings across {r['n']} rows. This is Phase 7: "
                    "each spelling gets matched to a real contact, and anything that isn't "
                    "a clean match comes to you. It runs through this same screen.",
             "count": r["n"], "rows": []}]


def _owner_groups_p7(conn) -> list[dict]:
    idx = TR._contact_index(conn)
    name = lambda i: idx["by_id"][i]["name"] if i in idx["by_id"] else f"contact #{i}"
    clean, review = [], {}
    for r in TR.unlinked_owner_rows(conn):
        p = TR.owner_plan(conn, r, idx)
        if p["status"] == "clean":
            after = name(p["owner_id"]) + (" · with " + ", ".join(name(i) for i in p["with_ids"])
                                           if p["with_ids"] else "")
            clean.append({"key": f"action_items:{r['id']}:owner", "table": "action_items",
                          "id": r["id"], "label": _lab(r),
                          "changes": [{"field": "owner_contact_id", "field_label": "owner",
                                       "before": None, "after": p["owner_id"],
                                       "before_label": r["owner"], "after_label": after}],
                          "with": p["with_ids"]})
        elif p["status"] == "review":
            review.setdefault(TR.owner_key(r["owner"]), 0)
            review[TR.owner_key(r["owner"])] += 1
    out = []
    if clean:
        out.append({"key": "owners:clean", "kind": "fix",
                    "title": "Action items — owners that match a contact exactly",
                    "why": "Each owner name matches exactly one verified contact (an internal "
                           "person carried once per account resolves to the item's own "
                           "account). The first person named owns it; anyone else named is "
                           "linked as \"with\"; a bare first name matching yours is you. The "
                           "owner text itself is kept as history. Exclude any row that looks "
                           "wrong.",
                    "count": len(clean), "rows": clean})
    if review:
        out.append({"key": "owners:review", "kind": "review", "href": "/tracking/owners",
                    "title": "Action items — owners to decide",
                    "why": f"{sum(review.values())} rows across {len(review)} spellings need a "
                           "person's judgement: a note that may change the owner, a first "
                           "name, a team, or a name not in contacts. Decide each spelling once; "
                           "it applies to every row with it and is remembered.",
                    "count": len(review), "rows": []})
    return out


# ------------------------------------------------------------------ R4.2c: shape

def _tree_groups(conn) -> list[dict]:
    """The item tree against R4.2's rules: project → item → sub-item, and a sub-item
    lives in its parent's project. Recomputed on every load, so after a promotion the
    groups below it shrink on their own — apply the promotions you want first."""
    if "parent_id" not in _cols(conn, "action_items"):
        return []
    rows = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, parent_id, project_id, status, description{_tcol(conn)} "
        f"FROM action_items")}
    pname = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects")}
    pn = lambda pid: pname.get(pid, f"project #{pid}") if pid is not None else None
    live = lambda r: (r["status"] or "") not in TR.DONE_STATES
    kids: dict[int, list[dict]] = {}
    for r in rows.values():
        if r["parent_id"] in rows:
            kids.setdefault(r["parent_id"], []).append(r)

    def chain(r):                       # ancestors, nearest first; cycle-safe
        out, seen, cur = [], {r["id"]}, r
        while cur["parent_id"] in rows and cur["parent_id"] not in seen:
            cur = rows[cur["parent_id"]]
            seen.add(cur["id"])
            out.append(cur)
        return out

    out = []
    promotable = set()
    if TR.r42_ready(conn):
        cand = [r for r in rows.values() if not r["parent_id"] and live(r)
                and any(live(k) for k in kids.get(r["id"], []))]
        prow, closed = [], []
        for u in sorted(cand, key=lambda r: r["id"]):
            try:
                pv = TR.promote_preview(u["id"])
            except Exception:
                continue                       # already promoted, or not promotable
            promotable.add(u["id"])
            sm = pv["summary"]
            lines = [f"→ new project “{pv['plan']['project']['name']}”"
                     + (f" (in {pv['item']['customer']})" if pv["item"]["customer"] else ""),
                     f"{sm['children_live']} live + {sm['children_done']} finished sub-items "
                     "move in" + (f"; {sm['deeper']} below them come along" if sm["deeper"]
                                  else "")]
            inproj: dict = {}
            for k in kids.get(u["id"], []):
                if k["project_id"] is not None:
                    inproj[pn(k["project_id"])] = inproj.get(pn(k["project_id"]), 0) + 1
            if inproj:
                lines.append("its sub-items are in a project today — they would leave it: "
                             + ", ".join(f"{n} ({c})" for n, c in sorted(inproj.items())))
            if pv["similar"]:
                lines.append("projects already in this account: "
                             + ", ".join(x["name"] for x in pv["similar"]))
            prow.append({"key": f"promote:{u['id']}", "table": "action_items", "id": u["id"],
                         "label": _lab(u),
                         "promote": {"name": pv["plan"]["project"]["name"],
                                     "plan": pv["sha"]},
                         "lines": lines})
        for r in rows.values():
            if not r["parent_id"] and not live(r) and any(live(k) for k in
                                                          kids.get(r["id"], [])):
                n = sum(1 for k in kids[r["id"]] if live(k))
                closed.append({"key": f"closed-umbrella:{r['id']}", "table": "action_items",
                               "id": r["id"], "label": _lab(r),
                               "note": f"{r['status']}, with {n} live sub-item"
                                       f"{'s' if n != 1 else ''}"})
        if prow:
            out.append({"key": "tree:promote", "kind": "promote",
                        "title": "Umbrella items — make each one a project",
                        "why": "These top-level items are live and have live sub-items: they "
                               "are projects in all but name. Each approved row becomes a "
                               "project, its sub-items become the project's items, and the "
                               "umbrella is kept, closed, pointing at it. Nothing starts ticked: "
                               "tick the ones that should become projects. Names are suggestions — rename on the "
                               "Projects tab afterwards. Undo demotes them back.",
                        "count": len(prow), "rows": prow})
        if closed:
            out.append({"key": "tree:closed-umbrellas", "kind": "watch",
                        "title": "Closed items that still have live sub-items",
                        "why": "Either the parent is not really finished (reopen it on the "
                               "tracking page), or the live sub-items have outgrown it "
                               "(detach them). Not something to guess.",
                        "count": len(closed), "rows": sorted(closed, key=lambda r: r["id"])})

    # Three deep or more → flatten into the top-level family.
    deep = []
    for r in rows.values():
        anc = chain(r)
        if len(anc) < 2:
            continue
        top = anc[-1]
        ch = [{"field": "parent_id", "before": r["parent_id"], "after": top["id"]}]
        if r["project_id"] != top["project_id"]:
            ch.append({"field": "project_id", "before": r["project_id"],
                       "after": top["project_id"], "before_label": pn(r["project_id"]),
                       "after_label": pn(top["project_id"])})
        row = {"key": f"depth:{r['id']}", "table": "action_items", "id": r["id"],
               "label": _lab(r), "changes": ch}
        if top["id"] in promotable:
            row["note"] = (f"or promote #{top['id']} above first — this then becomes a "
                           "normal sub-item and drops out of this list")
        deep.append(row)
    if deep:
        out.append({"key": "tree:depth", "kind": "fix",
                    "title": "Sub-items of sub-items — nesting stops at one level",
                    "why": "Each becomes a direct sub-item of its top-level item (and takes "
                           "that item's project). If the top-level item is about to become "
                           "a project, promote it first: its sub-items become the project's "
                           "items and these stay sub-items of theirs.",
                    "count": len(deep), "rows": sorted(deep, key=lambda r: r["id"])})

    # A sub-item whose project differs from its (top-level) parent's.
    follow, adopt, split = [], [], {}
    for pid, ks in kids.items():
        par = rows[pid]
        if par["parent_id"]:
            continue                          # deeper rows are the depth group's job
        diff = [k for k in ks if k["project_id"] != par["project_id"]]
        if not diff:
            continue
        if par["project_id"] is not None:
            for k in diff:
                follow.append({"key": f"follow:{k['id']}", "table": "action_items",
                               "id": k["id"], "label": _lab(k),
                               "changes": [{"field": "project_id", "before": k["project_id"],
                                            "after": par["project_id"],
                                            "before_label": pn(k["project_id"]),
                                            "after_label": pn(par["project_id"])}],
                               "note": f"sub-item of #{pid}"})
            continue
        projs = sorted({k["project_id"] for k in ks}, key=lambda x: (x is None, x or 0))
        named = [x for x in projs if x is not None]
        if len(projs) == 1:                   # every sub-item in the same project
            adopt.append({"key": f"adopt:{pid}", "table": "action_items", "id": pid,
                          "label": _lab(par),
                          "changes": [{"field": "project_id", "before": None,
                                       "after": named[0], "after_label": pn(named[0])}],
                          "note": f"all {len(ks)} of its sub-items are already in it"})
        else:
            split[pid] = (par, ks, named)
    if follow:
        out.append({"key": "tree:follow", "kind": "fix",
                    "title": "Sub-items in a different project from their parent",
                    "why": "A sub-item lives in its parent's project. Each one moves to its "
                           "parent's project.", "count": len(follow),
                    "rows": sorted(follow, key=lambda r: r["id"])})
    if adopt:
        out.append({"key": "tree:adopt", "kind": "fix",
                    "title": "Parents with no project whose sub-items all share one",
                    "why": "The parent joins the project its sub-items are already in.",
                    "count": len(adopt), "rows": sorted(adopt, key=lambda r: r["id"])})
    for pid, (par, ks, named) in sorted(split.items()):
        rws = [{"key": f"split:{pid}:{pid}", "table": "action_items", "id": pid,
                "label": _lab(par),
                "changes": [{"field": "project_id", "before": None, "after": None}],
                "note": "the parent"}]
        for k in sorted(ks, key=lambda k: k["id"]):
            rws.append({"key": f"split:{pid}:{k['id']}", "table": "action_items",
                        "id": k["id"], "label": _lab(k),
                        "changes": [{"field": "project_id", "before": k["project_id"],
                                     "after": None, "before_label": pn(k["project_id"])}]})
        why = (f"#{pid}'s sub-items are split across "
               + " and ".join(pn(x) for x in named)
               + (" and no project" if None in {k["project_id"] for k in ks} else "")
               + ". Pick the one project the whole family belongs to.")
        if pid in promotable:
            why += f" Or promote #{pid} above and it becomes its own project instead."
        out.append({"key": f"tree:split:{pid}", "kind": "decide",
                    "title": f"#{pid} — one project for the whole family",
                    "why": why, "count": len(rws), "rows": rws,
                    "choices": [{"value": str(x), "label": pn(x)} for x in named]})
    return out


def _title_group(conn) -> list[dict]:
    """R4.4c — the assistant's drafted titles waiting for the user. Reviewed on
    /tracking/titles (one at a time, with the full description), not applied from here — a
    "review" card counts toward the badge but has no Apply button."""
    try:
        n = conn.execute("SELECT count(*) FROM item_title_drafts d JOIN action_items a "
                         "ON a.id = d.item_id WHERE d.decided_at IS NULL "
                         "AND COALESCE(a.title,'') = ''").fetchone()[0]
    except Exception:
        return []                  # table not on this DB yet (pre-R4.4c)
    if not n:
        return []
    return [{"key": "titles:review", "kind": "review", "href": "/tracking/titles",
             "title": "Action items — drafted titles to review",
             "why": "Your assistant drafted a short title for each live item. Accept, edit "
                    "or skip each one; nothing is written until you do. The full description "
                    "is on screen beside each draft.", "count": n, "rows": []}]


CHECKS = (_vocab_groups, _slack_group, _link_groups, _rag_group, _owner_group,
          _tree_groups, _title_group)


def _all_groups(conn) -> list[dict]:
    out = []
    for check in CHECKS:
        try:
            out.extend(check(conn))
        except Exception as e:                     # one broken check must not hide the rest
            out.append({"key": f"error:{check.__name__}", "kind": "watch",
                        "title": f"A check failed: {check.__name__}",
                        "why": f"{type(e).__name__}: {e}", "count": 0, "rows": []})
    for g in out:
        g["sha"] = _sha(g["rows"])
        g["actionable"] = (g["kind"] in ("fix", "decide", "link", "promote", "review")
                           and g["count"] > 0)
    return out


ORDER = {"review": 0, "promote": 1, "decide": 2, "fix": 3, "link": 4, "watch": 5,
         "locked": 6}


def data_groups() -> dict:
    """GET /api/hygiene/data. Read-only: never creates a table just by being looked at."""
    conn = _ro()
    if conn is None:
        return {"groups": [], "error": f"database not found: {_db()}"}
    try:
        groups = _shape(conn, _all_groups(conn))
    finally:
        conn.close()
    for g in groups:
        if len(g["rows"]) > SAMPLE_MAX:
            g["rows_truncated"] = len(g["rows"]) - SAMPLE_MAX
            g["rows"] = g["rows"][:SAMPLE_MAX]
    groups.sort(key=lambda g: (g["skipped"], ORDER.get(g["kind"], 9), -g["count"]))
    return {"groups": groups, "by": _by(),
            "open": sum(1 for g in groups if g["actionable"] and not g["skipped"])}


def counts() -> dict:
    """GET /api/hygiene/counts — the nav badge. Open = actionable groups not skipped."""
    d = data_groups()
    try:
        import paths as P
        import hygiene_memory as HM
        mem = HM.open_count() if P.MEMORY_DIR is not None else None
    except Exception:
        mem = None                 # a broken memory module must not hide the data count
    return {"data": d.get("open", 0), "memory": mem, "error": d.get("error")}


# ------------------------------------------------------------------ backup

def backup(label: str) -> str:
    """A verified, gzip'd point-in-time copy of the whole DB, next to it."""
    src = _db()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "group"
    dest = src.parent / f"{src.name}.bak-{stamp}-hygiene-{safe}.gz"
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="hygiene-")
    os.close(fd)
    try:
        s = sqlite3.connect(str(src), timeout=TR.TIMEOUT)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
            ok = d.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            d.close()
            s.close()
        if ok != "ok":
            raise RuntimeError(f"backup failed its integrity check: {ok}")
        with open(tmp, "rb") as fi, gzip.open(dest, "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return str(dest)


# ------------------------------------------------------------------ apply / undo

def _find(conn, key: str) -> dict | None:
    return next((g for g in _shape(conn, _all_groups(conn)) if g["key"] == key), None)


def apply(key: str, sha: str, exclude=None, choice: str = "", by: str = "") -> dict:
    """POST /api/hygiene/apply. Refuses with `why` rather than raising."""
    exclude = set(exclude or [])
    conn = _ro()
    if conn is None:
        return {"ok": False, "why": "the database could not be opened"}
    try:
        g = _find(conn, key)
    finally:
        conn.close()
    if g is None:
        return {"ok": False, "why": "that group is gone — it may already be fixed. Reload."}
    if g["kind"] not in ("fix", "decide", "link", "promote"):
        return {"ok": False, "why": "this group can't be applied here"}
    if g["sha"] != sha:
        return {"ok": False, "why": "these rows changed since the page loaded. Reload and "
                                    "check them again before approving."}
    if g["kind"] == "decide":
        allowed = [c["value"] if isinstance(c, dict) else c for c in g.get("choices") or []]
        if choice not in allowed:
            return {"ok": False, "why": "pick what the value should become first"}
    rows = [r for r in g["rows"] if r["key"] not in exclude]
    if len(g["rows"]) < g["count"]:
        # The page only ever saw SAMPLE_MAX rows; it can't approve ones it never showed.
        return {"ok": False, "why": f"this group is larger than the page can show "
                                    f"({g['count']} rows). Ask your assistant to split it."}
    if not rows:
        return {"ok": False, "why": "every row is excluded — nothing to apply"}
    if g["kind"] == "decide":
        for r in rows:
            for c in r["changes"]:
                # An id column gets an int, so the undo's `IS ?` guard compares like for like.
                c["after"] = (int(choice) if c["field"].endswith("_id") and choice.isdigit()
                              else choice)

    who = by or _by()
    if g["kind"] == "promote":
        return _apply_promote(key, g, rows, who, exclude)
    # The same added columns tracking's own writes add (projects.priority, updated_by…),
    # before the backup so the backup is of the DB exactly as the fix found it.
    TR.ensure_schema()
    bk = backup(key.replace(":", "-"))
    conn = _rw()
    done = []
    try:
        ensure_tables(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for r in rows:
            if "link" in r:
                p, role = r["link"]["path"], r["link"]["role"]
                cur = conn.execute(
                    "INSERT OR IGNORE INTO external_links (local_table, local_id, system, "
                    "role, external_container_id, external_item_id, notes) "
                    "VALUES (?,?,'file',?,'workspace',?,?)",
                    (r["table"], r["id"], role, p, f"hygiene: from the row's own text"))
                if cur.rowcount:
                    done.append({"table": r["table"], "id": r["id"],
                                 "link_id": cur.lastrowid, "path": p, "role": role})
                continue
            has_by = "updated_by" in _cols(conn, r["table"])
            for c in r["changes"]:
                sets = f"{c['field']}=?" + (", updated_by=?" if has_by else "")
                args = [c["after"]] + ([who] if has_by else []) + [r["id"], c["before"]]
                cur = conn.execute(
                    f"UPDATE {r['table']} SET {sets} WHERE id=? AND {c['field']} IS ?", args)
                if cur.rowcount != 1:
                    raise _Moved(f"{r['table']} #{r['id']} {c['field']} changed since the "
                                 "page loaded")
                done.append({"table": r["table"], "id": r["id"], "field": c["field"],
                             "before": c["before"], "after": c["after"]})
            for cid in r.get("with") or []:            # Phase 7: the "with" people
                cur = conn.execute(
                    "INSERT OR IGNORE INTO item_people (item_id, contact_id, role, created_at, "
                    "created_by) VALUES (?,?,'with',?,?)", (r["id"], cid, TR._now(), who))
                if cur.rowcount:
                    done.append({"table": "item_people", "id": r["id"],
                                 "people_id": cur.lastrowid, "contact_id": cid})
        cur = conn.execute(
            "INSERT INTO hygiene_log (area, group_key, title, choice, rows_json, n, backup, "
            "applied_at, applied_by) VALUES ('data',?,?,?,?,?,?,?,?)",
            (key, g["title"], choice or None, json.dumps(done), len(done), bk, TR._now(),
             who))
        log_id = cur.lastrowid
        conn.execute("DELETE FROM hygiene_skip WHERE area='data' AND group_key=?", (key,))
        conn.commit()
    except _Moved as e:
        conn.rollback()
        return {"ok": False, "why": f"nothing was changed — {e}. Reload.", "backup": bk}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "why": f"nothing was changed — {type(e).__name__}: {e}",
                "backup": bk}
    finally:
        conn.close()
    return {"ok": True, "log_id": log_id, "n": len(done), "backup": bk,
            "excluded": len(exclude & {r["key"] for r in g["rows"]})}


class _Moved(Exception):
    pass


def _apply_promote(key: str, g: dict, rows: list[dict], who: str, exclude: set) -> dict:
    """Promotions go through tracking.promote_item one by one — the same code, previews
    and guards as the button on the tracking page — after one backup of the whole DB.
    Each promotion is its own transaction, so a failure part-way leaves the earlier ones
    applied: they are logged (and undoable) and the reply says exactly where it stopped."""
    bk = backup(key.replace(":", "-"))
    done, stop = [], None
    for r in rows:
        try:
            pv = TR.promote_preview(r["id"])
            if pv["sha"] != r["promote"]["plan"]:
                raise ValueError(f"#{r['id']} or its sub-items changed since the page loaded")
            p = TR.promote_item(r["id"], pv["sha"], name=r["promote"]["name"],
                                ptype="other", by=who)
            done.append({"table": "action_items", "id": r["id"], "promoted_to": p["id"],
                         "name": p.get("name")})
        except Exception as e:
            stop = f"#{r['id']}: {e}"
            break
    if done:
        conn = _rw()
        try:
            ensure_tables(conn)
            cur = conn.execute(
                "INSERT INTO hygiene_log (area, group_key, title, choice, rows_json, n, "
                "backup, applied_at, applied_by) VALUES ('data',?,?,?,?,?,?,?,?)",
                (key, g["title"], None, json.dumps(done), len(done), bk, TR._now(), who))
            log_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    else:
        log_id = None
    if stop and not done:
        return {"ok": False, "why": f"nothing was changed — {stop}. Reload.", "backup": bk}
    out = {"ok": True, "log_id": log_id, "n": len(done), "backup": bk,
           "excluded": len(exclude & {r["key"] for r in g["rows"]})}
    if stop:
        out["warning"] = (f"stopped after {len(done)} of {len(rows)} — {stop}. The ones "
                          "before it are applied and in History.")
    return out


def _undo_promote(e, rows: list[dict], by: str) -> dict:
    """Demote each promoted project back, newest first. Anything that would nest three
    deep is flattened under the restored item (the closest the rules allow)."""
    undone = 0
    for r in reversed(rows):
        try:
            pv = TR.demote_preview(r["promoted_to"])
            TR.demote_project(r["promoted_to"], pv["sha"],
                              {n["id"]: "flatten" for n in pv["needs_choice"]}, by=by)
            undone += 1
        except Exception as ex:
            return {"ok": False, "why": f"undid {undone} of {len(rows)}, then stopped at "
                                        f"project #{r['promoted_to']}: {ex}. Demote it from "
                                        f"the Projects tab, or restore {e['backup']}"}
    conn = _rw()
    try:
        conn.execute("UPDATE hygiene_log SET undone_at=?, undone_by=? WHERE id=?",
                     (TR._now(), by, e["id"]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "n": undone}


# ------------------------------------------------------------------ Phase 7b: owner review

# /tracking/owners — every owner spelling that is not an exact match, one at a time. Each
# decision is stored once per spelling (owner_decisions), applied to every unlinked row
# with that spelling, and logged here like any other fix (so it can be undone from the
# log). One DB backup covers a whole review session (30 minutes), not one per decision.
REVIEW_BACKUP_MINUTES = 30
_review_backup = {"path": None, "at": None}


def _session_backup() -> str:
    now = dt.datetime.now()
    b = _review_backup
    if b["path"] and b["at"] and (now - b["at"]).total_seconds() < REVIEW_BACKUP_MINUTES * 60 \
            and os.path.exists(b["path"]):
        return b["path"]
    b["path"], b["at"] = backup("owners-review"), now
    return b["path"]


def _candidates(name: str, customer_id, idx: dict, limit: int = 8) -> list[dict]:
    """Contacts this name might mean — offered, never chosen. Exact name rows first, then
    first-name / surname matches, the item's own account first."""
    key = " ".join((name or "").lower().split())
    toks = key.split()
    seen, out = set(), []

    def add(h, why):
        if h["id"] in seen:
            return
        seen.add(h["id"])
        out.append({"id": h["id"], "name": h["name"], "role": h.get("role"),
                    "company": h.get("company"), "customer_id": h.get("customer_id"),
                    "verified": h.get("verification_status") == "verified", "why": why})
    for h in idx["by_name"].get(key, []) + idx["by_alias"].get(key, []):
        add(h, "same name")
    fuzzy = []
    for h in idx["by_id"].values():
        nt = h["name"].lower().split()
        if toks and nt and (nt[0] == toks[0] or (len(toks) > 1 and nt[-1] == toks[-1])
                            or (len(toks) == 1 and toks[0] in nt)):
            fuzzy.append(h)
    fuzzy.sort(key=lambda h: (h.get("customer_id") != customer_id, h["name"]))
    for h in fuzzy:
        add(h, "similar name")
        if len(out) >= limit:
            break
    return out


def owner_queue() -> dict:
    """GET /api/hygiene/owners — the spellings waiting for a decision, most live rows first."""
    conn = _ro()
    if conn is None:
        return {"ready": False, "items": []}
    try:
        if not TR.p7_ready(conn):
            return {"ready": False, "items": []}
        idx = TR._contact_index(conn)
        cust = {r["id"]: r["short_name"] for r in conn.execute("SELECT id, short_name FROM customers")}
        groups: dict = {}
        for r in TR.unlinked_owner_rows(conn):
            p = TR.owner_plan(conn, r, idx)
            if p["status"] != "review":
                continue
            k = TR.owner_key(r["owner"])
            g = groups.setdefault(k, {"key": k, "example": r["owner"], "rows": [], "count": 0,
                                      "live": 0, "reasons": set(), "accounts": set()})
            g["count"] += 1
            live = (r["status"] or "") not in TR.DONE_STATES
            g["live"] += int(live)
            g["reasons"].update(p["reasons"])
            g["accounts"].add(r["customer_id"])
            g["rows"].append({"id": r["id"], "label": _lab(r), "account": cust.get(r["customer_id"]),
                              "status": r["status"], "live": live})
        items = []
        for g in groups.values():
            parsed = TR.parse_owner(g["example"])
            acct = next(iter(g["accounts"])) if len(g["accounts"]) == 1 else None
            names = []
            for role, n in [("owner", parsed["owner"])] + [("with", w) for w in parsed["with"]]:
                if not n:
                    continue
                res = TR.resolve_person(n, acct, idx)
                names.append({"name": n, "role": role, "resolved": res.get("id"),
                              "candidates": _candidates(n, acct, idx)})
            g["rows"].sort(key=lambda x: (not x["live"], -x["id"]))
            items.append({"key": g["key"], "example": g["example"], "count": g["count"],
                          "live": g["live"], "note": parsed["note"], "names": names,
                          "reasons": sorted(g["reasons"]), "rows": g["rows"][:8],
                          "accounts": sorted(cust.get(a) or "none" for a in g["accounts"])})
        items.sort(key=lambda i: (-i["live"], -i["count"], i["key"]))
        return {"ready": True, "items": items, "spellings": len(items),
                "rows": sum(i["count"] for i in items)}
    finally:
        conn.close()


def decide_owner(key: str, kind: str, owner_id=None, with_ids=None, note: str = "",
                 by: str = "") -> dict:
    """POST /api/hygiene/owners/decide — {key, kind: people | not_person, owner_id,
    with_ids}. Stores the decision and links every unlinked row with this spelling."""
    who = by or _by()
    if kind not in ("people", "not_person"):
        return {"ok": False, "why": "decide people or not_person"}
    conn = _rw()
    try:
        if not TR.p7_ready(conn):
            return {"ok": False, "why": "run the Phase 7 migration first"}
        idx = TR._contact_index(conn)
        rows = [r for r in TR.unlinked_owner_rows(conn) if TR.owner_key(r["owner"]) == key]
        if not rows:
            return {"ok": False, "why": "no unlinked rows have that spelling any more — reload"}
        owner_name, with_names, picks = None, [], {}
        if kind == "people":
            ids = [int(owner_id)] + [int(w) for w in (with_ids or []) if str(w).strip()]
            for i in ids:
                if i not in idx["by_id"]:
                    return {"ok": False, "why": f"contact #{i} not found (merged or archived?)"}
            if len(set(ids)) != len(ids):
                return {"ok": False, "why": "the same person is in twice"}
            owner_name = idx["by_id"][ids[0]]["name"]
            with_names = [idx["by_id"][i]["name"] for i in ids[1:]]
            # The pick wins wherever the name alone is not one person on that account.
            picks = {idx["by_id"][i]["name"]: i for i in ids}
        conn.execute(
            "INSERT INTO owner_decisions (spelling, example, kind, owner_name, with_names, "
            "pick_json, note, decided_at, decided_by) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(spelling) DO UPDATE SET example=excluded.example, kind=excluded.kind, "
            "owner_name=excluded.owner_name, with_names=excluded.with_names, "
            "pick_json=excluded.pick_json, note=excluded.note, decided_at=excluded.decided_at, "
            "decided_by=excluded.decided_by",
            (key, rows[0]["owner"], kind, owner_name, json.dumps(with_names),
             json.dumps(picks), (note or "").strip()[:400] or None, TR._now(), who))
        conn.commit()
        if kind == "not_person":
            return {"ok": True, "n": 0, "kind": kind, "rows": len(rows)}
        idx = TR._contact_index(conn)
        plans = [(r, TR.owner_plan(conn, r, idx)) for r in rows]
    finally:
        conn.close()
    bk = _session_backup()
    conn = _rw()
    done = []
    try:
        ensure_tables(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        for r, p in plans:
            if p["status"] != "clean":
                continue                                   # reported back, not forced
            cur = conn.execute("UPDATE action_items SET owner_contact_id=?, updated_by=? "
                               "WHERE id=? AND owner_contact_id IS NULL",
                               (p["owner_id"], who, r["id"]))
            if cur.rowcount != 1:
                continue
            done.append({"table": "action_items", "id": r["id"], "field": "owner_contact_id",
                         "before": None, "after": p["owner_id"]})
            for cid in p["with_ids"]:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO item_people (item_id, contact_id, role, created_at, "
                    "created_by) VALUES (?,?,'with',?,?)", (r["id"], cid, TR._now(), who))
                if cur.rowcount:
                    done.append({"table": "item_people", "id": r["id"],
                                 "people_id": cur.lastrowid, "contact_id": cid})
        log_id = None
        if done:
            cur = conn.execute(
                "INSERT INTO hygiene_log (area, group_key, title, choice, rows_json, n, backup, "
                "applied_at, applied_by) VALUES ('data',?,?,?,?,?,?,?,?)",
                (f"owners:review:{key[:80]}", f"Owner decided: “{rows[0]['owner'][:80]}”",
                 owner_name, json.dumps(done), len(done), bk, TR._now(), who))
            log_id = cur.lastrowid
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {"ok": False, "why": f"nothing was changed — {type(e).__name__}: {e}"}
    finally:
        conn.close()
    linked = sum(1 for d in done if d["table"] == "action_items")
    left = [{"id": r["id"], "reasons": p["reasons"]} for r, p in plans if p["status"] != "clean"]
    return {"ok": True, "kind": kind, "linked": linked, "rows": len(rows), "left": left,
            "log_id": log_id, "backup": bk}


def undo(log_id, by: str = "") -> dict:
    """POST /api/hygiene/undo — put back exactly what one apply changed, or nothing."""
    try:
        log_id = int(log_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "no such log entry"}
    conn = _rw()
    try:
        ensure_tables(conn)
        e = conn.execute("SELECT * FROM hygiene_log WHERE id=? AND area='data'",
                         (log_id,)).fetchone()
        if not e:
            return {"ok": False, "why": "no such log entry"}
        if e["undone_at"]:
            return {"ok": False, "why": f"already undone at {e['undone_at']}"}
        rows = json.loads(e["rows_json"] or "[]")
        conn.commit()
        if rows and "promoted_to" in rows[0]:
            conn.close()
            return _undo_promote(e, rows, by or _by())
        conn.execute("BEGIN IMMEDIATE")
        for r in reversed(rows):
            if "keep_key" in r:                       # "Suggest again": nothing to put back
                continue
            if "people_id" in r:
                cur = conn.execute(
                    "UPDATE item_people SET removed_at=?, removed_by=? WHERE id=? AND "
                    "removed_at IS NULL", (TR._now(), by or _by(), r["people_id"]))
                if cur.rowcount != 1:
                    raise _Moved(f"the \"with\" person on action_items #{r['id']} was "
                                 "already removed")
                continue
            if "link_id" in r:
                cur = conn.execute(
                    "DELETE FROM external_links WHERE id=? AND local_table=? AND local_id=? "
                    "AND system='file' AND external_item_id=?",
                    (r["link_id"], r["table"], r["id"], r["path"]))
                if cur.rowcount != 1:
                    raise _Moved(f"the link on {r['table']} #{r['id']} was already removed "
                                 "or changed")
                continue
            cur = conn.execute(
                f"UPDATE {r['table']} SET {r['field']}=? WHERE id=? AND {r['field']} IS ?",
                (r["before"], r["id"], r["after"]))
            if cur.rowcount != 1:
                raise _Moved(f"{r['table']} #{r['id']} {r['field']} was edited after the "
                             "fix")
        conn.execute("UPDATE hygiene_log SET undone_at=?, undone_by=? WHERE id=?",
                     (TR._now(), by or _by(), log_id))
        conn.commit()
    except _Moved as ex:
        conn.rollback()
        return {"ok": False, "why": f"nothing was undone — {ex}. Fix that row by hand, "
                                    f"or restore the backup: {e['backup']}"}
    except Exception as ex:
        conn.rollback()
        return {"ok": False, "why": f"nothing was undone — {type(ex).__name__}: {ex}"}
    finally:
        conn.close()
    return {"ok": True, "n": len(rows)}


def skip(key: str, sha: str) -> dict:
    """Hide these rows until NEW rows appear in the group (editing a skipped row no longer
    brings it back). Row-less cards come back when their count grows. Stored in the DB so
    the assistant sees it too."""
    conn = _rw()
    try:
        ensure_tables(conn)
        conn.commit()
        g = _find(conn, key)
        if g is None:
            return {"ok": False, "why": "that group is gone — reload"}
        prev = _skips(conn).get(key) or {}
        keys = set(prev.get("rows") or set())
        keys |= ({r["key"] for r in g["rows"]} if g["rows"] else {f"count:{g['count']}"})
        conn.execute("INSERT OR REPLACE INTO hygiene_skip (area, group_key, sha, skipped_at, "
                     "row_keys) VALUES ('data',?,?,?,?)",
                     (key, sha or g["sha"], TR._now(), json.dumps(sorted(keys))))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def keep(row_key: str, by: str = "") -> dict:
    """POST /api/hygiene/keep {key: "promote:520"} — "Keep as an item": never suggest
    promoting it again. Logged like a fix, so Hygiene → log can undo it (Suggest again)."""
    if not re.fullmatch(r"promote:\d+", row_key or ""):
        return {"ok": False, "why": "only a promote suggestion can be kept"}
    iid = int(row_key.split(":")[1])
    conn = _rw()
    try:
        ensure_tables(conn)
        conn.commit()
        if row_key in _kept(conn):
            return {"ok": True, "already": True}
        r = conn.execute("SELECT title, description FROM action_items WHERE id=?",
                         (iid,)).fetchone()
        if not r:
            return {"ok": False, "why": "no such item"}
        conn.execute(
            "INSERT INTO hygiene_log (area, group_key, title, choice, rows_json, n, backup, "
            "applied_at, applied_by) VALUES ('data','keep:tree:promote',?,NULL,?,1,NULL,?,?)",
            (f"Kept #{iid} as an item — {_lab(dict(r))[:70]}",
             json.dumps([{"keep_key": row_key, "table": "action_items", "id": iid}]),
             TR._now(), by or _by()))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def unskip(key: str) -> dict:
    conn = _rw()
    try:
        ensure_tables(conn)
        conn.execute("DELETE FROM hygiene_skip WHERE area='data' AND group_key=?", (key,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def log(limit: int = 50) -> dict:
    """GET /api/hygiene/log — newest first."""
    conn = _ro()
    if conn is None:
        return {"log": []}
    try:
        rs = conn.execute(
            "SELECT id, group_key, title, choice, n, backup, applied_at, applied_by, "
            "undone_at, undone_by, rows_json FROM hygiene_log WHERE area='data' "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.OperationalError:
        return {"log": []}
    finally:
        conn.close()
    out = []
    for r in rs:
        d = {k: r[k] for k in r.keys() if k != "rows_json"}
        d["rows"] = json.loads(r["rows_json"] or "[]")[:SAMPLE_MAX]
        out.append(d)
    return {"log": out}


# ------------------------------------------------------------------ CLI

def _main(argv: list[str]) -> int:
    if argv[:1] == ["--log"]:
        for e in log()["log"]:
            print(f"#{e['id']} {e['applied_at']} {e['group_key']} n={e['n']}"
                  + (f" UNDONE {e['undone_at']}" if e["undone_at"] else ""))
        return 0
    d = data_groups()
    if d.get("error"):
        print(d["error"])
        return 1
    for g in d["groups"]:
        flag = " (skipped)" if g["skipped"] else ""
        print(f"[{g['kind']:6}] {g['count']:4}  {g['title']}{flag}")
    print(f"open: {d['open']}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

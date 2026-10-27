#!/usr/bin/env python3
"""
tracking.py — the data layer for the mt tracking page.

Reads and writes `action_items` and `projects` in the canonical workspace DB.
Everything here is defensive on purpose: `mt web` imports this lazily and a
broken/locked/missing DB must degrade to an empty page, never a 500.

House rules baked in (see TRACKING-PLAN.md):
  * schema changes are ALTER TABLE ... ADD COLUMN only
  * never DELETE a row
  * read functions return empty/None instead of raising; write functions may raise
  * ensure_schema() is called only from POST handlers, never on a read

THE VALIDATION RULE, in one paragraph. `status`, `priority` and `report_scope` have
canonical vocabularies below. A write may set one only to a canonical value — that is
what stops a new outlier spelling. But 739 rows already hold legacy values (`closed`,
`completed`, `medium`, `jason_action` ...) and forcing a cleanup just to fix a typo in
a description would be obnoxious, so an UNCHANGED value always passes even when it is
legacy. You cannot introduce a bad value; you are never blocked by an old one. That is
"validate forward + assisted cleanup" (Sep 22 2026). The bulk cleanup of the
legacy values is the hygiene queue, which is a later phase and NOT in this file yet.

CLI (for development against a COPY of the DB):
    python3 tracking.py --stats
    python3 tracking.py --vocab
    python3 tracking.py --drift
    python3 tracking.py --migrate /tmp/copy.db
    python3 tracking.py "search term"
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import datetime as dt
import functools
import json
from pathlib import Path

import paths as P

HERE = Path(__file__).resolve().parent
WORKSPACE = P.WORKSPACE

# Module-level so tests (and the CLI --migrate flag) can point at a copy.
DB_PATH = Path(os.environ.get("MT_TRACKING_DB") or P.DB)

TIMEOUT = 5.0
LIMIT_DEFAULT = 400
LIMIT_MAX = 2000
Q_MAX = 200

# --------------------------------------------------------------- vocabularies
#
# Canonical values. A write may only set one of these (or leave the field unchanged).
# Ordered for display — the UI renders them in this order, so it reads as a lifecycle
# rather than an alphabetical jumble.

STATUS = ["open", "waiting", "blocked", "done", "wont_do"]
PRIORITY = ["low", "normal", "high", "critical"]
SCOPE = ["update", "personal", "internal", "watch", "n/a", "unset"]

# What each report_scope means. Fixed by the memory file `feedback_report_scope_flag.md`
# and NOT reinterpreted here — the UI shows these as help text so the meaning travels
# with the picker instead of living only in the owner's head.
SCOPE_HELP = {
    "update":   "Belongs in the weekly update — customer-facing, a hard date, or "
                "something a reader of the channel/ticket needs.",
    "personal": "Your own follow-up. Real work, tracked, but not for the global update.",
    "internal": "Internal-only and sensitive. NEVER goes in a report, even flagged.",
    "watch":    "Another party owns it and is managing it — you are tracking it, not "
                "actioning it. Not an Open Item; it belongs in a labelled watch section "
                "at the end of a report, if it appears at all.",
    "n/a":      "Closed or done; scope is irrelevant.",
    "unset":    "Not yet triaged.",
}

# What each field is FOR, and what actually changes downstream when you set it. Shown
# in the edit sheet behind a "Field guide" toggle. Written as consequences, not
# definitions — "what happens if I change this" is the question the form cannot answer
# on its own. Every claim here was checked against the code or the data on Sep 22 2026.
FIELD_HELP = {
    "title":
        "The headline, as `XXX - Short title summary`: XXX is the account's code (set per "
        "account in config.json; INT for internal work), then a short topic summary — ideally the "
        "whole thing under 70 characters. It is what the list, the Gantt, the slide export "
        "and reports show. Leave it empty and they fall back to the description's first "
        "sentence.",
    "description":
        "The wording that actually gets read. If this item is scoped `update`, this text "
        "is what lands in the Jira comment and the Slack post — so write it for that "
        "reader, not as a note to yourself. House style in the existing rows is "
        "`CUSTOMER -- THE ACTION IN CAPS. Then the context.` The full text is always "
        "kept; only the list view clamps it to two lines. Add each update to the end of "
        "the description with `+ Add entry`, which stamps it `[date time — you]`. Say "
        "where it happened (a Slack channel, meeting, ticket) and end with `NEXT:` when "
        "there is one.",
    "owner":
        "Who owns the next move. Picking from the list links the row to a contact, which "
        "is what makes per-person views trustworthy — and it is the only way to set an "
        "owner without adding to the free-text tail. Typing a name instead leaves the row "
        "unlinked. Internal people who are linked can be @-mentioned in the Slack weekly "
        "update from their stored Slack id; customer-side owners are not in your Slack "
        "and stay as plain text.",
    "customer_id":
        "Which account this belongs to. It decides which weekly update the item can "
        "appear in — each account posts to its own TAM epic and Slack channel — and which "
        "monthly recap picks it up. Leave it empty for internal work; it will then appear "
        "in no customer report at all.",
    "status":
        "`open` live work · `waiting` you need someone else to come back to you · "
        "`blocked` you cannot proceed at all · `done` / `wont_do` finished. "
        "Setting a finished state drops the row out of the Hide-done list and stops it "
        "counting as overdue. The waiting-vs-blocked distinction is the one that matters "
        "for reporting: a pending reply only belongs in a weekly update if it is actually "
        "holding something up.",
    "priority":
        "Ordering, not deadline — that is what Due date is for. The useful move is "
        "downward: dropping a genuine long-horizon item to `low` stops a stalest-first "
        "sort from making months of correct silence look like a lapse.",
    "report_scope":
        "Who the item is for, and the single biggest control over whether it reaches a "
        "report. A weekly-update draft pulls `update` and `unset` and nothing else, so "
        "this field alone decides visibility. Each value's meaning is shown below.",
    "due_date":
        "YYYY-MM-DD. Drives the overdue count and the overdue tag, and only counts while "
        "the status is unfinished — closing an item clears it without editing the date. "
        "For a recurring obligation, roll this date forward rather than raising a fresh "
        "item each cycle.",
    "project_id":
        "Groups the item under a project or RAIL. It drives the Project filter and the "
        "project sidebar's open / done / overdue roll-ups on this page; the weekly update "
        "and the published tracker do not read it yet.",
    "parent_id":
        "Makes this a sub-item of another item (`#392`). One level only: the parent must "
        "be top-level, and an item that has sub-items cannot become one. A sub-item lives "
        "in its parent's project, so Project is parked while this is set.",
    "item_type":
        "`task` the default · `story` a piece of a project that will become a Jira story "
        "· `milestone` a date with no length (an SBR, a cert expiry), drawn as a diamond on "
        "the Gantt — its end is its start.",
    "start_date":
        "When the work is planned to start. The plan is Start → End; Due stays the "
        "promise. Setting a first Start with no End fills End from Due.",
    "end_date":
        "When the work is planned to finish. Cannot be before Start. If it lands after "
        "Due, the plan is already late against the promise — that is worth seeing.",
    "source":
        "Where the item came from. Free text and unvalidated — it is carrying provenance, "
        "ticket ids and file paths all at once, which is why it is the messiest column in "
        "the table. One real consequence: the meetings list marks a recording as "
        "`tracked` by matching `source LIKE 'meeting:%'`. The COLON is what matters — "
        "`meeting:<slug>` registers (a space after the colon is fine, and case is "
        "ignored), but a prose form like `Meeting 2026-07-31 SBR planning call` does not.",
}

# Legacy values still in the data, and what the hygiene queue will eventually propose.
# Read-only knowledge: nothing in this file rewrites a row. `None` means the drift is
# real but ambiguous and needs the user's call rather than a guess.
LEGACY = {
    "status":       {"closed": "done", "completed": "done", "in_progress": "open"},
    "priority":     {"medium": "normal"},
    # `watch` was promoted to a real value on Sep 22 2026 — it was never drift. The
    # memory file has carried the rule since Jul 31 2026 ("customer-owned watching-only
    # items go in a labelled section at the end, not in his action list"); it simply had
    # no column value until now. `external` collapsed into it.
    "report_scope": {"report": "update", "weekly": "update", "jason_action": "personal",
                     "none": "n/a", "external": "watch"},
}

FIELD_VOCAB = {"status": STATUS, "priority": PRIORITY, "report_scope": SCOPE}

# --- projects ----------------------------------------------------------------
#
# `projects.type` was carrying two unrelated ideas at once: a work category (`upgrade`)
# and a storage medium (`google_sheet`, `confluence_rail`). The medium never needed a
# column — it is derivable from `rail_url`, which is why `project_medium()` below reads
# the URL instead. That frees `type` to mean only what kind of work this is.
#
# Existing rows keep their old values and are shown as legacy, exactly like action_items:
# validate forward, clean up when you choose to. Nothing is rewritten.
PROJECT_TYPE = ["rail", "upgrade", "lifecycle", "migration", "deployment", "renewal",
                "other"]
PROJECT_STATUS = ["active", "paused", "complete"]
PROJECT_LEGACY = {
    "type": {"confluence_rail": "rail", "google_sheet": "rail"},
    "status": {"low_priority": "active"},      # ... and set priority=low
}
PROJECT_VOCAB = {"type": PROJECT_TYPE, "status": PROJECT_STATUS,
                 "priority": PRIORITY}

PROJECT_FIELD_HELP = {
    "name":
        "What the project is called. It labels the Project picker on every action item, "
        "so write it the way you would say it out loud.",
    "type":
        "What KIND of work this is — not where it is tracked. Where it lives is read off "
        "the link below, so a RAIL on a Google Sheet and a RAIL in Confluence are both "
        "just `rail`.",
    "status":
        "`active` live · `paused` deliberately parked · `complete` finished. Archiving is "
        "separate and is how you get a project out of the list without deleting it.",
    "priority":
        "How much attention it deserves while active. This is what `low_priority` was "
        "doing in the status column, which is why it moved here.",
    "rail_url":
        "The tracker this project lives in — a Confluence RAIL, a Google Sheet, a Monday "
        "board. The medium shown on the card is read from this URL, so paste the real "
        "link rather than describing it.",
    "customer_id":
        "Which account owns the project. Leave empty for internal work.",
    "notes":
        "Your working record. Nothing auto-edits it.",
}

# Everything that counts as "finished", canonical and legacy together. Used by the
# Hide-done filter and by the overdue count — an item that is closed is not overdue.
DONE_STATES = ("done", "closed", "completed", "wont_do")

# A bulk write touches many rows at once against the canonical 68 MB DB, so it is capped.
# 500 is well above any real cleanup slice and well below "I clicked the wrong thing".
BULK_MAX = 500

# Columns ensure_schema() adds. Nullable, no defaults — adding them rewrites no rows.
ITEM_NEW_COLUMNS = [
    # The normalized owner. `owner` free text stays exactly as it is for history and
    # display; new writes through a contact picker set both. PLAN.md has named this
    # the real fix for the 216-spelling owner problem since Sep 15.
    ("owner_contact_id", "INTEGER"),
    # 'mt' or the assistant's name — both are writers now, and last-write-wins
    # is a lot easier to reason about when you can see who wrote last.
    ("updated_by", "TEXT"),
]
PROJECT_NEW_COLUMNS = [
    ("updated_at", "TEXT"),
    ("updated_by", "TEXT"),
    ("priority", "TEXT"),
    # Same reasoning as contacts: 109 action_items point at a project, so removing one
    # would orphan them. Archive hides it from the list and the picker; it never deletes.
    ("archived_at", "TEXT"),
]

# R4.1. `action_items` had no indexes at all (Oct 9 2026). At ~900 rows SQL is not the
# cost, but these are free, additive, and every filter on the page hits one of them.
# Created by ensure_schema() — so on a POST, never on a read — and IF NOT EXISTS.
ITEM_INDEXES = ["status", "due_date", "project_id", "parent_id", "customer_id"]

# --- R4.2: the data model behind projects, predecessors and the Gantt ----------------
#
# NOT applied by ensure_schema(). Every other column this module adds rides along on the
# first save; these do not, because R4.2 also puts the first TRIGGERS on the canonical DB
# and a broken trigger fails every writer, the assistant included. They go in only through
# migrate_r42(): run against a copy first, then `--migrate-r42 --live`, which takes a
# named backup before it touches anything. Until then every R4.2 feature is simply absent
# (the code checks for `rev`), so the page works on either side of the migration.

ITEM_TYPE = ["task", "story", "milestone"]

ITEM_R42_COLUMNS = [
    ("start_date", "TEXT"),          # the plan ...
    ("end_date", "TEXT"),            # ... while due_date stays the commitment
    ("completed_at", "TEXT"),        # set/cleared by trg_ai_rev when status crosses done
    ("item_type", "TEXT DEFAULT 'task'"),
    ("promoted_to_project_id", "INTEGER"),
    ("rev", "INTEGER DEFAULT 0"),    # bumped by trg_ai_rev on every UPDATE, by any writer
]
PROJECT_R42_COLUMNS = [
    ("origin_item_id", "INTEGER"),   # the umbrella item it was promoted from
    ("demoted_to_item_id", "INTEGER"),
    ("target_date", "TEXT"),
]

# Fields whose every change becomes one item_events row. completed_at and rev are
# derived and updated_at/updated_by are bookkeeping, so they are not logged.
EVENT_FIELDS = [
    "description", "owner", "owner_contact_id", "due_date", "status", "priority",
    "report_scope", "customer_id", "project_id", "parent_id", "source", "start_date",
    "end_date", "item_type", "promoted_to_project_id",
]

_NOW_SQL = "strftime('%Y-%m-%d %H:%M:%S','now','localtime')"   # matches _now()
_DONE_SQL = "(" + ", ".join(f"'{s}'" for s in DONE_STATES) + ")"

# `by` is only trusted when the writer also touched updated_by/updated_at in the same
# statement — mt always does. A writer that does neither (a bare sqlite3 UPDATE) is
# logged with by=NULL rather than blamed on whoever wrote the row last.
_BY_SQL = ("CASE WHEN NEW.updated_by IS NOT OLD.updated_by "
           "OR NEW.updated_at IS NOT OLD.updated_at THEN NEW.updated_by END")

R42_TABLES = [
    """CREATE TABLE IF NOT EXISTS item_events (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        old TEXT,
        new TEXT,
        at TEXT NOT NULL,
        by TEXT,
        rev INTEGER)""",
    """CREATE TABLE IF NOT EXISTS action_item_deps (
        id INTEGER PRIMARY KEY,
        pred_id INTEGER NOT NULL REFERENCES action_items(id),
        succ_id INTEGER NOT NULL REFERENCES action_items(id),
        kind TEXT NOT NULL DEFAULT 'FS',
        lag_days INTEGER NOT NULL DEFAULT 0,
        met_at TEXT,
        met_note TEXT,
        created_at TEXT NOT NULL,
        created_by TEXT,
        removed_at TEXT,
        removed_by TEXT,
        CHECK (pred_id <> succ_id))""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_action_item_deps_live "
    "ON action_item_deps(pred_id, succ_id) WHERE removed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_action_item_deps_pred ON action_item_deps(pred_id)",
    "CREATE INDEX IF NOT EXISTS ix_action_item_deps_succ ON action_item_deps(succ_id)",
    "CREATE INDEX IF NOT EXISTS ix_item_events_item ON item_events(item_id)",
]


def _r42_triggers() -> list[tuple[str, str]]:
    """(name, CREATE TRIGGER sql). Plain INSERTs, plus the one UPDATE in trg_ai_rev."""
    out = []
    for f in EVENT_FIELDS:
        out.append((f"trg_ai_ev_{f}", f"""
CREATE TRIGGER IF NOT EXISTS trg_ai_ev_{f} AFTER UPDATE OF {f} ON action_items
WHEN OLD.{f} IS NOT NEW.{f}
BEGIN
  INSERT INTO item_events (item_id, field, old, new, at, by, rev)
  VALUES (NEW.id, '{f}', OLD.{f}, NEW.{f}, {_NOW_SQL}, {_BY_SQL}, COALESCE(OLD.rev, 0));
END"""))
    out.append(("trg_ai_ev_created", f"""
CREATE TRIGGER IF NOT EXISTS trg_ai_ev_created AFTER INSERT ON action_items
BEGIN
  INSERT INTO item_events (item_id, field, old, new, at, by, rev)
  VALUES (NEW.id, '*created', NULL, NEW.status, {_NOW_SQL}, NEW.updated_by, 0);
END"""))
    # Every UPDATE by any writer bumps rev — that is what makes the optimistic check work
    # against the assistant's sqlite3 as well as the other mt window. The same statement keeps
    # completed_at honest: stamped when status moves into a finished state, cleared when
    # it reopens. A writer that sets rev itself is left alone (the WHEN), which also
    # stops this from re-firing on its own UPDATE even with recursive_triggers on.
    out.append(("trg_ai_rev", f"""
CREATE TRIGGER IF NOT EXISTS trg_ai_rev AFTER UPDATE ON action_items
WHEN NEW.rev IS OLD.rev
BEGIN
  UPDATE action_items SET
    rev = COALESCE(OLD.rev, 0) + 1,
    completed_at = CASE
      WHEN COALESCE(NEW.status,'') IN {_DONE_SQL}
           AND COALESCE(OLD.status,'') NOT IN {_DONE_SQL}
        THEN COALESCE(NEW.completed_at, {_NOW_SQL})
      WHEN COALESCE(NEW.status,'') NOT IN {_DONE_SQL}
           AND COALESCE(OLD.status,'') IN {_DONE_SQL}
        THEN NULL
      ELSE NEW.completed_at END
  WHERE id = NEW.id;
END"""))
    return out


def r42_ready(conn) -> bool:
    return "rev" in _cols(conn, "action_items")


def migrate_r42(db_path) -> list[str]:
    """Apply the R4.2 schema to `db_path`. Additive and idempotent: ADD COLUMN, CREATE …
    IF NOT EXISTS. One transaction, so a failure leaves the DB exactly as it was."""
    conn = sqlite3.connect(str(db_path), timeout=TIMEOUT)
    done: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table, newcols in (("action_items", ITEM_R42_COLUMNS),
                               ("projects", PROJECT_R42_COLUMNS)):
            have = _cols(conn, table)
            for col, typ in newcols:
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    done.append(f"{table}.{col}")
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        for sql in R42_TABLES:
            conn.execute(sql)
        for name, sql in _r42_triggers():
            conn.execute(sql)
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        done += sorted(after - before)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return done


def rollback_r42_triggers(db_path) -> list[str]:
    """The one-line rollback the plan asks for: drop the triggers, keep the data. Columns
    and tables stay (never drop a column); without triggers they simply stop updating."""
    conn = sqlite3.connect(str(db_path), timeout=TIMEOUT)
    try:
        names = [n for n, _ in _r42_triggers()]
        dropped = [r[0] for r in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
            f"({', '.join('?' * len(names))})", names)]
        for n in dropped:
            conn.execute(f"DROP TRIGGER IF EXISTS {n}")
        conn.commit()
        return dropped
    finally:
        conn.close()


# --- R4.4c: a title on every item ---------------------------------------------------
#
# Oct 13: "the whole description is very handy but that is lots of words and we're
# just using the beginning of the line … this title is a must." Every live item's
# description was over 200 characters (avg ~1,800), so every label anywhere was a chopped
# first line. `title` is the headline (and what Jira's Summary / Monday's item name map
# to later); `description` stays the full working record.
#
# Same posture as R4.2: NOT applied by ensure_schema(). One migration (copy first, then
# --live, which takes a named backup) adds the column, the title change-log trigger, and
# item_title_drafts — the assistant's drafted titles waiting for the user to accept, edit or
# skip on /tracking/titles. Nothing writes a title without that decision. Until then, and for
# any item without one, display falls back to the first sentence of the description.

TITLE_MAX = 120

# Title convention: "XXX - Short title summary", XXX = the account's code from config.json
# (accounts.<slug>.code). Items with no account use INT unless they are plainly about one
# customer.
ACCOUNT_CODE = {s: a["code"] for s, a in P.ACCOUNTS.items() if a["code"]}
INTERNAL_CODE = "INT"
ITEM_R44C_COLUMNS = [("title", "TEXT")]
R44C_TABLES = [
    """CREATE TABLE IF NOT EXISTS item_title_drafts (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES action_items(id),
        draft TEXT NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT,
        decided_at TEXT,
        decided_by TEXT,
        decision TEXT,               -- accepted | edited | skipped
        final TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_item_title_drafts_item ON item_title_drafts(item_id)",
]


def _r44c_triggers() -> list[tuple[str, str]]:
    f = "title"
    return [(f"trg_ai_ev_{f}", f"""
CREATE TRIGGER IF NOT EXISTS trg_ai_ev_{f} AFTER UPDATE OF {f} ON action_items
WHEN OLD.{f} IS NOT NEW.{f}
BEGIN
  INSERT INTO item_events (item_id, field, old, new, at, by, rev)
  VALUES (NEW.id, '{f}', OLD.{f}, NEW.{f}, {_NOW_SQL}, {_BY_SQL}, COALESCE(OLD.rev, 0));
END""")]


def r44c_ready(conn) -> bool:
    return "title" in _cols(conn, "action_items")


def _load_title_drafts(conn, drafts: list[dict], by: str = P.ASSISTANT) -> dict:
    """Queue drafts [{id, title}]. Skips an item that is missing, already titled, or
    already has an undecided draft — so loading the same file twice is harmless."""
    added, skipped = 0, []
    now = _now()
    for d in drafts or []:
        try:
            iid = int(d["id"])
            t = _clean_title(d.get("title"))
        except Exception as e:
            skipped.append({"id": d.get("id"), "why": str(e)})
            continue
        row = conn.execute("SELECT title FROM action_items WHERE id=?", (iid,)).fetchone()
        if not row or not t:
            skipped.append({"id": iid, "why": "no such item" if not row else "empty"})
            continue
        if row[0]:
            skipped.append({"id": iid, "why": "already has a title"})
            continue
        if conn.execute("SELECT 1 FROM item_title_drafts WHERE item_id=? AND decided_at "
                        "IS NULL", (iid,)).fetchone():
            skipped.append({"id": iid, "why": "a draft is already waiting"})
            continue
        conn.execute("INSERT INTO item_title_drafts (item_id, draft, created_at, created_by)"
                     " VALUES (?, ?, ?, ?)", (iid, t, now, by))
        added += 1
    return {"added": added, "skipped": skipped}


def migrate_r44c(db_path, drafts: list[dict] | None = None) -> list[str]:
    """Apply R4.4c to `db_path` (needs R4.2 — the trigger writes item_events), and queue
    `drafts` if given. One transaction: a failure leaves the DB exactly as it was."""
    conn = sqlite3.connect(str(db_path), timeout=TIMEOUT)
    conn.row_factory = sqlite3.Row
    done: list[str] = []
    try:
        if "rev" not in _cols(conn, "action_items"):
            raise RuntimeError("R4.2 is not on this DB — run --migrate-r42 first")
        conn.execute("BEGIN IMMEDIATE")
        have = _cols(conn, "action_items")
        for col, typ in ITEM_R44C_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE action_items ADD COLUMN {col} {typ}")
                done.append(f"action_items.{col}")
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        for sql in R44C_TABLES:
            conn.execute(sql)
        for _, sql in _r44c_triggers():
            conn.execute(sql)
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        done += sorted(after - before)
        if drafts:
            r = _load_title_drafts(conn, drafts)
            done.append(f"{r['added']} title drafts queued"
                        + (f", {len(r['skipped'])} skipped" if r["skipped"] else ""))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return done


def rollback_r44c_triggers(db_path) -> list[str]:
    conn = sqlite3.connect(str(db_path), timeout=TIMEOUT)
    try:
        dropped = []
        for n, _ in _r44c_triggers():
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                            (n,)).fetchone():
                conn.execute(f"DROP TRIGGER IF EXISTS {n}")
                dropped.append(n)
        conn.commit()
        return dropped
    finally:
        conn.close()


# --- R4.5b: soft leases ----------------------------------------------------------------
#
# While the Gantt tray holds unsaved edits, the window holds a lease on those items
# (renewed every few minutes, 10-minute expiry, released on Save / Discard / closing the
# window). Nothing is LOCKED: the assistant checks `tracking.py --leases` before writing an item
# and warns instead of writing silently; an expired lease is ignored, so a crashed window
# strands nothing. Rows are ephemeral bookkeeping, so they are deleted, not tombstoned.

LEASE_MINUTES = 10
R45B_TABLES = [
    """CREATE TABLE IF NOT EXISTS edit_leases (
        item_id INTEGER NOT NULL,
        holder TEXT NOT NULL,
        label TEXT,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (item_id, holder))""",
]


def r45b_ready(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                             "name='edit_leases'").fetchone())


def migrate_r45b(db_path) -> list[str]:
    conn = sqlite3.connect(str(db_path), timeout=TIMEOUT)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        for sql in R45B_TABLES:
            conn.execute(sql)
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.commit()
        return sorted(after - before)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_leases(holder: str, ids: list, label: str = "Gantt") -> dict:
    """Replace `holder`'s leases with `ids` (an empty list releases them)."""
    holder = (holder or "").strip()[:80]
    if not holder:
        raise ValueError("a lease needs a holder")
    conn = _connect_w()
    try:
        if not r45b_ready(conn):
            return {"ok": True, "ready": False, "held": 0}
        until = (dt.datetime.now() + dt.timedelta(minutes=LEASE_MINUTES)).strftime(
            "%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM edit_leases WHERE holder=? OR expires_at < ?",
                     (holder, _now()))
        ids = sorted({int(i) for i in (ids or [])})[:BULK_MAX]
        conn.executemany("INSERT INTO edit_leases (item_id, holder, label, expires_at, "
                         "created_at) VALUES (?,?,?,?,?)",
                         [(i, holder, (label or "")[:60], until, _now()) for i in ids])
        conn.commit()
        return {"ok": True, "ready": True, "held": len(ids), "until": until}
    finally:
        conn.close()


def active_leases(ids=None) -> list[dict]:
    """Unexpired leases, optionally only on `ids` — what the assistant checks first."""
    conn = _connect()
    if conn is None:
        return []
    try:
        if not r45b_ready(conn):
            return []
        rows = [dict(r) for r in conn.execute(
            "SELECT l.item_id, l.holder, l.label, l.expires_at, a.title, a.description "
            "FROM edit_leases l LEFT JOIN action_items a ON a.id = l.item_id "
            "WHERE l.expires_at >= ? ORDER BY l.item_id", (_now(),))]
        if ids:
            want = {int(i) for i in ids}
            rows = [r for r in rows if r["item_id"] in want]
        return rows
    finally:
        conn.close()


def _clean_title(v) -> str | None:
    """One line, single spaces, at most TITLE_MAX characters. Empty means none."""
    t = " ".join(str(v or "").split())
    if not t:
        return None
    if len(t) > TITLE_MAX:
        raise ValueError(f"title is {len(t)} characters — keep it to {TITLE_MAX}")
    return t


_SENTENCE_END = re.compile(r"(?<=[.!?])\s|\s[—–]\s|\s--\s")


def auto_title(desc: str, n: int = 90) -> str:
    """The fallback label when an item has no title: the description's first sentence
    (or clause before a dash), cut at `n` characters. What every label was before R4.4c."""
    t = " ".join((desc or "").split())
    if not t:
        return ""
    m = _SENTENCE_END.search(t, 12)       # not inside a short "ACME --" prefix
    head = t[:m.start()].rstrip(".") if m and m.start() <= n else t
    return head if len(head) <= n else head[:n - 1].rstrip() + "…"


def label(r: dict) -> str:
    return (r.get("title") or "").strip() or auto_title(r.get("description") or "")


def title_drafts(limit: int = 25, offset: int = 0, decided: bool = False) -> dict:
    """GET /api/tracking/titles — drafts waiting for a decision (or, with decided, the
    ones already made, newest first), each with its item's full description: the user needs
    the detail to judge whether the item belongs, not just the headline."""
    out = {"drafts": [], "total": 0, "done": 0, "ready": False}
    conn = _connect()
    if conn is None:
        return out
    try:
        if not r44c_ready(conn):
            return out
        out["ready"] = True
        cmap = _customer_map(conn)
        projects = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM projects")}
        # A draft for an item that already has a title (set in its sheet, or by the assistant)
        # is not shown — accepting it would overwrite that title.
        where = ("d.decided_at IS NOT NULL" if decided
                 else "d.decided_at IS NULL AND COALESCE(a.title,'') = ''")
        out["total"] = conn.execute(
            f"SELECT count(*) FROM item_title_drafts d JOIN action_items a "
            f"ON a.id = d.item_id WHERE {where}").fetchone()[0]
        out["done"] = conn.execute(
            "SELECT count(*) FROM item_title_drafts WHERE decided_at IS NOT NULL").fetchone()[0]
        order = "d.decided_at DESC" if decided else (
            "a.customer_id IS NULL, a.customer_id, a.project_id IS NULL, a.project_id, "
            "COALESCE(a.parent_id, a.id), a.parent_id IS NOT NULL, a.id")
        rows = conn.execute(
            f"SELECT d.id AS draft_id, d.draft, d.decision, d.final, d.decided_at, a.id, "
            f"a.title, a.description, a.status, a.owner, a.due_date, a.customer_id, "
            f"a.project_id, a.parent_id, a.rev FROM item_title_drafts d "
            f"JOIN action_items a ON a.id = d.item_id WHERE {where} ORDER BY {order} "
            f"LIMIT ? OFFSET ?", (max(1, min(int(limit), 200)), max(0, int(offset))))
        for r in rows:
            d = dict(r)
            d["customer"] = cmap.get(d["customer_id"])
            d["project"] = projects.get(d["project_id"])
            out["drafts"].append(d)
        return out
    finally:
        conn.close()


def decide_title(draft_id: int, decision: str, final: str = "", by: str = "mt") -> dict:
    """accept → the draft becomes the title; edit → `final` does; skip → nothing is
    written (the item keeps its fallback label, and can be titled later in its sheet)."""
    if decision not in ("accept", "edit", "skip"):
        raise ValueError("decision must be accept, edit or skip")
    conn = _connect_w()
    try:
        d = conn.execute("SELECT * FROM item_title_drafts WHERE id=?",
                         (int(draft_id),)).fetchone()
        if not d:
            raise ValueError("no such draft")
        if d["decided_at"]:
            raise ValueError(f"already decided ({d['decision']})")
        now_title = (conn.execute("SELECT title FROM action_items WHERE id=?",
                                  (d["item_id"],)).fetchone() or [None])[0]
        if now_title:
            # Titled elsewhere since the draft was queued: that title wins.
            conn.execute("UPDATE item_title_drafts SET decided_at=?, decided_by=?, "
                         "decision='superseded', final=? WHERE id=?",
                         (_now(), by, now_title, int(draft_id)))
            conn.commit()
            raise ValueError(f"#{d['item_id']} already has a title ({now_title!r}) — "
                             "kept it; this draft is closed")
    finally:
        conn.close()
    title = None
    if decision == "accept":
        title = d["draft"]
    elif decision == "edit":
        title = _clean_title(final)
        if not title:
            raise ValueError("an edited title cannot be empty — skip it instead")
        if title == d["draft"]:
            decision = "accept"
    item = None
    if title:
        item = update_item(d["item_id"], {"title": title}, by=by)
    conn = _connect_w()
    try:
        conn.execute("UPDATE item_title_drafts SET decided_at=?, decided_by=?, decision=?, "
                     "final=? WHERE id=?", (_now(), by, {"accept": "accepted",
                     "edit": "edited", "skip": "skipped"}[decision], title, int(draft_id)))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "decision": decision, "title": title, "item": item}


def title_draft_count() -> int:
    conn = _connect()
    if conn is None:
        return 0
    try:
        if not r44c_ready(conn):
            return 0
        return conn.execute(
            "SELECT count(*) FROM item_title_drafts d JOIN action_items a ON a.id = "
            "d.item_id WHERE d.decided_at IS NULL AND COALESCE(a.title,'') = ''"
            ).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


ITEM_BASE_COLUMNS = [
    "id", "customer_id", "project_id", "description", "owner", "due_date", "status",
    "priority", "source", "created_at", "updated_at", "report_scope", "parent_id",
]
ITEM_ALL_COLUMNS = (ITEM_BASE_COLUMNS + [c for c, _ in ITEM_NEW_COLUMNS]
                    + [c for c, _ in ITEM_R42_COLUMNS] + [c for c, _ in ITEM_R44C_COLUMNS])

# `source` is writable but deliberately unvalidated — it is doing four jobs at once
# (provenance, ticket ids, file paths, prose) and untangling it is the hygiene phase.
ITEM_WRITABLE = {
    "description", "owner", "owner_contact_id", "due_date", "status", "priority",
    "report_scope", "customer_id", "project_id", "parent_id", "source",
    "start_date", "end_date", "item_type",          # R4.2 — only once migrated in
    "title",                                        # R4.4c — likewise
}

PROJECT_WRITABLE = {"name", "type", "rail_url", "status", "notes", "customer_id",
                    "priority"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- file links ---------------------------------------------------------------
#
# A DB row points at a local file through `external_links`, the table built for exactly
# this and which carried essentially no file rows until now — one, a template workbook
# linked by hand in Aug 2026, which is what confirmed the convention was already right.
#
#   system                = 'file'
#   external_container_id = the root alias ('workspace')
#   external_item_id      = the workspace-relative path
#   role                  = what the file IS to this row
#
# `external_container_id` is what makes the memory/ root possible later: those files live
# outside the workspace entirely, so a bare path could never resolve on its own.
FILE_ROLES = ["source_meeting", "summary", "recap", "weekly_update", "attachment",
              "reference"]

LINKABLE_TABLES = ("action_items", "projects", "weekly_updates", "sbr_meetings")

# Paths sitting inside free text, for the "we found this, link it?" suggestion.
_PATH_RE = re.compile(r"((?:Reports|Customers)/[A-Za-z0-9._/-]+?\.md)")
_DIR_RE = re.compile(r"(Customers/[A-Za-z0-9._-]+/meetings/[A-Za-z0-9._-]+)/?")


def _docs():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import docs as D
    return D


def links_for(table: str, local_id: int, system: str = "") -> list[dict]:
    """Links on one row. `system` filters ('file' for just the local ones)."""
    conn = _connect()
    if conn is None:
        return []
    try:
        sql = ("SELECT id, local_table, local_id, system, role, external_container_id, "
               "external_item_id, notes, created_at FROM external_links "
               "WHERE local_table=? AND local_id=?")
        args = [str(table), int(local_id)]
        if system:
            sql += " AND system=?"
            args.append(system)
        return [{k: r[k] for k in r.keys()}
                for r in conn.execute(sql + " ORDER BY id", args)]
    except Exception:
        return []
    finally:
        conn.close()


def _role_for(path: str) -> str:
    p = (path or "").lower()
    if "/monthly-recaps/" in p:
        return "recap"
    if "/weekly-updates/" in p:
        return "weekly_update"
    if p.endswith("/summary.md"):
        return "source_meeting"
    return "reference"


def add_link(table: str, local_id: int, path: str, role: str = "reference",
             root: str = "workspace", notes: str = "") -> dict:
    """Point a row at a local file. Idempotent — the table already has a unique index on
    (local_table, local_id, system, role, external_item_id), so re-adding is a no-op."""
    table = str(table or "").strip()
    if table not in LINKABLE_TABLES:
        raise ValueError(f"cannot link from {table!r}")
    role = (role or "reference").strip()
    if role not in FILE_ROLES:
        raise ValueError(f"role must be one of: {', '.join(FILE_ROLES)}")
    path = (path or "").strip().lstrip("/")
    if not path:
        raise ValueError("a path is required")
    # The file must be one the editor can actually open, or the link is a dead end.
    if _docs().resolve(root, path) is None:
        raise ValueError("that file is not one the editor can open")

    conn = _connect_w()
    try:
        if not conn.execute(f"SELECT 1 FROM {table} WHERE id=?",
                            (int(local_id),)).fetchone():
            raise ValueError(f"no {table} #{local_id}")
        conn.execute(
            "INSERT OR IGNORE INTO external_links "
            "(local_table, local_id, system, role, external_container_id, "
            " external_item_id, notes) VALUES (?,?,'file',?,?,?,?)",
            (table, int(local_id), role, root, path,
             (notes or "").strip()[:400] or None))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "path": path, "role": role}


def remove_link(link_id: int) -> dict:
    """Unlink. This DELETEs the row, a deliberate and narrow exception to the never-delete
    rule: a link is a pointer, not a record of work or of a person, and a wrong one has to
    be removable or the feature is unusable. Confined to `system='file'` — jira and monday
    links carry provenance and are refused here."""
    conn = _connect_w()
    try:
        row = conn.execute("SELECT system FROM external_links WHERE id=?",
                           (int(link_id),)).fetchone()
        if not row:
            raise ValueError("no such link")
        if row["system"] != "file":
            raise ValueError("only file links can be removed here (this one is "
                             f"{row['system']!r} — remove it where it was created)")
        conn.execute("DELETE FROM external_links WHERE id=?", (int(link_id),))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def suggest_links(table: str, local_id: int) -> list[dict]:
    """Paths already sitting in this row's free text, offered as links.

    118 action_items smuggled a path into `source` or `description` before there was
    anywhere else to put one. This reads them back out rather than making the user retype
    them, and proposes a role from the shape of the path. Nothing is written."""
    if table != "action_items":
        return []
    row = get_item(local_id)
    if not row:
        return []
    blob = " ".join(str(row.get(k) or "") for k in ("source", "description"))
    have = {l["external_item_id"] for l in links_for(table, local_id, "file")}
    try:
        D = _docs()
    except Exception:
        return []
    out, seen = [], set()
    for m in _PATH_RE.finditer(blob):
        p = m.group(1)
        if p in have or p in seen or D.resolve("workspace", p) is None:
            continue
        seen.add(p)
        out.append({"path": p, "role": _role_for(p)})
    # A bare meeting directory is a path too — the summary inside it is what is meant.
    for m in _DIR_RE.finditer(blob):
        p = m.group(1) + "/summary.md"
        if p in have or p in seen or D.resolve("workspace", p) is None:
            continue
        seen.add(p)
        out.append({"path": p, "role": "source_meeting"})
    return out


# --------------------------------------------------------------------- plumbing

def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _connect(write: bool = False):
    """Open the DB. Returns None if it cannot be opened (missing / locked / corrupt)."""
    p = Path(DB_PATH)
    if not p.exists():
        return None
    try:
        if write:
            conn = sqlite3.connect(str(p), timeout=TIMEOUT)
        else:
            conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=TIMEOUT)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _connect_w():
    """Open for writing, raising a clear message on failure."""
    p = Path(DB_PATH)
    if not p.exists():
        raise RuntimeError(f"tracking DB not found: {p}")
    conn = sqlite3.connect(str(p), timeout=TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(conn, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except Exception:
        return []


def _select_list(have: list[str], want: list[str], alias: str) -> str:
    """Select every contract column, substituting NULL for ones not yet migrated in."""
    return ", ".join(f"{alias}.{c}" if c in have else f"NULL AS {c}" for c in want)


# ---------------------------------------------------------------------- schema

def ensure_schema() -> list[str]:
    """Add any missing columns. Idempotent. Returns the names it added (empty if none)."""
    conn = _connect(write=True)
    if conn is None:
        return []
    added: list[str] = []
    try:
        for table, newcols in (("action_items", ITEM_NEW_COLUMNS),
                               ("projects", PROJECT_NEW_COLUMNS)):
            have = _cols(conn, table)
            if not have:
                continue
            for col, typ in newcols:
                if col in have:
                    continue
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                added.append(f"{table}.{col}")
        have = _cols(conn, "action_items")
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='action_items'")}
        for col in ITEM_INDEXES:
            name = f"ix_action_items_{col}"
            if col in have and name not in idx:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON action_items({col})")
                added.append(f"index {name}")
        if added:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return added
    finally:
        conn.close()
    return added


# ---------------------------------------------------------------------- vocab

def _entry_author() -> str:
    """The name the "+ Add entry" stamp signs with: the first word of `owner` in
    config.json. A first name is what the existing hand-written entries use, and it keeps
    the stamp short; an assistant appending to the same description signs with its own."""
    name = (getattr(P, "OWNER", "") or "").strip()
    return name.split()[0] if name else ""


def vocab() -> dict:
    """Everything the page needs to build its pickers, so nothing is hardcoded in JS."""
    return {
        "status": STATUS,
        "priority": PRIORITY,
        "report_scope": SCOPE,
        "scope_help": SCOPE_HELP,
        "field_help": FIELD_HELP,
        "legacy": LEGACY,
        "sorts": available_sorts(),
        "item_type": ITEM_TYPE,
        "r42": _is_r42(),
        "r44c": _has_titles(),
        "p7": _has_p7(),
        "title_max": TITLE_MAX,
        "entry_author": _entry_author(),
        "account_codes": ACCOUNT_CODE,
        "internal_code": INTERNAL_CODE,
        "due_filters": DUE_FILTERS,
        "dep_filters": DEP_FILTERS,
        "project": {"type": PROJECT_TYPE, "status": PROJECT_STATUS,
                    "priority": PRIORITY, "legacy": PROJECT_LEGACY,
                    "field_help": PROJECT_FIELD_HELP},
    }


def is_legacy(field: str, value) -> bool:
    return bool(value) and value in (LEGACY.get(field) or {})


def _validate(field: str, value, current=None):
    """Canonical values always pass. An unchanged value passes even when legacy.
    Anything else raises — that is the whole point of this module."""
    allowed = FIELD_VOCAB.get(field)
    if allowed is None:
        return value
    if value in allowed:
        return value
    if current is not None and value == current:
        return value                       # untouched legacy value, not our business
    nice = ", ".join(allowed)
    if is_legacy(field, value):
        raise ValueError(
            f"{field}={value!r} is a legacy value being retired. Pick one of: {nice}")
    raise ValueError(f"{field} must be one of: {nice} (got {value!r})")


def _validate_date(value, field: str = "due_date"):
    if _empty(value):
        return None
    v = str(value).strip()
    if not DATE_RE.match(v):
        raise ValueError(f"{field} must be YYYY-MM-DD (got {value!r})")
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{field} is not a real date: {value!r}")
    return v


_validate_due = _validate_date          # the name the rest of the module grew up with


# ---------------------------------------------------------------------- reads

def customers() -> list[dict]:
    """id -> name/short_name, for the customer picker. Never raises."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT id, name, short_name FROM customers ORDER BY name").fetchall()
        return [{"id": r["id"], "name": r["name"], "short_name": r["short_name"]}
                for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _customer_map(conn) -> dict:
    try:
        return {r["id"]: (r["short_name"] or r["name"])
                for r in conn.execute("SELECT id, name, short_name FROM customers")}
    except Exception:
        return {}


def _decorate(d: dict, cmap: dict) -> dict:
    d["customer"] = cmap.get(d.get("customer_id"))
    d["legacy_fields"] = [f for f in FIELD_VOCAB if is_legacy(f, d.get(f))]
    return d


# --- sorting (R4.1) -------------------------------------------------------------
#
# Server-side, because paging is: a browser sort would only ever sort the 20 rows it was
# sent. Each sort has a natural first direction — soonest due, highest priority, newest
# id — and `dir` flips it. Rows with no value for the key (no due date) sort LAST in
# both directions, so "due, descending" does not open on a page of undated rows. Ties
# always break newest-id-first, so the order is stable from one request to the next.
#
#   key: (label, column, natural direction, how the value ranks)
SORTS = {
    "due":      ("Due", "due_date", "asc", "text"),
    "start":    ("Start", "start_date", "asc", "text"),
    "priority": ("Priority", "priority", "asc", "priority"),
    "status":   ("Status", "status", "asc", "status"),
    "title":    ("Title", "description", "asc", "title"),
    "updated":  ("Updated", "updated_at", "desc", "text"),
    "id":       ("Newest", "id", "desc", "int"),
}
SORT_DEFAULT = "id"
_PRIORITY_RANK = {v: i for i, v in enumerate(reversed(PRIORITY))}   # critical first
_STATUS_RANK = {v: i for i, v in enumerate(STATUS)}                  # lifecycle order


def _is_r42() -> bool:
    conn = _connect()
    if conn is None:
        return False
    try:
        return r42_ready(conn)
    finally:
        conn.close()


def _has_p7() -> bool:
    conn = _connect()
    if conn is None:
        return False
    try:
        return p7_ready(conn)
    finally:
        conn.close()


def _has_titles() -> bool:
    conn = _connect()
    if conn is None:
        return False
    try:
        return r44c_ready(conn)
    finally:
        conn.close()


def available_sorts(have: list[str] | None = None) -> list[dict]:
    """The sorts the DB can actually answer. `start` needs R4.2's start_date column and
    is left off until it exists, rather than offered and silently ignored."""
    if have is None:
        conn = _connect()
        have = _cols(conn, "action_items") if conn is not None else []
        if conn is not None:
            conn.close()
    return [{"key": k, "label": s[0], "dir": s[2]} for k, s in SORTS.items()
            if s[1] in have or s[1] == "id"]


def _rank(kind: str, v):
    """A comparable value, or None when the row has nothing to sort on."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if kind == "priority":
        return _PRIORITY_RANK.get(v, len(_PRIORITY_RANK))
    if kind == "status":
        return _STATUS_RANK.get(v, len(_STATUS_RANK))
    if kind == "title":
        return str(v).strip().lower()
    if kind == "int":
        return int(v)
    return str(v)


def _order(rows: list[dict], sort: str, direction: str | None,
           prefix: dict | None = None) -> list[dict]:
    """Families sort as a unit: a parent is placed by its own key and its children follow
    it, themselves sorted by the same key — the convention gen_tracker.py already uses,
    so a sort never rips a child away from its parent. A child whose parent the filters
    did not match stands on its own (the page still shows `↳ #N`). Each returned row
    carries `depth` (0 = top of its family in this view) and `kids` (children in view)."""
    label, col, natural, kind = SORTS.get(sort) or SORTS[SORT_DEFAULT]
    flip = -1 if (direction or natural) == "desc" else 1

    def cmp(a, b):
        if prefix:                              # R4.3 steps: earlier step first, always
            pa, pb = prefix.get(a["id"], 0), prefix.get(b["id"], 0)
            if pa != pb:
                return -1 if pa < pb else 1
        ra, rb = _rank(kind, a.get(col)), _rank(kind, b.get(col))
        if ra is None and rb is not None:
            return 1
        if rb is None and ra is not None:
            return -1
        if ra is not None and ra != rb:
            return flip * (-1 if ra < rb else 1)
        return b["id"] - a["id"]

    key = functools.cmp_to_key(cmp)
    by_id = {r["id"]: r for r in rows}
    kids: dict[int, list[dict]] = {}
    roots = []
    for r in rows:
        p = r.get("parent_id")
        if p is not None and p in by_id and p != r["id"]:
            kids.setdefault(p, []).append(r)
        else:
            roots.append(r)

    out, seen = [], set()

    def walk(r, depth):
        if r["id"] in seen:                     # a cycle in old data must not hang this
            return
        seen.add(r["id"])
        ch = sorted(kids.get(r["id"], []), key=key)
        out.append(dict(r, depth=depth, kids=len(ch)))
        for c in ch:
            walk(c, depth + 1)

    for r in sorted(roots, key=key):
        walk(r, 0)
    # Rows only reachable through a cycle (no root above them) still have to appear.
    for r in sorted((r for r in rows if r["id"] not in seen), key=key):
        walk(r, 0)
    return out


# --- "Mine", due windows, owner groups (R4.1) -------------------------------------

# Quick views and the Due filter. Every one excludes finished work: a closed item is not
# overdue and is not "due this week" in any sense that helps.
DUE_FILTERS = [
    {"key": "overdue", "label": "Overdue"},
    {"key": "week", "label": "Due this week"},
    {"key": "next14", "label": "Due in the next 14 days"},
    {"key": "none", "label": "No due date"},
]


def _me(conn) -> tuple[list[int], list[str]]:
    """Who "Mine" means: the `owner` in config.json (default: your macOS full name).

    A row is yours when it is linked to that contact, or — for the unlinked tail — when
    its free-text owner is exactly that name. Exact on purpose: `Alex Sample` is a
    different person from Alex Example, so nothing here prefix-matches, and multi-owner
    strings (`Alex Example / Sam Sample`) are shared work, not "mine"."""
    name = (getattr(P, "OWNER", "") or "").strip()
    if not name:
        return [], []
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM contacts WHERE lower(trim(name)) = lower(?) "
            "AND merged_into IS NULL", (name,))]
    except Exception:
        ids = []
    return ids, [name.lower()]


def _mine_sql(me) -> tuple[str, list]:
    ids, names = me
    parts, args = [], []
    if ids:
        parts.append(f"a.owner_contact_id IN ({', '.join('?' * len(ids))})")
        args += ids
    if names:
        parts.append(f"(a.owner_contact_id IS NULL AND lower(trim(COALESCE(a.owner,''))) "
                     f"IN ({', '.join('?' * len(names))}))")
        args += names
    if not parts:
        return "0", []
    return "(" + " OR ".join(parts) + ")", args


def _week_bounds(today: dt.date | None = None) -> tuple[str, str]:
    """Monday..Sunday of the current week, ISO."""
    t = today or dt.date.today()
    mon = t - dt.timedelta(days=t.weekday())
    return mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()


def _live_sql() -> tuple[str, list]:
    return (f"COALESCE(a.status,'') NOT IN ({', '.join('?' * len(DONE_STATES))})",
            list(DONE_STATES))


_ID_TOKEN = re.compile(r"#?\d{1,9}")


def _ids_in(q: str) -> tuple[list[int], bool]:
    """The item ids a search asks for, when it is nothing but ids: `#671`, `#671 #787`,
    `671, 787`. Returns (ids, tagged) — tagged when every one carries a `#`. Any other
    word in the search makes it a text search: ([], False)."""
    toks = [t for t in re.split(r"[\s,]+", q or "") if t]
    if not toks or len(toks) > 200 or not all(_ID_TOKEN.fullmatch(t) for t in toks):
        return [], False
    return sorted({int(t.lstrip("#")) for t in toks}), all(t.startswith("#") for t in toks)


def _item_where(cmap: dict, q: str = "", customer: str | None = None,
                status: str | None = None, priority: str | None = None,
                scope: str | None = None, owner: str | None = None,
                hide_done: bool = False, project: str | None = None,
                mine: bool = False, due: str | None = None, me=None,
                titles: bool = False):
    """The WHERE clause every item query shares — the page, its total, and the id list
    behind "select all matching" must agree exactly, or the count on screen lies about
    what a bulk write will touch. Returns (sql, args), or None when the filter can match
    nothing at all (an unknown account, a malformed project or owner).

    `owner` is `c:<contact id>` (a linked person), `s:<text>` (an unlinked spelling —
    only rows with no contact link), or a bare string (exact `owner`, the old form).
    `project` is a project id or `none`. `due` is a DUE_FILTERS key. `mine` needs `me`,
    from _me()."""
    where, args = ["1=1"], []

    q = (q or "").strip()[:Q_MAX]
    qids, tagged = _ids_in(q)
    text_sql = ("(lower(a.description) LIKE ? OR lower(COALESCE(a.owner,'')) "
                "LIKE ? OR lower(COALESCE(a.source,'')) LIKE ?" + (
                    " OR lower(COALESCE(a.title,'')) LIKE ?)" if titles else ")"))
    nt = 4 if titles else 3
    if qids:
        # `#671 #787` is a lookup by id and nothing else. A bare `552` could equally be
        # item 552 or "PROJ-552" in the text, so it matches either.
        id_sql = f"a.id IN ({', '.join('?' * len(qids))})"
        if tagged:
            where.append(id_sql)
            args += qids
        else:
            where.append(f"({id_sql} OR {text_sql})")
            args += qids + [f"%{q.lower()}%"] * nt
    elif q:
        where.append(text_sql)
        args += [f"%{q.lower()}%"] * nt

    if customer:
        if customer == "none":
            where.append("a.customer_id IS NULL")
        else:
            ids = [i for i, s in cmap.items() if s == customer]
            if not ids:
                return None
            where.append(f"a.customer_id IN ({', '.join('?' * len(ids))})")
            args += ids

    for col, val in (("status", status), ("priority", priority),
                     ("report_scope", scope)):
        if val:
            where.append(f"a.{col} = ?")
            args.append(val)

    if owner:
        if owner.startswith("c:"):
            try:
                where.append("a.owner_contact_id = ?")
                args.append(int(owner[2:]))
            except ValueError:
                return None
        elif owner.startswith("s:"):
            where.append("a.owner_contact_id IS NULL AND COALESCE(a.owner,'') = ?")
            args.append(owner[2:])
        else:
            where.append("a.owner = ?")
            args.append(owner)

    if mine:
        sql, a = _mine_sql(me or ([], []))
        where.append(sql)
        args += a

    if project:
        if project == "none":
            where.append("a.project_id IS NULL")
        else:
            try:
                where.append("a.project_id = ?")
                args.append(int(project))
            except ValueError:
                return None

    if due:
        live, la = _live_sql()
        today = dt.date.today().isoformat()
        has = "COALESCE(a.due_date,'') <> ''"
        if due == "overdue":
            where += [has, "a.due_date < ?", live]
            args += [today] + la
        elif due == "week":
            mon, sun = _week_bounds()
            where += [has, "a.due_date BETWEEN ? AND ?", live]
            args += [mon, sun] + la
        elif due == "next14":
            end = (dt.date.today() + dt.timedelta(days=14)).isoformat()
            where += [has, "a.due_date BETWEEN ? AND ?", live]
            args += [today, end] + la
        elif due == "none":
            where.append("COALESCE(a.due_date,'') = ''")
        else:
            return None

    # An explicit status filter wins: asking for `closed` while Hide done is on should
    # show closed rows, not nothing. So does asking for an item by id.
    if hide_done and not status and not qids:
        where.append(
            f"COALESCE(a.status,'') NOT IN ({', '.join('?' * len(DONE_STATES))})")
        args += list(DONE_STATES)
    return " AND ".join(where), args


def list_items(q: str = "", customer: str | None = None, status: str | None = None,
               priority: str | None = None, scope: str | None = None,
               owner: str | None = None, limit: int = LIMIT_DEFAULT,
               hide_done: bool = False, offset: int = 0, **kw) -> list[dict]:
    """`q` matches description/owner/source, case-insensitive substring.
    `customer` is a short_name ('acme') or 'none' for rows with no customer_id.
    `status`/`priority`/`scope` are exact matches; `owner` is described in _item_where().
    `hide_done` drops every finished state at once, canonical and legacy — that is the
    "stop showing me all 745" switch, and it is not the same as picking a single status.
    Newest first unless `sort` says otherwise. `limit`/`offset` page the result; see
    page_items() for the total alongside it, and for the other keyword filters."""
    return page_items(q=q, customer=customer, status=status, priority=priority,
                      scope=scope, owner=owner, limit=limit, hide_done=hide_done,
                      offset=offset, **kw)["items"]


def page_items(q: str = "", customer: str | None = None, status: str | None = None,
               priority: str | None = None, scope: str | None = None,
               owner: str | None = None, limit: int = LIMIT_DEFAULT,
               hide_done: bool = False, offset: int = 0,
               with_ids: bool = False, project: str | None = None,
               mine: bool = False, due: str | None = None,
               sort: str = SORT_DEFAULT, direction: str | None = None,
               dep: str | None = None) -> dict:
    """One page of items plus the TOTAL that matched, so the page can say "21–40 of 506"
    without fetching 506 rows. The full list is ~2 KB a row — 1.5 MB for all 753 — which
    is why this pages on the server rather than in the browser.

    Ordering happens in Python over a slim projection of every matching row (id, parent,
    the sort column — a few KB even for all 920), because families have to stay together
    and SQL cannot page a tree. Then only the page's ids are fetched in full.

    `with_ids` also returns every matching id in display order (a few KB), which is what
    "select all 506 matching" needs without shipping 506 descriptions to get them."""
    empty = {"items": [], "total": 0, "ids": [] if with_ids else None}
    conn = _connect()
    if conn is None:
        return empty
    try:
        have = _cols(conn, "action_items")
        if not have:
            return empty
        cmap = _customer_map(conn)
        w = _item_where(cmap, q, customer, status, priority, scope, owner, hide_done,
                        project=project, mine=mine, due=due,
                        me=_me(conn) if mine else None, titles="title" in have)
        if w is None:
            return empty
        where, args = w

        try:
            n = max(1, min(int(limit), LIMIT_MAX))
        except (TypeError, ValueError):
            n = LIMIT_DEFAULT
        try:
            off = max(0, int(offset))
        except (TypeError, ValueError):
            off = 0

        sort = sort if sort in SORTS and SORTS[sort][1] in have + ["id"] else SORT_DEFAULT
        direction = direction if direction in ("asc", "desc") else None
        col = SORTS[sort][1]
        slim = conn.execute(
            f"SELECT a.id, a.parent_id, a.{col} AS {col} FROM action_items a "
            f"WHERE {where}", args).fetchall() if col != "id" else conn.execute(
            f"SELECT a.id, a.parent_id FROM action_items a WHERE {where}",
            args).fetchall()
        slim = [{k: r[k] for k in r.keys()} for r in slim]
        if sort == "title" and "title" in have:   # R4.4c: order by what is displayed
            heads = {r["id"]: label(dict(r)) for r in conn.execute(
                f"SELECT a.id, a.title, a.description FROM action_items a WHERE {where}",
                args)}
            for r in slim:
                r["description"] = heads.get(r["id"], r.get("description"))

        # R4.3 — the sequence. Flags are computed for every link in the DB (a few hundred
        # rows at most), then used three ways: the dep filter, steps, and the row chips.
        seq = _seq(conn)
        flags = _seq_flags(seq) if seq else {}
        if dep:
            if dep not in DEP_FILTERS_KEYS:
                return empty
            keep = {i for i, f in flags.items() if _dep_match(dep, f)}
            slim = [r for r in slim if r["id"] in keep]
        steps = None
        if seq and project and str(project).isdigit():
            steps = _steps({r["id"] for r in slim}, seq)
        ordered = _order(slim, sort, direction, prefix=steps)
        total = len(ordered)
        window = ordered[off:off + n]

        sel = _select_list(have, ITEM_ALL_COLUMNS, "a")
        full = {}
        ids = [r["id"] for r in window]
        for i in range(0, len(ids), 500):          # SQLite's bound-parameter ceiling
            chunk = ids[i:i + 500]
            for r in conn.execute(
                    f"SELECT {sel} FROM action_items a "
                    f"WHERE a.id IN ({', '.join('?' * len(chunk))})", chunk):
                full[r["id"]] = {k: r[k] for k in r.keys()}
        withs = people_for(conn, ids) if p7_ready(conn) else {}
        items = []
        for w_ in window:
            d = full.get(w_["id"])
            if d is None:
                continue
            d["depth"], d["kids"] = w_["depth"], w_["kids"]
            f = flags.get(d["id"])
            if f:
                d.update(after=f["after"], unblocks=f["unblocks"],
                         blocked_by=f["blocked_by"], conflicts=f["conflicts"])
            if steps:
                d["step"] = steps.get(d["id"], 1)
            d["with"] = withs.get(d["id"], [])
            items.append(_decorate(d, cmap))
        out = {"items": items, "total": total, "ids": None,
               "sort": sort, "dir": direction or SORTS[sort][2], "steps": bool(steps)}
        if with_ids:
            out["ids"] = [r["id"] for r in ordered]
        return out
    except Exception:
        return empty
    finally:
        conn.close()


def get_item(iid: int) -> dict | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        have = _cols(conn, "action_items")
        if not have:
            return None
        sel = _select_list(have, ITEM_ALL_COLUMNS, "a")
        row = conn.execute(f"SELECT {sel} FROM action_items a WHERE a.id=?",
                           (int(iid),)).fetchone()
        if not row:
            return None
        d = _decorate({k: row[k] for k in row.keys()}, _customer_map(conn))
        d["with"] = people_for(conn, [d["id"]]).get(d["id"], []) if p7_ready(conn) else []
        d["child_count"] = (conn.execute(
            "SELECT count(*) FROM action_items WHERE parent_id=?",
            (int(iid),)).fetchone() or [0])[0]
        return d
    except Exception:
        return None
    finally:
        conn.close()


def children_of(iid: int) -> list[dict]:
    """The sub-items of one item, oldest first — the item sheet's Sub-items list."""
    conn = _connect()
    if conn is None:
        return []
    try:
        have = _cols(conn, "action_items")
        cols = [c for c in ("id", "title", "description", "status", "item_type", "owner",
                            "due_date", "start_date", "end_date") if c in have]
        return [dict(r, label=label(dict(r))) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM action_items WHERE parent_id=? ORDER BY id",
            (int(iid),))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def project_medium(url: str) -> str:
    """Where a project is tracked, read off its URL. This is why `type` does not need to
    carry it — 'google_sheet' was never a kind of work."""
    u = (url or "").lower()
    if not u.strip():
        return ""
    if "docs.google.com/spreadsheets" in u:
        return "google sheet"
    if "atlassian.net/wiki" in u or "confluence" in u:
        return "confluence"
    if "monday.com" in u:
        return "monday"
    if "docs.google.com/document" in u:
        return "google doc"
    return "link"


_ROLLUP_EMPTY = {"item_count": 0, "live": 0, "done": 0, "overdue": 0,
                 "due_min": None, "due_max": None}


def project_rollups(conn) -> dict:
    """Per project: items, live, done, overdue, and the span of due dates — the sidebar's
    numbers. Key None is the "no project" bucket. One GROUP BY, not a query per project.
    The span is due dates only until R4.2 adds start/end; then it becomes the plan."""
    live, la = _live_sql()
    today = dt.date.today().isoformat()
    rows = conn.execute(
        f"SELECT a.project_id AS pid, count(*) AS n, "
        f"sum(CASE WHEN {live} THEN 1 ELSE 0 END) AS live, "
        f"sum(CASE WHEN COALESCE(a.due_date,'') <> '' AND a.due_date < ? AND {live} "
        f"THEN 1 ELSE 0 END) AS overdue, "
        f"min(NULLIF(a.due_date,'')) AS due_min, max(NULLIF(a.due_date,'')) AS due_max "
        f"FROM action_items a GROUP BY a.project_id",
        la + [today] + la).fetchall()
    return {r["pid"]: {"item_count": r["n"], "live": r["live"] or 0,
                       "done": r["n"] - (r["live"] or 0), "overdue": r["overdue"] or 0,
                       "due_min": r["due_min"], "due_max": r["due_max"]}
            for r in rows}


def no_project_rollup() -> dict:
    conn = _connect()
    if conn is None:
        return dict(_ROLLUP_EMPTY)
    try:
        return project_rollups(conn).get(None) or dict(_ROLLUP_EMPTY)
    except Exception:
        return dict(_ROLLUP_EMPTY)
    finally:
        conn.close()


def list_projects(archived: bool = False) -> list[dict]:
    """Every project, newest id last. `archived` swaps to the archived ones."""
    conn = _connect()
    if conn is None:
        return []
    try:
        have = _cols(conn, "projects")
        if not have:
            return []
        cmap = _customer_map(conn)
        want = (["id", "customer_id", "name", "type", "rail_url", "status", "notes",
                 "created_at"] + [c for c, _ in PROJECT_NEW_COLUMNS]
                + [c for c, _ in PROJECT_R42_COLUMNS])
        sel = _select_list(have, want, "p")
        where = "1=1"
        if "archived_at" in have:
            where = ("p.archived_at IS NOT NULL" if archived
                     else "p.archived_at IS NULL")
        rows = conn.execute(
            f"SELECT {sel} FROM projects p WHERE {where} ORDER BY p.id").fetchall()
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            d["customer"] = cmap.get(d.get("customer_id"))
            d["medium"] = project_medium(d.get("rail_url") or "")
            d["legacy_fields"] = [f for f, m in PROJECT_LEGACY.items()
                                  if d.get(f) and d.get(f) in m]
            d["item_count"] = 0
            out.append(d)
        try:
            roll = project_rollups(conn)
            for d in out:
                d.update(roll.get(d["id"]) or _ROLLUP_EMPTY)
        except Exception:
            pass
        return out
    except Exception:
        return []
    finally:
        conn.close()


def owners(limit: int = 60) -> list[dict]:
    """Distinct owner strings with counts, commonest first — the filter dropdown, and
    an honest look at the 216-spelling problem. `linked` is how many of those rows
    already carry an owner_contact_id."""
    conn = _connect()
    if conn is None:
        return []
    try:
        have = _cols(conn, "action_items")
        linked = ("sum(CASE WHEN owner_contact_id IS NOT NULL THEN 1 ELSE 0 END)"
                  if "owner_contact_id" in have else "0")
        rows = conn.execute(
            f"SELECT COALESCE(owner,'') AS owner, count(*) AS n, {linked} AS linked "
            "FROM action_items GROUP BY COALESCE(owner,'') "
            "ORDER BY n DESC, owner LIMIT ?", (int(limit),)).fetchall()
        return [{"owner": r["owner"], "n": r["n"], "linked": r["linked"]} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def owner_groups(tail: int = 80) -> dict:
    """The owner filter, grouped (R4.1). `people`: one entry per linked contact, however
    many spellings its rows once had. `tail`: the raw spellings left on rows that are NOT
    linked, so the unlinked rows stay reachable. `me`: the contact ids "Mine" resolves
    to, so the page can mark you in the list. Counts are live / all."""
    empty = {"people": [], "tail": [], "me": []}
    conn = _connect()
    if conn is None:
        return empty
    try:
        if "owner_contact_id" not in _cols(conn, "action_items"):
            return empty
        live, la = _live_sql()
        people = [{"id": r["cid"], "name": r["name"], "n": r["n"], "live": r["live"] or 0}
                  for r in conn.execute(
                      f"SELECT a.owner_contact_id AS cid, c.name AS name, count(*) AS n, "
                      f"sum(CASE WHEN {live} THEN 1 ELSE 0 END) AS live "
                      f"FROM action_items a JOIN contacts c ON c.id = a.owner_contact_id "
                      f"GROUP BY a.owner_contact_id ORDER BY live DESC, n DESC, c.name",
                      la)]
        rest = [{"owner": r["owner"], "n": r["n"], "live": r["live"] or 0}
                for r in conn.execute(
                    f"SELECT COALESCE(a.owner,'') AS owner, count(*) AS n, "
                    f"sum(CASE WHEN {live} THEN 1 ELSE 0 END) AS live "
                    f"FROM action_items a WHERE a.owner_contact_id IS NULL "
                    f"GROUP BY COALESCE(a.owner,'') ORDER BY live DESC, n DESC, owner "
                    f"LIMIT ?", la + [int(tail)])]
        return {"people": people, "tail": rest, "me": _me(conn)[0]}
    except Exception:
        return empty
    finally:
        conn.close()


def stats() -> dict:
    """Counts for the page header, including how much drift is left to clean."""
    empty = {"total": 0, "open": 0, "overdue": 0, "unset_scope": 0,
             "legacy_values": 0, "distinct_owners": 0, "owner_linked": 0,
             "projects": 0}
    conn = _connect()
    if conn is None:
        return empty
    try:
        out = dict(empty)
        one = lambda sql, a=(): (conn.execute(sql, a).fetchone() or [0])[0] or 0
        out["total"] = one("SELECT count(*) FROM action_items")
        out["open"] = one("SELECT count(*) FROM action_items WHERE status='open'")
        out["unset_scope"] = one(
            "SELECT count(*) FROM action_items WHERE report_scope='unset'")
        out["distinct_owners"] = one(
            "SELECT count(DISTINCT COALESCE(owner,'')) FROM action_items")
        out["projects"] = one("SELECT count(*) FROM projects")
        today = dt.date.today().isoformat()
        out["overdue"] = one(
            "SELECT count(*) FROM action_items WHERE due_date IS NOT NULL "
            "AND due_date <> '' AND due_date < ? AND COALESCE(status,'') NOT IN "
            f"({', '.join('?' * len(DONE_STATES))})", (today,) + DONE_STATES)
        legacy_n = 0
        for field, mapping in LEGACY.items():
            vals = list(mapping)
            legacy_n += one(
                f"SELECT count(*) FROM action_items WHERE {field} IN "
                f"({', '.join('?' * len(vals))})", vals)
        out["legacy_values"] = legacy_n
        if "owner_contact_id" in _cols(conn, "action_items"):
            out["owner_linked"] = one(
                "SELECT count(*) FROM action_items WHERE owner_contact_id IS NOT NULL")
        return out
    except Exception:
        return empty
    finally:
        conn.close()


def drift() -> list[dict]:
    """Every legacy value still present, with its count and proposed target. This is
    what the hygiene queue will act on in a later phase — here it is read-only, so the
    page can show the size of the problem without offering to rewrite anything yet."""
    conn = _connect()
    if conn is None:
        return []
    try:
        out = []
        for field, mapping in LEGACY.items():
            for bad, good in mapping.items():
                n = (conn.execute(
                    f"SELECT count(*) FROM action_items WHERE {field}=?",
                    (bad,)).fetchone() or [0])[0]
                if n:
                    out.append({"field": field, "value": bad, "n": n,
                                "proposed": good, "needs_decision": good is None})
        return sorted(out, key=lambda d: -d["n"])
    except Exception:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------- writes

class Conflict(ValueError):
    """The row moved since the page loaded it (R4.2's optimistic check). Carries what
    changed and who changed it, from item_events, so the page can say so."""

    def __init__(self, iid: int, rev: int, changes: list[dict]):
        self.iid, self.rev, self.changes = iid, rev, changes
        who = sorted({c.get("by") or "another writer" for c in changes}) or ["another writer"]
        fields = sorted({c["field"] for c in changes}) or ["the row"]
        super().__init__(f"#{iid} changed since you opened it ({', '.join(fields)} — by "
                         f"{', '.join(who)}). Keep yours, or take theirs.")


def events_since(conn, iid: int, rev: int) -> list[dict]:
    """Field changes made on top of `rev` or later — what someone else did meanwhile."""
    try:
        return [{k: r[k] for k in r.keys()} for r in conn.execute(
            "SELECT field, old, new, at, by, rev FROM item_events "
            "WHERE item_id=? AND rev>=? AND field<>'*created' ORDER BY id",
            (int(iid), int(rev)))]
    except sqlite3.OperationalError:
        return []


def _clean(fields: dict, have: list[str], current: dict | None) -> dict:
    """Whitelist, validate, normalize. Raises on anything that would put an outlier
    spelling into a controlled field."""
    data = {k: v for k, v in (fields or {}).items()
            if k in ITEM_WRITABLE and k in have}

    for field in FIELD_VOCAB:
        if field in data:
            cur = (current or {}).get(field)
            data[field] = _validate(field, data[field], cur)

    for field in ("due_date", "start_date", "end_date"):
        if field in data:
            data[field] = _validate_date(data[field], field)

    if "item_type" in data:
        v = data["item_type"] or "task"
        if v not in ITEM_TYPE:
            raise ValueError(f"item_type must be one of: {', '.join(ITEM_TYPE)} (got {v!r})")
        data["item_type"] = v

    if "description" in data:
        data["description"] = (data["description"] or "").strip()

    if "title" in data:
        data["title"] = _clean_title(data["title"])

    for fk in ("customer_id", "project_id", "parent_id", "owner_contact_id"):
        if fk in data:
            v = data[fk]
            if _empty(v):
                data[fk] = None
            else:
                try:
                    data[fk] = int(str(v).strip().lstrip("#"))
                except (TypeError, ValueError):
                    raise ValueError(f"{fk} must be a number (got {v!r})")

    if "owner" in data and isinstance(data["owner"], str):
        data["owner"] = data["owner"].strip()
    return data


def _plan_dates(data: dict, current: dict | None, have: list[str]) -> dict:
    """The three dates (R4.2). due_date is the commitment; start/end are the plan.
    - Setting a start for the first time with no end: end defaults to the due date.
    - A milestone has no length: its end is its start.
    - The plan cannot end before it starts."""
    if "start_date" not in have:
        return data
    cur = current or {}
    get = lambda k: data[k] if k in data else cur.get(k)
    start, end = get("start_date"), get("end_date")
    if "start_date" in data and start and not cur.get("start_date") and not end:
        end = get("due_date")
        if end:
            data["end_date"] = end
    if (get("item_type") or "task") == "milestone" and start:
        if end != start:
            data["end_date"] = end = start
    if start and end and end < start:
        raise ValueError(f"the plan ends ({end}) before it starts ({start})")
    return data


def _resolve_owner(conn, data: dict) -> dict:
    """When an owner is picked from contacts, the contact's name is authoritative and
    overwrites the free text. This is the mechanism that stops new owner spellings —
    the picker is the easy path and it can only produce canonical names."""
    cid = data.get("owner_contact_id")
    if cid:
        row = conn.execute("SELECT name FROM contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            raise ValueError(f"no contact #{cid}")
        data["owner"] = row["name"]
    return data


def _guard_parent(conn, iid: int | None, parent_id):
    """A sub-item may not be its own parent, and may not close a cycle. PLAN.md flags
    exactly this bug for contacts.reports_to; action_items.parent_id has the same shape
    and a cycle hangs any tree render."""
    if not parent_id:
        return
    if iid is not None and int(parent_id) == int(iid):
        raise ValueError("an item cannot be its own parent")
    seen, cur, hops = set(), parent_id, 0
    while cur and hops < 50:
        if iid is not None and int(cur) == int(iid):
            raise ValueError("that parent would create a loop")
        if cur in seen:
            break
        seen.add(cur)
        row = conn.execute("SELECT parent_id FROM action_items WHERE id=?",
                           (int(cur),)).fetchone()
        if not row:
            raise ValueError(f"no action item #{cur}")
        cur, hops = row["parent_id"], hops + 1


def _guard_tree(conn, iid: int | None, data: dict, current: dict | None) -> dict:
    """R4.2's shape rules, checked only when a write touches parent_id or project_id —
    old rows that already break them are fixed on the Hygiene page, not on every save.

      project → item → sub-item, nothing deeper:
        * a parent must itself be top-level
        * an item with sub-items cannot become a sub-item
      a sub-item lives in its parent's project:
        * picking a parent takes the parent's project
        * changing the project of a sub-item is refused — move the parent, or detach
    """
    cur = current or {}
    touches_parent = "parent_id" in data
    parent_id = data["parent_id"] if touches_parent else cur.get("parent_id")

    if touches_parent and parent_id:
        _guard_parent(conn, iid, parent_id)
        p = conn.execute("SELECT id, parent_id, project_id FROM action_items WHERE id=?",
                         (int(parent_id),)).fetchone()
        if p["parent_id"]:
            raise ValueError(f"#{parent_id} is itself a sub-item of #{p['parent_id']}. "
                             "Nesting stops at one level (project → item → sub-item) — "
                             f"pick #{p['parent_id']}, or detach #{parent_id} first.")
        if iid is not None:
            n = (conn.execute("SELECT count(*) FROM action_items WHERE parent_id=?",
                              (int(iid),)).fetchone() or [0])[0]
            if n:
                raise ValueError(f"#{iid} has {n} sub-item{'s' if n != 1 else ''} of its "
                                 "own, so it can't become a sub-item. Move or detach them "
                                 "first.")
        if "project_id" in data and data["project_id"] != p["project_id"]:
            raise ValueError(f"a sub-item lives in its parent's project — #{parent_id} is "
                             + (f"in project #{p['project_id']}" if p["project_id"]
                                else "in no project")
                             + ". Leave Project alone; it follows the parent.")
        data["project_id"] = p["project_id"]
    elif "project_id" in data and parent_id and data["project_id"] != cur.get("project_id"):
        raise ValueError(f"#{iid} is a sub-item of #{parent_id}, so it follows that "
                         f"item's project. Change #{parent_id}'s project, or detach "
                         f"#{iid} first.")
    return data


def create_item(fields: dict, by: str = "mt") -> dict:
    conn = _connect_w()
    try:
        have = _cols(conn, "action_items")
        if not have:
            raise RuntimeError("action_items table not found")
        data = _clean(fields, have, None)
        if not (data.get("description") or "").strip():
            raise ValueError("a description is required")
        data = _resolve_owner(conn, data)
        data = _guard_tree(conn, None, data, None)
        data = _plan_dates(data, None, have)

        data.setdefault("status", "open")
        data.setdefault("priority", "normal")
        data.setdefault("report_scope", "unset")
        now = _now()
        if "created_at" in have:
            data.setdefault("created_at", now)
        if "updated_at" in have:
            data["updated_at"] = now
        if "updated_by" in have:
            data["updated_by"] = by

        cols = list(data)
        cur = conn.execute(
            f"INSERT INTO action_items ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", [data[c] for c in cols])
        conn.commit()
        iid = cur.lastrowid
    finally:
        conn.close()
    return get_item(iid)


def update_item(iid: int, fields: dict, by: str = "mt", base_rev=None) -> dict:
    """Partial update. `base_rev` is the `rev` the page loaded (R4.2): if the row has
    moved since — the assistant, a script, the other mt window — nothing is written and
    Conflict says what changed. Omit it (or pre-migration) for last-write-wins.

    Changing an item's project carries its sub-items with it, in the same transaction;
    the returned row lists them in `carried`."""
    iid = int(iid)
    conn = _connect_w()
    carried: list[int] = []
    try:
        have = _cols(conn, "action_items")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM action_items WHERE id=?", (iid,)).fetchone()
        if not row:
            raise ValueError(f"no action item #{iid}")
        current = {k: row[k] for k in row.keys()}

        if base_rev not in (None, "") and "rev" in have:
            base = int(base_rev)
            if int(current.get("rev") or 0) != base:
                raise Conflict(iid, int(current.get("rev") or 0),
                               events_since(conn, iid, base))

        data = _clean(fields, have, current)
        if "description" in data and not data["description"]:
            raise ValueError("a description cannot be blank")
        if not data:
            raise ValueError("nothing to update")
        data = _resolve_owner(conn, data)
        data = _guard_tree(conn, iid, data, current)
        data = _plan_dates(data, current, have)

        # Unlinking (owner_contact_id -> None) deliberately leaves the `owner` free
        # text alone: the row still knows who owns it, it just no longer claims that
        # name is a resolved contact. Losing the name entirely would be worse.

        stamp = {}
        if "updated_at" in have:
            stamp["updated_at"] = _now()
        if "updated_by" in have:
            stamp["updated_by"] = by
        data.update(stamp)

        sets = ", ".join(f"{c}=?" for c in data)
        conn.execute(f"UPDATE action_items SET {sets} WHERE id=?",
                     [data[c] for c in data] + [iid])

        if "project_id" in data and data["project_id"] != current.get("project_id"):
            carried = [r[0] for r in conn.execute(
                "SELECT id FROM action_items WHERE parent_id=? AND project_id IS NOT ?",
                (iid, data["project_id"]))]
            if carried:
                sets = "project_id=?" + "".join(f", {k}=?" for k in stamp)
                conn.execute(
                    f"UPDATE action_items SET {sets} WHERE id IN "
                    f"({', '.join('?' * len(carried))})",
                    [data["project_id"]] + list(stamp.values()) + carried)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    out = get_item(iid)
    if out is not None:
        out["carried"] = carried
    return out


def _clean_project(fields: dict, have: list[str], current: dict | None) -> dict:
    data = {k: v for k, v in (fields or {}).items()
            if k in PROJECT_WRITABLE and k in have}
    for field, allowed in PROJECT_VOCAB.items():
        if field not in data:
            continue
        v = data[field]
        if v in ("", None):
            data[field] = None if field == "priority" else ""
            continue
        cur = (current or {}).get(field)
        if v in allowed or (cur is not None and v == cur):
            continue                      # canonical, or an untouched legacy value
        raise ValueError(
            f"{field} must be one of: {', '.join(allowed)} (got {v!r})")
    if "name" in data:
        data["name"] = (data["name"] or "").strip()
    if "customer_id" in data:
        v = data["customer_id"]
        data["customer_id"] = None if _empty(v) else int(v)
    return data


def create_project(fields: dict, by: str = "mt") -> dict:
    conn = _connect_w()
    try:
        have = _cols(conn, "projects")
        if not have:
            raise RuntimeError("projects table not found")
        data = _clean_project(fields, have, None)
        if not (data.get("name") or "").strip():
            raise ValueError("a name is required")
        data.setdefault("status", "active")
        now = _now()
        if "created_at" in have:
            data.setdefault("created_at", now)
        if "updated_at" in have:
            data["updated_at"] = now
        if "updated_by" in have:
            data["updated_by"] = by
        cols = list(data)
        cur = conn.execute(
            f"INSERT INTO projects ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})", [data[c] for c in cols])
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    return get_project(pid)


def get_project(pid: int) -> dict | None:
    for arch in (False, True):
        for p in list_projects(archived=arch):
            if int(p["id"]) == int(pid):
                return p
    return None


def update_project(pid: int, fields: dict, by: str = "mt") -> dict:
    pid = int(pid)
    conn = _connect_w()
    try:
        have = _cols(conn, "projects")
        row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ValueError(f"no project #{pid}")
        current = {k: row[k] for k in row.keys()}
        data = _clean_project(fields, have, current)
        if "name" in data and not data["name"]:
            raise ValueError("a name cannot be blank")
        if not data:
            raise ValueError("nothing to update")
        if "updated_at" in have:
            data["updated_at"] = _now()
        if "updated_by" in have:
            data["updated_by"] = by
        sets = ", ".join(f"{c}=?" for c in data)
        conn.execute(f"UPDATE projects SET {sets} WHERE id=?",
                     [data[c] for c in data] + [pid])
        conn.commit()
    finally:
        conn.close()
    return get_project(pid)


def set_project_archived(pid: int, archived: bool, by: str = "mt") -> dict:
    """Hide a project without deleting it. 109 action_items carry a project_id; deleting
    a project would leave them pointing at nothing, and the items themselves are still
    real work. Archiving takes it out of the list and the picker and nothing else."""
    pid = int(pid)
    conn = _connect_w()
    try:
        have = _cols(conn, "projects")
        if "archived_at" not in have:
            raise RuntimeError("archive column is not migrated in yet")
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
            raise ValueError(f"no project #{pid}")
        n = (conn.execute("SELECT count(*) FROM action_items WHERE project_id=?",
                          (pid,)).fetchone() or [0])[0]
        conn.execute("UPDATE projects SET archived_at=?, updated_at=?, updated_by=? "
                     "WHERE id=?",
                     (_now() if archived else None, _now(), by, pid))
        conn.commit()
    finally:
        conn.close()
    out = get_project(pid) or {}
    out["item_count"] = n
    return out


# --------------------------------------------------------------- promote / demote (R4.2b)
#
# An umbrella item becomes a project, and a project can go back to being an item. Both
# directions are previewed first and applied only against the plan the user saw: the preview
# carries a `sha` of exactly what will change, and apply recomputes it and refuses if the
# data moved in between — the Hygiene page's posture. Nothing is deleted: a promoted item
# is closed with a pointer to its project; a demoted project is archived with a pointer
# to its item. Every item-side change lands in item_events through the triggers, so the
# history of a promotion is readable after the fact.

PROJECT_STATUS_FROM_ITEM = {"open": "active", "waiting": "active", "blocked": "paused",
                            "done": "complete", "wont_do": "complete"}
ITEM_STATUS_FROM_PROJECT = {"active": "open", "paused": "waiting", "complete": "done"}

_UMBRELLA_RE = re.compile(r"\s*[-—–]*\s*PARENT\s*/\s*UMBRELLA\b.*$", re.I)


def _plan_sha(plan: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(plan, sort_keys=True, default=str)
                          .encode()).hexdigest()[:16]


def _title(seg: str) -> str:
    """Shouting house style → a name. Leaves mixed-case text and short acronyms alone."""
    if seg != seg.upper():
        return seg
    small = {"and", "or", "of", "the", "to", "for", "in", "on", "a", "an", "&"}
    out = []
    for i, w in enumerate(seg.split()):
        lw = w.lower()
        if len(w) <= 3 and w.isalpha() and lw not in small:
            out.append(w)                                  # RPM, OVMF-ish, SBR, ACM
        elif i and lw in small:
            out.append(lw)
        else:
            out.append(w[:1] + w[1:].lower())
    return " ".join(out)


# Per-account words and labels come from config.json "accounts" (paths.ACCOUNTS).
_ACCOUNT_ALIASES = {s: a["aliases"] for s, a in P.ACCOUNTS.items() if a["aliases"]}
_ACCOUNT_LABEL = {s: a["label"] for s, a in P.ACCOUNTS.items() if a["label"]}


def propose_project_name(desc: str, customer: str | None) -> str:
    """A starting name for the preview, always editable: the first line/sentence of the
    description, minus a leading emoji, the account prefix and the PARENT / UMBRELLA tag,
    cut at the first colon or comma once it is long, de-shouted, prefixed with the
    account's label."""
    d = (desc or "").strip().split("\n", 1)[0]
    d = re.split(r"\.\s|\(", d, 1)[0]
    d = _UMBRELLA_RE.sub("", d)
    d = re.sub(r"^[^\w']+", "", d)                       # 🟢 / 🆕 / stray dashes
    low = d.lower()
    for alias in _ACCOUNT_ALIASES.get(customer or "", []) + ["internal"]:
        if low.startswith(alias) and (len(low) == len(alias) or not low[len(alias)].isalnum()):
            d = d[len(alias):]
            break
    d = re.sub(r"^[^\w']+", "", d)
    d = re.split(r"\s+(?:—|–|--)\s+", d, 1)[0]          # house-style clause break
    if len(d) > 40:
        d = re.split(r"[:,]\s", d, 1)[0]
    core = _title(d.strip(" -—:"))[:80].strip()
    label = _ACCOUNT_LABEL.get(customer or "", customer or "")
    if label and label.lower().replace("'", "") not in core.lower().replace("'", ""):
        return f"{label} {core}".strip()
    return core


def _family(conn, root: int) -> tuple[list[dict], list[dict]]:
    """(children, deeper) of an item: direct children, then every descendant below them
    with its depth (2 = grandchild). Cycle-safe."""
    kids = [dict(r) for r in conn.execute(
        "SELECT id, parent_id, project_id, status, description FROM action_items "
        "WHERE parent_id=? ORDER BY id", (root,))]
    deeper, seen, frontier, depth = [], {root} | {k["id"] for k in kids}, kids, 2
    while frontier and depth < 12:
        ids = [f["id"] for f in frontier]
        nxt = [dict(r) for r in conn.execute(
            f"SELECT id, parent_id, project_id, status, description FROM action_items "
            f"WHERE parent_id IN ({', '.join('?' * len(ids))}) ORDER BY id", ids)
            if r["id"] not in seen]
        for n in nxt:
            seen.add(n["id"])
            n["depth"] = depth
        deeper += nxt
        frontier, depth = nxt, depth + 1
    return kids, deeper


def _similar_projects(conn, customer_id, name: str) -> list[dict]:
    """Existing projects in the same account that share a meaningful word with `name` —
    shown on the preview so a duplicate (ACM POS vs the gold-image umbrella) is seen,
    never silently merged."""
    words = {w for w in re.findall(r"[a-z0-9]{4,}", (name or "").lower())}
    out = []
    for r in conn.execute("SELECT id, name, status, archived_at FROM projects WHERE "
                          "customer_id IS ? ORDER BY id", (customer_id,)):
        if words & set(re.findall(r"[a-z0-9]{4,}", (r["name"] or "").lower())) \
                or customer_id is not None:
            out.append({"id": r["id"], "name": r["name"], "status": r["status"],
                        "archived": bool(r["archived_at"]),
                        "shares": sorted(words & set(re.findall(
                            r"[a-z0-9]{4,}", (r["name"] or "").lower())))})
    return out


def promote_preview(iid: int, name: str = "") -> dict:
    """What promoting item `iid` would do. Raises ValueError when it cannot be promoted."""
    conn = _connect_w()
    try:
        if not r42_ready(conn):
            raise RuntimeError("promote needs the R4.2 migration first")
        it = conn.execute("SELECT * FROM action_items WHERE id=?", (int(iid),)).fetchone()
        if not it:
            raise ValueError(f"no action item #{iid}")
        it = dict(it)
        if it.get("promoted_to_project_id"):
            raise ValueError(f"#{iid} was already promoted to project "
                             f"#{it['promoted_to_project_id']}")
        if it.get("parent_id"):
            raise ValueError(f"#{iid} is a sub-item of #{it['parent_id']} — only a top-level "
                             "item can become a project. Detach it first.")
        cmap = _customer_map(conn)
        kids, deeper = _family(conn, it["id"])
        links = [dict(r) for r in conn.execute(
            "SELECT id, system, role, external_item_id FROM external_links WHERE "
            "local_table='action_items' AND local_id=? AND system <> 'file' ORDER BY id",
            (it["id"],))]
        name = (name or "").strip() or propose_project_name(
            it["description"], cmap.get(it["customer_id"]))
        # Rows below a grandchild would be three deep under the new project; they are
        # re-parented to the item directly under the project (their ancestor at depth 1).
        anc = {k["id"]: k["id"] for k in kids}
        flatten = []
        for d in deeper:
            anc[d["id"]] = anc.get(d["parent_id"], d["parent_id"])
            if d["depth"] >= 3:
                flatten.append({"id": d["id"], "from": d["parent_id"], "to": anc[d["id"]]})
        live = lambda rs: sum(1 for r in rs if (r["status"] or "") not in DONE_STATES)
        plan = {
            "item": {"id": it["id"], "rev": it.get("rev") or 0},
            "project": {"name": name, "customer_id": it["customer_id"],
                        "priority": it["priority"] if it["priority"] in PRIORITY else None,
                        "status": PROJECT_STATUS_FROM_ITEM.get(it["status"] or "", "active"),
                        "target_date": it.get("due_date") or None},
            "children": [k["id"] for k in kids],
            "deeper": [d["id"] for d in deeper],
            "flatten": flatten,
            "links": [l["id"] for l in links],
        }
        # The name is typed on the preview, so it is not part of what is being approved:
        # editing it must not invalidate the plan.
        nameless = dict(plan, project={k: v for k, v in plan["project"].items()
                                       if k != "name"})
        return {
            "plan": plan, "sha": _plan_sha(nameless),
            "item": {"id": it["id"], "description": it["description"],
                     "customer": cmap.get(it["customer_id"]), "status": it["status"],
                     "due_date": it.get("due_date")},
            "summary": {"children_live": live(kids), "children_done": len(kids) - live(kids),
                        "deeper": len(deeper), "flatten": len(flatten),
                        "links": len(links)},
            "links": links,
            "similar": _similar_projects(conn, it["customer_id"], name),
        }
    finally:
        conn.close()


def promote_item(iid: int, sha: str, name: str = "", ptype: str = "other",
                 by: str = "mt") -> dict:
    """Apply exactly the plan promote_preview() showed (same `name` → same sha)."""
    pv = promote_preview(iid, name)
    if pv["sha"] != sha:
        raise ValueError("this item or its sub-items changed since the preview — reload the "
                         "preview and check it again")
    if ptype not in PROJECT_TYPE:
        raise ValueError(f"type must be one of: {', '.join(PROJECT_TYPE)}")
    plan, now = pv["plan"], _now()
    conn = _connect_w()
    try:
        conn.execute("BEGIN IMMEDIATE")
        pr = plan["project"]
        notes = (f"Promoted from action item #{iid} on {now[:10]}. "
                 f"Original text: {pv['item']['description']}")
        pcols = _cols(conn, "projects")
        row = {"name": pr["name"], "type": ptype, "status": pr["status"],
               "customer_id": pr["customer_id"], "priority": pr["priority"],
               "notes": notes, "created_at": now, "updated_at": now, "updated_by": by,
               "origin_item_id": int(iid), "target_date": pr["target_date"]}
        row = {k: v for k, v in row.items() if k in pcols}
        cur = conn.execute(f"INSERT INTO projects ({', '.join(row)}) VALUES "
                           f"({', '.join('?' * len(row))})", list(row.values()))
        pid = cur.lastrowid
        stamp = [now, by]
        for cid in plan["children"]:
            conn.execute("UPDATE action_items SET project_id=?, parent_id=NULL, "
                         "updated_at=?, updated_by=? WHERE id=? AND parent_id=?",
                         [pid] + stamp + [cid, int(iid)])
        for did in plan["deeper"]:
            conn.execute("UPDATE action_items SET project_id=?, updated_at=?, updated_by=? "
                         "WHERE id=?", [pid] + stamp + [did])
        for f in plan["flatten"]:
            conn.execute("UPDATE action_items SET parent_id=? WHERE id=? AND parent_id=?",
                         (f["to"], f["id"], f["from"]))
        for lid in plan["links"]:
            conn.execute(
                "INSERT OR IGNORE INTO external_links (local_table, local_id, system, role, "
                "external_container_id, external_item_id, notes) SELECT 'projects', ?, "
                "system, role, external_container_id, external_item_id, "
                "'copied from item #' || local_id || ' when it was promoted' "
                "FROM external_links WHERE id=?", (pid, lid))
        n = conn.execute("UPDATE action_items SET status='done', promoted_to_project_id=?, "
                         "updated_at=?, updated_by=? WHERE id=? AND COALESCE(rev,0)=?",
                         [pid] + stamp + [int(iid), plan["item"]["rev"]]).rowcount
        if n != 1:
            raise ValueError(f"#{iid} changed while promoting — nothing was changed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    out = get_project(pid) or {"id": pid}
    out["moved"] = len(plan["children"]) + len(plan["deeper"])
    return out


def demote_preview(pid: int) -> dict:
    """What turning project `pid` back into an item would do. Items in the project
    become sub-items of that item; anything already a sub-item would end up two deep,
    so each of those needs a choice (flatten under the new item, or move to another
    project) — listed in `needs_choice`, never decided silently."""
    conn = _connect_w()
    try:
        if not r42_ready(conn):
            raise RuntimeError("demote needs the R4.2 migration first")
        p = conn.execute("SELECT * FROM projects WHERE id=?", (int(pid),)).fetchone()
        if not p:
            raise ValueError(f"no project #{pid}")
        p = dict(p)
        if p.get("demoted_to_item_id"):
            raise ValueError(f"project #{pid} was already demoted to item "
                             f"#{p['demoted_to_item_id']}")
        origin = None
        if p.get("origin_item_id"):
            o = conn.execute("SELECT id, status, project_id, promoted_to_project_id, rev "
                             "FROM action_items WHERE id=?",
                             (p["origin_item_id"],)).fetchone()
            if o and o["promoted_to_project_id"] == p["id"]:
                origin = dict(o)
        items = [dict(r) for r in conn.execute(
            "SELECT id, parent_id, status, description FROM action_items WHERE project_id=? "
            "AND id IS NOT ? ORDER BY id", (int(pid), (origin or {}).get("id")))]
        ids = {i["id"] for i in items}
        top = [i["id"] for i in items if not i["parent_id"] or i["parent_id"] not in ids]
        subs = [i for i in items if i["parent_id"] and i["parent_id"] in ids]
        # Restoring the original: its status from just before the promotion closed it.
        restore = "open"
        if origin:
            ev = conn.execute("SELECT old FROM item_events WHERE item_id=? AND field='status' "
                              "AND new='done' ORDER BY id DESC LIMIT 1",
                              (origin["id"],)).fetchone()
            restore = (ev[0] if ev and ev[0] in STATUS else None) or "open"
        target_project = origin["project_id"] if origin else None
        plan = {
            "project": {"id": p["id"]},
            "origin": ({"id": origin["id"], "rev": origin["rev"] or 0, "status": restore}
                       if origin else None),
            "new_item": None if origin else {
                "description": (p["name"] or "") + (f" — {p['notes']}" if p.get("notes")
                                                    else ""),
                "customer_id": p["customer_id"],
                "priority": p.get("priority") if p.get("priority") in PRIORITY else "normal",
                "status": ITEM_STATUS_FROM_PROJECT.get(p["status"] or "", "open"),
                "due_date": p.get("target_date")},
            "project_id_after": target_project,
            "top": top,
            "subs": [{"id": s_["id"], "parent_id": s_["parent_id"]} for s_ in subs],
        }
        others = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            "SELECT id, name FROM projects WHERE archived_at IS NULL AND id<>? ORDER BY name",
            (int(pid),))]
        return {"plan": plan, "sha": _plan_sha(plan),
                "project": {"id": p["id"], "name": p["name"], "customer_id": p["customer_id"]},
                "summary": {"top": len(top), "subs": len(subs),
                            "restores": bool(origin)},
                "needs_choice": [{"id": s_["id"], "parent_id": s_["parent_id"],
                                  "description": (s_["description"] or "")[:140]}
                                 for s_ in subs],
                "other_projects": others}
    finally:
        conn.close()


def demote_project(pid: int, sha: str, choices: dict | None = None,
                   by: str = "mt") -> dict:
    """Apply exactly the plan demote_preview() showed. `choices` maps every id in
    needs_choice to "flatten" or a project id to move it (detached) into."""
    pv = demote_preview(pid)
    if pv["sha"] != sha:
        raise ValueError("this project or its items changed since the preview — reload the "
                         "preview and check it again")
    plan, now = pv["plan"], _now()
    choices = {int(k): v for k, v in (choices or {}).items()}
    for s_ in plan["subs"]:
        c = choices.get(s_["id"])
        if c != "flatten" and not (str(c).isdigit() and
                                   int(c) in {o["id"] for o in pv["other_projects"]}):
            raise ValueError(f"#{s_['id']} needs a choice: flatten it under the new item, "
                             "or move it to another project")
    conn = _connect_w()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stamp = [now, by]
        if plan["origin"]:
            iid = plan["origin"]["id"]
            n = conn.execute(
                "UPDATE action_items SET status=?, promoted_to_project_id=NULL, "
                "updated_at=?, updated_by=? WHERE id=? AND COALESCE(rev,0)=?",
                [plan["origin"]["status"]] + stamp + [iid, plan["origin"]["rev"]]).rowcount
            if n != 1:
                raise ValueError(f"#{iid} changed while demoting — nothing was changed")
        else:
            ni = dict(plan["new_item"], project_id=None, report_scope="unset",
                      owner=(getattr(P, "OWNER", "") or None), created_at=now,
                      updated_at=now, updated_by=by,
                      source=f"demoted from project #{pid} on {now[:10]}")
            cols = [c for c in ni if c in _cols(conn, "action_items")]
            iid = conn.execute(f"INSERT INTO action_items ({', '.join(cols)}) VALUES "
                               f"({', '.join('?' * len(cols))})",
                               [ni[c] for c in cols]).lastrowid
        after = plan["project_id_after"]
        for t in plan["top"]:
            conn.execute("UPDATE action_items SET parent_id=?, project_id=?, updated_at=?, "
                         "updated_by=? WHERE id=?", [iid, after] + stamp + [t])
        for s_ in plan["subs"]:
            c = choices[s_["id"]]
            if c == "flatten":
                conn.execute("UPDATE action_items SET parent_id=?, project_id=?, "
                             "updated_at=?, updated_by=? WHERE id=?",
                             [iid, after] + stamp + [s_["id"]])
            else:
                conn.execute("UPDATE action_items SET parent_id=NULL, project_id=?, "
                             "updated_at=?, updated_by=? WHERE id=?",
                             [int(c)] + stamp + [s_["id"]])
        conn.execute(
            "INSERT OR IGNORE INTO external_links (local_table, local_id, system, role, "
            "external_container_id, external_item_id, notes) SELECT 'action_items', ?, "
            "system, role, external_container_id, external_item_id, "
            "'copied from project #' || local_id || ' when it was demoted' "
            "FROM external_links WHERE local_table='projects' AND local_id=? "
            "AND system <> 'file'", (iid, int(pid)))
        conn.execute("UPDATE projects SET archived_at=?, demoted_to_item_id=?, "
                     "updated_at=?, updated_by=? WHERE id=?",
                     (now, iid, now, by, int(pid)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    out = get_item(iid) or {"id": iid}
    out["moved"] = len(plan["top"]) + len(plan["subs"])
    return out


# --------------------------------------------------------------- sequence (R4.3)
#
# What the predecessor links MEAN on the list page. Rules, decided Oct 13 2026 from the
# plan's proposals:
#   * lag and Push dependents count WORKING days (Mon–Fri, no holiday calendar yet)
#   * a successor may start the same day its predecessor finishes
#   * when an item has no plan yet, its due date stands in: finish = end or due,
#     begin = start, else end, else due. Only 0 of ~200 live items had a start on the
#     day this shipped, so without the fallback no conflict could ever show.
#   * a predecessor that is finished, or a link marked "met anyway", never blocks and
#     never conflicts.

DEP_FILTERS = [
    {"key": "ready", "label": "Ready to start"},
    {"key": "blocked", "label": "Waiting on a predecessor"},
    {"key": "conflict", "label": "Sequence conflict"},
    {"key": "linked", "label": "Has predecessors or successors"},
]
DEP_FILTERS_KEYS = {d["key"] for d in DEP_FILTERS}


def _d(s):
    return dt.date.fromisoformat(s) if s else None


def wd_add(day: str, n: int) -> str:
    """`day` plus n working days. n=0 returns the day itself."""
    d = _d(day)
    step = 1 if n >= 0 else -1
    left = abs(int(n))
    while left:
        d += dt.timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat()


def wd_between(a: str, b: str) -> int:
    """Working days from a to b (positive when b is later)."""
    da, db = _d(a), _d(b)
    sign = 1 if db >= da else -1
    lo, hi = (da, db) if sign == 1 else (db, da)
    n, d = 0, lo
    while d < hi:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return sign * n


def _finish(r: dict):
    return r.get("end_date") or r.get("due_date") or None


def _begin(r: dict):
    return r.get("start_date") or r.get("end_date") or r.get("due_date") or None


def _is_done(r: dict) -> bool:
    return (r.get("status") or "") in DONE_STATES


def _seq(conn) -> dict | None:
    """Every live link, and the few columns of the items they touch."""
    if not r42_ready(conn):
        return None
    links = [dict(r) for r in conn.execute(
        "SELECT id, pred_id, succ_id, lag_days, met_at FROM action_item_deps "
        "WHERE removed_at IS NULL")]
    ids = sorted({l["pred_id"] for l in links} | {l["succ_id"] for l in links})
    items = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
                "SELECT id, status, start_date, end_date, due_date, project_id, rev, "
                f"description FROM action_items WHERE id IN ({', '.join('?' * len(chunk))})",
                chunk):
            items[r["id"]] = dict(r)
    return {"links": [l for l in links if l["pred_id"] in items and l["succ_id"] in items],
            "items": items}


def _conflict(l: dict, p: dict, s: dict) -> dict | None:
    if l["met_at"] or _is_done(p) or _is_done(s):
        return None
    pf, sb = _finish(p), _begin(s)
    if not pf or not sb:
        return None
    need = wd_add(pf, l["lag_days"] or 0)
    return {"pred": p["id"], "need": need, "has": sb} if need > sb else None


def _seq_flags(seq: dict) -> dict:
    """id -> {after, unblocks, blocked_by, conflicts} for every item with a link."""
    out: dict[int, dict] = {}
    items = seq["items"]
    blank = lambda: {"after": [], "unblocks": [], "blocked_by": [], "conflicts": []}
    for l in seq["links"]:
        p, s_ = items[l["pred_id"]], items[l["succ_id"]]
        fs, fp = out.setdefault(s_["id"], blank()), out.setdefault(p["id"], blank())
        fs["after"].append({"dep": l["id"], "id": p["id"], "done": _is_done(p),
                            "met": bool(l["met_at"]), "lag": l["lag_days"] or 0})
        fp["unblocks"].append({"dep": l["id"], "id": s_["id"], "done": _is_done(s_)})
        if not _is_done(p) and not l["met_at"] and not _is_done(s_):
            fs["blocked_by"].append(p["id"])
        c = _conflict(l, p, s_)
        if c:
            fs["conflicts"].append(c)
    for i, f in out.items():
        f["status"] = items[i]["status"]
    return out


def _dep_match(key: str, f: dict) -> bool:
    if key == "linked":
        return bool(f["after"] or f["unblocks"])
    if key == "blocked":
        return bool(f["blocked_by"])
    if key == "conflict":
        return bool(f["conflicts"])
    if key == "ready":                         # open, has predecessors, none holding it
        return f["status"] == "open" and bool(f["after"]) and not f["blocked_by"]
    return False


def _steps(ids: set, seq: dict) -> dict:
    """Longest-path layering inside a project: step 1 has no predecessor in the set;
    every other item sits one step below its LATEST predecessor. Cycle-safe (links are
    guarded against loops on insert; this does not trust that)."""
    preds: dict[int, list[int]] = {}
    for l in seq["links"]:
        if l["pred_id"] in ids and l["succ_id"] in ids:
            preds.setdefault(l["succ_id"], []).append(l["pred_id"])
    memo: dict[int, int] = {}

    def step(i, seen=()):
        if i in memo:
            return memo[i]
        if i in seen:
            return 1
        v = 1 + max((step(p, seen + (i,)) for p in preds.get(i, [])), default=0)
        memo[i] = v
        return v

    return {i: step(i) for i in ids}


def item_sequence(iid: int) -> dict:
    """The edit sheet's view of one item: its links both ways, with each other item's
    title, status and dates, and this item's blocked/conflict state."""
    conn = _connect()
    empty = {"after": [], "unblocks": [], "blocked_by": [], "conflicts": [],
             "ready": r42_ready(conn) if conn else False}
    if conn is None:
        return empty
    try:
        seq = _seq(conn)
        if not seq:
            return empty
        f = _seq_flags(seq).get(int(iid))
        if not f:
            return dict(empty, ready=True)
        items = seq["items"]
        info = lambda i: {k: items[i].get(k) for k in ("status", "start_date", "end_date",
                                                         "due_date", "project_id")} | {
            "title": (items[i].get("description") or "")[:120]}
        for k in ("after", "unblocks"):
            f[k] = [dict(x, **info(x["id"])) for x in f[k]]
        return dict(f, ready=True)
    finally:
        conn.close()


def push_preview(pred_id: int) -> dict:
    """What "Push dependents" would do from `pred_id`: walk its successors in order and
    move each one whose start is now too early to the earliest day the sequence allows,
    keeping its length in working days. Due dates are commitments and never move: an item
    with only a due date is listed as `held` with the day it would need, for a person to
    decide. Nothing is written."""
    conn = _connect()
    if conn is None:
        raise RuntimeError("the database could not be opened")
    try:
        seq = _seq(conn)
        if not seq:
            raise RuntimeError("Push dependents needs the R4.2 migration first")
    finally:
        conn.close()
    items, links = seq["items"], [l for l in seq["links"] if not l["met_at"]]
    pred_id = int(pred_id)
    if pred_id not in items:
        raise ValueError(f"#{pred_id} has no successors to push")
    succs: dict[int, list[dict]] = {}
    into: dict[int, list[dict]] = {}
    for l in links:
        succs.setdefault(l["pred_id"], []).append(l)
        into.setdefault(l["succ_id"], []).append(l)
    reach, stack = set(), [pred_id]
    while stack:
        for l in succs.get(stack.pop(), []):
            s_ = items[l["succ_id"]]
            if l["succ_id"] not in reach and not _is_done(s_):
                reach.add(l["succ_id"])
                stack.append(l["succ_id"])
    # Topological order over the reachable successors (Kahn).
    indeg = {i: sum(1 for l in into.get(i, []) if l["pred_id"] in reach) for i in reach}
    order, ready = [], sorted(i for i, n in indeg.items() if n == 0)
    while ready:
        i = ready.pop(0)
        order.append(i)
        for l in succs.get(i, []):
            j = l["succ_id"]
            if j in indeg:
                indeg[j] -= 1
                if indeg[j] == 0:
                    ready.append(j)
    est = {}                                       # id -> finish after the push
    moves, held = [], []
    for i in order:
        it = items[i]
        need = None
        for l in into.get(i, []):
            p = items[l["pred_id"]]
            if _is_done(p):
                continue
            pf = est.get(p["id"], _finish(p))
            if pf:
                n_ = wd_add(pf, l["lag_days"] or 0)
                need = n_ if need is None or n_ > need else need
        begin = _begin(it)
        if not need or not begin or need <= begin:
            est[i] = _finish(it)
            continue
        if it.get("start_date"):
            delta = wd_between(it["start_date"], need)
            new_end = wd_add(it["end_date"], delta) if it.get("end_date") else None
            mv = {"id": i, "rev": it.get("rev") or 0, "title": (it["description"] or "")[:100],
                  "start_date": [it["start_date"], need],
                  "end_date": [it.get("end_date"), new_end], "days": delta}
            est[i] = new_end or need
        elif it.get("end_date"):
            mv = {"id": i, "rev": it.get("rev") or 0, "title": (it["description"] or "")[:100],
                  "start_date": [None, None], "end_date": [it["end_date"], need],
                  "days": wd_between(it["end_date"], need)}
            est[i] = need
        else:
            held.append({"id": i, "title": (it["description"] or "")[:100],
                         "due_date": it.get("due_date"), "need": need,
                         "days": wd_between(it["due_date"], need)})
            est[i] = need
            continue
        fin = mv["end_date"][1] or mv["start_date"][1]
        mv["past_due"] = bool(it.get("due_date") and fin and fin > it["due_date"])
        mv["due_date"] = it.get("due_date")
        old_fin = mv["end_date"][0] or mv["start_date"][0]
        mv["new_month"] = bool(old_fin and fin and old_fin[:7] != fin[:7])
        moves.append(mv)
    plan = {"from": pred_id, "moves": [(m["id"], m["rev"], m["start_date"], m["end_date"])
                                       for m in moves]}
    return {"from": pred_id, "moves": moves, "held": held, "sha": _plan_sha(plan),
            "summary": {"moves": len(moves), "held": len(held),
                        "max_days": max([m["days"] for m in moves], default=0),
                        "new_month": sum(1 for m in moves if m["new_month"]),
                        "past_due": sum(1 for m in moves if m["past_due"])}}


def apply_dates(moves: list[dict], by: str = "mt") -> list[dict]:
    """Set start/end on several items in ONE transaction, each guarded on the rev it was
    read at: one moved row and nothing is written. Returns the undo for the batch — the
    old dates, keyed to the revs this write produces."""
    conn = _connect_w()
    undo = []
    try:
        if not r42_ready(conn):
            raise RuntimeError("needs the R4.2 migration first")
        conn.execute("BEGIN IMMEDIATE")
        now = _now()
        for m in moves:
            iid, rev = int(m["id"]), int(m.get("rev") or 0)
            start = _validate_date(m.get("start_date"), "start_date")
            end = _validate_date(m.get("end_date"), "end_date")
            if start and end and end < start:
                raise ValueError(f"#{iid}: the plan would end before it starts")
            row = conn.execute("SELECT start_date, end_date, rev FROM action_items WHERE id=?",
                               (iid,)).fetchone()
            if not row:
                raise ValueError(f"no action item #{iid}")
            n = conn.execute(
                "UPDATE action_items SET start_date=?, end_date=?, updated_at=?, updated_by=? "
                "WHERE id=? AND COALESCE(rev,0)=?", (start, end, now, by, iid, rev)).rowcount
            if n != 1:
                raise ValueError(f"#{iid} changed since this was worked out — nothing was "
                                 "moved. Look again.")
            undo.append({"id": iid, "start_date": row["start_date"],
                         "end_date": row["end_date"]})
        conn.commit()
        for u in undo:
            u["rev"] = (conn.execute("SELECT COALESCE(rev,0) FROM action_items WHERE id=?",
                                     (u["id"],)).fetchone() or [0])[0]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return undo


PLAN_FIELDS = ("start_date", "end_date", "due_date")


def apply_plan(changes: list[dict], by: str = "mt", links: list[dict] | None = None) -> dict:
    """R4.5 — the Gantt tray's Save. Each change is {id, rev, start_date?, end_date?,
    due_date?}; only the keys present are written (a due-date drag never touches the
    plan). ONE transaction, every row guarded on the rev the page read it at: if any row
    moved since, nothing is written and the refusal names it (and who moved it). Returns
    {saved, undo} — the undo carries the old values and the revs this write produced, so
    it is itself rev-guarded."""
    changes = changes or []
    links = links or []
    if not changes and not links:
        return {"saved": 0, "undo": [], "undo_links": []}
    if len(changes) + len(links) > BULK_MAX:
        raise ValueError(f"at most {BULK_MAX} changes at once")
    conn = _connect_w()
    undo = []
    try:
        if not r42_ready(conn):
            raise RuntimeError("needs the R4.2 migration first")
        conn.execute("BEGIN IMMEDIATE")
        now = _now()
        for c in changes:
            iid, rev = int(c["id"]), int(c.get("rev") or 0)
            row = conn.execute("SELECT start_date, end_date, due_date, item_type, "
                               "COALESCE(rev,0) AS rev FROM action_items WHERE id=?",
                               (iid,)).fetchone()
            if not row:
                raise ValueError(f"no action item #{iid}")
            if row["rev"] != rev:
                who = events_since(conn, iid, rev)
                by_ = ", ".join(sorted({e.get("by") or "someone" for e in who})) or "someone"
                raise ValueError(f"#{iid} was changed by {by_} after this chart loaded — "
                                 "nothing was saved. Undo that one, or Refresh it.")
            sets = {k: _validate_date(c[k], k) for k in PLAN_FIELDS if k in c}
            if not sets:
                continue
            start = sets.get("start_date", row["start_date"])
            end = sets.get("end_date", row["end_date"])
            if (row["item_type"] or "task") == "milestone" and start and end != start:
                end = sets["end_date"] = start
            if start and end and end < start:
                raise ValueError(f"#{iid}: the plan would end ({end}) before it starts "
                                 f"({start})")
            cols = ", ".join(f"{k}=?" for k in sets)
            conn.execute(f"UPDATE action_items SET {cols}, updated_at=?, updated_by=? "
                         f"WHERE id=? AND COALESCE(rev,0)=?",
                         list(sets.values()) + [now, by, iid, rev])
            undo.append(dict({"id": iid}, **{k: row[k] for k in sets}))
        # R4.5b — link edits from the Gantt, in the same transaction as the dates.
        # Each op returns its own inverse, so one Undo puts the whole save back.
        undo_links = []
        for op in links:
            kind = op.get("op")
            if kind == "add":
                dep_id, existed = _dep_add(conn, op.get("pred"), op.get("succ"), by,
                                           op.get("lag") or 0,
                                           op.get("met_at") or (_now() if op.get("met")
                                                                else None))
                if not existed:
                    undo_links.append({"op": "remove", "id": dep_id})
            elif kind == "remove":
                was = _dep_remove(conn, op.get("id"), by)
                undo_links.append({"op": "add", "pred": was["pred"], "succ": was["succ"],
                                   "lag": was["lag"], "met_at": was["met_at"]})
            elif kind == "met":
                was = _dep_met(conn, op.get("id"), bool(op.get("met")), op.get("note") or "")
                undo_links.append({"op": "met", "id": int(op.get("id")), "met": was})
            else:
                raise ValueError(f"unknown link op {kind!r}")
        conn.commit()
        for u in undo:
            u["rev"] = (conn.execute("SELECT COALESCE(rev,0) FROM action_items WHERE id=?",
                                     (u["id"],)).fetchone() or [0])[0]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"saved": len(undo) + len(links), "undo": undo,
            "undo_links": list(reversed(undo_links))}


def push_dependents(pred_id: int, sha: str, by: str = "mt") -> dict:
    pv = push_preview(pred_id)
    if pv["sha"] != sha:
        raise ValueError("the sequence changed since the preview — look again")
    if not pv["moves"]:
        raise ValueError("nothing to move")
    undo = apply_dates([{"id": m["id"], "rev": m["rev"], "start_date": m["start_date"][1],
                         "end_date": m["end_date"][1]} for m in pv["moves"]], by=by)
    return {"moved": [m["id"] for m in pv["moves"]], "held": pv["held"], "undo": undo}


# --------------------------------------------------------------- predecessors (R4.2)
#
# Links live in action_item_deps: pred must finish before succ starts (FS). Many-to-many
# falls out of the shape. Never DELETEd — removing a link sets removed_at. The list page
# (R4.3) and the Gantt (R4.4/5) are the UI; this is the data layer they share.

def _live_links(conn) -> dict[int, list[int]]:
    succ: dict[int, list[int]] = {}
    for r in conn.execute("SELECT pred_id, succ_id FROM action_item_deps "
                          "WHERE removed_at IS NULL"):
        succ.setdefault(r[0], []).append(r[1])
    return succ


def _would_cycle(conn, pred: int, succ: int) -> bool:
    """Adding pred→succ closes a loop iff pred is already reachable from succ."""
    graph = _live_links(conn)
    stack, seen = [succ], set()
    while stack:
        n = stack.pop()
        if n == pred:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(graph.get(n, []))
    return False


def deps_for(iid: int) -> dict:
    """{'after': links where this item is the successor, 'unblocks': where it is the
    predecessor}, live links only, each with the other item's status and dates."""
    empty = {"after": [], "unblocks": []}
    conn = _connect()
    if conn is None:
        return empty
    try:
        if not r42_ready(conn):
            return empty
        out = {}
        for key, mine, other in (("after", "succ_id", "pred_id"),
                                 ("unblocks", "pred_id", "succ_id")):
            out[key] = [{k: r[k] for k in r.keys()} for r in conn.execute(
                f"SELECT d.id, d.{other} AS item_id, d.kind, d.lag_days, d.met_at, "
                f"d.met_note, a.status, a.description, a.start_date, a.end_date, "
                f"a.due_date, a.project_id FROM action_item_deps d "
                f"JOIN action_items a ON a.id = d.{other} "
                f"WHERE d.{mine}=? AND d.removed_at IS NULL ORDER BY d.{other}",
                (int(iid),))]
        return out
    except Exception:
        return empty
    finally:
        conn.close()


# Link writes, at connection level so one transaction can carry several (the Gantt
# tray's Save, R4.5b). The public add_dep / remove_dep / set_dep_met wrap them.
def _dep_add(conn, pred, succ, by: str, lag_days=0, met=None) -> tuple[int, bool]:
    """(link id, existed). Guards: both items exist, no self-link, no loop."""
    try:
        pred, succ = (int(str(x).strip().lstrip("#")) for x in (pred, succ))
        lag_days = int(lag_days or 0)
    except (TypeError, ValueError):
        raise ValueError("predecessor and successor are item numbers, like #919")
    if not 0 <= lag_days <= 260:
        raise ValueError("lag is 0 to 260 working days")
    if pred == succ:
        raise ValueError("an item cannot come after itself")
    for i in (pred, succ):
        if not conn.execute("SELECT 1 FROM action_items WHERE id=?", (i,)).fetchone():
            raise ValueError(f"no action item #{i}")
    live = conn.execute("SELECT id FROM action_item_deps WHERE pred_id=? AND succ_id=? "
                        "AND removed_at IS NULL", (pred, succ)).fetchone()
    if live:
        return live[0], True
    if _would_cycle(conn, pred, succ):
        raise ValueError(f"#{succ} already leads to #{pred} — linking #{pred} before "
                         f"#{succ} would make a loop")
    cur = conn.execute(
        "INSERT INTO action_item_deps (pred_id, succ_id, lag_days, met_at, created_at, "
        "created_by) VALUES (?,?,?,?,?,?)", (pred, succ, lag_days, met, _now(), by))
    return cur.lastrowid, False


def _dep_remove(conn, dep_id, by: str) -> dict:
    """Tombstone a live link; returns what it was (for an undo that re-adds it)."""
    row = conn.execute("SELECT pred_id, succ_id, lag_days, met_at FROM action_item_deps "
                       "WHERE id=? AND removed_at IS NULL", (int(dep_id),)).fetchone()
    if not row:
        raise ValueError("that link is already gone — Refresh")
    conn.execute("UPDATE action_item_deps SET removed_at=?, removed_by=? WHERE id=?",
                 (_now(), by, int(dep_id)))
    return {"pred": row[0], "succ": row[1], "lag": row[2], "met_at": row[3]}


def _dep_met(conn, dep_id, met: bool, note: str = "") -> bool:
    """Set or clear met-anyway; returns what it was before."""
    row = conn.execute("SELECT met_at FROM action_item_deps WHERE id=? AND removed_at IS "
                       "NULL", (int(dep_id),)).fetchone()
    if not row:
        raise ValueError("that link is gone — Refresh")
    conn.execute("UPDATE action_item_deps SET met_at=?, met_note=? WHERE id=?",
                 ((_now() if met else None),
                  ((note or "").strip()[:400] or None) if met else None, int(dep_id)))
    return bool(row[0])


def add_dep(pred, succ, by: str = "mt", lag_days: int = 0) -> dict:
    conn = _connect_w()
    try:
        if not r42_ready(conn):
            raise RuntimeError("predecessors need the R4.2 migration first")
        conn.execute("BEGIN IMMEDIATE")
        dep_id, existed = _dep_add(conn, pred, succ, by, lag_days)
        conn.commit()
        return {"ok": True, "id": dep_id, "existed": existed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_dep(dep_id: int, by: str = "mt") -> dict:
    conn = _connect_w()
    try:
        _dep_remove(conn, dep_id, by)
        conn.commit()
        return {"ok": True}
    except ValueError:
        raise ValueError("no such live link")
    finally:
        conn.close()


def set_dep_met(dep_id: int, met: bool, note: str = "", by: str = "mt") -> dict:
    """"Go ahead anyway" (decision 3): the link counts as satisfied while the
    predecessor stays open — its last cleanup can finish after the successor starts."""
    conn = _connect_w()
    try:
        _dep_met(conn, dep_id, met, note)
        conn.commit()
        return {"ok": True}
    except ValueError:
        raise ValueError("no such live link")
    finally:
        conn.close()


# --- R4.4: the Gantt's projection -------------------------------------------------------
#
# One request per view, a slim row (~150 B, not the list's ~2 KB) and only what the chart
# draws. Read-only: nothing here writes, and nothing here needs the R4.2 migration to have
# run — without it there are no plan dates or links, and the chart is due markers.
#
# Rows come out in the order they are drawn: grouped by project (a whole account shows
# each of its projects, then "No project"), and inside a group by sequence step, then by
# when the work begins (plan start, else plan end, else due — the same fallback R4.3
# uses), families kept together. The browser does no ordering of its own.

GANTT_CAP = 300                  # bars; above this the view is cut and says so
GANTT_DESC = 360                 # characters of description the tooltip carries
SORTS["plan"] = ("Plan", "plan_begin", "asc", "text")   # internal: never in `have`


def _who(r: dict, me) -> str:
    """me | shared | them | none — colour-by-who. `shared` is a multi-owner string that
    includes your exact name (`Alex Example / Sam Sample`); the exact-name rule from
    _me() still holds, so `Alex Sample` is never you."""
    ids, names = me
    if r.get("owner_contact_id") is not None and r["owner_contact_id"] in ids:
        return "me"
    # Phase 7: when an item has linked "with" people, they decide it — you are with it
    # (shared) or you are not (them). Items with none yet keep the text rule below.
    withs = r.get("_with") or []
    if withs:
        return "shared" if any(p["id"] in ids for p in withs) else "them"
    own = (r.get("owner") or "").strip().lower()
    if not own and r.get("owner_contact_id") is None:
        return "none"
    if r.get("owner_contact_id") is None and own in names:
        return "me"
    if any(re.search(r"(^|[^a-z])" + re.escape(n) + r"($|[^a-z])", own) for n in names):
        return "shared"
    return "them"


def gantt(project: str | None = None, customer: str | None = None,
          hide_done: bool = True, cap: int = GANTT_CAP) -> dict:
    """GET /api/tracking/gantt — one project (`project` = id or `none`) or one whole
    account (`customer` = short name). Returns rows in draw order, the links between
    rows in view, a summary per project group, the date range, and the newest
    item_events id (the live-update feed starts from it)."""
    out = {"rows": [], "links": [], "groups": [], "total": 0, "cut": False,
           "range": None, "today": dt.date.today().isoformat(), "last_event": 0,
           "title": "", "r42": False, "error": None}
    if not project and not customer:
        out["error"] = "Pick a project or an account."
        return out
    conn = _connect()
    if conn is None:
        out["error"] = "tracking DB not available"
        return out
    try:
        have = _cols(conn, "action_items")
        cmap = _customer_map(conn)
        r42 = r42_ready(conn)
        out["r42"] = r42
        w = _item_where(cmap, customer=customer, project=project, hide_done=hide_done)
        if w is None:
            out["error"] = "No such project or account."
            return out
        where, args = w
        cols = [c for c in ("id", "parent_id", "project_id", "customer_id", "description",
                            "owner", "owner_contact_id", "status", "priority", "due_date",
                            "start_date", "end_date", "completed_at", "item_type", "rev",
                            "title")
                if c in have]
        rows = [dict(r) for r in conn.execute(
            f"SELECT {', '.join('a.' + c for c in cols)} FROM action_items a "
            f"WHERE {where}", args)]
        for r in rows:
            r["plan_begin"] = _begin(r)
        out["total"] = len(rows)

        projects = {p["id"]: p for p in (dict(x) for x in conn.execute(
            "SELECT * FROM projects"))} if _cols(conn, "projects") else {}
        if project and project != "none":
            p = projects.get(int(project))
            out["title"] = p["name"] if p else f"Project {project}"
        elif project == "none":
            out["title"] = "No project" + (f" · {customer}" if customer else "")
        else:
            out["title"] = next((r["name"] for r in conn.execute(
                "SELECT name FROM customers WHERE short_name=?", (customer,))), customer)

        seq = _seq(conn) if r42 else None
        flags = _seq_flags(seq) if seq else {}
        ids = {r["id"] for r in rows}
        me = _me(conn)
        withs = people_for(conn, list(ids)) if p7_ready(conn) else {}
        for r in rows:
            r["_with"] = withs.get(r["id"], [])
        today = out["today"]

        groups: dict = {}
        for r in rows:
            groups.setdefault(r.get("project_id"), []).append(r)

        def gkey(pid):
            g = groups[pid]
            first = min((r["plan_begin"] for r in g if r["plan_begin"]), default="9999")
            return (pid is None, first, (projects.get(pid) or {}).get("name") or "")

        drawn, dates = [], [today]
        for pid in sorted(groups, key=gkey):
            g = groups[pid]
            steps = _steps({r["id"] for r in g}, seq) if seq else None
            ordered = _order(g, "plan", "asc", prefix=steps)
            p = projects.get(pid) or {}
            begins = [r["plan_begin"] for r in g if r["plan_begin"]]
            ends = [x for r in g for x in (_finish(r), r.get("completed_at", "")[:10]
                                            if r.get("completed_at") else None) if x]
            live = [r for r in g if not _is_done(r)]
            grp = {"id": pid, "name": p.get("name") or "No project",
                   "customer": cmap.get(p.get("customer_id")) if p else None,
                   "target": p.get("target_date"), "status": p.get("status"),
                   "start": min(begins) if begins else None,
                   "end": max(ends) if ends else None,
                   "n": len(g), "live": len(live),
                   "overdue": sum(1 for r in live if r.get("due_date")
                                  and r["due_date"] < today)}
            out["groups"].append(grp)
            for o in ordered:
                r = next(x for x in g if x["id"] == o["id"])
                f = flags.get(r["id"]) or {}
                done_at = (r.get("completed_at") or "")[:10] or None
                row = {"id": r["id"], "g": pid,
                       "t": label(r),
                       "titled": bool(r.get("title")),
                       # the detail, for the tooltip — whether an item belongs is judged
                       # on this, not on the headline
                       "desc": " ".join((r.get("description") or "").split())[:GANTT_DESC],
                       "type": r.get("item_type") or "task",
                       "status": r.get("status") or "open",
                       "prio": r.get("priority"),
                       "owner": r.get("owner") or "",
                       "who": _who(r, me),
                       "with": [p["name"] for p in r.get("_with") or []],
                       "cust": cmap.get(r.get("customer_id")),
                       "parent": r.get("parent_id"),
                       "depth": min(o["depth"], 2), "kids": o["kids"],
                       "start": r.get("start_date") or None,
                       "end": r.get("end_date") or None,
                       "due": r.get("due_date") or None,
                       "done_at": done_at, "rev": r.get("rev"),
                       "step": steps.get(r["id"]) if steps else None,
                       "blocked": bool(f.get("blocked_by")),
                       "blocked_by": list(f.get("blocked_by") or []),
                       # all predecessors finished (or met anyway) and still open
                       "ready": bool(f.get("after")) and not f.get("blocked_by")
                                and (r.get("status") or "open") == "open",
                       "conflict": bool(f.get("conflicts")),
                       "overdue": bool(r.get("due_date") and r["due_date"] < today
                                       and not _is_done(r)),
                       # links whose other end is not on this chart, so a row never
                       # silently loses a dependency the list would show
                       "ext_after": [a["id"] for a in f.get("after", [])
                                     if a["id"] not in ids],
                       "ext_unblocks": [a["id"] for a in f.get("unblocks", [])
                                        if a["id"] not in ids]}
                drawn.append(row)
                dates += [d for d in (row["start"], row["end"], row["due"], done_at) if d]
            if grp["target"]:
                dates.append(grp["target"])

        if len(drawn) > cap:
            drawn, out["cut"] = drawn[:cap], True
        out["rows"] = drawn
        shown = {r["id"] for r in drawn}
        if seq:
            items = seq["items"]
            for l in seq["links"]:
                if l["pred_id"] in shown and l["succ_id"] in shown:
                    p_, s_ = items[l["pred_id"]], items[l["succ_id"]]
                    out["links"].append({
                        "id": l["id"], "pred": l["pred_id"], "succ": l["succ_id"],
                        "lag": l["lag_days"] or 0, "met": bool(l["met_at"]),
                        "done": _is_done(p_),
                        "conflict": bool(_conflict(l, p_, s_))})

        lo, hi = _d(min(dates)), _d(max(dates))
        lo -= dt.timedelta(days=7)
        hi = max(hi + dt.timedelta(days=14), lo + dt.timedelta(days=35))
        out["range"] = {"from": lo.isoformat(), "to": hi.isoformat()}
        if r42:
            try:
                out["last_event"] = conn.execute(
                    "SELECT COALESCE(max(id), 0) FROM item_events").fetchone()[0]
            except sqlite3.Error:
                pass
        return out
    except Exception as e:
        out["error"] = f"gantt failed — {e}"
        return out
    finally:
        conn.close()


# --- R4.4b: the live-update feed ------------------------------------------------------
#
# Polled every 15 s by a VISIBLE list or Gantt tab (hidden tabs do not poll), and at once
# when another mt window says it wrote. Normally the answer is an empty list — a few
# bytes. item_events catches every writer (mt, the assistant's sqlite3, scripts) because it is
# fed by triggers, inserts included. Links and project rows have no events, so their
# state rides along as a short signature: any add / remove / met-anyway / project edit
# changes it, and the page refetches.

CHANGES_MAX = 500


def changes(since: int = -1) -> dict:
    """Item ids changed after item_events id `since`. `since` < 0 asks only for the head
    (where to start). `more` = over CHANGES_MAX — the page should refresh, not patch."""
    out = {"last": 0, "ids": [], "more": False, "sig": "", "ready": False}
    conn = _connect()
    if conn is None:
        return out
    try:
        if not r42_ready(conn):
            return out
        out["ready"] = True
        head = conn.execute("SELECT COALESCE(max(id), 0) FROM item_events").fetchone()[0]
        out["last"] = head
        if since is not None and int(since) >= 0 and head > int(since):
            rows = conn.execute(
                "SELECT id, item_id FROM item_events WHERE id > ? ORDER BY id LIMIT ?",
                (int(since), CHANGES_MAX + 1)).fetchall()
            if len(rows) > CHANGES_MAX:
                out["more"] = True
            seen = []
            for r in rows[:CHANGES_MAX]:
                if r["item_id"] not in seen:
                    seen.append(r["item_id"])
            out["ids"] = seen
        d = conn.execute(
            "SELECT count(*), COALESCE(max(id), 0), count(met_at), count(removed_at) "
            "FROM action_item_deps").fetchone()
        pc = _cols(conn, "projects")
        p = conn.execute(
            "SELECT count(*), " + ("COALESCE(max(updated_at), '')" if "updated_at" in pc
                                   else "''") + ", "
            + ("count(archived_at)" if "archived_at" in pc else "0")
            + " FROM projects").fetchone()
        out["sig"] = "d{}.{}.{}.{}|p{}.{}.{}".format(*tuple(d), *tuple(p))
        return out
    except Exception:
        return out
    finally:
        conn.close()


# --- Phase 7: owners → contacts -----------------------------------------------------------
#
# Every action item's free-text `owner` gets linked to real contact rows. The rules
# (Oct 16 2026): the FIRST person named owns it (owner_contact_id — drives Mine, and the
# Jira assignee later); everyone else named is linked as "with" (item_people). In "A -> B"
# the left side owns and the right side is "with". A bare first name equal to the
# configured owner's first name ("Alex") is the owner (Alex Example) — the published
# tracker's long-standing rule. Internal people carried once per account (one contact on
# two accounts — deliberate, confirmed Sep 15) resolve to the row on the item's
# own account; nothing is merged.
#
# Person Verification is a hard rule: only an EXACT name (or a recorded alias) that
# resolves to exactly one person is proposed automatically, and even then the user approves
# the group on Hygiene. Anything else — a note in brackets, a first name, a team, a name
# not in contacts — goes to the per-spelling review on /tracking/owners. Decisions there
# are remembered (owner_decisions) so the next row with the same spelling is proposed
# with them. The free-text `owner` is never rewritten: it stays as history.

P7_TABLES = [
    """CREATE TABLE IF NOT EXISTS item_people (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES action_items(id),
        contact_id INTEGER NOT NULL REFERENCES contacts(id),
        role TEXT NOT NULL DEFAULT 'with',
        created_at TEXT NOT NULL,
        created_by TEXT,
        removed_at TEXT,
        removed_by TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_item_people_live "
    "ON item_people(item_id, contact_id, role) WHERE removed_at IS NULL",
    "CREATE INDEX IF NOT EXISTS ix_item_people_item ON item_people(item_id)",
    "CREATE INDEX IF NOT EXISTS ix_item_people_contact ON item_people(contact_id)",
    """CREATE TABLE IF NOT EXISTS owner_decisions (
        id INTEGER PRIMARY KEY,
        spelling TEXT NOT NULL UNIQUE,      -- owner_key() of the raw text
        example TEXT,                       -- one raw spelling, for the record
        kind TEXT NOT NULL,                 -- people | not_person
        owner_name TEXT,                    -- canonical contact names, resolved per row
        with_names TEXT,                    --   by the item's account (JSON list)
        pick_json TEXT,                     -- {name: contact id} when a name needs a pick
        note TEXT,
        decided_at TEXT NOT NULL,
        decided_by TEXT)""",
]


def p7_ready(conn) -> bool:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {"item_people", "owner_decisions"} <= have


def migrate_p7(db_path) -> list[str]:
    conn = sqlite3.connect(str(db_path), timeout=TIMEOUT)
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        for sql in P7_TABLES:
            conn.execute(sql)
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        conn.commit()
        return sorted(after - before)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_OWNER_NOTE = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")
_OWNER_TAIL = re.compile(r"\s+(?:--|—|–)\s+(.*)$")
_OWNER_ARROW = re.compile(r"\s*(?:-->|->|=>|→)\s*")
_OWNER_SPLIT = re.compile(r"\s*(?:/|&|\+|,|;|\band\b)\s*", re.I)


def owner_key(raw: str) -> str:
    return " ".join((raw or "").lower().split())


def parse_owner(raw: str) -> dict:
    """{owner, with: [...], note} from a free-text owner. Names are as written (bare
    first name of the configured owner expanded); `note` holds bracketed or trailing
    "-- …" text, which always sends the row to review (it can change who owns it)."""
    text = " ".join((raw or "").split())
    notes = [a or b for a, b in _OWNER_NOTE.findall(text)]
    text = _OWNER_NOTE.sub(" ", text)
    m = _OWNER_TAIL.search(text)
    if m:
        notes.append(m.group(1))
        text = text[:m.start()]
    text = " ".join(text.split()).strip(" -–—:;.")
    me = (getattr(P, "OWNER", "") or "").strip()
    me_first = me.split()[0].lower() if me else ""

    def names(s):
        out = []
        for n in _OWNER_SPLIT.split(s):
            n = " ".join(n.split()).strip(" -–—:;.")
            if not n:
                continue
            if me_first and n.lower() == me_first:
                n = me
            out.append(n)
        return out

    parts = _OWNER_ARROW.split(text, maxsplit=1)
    left = names(parts[0])
    right = names(parts[1]) if len(parts) > 1 else []
    everyone = left + right
    seen, uniq = set(), []
    for n in everyone:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return {"owner": uniq[0] if uniq else None, "with": uniq[1:],
            "note": "; ".join(n.strip() for n in notes if n.strip()) or None}


def _contact_index(conn) -> dict:
    """Live contacts by lower-case name and by alias, plus the same-person rulings."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, customer_id, aliases, role, company, not_duplicate_of, "
        "verification_status FROM contacts WHERE merged_into IS NULL AND archived_at IS NULL")]
    by_name, by_alias = {}, {}
    for r in rows:
        by_name.setdefault(" ".join(r["name"].lower().split()), []).append(r)
        for a in re.split(r"[;,]", r.get("aliases") or ""):
            a = " ".join(a.lower().split())
            if a:
                by_alias.setdefault(a, []).append(r)
    affil = set(P.AFFILIATIONS)
    for a in P.ACCOUNTS.values():
        affil |= set(a["aliases"]) | {v.lower() for v in (a["code"], a["label"]) if v}
    for c in conn.execute("SELECT short_name, name FROM customers"):
        for v in (c["short_name"], c["name"], ACCOUNT_CODE.get(c["short_name"] or "")):
            if v:
                affil.add(v.strip().lower())
    return {"by_name": by_name, "by_alias": by_alias, "affil": affil,
            "by_id": {r["id"]: r for r in rows}}


def resolve_person(name: str, customer_id, idx: dict) -> dict:
    """{id} when the name is exactly one person (on this account, for people carried once
    per account), {pick: [ids], same_person: bool} when it is one person on several rows
    and none is on this account (a human picks the row), {ambiguous: [ids]} when it could
    be different people, {missing: True} when no contact has the name."""
    key = " ".join((name or "").lower().split())
    hits = idx["by_name"].get(key) or idx["by_alias"].get(key) or []
    if not hits:
        return {"missing": True}
    def ok(h):
        # Two more guards before anything is linked without the user looking at it: the
        # contact itself is verified, and a first-name-only contact ("Pat", a GLX
        # contact) only matches on its own account — elsewhere it could be anyone.
        if (h.get("verification_status") or "") != "verified":
            return {"unverified": h["id"]}
        if " " not in h["name"].strip() and h["customer_id"] != customer_id:
            return {"first_name_only": h["id"]}
        return {"id": h["id"]}
    if len(hits) == 1:
        return ok(hits[0])
    mine = [h for h in hits if customer_id is not None and h["customer_id"] == customer_id]
    if len(mine) == 1:
        return ok(mine[0])
    ids = [h["id"] for h in hits]
    nd = lambda h: {int(x) for x in re.findall(r"\d+", h.get("not_duplicate_of") or "")}
    same = all(set(ids) - {h["id"]} <= nd(h) for h in hits) and \
        len({h["customer_id"] for h in hits}) == len(hits)
    return {"pick": ids, "same_person": True} if same else {"ambiguous": ids}


def _decision_for(conn, key: str):
    try:
        r = conn.execute("SELECT * FROM owner_decisions WHERE spelling=?", (key,)).fetchone()
        return dict(r) if r else None
    except sqlite3.Error:
        return None


def owner_plan(conn, row: dict, idx: dict) -> dict:
    """What linking one item's owner would do: {status: clean | review | not_person,
    owner_id, with_ids, owner_name, with_names, reasons, decision}."""
    key = owner_key(row.get("owner"))
    dec = _decision_for(conn, key)
    if dec and dec["kind"] == "not_person":
        return {"status": "not_person", "reasons": ["decided: not a person"], "decision": True}
    if dec:
        owner, withs, note = dec["owner_name"], json.loads(dec["with_names"] or "[]"), None
        picks = {k.lower(): v for k, v in json.loads(dec["pick_json"] or "{}").items()}
    else:
        p = parse_owner(row.get("owner"))
        owner, withs, note, picks = p["owner"], p["with"], p["note"], {}
    out = {"owner_name": owner, "with_names": withs, "owner_id": None, "with_ids": [],
           "reasons": [], "decision": bool(dec)}
    # A note that only says where someone works — "(ACM)", "(GLX)", "(Acme)" — does not
    # change who owns the item. Any other note ("pending handoff to …", "unconfirmed",
    # "confirm which") might, so it goes to the user.
    if note and not all(seg.strip().lower() in idx.get("affil", set())
                        for seg in note.split(";") if seg.strip()):
        out["reasons"].append(f"note: {note}")
    if not owner:
        out["reasons"].append("no name")
    for i, n in enumerate([owner] + list(withs)):
        if not n:
            continue
        r = resolve_person(n, row.get("customer_id"), idx)
        cid = r.get("id") or picks.get(n.lower())       # a decision's pick wins
        if cid is None:
            out["reasons"].append(
                f"{n}: not in contacts" if r.get("missing") else
                f"{n}: one person, several account rows — pick one" if r.get("pick") else
                f"{n}: that contact is not verified yet" if r.get("unverified") else
                f"{n}: first-name-only contact on another account" if r.get("first_name_only")
                else f"{n}: more than one contact has this name")
            continue
        if i == 0:
            out["owner_id"] = cid
        elif cid != out["owner_id"] and cid not in out["with_ids"]:
            out["with_ids"].append(cid)
    out["status"] = "clean" if not out["reasons"] and out["owner_id"] else "review"
    return out


def unlinked_owner_rows(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, owner, customer_id, status, title, description FROM action_items "
        "WHERE owner_contact_id IS NULL AND COALESCE(TRIM(owner),'') <> '' ORDER BY id")]


def people_for(conn, ids: list[int]) -> dict:
    """item id → [{pid, id, name, role}] — the "with" people, for display."""
    out: dict[int, list] = {}
    if not ids:
        return out
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(
                    f"SELECT p.id AS pid, p.item_id, c.id, c.name, p.role FROM item_people p "
                    f"JOIN contacts c ON c.id = p.contact_id WHERE p.removed_at IS NULL AND "
                    f"p.item_id IN ({', '.join('?' * len(chunk))}) ORDER BY p.id", chunk):
                out.setdefault(r["item_id"], []).append(
                    {"pid": r["pid"], "id": r["id"], "name": r["name"], "role": r["role"]})
    except sqlite3.Error:
        pass
    return out


def set_item_person(item_id, contact_id=None, remove_pid=None, by: str = "mt") -> dict:
    """Add a "with" person to an item, or remove one (tombstone)."""
    conn = _connect_w()
    try:
        if not p7_ready(conn):
            raise RuntimeError("needs the Phase 7 migration first")
        if remove_pid:
            cur = conn.execute("UPDATE item_people SET removed_at=?, removed_by=? WHERE id=? "
                               "AND item_id=? AND removed_at IS NULL",
                               (_now(), by, int(remove_pid), int(item_id)))
            conn.commit()
            if not cur.rowcount:
                raise ValueError("already removed")
            return {"ok": True}
        cid = int(contact_id)
        if not conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone():
            raise ValueError("no such contact")
        own = conn.execute("SELECT owner_contact_id FROM action_items WHERE id=?",
                           (int(item_id),)).fetchone()
        if not own:
            raise ValueError("no such item")
        if own[0] == cid:
            raise ValueError("that person already owns it")
        conn.execute("INSERT OR IGNORE INTO item_people (item_id, contact_id, role, "
                     "created_at, created_by) VALUES (?,?,'with',?,?)",
                     (int(item_id), cid, _now(), by))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def bulk_move(ids: list[int], fields: dict, by: str = "mt") -> dict:
    """Move many items at once: into a project (`project_id`, "" = none) or under a
    parent (`parent_id`, "" = top level). Each row goes through update_item(), so the
    shape rules are exactly the edit sheet's — one level of nesting, a sub-item follows
    its parent's project, a parent's project change carries its sub-items. A row the
    rules refuse lands in `skipped` with the sentence that says why; the rest still move.

    Parents go first, so selecting a parent with some of its sub-items moves the family
    once; those sub-items report as `carried`."""
    keys = [k for k in ("project_id", "parent_id") if k in (fields or {})]
    if len(keys) != 1:
        raise ValueError("move into a project, or under a parent — one at a time")
    key = keys[0]
    raw = fields[key]
    val = None if _empty(raw) else str(raw).strip().lstrip("#")
    if val is not None and not val.isdigit():
        raise ValueError(f"{key} must be a number (got {raw!r})")
    val = int(val) if val is not None else None

    conn = _connect_w()
    try:
        if val is not None:
            table = "projects" if key == "project_id" else "action_items"
            row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (val,)).fetchone()
            if not row:
                raise ValueError(f"no {'project' if table == 'projects' else 'item'} #{val}")
            if key == "parent_id" and row["parent_id"]:
                raise ValueError(f"#{val} is itself a sub-item of #{row['parent_id']} — "
                                 f"nesting stops at one level. Pick #{row['parent_id']}.")
        cur = {r["id"]: dict(r) for r in conn.execute(
            f"SELECT id, parent_id, project_id FROM action_items WHERE id IN "
            f"({', '.join('?' * len(ids))})", ids)}
    finally:
        conn.close()

    order = sorted(ids, key=lambda i: (bool((cur.get(i) or {}).get("parent_id")), ids.index(i)))
    changed, skipped, carried = [], [], set()
    for iid in order:
        if iid not in cur:
            skipped.append({"id": iid, "why": "no such item"})
            continue
        if iid in carried:
            continue
        if cur[iid][key] == val:
            skipped.append({"id": iid, "why": "already there"})
            continue
        try:
            out = update_item(iid, {key: val}, by=by)
            changed.append(iid)
            for c in (out or {}).get("carried") or []:
                carried.add(c)
        except Exception as e:
            skipped.append({"id": iid, "why": str(e)})
    return {"changed": changed, "skipped": skipped, "carried": sorted(carried),
            "fields": {key: val}}


def bulk_update(ids, fields: dict, by: str = "mt") -> dict:
    """Set the same field(s) on many rows at once — the difference between closing out
    a quarter of stale items in one go and clicking 200 times.

    Canonical values ONLY. The "an unchanged legacy value still passes" escape hatch in
    update_item() exists so a stale row is not held hostage while you fix a typo; a bulk
    write is a deliberate choice about exactly these rows, so there is no reason to let
    one set `closed` on 200 of them. Only the three controlled fields are bulk-settable —
    descriptions and owners are per-row judgements and are deliberately not offered.

    One bad id never drops the rest: it lands in `skipped` and the others still commit."""
    try:
        ids = [int(i) for i in (ids or [])]
    except (TypeError, ValueError):
        raise ValueError("bad selection")
    ids = list(dict.fromkeys(ids))                    # de-dupe, keep order
    if not ids:
        raise ValueError("nothing selected")
    if len(ids) > BULK_MAX:
        raise ValueError(f"{len(ids)} rows selected — {BULK_MAX} is the most in one go")

    if any(k in (fields or {}) for k in ("project_id", "parent_id")):
        return bulk_move(ids, fields, by=by)

    data = {k: v for k, v in (fields or {}).items() if k in FIELD_VOCAB}
    if not data:
        raise ValueError("pick a field to set")
    for f, v in data.items():
        if v not in FIELD_VOCAB[f]:
            raise ValueError(
                f"{f} must be one of: {', '.join(FIELD_VOCAB[f])} (got {v!r})")

    conn = _connect_w()
    changed: list[int] = []
    skipped: list[dict] = []
    try:
        have = _cols(conn, "action_items")
        row = dict(data)
        if "updated_at" in have:
            row["updated_at"] = _now()
        if "updated_by" in have:
            row["updated_by"] = by
        sets = ", ".join(f"{c}=?" for c in row)
        vals = [row[c] for c in row]
        for iid in ids:
            if not conn.execute("SELECT 1 FROM action_items WHERE id=?",
                                (iid,)).fetchone():
                skipped.append({"id": iid, "why": "no such item"})
                continue
            conn.execute(f"UPDATE action_items SET {sets} WHERE id=?", vals + [iid])
            changed.append(iid)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return {"changed": changed, "skipped": skipped, "fields": data}


# ------------------------------------------------------------------------- CLI

def _canonical_dbs() -> set[Path]:
    """Every path that could be the real DB. paths.DB alone is NOT enough: run from a git
    worktree (or any other checkout), paths.DB falls back to ~/mt-data while the live DB is
    whatever the main install's config.json names — which is exactly how the Oct 12 2026
    R4.2 run reached the live DB while believing it was a copy. So every mt install above
    this file (a config.json beside an `mt` script) adds its "db" too."""
    out = {P.DB.resolve(), Path(DB_PATH).resolve()}
    for up in Path(__file__).resolve().parents:
        cfg = up / "config.json"
        if not ((up / "mt").is_file() and cfg.is_file()):
            continue
        try:
            db = json.loads(cfg.read_text()).get("db")
        except Exception:
            db = None
        if db:
            out.add(Path(os.path.expandvars(os.path.expanduser(db))).resolve())
    return out


def _is_canonical(target: Path) -> bool:
    """The live DB, or anything with its file name (a copy must be renamed to be migrated)."""
    t = Path(target).resolve()
    canon = _canonical_dbs()
    return t in canon or t.name in {c.name for c in canon}


def _main(args: list[str]) -> int:
    global DB_PATH

    if args and args[0] == "--migrate":
        if len(args) < 2:
            print("usage: tracking.py --migrate /path/to/copy.db", file=sys.stderr)
            return 2
        target = Path(args[1]).resolve()
        if _is_canonical(target):
            print(f"refusing to migrate the live DB {target} — "
                  "back it up and migrate the copy", file=sys.stderr)
            return 3
        DB_PATH = target
        added = ensure_schema()
        print(f"{target}: added {added or '(nothing — already migrated)'}")
        return 0

    if args and args[0] == "--leases":
        rows = active_leases([a.lstrip("#") for a in args[1:]] or None)
        if not rows:
            print("no unsaved Gantt edits holding any item")
            return 0
        for r in rows:
            print(f"  #{r['item_id']:<5} held by {r['label'] or 'mt'} ({r['holder'][:8]}) "
                  f"until {r['expires_at'][11:16]} — "
                  f"{(r['title'] or r['description'] or '')[:70]}")
        return 0

    if args and args[0] in ("--migrate-r42", "--r42-rollback", "--migrate-r44c",
                            "--r44c-rollback", "--migrate-r45b", "--migrate-p7"):
        phase = ("p7" if "p7" in args[0] else "r45b" if "r45b" in args[0]
                 else "r44c" if "r44c" in args[0] else "r42")
        live = "--live" in args[1:]
        drafts = None
        rest = []
        it = iter(args[1:])
        for a in it:
            if a == "--live":
                continue
            if a == "--title-drafts":
                f = next(it, None)
                if phase != "r44c" or not f or not Path(f).exists():
                    print("--title-drafts needs an existing JSON file, with --migrate-r44c",
                          file=sys.stderr)
                    return 2
                import json
                drafts = json.loads(Path(f).read_text())
                continue
            rest.append(a)
        if live:
            # --live names the DB explicitly rather than trusting paths.DB, for the
            # reason in _canonical_dbs(). It must be the live DB, and it must exist.
            if not rest or not _is_canonical(Path(rest[0])) or not Path(rest[0]).exists():
                print("usage: tracking.py " + args[0] + " --live <path to the live DB>",
                      file=sys.stderr)
                return 2
            real = target = Path(rest[0]).resolve()
        elif rest:
            target = Path(rest[0]).resolve()
            if _is_canonical(target):
                print("that is the live DB — use --live <path>, which backs it up first",
                      file=sys.stderr)
                return 3
        else:
            print(f"usage: tracking.py {args[0]} /path/to/copy.db | --live",
                  file=sys.stderr)
            return 2
        if args[0].endswith("-rollback"):
            fn = rollback_r44c_triggers if phase == "r44c" else rollback_r42_triggers
            print(f"{target}: dropped {fn(target) or 'nothing'}")
            return 0
        if live:
            bk = real.parent / "backups" / (
                f"{real.stem}-{dt.date.today().isoformat()}-pre-{phase}.db")
            bk.parent.mkdir(exist_ok=True)
            if bk.exists():
                bk = bk.with_name(bk.stem + dt.datetime.now().strftime("-%H%M%S") + ".db")
            s_, d_ = sqlite3.connect(str(real), timeout=TIMEOUT), sqlite3.connect(str(bk))
            try:
                s_.backup(d_)
                ok = d_.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                d_.close()
                s_.close()
            if ok != "ok":
                print(f"backup failed its check ({ok}) — nothing migrated", file=sys.stderr)
                return 4
            print(f"backup: {bk}")
        done = (migrate_r44c(target, drafts) if phase == "r44c"
                else migrate_r45b(target) if phase == "r45b"
                else migrate_p7(target) if phase == "p7" else migrate_r42(target))
        print(f"{target}: {', '.join(done) if done else 'already migrated'}")
        if phase not in ("r45b", "p7"):      # these add tables only, no triggers
            print(f"rollback (triggers only): python3 tracking.py --{phase}-rollback "
                  f"{'--live ' if live else ''}{target}")
        return 0

    if not args or args[0] == "--stats":
        s = stats()
        for k in ("total", "open", "overdue", "unset_scope", "legacy_values",
                  "distinct_owners", "owner_linked", "projects"):
            print(f"{k:>16}: {s[k]}")
        return 0

    if args[0] == "--vocab":
        for field, vals in FIELD_VOCAB.items():
            print(f"{field:>13}: {', '.join(vals)}")
        return 0

    if args[0] == "--drift":
        rows = drift()
        if not rows:
            print("no legacy values left")
            return 0
        print(f"{sum(r['n'] for r in rows)} row(s) holding a legacy value\n")
        for r in rows:
            to = r["proposed"] or "?? needs your call"
            print(f"  {r['field']:>13}  {r['value']:<14} {r['n']:>4}  ->  {to}")
        return 0

    rows = list_items(q=" ".join(args))
    print(f"{len(rows)} match(es)\n")
    for r in rows:
        flag = " *" if r["legacy_fields"] else "  "
        print(f"  #{r['id']:<5}{flag}{(r['customer'] or '-'):<14} "
              f"{r['status']:<10} {(r['owner'] or '-'):<22} "
              f"{(r['description'] or '')[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

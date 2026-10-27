#!/usr/bin/env python3
"""
contacts.py — the data layer for the mt contacts page.

Reads and writes the `contacts` table in the canonical workspace DB. Everything here
is defensive on purpose: `mt web` imports this lazily and a broken/locked/missing DB
must degrade to an empty page, never a 500.

House rules baked in (see CONTACTS-CONTRACT.md):
  * schema changes are ALTER TABLE ... ADD COLUMN only
  * merging NEVER deletes a row — it sets a `merged_into` tombstone
  * read functions return empty/None instead of raising; write functions may raise

CLI (for development against a COPY of the DB):
    python3 contacts.py --stats
    python3 contacts.py --dupes
    python3 contacts.py --migrate /tmp/reg/test.db
    python3 contacts.py "search term"
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import datetime as dt
from pathlib import Path

import paths as P

HERE = Path(__file__).resolve().parent
WORKSPACE = P.WORKSPACE

# Module-level so tests (and the CLI --migrate flag) can point at a copy.
DB_PATH = Path(os.environ.get("MT_CONTACTS_DB") or P.DB)
VOICEPRINTS = Path(os.environ.get("MT_VOICEPRINTS") or (HERE / "voiceprints"))

TIMEOUT = 5.0

# Columns ensure_schema() adds. Nullable, no defaults — adding them rewrites no rows.
NEW_COLUMNS = [
    ("phone", "TEXT"),
    ("company", "TEXT"),
    ("aliases", "TEXT"),
    ("updated_at", "TEXT"),
    ("merged_into", "INTEGER"),
    # "Forget this one for now." NOT a delete — see archive() for why deleting a contact
    # is the one thing that guarantees they come back. Archived rows stay resolvable by
    # name; they are only hidden from the list, the pickers and the duplicate scan.
    ("archived_at", "TEXT"),
    ("archived_reason", "TEXT"),
    # Reserved for the deferred org-chart pass: self-reference to contacts(id).
    # Shipped now so that feature needs no second ALTER TABLE against the canonical
    # main DB. Ships empty and inert — nothing populates or reads it, and it
    # is deliberately NOT in WRITABLE. It is carried in the returned dicts like every
    # other column, and merge() fills it under the ordinary empty-field rule.
    ("reports_to", "INTEGER"),
    # "these two are NOT the same person" — comma-separated contact ids, written
    # symmetrically (each row names the other). It lives in the DB rather than in the
    # browser for one reason: the assistant has to be able to see it. Four of the twelve
    # duplicate-name groups were one internal person carried deliberately once per account
    # (e.g. Alex Example on acme and on globex); a ruling that only the owner's browser
    # knows about means those get re-raised forever. Not in WRITABLE — only
    # dismiss/undismiss touch it.
    ("not_duplicate_of", "TEXT"),
]

BASE_COLUMNS = [
    "id", "customer_id", "name", "email", "role", "notes",
    "created_at", "verification_status", "source", "slack_user_id",
]
ALL_COLUMNS = BASE_COLUMNS + [c for c, _ in NEW_COLUMNS]

# Fields a caller may set through create_contact/update_contact. `id`, `created_at`,
# `merged_into` and `reports_to` are deliberately not writable from the UI.
WRITABLE = {
    "customer_id", "name", "email", "role", "notes", "verification_status",
    "source", "slack_user_id", "phone", "company", "aliases",
}

STATUSES = ("verified", "needs_confirmation")

# What each field is FOR, and what actually changes downstream when you set it. Shown in
# the contact sheet behind a "Field guide" toggle, and served with /api/contacts so the
# page never hardcodes it. Written as consequences, not definitions. Every claim was
# checked against the code on Sep 22 2026 — in particular the voiceprint coupling, which
# is real and is the least obvious thing on this form.
FIELD_HELP = {
    "name":
        "The canonical spelling, and what the Person Verification Protocol checks names "
        "against. Two things happen when you change it. `mt` writes this exact string "
        "onto a transcript when a voiceprint matches — and the voiceprint link is looked "
        "up by the SLUG of this name, so renaming here silently unlinks an enrolled "
        "voice until you also run `mt rename`. If you are fixing a garbled spelling, add "
        "it under Also known as instead of editing this.",
    "role":
        "Free text, display only. Your org chart is authoritative for internal titles "
        "— copy from there rather than inventing one.",
    "email":
        "The strongest way to tell two people with the same name apart, and how this "
        "person is recognised across mail sweeps. Worth filling even when you have "
        "nothing else.",
    "phone":
        "Reference only — nothing in the tool reads it.",
    "company":
        "For people with no customer account: your company, a reseller, a partner. Account "
        "below is for customer-side people; this is for everyone else. Watch the spelling — "
        "`Acme Corp` and `Acme` side by side is exactly the kind of split that makes a "
        "group-by lie.",
    "customer_id":
        "Which customer this person belongs to. It filters the contact list and labels "
        "them in the owner picker on the tracking page. Leave it empty for internal staff "
        "and partners and use Company instead.",
    "slack_user_id":
        "The one field on this form with a direct effect on what gets posted. A weekly "
        "update turns an owner into an @-mention from this id, so an internal person "
        "without one is named in plain text instead. **Internal people only** — "
        "customer-side contacts are not in your Slack and should be left blank.",
    "source":
        "Where this record came from. It is the provenance the verification protocol "
        "leans on: `Gong participant list 2026-09-17` is worth a great deal more than a "
        "blank when someone later asks how we know this person exists.",
    "aliases":
        "One spelling per line, and the highest-leverage field here. Name matching checks "
        "the name PLUS every alias, so an alias is what stops a garbled transcript "
        "spelling becoming a second contact row. Add the WRONG spelling here — never "
        "edit the transcript to fix a name.",
    "notes":
        "Your working record. Per the house rule nothing auto-edits this — no tool "
        "invents, corrects or 'improves' what you write about a real person.",
    "verification_status":
        "A flag, not a gate — nothing in `mt` is blocked by it. The hard rule runs the "
        "other way: never promote `needs_confirmation` to `verified` without confirming "
        "the person is real and correctly named. That rule exists because a garbled "
        "transcript name was once propagated as a real owner.",
}


def field_help() -> dict:
    """The contact form's field guide. Static; safe to call any time."""
    return dict(FIELD_HELP)


# Fields merge() will copy from the dropped row when the keeper's value is empty.
MERGE_FILL = [
    "customer_id", "email", "role", "phone", "company", "source", "slack_user_id",
    "reports_to",
]


# --------------------------------------------------------------------------- utils

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
        raise RuntimeError(f"contacts DB not found: {p}")
    conn = sqlite3.connect(str(p), timeout=TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(conn) -> list[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(contacts)")]
    except Exception:
        return []


def _select_list(have: list[str]) -> str:
    """Select every contract column, substituting NULL for ones not yet migrated in."""
    parts = []
    for c in ALL_COLUMNS:
        parts.append(f"c.{c}" if c in have else f"NULL AS {c}")
    return ", ".join(parts)


def _alive(have: list[str], archived: str = "live") -> str:
    """WHERE fragment for which rows count as real (works pre-migration too).

    `archived`: 'live' hides archived rows, 'archived' shows only them, 'all' shows both.
    Tombstoned (merged) rows are excluded in every mode — a merge means the row is not a
    person any more, which is different from a person you have stopped dealing with."""
    bits = ["c.merged_into IS NULL"] if "merged_into" in have else ["1=1"]
    if "archived_at" in have:
        if archived == "live":
            bits.append("c.archived_at IS NULL")
        elif archived == "archived":
            bits.append("c.archived_at IS NOT NULL")
    return " AND ".join(bits)


def _row_to_dict(row, vp_slugs: set[str] | None = None) -> dict:
    d = {k: row[k] for k in row.keys()}
    if "customer" not in d:
        d["customer"] = None
    d["has_voiceprint"] = _slug(d.get("name") or "") in (
        vp_slugs if vp_slugs is not None else _voiceprint_slugs()
    )
    return d


# ----------------------------------------------------------------- voiceprints / mt

_vp_cache: dict = {"mtime": None, "by_slug": {}}


def _slug(s: str) -> str:
    """Reuse mt.slugify. Imported lazily — importing mt must not be a side effect for
    a caller that only wants contacts. Falls back to a no-op key if mt is unavailable."""
    try:
        import mt  # noqa: PLC0415
        return mt.slugify(s)
    except Exception:
        import re
        import unicodedata
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
        return re.sub(r"-{2,}", "-", s) or "meeting"


def _registry() -> dict:
    """slug -> entry, from voiceprints/index.json (the authoritative registry).
    Cached on the file's mtime. Never raises."""
    idx = Path(VOICEPRINTS) / "index.json"
    try:
        mtime = idx.stat().st_mtime
    except Exception:
        _vp_cache["mtime"], _vp_cache["by_slug"] = None, {}
        return {}
    if _vp_cache["mtime"] == mtime:
        return _vp_cache["by_slug"]
    by_slug: dict = {}
    try:
        data = json.loads(idx.read_text())
        for sp in (data or {}).get("speakers", []) or []:
            if not isinstance(sp, dict):
                continue
            slug = sp.get("slug") or _slug(sp.get("name") or "")
            if slug:
                by_slug[slug] = sp
    except Exception:
        by_slug = {}
    _vp_cache["mtime"], _vp_cache["by_slug"] = mtime, by_slug
    return by_slug


def _voiceprint_slugs() -> set[str]:
    return set(_registry().keys())


def voiceprint_status(name: str) -> dict:
    """{'enrolled': bool, 'file': str|None}. Never raises."""
    try:
        entry = _registry().get(_slug(name or ""))
    except Exception:
        entry = None
    if not entry:
        return {"enrolled": False, "file": None}
    return {"enrolled": True, "file": entry.get("file") or None}


# ---------------------------------------------------------------------- schema

def ensure_schema() -> list[str]:
    """Add any missing columns. Idempotent. Returns the names it added (empty if none)."""
    conn = _connect(write=True)
    if conn is None:
        return []
    added: list[str] = []
    try:
        have = _cols(conn)
        if not have:
            return []
        for col, typ in NEW_COLUMNS:
            if col in have:
                continue
            conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {typ}")
            added.append(col)
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


# ---------------------------------------------------------------------- reads

def list_contacts(q: str = "", customer: str | None = None,
                  status: str | None = None, limit: int = 500,
                  archived: str = "live") -> list[dict]:
    """`archived`: 'live' (default) hides archived rows, 'archived' shows only them,
    'all' shows both. Callers that must not lose sight of a person — name resolution
    above all — pass 'all'."""
    conn = _connect()
    if conn is None:
        return []
    try:
        have = _cols(conn)
        if not have:
            return []
        sql = (f"SELECT {_select_list(have)}, cu.short_name AS customer "
               "FROM contacts c LEFT JOIN customers cu ON cu.id = c.customer_id "
               f"WHERE {_alive(have, archived)}")
        args: list = []

        if q and q.strip():
            needle = f"%{q.strip().lower()}%"
            fields = ["c.name", "c.email", "c.role"]
            for extra in ("company", "aliases"):
                if extra in have:
                    fields.append(f"c.{extra}")
            sql += " AND (" + " OR ".join(
                f"lower(COALESCE({f},'')) LIKE ?" for f in fields) + ")"
            args += [needle] * len(fields)

        if customer:
            if str(customer).lower() == "none":
                sql += " AND c.customer_id IS NULL"
            else:
                sql += " AND lower(cu.short_name) = ?"
                args.append(str(customer).lower())

        if status:
            sql += " AND c.verification_status = ?"
            args.append(status)

        sql += " ORDER BY c.name COLLATE NOCASE, c.id LIMIT ?"
        args.append(int(limit))

        vp = _voiceprint_slugs()
        return [_row_to_dict(r, vp) for r in conn.execute(sql, args)]
    except Exception:
        return []
    finally:
        conn.close()


def get_contact(cid: int) -> dict | None:
    """Full row by id (tombstoned rows included, so merge/audit can inspect them)."""
    conn = _connect()
    if conn is None:
        return None
    try:
        have = _cols(conn)
        if not have:
            return None
        row = conn.execute(
            f"SELECT {_select_list(have)}, cu.short_name AS customer "
            "FROM contacts c LEFT JOIN customers cu ON cu.id = c.customer_id "
            "WHERE c.id = ?", (int(cid),)).fetchone()
        return _row_to_dict(row) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def duplicates(include_dismissed: bool = False) -> list[dict]:
    """Exact duplicate names only. No fuzzy/surname matching — 'Sam Sample' and
    'Samantha "Sam" Sample' can be two different people at the same customer.

    A group every pair of which has been dismissed as "different people" is gone by
    default — that is the whole point of the ruling being durable. `include_dismissed`
    brings them back so the page can offer "show N dismissed"; nothing else should pass
    it, and the contract's plain `duplicates()` call is unchanged.

    Each group also carries the decision signal that matters most here:
      same_account  — every row sits on the same customer_id. Same account almost
                      always means a real duplicate; different accounts almost always
                      means one person deliberately carried once per account.
      accounts      — the short_names involved, for the one-line hint on the card.
      dismissed / dismissed_pairs — what has already been ruled on.
    """
    conn = _connect()
    if conn is None:
        return []
    try:
        have = _cols(conn)
        if not have:
            return []
        names = [r[0] for r in conn.execute(
            f"SELECT lower(trim(c.name)) AS g FROM contacts c "
            f"WHERE {_alive(have)} AND c.name IS NOT NULL AND trim(c.name) <> '' "
            "GROUP BY g HAVING count(*) > 1 ORDER BY g")]
        if not names:
            return []
        vp = _voiceprint_slugs()
        out = []
        for g in names:
            rows = [_row_to_dict(r, vp) for r in conn.execute(
                f"SELECT {_select_list(have)}, cu.short_name AS customer "
                "FROM contacts c LEFT JOIN customers cu ON cu.id = c.customer_id "
                f"WHERE {_alive(have)} AND lower(trim(c.name)) = ? "
                "ORDER BY COALESCE(c.created_at,''), c.id", (g,))]
            if len(rows) <= 1:
                continue
            pairs = _pairs(rows)
            dpairs = _dismissed_pairs(rows)
            allgone = len(dpairs) == len(pairs) and bool(pairs)
            if allgone and not include_dismissed:
                continue
            accounts = sorted({r["customer"] for r in rows if r.get("customer")})
            cids = {r.get("customer_id") for r in rows}
            out.append({
                "name": rows[0]["name"],
                "rows": rows,
                "dismissed": allgone,
                "dismissed_pairs": dpairs,
                "same_account": len(cids) == 1,
                "accounts": accounts,
            })
        return out
    except Exception:
        return []
    finally:
        conn.close()


def accounts() -> dict:
    """{customer id: short_name} for the edit sheet's Account picker. Never raises."""
    conn = _connect()
    if conn is None:
        return {}
    try:
        return {str(r["id"]): r["short_name"] for r in conn.execute(
            "SELECT id, short_name FROM customers WHERE COALESCE(short_name,'') <> '' "
            "ORDER BY id")}
    except Exception:
        return {}
    finally:
        conn.close()


def stats() -> dict:
    empty = {"total": 0, "verified": 0, "needs_confirmation": 0, "missing_email": 0,
             "missing_role": 0, "duplicate_groups": 0, "with_voiceprint": 0,
             "archived": 0, "triage": 0}
    conn = _connect()
    if conn is None:
        return empty
    try:
        have = _cols(conn)
        if not have:
            return empty
        alive = _alive(have)
        row = conn.execute(
            "SELECT count(*) AS total,"
            " SUM(CASE WHEN c.verification_status='verified' THEN 1 ELSE 0 END) AS verified,"
            " SUM(CASE WHEN c.verification_status='needs_confirmation' THEN 1 ELSE 0 END) AS needs,"
            " SUM(CASE WHEN c.email IS NULL OR trim(c.email)='' THEN 1 ELSE 0 END) AS no_email,"
            " SUM(CASE WHEN c.role IS NULL OR trim(c.role)='' THEN 1 ELSE 0 END) AS no_role"
            f" FROM contacts c WHERE {alive}").fetchone()
        # Counted through duplicates() rather than by SQL, so a dismissed group drops
        # out of the header chip exactly when it drops out of the list. A second small
        # query against 274 rows is cheaper than the two numbers disagreeing.
        n_arch = 0
        if "archived_at" in have:
            n_arch = (conn.execute(
                "SELECT count(*) FROM contacts c WHERE c.archived_at IS NOT NULL"
                + (" AND c.merged_into IS NULL" if "merged_into" in have else "")
            ).fetchone() or [0])[0] or 0
        dupes = len(duplicates())
        vp = _voiceprint_slugs()
        n_vp = sum(1 for r in conn.execute(
            f"SELECT c.name FROM contacts c WHERE {alive}") if _slug(r[0] or "") in vp)
        return {
            "total": row["total"] or 0,
            "verified": row["verified"] or 0,
            "needs_confirmation": row["needs"] or 0,
            "missing_email": row["no_email"] or 0,
            "missing_role": row["no_role"] or 0,
            "duplicate_groups": dupes or 0,
            "with_voiceprint": n_vp,
            "archived": n_arch,
            "triage": len(triage()),
        }
    except Exception:
        return empty
    finally:
        conn.close()


# ---------------------------------------------------------------------- writes

def create_contact(fields: dict) -> dict:
    fields = dict(fields or {})
    name = (fields.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    conn = _connect_w()
    try:
        have = _cols(conn)
        if not have:
            raise RuntimeError("contacts table not found")
        data = {k: v for k, v in fields.items() if k in WRITABLE and k in have}
        data["name"] = name
        st = data.get("verification_status")
        if st is not None and st not in STATUSES:
            raise ValueError(f"bad verification_status: {st!r}")
        now = _now()
        if "created_at" in have:
            data.setdefault("created_at", now)
        if "updated_at" in have:
            data["updated_at"] = now
        cols = list(data)
        cur = conn.execute(
            f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [data[c] for c in cols])
        conn.commit()
        cid = cur.lastrowid
    finally:
        conn.close()
    return get_contact(cid)


def update_contact(cid: int, fields: dict) -> dict:
    cid = int(cid)
    fields = dict(fields or {})
    conn = _connect_w()
    try:
        have = _cols(conn)
        if not conn.execute("SELECT 1 FROM contacts WHERE id=?", (cid,)).fetchone():
            raise ValueError(f"no contact #{cid}")
        data = {k: v for k, v in fields.items() if k in WRITABLE and k in have}
        if "name" in data:
            if not (data["name"] or "").strip():
                raise ValueError("name cannot be blank")
            data["name"] = data["name"].strip()
        st = data.get("verification_status")
        if st is not None and st not in STATUSES:
            raise ValueError(f"bad verification_status: {st!r}")
        if not data:
            raise ValueError("nothing to update")
        if "updated_at" in have:
            data["updated_at"] = _now()
        sets = ", ".join(f"{c}=?" for c in data)
        conn.execute(f"UPDATE contacts SET {sets} WHERE id=?",
                     [data[c] for c in data] + [cid])
        conn.commit()
    finally:
        conn.close()
    return get_contact(cid)


def set_status(cid: int, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    return update_contact(cid, {"verification_status": status})


def _alias_lines(v) -> list[str]:
    return [ln.strip() for ln in (v or "").splitlines() if ln.strip()]


# ------------------------------------------------- "not the same person" bookkeeping

def _nd_set(v) -> set[int]:
    """Parse a not_duplicate_of cell. Tolerant: blanks, spaces and junk are ignored,
    because a column read back from a half-hand-edited row must never throw."""
    out: set[int] = set()
    for part in str(v or "").replace(" ", "").split(","):
        if part.isdigit():
            n = int(part)
            if n > 0:
                out.add(n)
    return out


def _nd_text(ids) -> str | None:
    """Render the set back out. Sorted so the cell is stable and diffable by eye."""
    s = sorted({int(i) for i in ids if int(i) > 0})
    return ",".join(str(i) for i in s) if s else None


def _pairs(rows: list[dict]) -> list[tuple[int, int]]:
    out = []
    for i in range(len(rows)):
        for k in range(i + 1, len(rows)):
            a, b = rows[i]["id"], rows[k]["id"]
            out.append((min(a, b), max(a, b)))
    return out


def _pair_dismissed(ra: dict, rb: dict) -> bool:
    """MUTUALLY dismissed — both rows have to name each other.

    Strict on purpose. dismiss_duplicate() writes both sides in one transaction, so a
    one-sided pair means something went wrong, and the safe failure is for the pair to
    surface again rather than to stay silently hidden.
    """
    return (rb["id"] in _nd_set(ra.get("not_duplicate_of"))
            and ra["id"] in _nd_set(rb.get("not_duplicate_of")))


def _dismissed_pairs(rows: list[dict]) -> list[list[int]]:
    by_id = {r["id"]: r for r in rows}
    return [[a, b] for a, b in _pairs(rows) if _pair_dismissed(by_id[a], by_id[b])]


def merge(keep_id: int, drop_id: int) -> dict:
    """Fill empty keeper fields from the dropped row, union aliases, concatenate notes,
    then tombstone the dropped row. Never deletes."""
    keep_id, drop_id = int(keep_id), int(drop_id)
    if keep_id == drop_id:
        raise ValueError("cannot merge a contact into itself")
    conn = _connect_w()
    try:
        have = _cols(conn)
        if "merged_into" not in have:
            raise RuntimeError("merged_into column missing — run ensure_schema() first")
        keep = conn.execute("SELECT * FROM contacts WHERE id=?", (keep_id,)).fetchone()
        drop = conn.execute("SELECT * FROM contacts WHERE id=?", (drop_id,)).fetchone()
        if keep is None:
            raise ValueError(f"no contact #{keep_id}")
        if drop is None:
            raise ValueError(f"no contact #{drop_id}")
        if keep["merged_into"] is not None:
            raise ValueError(
                f"#{keep_id} was already merged into #{keep['merged_into']}")
        if drop["merged_into"] is not None:
            raise ValueError(
                f"#{drop_id} was already merged into #{drop['merged_into']}")

        upd: dict = {}
        # 1. fill only EMPTY fields on the keeper — never overwrite a non-empty value
        for f in MERGE_FILL:
            if f in have and _empty(keep[f]) and not _empty(drop[f]):
                upd[f] = drop[f]

        # 2. notes — concatenate with a provenance separator
        if not _empty(drop["notes"]):
            sep = f"--- merged from #{drop_id} ---"
            base = (keep["notes"] or "").rstrip()
            upd["notes"] = (f"{base}\n\n{sep}\n{drop['notes']}" if base
                            else f"{sep}\n{drop['notes']}")

        # 3. aliases — union, preserving order. The dropped row's name becomes an alias
        #    when it differs from the keeper's (this is what aliases are for).
        seen, merged_aliases = set(), []
        for v in (_alias_lines(keep["aliases"]) + _alias_lines(drop["aliases"])
                  + ([drop["name"]] if drop["name"] else [])):
            v = (v or "").strip()
            if not v or v.lower() == (keep["name"] or "").strip().lower():
                continue
            if v.lower() in seen:
                continue
            seen.add(v.lower())
            merged_aliases.append(v)
        new_aliases = "\n".join(merged_aliases)
        if new_aliases != (keep["aliases"] or ""):
            upd["aliases"] = new_aliases or None

        now = _now()
        if upd:
            upd["updated_at"] = now
            sets = ", ".join(f"{c}=?" for c in upd)
            conn.execute(f"UPDATE contacts SET {sets} WHERE id=?",
                         [upd[c] for c in upd] + [keep_id])
        conn.execute("UPDATE contacts SET merged_into=?, updated_at=? WHERE id=?",
                     (keep_id, now, drop_id))
        conn.commit()
    finally:
        conn.close()
    return get_contact(keep_id)


def _set_not_duplicate(a_id: int, b_id: int, dismiss: bool) -> dict:
    """The one writer of not_duplicate_of. Symmetric, idempotent, one transaction.

    Both rows are updated together: a ruling that only one side of a pair knows about
    would resurface the moment you looked it up from the other direction, and
    _pair_dismissed() deliberately requires both.
    """
    a_id, b_id = int(a_id), int(b_id)
    if a_id == b_id:
        raise ValueError("a contact cannot be a different person from itself")
    conn = _connect_w()
    try:
        have = _cols(conn)
        if "not_duplicate_of" not in have:
            raise RuntimeError(
                "not_duplicate_of column missing — run ensure_schema() first")
        cur: dict = {}
        for cid in (a_id, b_id):
            r = conn.execute("SELECT id, not_duplicate_of FROM contacts WHERE id=?",
                             (cid,)).fetchone()
            if r is None:
                raise ValueError(f"no contact #{cid}")
            cur[cid] = _nd_set(r["not_duplicate_of"])
        changed = False
        now = _now()
        for cid, other in ((a_id, b_id), (b_id, a_id)):
            want = (cur[cid] | {other}) if dismiss else (cur[cid] - {other})
            if want == cur[cid]:
                continue                      # already recorded — a no-op, not a second entry
            changed = True
            if "updated_at" in have:
                conn.execute(
                    "UPDATE contacts SET not_duplicate_of=?, updated_at=? WHERE id=?",
                    (_nd_text(want), now, cid))
            else:
                conn.execute("UPDATE contacts SET not_duplicate_of=? WHERE id=?",
                             (_nd_text(want), cid))
        if changed:
            conn.commit()
    finally:
        conn.close()
    return {"a": get_contact(a_id), "b": get_contact(b_id),
            "dismissed": bool(dismiss), "changed": changed}


def dismiss_duplicate(a_id: int, b_id: int) -> dict:
    """Record that #a and #b are genuinely different people who share a name.

    Nothing is deleted and nothing is merged — this only stops the pair being offered
    as a duplicate, to the user and to the assistant alike. Returns both rows plus `changed`,
    which is False when the ruling was already on file.
    """
    return _set_not_duplicate(a_id, b_id, True)


def undismiss_duplicate(a_id: int, b_id: int) -> dict:
    """Undo dismiss_duplicate(): the pair goes back on the duplicate list."""
    return _set_not_duplicate(a_id, b_id, False)


# ---------------------------------------------------------------------- seen_in

def _fts_phrase(s: str) -> str:
    """Quote a name as an fts5 phrase; internal double-quotes are doubled."""
    return '"' + (s or "").replace('"', '""') + '"'


def seen_in(cid: int, limit: int = 20) -> list[dict]:
    """Meetings this person appears in, newest first, via the search index.
    Returns [] rather than raising, whatever goes wrong."""
    try:
        c = get_contact(int(cid))
        if not c:
            return []
        terms = []
        for t in [c.get("name")] + _alias_lines(c.get("aliases")):
            t = (t or "").strip()
            if t and t.lower() not in {x.lower() for x in terms}:
                terms.append(t)
        if not terms:
            return []

        try:
            import search  # noqa: PLC0415  (lazy: contacts must import without it)
        except Exception:
            return []

        best: dict = {}
        for term in terms:
            try:
                hits = search.search(_fts_phrase(term), limit=max(int(limit) * 5, 50))
            except Exception:
                hits = []
            for h in hits or []:
                try:
                    sess = h.get("session")
                    if not sess:
                        continue          # recaps/memory/notes aren't meetings
                    score = h.get("score") or 0.0
                    prev = best.get(sess)
                    if prev is None or score > prev["_score"]:
                        best[sess] = {
                            "session": sess,
                            "date": h.get("date"),
                            "customer": h.get("customer"),
                            "title": h.get("title"),
                            "kind": h.get("kind"),
                            "snippet": h.get("snippet"),
                            "path": h.get("path"),
                            "_score": score,
                        }
                except Exception:
                    continue

        out = sorted(best.values(),
                     key=lambda r: ((r["date"] or ""), r["_score"]), reverse=True)
        for r in out:
            r.pop("_score", None)
        return out[:int(limit)]
    except Exception:
        return []


# ------------------------------------------------- name resolution (the mt bridge)
#
# Enrolling a voice in `mt` and having a row in this table were, until now, two facts
# with no code path between them: enrolling "Pat Placeholder" told the contact list nothing.
# What follows closes that gap in the ONLY direction that is safe — it reports what it
# found and never acts on it. Nothing here writes; nothing here decides. A name is a
# person, and only the user says which person.
#
# Three different things go wrong with a name typed mid-call, and they need three
# different fixes, which is exactly why this cannot be automated:
#
#   a typo               "Alex Exmaple" for Alex Example -> RENAME the voiceprint.
#                        Aliasing a typo entrenches it.
#   the contact is wrong  voiceprint "Sam Sample" was right, the row said "Sam Sampel"
#                        -> fix the row, alias the old spelling.
#   nickname / legal name "Pat Placeholder" vs HR's "Patricia" -> ALIAS. Both are them.
#
# Only the user can tell those apart, so resolve_name() hands them the evidence and stops.

# Calibrated against the real workspace on 2026-09-16: every enrolled voiceprint name
# (55) scored against every live contact (274), and every contact scored against every
# other contact (37,401 distinct-name pairs).
#
#   0.917  a typo of a surname                          must be offered
#   0.954  a one-letter first-name variant              ditto
#   0.975  full first name vs its short form            nickname, must be offered
#   1.000  short name inside 'Full "Short" Surname'     nickname, must be offered
#   ---- 0.90, the threshold ----
#   0.864  same surname, near-identical first names     two rows, possibly two people
#   0.833  same surname, different first names          two different people
#   0.833  same surname, different first names          two different people
#   0.767  same first name, different surnames          two different people
#
# The band that separates them is real and it is empty: nothing scores between 0.864 and
# 0.917 anywhere in the workspace. 0.90 sits in the middle of that gap, and at 0.90 no
# pair of DIFFERENT people anywhere in those 37,401 pairs produces a suggestion. Suggesting
# the wrong person's name is worse than suggesting nobody, so the threshold is set to the
# highest value that still catches every known-real case, not the lowest that admits them.
NAME_MATCH_THRESHOLD = 0.90

# A first name can never carry a match on its own: "Sam Sample" must not propose
# "Sam Placeholder". Below this the surnames are simply different people.
SURNAME_FLOOR = 0.80


def normalize_name(s) -> str:
    """Fold a name to the form comparisons happen in. Lossy on purpose, never stored.

    accents -> ascii, "Sample, Sam" -> "sam sample" (the HR export order),
    "(UNRESOLVED - ...)" parentheticals dropped, punctuation to spaces, case flattened.
    `Patricia 'Pat' Placeholder` becomes `patricia pat placeholder`, which is what lets the
    nickname inside a stored name be seen as a token rather than as noise.
    """
    import unicodedata  # noqa: PLC0415  — only this corner of the module needs it
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"\([^)]*\)", " ", s)
    if s.count(",") == 1:                      # "Sample, Sam" -> "Sam Sample"
        a, b = s.split(",")
        if a.strip() and b.strip():
            s = b + " " + a
    return " ".join(re.sub(r"[^A-Za-z0-9]+", " ", s).lower().split())


def _ratio(a: str, b: str) -> float:
    import difflib  # noqa: PLC0415
    return difflib.SequenceMatcher(None, a, b).ratio()


def compare_names(q: str, c: str) -> tuple[float, str]:
    """Score two ALREADY-normalised names. Returns (score, why) — 0.0 means "not close".

    Both sides must have at least two tokens. A one-word name ("Sample (UNRESOLVED -
    surname only)") is a stub about someone who may not be this person at all, and
    proposing it off a surname alone is the kind of guess this whole feature exists to
    avoid. Such rows can still match exactly; they are never suggested.
    """
    qt, ct = q.split(), c.split()
    if len(qt) < 2 or len(ct) < 2:
        return 0.0, ""
    surname = 1.0 if qt[-1] == ct[-1] else _ratio(qt[-1], ct[-1])
    if surname < SURNAME_FLOOR:
        return 0.0, ""
    qf, cf, qs, cs = qt[0], ct[0], set(qt), set(ct)
    if qf == cf:
        first, why = 1.0, "same first name, surname spelled differently"
    elif (qs <= cs or cs <= qs) and len(qs & cs) >= 2:
        # `Pat Placeholder` is literally inside `Patricia 'Pat' Placeholder`. Two shared tokens
        # minimum, so a bare surname can never trigger this.
        first, why = 1.0, "one of these names contains the other"
    elif len(qf) >= 3 and len(cf) >= 3 and (qf.startswith(cf) or cf.startswith(qf)):
        first, why = 0.95, "shortened first name (Dan/Daniel, Jon/Jonathan)"
    else:
        first = _ratio(qf, cf)
        why = "first names are close"
    return 0.5 * surname + 0.5 * first, why


def name_keys(row: dict) -> list[str]:
    """Every normalised spelling a contact answers to: the name plus each alias."""
    out, seen = [], set()
    for v in [(row or {}).get("name")] + _alias_lines((row or {}).get("aliases")):
        n = normalize_name(v)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def resolve_name(name: str, rows: list[dict] | None = None) -> dict:
    """Where does this typed name land in the contact list? READ-ONLY, always.

    {'query', 'normalized', 'exact': [contact,...], 'close': [{'contact','score','why',
     'matched'},...], 'checked': int}

    `exact` is a list because two rows legitimately share a name (one internal person
    carried once per account). `close` is a list for the same reason a "did you mean"
    with two answers must show both rather than pick one.
    """
    q = normalize_name(name)
    out = {"query": str(name or ""), "normalized": q,
           "exact": [], "close": [], "checked": 0}
    if not q:
        return out
    try:
        # 'all' on purpose. An archived contact must still resolve, or the next meeting
        # they speak in re-creates them as an unverified stranger — which is exactly the
        # failure archiving exists to avoid.
        rows = list_contacts(limit=5000, archived="all") if rows is None else list(rows)
    except Exception:
        rows = []
    out["checked"] = len(rows)
    for row in rows:
        keys = name_keys(row)
        if q in keys:
            out["exact"].append(row)
            continue
        best, why, matched = 0.0, "", ""
        for k in keys:
            s, w = compare_names(q, k)
            if s > best:
                best, why, matched = s, w, k
        if best >= NAME_MATCH_THRESHOLD:
            out["close"].append({"contact": row, "score": round(best, 3),
                                 "why": why, "matched": matched})
    # A suggestion the user has to choose between is honest; a suggestion picked for them
    # is not. Ranked, all of them kept.
    out["close"].sort(key=lambda h: (-h["score"], (h["contact"].get("name") or "")))
    return out


def add_alias(cid: int, alias: str) -> dict:
    """Append one spelling to a contact's aliases. Idempotent, never replaces.

    aliases is a newline-separated list and it is somebody's accumulated history of how
    their name gets garbled — writing the field wholesale would throw the rest away, so
    this reads, appends and writes back.
    """
    cid = int(cid)
    alias = " ".join(str(alias or "").split())
    if not alias:
        raise ValueError("an alias cannot be blank")
    row = get_contact(cid)
    if not row:
        raise ValueError(f"no contact #{cid}")
    have = _alias_lines(row.get("aliases"))
    known = {normalize_name(x) for x in have} | {normalize_name(row.get("name"))}
    if normalize_name(alias) in known:
        return row                                   # already answers to it — no write
    return update_contact(cid, {"aliases": "\n".join(have + [alias])})


def unlinked_voiceprints() -> list[dict]:
    """Enrolled voiceprints that no contact row answers to. Never raises.

    This is the backlog the enrolment flow was silently creating: real people whose
    voiceprint names differ from the org chart's. Each carries its own resolve_name()
    result so the worklist can offer the same three choices the review page does.
    """
    try:
        rows = list_contacts(limit=5000, archived="all")
    except Exception:
        rows = []
    out = []
    try:
        entries = list(_registry().values())
    except Exception:
        entries = []
    for sp in entries:
        nm = str(sp.get("name") or sp.get("slug") or "").strip()
        if not nm:
            continue
        r = resolve_name(nm, rows=rows)
        if r["exact"]:
            continue
        out.append({"name": nm, "slug": sp.get("slug") or _slug(nm),
                    "samples": sp.get("samples"), "updated": sp.get("updated"),
                    "close": r["close"]})
    out.sort(key=lambda d: (not d["close"], d["name"].lower()))
    return out


# ---------------------------------------------------------------------- CLI

def set_archived(cid: int, archived: bool, reason: str = "") -> dict:
    """"Forget this one for now" — hide a contact without losing them.

    NOT a delete, and there is no delete anywhere in this module on purpose. Deleting a
    contact is the one action that guarantees they come back: `resolve_name()` reads the
    contact list on every voice enrolment, so a deleted person stops resolving and the
    next meeting they speak in re-creates them as a fresh `needs_confirmation` row —
    without the notes, aliases, role or provenance you had built up. Archiving keeps the
    row and the knowledge; it only takes them out of the list, the pickers and the
    duplicate scan.

    Distinct from `merged_into`, which means "this row is not a person, it is a duplicate
    of that one". Archiving means "this is a real, separate person I have stopped dealing
    with".
    """
    cid = int(cid)
    conn = _connect_w()
    try:
        have = _cols(conn)
        if "archived_at" not in have:
            raise RuntimeError("archive columns are not migrated in yet")
        row = conn.execute("SELECT merged_into FROM contacts WHERE id=?", (cid,)).fetchone()
        if not row:
            raise ValueError(f"no contact #{cid}")
        if "merged_into" in have and row["merged_into"] is not None:
            raise ValueError(f"#{cid} was merged into #{row['merged_into']} — "
                             "it is already out of the list")
        if archived:
            conn.execute("UPDATE contacts SET archived_at=?, archived_reason=?, "
                         "updated_at=? WHERE id=?",
                         (_now(), (reason or "").strip()[:400] or None, _now(), cid))
        else:
            conn.execute("UPDATE contacts SET archived_at=NULL, archived_reason=NULL, "
                         "updated_at=? WHERE id=?", (_now(), cid))
        conn.commit()
    finally:
        conn.close()
    return get_contact(cid)


# Rows that look like a PLACEHOLDER rather than a person. This is a WORKLIST, not a
# verdict — nothing archives itself, and the Person Verification Protocol means every one
# still needs the user's eyes.
#
# Calibrated against the real data on Sep 22 2026, after a first attempt got it badly
# wrong. Two signals had to be thrown out:
#   * `TBD` in the ROLE field — 34 rows. That means the ROLE is unfilled, not that the
#     person is fictional. One contact had role 'TBD — <customer>' and a real customer
#     address; flagging them would have walked the user into archiving a live contact.
#   * an EMBEDDED quote — 5 rows, all legitimate nicknames like `Samantha "Sam" Sample`.
#     The contract already warns that such a name can be a real and distinct person.
# What actually marks a placeholder is the NAME itself: it opens with a quote, or it says
# outright that nobody knows who this is.
_TRIAGE_MARKERS = ("unresolved", "unidentified", "unnamed", "unknown person")


def triage(limit: int = 200) -> list[dict]:
    """Live contacts that look like placeholders, strongest signal first. Never raises."""
    try:
        rows = list_contacts(limit=5000)
    except Exception:
        return []
    out = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        why, strong = [], False

        if name[:1] in ('"', "\u201c", "'"):
            why.append("the whole name is quoted")
            strong = True
        for m in _TRIAGE_MARKERS:
            if m in low:
                why.append(f"the name says {m}")
                strong = True
                break

        bare = " " not in name
        if bare:
            why.append("first name only")
        no_reach = not (r.get("email") or "").strip() and not (r.get("slack_user_id") or "").strip()
        if no_reach:
            why.append("no email or Slack id")

        # Strong on its own. Otherwise it takes a first-name-only row with no way to reach
        # them — a real person who simply has no email on file is never dragged in alone.
        if not (strong or (bare and no_reach)):
            continue
        d = dict(r)
        d["why"] = why
        d["strength"] = (2 if strong else 1) + (1 if r.get("has_voiceprint") else 0)
        out.append(d)
    out.sort(key=lambda d: (-d["strength"], (d.get("name") or "").lower()))
    return out[:limit]


def rename_impact(cid: int, new_name: str) -> dict:
    """What a rename would actually do. READ-ONLY.

    Two things the form cannot see on its own:
      * the voiceprint registry is keyed by the SLUG OF THE NAME, so renaming a contact
        silently unlinks an enrolled voice unless `mt rename` runs too;
      * if the new name already belongs to another contact, this is not a rename at all,
        it is a merge — and merge() already unions aliases and fills empty fields.

    {'changed', 'old', 'new', 'voiceprint': {...}, 'collisions': [contact,...]}
    """
    out = {"changed": False, "old": "", "new": (new_name or "").strip(),
           "voiceprint": {"enrolled": False, "file": None}, "collisions": []}
    try:
        cur = get_contact(int(cid))
    except Exception:
        cur = None
    if not cur:
        return out
    out["old"] = (cur.get("name") or "").strip()
    if not out["new"] or out["new"] == out["old"]:
        return out
    out["changed"] = True
    try:
        out["voiceprint"] = voiceprint_status(out["old"])
    except Exception:
        pass
    try:
        key = normalize_name(out["new"])
        for r in list_contacts(limit=5000, archived="all"):
            if int(r.get("id") or 0) == int(cid):
                continue
            if key and key in name_keys(r):
                out["collisions"].append(r)
    except Exception:
        pass
    return out


def _main(argv: list[str]) -> int:
    global DB_PATH
    args = list(argv)
    if args and args[0] == "--migrate":
        if len(args) < 2:
            print("usage: contacts.py --migrate <dbpath>", file=sys.stderr)
            return 2
        target = Path(args[1]).resolve()
        real = P.DB.resolve()
        if target == real:
            print(f"refusing to migrate the live DB {real} — "
                  "back it up and migrate the copy", file=sys.stderr)
            return 3
        DB_PATH = target
        added = ensure_schema()
        print(f"{target}: added {added or '(nothing — already migrated)'}")
        return 0

    if not args or args[0] == "--stats":
        s = stats()
        for k in ("total", "verified", "needs_confirmation", "missing_email",
                  "missing_role", "duplicate_groups", "with_voiceprint"):
            print(f"{k:>18}: {s[k]}")
        return 0

    if args[0] == "--dupes":
        groups = duplicates(include_dismissed=True)
        live = [g for g in groups if not g["dismissed"]]
        print(f"{len(live)} open exact-duplicate name group(s), "
              f"{len(groups) - len(live)} dismissed\n")
        for g in groups:
            ids = ", ".join(f"#{r['id']}" for r in g["rows"])
            where = "same account" if g["same_account"] else (
                "ACROSS " + "|".join(g["accounts"] or ["-"]))
            flag = "  [dismissed]" if g["dismissed"] else ""
            print(f"  {g['name']}  ({len(g['rows'])} rows: {ids}) — {where}{flag}")
        return 0

    q = " ".join(args)
    rows = list_contacts(q=q)
    print(f"{len(rows)} match(es) for {q!r}\n")
    for r in rows:
        vp = " [voiceprint]" if r["has_voiceprint"] else ""
        who = r["customer"] or r.get("company") or "-"
        print(f"  #{r['id']:<5} {r['name']:<30} {who:<14} "
              f"{r['verification_status']:<19} {r['email'] or ''}{vp}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

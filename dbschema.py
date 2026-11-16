"""Create mt's database, check that an existing one meets the contract in schema.sql, and
bring it up to date with the tracking features added since (upgrade()).

mt never restructures a database it did not create. `create()` only ever builds a NEW
file; `check()` is read-only and reports what is missing, so bringing your own data in is
a deliberate step (docs/DATABASE.md), not something that happens to you on startup.
`upgrade()` follows the same rule: it changes a database on its own only when mt created
it (PRAGMA user_version ≥ 1, set by create()); your own database is upgraded only when you
ask (`mt migrate --own-db`), and every upgrade takes a backup first.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"
SCHEMA_VERSION = 1


def _columns(con: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    return {t: {r[1] for r in con.execute(f'pragma table_info("{t}")')} for t in tables}


def expected() -> dict[str, set[str]]:
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(SCHEMA.read_text())
        return _columns(con)
    finally:
        con.close()


def create(db: Path) -> bool:
    """Build a new database at `db`. Returns False (and touches nothing) if it exists."""
    if db.exists():
        return False
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA.read_text())
        con.execute(f"pragma user_version = {SCHEMA_VERSION}")
        con.commit()
    finally:
        con.close()
    return True


def check(db: Path) -> dict:
    """{'ok', 'missing_tables', 'missing_columns': {table: [col]}, 'user_version'}. Read-only."""
    if not db.exists():
        return {"ok": False, "exists": False, "missing_tables": [], "missing_columns": {}}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    try:
        have = _columns(con)
        uv = con.execute("pragma user_version").fetchone()[0]
    finally:
        con.close()
    want = expected()
    missing_tables = sorted(t for t in want if t not in have)
    missing_columns = {t: sorted(want[t] - have[t]) for t in want
                       if t in have and want[t] - have[t]}
    return {"ok": not missing_tables and not missing_columns, "exists": True,
            "missing_tables": missing_tables, "missing_columns": missing_columns,
            "user_version": uv}


# ------------------------------------------------------------------ feature upgrades
#
# Tracking features added after schema v1. Each step is additive (new columns, tables,
# indexes, triggers — never a rename or a drop) and idempotent, and each already exists
# as a `tracking.py --migrate-…` command; this is the ordered list, so an update can apply
# whatever is missing in one go. Order matters: R4.2's triggers read columns that
# "columns" adds, and R4.9 logs to R4.2's item_events.
#   (key, what it adds, is-it-there check, apply)
def _steps():
    import tracking as TR
    cols = [c for c, _ in TR.ITEM_NEW_COLUMNS]
    pcols = [c for c, _ in TR.PROJECT_NEW_COLUMNS]

    def columns_ready(con):
        return (set(cols) <= set(TR._cols(con, "action_items"))
                and set(pcols) <= set(TR._cols(con, "projects")))

    def columns_apply(db):
        old = TR.DB_PATH
        TR.DB_PATH = Path(db)
        try:
            return TR.ensure_schema()
        finally:
            TR.DB_PATH = old

    return [
        ("columns", "owner/updated-by columns", columns_ready, columns_apply),
        ("r42", "change log, plan dates, sequence links (R4.2)", TR.r42_ready,
         TR.migrate_r42),
        ("r44c", "item titles (R4.4c)", TR.r44c_ready, lambda db: TR.migrate_r44c(db, None)),
        ("r45b", "Gantt edit leases (R4.5b)", TR.r45b_ready, TR.migrate_r45b),
        ("p7", "owners linked to contacts (Phase 7)", TR.p7_ready, TR.migrate_p7),
        ("r49", "links: URL, primary, roles, follow-up date, stale tickets (R4.9)",
         lambda con: TR.links_ready(con)
         and "follow_up_date" in TR._cols(con, "action_items"),
         TR.migrate_links),
    ]


def pending(db: Path) -> dict:
    """Read-only. {'exists', 'mine' (mt created it), 'user_version', 'pending': [(key, what)]}"""
    db = Path(db)
    if not db.exists():
        return {"exists": False, "mine": False, "user_version": None, "pending": []}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    try:
        uv = con.execute("pragma user_version").fetchone()[0]
        todo = [(k, what) for k, what, ready, _ in _steps() if not ready(con)]
    finally:
        con.close()
    return {"exists": True, "mine": uv >= 1, "user_version": uv, "pending": todo}


def backup(db: Path, tag: str = "pre-upgrade") -> Path:
    """A checked copy beside the DB, in backups/. Raises if the copy does not check out."""
    db = Path(db)
    bk = db.parent / "backups" / f"{db.stem}-{dt.date.today().isoformat()}-{tag}.db"
    bk.parent.mkdir(exist_ok=True)
    if bk.exists():
        bk = bk.with_name(bk.stem + dt.datetime.now().strftime("-%H%M%S") + ".db")
    src, dst = sqlite3.connect(str(db), timeout=10), sqlite3.connect(str(bk))
    try:
        src.backup(dst)
        ok = dst.execute("pragma quick_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        raise RuntimeError(f"the backup did not check out ({ok}) — nothing was changed")
    return bk


def upgrade(db: Path, own: bool = False) -> dict:
    """Apply every pending feature step, after one backup. A database mt did not create is
    left alone unless `own` (the user asked: `mt migrate --own-db`).
    {'status': 'current'|'own'|'done'|'missing', 'backup', 'applied': [(key, [what])]}"""
    p = pending(db)
    if not p["exists"]:
        return {"status": "missing", "backup": None, "applied": [], "pending": []}
    if not p["pending"]:
        return {"status": "current", "backup": None, "applied": [], "pending": []}
    if not p["mine"] and not own:
        return {"status": "own", "backup": None, "applied": [], "pending": p["pending"]}
    bk = backup(db)
    todo = {k for k, _ in p["pending"]}
    applied = []
    for key, _, _, apply in _steps():
        if key in todo:
            applied.append((key, apply(db) or []))
    return {"status": "done", "backup": str(bk), "applied": applied, "pending": []}

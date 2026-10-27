"""Create mt's database, or check that an existing one meets the contract in schema.sql.

mt never restructures a database it did not create. `create()` only ever builds a NEW
file; `check()` is read-only and reports what is missing, so bringing your own data in is
a deliberate step (docs/DATABASE.md), not something that happens to you on startup.
"""
from __future__ import annotations

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

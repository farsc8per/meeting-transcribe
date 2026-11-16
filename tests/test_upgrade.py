"""Feature upgrades for databases on other installs (dbschema.pending / upgrade, mt migrate).

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Builds databases from schema.sql in a temp folder; never opens a real one.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbschema as DS      # noqa: E402
import tracking as TR      # noqa: E402


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-upgrade-"))
        self.db = self.tmp / "DB" / "mt.db"
        self.assertTrue(DS.create(self.db))                 # what `mt init` does
        c = sqlite3.connect(str(self.db))
        c.executescript("""
            INSERT INTO customers (id, name, short_name) VALUES (1, 'Acme Stores', 'acme');
            INSERT INTO action_items (id, customer_id, description, due_date, status)
                VALUES (1, 1, 'Live work', '2026-12-01', 'open'),
                       (2, 1, 'Done work', '2026-10-01', 'done');
            INSERT INTO external_links (local_table, local_id, system, role,
                external_container_id, external_item_id)
                VALUES ('action_items', 1, 'jira', 'eng_ticket', 'ABC', 'ABC-100');
        """)
        c.commit()
        c.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql):
        c = sqlite3.connect(str(self.db))
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def test_fresh_install_upgrades_everything_once(self):
        p = DS.pending(self.db)
        self.assertTrue(p["mine"])
        self.assertIn("r49", [k for k, _ in p["pending"]])
        r = DS.upgrade(self.db)
        self.assertEqual(r["status"], "done")
        self.assertTrue(Path(r["backup"]).exists())
        self.assertEqual(DS.pending(self.db)["pending"], [])
        self.assertEqual(DS.upgrade(self.db)["status"], "current")
        # existing data carried over and normalized
        self.assertEqual(self.q("SELECT role, role_legacy, is_primary FROM external_links"),
                         [("tracks", "eng_ticket", 1)])
        self.assertEqual(self.q("SELECT id, follow_up_date FROM action_items ORDER BY id"),
                         [(1, "2026-12-01"), (2, None)])
        self.assertEqual(self.q("PRAGMA quick_check"), [("ok",)])
        # the backup is the pre-upgrade database
        b = sqlite3.connect(r["backup"])
        try:
            self.assertEqual(b.execute("SELECT role FROM external_links").fetchall(),
                             [("eng_ticket",)])
        finally:
            b.close()

    def test_own_database_is_left_alone_unless_asked(self):
        c = sqlite3.connect(str(self.db))
        c.execute("PRAGMA user_version = 0")              # a database mt did not create
        c.commit()
        c.close()
        r = DS.upgrade(self.db)
        self.assertEqual(r["status"], "own")
        self.assertFalse((self.db.parent / "backups").exists())
        self.assertNotIn("is_primary", TR._cols(sqlite3.connect(str(self.db)), "external_links"))
        r = DS.upgrade(self.db, own=True)
        self.assertEqual(r["status"], "done")
        self.assertEqual(DS.pending(self.db)["pending"], [])

    def test_missing_database(self):
        self.assertEqual(DS.upgrade(self.tmp / "nope.db")["status"], "missing")


if __name__ == "__main__":
    unittest.main()

"""R4.9 — tracker links: roles, URLs, paste parsing, primary, migration, stale view.

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Builds a throwaway DB in a temp folder with the pre-R4.9 shape; never opens a real one.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths as P          # noqa: E402
import tracking as TR      # noqa: E402

SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, short_name TEXT);
CREATE TABLE projects (id INTEGER PRIMARY KEY, customer_id INTEGER, name TEXT, type TEXT,
    rail_url TEXT, status TEXT, notes TEXT, created_at TEXT);
CREATE TABLE action_items (id INTEGER PRIMARY KEY, customer_id INTEGER, project_id INTEGER,
    description TEXT NOT NULL, owner TEXT, due_date TEXT, status TEXT DEFAULT 'open',
    priority TEXT DEFAULT 'normal', source TEXT, created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')), report_scope TEXT DEFAULT 'unset',
    parent_id INTEGER, title TEXT);
CREATE TABLE external_links (id INTEGER PRIMARY KEY, local_table TEXT NOT NULL,
    local_id INTEGER NOT NULL, system TEXT NOT NULL, role TEXT NOT NULL,
    external_container_id TEXT, external_item_id TEXT NOT NULL, notes TEXT,
    created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')));
CREATE UNIQUE INDEX ux_external_links_dedup ON external_links(local_table, local_id, system,
    role, external_item_id);
INSERT INTO customers VALUES (1, 'Acme Stores', 'acme');
INSERT INTO action_items (id, customer_id, description, due_date, status) VALUES
    (1, 1, 'Fix the thing', '2026-11-20', 'open'),
    (2, 1, 'Old ticket work', NULL, 'open'),
    (3, 1, 'Closed work', '2026-10-01', 'done'),
    (4, NULL, 'No links yet', NULL, 'open');
INSERT INTO external_links (local_table, local_id, system, role, external_container_id,
    external_item_id, notes) VALUES
    ('action_items', 1, 'file', 'source_meeting', 'workspace', 'Customers/acme/m/summary.md', NULL),
    ('action_items', 1, 'jira', 'eng_ticket', 'ENG', 'ABC-100', 'the work'),
    ('action_items', 1, 'jira', 'ticket', NULL, 'ABC-100', 'dup under another role'),
    ('action_items', 1, 'jira', 'epic', 'ENG', 'ABC-99', 'the epic'),
    ('action_items', 1, 'monday', 'customer_share', '111', '222', NULL),
    ('action_items', 2, 'jira', 'dev_ticket', 'DEV', 'DEV-5', NULL),
    ('action_items', 2, 'slack', 'conversation', 'C0ABCDEF1', '1786455844.757869',
        'thread https://acme.slack.com/archives/C0ABCDEF1/p1786455844757869 here'),
    ('action_items', 2, 'google_sheets', 'tracker', 'SHEETID_abcdefghijklmnop', 'gid=0', NULL),
    ('action_items', 3, 'jira', 'rel_ticket', 'REL', 'XYZ-7', NULL),
    ('action_items', 2, 'jira', 'weird_new_role', NULL, 'ABC-55', NULL);
"""


class LinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-links-"))
        self.db = self.tmp / "links-fixture.db"
        c = sqlite3.connect(str(self.db))
        c.executescript(SCHEMA)
        c.commit()
        c.close()
        self._old = (TR.DB_PATH, P.JIRA_SITE, P.MONDAY_SITE, P.SLACK_SITE, P.GOOGLE_ACCOUNT)
        TR.DB_PATH = self.db
        TR.ensure_schema()                 # the real DB's order: columns, then R4.2
        TR.migrate_r42(self.db)
        P.JIRA_SITE, P.MONDAY_SITE = "https://acme.atlassian.net", "https://acme.monday.com"
        P.SLACK_SITE, P.GOOGLE_ACCOUNT = "https://acme.slack.com", ""
        self.report = TR.migrate_links(self.db)

    def tearDown(self):
        TR.DB_PATH, P.JIRA_SITE, P.MONDAY_SITE, P.SLACK_SITE, P.GOOGLE_ACCOUNT = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, *a):
        c = sqlite3.connect(str(self.db))
        try:
            return c.execute(sql, a).fetchall()
        finally:
            c.close()

    # -- migration
    def test_roles_are_the_seven(self):
        roles = {r for (r,) in self.q("SELECT DISTINCT role FROM external_links")}
        self.assertTrue(roles <= set(TR.LINK_ROLES), roles)
        self.assertIn("unknown role(s) mapped to reference: weird_new_role",
                      "; ".join(self.report))
        legacy = dict(self.q("SELECT external_item_id, role_legacy FROM external_links "
                             "WHERE local_id=1 AND system='jira'"))
        self.assertEqual(legacy["ABC-99"], "epic")
        self.assertIn("eng_ticket", legacy["ABC-100"])

    def test_duplicate_target_folds(self):
        rows = self.q("SELECT notes FROM external_links WHERE external_item_id='ABC-100'")
        self.assertEqual(len(rows), 1)
        self.assertIn("dup under another role", rows[0][0])

    def test_one_primary_per_row_and_epic_wins(self):
        bad = self.q("SELECT local_id FROM external_links GROUP BY local_table, local_id "
                     "HAVING sum(is_primary) <> 1")
        self.assertEqual(bad, [])
        self.assertEqual(self.q("SELECT external_item_id FROM external_links WHERE "
                                "local_id=1 AND is_primary=1"), [("ABC-99",)])

    def test_sheets_and_notes_urls(self):
        (u,) = self.q("SELECT url FROM external_links WHERE system='gdrive'")[0]
        self.assertTrue(u.startswith("https://docs.google.com/spreadsheets/d/SHEETID_"))
        (s,) = self.q("SELECT url FROM external_links WHERE system='slack'")[0]
        self.assertEqual(s, "https://acme.slack.com/archives/C0ABCDEF1/p1786455844757869")

    def test_follow_up_copied_live_only(self):
        fu = dict(self.q("SELECT id, follow_up_date FROM action_items"))
        self.assertEqual(fu[1], "2026-11-20")
        self.assertIsNone(fu[3])                       # closed: left alone
        self.assertEqual(self.q("SELECT due_date FROM action_items WHERE id=1"),
                         [("2026-11-20",)])            # due_date untouched

    def test_idempotent(self):
        self.assertEqual(TR.migrate_links(self.db), [])

    # -- urls + parsing
    def test_link_url(self):
        self.assertEqual(TR.link_url("jira", None, "ABC-1"),
                         "https://acme.atlassian.net/browse/ABC-1")
        self.assertEqual(TR.link_url("monday", "111", "222"),
                         "https://acme.monday.com/boards/111/pulses/222")
        self.assertEqual(TR.link_url("monday", "111", "REQ-9"),
                         "https://acme.monday.com/boards/111")
        self.assertEqual(TR.link_url("slack", "", "C0ABCDEF1/p1786455844757869"),
                         "https://acme.slack.com/archives/C0ABCDEF1/p1786455844757869")
        self.assertIn("#all/0a1b2c3d4e5f6a7b", TR.link_url("gmail", None, "0a1b2c3d4e5f6a7b"))
        self.assertIsNone(TR.link_url("file", "workspace", "x.md"))
        P.MONDAY_SITE = ""
        self.assertIsNone(TR.link_url("monday", "111", "222"))   # no site: no URL, no error

    def test_parse_link(self):
        cases = {
            "abc-123": ("jira", "ABC-123"),
            "https://acme.atlassian.net/browse/XYZ-55": ("jira", "XYZ-55"),
            "https://acme.atlassian.net/jira/software/c/projects/ABC/boards/1?selectedIssue=ABC-9":
                ("jira", "ABC-9"),
            "https://acme.atlassian.net/wiki/spaces/DOCS/pages/1234567890/Guide":
                ("confluence", "1234567890"),
            "https://acme.monday.com/boards/111/pulses/222": ("monday", "222"),
            "https://acme.slack.com/archives/C0ABCDEF1/p1786455844757869":
                ("slack", "1786455844.757869"),
            "https://mail.google.com/mail/u/0/#inbox/FMfcgzQAbCdEfGhIjKlMnOpQ":
                ("gmail", "FMfcgzQAbCdEfGhIjKlMnOpQ"),
            "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/edit":
                ("gdrive", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
            "https://example.com/a?b=1": ("web", "https://example.com/a?b=1"),
        }
        for text, (sysn, item) in cases.items():
            p = TR.parse_link(text)
            self.assertEqual((p["system"], p["item"]), (sysn, item), text)
        with self.assertRaises(ValueError):
            TR.parse_link("just some words")

    # -- writes
    def test_add_primary_remove(self):
        r = TR.add_link("action_items", 4, text="https://acme.atlassian.net/browse/ABC-7",
                        role="tracks")
        self.assertFalse(r["existed"])
        links = TR.links_for("action_items", 4)
        self.assertEqual(links[0]["is_primary"], 1)              # first link → primary
        self.assertEqual(links[0]["href"], "https://acme.atlassian.net/browse/ABC-7")
        r2 = TR.add_link("action_items", 4, text="ABC-7", role="related")
        self.assertTrue(r2["existed"])                           # same target: no dup
        self.assertEqual(TR.links_for("action_items", 4)[0]["role"], "related")
        w = TR.add_link("action_items", 4, text="https://example.com/x", role="reference")
        TR.set_primary_link(w["id"])
        self.assertEqual(TR.links_for("action_items", 4)[0]["system"], "web")
        TR.remove_link(w["id"])                                  # primary removed → repick
        ls = TR.links_for("action_items", 4)
        self.assertEqual([l["external_item_id"] for l in ls if l["is_primary"]], ["ABC-7"])
        ev = [r for (r,) in self.q("SELECT field FROM item_events WHERE item_id=4")]
        self.assertGreaterEqual(ev.count("link"), 3)
        with self.assertRaises(ValueError):
            TR.add_link("action_items", 4, text="ABC-1", role="eng_tix")

    def test_legacy_role_names_still_accepted(self):
        r = TR.add_link("action_items", 4, text="DEV-1", role="dev_ticket")
        self.assertEqual(r["role"], "tracks")

    def test_primary_links_for_list(self):
        c = sqlite3.connect(str(self.db))
        c.row_factory = sqlite3.Row                              # as every app connection
        pl = TR.primary_links(c, [1, 2, 4])
        c.close()
        self.assertEqual(pl[1]["label"], "ABC-99")
        self.assertEqual(pl[1]["n"], 4)
        self.assertNotIn(4, pl)

    # -- stale
    def test_stale(self):
        s = TR.stale_links(60, today=dt.date(2026, 11, 10))
        self.assertEqual({r["key"] for r in s["unchecked"]}, {"ABC-100", "ABC-99", "DEV-5"})
        TR.record_link_activity({
            "ABC-99": {"status": "In Progress", "updated": "2026-11-01T10:00:00"},
            "ABC-100": {"status": "Needs Triage", "updated": "2026-08-01T10:00:00"},
            "DEV-5": {"status": "Done", "updated": "2026-09-01T10:00:00"}})
        s = TR.stale_links(60, today=dt.date(2026, 11, 10))
        self.assertEqual([(r["key"], r["idle_days"]) for r in s["idle"]], [("ABC-100", 101)])
        self.assertEqual([r["key"] for r in s["done"]], ["DEV-5"])
        self.assertEqual(s["unchecked"], [])
        self.assertNotIn("XYZ-7", str(s))                        # closed item never listed


if __name__ == "__main__":
    unittest.main()

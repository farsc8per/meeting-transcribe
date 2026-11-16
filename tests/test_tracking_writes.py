"""The tracker's write paths: what each one changes, what it logs, and what it bumps.

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Builds a fresh database from schema.sql in a temp folder (what `mt init` does), brings it
to the current shape with dbschema.upgrade(), and never opens a real one.

For every write: the row changes as expected; an item_events row appears where the code
writes one (and none where it does not); `rev` moves only where it should. rev is bumped
by the trg_ai_rev trigger on every UPDATE of action_items, by any writer; writes to other
tables (links, deps, people, projects) leave the item's rev alone.
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

BY = "tester"
SEED = """
INSERT INTO customers (id, name, short_name) VALUES (1, 'Acme Stores', 'acme');
INSERT INTO contacts (id, customer_id, name, role) VALUES
    (1, 1, 'Alex Example', 'IT lead'),
    (2, 1, 'Sam Sample', 'Store ops');
INSERT INTO projects (id, customer_id, name, status) VALUES
    (1, 1, 'Rollout', 'active'),
    (2, 1, 'Refresh', 'active');
INSERT INTO action_items (id, customer_id, project_id, parent_id, description, status)
VALUES
    (1, 1, 1, NULL, 'Parent work', 'open'),
    (2, 1, 1, 1, 'Child work', 'open'),
    (3, 1, NULL, NULL, 'Loose work', 'open'),
    (4, 1, NULL, NULL, 'More loose work', 'open');
"""


class TrackingWritesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-writes-"))
        self.db = self.tmp / "DB" / "mt.db"
        self.assertTrue(DS.create(self.db))
        self.assertEqual(DS.upgrade(self.db)["status"], "done")
        self._old = TR.DB_PATH
        TR.DB_PATH = self.db
        TR.ensure_schema()
        c = sqlite3.connect(str(self.db))
        c.executescript(SEED)
        c.commit()
        c.close()
        self.ev0 = self.max_event()        # the seed's own events are not under test

    def tearDown(self):
        TR.DB_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers

    def q(self, sql, *a):
        c = sqlite3.connect(str(self.db))
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql, a)]
        finally:
            c.close()

    def one(self, sql, *a):
        rows = self.q(sql, *a)
        self.assertEqual(len(rows), 1, f"{sql} {a} -> {rows}")
        return rows[0]

    def max_event(self) -> int:
        return self.one("SELECT COALESCE(max(id), 0) AS n FROM item_events")["n"]

    def events(self, item_id=None):
        """item_events written since setUp (optionally for one item), oldest first."""
        if item_id is None:
            return self.q("SELECT * FROM item_events WHERE id > ? ORDER BY id", self.ev0)
        return self.q("SELECT * FROM item_events WHERE id > ? AND item_id = ? ORDER BY id",
                      self.ev0, item_id)

    def rev(self, iid) -> int:
        return self.one("SELECT COALESCE(rev, 0) AS r FROM action_items WHERE id=?", iid)["r"]

    def item(self, iid) -> dict:
        return self.one("SELECT * FROM action_items WHERE id=?", iid)

    # ---- items

    def test_create_item(self):
        out = TR.create_item({"customer_id": 1, "description": "New work", "due_date":
                              "2026-12-01", "owner": "Alex Example"}, by=BY)
        iid = out["id"]
        r = self.item(iid)
        self.assertEqual((r["description"], r["status"], r["priority"], r["report_scope"],
                          r["due_date"], r["updated_by"]),
                         ("New work", "open", "normal", "unset", "2026-12-01", BY))
        self.assertEqual(self.rev(iid), 0)
        ev = self.events(iid)
        self.assertEqual([(e["field"], e["old"], e["new"], e["by"], e["rev"]) for e in ev],
                         [("*created", None, "open", BY, 0)])

    def test_create_item_refuses_blank_description(self):
        with self.assertRaises(ValueError):
            TR.create_item({"customer_id": 1, "description": "  "}, by=BY)
        self.assertEqual(self.events(), [])

    def test_update_item_logs_each_field_and_bumps_rev_once(self):
        TR.update_item(3, {"status": "done", "due_date": "2026-12-05"}, by=BY)
        r = self.item(3)
        self.assertEqual((r["status"], r["due_date"], r["updated_by"]),
                         ("done", "2026-12-05", BY))
        self.assertIsNotNone(r["completed_at"])           # stamped by trg_ai_rev
        self.assertEqual(self.rev(3), 1)
        got = {(e["field"], e["old"], e["new"], e["by"], e["rev"]) for e in self.events(3)}
        self.assertEqual(got, {("status", "open", "done", BY, 0),
                               ("due_date", None, "2026-12-05", BY, 0)})

    def test_update_item_with_current_base_rev(self):
        TR.update_item(3, {"priority": "high"}, by=BY, base_rev=0)
        TR.update_item(3, {"priority": "low"}, by=BY, base_rev=1)
        self.assertEqual((self.item(3)["priority"], self.rev(3)), ("low", 2))
        self.assertEqual([e["rev"] for e in self.events(3)], [0, 1])

    def test_update_item_stale_base_rev_is_a_conflict_and_writes_nothing(self):
        TR.update_item(3, {"status": "waiting"}, by="the other window")     # rev 0 -> 1
        before, n_ev = self.item(3), len(self.events())
        with self.assertRaises(TR.Conflict) as cm:
            TR.update_item(3, {"status": "blocked"}, by=BY, base_rev=0)
        self.assertEqual(cm.exception.rev, 1)
        self.assertEqual([(c["field"], c["by"]) for c in cm.exception.changes],
                         [("status", "the other window")])
        self.assertEqual(self.item(3), before)
        self.assertEqual(len(self.events()), n_ev)

    def test_update_item_project_change_carries_sub_items(self):
        out = TR.update_item(1, {"project_id": 2}, by=BY)
        self.assertEqual(out["carried"], [2])
        self.assertEqual((self.item(1)["project_id"], self.item(2)["project_id"]), (2, 2))
        self.assertEqual((self.rev(1), self.rev(2)), (1, 1))
        for iid in (1, 2):
            self.assertEqual([(e["field"], e["old"], e["new"], e["by"])
                              for e in self.events(iid)], [("project_id", "1", "2", BY)])

    def test_update_item_refused_by_shape_rules_writes_nothing(self):
        with self.assertRaises(ValueError):
            TR.update_item(2, {"project_id": 2}, by=BY)      # a sub-item follows its parent
        self.assertEqual((self.item(2)["project_id"], self.rev(2)), (1, 0))
        self.assertEqual(self.events(), [])

    def test_bulk_update(self):
        out = TR.bulk_update([3, 4, 999, 3], {"status": "waiting"}, by=BY)
        self.assertEqual(out["changed"], [3, 4])
        self.assertEqual(out["skipped"], [{"id": 999, "why": "no such item"}])
        for iid in (3, 4):
            self.assertEqual((self.item(iid)["status"], self.item(iid)["updated_by"],
                              self.rev(iid)), ("waiting", BY, 1))
            self.assertEqual([(e["field"], e["old"], e["new"], e["by"])
                              for e in self.events(iid)], [("status", "open", "waiting", BY)])

    def test_bulk_update_refuses_non_canonical_value(self):
        with self.assertRaises(ValueError):
            TR.bulk_update([3, 4], {"status": "closed"}, by=BY)
        self.assertEqual(self.events(), [])
        self.assertEqual((self.rev(3), self.rev(4)), (0, 0))

    # ---- leases

    def test_set_leases_replaces_and_releases(self):
        self.assertEqual(TR.set_leases("gantt-A", [3, 4])["held"], 2)
        self.assertEqual([(l["item_id"], l["holder"]) for l in TR.active_leases()],
                         [(3, "gantt-A"), (4, "gantt-A")])
        TR.set_leases("gantt-A", [4])                       # replaces, not adds
        self.assertEqual([l["item_id"] for l in TR.active_leases()], [4])
        TR.set_leases("gantt-B", [3])
        self.assertEqual([l["item_id"] for l in TR.active_leases([3])], [3])
        TR.set_leases("gantt-A", [])                        # releases A only
        self.assertEqual([(l["item_id"], l["holder"]) for l in TR.active_leases()],
                         [(3, "gantt-B")])
        self.assertEqual(self.events(), [])                 # leases are not item changes
        with self.assertRaises(ValueError):
            TR.set_leases("  ", [3])

    def test_lease_is_advisory_write_still_goes_through(self):
        """R4.5b leases are soft: web.py's /api/tracking/lease "never blocks anything",
        and no write path reads edit_leases. The check is the caller's job
        (`tracking.py --leases #id`). This pins today's behavior; if leases become a hard
        refusal, this test changes with them."""
        TR.set_leases("gantt-A", [3])
        self.assertEqual([l["holder"] for l in TR.active_leases([3])], ["gantt-A"])
        TR.update_item(3, {"status": "waiting"}, by=BY)
        self.assertEqual((self.item(3)["status"], self.rev(3)), ("waiting", 1))

    # ---- links

    def test_add_link_first_is_primary_and_logged(self):
        a = TR.add_link("action_items", 3, system="jira", item="ABC-1", role="tracks", by=BY)
        b = TR.add_link("action_items", 3, system="jira", item="ABC-2", role="reference",
                        by=BY)
        rows = self.q("SELECT id, external_item_id, role, is_primary FROM external_links "
                      "WHERE local_table='action_items' AND local_id=3 ORDER BY id")
        self.assertEqual([(r["external_item_id"], r["role"], r["is_primary"]) for r in rows],
                         [("ABC-1", "tracks", 1), ("ABC-2", "reference", 0)])
        self.assertEqual((a["existed"], b["existed"]), (False, False))
        ev = self.events(3)
        self.assertEqual([(e["field"], e["old"], e["new"], e["by"]) for e in ev],
                         [("link", None, "+ tracks jira:ABC-1", BY),
                          ("link", None, "+ reference jira:ABC-2", BY)])
        self.assertEqual(self.rev(3), 0)                    # a link is not an item UPDATE

    def test_add_link_again_is_idempotent(self):
        TR.add_link("action_items", 3, system="jira", item="ABC-1", role="tracks", by=BY)
        n_ev = len(self.events())
        again = TR.add_link("action_items", 3, system="jira", item="ABC-1", role="tracks",
                            by=BY)
        self.assertTrue(again["existed"])
        self.assertEqual(len(self.q("SELECT id FROM external_links WHERE local_id=3")), 1)
        self.assertEqual(len(self.events()), n_ev)

    def test_add_link_refuses_missing_item(self):
        with self.assertRaises(ValueError):
            TR.add_link("action_items", 999, system="jira", item="ABC-1", by=BY)
        self.assertEqual(self.q("SELECT id FROM external_links"), [])

    def test_set_primary_link_keeps_one_primary(self):
        TR.add_link("action_items", 3, system="jira", item="ABC-1", by=BY)
        b = TR.add_link("action_items", 3, system="jira", item="ABC-2", by=BY)
        TR.add_link("action_items", 4, system="jira", item="ABC-9", by=BY)
        TR.set_primary_link(b["id"], by=BY)
        prim = self.q("SELECT external_item_id FROM external_links WHERE local_id=3 AND "
                      "is_primary=1")
        self.assertEqual([r["external_item_id"] for r in prim], ["ABC-2"])
        self.assertEqual(self.one("SELECT is_primary FROM external_links WHERE "
                                  "external_item_id='ABC-9'")["is_primary"], 1)   # other row
        self.assertEqual([(e["field"], e["new"], e["by"]) for e in self.events(3)][-1],
                         ("link", "★ jira:ABC-2", BY))
        n_ev = len(self.events())
        TR.set_primary_link(b["id"], by=BY)                 # already primary: no-op
        self.assertEqual(len(self.events()), n_ev)
        self.assertEqual(self.rev(3), 0)

    # ---- deps

    def test_add_dep(self):
        out = TR.add_dep(3, 4, by=BY, lag_days=2)
        r = self.one("SELECT * FROM action_item_deps WHERE id=?", out["id"])
        self.assertEqual((r["pred_id"], r["succ_id"], r["lag_days"], r["kind"], r["created_by"],
                          r["met_at"], r["removed_at"]), (3, 4, 2, "FS", BY, None, None))
        self.assertFalse(out["existed"])
        self.assertTrue(TR.add_dep(3, 4, by=BY)["existed"])
        self.assertEqual(len(self.q("SELECT id FROM action_item_deps")), 1)
        # one 'dep' event on EACH item (none for the repeat), and no rev change
        self.assertEqual([(e["item_id"], e["field"], e["old"], e["new"], e["by"], e["rev"])
                          for e in self.events()],
                         [(4, "dep", None, "+ after #3 (lag 2 working days)", BY, 0),
                          (3, "dep", None, "+ before #4 (lag 2 working days)", BY, 0)])
        self.assertEqual((self.rev(3), self.rev(4)), (0, 0))

    def test_remove_dep_logged_on_both(self):
        dep = TR.add_dep(3, 4, by=BY)["id"]
        n = len(self.events())
        TR.remove_dep(dep, by="other")
        self.assertEqual([(e["item_id"], e["old"], e["new"], e["by"])
                          for e in self.events()[n:]],
                         [(4, "- after #3", None, "other"), (3, "- before #4", None, "other")])
        self.assertEqual((self.rev(3), self.rev(4)), (0, 0))

    def test_add_dep_refuses_self_loop_and_missing(self):
        TR.add_dep(3, 4, by=BY)
        for pred, succ in ((3, 3), (4, 3), (3, 999)):
            with self.assertRaises(ValueError, msg=f"{pred}->{succ}"):
                TR.add_dep(pred, succ, by=BY)
        self.assertEqual(len(self.q("SELECT id FROM action_item_deps")), 1)

    def test_set_dep_met(self):
        dep = TR.add_dep(3, 4, by=BY)["id"]
        n = len(self.events())
        TR.set_dep_met(dep, True, note="  last cleanup can trail  ", by=BY)
        r = self.one("SELECT met_at, met_note FROM action_item_deps WHERE id=?", dep)
        self.assertIsNotNone(r["met_at"])
        self.assertEqual(r["met_note"], "last cleanup can trail")
        TR.set_dep_met(dep, False, note="ignored when clearing", by=BY)
        r = self.one("SELECT met_at, met_note FROM action_item_deps WHERE id=?", dep)
        self.assertEqual((r["met_at"], r["met_note"]), (None, None))
        self.assertEqual([(e["item_id"], e["old"], e["new"]) for e in self.events()[n:]], [
            (4, "after #3", "after #3 — met anyway: last cleanup can trail"),
            (3, "before #4", "before #4 — met anyway: last cleanup can trail"),
            (4, "after #3 — met anyway", "after #3"),
            (3, "before #4 — met anyway", "before #4")])
        n = len(self.events())
        TR.set_dep_met(dep, False, by=BY)                   # already clear: nothing logged
        self.assertEqual(len(self.events()), n)
        self.assertEqual((self.rev(3), self.rev(4)), (0, 0))
        TR.remove_dep(dep, by=BY)
        with self.assertRaises(ValueError):
            TR.set_dep_met(dep, True, by=BY)

    # ---- people

    def test_set_item_person_add_dedupe_remove(self):
        TR.update_item(3, {"owner_contact_id": 1}, by=BY)
        rev = self.rev(3)
        TR.set_item_person(3, contact_id=2, by=BY)
        TR.set_item_person(3, contact_id=2, by=BY)          # INSERT OR IGNORE
        live = self.q("SELECT * FROM item_people WHERE item_id=3 AND removed_at IS NULL")
        self.assertEqual([(r["contact_id"], r["role"], r["created_by"]) for r in live],
                         [(2, "with", BY)])
        with self.assertRaises(ValueError):
            TR.set_item_person(3, contact_id=1, by=BY)      # already the owner
        with self.assertRaises(ValueError):
            TR.set_item_person(3, contact_id=999, by=BY)
        TR.set_item_person(3, remove_pid=live[0]["id"], by=BY)
        r = self.one("SELECT removed_at, removed_by FROM item_people WHERE id=?",
                     live[0]["id"])
        self.assertIsNotNone(r["removed_at"])
        self.assertEqual(r["removed_by"], BY)
        with self.assertRaises(ValueError):
            TR.set_item_person(3, remove_pid=live[0]["id"], by=BY)
        self.assertEqual(self.rev(3), rev)                  # people live in item_people
        # one 'with' event per real change: the duplicate add and the refusals log nothing
        self.assertEqual([(e["field"], e["old"], e["new"], e["by"], e["rev"])
                          for e in self.events(3) if e["field"] == "with"],
                         [("with", None, "+ Sam Sample", BY, rev),
                          ("with", "- Sam Sample", None, BY, rev)])
        self.assertEqual([e["item_id"] for e in self.events() if e["field"] == "with"], [3, 3])
        # and the change feed now names the item, so other windows repaint it
        head = TR.changes(-1)["last"]
        TR.set_item_person(3, contact_id=2, by=BY)
        self.assertEqual(TR.changes(head)["ids"], [3])

    # ---- the Gantt's Save: dates and links in one transaction

    def test_apply_plan_logs_links_and_undo_still_matches(self):
        out = TR.apply_plan([{"id": 3, "rev": 0, "start_date": "2026-12-01",
                              "end_date": "2026-12-03"}],
                            by=BY, links=[{"op": "add", "pred": 3, "succ": 4}])
        self.assertEqual(self.rev(3), 1)                    # the date change, once
        self.assertEqual(self.rev(4), 0)                    # only linked: no bump
        self.assertEqual(out["undo"][0]["rev"], 1)          # undo is guarded on that rev
        deps = [(e["item_id"], e["new"]) for e in self.events() if e["field"] == "dep"]
        self.assertEqual(deps, [(4, "+ after #3"), (3, "+ before #4")])
        # Undo (what the toast sends) goes through: the dep events did not move rev
        TR.apply_plan([{"id": 3, "rev": 1, "start_date": None, "end_date": None}], by=BY,
                      links=out["undo_links"])
        self.assertEqual((self.item(3)["start_date"], self.item(3)["end_date"]), (None, None))
        self.assertEqual(self.q("SELECT id FROM action_item_deps WHERE removed_at IS NULL"), [])
        self.assertEqual([(e["item_id"], e["old"]) for e in self.events()
                          if e["field"] == "dep"][-2:], [(4, "- after #3"), (3, "- before #4")])

    # ---- projects

    def test_create_project(self):
        out = TR.create_project({"customer_id": 1, "name": "  Pilot  ", "type": "",
                                 "notes": "n"}, by=BY)
        r = self.one("SELECT * FROM projects WHERE id=?", out["id"])
        self.assertEqual((r["name"], r["status"], r["customer_id"], r["updated_by"]),
                         ("Pilot", "active", 1, BY))
        self.assertIsNone(r["archived_at"])
        with self.assertRaises(ValueError):
            TR.create_project({"customer_id": 1, "name": " "}, by=BY)
        self.assertEqual(self.events(), [])

    def test_update_project(self):
        TR.update_project(2, {"notes": "moved to Q1", "status": "paused"}, by=BY)
        r = self.one("SELECT * FROM projects WHERE id=2")
        self.assertEqual((r["notes"], r["status"], r["updated_by"]),
                         ("moved to Q1", "paused", BY))
        before = self.one("SELECT * FROM projects WHERE id=2")
        for bad in ({"name": ""}, {"status": "nope"}, {}):
            with self.assertRaises(ValueError, msg=str(bad)):
                TR.update_project(2, bad, by=BY)
        self.assertEqual(self.one("SELECT * FROM projects WHERE id=2"), before)
        with self.assertRaises(ValueError):
            TR.update_project(999, {"notes": "x"}, by=BY)

    def test_set_project_archived_leaves_items_alone(self):
        out = TR.set_project_archived(1, True, by=BY)
        self.assertEqual(out["item_count"], 2)
        r = self.one("SELECT archived_at, updated_by FROM projects WHERE id=1")
        self.assertIsNotNone(r["archived_at"])
        self.assertEqual(r["updated_by"], BY)
        self.assertEqual([(self.item(i)["project_id"], self.rev(i)) for i in (1, 2)],
                         [(1, 0), (1, 0)])
        self.assertEqual(self.events(), [])
        TR.set_project_archived(1, False, by=BY)
        self.assertIsNone(self.one("SELECT archived_at FROM projects WHERE id=1")["archived_at"])
        with self.assertRaises(ValueError):
            TR.set_project_archived(999, True, by=BY)


if __name__ == "__main__":
    unittest.main()

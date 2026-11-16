"""Hygiene backups go to backups/ beside the DB and are pruned to the newest N.

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Builds a throwaway DB in a temp folder (no backups/ yet, not named like anyone's real DB);
never opens a real one.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paths as P          # noqa: E402
import tracking as TR      # noqa: E402
import hygiene as HY       # noqa: E402


class HygieneBackupTest(unittest.TestCase):
    KEEP = 4

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-hygbk-"))
        self.db = self.tmp / "someone-else.db"
        c = sqlite3.connect(str(self.db))
        c.execute("CREATE TABLE t (x INTEGER)")
        c.execute("INSERT INTO t VALUES (42)")
        c.commit()
        c.close()
        self._old = (TR.DB_PATH, P.HYGIENE_BACKUPS_KEEP)
        TR.DB_PATH = self.db
        P.HYGIENE_BACKUPS_KEEP = self.KEEP

    def tearDown(self):
        TR.DB_PATH, P.HYGIENE_BACKUPS_KEEP = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_ten(self):
        self.assertEqual(P._int_setting("no_such_key", 10), 10)

    def test_creates_backups_folder_and_restorable_copy(self):
        self.assertFalse((self.tmp / "backups").exists())
        out = Path(HY.backup("first fix"))
        self.assertEqual(out.parent, self.tmp / "backups")
        self.assertRegex(out.name, r"^someone-else\.db\.bak-\d{8}-\d{6}-hygiene-first-fix\.gz$")
        restored = self.tmp / "restored.db"
        restored.write_bytes(gzip.decompress(out.read_bytes()))
        c = sqlite3.connect(str(restored))
        self.assertEqual(c.execute("SELECT x FROM t").fetchone()[0], 42)
        c.close()

    def test_nothing_written_next_to_db(self):
        HY.backup("x")
        loose = [p.name for p in self.tmp.iterdir() if p.name != "backups"]
        self.assertEqual(loose, ["someone-else.db"])

    def test_keeps_newest_n_and_leaves_other_files_alone(self):
        bk = self.tmp / "backups"
        bk.mkdir()
        others = {
            "mydb-2026-10-01-pre-billing.db.gz": b"manual named copy",
            "someone-else-2026-11-13-pre-upgrade.db": b"dbschema.backup",
            "someone-else.db.bak-20260101-000000-hygiene-old.gz.part": b"half written",
            "other.db.bak-20200101-000000-hygiene-x.gz": b"another DB's hygiene backup",
            "someone-else.db.bak-20200101-000000-manual.gz": b"not hygiene",
            "notes.txt": b"hello",
        }
        for name, data in others.items():
            (bk / name).write_bytes(data)
        written = [Path(HY.backup(f"group {i}")) for i in range(self.KEEP + 3)]

        mine = sorted(p.name for p in bk.iterdir() if HY._backup_re("someone-else.db").match(p.name))
        self.assertEqual(len(mine), self.KEEP)
        self.assertEqual(set(mine), {p.name for p in written[-self.KEEP:]})
        for name, data in others.items():
            self.assertEqual((bk / name).read_bytes(), data, name)

    def test_same_second_same_label_does_not_overwrite(self):
        a, b = HY.backup("same"), HY.backup("same")
        self.assertNotEqual(a, b)
        self.assertTrue(Path(a).exists() and Path(b).exists())

    def test_explicit_keep_and_floor_of_one(self):
        for i in range(3):
            last = HY.backup(f"k{i}", keep=0)
        left = [p for p in (self.tmp / "backups").iterdir()]
        self.assertEqual([p.name for p in left], [Path(last).name])


if __name__ == "__main__":
    unittest.main()

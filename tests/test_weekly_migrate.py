"""weekly.py --migrate refuses the live DB the same way tracking.py does.

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Builds databases from schema.sql in a temp folder; never opens a real one. The "live" DB
here is whatever tracking.DB_PATH points at during the test (a temp file).
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dbschema as DS      # noqa: E402
import tracking as TR      # noqa: E402
import weekly as WK        # noqa: E402


class WeeklyMigrateGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-wkmig-"))
        self.live = self.tmp / "live" / "tracker-live.db"
        self.assertTrue(DS.create(self.live))
        self._old = TR.DB_PATH
        TR.DB_PATH = self.live

    def tearDown(self):
        TR.DB_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, *args) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = WK._main(list(args))
        TR.DB_PATH = self.live           # _main repoints it on success
        return rc, err.getvalue()

    def test_refuses_the_live_db(self):
        before = self.live.read_bytes()
        rc, err = self.run_main("--migrate", str(self.live))
        self.assertEqual(rc, 3)
        self.assertIn("refusing", err)
        self.assertEqual(self.live.read_bytes(), before)

    def test_refuses_a_copy_with_the_live_name(self):
        """The case paths.DB alone missed: a different folder, the live DB's file name."""
        other = self.tmp / "elsewhere" / self.live.name
        self.assertTrue(DS.create(other))
        before = other.read_bytes()
        rc, _ = self.run_main("--migrate", str(other))
        self.assertEqual(rc, 3)
        self.assertEqual(other.read_bytes(), before)

    def test_migrates_a_renamed_copy(self):
        copy = self.tmp / "copy" / "renamed-copy.db"
        self.assertTrue(DS.create(copy))
        rc, err = self.run_main("--migrate", str(copy))
        self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()

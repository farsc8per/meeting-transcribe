"""Mark reviewed and hand off → auto-confirm strong voices (promote.py + web.handoff).

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Uses throwaway session folders and an explicit contact set: it never opens a DB, never
reads or writes voiceprints/.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mt as MT          # noqa: E402
import promote as PR     # noqa: E402

KNOWN = {"Alex Example", "Sam Sample", "Pat Placeholder", "Lee Live"}
NOW = "2026-11-09T16:45:00+00:00"


def vp(name, score, runner, ss=0.9, source="voiceprint match"):
    return {"name": name, "source": source, "confidence": "unconfirmed",
            "self_similarity": ss, "score": score, "runner_up": runner}


def session(tmp: Path, smap: dict) -> Path:
    d = tmp / "2026-11-09-1000-test-session"
    d.mkdir()
    smap = {**smap, "mic": {"name": "Owner Person", "source": "mic track (you)",
                            "confidence": "verified"}}
    (d / "speakers.json").write_text(json.dumps({"owner": "Owner Person", "map": smap},
                                                indent=2) + "\n")
    tl = [{"start": float(i * 5), "end": float(i * 5 + 4), "track": "system" if k != "mic"
           else "mic", "speaker_key": k, "text": f"line from {k}"}
          for i, k in enumerate(smap)]
    (d / "transcript.json").write_text(json.dumps(
        {"meta": {"title": "test", "duration": 60}, "timeline": tl, "speakers": smap},
        indent=2) + "\n")
    return d


class Promote(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-promote-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_promote(self, smap):
        d = session(self.tmp, smap)
        r = PR.promote(d, known=KNOWN, now=NOW)
        return d, r, json.loads((d / "speakers.json").read_text())["map"]

    def test_reference_session_shape(self):
        # Same numbers as the Nov 9 SBR-runbook call: 0.847 with a 0.236 lead, and 0.917.
        d, r, m = self.run_promote({
            "system-unknown": {"name": "Unidentified voice", "source": "brief/unmatched speech",
                               "confidence": "unconfirmed"},
            "system-spk0": vp("Alex Example", 0.847, 0.611, ss=0.758),
            "system-spk1": vp("Sam Sample", 0.917, 0.539, ss=0.939,
                              source="voiceprint match; also in the on-screen roster"),
        })
        self.assertEqual({p["key"] for p in r["promoted"]}, {"system-spk0", "system-spk1"})
        for k in ("system-spk0", "system-spk1"):
            self.assertEqual(m[k]["confidence"], "verified")
            self.assertEqual(m[k]["confirmed_by"], "ready-to-review")
            self.assertEqual(m[k]["confirmed_at"], NOW)
        self.assertIn("0.847", m["system-spk0"]["confirmed_basis"])
        self.assertIn("ahead of the next print", m["system-spk0"]["confirmed_basis"])
        self.assertEqual(m["system-unknown"]["confidence"], "unconfirmed")
        self.assertNotIn("confirmed_by", m["system-unknown"])
        self.assertNotIn("confirmed_by", m["mic"])
        self.assertIn("Alex Example", r["line"])
        self.assertIn("still unconfirmed: Unidentified voice (unidentified)", r["line"])

    def test_weak_and_ineligible_stay_open(self):
        d, r, m = self.run_promote({
            "system-spk0": vp("Alex Example", 0.80, 0.70),                    # weak, small lead
            "system-spk1": vp("Nobody Known", 0.93, 0.40),                    # not a contact
            "system-spk2": {**vp("Sam Sample", 0.95, 0.30), "mixed": True},   # mixed
            "system-spk3": vp("Pat Placeholder", 0.92, 0.30, ss=0.50),        # impure
            "system-spk4": {"name": "Pat Placeholder", "source": "on-screen speaker",
                            "confidence": "unconfirmed"},                     # screen only
        })
        self.assertEqual(r["promoted"], [])
        self.assertTrue(all(v["confidence"] == "unconfirmed"
                            for k, v in m.items() if k != "mic"))
        why = {s["key"]: s["why"] for s in r["open"]}
        self.assertEqual(why["system-spk0"], "weak match 0.800")
        self.assertEqual(why["system-spk1"], "not a verified contact")
        self.assertEqual(why["system-spk2"], "several voices in one cluster")
        self.assertTrue(why["system-spk3"].startswith("impure cluster"))
        self.assertEqual(why["system-spk4"], "not a voiceprint match")
        self.assertTrue(r["line"].startswith("Nothing new to auto-confirm"))

    def test_just_under_bar_without_lead_stays_open(self):
        _, r, m = self.run_promote({"system-spk0": vp("Alex Example", 0.849, 0.70)})
        self.assertEqual(r["promoted"], [])
        self.assertEqual(m["system-spk0"]["confidence"], "unconfirmed")

    def test_live_named(self):
        live = {"name": "Lee Live", "source": "named live by you — strong match (0.91)",
                "confidence": "unconfirmed", "live_named": 0.91, "live_strong": True,
                "self_similarity": 0.9}
        weak = {**live, "name": "Sam Sample", "live_named": 0.74, "live_strong": False,
                "source": "named live by you — possible match (0.74)"}
        _, r, m = self.run_promote({"system-spk0": live, "system-spk1": weak})
        self.assertEqual([p["key"] for p in r["promoted"]], ["system-spk0"])
        self.assertIn("named live", m["system-spk0"]["confirmed_basis"])
        self.assertEqual(m["system-spk1"]["confidence"], "unconfirmed")
        self.assertEqual(r["open"][0]["why"], "weak live match 0.740")

    def test_owner_confirmed_is_left_alone_and_rerun_is_a_noop(self):
        mine = {**vp("Pat Placeholder", 0.70, 0.60), "confidence": "verified",
                "source": "confirmed by Owner Person (listened)"}
        d, r, m = self.run_promote({"system-spk0": mine,
                                    "system-spk1": vp("Sam Sample", 0.90, 0.40)})
        self.assertNotIn("confirmed_by", m["system-spk0"])
        before = (d / "speakers.json").read_text()
        r2 = PR.promote(d, known=KNOWN, now="2026-11-10T00:00:00+00:00")
        self.assertEqual(r2["promoted"], [])
        self.assertEqual((d / "speakers.json").read_text(), before)

    def test_never_touches_voiceprints(self):
        calls = []
        saved = MT.cmd_enroll, MT.enroll_guard
        MT.cmd_enroll = lambda *a, **k: calls.append("enroll")
        MT.enroll_guard = lambda *a, **k: calls.append("guard")
        try:
            d = session(self.tmp, {"system-spk0": vp("Sam Sample", 0.95, 0.30)})
            files = {p.name: p.stat().st_mtime_ns for p in d.iterdir()}
            PR.promote(d, known=KNOWN, now=NOW)
            changed = {p.name for p in d.iterdir()
                       if files.get(p.name) != p.stat().st_mtime_ns}
        finally:
            MT.cmd_enroll, MT.enroll_guard = saved
        self.assertEqual(calls, [])
        self.assertEqual(changed, {"speakers.json"})


class Handoff(unittest.TestCase):
    """web.handoff end to end: promote, re-render like `mt relabel`, write handoff.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-handoff-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handoff_promotes_and_relabels(self):
        import web as W
        d = session(self.tmp, {
            "system-unknown": {"name": "Unidentified voice", "source": "brief/unmatched speech",
                               "confidence": "unconfirmed"},
            "system-spk0": vp("Alex Example", 0.847, 0.611, ss=0.758),
            "system-spk1": vp("Sam Sample", 0.917, 0.539),
        })
        saved = (W.existing_session_dirs, MT.WORKSPACE, PR._known)
        W.existing_session_dirs = lambda: [d]
        MT.WORKSPACE = self.tmp
        PR._known = lambda: KNOWN
        try:
            r = W.handoff({"session": d.name})
        finally:
            W.existing_session_dirs, MT.WORKSPACE, PR._known = saved
        self.assertTrue(r["ok"])
        self.assertEqual({p["key"] for p in r["promoted"]}, {"system-spk0", "system-spk1"})
        self.assertEqual(r["open"], ["system-unknown"])
        self.assertIn("Auto-confirmed", r["promote_line"])
        h = json.loads((d / "handoff.json").read_text())
        self.assertEqual(sorted(h["auto_confirmed"]), ["system-spk0", "system-spk1"])
        self.assertEqual(h["voices_open"], ["system-unknown"])
        # Re-rendered the way `mt relabel` does: transcript.json carries the new map, and
        # the markdown roster no longer calls the promoted voices unconfirmed.
        tj = json.loads((d / "transcript.json").read_text())
        self.assertEqual(tj["speakers"]["system-spk1"]["confidence"], "verified")
        md = (d / "transcript.md").read_text()
        row = next(l for l in md.splitlines() if "`system-spk0`" in l)
        self.assertNotIn("unconfirmed", row)
        self.assertIn("auto-confirmed at hand-off", row)
        unk = next(l for l in md.splitlines() if "`system-unknown`" in l)
        self.assertIn("**unconfirmed**", unk)
        self.assertIn("Alex Example:", (d / "transcript.txt").read_text())


if __name__ == "__main__":
    unittest.main()

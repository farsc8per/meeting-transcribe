"""Room naming without vocal cues, stable card order, and ✓ Confirm handed down after the pass
(livenames.py + cues.live_score).

Run from the repo root:  lib/venv/bin/python -m unittest discover -s tests -v
Throwaway session folders only: the contacts set is passed in, stored voiceprints are
stubbed out, and in-call samples are written directly (no audio is decoded).
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np       # noqa: E402

import cues              # noqa: E402
import livenames as LN   # noqa: E402

DIM = 192
KNOWN = {"Alex Example", "Sam Sample"}
T = "2026-11-09T18:00:00+00:00"


def unit(i: int) -> np.ndarray:
    v = np.zeros(DIM, np.float32)
    v[i] = 1.0
    return v


def near(i: int, cos: float, j: int = 100) -> np.ndarray:
    """A vector at exactly `cos` to unit(i)."""
    return (cos * unit(i) + math.sqrt(max(0.0, 1 - cos * cos)) * unit(j)).astype(np.float32)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mt-livenames-"))
        self.d = self.tmp / "2026-11-09-1300-test-call"
        (self.d / "work").mkdir(parents=True)
        LN._cache.clear()
        self.p = mock.patch.object(LN, "_stored_prints", lambda: [])
        self.p.start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def live_doc(self, rules, prints):
        (self.d / LN.FILE).write_text(json.dumps({
            "rules": rules,
            "prints": {n: {"vecs": [[float(a) for a in v]], "secs": 20.0}
                       for n, v in prints.items()}}) + "\n")

    def speakers(self, clusters: dict) -> None:
        m = {"mic": {"name": "Owner Person", "confidence": "verified"}}
        for key, (name, vec) in clusters.items():
            m[key] = {"name": name, "confidence": "unconfirmed", "source": "voiceprint match"}
            np.save(self.d / "work" / f"{key}.npy", vec[None, :])
        (self.d / "speakers.json").write_text(json.dumps({"map": m}) + "\n")

    def smap(self) -> dict:
        return json.loads((self.d / "speakers.json").read_text())["map"]


def rule(kind, frm, to, at=T):
    return {"kind": kind, "from": frm, "to": to, "segments": [[1.0, 9.0]], "at": at}


class ConfirmHandedDown(Base):
    def test_confirmed_contact_strong_match_is_verified(self):
        self.live_doc([rule("match", "Alex Example", "Alex Example"),
                       rule(LN.CONFIRM, "Alex Example", "Alex Example")],
                      {"Alex Example": unit(0)})
        self.speakers({"system-spk0": ("Speaker 0", near(0, 0.90)),
                       "system-spk1": ("Speaker 1", unit(5))})
        LN.apply_after_pass(self.d, known=KNOWN)
        v = self.smap()["system-spk0"]
        self.assertEqual(v["confidence"], "verified")
        self.assertEqual(v["confirmed_by"], LN.LIVE_BY)
        self.assertEqual(v["confirmed_at"], T)
        self.assertIn("confirmed Alex Example live", v["confirmed_basis"])
        self.assertEqual(self.smap()["system-spk1"]["confidence"], "unconfirmed")

    def test_confirmed_top_cluster_under_strong_is_verified_runner_up_waits(self):
        # 0.80 is under POST_STRONG and has no 0.20 lead over the 0.78 cluster — it is
        # still the closest cluster to what you confirmed, so it is the one you meant.
        self.live_doc([rule(LN.CONFIRM, "Sam Sample", "Sam Sample")], {"Sam Sample": unit(0)})
        self.speakers({"system-spk0": ("Speaker 0", near(0, 0.80, 101)),
                       "system-spk1": ("Speaker 1", near(0, 0.78, 102))})
        LN.apply_after_pass(self.d, known=KNOWN)
        m = self.smap()
        self.assertEqual(m["system-spk0"]["confidence"], "verified")
        self.assertEqual(m["system-spk1"]["confidence"], "unconfirmed")
        self.assertEqual(m["system-spk1"]["name"], "Sam Sample")      # still pre-filled

    def test_confirmed_but_not_a_contact_waits(self):
        self.live_doc([rule("voice", "Speaker 1", "New Person"),
                       rule(LN.CONFIRM, "New Person", "New Person")], {"New Person": unit(0)})
        self.speakers({"system-spk0": ("Speaker 0", near(0, 0.95))})
        LN.apply_after_pass(self.d, known=KNOWN)
        v = self.smap()["system-spk0"]
        self.assertEqual(v["confidence"], "unconfirmed")
        self.assertEqual(v["name"], "New Person")
        self.assertIn("not a verified contact", v["source"])
        self.assertNotIn("confirmed_by", v)

    def test_picked_but_not_confirmed_still_waits(self):
        # The Oct 6 rule stands: a name you only PICKED live is never a done deal.
        self.live_doc([rule("voice", "Speaker 1", "Alex Example")], {"Alex Example": unit(0)})
        self.speakers({"system-spk0": ("Speaker 0", near(0, 0.95))})
        LN.apply_after_pass(self.d, known=KNOWN)
        v = self.smap()["system-spk0"]
        self.assertEqual(v["confidence"], "unconfirmed")
        self.assertTrue(v["live_strong"])
        self.assertNotIn("confirmed_by", v)

    def test_confirmed_weak_match_waits(self):
        self.live_doc([rule(LN.CONFIRM, "Alex Example", "Alex Example")], {"Alex Example": unit(0)})
        self.speakers({"system-spk0": ("Speaker 0", near(0, 0.73))})
        LN.apply_after_pass(self.d, known=KNOWN)
        v = self.smap()["system-spk0"]
        self.assertEqual(v["confidence"], "unconfirmed")
        self.assertIn("not a clear match", v["source"])


class Decisions(Base):
    def setUp(self):
        super().setUp()
        self.rb = mock.patch.object(LN, "_rebuild_prints", lambda d, doc: None)
        self.rb.start()

    def tearDown(self):
        self.rb.stop()
        super().tearDown()

    def test_confirm_is_not_naming_and_never_relabels(self):
        LN.confirm(self.d, "Alex Example", [(1.0, 4.0)])
        doc = LN.load(self.d)
        self.assertEqual(LN.confirmed_set(doc), {"Alex Example"})
        self.assertEqual(LN.named_set(doc), set())
        LN._cache.clear()
        # a row the stored prints called someone else stays theirs
        self.assertEqual(LN.resolve(self.d, 1.0, 4.0, "Sam Sample", None), ("Sam Sample", None))
        self.assertEqual(LN.confirm(self.d, "Alex Example", [(5.0, 6.0)])["ok"], True)
        self.assertEqual(len(LN.load(self.d)["rules"]), 1)               # idempotent

    def test_rename_drops_the_check_unconfirm_and_undo(self):
        LN.add_rule(self.d, "voice", "Speaker 1", "Alex Example", [(1.0, 4.0)])
        LN.confirm(self.d, "Alex Example", [(1.0, 4.0)])
        LN.rename(self.d, "Alex Example", "Sam Sample")
        doc = LN.load(self.d)
        self.assertEqual(LN.named_set(doc), {"Sam Sample"})
        self.assertEqual(LN.confirmed_set(doc), set())
        LN.confirm(self.d, "Sam Sample", [(1.0, 4.0)])
        self.assertTrue(LN.unconfirm(self.d, "Sam Sample")["ok"])
        self.assertEqual(LN.confirmed_set(LN.load(self.d)), set())
        self.assertFalse(LN.unconfirm(self.d, "Sam Sample")["ok"])
        LN.confirm(self.d, "Sam Sample", [(1.0, 4.0)])
        LN.undo(self.d)                                                  # Undo last = the ✓
        doc = LN.load(self.d)
        self.assertEqual(LN.confirmed_set(doc), set())
        self.assertEqual(LN.named_set(doc), {"Sam Sample"})

    def test_confirm_needs_speech(self):
        self.assertFalse(LN.confirm(self.d, "Alex Example", [])["ok"])


class RoomWithoutCues(Base):
    def rows(self, spec):
        """spec: (start, track, voice index or None, text)"""
        with open(self.d / cues.LIVE_FILE, "w") as f:
            for s, track, vi, text in spec:
                r = {"s": s, "e": s + 3.0, "track": track, "who": "Owner Person" if track == "mic" else None,
                     "hint": None, "match": None, "text": text}
                if vi is not None:
                    r["v"] = [round(float(a), 4) for a in unit(vi)]
                f.write(json.dumps(r) + "\n")

    def test_names_only_feed_and_stable_order(self):
        # Voice A speaks first (10 s) but only gets a card with its 2nd line (40 s);
        # voice B's card appears at 30 s. B's card must sit before A's — A forming late
        # must not slide in above a card already on screen.
        self.rows([(5.0, "mic", None, "hi all"),
                   (10.0, "system", 0, "first from A"),
                   (20.0, "system", 1, "first from B"),
                   (30.0, "system", 1, "second from B"),
                   (40.0, "system", 0, "second from A")])
        out = cues.live_score(self.d, owner="Owner Person")
        self.assertFalse(out["requested"])
        far = [p for p in out["people"] if p["kind"] != "you"]
        self.assertEqual([p["name"] for p in far], ["Speaker 2", "Speaker 1"])
        self.assertEqual(far[0]["order"], 30.0)      # B: its card appeared with its 2nd line
        self.assertEqual(far[1]["order"], 40.0)
        self.assertEqual(out["people"][-1]["kind"], "you")
        for p in out["people"]:
            self.assertIsNone(p["now"])
            self.assertEqual(p["trend"], [])
            self.assertFalse(p["confirmed"])
        self.assertEqual(far[1]["recent"][-1]["text"], "second from A")

    def test_naming_keeps_the_cards_spot_and_shows_the_check(self):
        self.rows([(10.0, "system", 0, "a1"), (20.0, "system", 1, "b1"),
                   (30.0, "system", 1, "b2"), (40.0, "system", 0, "a2")])
        with mock.patch.object(LN, "_rebuild_prints", lambda d, doc: None):
            LN.add_rule(self.d, "voice", "Speaker 1", "Alex Example", [(10.0, 13.0), (40.0, 43.0)])
            LN.confirm(self.d, "Alex Example", [(10.0, 13.0), (40.0, 43.0)])
        LN._cache.clear()
        out = cues.live_score(self.d)
        far = [p for p in out["people"] if p["kind"] != "you"]
        self.assertEqual([p["name"] for p in far], ["Speaker 2", "Alex Example"])
        self.assertEqual(far[1]["order"], 40.0)
        self.assertEqual(far[1]["kind"], "named by you")
        self.assertTrue(far[1]["confirmed"])

    def test_append_live_without_features_skips_decode(self):
        wav = self.tmp / "chunk.wav"
        with mock.patch.object(cues, "decode", side_effect=AssertionError("decoded")), \
             mock.patch.object(cues, "line_features", side_effect=AssertionError("measured")):
            n = cues.append_live(self.d, "mic", 0.0, [{"start": 0.0, "end": 2.0, "text": "hello"}],
                                 wav, "Owner Person", features=False)
        self.assertEqual(n, 1)
        row = json.loads((self.d / cues.LIVE_FILE).read_text().splitlines()[0])
        self.assertEqual(row["text"], "hello")
        self.assertNotIn("E", row)


if __name__ == "__main__":
    unittest.main()

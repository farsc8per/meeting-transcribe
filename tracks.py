"""Is the same audio on both tracks? Measured from the audio, before anything is transcribed.

mt records two tracks on the assumption that they hear different things: the mic hears
the user, the system track hears the call. That holds at a desk on headphones. It breaks in
two ways, and each leaves a fingerprint that needs no transcript and no model:

- **Speakers, not headphones.** The mic hears the far side coming out of the laptop, so
  whenever the system track is talking, the mic carries a copy of it.
- **A room on Teams.** The room mic (the OSM09 puck) hears everyone at the table, and the
  laptop's Teams audio carries the room too, through the room's own Teams microphones.
  Whenever the mic is talking, the system track carries a copy of it. The whole meeting is
  on both tracks, and transcribing both writes every line twice.

The test, per 3 s window where one track is clearly talking: compare that track's
spectrum, frame by frame, with the other track's up to 2.5 s either side. Loudness is
removed per frame, so it compares *what* is heard, not how loud. Then it asks whether the
copy is loud enough to matter.

Measured Oct 18 2026 on 47 real two-track calls (headphones, desk mics) and on copies
injected into a clean call (laptop speakers; room + Teams):
- headphones leak a faint copy of the far side into the mic on most calls (spectral match
  in up to 94% of windows) but 39-50 dB below the original — harmless, and ignored here;
  injected laptop-speaker copies sit ~21 dB down. Hence ATTEN_DB.
- six calls where the user barely spoke fooled a level test made against their own track (its
  "normal speech" was really leakage). Hence the copy is judged against the ORIGINAL, and
  a track must carry real speech (TALK_DBFS) before its voices are looked for elsewhere.
- the two tracks drift against each other by up to ~0.8 s over a call (USB mic clock vs
  system clock), so the lag is reported but never used to decide anything. Teams adds its
  own 0.2-0.5 s on top, and an Oct 18 room test (a meeting video on two laptops, one
  near the puck) sat at a steady 1.25 s, which a +/-0.8 s search saw only 22% of. Hence
  +/-2.5 s.
- that same test had the user talking over the video: their voice was on the mic and nowhere
  on the call audio. "Use the Teams track only" would have thrown it away. So the Teams
  track is recommended only when it COVERS the mic (COVER): nearly every window where the
  mic is talking has a copy on the call audio. Copies both ways without that is "overlap":
  much is doubled, but the mic holds something the call does not. The rule is symmetric —
  keep whichever track covers the other. On that test the mic covered the call audio
  completely (100%: the video reached the puck, plus the user), so the room mic alone was the
  whole meeting, once.
Result with the rules below: 47 real calls -> 47 "separate" (far-side-on-mic loud share
0-7% against SHARE 20%); injected speakers -> "speakers" (60%); injected room + Teams ->
"same_room" (82% / 100%). The room direction is calibrated only on injected copies; a
real Teams Room rehearsal should confirm NEAR_DB and SHARE.

Never in the caller's way: process() treats any failure as "no verdict".
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import numpy as np

SR, HOP, NFFT = 16000, 160, 512           # 10 ms frames
WIN, STEP, MAXLAG = 300, 150, 250         # 3 s windows, 1.5 s apart, +/-2.5 s
MAX_WINDOWS = 400                         # per direction; bounds the cost of a long call
GATE_DB = 6.5                             # "talking" = within 6.5 dB of the track's p95 level
TALK_DBFS = -45.0                         # ...and real speech: a silent track's p95 is noise
MATCH = 0.30                              # spectral-shape correlation that counts as a copy
ATTEN_DB = 25.0                           # far side -> mic: counts when < 25 dB below original
NEAR_DB = 20.0                            # mic -> call audio: Teams levels the room's audio, so
                                          #   a real copy sits near the call's normal level
SHARE = 0.20                              # share of windows that makes a direction real
COVER = 0.85                              # mic windows with a copy on the call audio before
                                          #   the call audio alone can stand in for the mic
EDGES = np.geomspace(200, 4000, 25)       # 24 log-spaced speech bands
VERSION = 2                               # bump when a verdict can change; caches recompute
                                          #   (2: +/-2.5 s search, COVER, "overlap")


def _load(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
                          "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768


def _bands(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-frame band shape (unit vectors, loudness removed) and level in dBFS."""
    f = np.fft.rfftfreq(NFFT, 1 / SR)
    masks = [(f >= lo) & (f < hi) for lo, hi in zip(EDGES[:-1], EDGES[1:])]
    win = np.hanning(NFFT).astype(np.float32)
    n = max(0, (len(x) - NFFT) // HOP)
    Z = np.empty((n, len(masks)), np.float32)
    E = np.empty(n, np.float32)
    chunk = 6000                                       # 60 s of frames at a time
    for c0 in range(0, n, chunk):
        c1 = min(n, c0 + chunk)
        idx = np.arange(NFFT)[None, :] + HOP * np.arange(c0, c1)[:, None]
        S = np.abs(np.fft.rfft(x[idx] * win, axis=1)) ** 2
        L = np.log(np.stack([S[:, m].sum(1) for m in masks], 1) + 1e-12)
        z = L - L.mean(1)[:, None]
        Z[c0:c1] = z / (np.linalg.norm(z, axis=1, keepdims=True) + 1e-9)
        E[c0:c1] = 10 * np.log10((x[idx] ** 2).mean(1) + 1e-12)
    return Z, E


def _direction(Zsrc, Esrc, Zdst, Edst, rule: str) -> dict:
    """Windows where SRC is clearly talking: is a copy of it on DST, loud enough to matter?

    rule "atten": the copy counts when within ATTEN_DB of the original (far side -> mic).
    rule "near":  the copy counts when within NEAR_DB of DST's own normal speech (mic ->
                  call audio; the two run at unrelated digital gains, so attenuation is not
                  meaningful in this direction).
    """
    n = min(len(Zsrc), len(Zdst))
    if n < WIN + 2 * MAXLAG:
        return {"windows": 0}
    talk = max(float(np.percentile(Esrc[:n], 95)) - GATE_DB, TALK_DBFS)
    loud_dst = float(np.percentile(Edst[:n], 95))
    starts = [i for i in range(MAXLAG, n - WIN - MAXLAG, STEP)
              if (Esrc[i:i + WIN] > talk).mean() >= 0.15]
    if len(starts) > MAX_WINDOWS:
        starts = [starts[k] for k in np.linspace(0, len(starts) - 1, MAX_WINDOWS).astype(int)]
    scores, lags, rel = [], [], []
    shifts = np.arange(-MAXLAG, MAXLAG + 1, 2)
    for i in starts:
        act = Esrc[i:i + WIN] > talk
        a = Zsrc[i:i + WIN][act]
        rows = i + np.flatnonzero(act)
        # every shift at once: (shifts, frames, bands) . (frames, bands) -> (shifts,)
        c = np.einsum("lfb,fb->l", Zdst[rows[None, :] + shifts[:, None]], a) / len(rows)
        k = int(c.argmax())
        best, lag = float(c[k]), int(shifts[k])
        copy = float(np.median(Edst[i + lag:i + lag + WIN][act]))
        ref = float(np.median(Esrc[i:i + WIN][act])) if rule == "atten" else loud_dst
        scores.append(best)
        lags.append(lag * 10)
        rel.append(copy - ref)
    if not scores:
        return {"windows": 0}
    s, g, r = np.array(scores), np.array(lags), np.array(rel)
    hit = s >= MATCH
    loud = hit & (r > -(ATTEN_DB if rule == "atten" else NEAR_DB))
    return {"windows": int(len(s)),
            "match_share": round(float(hit.mean()), 3),
            "loud_share": round(float(loud.mean()), 3),
            "copy_db": round(float(np.median(r[hit])), 1) if hit.any() else None,
            "lag_ms": int(np.median(g[loud])) if loud.any() else None}


def verdict(far: dict, room: dict) -> dict:
    fl = far.get("loud_share", 0) or 0
    fm = far.get("match_share", 0) or 0
    rl = room.get("loud_share", 0) or 0
    rm = room.get("match_share", 0) or 0
    if not far.get("windows"):
        return {"verdict": "system_silent", "recommend": None,
                "headline": "Nothing was talking on the system track."}
    # In a room, everything the call audio carries also reaches the puck directly, so the
    # far-side-on-mic test is ALWAYS high there. Requiring it is what keeps desk calls out:
    # on 47 of them the room direction alone reached 25% (few windows, the user barely
    # talking, chance spectral matches), while far-side-on-mic never passed 7%.
    if fl >= SHARE and rl >= SHARE and rm >= COVER:
        return {"verdict": "same_room", "recommend": "system_only",
                "headline": (f"The same room audio is on both tracks: the call audio carries "
                             f"your mic's voices in {max(rl, rm):.0%} of the windows checked. "
                             f"Transcribing both writes lines twice; the system (Teams) "
                             f"track alone carries everyone.")}
    if fl >= SHARE and rm >= SHARE and fm >= COVER:
        return {"verdict": "overlap", "recommend": "mic_only",
                "headline": (f"Much of the audio is on both tracks, so lines may appear "
                             f"twice. Your mic heard {fm:.0%} of what the call audio carried, "
                             f"plus {1 - rm:.0%} of its own that the call never had (your "
                             f"own voice, side conversation): the room mic alone is the "
                             f"whole meeting, once.")}
    if fl >= SHARE and rm >= SHARE:
        return {"verdict": "overlap", "recommend": None,
                "headline": (f"Much of the audio is on both tracks, so lines may appear "
                             f"twice — but {1 - rm:.0%} of what your mic heard is not on the "
                             f"call audio (your own voice, side conversation), so keep the "
                             f"mic track.")}
    if fl >= SHARE:
        return {"verdict": "speakers", "recommend": None,
                "headline": (f"The far side is leaking into your mic ({fl:.0%} of the windows "
                             f"checked): speakers rather than headphones. Repeated lines are "
                             f"dropped from your track automatically.")}
    return {"verdict": "separate", "recommend": None,
            "headline": "The two tracks hear different things, as they should."}


def analyze(mic: Path, system: Path) -> dict:
    t0 = dt.datetime.now(dt.timezone.utc)
    m, s = _load(mic), _load(system)
    n = min(len(m), len(s))
    Zm, Em = _bands(m[:n])
    Zs, Es = _bands(s[:n])
    far = _direction(Zs, Es, Zm, Em, "atten")   # system talking -> copy on the mic
    room = _direction(Zm, Em, Zs, Es, "near")   # mic talking    -> copy on the call audio
    out = {"version": VERSION, "analyzed_at": t0.isoformat(timespec="seconds"),
           "far_end_on_mic": far, "mic_on_system": room}
    out.update(verdict(far, room))
    out["seconds"] = round((dt.datetime.now(dt.timezone.utc) - t0).total_seconds(), 1)
    return out


def write(session: Path, mic: Path, system: Path) -> dict:
    r = analyze(mic, system)
    (session / "tracks.json").write_text(json.dumps(r, indent=2) + "\n")
    return r


def read(session: Path) -> dict | None:
    try:
        return json.loads((session / "tracks.json").read_text())
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    d = Path(sys.argv[1])
    pick = lambda k: next(d / f"{k}{e}" for e in (".flac", ".wav") if (d / f"{k}{e}").exists())
    print(json.dumps(analyze(pick("mic"), pick("system")), indent=2))

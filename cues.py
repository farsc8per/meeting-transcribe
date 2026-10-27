"""Vocal cues — how someone's delivery moves against THEIR OWN normal. See REFERENCE.md.

Two halves, deliberately apart:

  extract()  heavy, once per meeting. Decodes the archived tracks and measures every
             transcript line: pitch, pitch range, pace, loudness, pauses. Writes
             <session>/cues.json. It records NO names — only timeline positions.
  score()    light, every time the page asks. Resolves who said each line from the
             CURRENT speakers.json (confirmed speakers, per-line overrides and strong
             voiceprint matches — strong_match()),
             builds each person's baseline from their other analyzed meetings, and turns
             the measurements into the Energy and Friction gauges.

So confirming a speaker next week, or retuning a threshold, never needs a re-run.

What it can say: "faster and higher than his normal". What it cannot: why. Voice shows
activation, not whether it is good or bad — the gauges' words describe delivery, never
feelings or honesty. Pitch is tracked with a small numpy YIN rather than librosa: librosa
would pull scipy + numba + scikit-learn into every install for one function.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np

import mt as MT

FILE = "cues.json"
LIVE_FILE = "cues_live.jsonl"       # rough readings written during the call
REQUEST_FILE = "cues_requested.json"  # the record-time checkbox: measure after the pass too
VERSION = 1
SR = 16000

# ── tuning ────────────────────────────────────────────────────────────────────
# First-draft values. Every one of these is expected to move in step 4 ("my read"),
# which is why they can be overridden from config.json → "cues" without a code change.
DEFAULTS = {
    "bands": [-0.8, 0.8, 1.6],      # z cut points: below | normal | elevated | well above
    "window_sec": 45,               # smoothing: a reading is the last ~45s of that person
    "history_min_lines": 40,        # lines from OTHER meetings before history is trusted
    "call_min_lines": 12,           # otherwise: this call alone, if they said this much
    "min_speech_sec": 1.0,          # a line shorter than this is too thin to measure
    "min_window_speech_sec": 4.0,   # a reading needs this much of them inside its window;
                                    # one opening line read "keyed up (+4.97)" on its own
}
ENERGY_WORDS = ["subdued", "steady", "animated", "keyed up"]
FRICTION_WORDS = ["easy", "steady", "pressing", "tense"]

# Smallest spread a baseline may claim. Without a floor, someone who spoke in a perfectly
# even voice for twelve lines gets a near-zero spread and every later wobble reads "red".
SD_FLOOR = {"st": 0.5, "rng": 0.6, "rate": 0.35, "db": 1.5, "lat": 0.25, "pause": 0.04}
# Energy weights. Loudness counts for little on the far end: Zoom/Teams auto-gain flatten it.
E_WEIGHTS = {"st": 1.0, "rng": 0.7, "rate": 0.8}
DB_WEIGHT = {"mic": 0.8, "system": 0.3}
INTERRUPT_BUMP = 1.5            # starting over someone adds this much Friction outright
OVERLAP_SEC = 0.3               # less overlap than this is ordinary turn-taking

# YIN pitch tracker: 20 ms integration window, 10 ms hop, 70–400 Hz. The floor is 70, not
# 60: on the user's USB headset the 60 Hz mains hum sat 36 dB above its neighbours on the
# Oct 5 CAB call, and with a 60 Hz floor YIN locked onto it in every quiet line.
HOP, WIN = 160, 320
TAU_MIN, TAU_MAX = SR // 400, SR // 70
FRAME = WIN + TAU_MAX
NFFT = 1024
YIN_THRESHOLD = 0.15
# Loudness gates are RELATIVE to the line, with a low absolute floor. That headset puts
# speech near -47 dBFS; a fixed -50 dB gate threw away three quarters of it.
FLOOR_RMS = 0.001               # -60 dBFS: below this nothing counts, however quiet the mic
VOICE_DROP_DB = 20              # voiced frames are within 20 dB of the line's loud part
ACTIVE_DROP_DB = 25             # speaking (vs pausing) frames, within 25 dB
# Narrow notches at mains frequencies and harmonics (60 Hz US, 50 Hz EU), for analysis
# only. A few Hz wide, so speech harmonics barely notice; hum loses its grip on YIN.
MAINS_HZ = (50, 60, 100, 120, 150, 180, 240)


# The field guide for the Vocal cues panel and the Room cards — shown behind a "Field
# guide" toggle, served with /api/cues and /api/cues/live so the pages never hardcode it.
# Same house style as the contacts and tracking guides: what each thing MEANS for you and
# what it can't tell you, checked against this code on Oct 5 2026.
GUIDE = [
    ("Energy",
     "Pitch, pitch range, pace and loudness, compared with THAT person's own normal and "
     "smoothed over about 45 s of their speech. Loudness counts for little on the far "
     "end because Zoom/Teams level it out. 'Keyed up' means well above their normal — "
     "excited and upset look the same here; it never says why."),
    ("Friction",
     "The gap before they speak when the floor changes hands, pauses inside their lines, "
     "and a fixed bump each time they start over someone. 'Tense' means well above their "
     "normal. A long pause can be hesitation or someone typing — it can't tell which."),
    ("Colours",
     "Distance from that person's normal, not good or bad: blue below, green normal, "
     "amber elevated, red well above. Cut points are config.json → cues → bands "
     "(-0.8 / 0.8 / 1.6). Grey means fewer than 4 s of their speech nearby, or no "
     "baseline yet — it will not guess."),
    ("Baseline dots",
     "●●● measured against 3+ of their other analyzed meetings · ●●○ against 1–2 · ●○○ "
     "against this call only, so a call that was tense throughout reads 'steady' · ○○○ "
     "too little speech. Only meetings you chose to analyze ever feed a baseline."),
    ("Who gets gauges",
     "Confirmed speakers, lines you reassigned with 'not them?', and strong voiceprint "
     "matches (≥ 0.85, or ≥ 0.75 well clear of the next print — tagged 'matched'). A cue on "
     "the wrong person is worse than none, so weaker matches wait for Confirm; a wrong "
     "strong match is undone with ✗ Not under Voices. Nothing is re-measured."),
    ("Talk-over strip",
     "Ticks where someone started over someone, from transcript timestamps. Rougher than "
     "the Conversation section above, whose talk-over counts are checked against the "
     "audio (timestamps alone were 29% right on one noisy call)."),
    ("Re-measure / Clear",
     "Re-measure redoes the measurements and the laughter scan, about 45 s a meeting. "
     "Clear deletes them (cues.json, laughs.json), which opts the meeting back out and "
     "takes it out of everyone's baseline. Your Best Saying crown is kept."),
    ("Your own readings",
     "Your headset carries 60 Hz mains hum and records quietly, so your gauges are the "
     "least reliable on the page. Plugging it in directly instead of through the dock, "
     "or raising the input level, helps."),
]
ROOM_GUIDE = [
    ("you · mic", "Your microphone track — always you."),
    ("voice match 0.xx",
     "A far-end chunk whose voiceprint matched at or above the auto-assign score (0.62). "
     "A live guess, not a confirmation; the review page only names confirmed speakers."),
    ("Them · far end, blended",
     "Far-end speech that matched nobody, possibly several people mixed together, scored "
     "only against this call. Never wins an award. ✎ Name this voice names its last "
     "minute of speech."),
    ("Speaker N · unnamed voice",
     "Far-end speech no voiceprint claimed, split live by voice (a group needs 2+ "
     "lines). The number never changes during the call. 'maybe X?' is the closest "
     "voiceprint — ✓ It's X, or ✎ Name to pick anyone. One name covers everything that "
     "voice says from then on."),
    ("Earlier voices",
     "Cards for anyone not heard in the last 5 minutes fold away here; you and anyone you "
     "named always stay up."),
    ("maybe X · near miss",
     "Recordings from before voice separation only: a voice that came close to X's "
     "voiceprint at least 3 times. ✓ It's X or ✎ Someone else."),
    ("What they said",
     "Each card shows the last lines that voice said, so you can name someone from what "
     "they're talking about. The picker repeats them while you choose."),
    ("Naming live",
     "Your pick relabels that card's chunks and learns the voice for the rest of THIS "
     "call: later chunks that sound like it (0.62+) get the name, in the Room and the "
     "live transcript. It's never a done deal: after the full pass the review page opens "
     "with those voices filled in as 'named live by you — strong/possible match', and "
     "you confirm or change each one. Nothing is enrolled, and the people map is never "
     "touched."),
    ("named by you",
     "A voice you named during this call. ✎ Rename moves everything you gave that name to "
     "another; ✕ Unname takes it off and the voice goes back to its Speaker number. Undo "
     "last takes back your latest pick."),
    ("Lag", "Readings trail the conversation by roughly 5–30 s: each chunk is measured "
            "after it has been transcribed."),
    ("Awards so far",
     "For the whole call so far, not this minute — muting now doesn't undo earlier talk. "
     "Talk-overs count only when both sides were audibly talking (checked against the "
     "audio, live as after the call). Still rough: the review page settles them."),
]


def cfg() -> dict:
    c = dict(DEFAULTS)
    try:
        c.update(MT.config().get("cues") or {})
    except Exception:
        pass
    return c


# ── extraction ────────────────────────────────────────────────────────────────

def decode(src: Path) -> np.ndarray:
    """The track as the recorder wrote it, 16 kHz mono — no gain, so loudness stays real.
    Same 80 Hz high-pass as the transcription path, plus the mains notches."""
    notch = ",".join(f"bandreject=f={f}:width_type=h:width=4" for f in MAINS_HZ)
    r = subprocess.run(
        [MT.need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-af", f"highpass=f={MT.HIGHPASS_HZ}:poles=2,{notch}",
         "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        check=True, capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def track_file(d: Path, track: str) -> Path | None:
    for ext in (".flac", ".wav"):
        f = d / f"{track}{ext}"
        if f.exists():
            return f
    return None


def f0_track(x: np.ndarray, gate: float = FLOOR_RMS) -> np.ndarray:
    """Fundamental frequency per 10 ms frame (Hz), NaN where unvoiced. YIN (de Cheveigné &
    Kawahara 2002): difference function via FFT, cumulative-mean normalisation, first dip
    under the threshold, then walk down to that dip's floor."""
    n = 1 + (len(x) - FRAME) // HOP if len(x) >= FRAME else 0
    out = np.full(max(n, 0), np.nan)
    if n <= 0:
        return out
    taus = np.arange(1, TAU_MAX + 1)
    for b0 in range(0, n, 2048):
        b1 = min(n, b0 + 2048)
        idx = (np.arange(b0, b1) * HOP)[:, None] + np.arange(FRAME)[None, :]
        fr = x[idx].astype(np.float64)
        a = fr[:, :WIN]
        r = np.fft.irfft(np.conj(np.fft.rfft(a, NFFT)) * np.fft.rfft(fr, NFFT), NFFT)
        r = r[:, :TAU_MAX + 1]
        e0 = (a * a).sum(1)
        cs = np.concatenate([np.zeros((len(fr), 1)), np.cumsum(fr * fr, axis=1)], axis=1)
        etau = cs[:, WIN:WIN + TAU_MAX + 1] - cs[:, :TAU_MAX + 1]
        diff = np.maximum(e0[:, None] + etau - 2 * r, 0)[:, 1:]          # tau = 1..TAU_MAX
        cmn = diff * taus / np.maximum(np.cumsum(diff, axis=1), 1e-12)
        sub = cmn[:, TAU_MIN - 1:]                                         # tau = TAU_MIN..
        below = sub < YIN_THRESHOLD
        has = below.any(1)
        pos = np.argmax(below, axis=1)
        rows = np.arange(len(sub))
        last = sub.shape[1] - 1
        for _ in range(40):                                                # into the dip
            nxt = np.minimum(pos + 1, last)
            move = has & (sub[rows, nxt] < sub[rows, pos]) & (pos < last)
            if not move.any():
                break
            pos = pos + move
        f0 = SR / (pos + TAU_MIN).astype(np.float64)
        voiced = has & (np.sqrt(e0 / WIN) >= gate)
        out[b0:b1] = np.where(voiced, f0, np.nan)
    return out


_VOWELS = re.compile(r"[aeiouy]+")


def syllables(text: str) -> int:
    """Rough English syllable count from the transcript. Pace from the words actually said
    is steadier than counting acoustic onsets, and the transcript is already there."""
    n = 0
    for w in re.findall(r"[a-z']+", text.lower()):
        w = w.replace("'", "")
        if not w:
            continue
        k = len(_VOWELS.findall(w))
        if k > 1 and w.endswith("e") and not w.endswith(("le", "ee")):
            k -= 1
        n += max(1, k)
    return n + 2 * len(re.findall(r"\d+", text))


def line_features(x: np.ndarray, start: float, end: float, text: str) -> dict:
    seg = x[int(start * SR):int(end * SR)]
    dur = max(0.0, end - start)
    out = {"dur": round(dur, 2)}
    if seg.size < int(0.4 * SR):
        return out
    rms = MT.frame_rms(seg, frame=0.03)
    p90 = float(np.percentile(rms, 90))
    active = rms >= max(FLOOR_RMS, p90 * 10 ** (-ACTIVE_DROP_DB / 20))
    # Pauses are the quiet runs INSIDE the line, at least 250 ms; the padding whisper
    # leaves at either end is not a pause.
    pause = 0.0
    on = np.flatnonzero(active)
    if on.size:
        inner = active[on[0]:on[-1] + 1]
        run = 0
        for v in inner:
            if v:
                if run * 0.03 >= 0.25:
                    pause += run * 0.03
                run = 0
            else:
                run += 1
        speech = (on[-1] - on[0] + 1) * 0.03 - pause
    else:
        speech = 0.0
    syl = syllables(text)
    out.update({"speech": round(speech, 2), "pause": round(pause, 2),
                "db": round(20 * np.log10(max(p90, 1e-9)), 1), "syl": syl})
    if speech >= 0.3:
        out["rate"] = round(syl / speech, 2)
    f0 = f0_track(seg, gate=max(FLOOR_RMS, p90 * 10 ** (-VOICE_DROP_DB / 20)))
    v = f0[~np.isnan(f0)]
    out["voiced"] = round(len(v) / len(f0), 2) if len(f0) else 0.0
    if len(v) >= 10:
        st = 12 * np.log2(v / 100.0)                 # semitones re 100 Hz
        out["f0"] = round(float(np.median(v)), 1)
        out["st"] = round(float(np.median(st)), 2)
        out["rng"] = round(float(np.percentile(st, 90) - np.percentile(st, 10)), 2)
    return out


def fingerprint(timeline: list[dict]) -> str:
    """Which transcript the measurements belong to. Reassigning a line does not change it;
    re-running the transcription pass does, and then the cues are stale."""
    key = [(round(e["start"], 2), round(e["end"], 2), e.get("track", "system"))
           for e in timeline]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


def extract(d: Path, progress=None) -> dict:
    doc = json.loads((d / "transcript.json").read_text())
    tl = doc.get("timeline", [])
    audio = {}
    for t in sorted({e.get("track", "system") for e in tl}):
        f = track_file(d, t)
        if f is not None:
            audio[t] = decode(f)
    lines, n = [], len(tl)
    for i, e in enumerate(tl):
        t = e.get("track", "system")
        row = {"i": i, "key": e["speaker_key"], "track": t,
               "s": round(e["start"], 2), "e": round(e["end"], 2)}
        if t in audio:
            row.update(line_features(audio[t], e["start"], e["end"], e.get("text", "")))
        lines.append(row)
        if progress and (i % 25 == 0 or i == n - 1):
            progress(i + 1, n)
    out = {"version": VERSION,
           "analyzed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "fingerprint": fingerprint(tl), "tracks": sorted(audio), "lines": lines}
    (d / FILE).write_text(json.dumps(out, separators=(",", ":")) + "\n")
    return out


def clear(d: Path) -> bool:
    """Opt a meeting back out: the measurements go, including the laughter scan. The user's
    own Best Saying crown (awards.json) is their input, not a measurement, and stays."""
    gone = False
    for name in (FILE, "laughs.json"):
        f = d / name
        if f.exists():
            f.unlink()
            gone = True
    return gone


# ── scoring ───────────────────────────────────────────────────────────────────

def _load(d: Path):
    """(cues, timeline, speakers map) or None. speakers.json is the live copy of names —
    transcript.json only catches up on relabel."""
    try:
        cues = json.loads((d / FILE).read_text())
        tl = json.loads((d / "transcript.json").read_text()).get("timeline", [])
        names = json.loads((d / "speakers.json").read_text()).get("map", {})
    except (OSError, ValueError):
        return None
    return cues, tl, names


# A voiceprint match this strong counts as that person for cues and awards without a
# Confirm click. Measured Oct 7 2026 over every confirmed far-end group on disk: ≥ 0.85 was
# right 179 of 179; 0.75–0.85 with a 0.20 lead over the next print, 8 of 8; below 0.75,
# 2 of 5 (Alex Example read as Sam Sample at 0.62 and 0.64). Same lines as the live-name
# post-pass (livenames.POST_STRONG / POST_CONFIRM / POST_MARGIN).
STRONG = 0.85
STRONG_WITH_LEAD = 0.75
LEAD = 0.20


def strong_match(v: dict) -> bool:
    """A group named by a clear voiceprint match (or a strong live name), not confirmed.
    Never for a mixed or impure group, nor a name that came from the screen or roster."""
    if v.get("confidence") == "verified" or v.get("mixed"):
        return False
    ss = v.get("self_similarity")
    if ss is not None and ss < float(MT.config()["min_self_similarity"]):
        return False
    if v.get("live_strong"):
        return True
    if not str(v.get("source", "")).startswith("voiceprint"):
        return False
    sc, ru = v.get("score"), v.get("runner_up")
    if sc is None:
        return False
    return sc >= STRONG or (sc >= STRONG_WITH_LEAD and ru is not None and sc - ru >= LEAD)


def who(e: dict, names: dict) -> tuple[str | None, str]:
    """(name, why). A name only for a CONFIRMED attribution — the user's per-line override, a
    speaker marked verified — or a strong voiceprint match (strong_match), which they can
    still overturn with "Not them". A cue on the wrong person is worse than none, which
    is why a merely-good match (0.62–0.85) still waits for a Confirm."""
    if e.get("override"):
        return e["override"], "assigned by line"
    v = names.get(e["speaker_key"]) or {}
    if v.get("mixed"):
        return None, "several voices in one cluster"
    if v.get("confidence") == "verified":
        return v.get("name") or e["speaker_key"], "confirmed"
    if strong_match(v):
        return v.get("name") or e["speaker_key"], "matched"
    return None, "speaker not confirmed yet"


def _conversation(tl: list[dict]) -> list[dict]:
    """Turn-taking per line, from timing alone: did this line start over someone, and how
    long was the gap before it when the floor changed hands."""
    out = []
    for i, e in enumerate(tl):
        r = {"interrupt": False, "lat": None}
        k = e["speaker_key"]
        for j in range(max(0, i - 25), i):
            o = tl[j]
            if o["speaker_key"] != k and o["start"] < e["start"] \
                    and o["end"] - e["start"] >= OVERLAP_SEC:
                r["interrupt"] = True
                break
        if i and tl[i - 1]["speaker_key"] != k:
            r["lat"] = float(min(5.0, max(-OVERLAP_SEC, e["start"] - tl[i - 1]["end"])))
        out.append(r)
    return out


def _rows(d: Path, c: dict) -> list[dict] | None:
    """Every measured line of one session, joined to its confirmed speaker and turn-taking.
    None when the measurements belong to an older transcript."""
    got = _load(d)
    if got is None:
        return None
    cues, tl, names = got
    if cues.get("fingerprint") != fingerprint(tl) or len(cues.get("lines", [])) != len(tl):
        return None
    conv = _conversation(tl)
    rows = []
    for f, e, cv in zip(cues["lines"], tl, conv):
        name, why = who(e, names)
        r = dict(f)
        r.update(cv)
        r["name"], r["why"], r["text"] = name, why, e.get("text", "")
        if r.get("speech") is not None and r.get("dur"):
            r["pfrac"] = r["pause"] / r["dur"] if r["dur"] > 0 else None
        r["ok"] = (r.get("speech") or 0) >= c["min_speech_sec"]
        rows.append(r)
    return rows


_pool_cache: dict = {}


def _session_rows(d: Path, c: dict):
    stamp = tuple((d / f).stat().st_mtime if (d / f).exists() else 0
                  for f in (FILE, "transcript.json", "speakers.json"))
    hit = _pool_cache.get(str(d))
    if hit and hit[0] == stamp:
        return hit[1]
    rows = _rows(d, c)
    _pool_cache[str(d)] = (stamp, rows)
    return rows


def history(exclude: Path, c: dict) -> dict:
    """name -> [(session, row)] from every OTHER analyzed, current session. Only meetings
    the user chose to analyze contribute: the archive is opt-in too."""
    pool: dict = {}
    for d in MT.session_dirs():
        if d.resolve() == exclude.resolve() or not (d / FILE).exists():
            continue
        rows = _session_rows(d, c)
        for r in rows or []:
            if r["name"] and r["ok"]:
                pool.setdefault(r["name"], []).append((d.name, r))
    return pool


def _stats(vals: list, floor: float, robust: bool = True):
    """(centre, spread). Median/MAD for the acoustic features, where a stray octave error
    must not drag the centre. Mean/SD for pauses and gaps: those are mostly exactly zero,
    and a MAD of zero made every ordinary pause read as five spreads out (measured on the
    Sep 23 sprint-planning call: Friction 'tense' for 92% of one person's speech)."""
    v = np.array([x for x in vals if x is not None], dtype=float)
    if v.size < 3:
        return None
    if robust:
        med = float(np.median(v))
        return med, max(1.4826 * float(np.median(np.abs(v - med))), floor)
    return float(v.mean()), max(float(v.std()), floor)


def _baseline(rows: list[dict]) -> dict:
    b = {k: _stats([r.get(k) for r in rows], SD_FLOOR[k]) for k in ("st", "rng", "rate", "db")}
    b["lat"] = _stats([r.get("lat") for r in rows], SD_FLOOR["lat"], robust=False)
    b["pause"] = _stats([r.get("pfrac") for r in rows], SD_FLOOR["pause"], robust=False)
    return b


def _z(v, st):
    return None if v is None or st is None else (v - st[0]) / st[1]


def _energy_raw(r: dict, b: dict):
    num = den = 0.0
    for k, w in E_WEIGHTS.items():
        z = _z(r.get(k), b[k])
        if z is not None:
            num += w * z
            den += w
    z = _z(r.get("db"), b["db"])
    if z is not None:
        w = DB_WEIGHT.get(r.get("track"), 0.3)
        num += w * z
        den += w
    return num / den if den else None


def _friction_raw(r: dict, b: dict):
    num = den = 0.0
    z = _z(r.get("lat"), b["lat"])
    if z is not None:
        num += z
        den += 1.0
    z = _z(r.get("pfrac"), b["pause"])
    if z is not None:
        num += 0.6 * z
        den += 0.6
    base = num / den if den else 0.0
    return base + (INTERRUPT_BUMP if r.get("interrupt") else 0.0)


def _series(rows: list[dict], b: dict, half: float, min_speech: float = 0.0) -> list[tuple]:
    """(row, smoothed Energy, smoothed Friction) for one person in ONE meeting. Each
    reading is the speech-weighted mean of their lines within ±half seconds, or None when
    there is less than min_speech seconds of them to go on."""
    raw = [(r, _energy_raw(r, b), _friction_raw(r, b)) for r in rows]
    out = []
    for r, _, _ in raw:
        mid = (r["s"] + r["e"]) / 2
        near = [x for x in raw if abs((x[0]["s"] + x[0]["e"]) / 2 - mid) <= half]
        if sum(x[0].get("speech") or 0 for x in near) < min_speech:
            out.append((r, None, None))
            continue
        def smooth(j):
            ws = [(x[j], x[0].get("speech") or 0.1) for x in near if x[j] is not None]
            tot = sum(w for _, w in ws)
            return sum(v * w for v, w in ws) / tot if tot else None
        out.append((r, smooth(1), smooth(2)))
    return out


def _calibrate(vals: list):
    """Rescale by the spread the SMOOTHED readings show on the baseline itself. Calibrating
    on single lines and then colouring smoothed values (which are far flatter) left Energy
    pinned to 'steady' on the first real test, so the two must be the same kind of thing."""
    v = np.array([x for x in vals if x is not None], dtype=float)
    if v.size < 5:
        return 0.0, 1.0
    med = float(np.median(v))
    return med, max(1.4826 * float(np.median(np.abs(v - med))), 0.1)


def band(z, cuts) -> int | None:
    if z is None:
        return None
    return int(sum(z >= x for x in cuts))


def _score_person(name: str, rs: list[dict], pool: dict, c: dict,
                  use_history: bool = True) -> tuple[dict, list]:
    """One person's gauges: pick their baseline, smooth, calibrate, band. Shared by the
    post-call view and the live Room panel, so both read the same way. use_history=False
    for the blended live "Them", which is nobody in particular."""
    cuts = c["bands"]
    half = c["window_sec"] / 2.0
    mws = c["min_window_speech_sec"]
    interrupts = []
    mine = [r for r in rs if r["ok"]]
    hist = [r for _, r in pool.get(name, [])] if use_history else []
    if len(hist) >= c["history_min_lines"]:
        kind, ref = "history", hist
        sessions = len({s for s, _ in pool.get(name, [])})
        conf = 3 if sessions >= 3 else 2
    elif len(mine) >= c["call_min_lines"]:
        kind, ref, sessions, conf = "call", mine, 0, 1
    else:
        kind, ref, sessions, conf = "none", [], 0, 0
    b = _baseline(ref) if ref else None
    if b:
        if kind == "history":
            by_sess: dict = {}
            for sname, r in pool[name]:
                by_sess.setdefault(sname, []).append(r)
            base = [x for rs_ in by_sess.values()
                    for x in _series(sorted(rs_, key=lambda r: r["s"]), b, half, mws)]
        else:
            base = _series(mine, b, half, mws)
        ecal = _calibrate([e for _, e, _ in base])
        fcal = _calibrate([f for _, _, f in base])
        series = _series(mine, b, half, mws)
    else:
        series = [(r, None, None) for r in mine]
    points = []
    for r, E, F in series:
        E = None if E is None else (E - ecal[0]) / ecal[1]
        F = None if F is None else (F - fcal[0]) / fcal[1]
        pt = {"i": r["i"], "s": r["s"], "e": r["e"],
              "E": None if E is None else round(E, 2), "eb": band(E, cuts),
              "F": None if F is None else round(F, 2), "fb": band(F, cuts),
              "f0": r.get("f0"), "rate": r.get("rate"), "db": r.get("db"),
              "lat": None if r.get("lat") is None else round(r["lat"], 2),
              "interrupt": r["interrupt"]}
        if b:
            if r.get("st") is not None and b["st"]:
                pt["dst"] = round(r["st"] - b["st"][0], 1)
            if r.get("rate") is not None and b["rate"]:
                pt["drate"] = round(100 * (r["rate"] / b["rate"][0] - 1)) if b["rate"][0] else None
        points.append(pt)
        if r["interrupt"]:
            interrupts.append({"t": r["s"], "name": name})
    secs = sum(r.get("speech") or 0 for r in mine) or 1.0
    def share(key):
        s = [0.0] * 4
        for r, p in zip(mine, points):
            if p[key] is not None:
                s[p[key]] += (r.get("speech") or 0) / secs
        return [round(x, 3) for x in s]
    peak = max((p for p in points if p["E"] is not None), key=lambda p: p["E"], default=None)
    track = rs[0]["track"]
    person = {
        "name": name, "track": track, "lines": len(rs), "scored": len(mine),
        "speech": round(sum(r.get("speech") or 0 for r in rs)),
        "baseline": {"kind": kind, "sessions": sessions, "lines": len(ref)},
        "confidence": conf, "energy_share": share("eb"), "friction_share": share("fb"),
        "peak": None if not peak else {"t": peak["s"], "E": peak["E"]},
        "points": points}
    return person, interrupts


def score(d: Path) -> dict:
    c = cfg()
    if not (d / FILE).exists():
        return {"analyzed": False}
    rows = _rows(d, c)
    if rows is None:
        return {"analyzed": True, "stale": True,
                "why": "The transcript changed after these cues were measured — run the "
                       "analysis again."}
    meta = json.loads((d / FILE).read_text())
    pool = history(d, c)
    people, unscored = {}, {}
    for r in rows:
        if r["name"]:
            people.setdefault(r["name"], []).append(r)
        else:
            u = unscored.setdefault(r["key"], {"key": r["key"], "lines": 0, "why": r["why"]})
            u["lines"] += 1

    cuts = c["bands"]
    out_people, interrupts = [], []
    for name, rs in people.items():
        person, ints = _score_person(name, rs, pool, c)
        # Matched-not-confirmed is shown as such, so a gauge never looks more certain
        # than the name above it.
        person["matched"] = all(r["why"] == "matched" for r in rs)
        out_people.append(person)
        interrupts += ints
    out_people.sort(key=lambda p: (p["track"] == "mic", -p["speech"]))
    return {"analyzed": True, "stale": False, "analyzed_at": meta.get("analyzed_at"),
            "bands": cuts, "words": {"energy": ENERGY_WORDS, "friction": FRICTION_WORDS},
            "window_sec": c["window_sec"], "people": out_people,
            "unscored": sorted(unscored.values(), key=lambda u: -u["lines"]),
            "interrupts": sorted(interrupts, key=lambda x: x["t"])}


def summary_text(s: dict) -> str:
    """The CLI's view of score(): one block per person."""
    if not s.get("analyzed"):
        return "not analyzed"
    if s.get("stale"):
        return s["why"]
    L = []
    for p in s["people"]:
        b = p["baseline"]
        basis = {"history": f"their normal from {b['sessions']} other meeting(s), {b['lines']} lines",
                 "call": f"this call only ({b['lines']} lines) — no other analyzed meetings yet",
                 "none": "not enough speech to judge"}[b["kind"]]
        L.append(f"{p['name']}  ·  {p['scored']}/{p['lines']} lines measured  ·  {basis}")
        if b["kind"] != "none":
            for lab, key, words in (("Energy  ", "energy_share", ENERGY_WORDS),
                                    ("Friction", "friction_share", FRICTION_WORDS)):
                parts = [f"{w} {round(100 * x)}%" for w, x in zip(words, p[key]) if x >= 0.005]
                L.append(f"   {lab}  " + "  ".join(parts))
            if p["peak"] and p["peak"]["E"] is not None:
                L.append(f"   most animated around {MT.hhmmss(p['peak']['t'])}")
    for u in s["unscored"]:
        L.append(f"(not scored: {u['key']}, {u['lines']} lines — {u['why']})")
    if s["interrupts"]:
        L.append(f"talk-over moments: {len(s['interrupts'])}")
    return "\n".join(L)


# ── live (step 3) ─────────────────────────────────────────────────────────────
# During a call `mt live --cues` measures each chunk it has just transcribed, one row per
# whisper segment so a live reading has the same grain as a post-call line, and appends
# it here. Rough by nature: readings trail the talk by the chunking (~5–30 s), and the far
# end is one mixed track until a chunk matches a voiceprint.

def request(d: Path) -> None:
    """The record-time checkbox: this meeting is opted in, live and after the pass."""
    (d / REQUEST_FILE).write_text(json.dumps(
        {"requested_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "source": "record"}) + "\n")


def requested(d: Path) -> bool:
    return (d / REQUEST_FILE).exists()


def append_live(d: Path, kind: str, at: float, segs: list[dict], wav: Path,
                who: str | None, hint: str | None = None, match: float | None = None,
                people: list | None = None) -> int:
    """Measure the chunk just transcribed. `who` is a name only for the owner's mic or a
    voiceprint match at the auto-assign score; anything weaker stays the blended "Them"
    with the near-miss kept as a hint. With `people` (the stored voiceprints), a far-end
    line long enough to fingerprint is named from its own voice instead. Returns rows
    written."""
    x = decode(wav)                  # same high-pass + mains notches as the post pass
    raw = None
    if kind == "system":
        # One voice fingerprint per far-end segment, from the raw audio like every
        # voiceprint, so the Room can split "Them" into Speaker 1, 2, … (livenames.py).
        try:
            import livenames
            raw = MT.read_tail(wav, MT.wav_data_offset(wav), 0)
        except Exception:
            raw = None
    n = 0
    thr = MT.config()
    with open(d / LIVE_FILE, "a") as f:
        for sg in segs:
            s, e = float(sg["start"]), float(sg["end"])
            if e - s < 0.3:
                continue
            row = {"s": round(at + s, 2), "e": round(at + e, 2), "track": kind,
                   "who": who, "hint": hint,
                   "match": None if match is None else round(float(match), 3),
                   "text": sg.get("text", "").strip()}   # for Best Saying so far
            row.update(line_features(x, s, e, sg.get("text", "")))
            if raw is not None and e - s >= livenames.VOICE_MIN_SEC:
                try:
                    row["v"] = livenames.embed(raw[int(s * 16000):int(e * 16000)])
                except Exception:
                    pass
            if row.get("v") and people is not None:
                # The line's OWN voice decides, not the chunk's: one chunk can hold two
                # people. A single line is noisy (Oct 7 CAB: one contact's lines alone
                # had a median 0.57), so most firm names come later from the voice
                # group's average (livenames.group_names); this is the quick first guess.
                v = np.asarray(row["v"], np.float32)
                nm, sc = MT.match_voice(v, people) if people else (None, -1.0)
                lnm, lsc = livenames.match(d, v)        # a voice you named this call wins
                if lnm and lsc >= livenames.LIVE_MATCH and lsc >= sc:
                    row.update(who=lnm, hint=None, match=round(float(lsc), 3))
                elif sc >= thr["auto_assign_score"]:
                    row.update(who=nm, hint=None, match=round(float(sc), 3))
                elif sc >= thr["suggest_score"]:
                    row.update(who=None, hint=nm, match=round(float(sc), 3))
                else:
                    row.update(who=None, hint=None, match=round(float(sc), 3))
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
            n += 1
    return n


def _live_rows(d: Path, c: dict) -> list[dict]:
    raw = []
    try:
        for ln in (d / LIVE_FILE).read_text().splitlines():
            try:
                raw.append(json.loads(ln))
            except ValueError:
                pass                 # a line caught half-written
    except OSError:
        return []
    raw.sort(key=lambda r: r["s"])
    # Your live corrections (livenames.py) win over what the loop guessed at the time; then
    # whatever is still unnamed on the far end is split by voice into Speaker 1, 2, …
    by_voice = any(r.get("v") for r in raw)    # older recordings carry no fingerprints
    try:
        import livenames
        if by_voice:
            livenames.assign_voices(d, raw)      # numbering from what the loop saw: stable
        for r in raw:
            r["who"], r["hint"] = livenames.resolve(d, r["s"], r["e"], r.get("who"), r.get("hint"),
                                                    r.get("track", "system"))
        if by_voice:
            livenames.learned(d, raw)            # later speech in a voice you named
            # A voice's average names it once it has said enough; your corrections then
            # apply to those names too ("✎ actually Alex" on a group-named card).
            for r in livenames.group_names(d, raw):
                r["who"], r["hint"] = livenames.resolve(d, r["s"], r["e"], r.get("who"),
                                                        r.get("hint"), "system")
    except Exception:
        pass
    tl = [{"start": r["s"], "end": r["e"],
           "speaker_key": r.get("who") or r.get("voice") or f"them:{r.get('track')}"} for r in raw]
    rows = []
    for i, (r, cv) in enumerate(zip(raw, _conversation(tl))):
        r = dict(r)
        r.update(cv)
        r["i"] = i
        # Named → that name; else its voice group ("Speaker 2"); else, on recordings from
        # before voice fingerprints, a near-miss card ("maybe Sam Sample"); else Them.
        if r.get("who"):
            r["name"] = r["who"]
        elif by_voice:
            r["name"] = r.get("voice") or "Them"
        else:
            r["name"] = f"maybe {r['hint']}" if r.get("hint") else "Them"
        r.pop("v", None)             # 192 numbers per row; nothing downstream needs them
        if r.get("speech") is not None and r.get("dur"):
            r["pfrac"] = r["pause"] / r["dur"] if r["dur"] > 0 else None
        r["ok"] = (r.get("speech") or 0) >= c["min_speech_sec"]
        rows.append(r)
    return rows


def live_score(d: Path, owner: str | None = None) -> dict:
    """What the Room panel shows: per speaker, the reading now and the trend so far."""
    c = cfg()
    rows = _live_rows(d, c)
    base = {"requested": requested(d), "rows": len(rows), "bands": c["bands"],
            "words": {"energy": ENERGY_WORDS, "friction": FRICTION_WORDS},
            "window_sec": c["window_sec"], "people": []}
    if not rows:
        return base
    pool = history(d, c)
    now = max(r["e"] for r in rows)
    try:
        import livenames
        named_live = {r["to"] for r in livenames.cached(d)["rules"]}
    except Exception:
        named_live = set()
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["name"], []).append(r)
    # A one-off near-miss is noise, not a person to ask about: the Oct 6 Navan call grew
    # 15 cards, most of them "maybe X" with 1–4 segments. Under 3, it stays in "Them".
    for name in [n for n in groups if n.startswith("maybe ") and len(groups[n]) < 3]:
        groups.setdefault("Them", []).extend(groups.pop(name))
    if "Them" in groups:
        groups["Them"].sort(key=lambda r: r["s"])
    people = []
    for name, rs in groups.items():
        them = name == "Them"
        maybe = name.startswith("maybe ")
        speaker = name.startswith("Speaker ") and name not in named_live
        person, _ = _score_person(name, rs, pool, c, use_history=not (them or maybe or speaker))
        pts = person["points"]
        cur = next((p for p in reversed(pts) if p["E"] is not None or p["F"] is not None), None)
        track = rs[-1]["track"]
        if them:
            kind = "blended"
        elif maybe:
            kind = "near miss"
        elif speaker:
            kind = "speaker"         # an unnamed far-end voice, separated live
        elif track == "mic" or (owner and name == owner):
            kind = "you"
        elif name in named_live:
            kind = "named by you"
        else:
            kind = "voice match"
        hints = [r["hint"] for r in rs[-5:] if r.get("hint")]
        people.append({
            "name": name, "kind": kind, "track": track,
            # the stored prints' best guess: a near-miss card's name, or the most common
            # near-miss among a Speaker N's segments (shown as "maybe X?")
            "guess": (name[len("maybe "):] if maybe else
                      (Counter(r["hint"] for r in rs if r.get("hint")).most_common(1) or [[None]])[0][0]
                      if speaker else None),
            "match": max((r.get("match") or 0) for r in rs) or None,
            "hint": hints[-1] if hints else None,
            "speaking": rs[-1]["e"] >= now - 8, "last": rs[-1]["e"], "first": rs[0]["s"],
            "lines": len(rs), "baseline": person["baseline"],
            "confidence": person["confidence"],
            "now": None if not cur else {k: cur[k] for k in ("E", "eb", "F", "fb", "s", "e")
                                          if k in cur},
            "detail": None if not cur else {k: cur.get(k) for k in ("dst", "drate", "lat",
                                                                    "interrupt")},
            "trend": [[p["s"], p["E"], p["F"]] for p in pts[-40:]],
            # What this voice said most recently — so you can name someone from what
            # they're talking about instead of waiting for a name to come up.
            "recent": [{"t": r["s"], "text": r.get("text", "")} for r in rs[-3:] if r.get("text")],
        })
    people.sort(key=lambda p: (p["kind"] == "you", p["first"]))
    base.update({"now": now, "people": people})
    return base

"""Conversation dynamics + meeting awards — vocal cues step 4. See REFERENCE.md.

Everything here is a FACT about the meeting, not a guess about anyone's state: who talked
how much, who started over whom, the longest run without yielding, how long an answer
took. The awards are those facts with a sense of humour, and they follow strict rules:

  * per meeting, never a standing title on a person, always with the number behind it;
  * anyone who spoke is eligible, the user included — unconfirmed voices as their neutral
    "Speaker N" label, never a guessed name;
  * mt web only, behind a one-click hide for screen-sharing.

Best Saying is the one award that needs the audio: the line the room laughed at. mt has
no language model, so it cannot judge funny — but it can hear laughter, with sherpa-onnx's
AudioSet tagger (Apache-2.0, 26 MB). Measured on an Oct 1 standup: it heard the user laugh
on the mic at 0.73–0.85 right after "unless I get hit by a bus tomorrow, which would be
best-case scenario", and MISSED the far-end office laughing at the same moment — Zoom's
noise suppression strips it. So candidates are shown with their lead-in, and the user's
crown always wins.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import numpy as np

import mt as MT
import cues

LAUGHS = "laughs.json"
LIVE_LAUGHS = "laughs_live.jsonl"
CROWN = "awards.json"

TAG_DIR = MT.MODELS / "sherpa-onnx-zipformer-small-audio-tagging-2024-04-15"
LAUGH_CLASSES = {16, 17, 18, 19, 20, 21}   # Laughter, Baby laughter, Giggle, Snicker,
                                           # Belly laugh, Chuckle/chortle (AudioSet ids)
WIN, HOP = 2.0, 1.0
LAUGH_THRESHOLD = 0.5      # 7 events in 33 min on the Oct 1 far-end track at 0.5
QUIET_RMS = 0.001          # -60 dBFS: a silent window cannot be laughing, skip it

BACKCHANNEL_SEC = 1.2      # a "yeah" from someone else neither interrupts nor ends a run
OVERLAP_SEC = 0.3
RUN_GAP_SEC = 3.0          # a pause longer than this ends a monologue
HELD_SEC = 1.5             # talked over, but kept going this long afterwards

AWARDS = [  # key, emoji, title, minimum to award
    ("monologue", "🎤", "The Monologuer", 30.0),
    ("interrupter", "⚡", "The Interrupter", 3),
    ("iron", "🛡️", "Iron Floor", 2),
    ("silence", "🤐", "The Strategic Silence", 3.0),
    ("main", "📢", "Main Character", 0.40),
    ("spice", "🔥", "Spice Level", 1.6),
    ("best", "😂", "Best Saying", 0.0),
]


# Field guide for the Conversation section (served with /api/awards; shown behind a
# "Field guide" toggle). Thresholds match the constants and AWARDS table in this file.
GUIDE = [
    ("Talk", "Share of all speaking time in the meeting, from the transcript's timing."),
    ("Longest run",
     f"Their longest stretch of consecutive lines: a pause over {RUN_GAP_SEC:g} s ends it, "
     f"but someone else's short 'yeah' (under {BACKCHANNEL_SEC:g} s) doesn't."),
    ("Started over others",
     "Times they began talking while someone else was still going. Checked against the "
     "audio: both sides must be audibly talking in the second after it starts. Short "
     "'yeah's don't count."),
    ("Talked over (kept going)",
     f"Times someone started over them; in brackets, how often they kept going at least "
     f"{HELD_SEC:g} s anyway."),
    ("Slowest answer",
     "The longest gap before they answered a question (a line ending in '?'). Only "
     "counted when the floor really passed — if the asker kept talking, nobody was "
     "silent — and gaps over 10 s are treated as a dropped thread, not a pause."),
    ("Speaker N", "An unconfirmed voice. Shown by number, never by a guessed name; it "
                  "becomes a name the moment you confirm it under Voices."),
    ("🎤 The Monologuer", "Longest run of the meeting, if at least 30 s."),
    ("⚡ The Interrupter", "Most audio-checked talk-overs started, if at least 3."),
    ("🛡️ Iron Floor", "Most often talked over and kept going anyway, if at least 2."),
    ("🤐 The Strategic Silence", "Slowest answer to a question, if at least 3 s. "
                                 "The question and answer are on the card."),
    ("📢 Main Character", "Biggest share of the talking, if at least 40%."),
    ("🔥 Spice Level",
     "Biggest Energy spike against their own normal (+1.6 or more). Needs a confirmed "
     "speaker with other analyzed meetings behind them, so it only shows on meetings "
     "you analyzed."),
    ("😂 Best Saying",
     "The line the room laughed at. mt can't judge funny, but it can hear laughter "
     "(an AudioSet sound tagger) — the line just before a laugh scores, more when the "
     "OTHER side laughs. Zoom/Teams often strip far-end laughter, so it misses some. "
     "Your 👑 crown — on a candidate or any transcript line — always wins."),
    ("The rules",
     "Awards are per meeting, never a standing title, and always show the number behind "
     "them. Anyone who spoke can win, you included, but there must be at least two "
     "eligible people. They never leave mt web — not in summaries, weeklies, Jira or "
     "Slack. Hide awards (remembered per browser) before sharing your screen; "
     "config.json → fun_labels: false starts them hidden."),
]


def fun_labels_default() -> bool:
    try:
        return bool(MT.config().get("fun_labels", True))
    except Exception:
        return True


# ── who ───────────────────────────────────────────────────────────────────────

def label(e: dict, names: dict) -> tuple[str, bool]:
    """(label, eligible). A confirmed name or a strong voiceprint match (cues.strong_match),
    else the neutral "Speaker N" — never a merely-good voiceprint guess that speakers.json
    may carry. Unidentified and mixed
    clusters still hold the floor (they can be talked over) but win nothing."""
    if e.get("override"):
        return e["override"], True
    k = e["speaker_key"]
    v = names.get(k) or {}
    if v.get("mixed"):
        return "Multiple voices", False
    if k.endswith("-unknown") or k == "unknown":
        return "Unidentified voice", False
    if v.get("confidence") == "verified" or cues.strong_match(v):
        return v.get("name") or k, True
    m = re.search(r"spk(\d+)$", k)
    return (f"Speaker {int(m.group(1)) + 1}" if m else k), True


# ── dynamics ──────────────────────────────────────────────────────────────────

_activity_cache: dict = {}


def activity(d: Path):
    """check(track, t0, t1) -> was that track audibly talking? From the archived audio,
    decoded once and kept in memory only — nothing is written, so a meeting that was never
    opted into vocal cues gets no new files.

    Why it exists: every talk-over is mic-vs-far-end, and whisper's far-end line ends run
    long. Measured Oct 5 against the audio: 84% of timestamp talk-overs were real on the
    Oct 1 standup, 75% on a Sep 10 call — and 29% on the Sep 4 change control, where the user
    'won' The Interrupter 25× mostly on slop. Raising the overlap threshold did not help."""
    stamp = tuple((cues.track_file(d, t) or Path("/")).stat().st_mtime
                  if cues.track_file(d, t) else 0 for t in ("mic", "system"))
    hit = _activity_cache.get(str(d))
    if hit and hit[0] == stamp:
        return hit[1]
    frames = {}
    for t in ("mic", "system"):
        f = cues.track_file(d, t)
        if f is None:
            continue
        r = MT.frame_rms(cues.decode(f), frame=0.03)
        floor = max(cues.FLOOR_RMS, float(np.percentile(r, 60)) * 1.5)
        frames[t] = (r, floor)
    def check(track, t0, t1):
        if track not in frames:
            return True              # no audio to ask: keep the timestamp's word for it
        r, floor = frames[track]
        seg = r[int(t0 / 0.03):int(t1 / 0.03)]
        return seg.size > 0 and float(np.mean(seg >= floor)) >= 0.35
    _activity_cache[str(d)] = (stamp, check)
    return check


LIVE_ACTIVE_RMS = 0.002    # -54 dBFS: speech on either track, noise below (live check)
_live_act: dict = {}


def live_activity(d: Path):
    """The same check as activity(), for a call still being recorded: read just the one
    second asked about, by seeking in the growing wavs — no decode, no recorder contact.
    Oct 6, a vendor call: the user was 'The Interrupter' live with 22 talk-overs while
    muted; 1 of them had both sides audibly talking. The far end's line ends run long."""
    files = {t: d / f"{t}.wav" for t in ("mic", "system")}
    if not all(f.exists() for f in files.values()):
        return None
    cache = _live_act.setdefault(str(d), {})
    def check(track, t0, t1):
        f = files.get(track)
        if f is None:
            return True
        key = (track, round(t0, 2))
        if key in cache:
            return cache[key]
        try:
            off = MT.wav_data_offset(f)
            have = (f.stat().st_size - off) / 32000.0
            with open(f, "rb") as h:
                h.seek(off + int(t0 * 16000) * 2)
                raw = h.read(max(0, int((t1 - t0) * 16000)) * 2)
        except OSError:
            return True
        x = np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768
        r = MT.frame_rms(x, frame=0.03)
        ok = r.size > 0 and float(np.mean(r >= LIVE_ACTIVE_RMS)) >= 0.35
        if t1 < have - 2:            # never cache a window the recorder hasn't finished
            cache[key] = ok
        return ok
    return check


def dynamics(lines: list[dict], check=None) -> dict:
    """lines: [{i, s, e, label, eligible, text, track}] in time order. `check`, when given,
    confirms each talk-over against the audio (see activity())."""
    ppl: dict = {}
    def P(lab, elig):
        return ppl.setdefault(lab, {"label": lab, "eligible": elig, "talk": 0.0, "lines": 0,
                                    "longest": None, "overs": 0, "overed": 0, "held": 0,
                                    "answers": 0, "slowest": None})
    for ln in lines:
        p = P(ln["label"], ln["eligible"])
        p["talk"] += max(0.0, ln["e"] - ln["s"])
        p["lines"] += 1

    # monologues: consecutive lines of one person, short backchannels ignored
    run = None
    def close(r):
        if r:
            p = ppl[r["label"]]
            d = r["e"] - r["s"]
            if not p["longest"] or d > p["longest"]["dur"]:
                p["longest"] = {"dur": round(d, 1), "s": r["s"], "e": r["e"], "i": r["i"]}
    for ln in lines:
        short = ln["e"] - ln["s"] < BACKCHANNEL_SEC
        if run and ln["label"] == run["label"] and ln["s"] - run["e"] <= RUN_GAP_SEC:
            run["e"] = max(run["e"], ln["e"])
        elif run and ln["label"] != run["label"] and short:
            continue
        else:
            close(run)
            run = {"label": ln["label"], "s": ln["s"], "e": ln["e"], "i": ln["i"]}
    close(run)

    # talk-overs: starting while someone else is still going (not a backchannel)
    matrix: dict = {}
    overs = []
    for j, ln in enumerate(lines):
        if ln["e"] - ln["s"] < BACKCHANNEL_SEC:
            continue
        victim = None
        for i in range(max(0, j - 25), j):
            o = lines[i]
            if o["label"] != ln["label"] and o["s"] < ln["s"] and o["e"] - ln["s"] >= OVERLAP_SEC:
                victim = o
        if victim and check is not None and not (
                check(victim["track"], ln["s"], ln["s"] + 1.0)
                and check(ln["track"], ln["s"], ln["s"] + 1.0)):
            victim = None            # the timestamps overlapped; the voices did not
        if victim:
            ppl[ln["label"]]["overs"] += 1
            ppl[victim["label"]]["overed"] += 1
            if victim["e"] - ln["s"] >= HELD_SEC:
                ppl[victim["label"]]["held"] += 1
            key = f"{ln['label']}→{victim['label']}"
            matrix[key] = matrix.get(key, 0) + 1
            overs.append({"t": ln["s"], "by": ln["label"], "over": victim["label"], "i": ln["i"]})

    # answers: the gap after a question, only when the floor really passes — the very
    # next real line is someone else's. If the asker keeps talking, nobody was silent.
    for j, q in enumerate(lines):
        if not q.get("text", "").rstrip().endswith("?"):
            continue
        nxt = next((x for x in lines[j + 1:j + 12]
                    if x["s"] >= q["s"] and x["e"] - x["s"] >= 0.6), None)
        if nxt is None or nxt["label"] == q["label"] or nxt["s"] < q["e"] - OVERLAP_SEC:
            continue
        gap = max(0.0, nxt["s"] - q["e"])
        if gap > 10:
            continue                 # that long is a dropped thread, not a pause
        p = ppl[nxt["label"]]
        p["answers"] += 1
        if not p["slowest"] or gap > p["slowest"]["gap"]:
            p["slowest"] = {"gap": round(gap, 1), "s": q["s"], "i": nxt["i"],
                            "q": q["text"][:140], "a": nxt["text"][:140]}

    total = sum(p["talk"] for p in ppl.values()) or 1.0
    for p in ppl.values():
        p["share"] = round(p["talk"] / total, 3)
        p["talk"] = round(p["talk"], 1)
    people = sorted(ppl.values(), key=lambda p: -p["talk"])
    return {"people": people,
            "matrix": sorted(({"pair": k, "n": v} for k, v in matrix.items()),
                             key=lambda x: -x["n"]),
            "overs": overs}


# ── laughter ──────────────────────────────────────────────────────────────────

_tagger = None


def tagger():
    """The AudioSet tagger, or None when the model is not installed (run ./make.sh)."""
    global _tagger
    if _tagger is None:
        m = TAG_DIR / "model.int8.onnx"
        if not m.exists():
            return None
        import sherpa_onnx as so
        _tagger = so.AudioTagging(so.AudioTaggingConfig(
            model=so.AudioTaggingModelConfig(
                zipformer=so.OfflineZipformerAudioTaggingModelConfig(model=str(m)),
                num_threads=2, debug=False, provider="cpu"),
            labels=str(TAG_DIR / "class_labels_indices.csv"), top_k=20))
    return _tagger


def laugh_windows(x: np.ndarray, offset: float = 0.0) -> list[tuple[float, float]]:
    tg = tagger()
    if tg is None:
        return []
    out = []
    n = int(WIN * 16000)
    for s in np.arange(0, max(0.0, len(x) / 16000 - WIN) + 1e-9, HOP):
        w = x[int(s * 16000):int(s * 16000) + n]
        if w.size < n // 2 or float(np.abs(w).max()) < QUIET_RMS * 3:
            continue
        st = tg.create_stream()
        st.accept_waveform(sample_rate=16000, waveform=w)
        p = max([e.prob for e in tg.compute(st) if e.index in LAUGH_CLASSES] or [0.0])
        if p >= LAUGH_THRESHOLD * 0.6:
            out.append((round(offset + float(s), 2), round(float(p), 3)))
    return out


def events(windows: list, track: str) -> list[dict]:
    """Contiguous windows at or above the threshold, merged into one laugh."""
    ev, cur = [], None
    for t, p in windows:
        if p < LAUGH_THRESHOLD:
            continue
        if cur and t - cur["last"] <= HOP + 0.01:
            cur["last"], cur["e"] = t, t + WIN
            cur["peak"] = max(cur["peak"], p)
            cur["strength"] += p
        else:
            cur = {"track": track, "s": t, "e": t + WIN, "last": t, "peak": p, "strength": p}
            ev.append(cur)
    for x in ev:
        x.pop("last", None)
        x["strength"] = round(x["strength"], 2)
    return ev


def scan(d: Path, progress=None) -> dict | None:
    """Laughter over the whole meeting → laughs.json. None if the model is missing."""
    if tagger() is None:
        return None
    out = []
    for t in ("system", "mic"):
        f = cues.track_file(d, t)
        if f is None:
            continue
        x = cues.decode(f)
        out += events(laugh_windows(x), t)
        if progress:
            progress(t)
    doc = {"version": 1, "model": TAG_DIR.name, "threshold": LAUGH_THRESHOLD,
           "scanned_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "events": sorted(out, key=lambda e: e["s"])}
    (d / LAUGHS).write_text(json.dumps(doc) + "\n")
    return doc


def live_laughs(d: Path, kind: str, at: float, wav: Path) -> int:
    """During the call: tag the chunk just transcribed and append any laugh."""
    if tagger() is None:
        return 0
    ev = events(laugh_windows(cues.decode(wav), offset=at), kind)
    if ev:
        with open(d / LIVE_LAUGHS, "a") as f:
            for e in ev:
                f.write(json.dumps(e) + "\n")
    return len(ev)


def candidates(lines: list[dict], laughs: list[dict], top: int = 3) -> list[dict]:
    """The line each laugh followed, scored by how much laughing it got. A laugh on the
    OTHER track counts for more — the room laughing at you beats you laughing at yourself.
    Two lines of lead-in travel with it, because a laugh often lands a beat late."""
    by_line: dict = {}
    for ev in laughs:
        prior = [ln for ln in lines if ln["s"] <= ev["s"] - 0.5 and ln["e"] >= ev["s"] - 8
                 and ln["e"] - ln["s"] >= 0.8]
        if not prior:
            continue
        other = [ln for ln in prior if ln["track"] != ev["track"]]
        q = (other or prior)[-1]
        w = ev["strength"] * (1.3 if q["track"] != ev["track"] else 1.0)
        c = by_line.setdefault(q["i"], {"i": q["i"], "s": q["s"], "who": q["label"],
                                        "text": q["text"], "score": 0.0, "laughs": 0})
        c["score"] += w
        c["laughs"] += 1
    out = sorted(by_line.values(), key=lambda c: -c["score"])[:top]
    idx = {ln["i"]: k for k, ln in enumerate(lines)}
    for c in out:
        k = idx[c["i"]]
        c["lead"] = [{"who": ln["label"], "text": ln["text"]} for ln in lines[max(0, k - 2):k]]
        c["score"] = round(c["score"], 2)
    return out


# ── the meeting view ──────────────────────────────────────────────────────────

def _lines(d: Path):
    tl = json.loads((d / "transcript.json").read_text()).get("timeline", [])
    names = json.loads((d / "speakers.json").read_text()).get("map", {})
    lines = []
    for i, e in enumerate(tl):
        lab, ok = label(e, names)
        lines.append({"i": i, "s": e["start"], "e": e["end"], "label": lab, "eligible": ok,
                      "text": e.get("text", ""), "track": e.get("track", "system")})
    lines.sort(key=lambda x: x["s"])
    return tl, lines


def _award(key, who, value, t=None, i=None, detail=""):
    a = next(x for x in AWARDS if x[0] == key)
    return {"key": key, "emoji": a[1], "title": a[2], "who": who, "value": value,
            "t": t, "i": i, "detail": detail}


def award_list(dyn: dict, spice: dict | None, best: dict | None) -> list[dict]:
    el = [p for p in dyn["people"] if p["eligible"]]
    out = []
    if len(el) < 2:
        # An award needs a field. Live, before any far-end voice matches, the user is the only
        # eligible speaker and swept every category on replay — that is not a contest.
        el = []
    m = max((p for p in el if p["longest"]), key=lambda p: p["longest"]["dur"], default=None)
    if m and m["longest"]["dur"] >= AWARDS[0][3]:
        out.append(_award("monologue", m["label"], f"{MT.hhmmss(m['longest']['dur'])} without yielding",
                          m["longest"]["s"], m["longest"]["i"]))
    m = max(el, key=lambda p: p["overs"], default=None)
    if m and m["overs"] >= AWARDS[1][3]:
        out.append(_award("interrupter", m["label"], f"started over someone {m['overs']}×"))
    m = max(el, key=lambda p: p["held"], default=None)
    if m and m["held"] >= AWARDS[2][3]:
        out.append(_award("iron", m["label"], f"talked over {m['overed']}×, kept going {m['held']}×"))
    m = max((p for p in el if p["slowest"]), key=lambda p: p["slowest"]["gap"], default=None)
    if m and m["slowest"]["gap"] >= AWARDS[3][3]:
        out.append(_award("silence", m["label"], f"{m['slowest']['gap']}s before answering",
                          m["slowest"]["s"], m["slowest"]["i"],
                          f"Q: {m['slowest']['q']}  A: {m['slowest']['a']}"))
    if len(el) >= 2:
        m = max(el, key=lambda p: p["share"])
        if m["share"] >= AWARDS[4][3]:
            out.append(_award("main", m["label"], f"{round(100 * m['share'])}% of the talking"))
    if spice and spice["E"] >= AWARDS[5][3]:
        out.append(_award("spice", spice["who"], f"energy +{spice['E']:.1f} over their normal",
                          spice["t"], spice.get("i")))
    if best:
        out.append(_award("best", best["who"], f"“{best['text'][:160]}”", best["s"],
                          best["i"], "crowned by you" if best.get("crowned") else
                          f"{best.get('laughs', 0)} laugh{'s' if best.get('laughs', 0) != 1 else ''}"))
    return out


def _spice(d: Path) -> dict | None:
    if not (d / cues.FILE).exists():
        return None
    try:
        s = cues.score(d)
    except Exception:
        return None
    best = None
    for p in s.get("people", []):
        if p["baseline"]["kind"] != "history":
            continue                 # a spike needs a real normal to be a spike
        for pt in p["points"]:
            if pt["E"] is not None and (best is None or pt["E"] > best["E"]):
                best = {"who": p["name"], "E": pt["E"], "t": pt["s"], "i": pt["i"]}
    return best


def crown(d: Path, i: int | None) -> dict:
    tl, lines = _lines(d)
    if i is None:
        (d / CROWN).unlink(missing_ok=True)
        return {"ok": True, "crowned": None}
    if not (0 <= i < len(tl)):
        return {"ok": False, "why": "no such line"}
    (d / CROWN).write_text(json.dumps({
        "best_saying": {"i": i, "fingerprint": cues.fingerprint(tl),
                        "text": tl[i].get("text", "")},
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}) + "\n")
    return {"ok": True, "crowned": i}


def view(d: Path) -> dict:
    tl, lines = _lines(d)
    try:
        check = activity(d)
    except Exception:
        check = None                 # no audio decodable: timestamps only
    dyn = dynamics(lines, check)
    laughs_doc = None
    if (d / LAUGHS).exists():
        try:
            laughs_doc = json.loads((d / LAUGHS).read_text())
        except ValueError:
            laughs_doc = None
    cands = candidates(lines, laughs_doc["events"]) if laughs_doc else []
    crowned = None
    try:
        c = json.loads((d / CROWN).read_text()).get("best_saying")
        if c and c.get("fingerprint") == cues.fingerprint(tl):
            ln = next(x for x in lines if x["i"] == c["i"])
            crowned = {"i": ln["i"], "s": ln["s"], "who": ln["label"], "text": ln["text"],
                       "crowned": True}
    except (OSError, ValueError, StopIteration):
        pass
    best = crowned or (cands[0] if cands else None)
    return {"fun_labels": fun_labels_default(),
            "awards": award_list(dyn, _spice(d), best),
            "dynamics": dyn, "best": {"candidates": cands, "crowned": crowned},
            "laughs": {"model": tagger() is not None, "scanned": laughs_doc is not None,
                       "events": len(laughs_doc["events"]) if laughs_doc else 0},
            "analyzed": (d / cues.FILE).exists()}


def live_view(d: Path, owner: str | None) -> dict:
    """Awards so far, from the live rows: rougher labels (you / a voice match / Them)."""
    rows = cues._live_rows(d, cues.cfg())
    # The blended far end is a crowd, not a person: it holds the floor and can be quoted
    # for Best Saying, but it never wins an award ("Main Character: Them 55%" on replay).
    lines = [{"i": r["i"], "s": r["s"], "e": r["e"], "label": r["name"],
              "eligible": r["name"] != "Them" and not r["name"].startswith("maybe "),
              "text": r.get("text", ""), "track": r.get("track", "system")} for r in rows]
    if not lines:
        return {"fun_labels": fun_labels_default(), "awards": []}
    try:
        check = live_activity(d) or activity(d)   # growing wavs while live, flac after
    except Exception:
        check = None
    dyn = dynamics(lines, check)
    laughs = []
    try:
        for ln in (d / LIVE_LAUGHS).read_text().splitlines():
            try:
                laughs.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    cands = candidates(lines, laughs, top=1)
    return {"fun_labels": fun_labels_default(),
            "awards": award_list(dyn, None, cands[0] if cands else None)}


def summary_text(v: dict) -> str:
    L = [f"{a['emoji']} {a['title']}: {a['who']} — {a['value']}" for a in v["awards"]]
    if not v["laughs"]["model"]:
        L.append("(Best Saying needs the laughter model — run ./make.sh)")
    return "\n".join(L) or "no awards this meeting"

#!/usr/bin/env python3
"""
mt — local meeting transcription with speaker identification.

Everything runs on this Mac. No audio, transcript, or voiceprint leaves the machine.

  mt doctor                      check the install and permissions
  mt devices                     list microphones
  mt record  [opts]              record a meeting, then transcribe it
  mt process PATH [opts]         transcribe a session dir or any audio/video file
  mt enroll  SESSION --speaker N --name "Full Name"
  mt speakers                    list enrolled voiceprints
  mt forget  "Full Name"         retire a voiceprint (moved aside, never deleted)
  mt restore "Full Name"         put a retired voiceprint back
  mt rename  "Old" "New"         fix the spelling of a voiceprint name
  mt relabel SESSION             re-render after editing speakers.json
  mt recapture                   rebuild the system-audio tap on a running recording
  mt search "query" [opts]       full-text search across meetings and notes
  mt index [--rebuild]           (re)build the search index
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import wave
from collections import deque
from pathlib import Path

import numpy as np

import audioprefs
import passqueue
import paths as P

HERE = Path(__file__).resolve().parent
WORKSPACE = P.WORKSPACE                             # see paths.py for how it is resolved
VERSION = (HERE / "VERSION").read_text().strip() if (HERE / "VERSION").exists() else "dev"
# MT_CAPTURE swaps in a stand-in recorder (used by tests). Name it anything BUT mtcapture:
# every mt on the machine treats a process of that name as a live recording.
CAPTURE = Path(os.environ.get("MT_CAPTURE") or (HERE / "build" / "mtcapture"))
WATCH = HERE / "build" / "mtwatch"
CAL = HERE / "build" / "mtcal"
MODELS = HERE / "models"
# MT_VOICEPRINTS lets you point at an alternate store (used by the test suite)
VOICEPRINTS = Path(os.environ.get("MT_VOICEPRINTS") or (HERE / "voiceprints"))
# A retired print moves here, it is never unlinked: this whole tree syncs to Drive, so a
# delete is permanent in both places. Retirement has to be undoable.
RETIRED = VOICEPRINTS / "retired"
RECORDINGS = P.RECORDINGS

WHISPER_MODEL = MODELS / "ggml-large-v3-turbo-q5_0.bin"
SEG_MODEL = MODELS / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMB_MODEL = MODELS / "nemo_en_titanet_small.onnx"

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".aif",
             ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".caf"}

DEFAULTS = {
    "owner": P.OWNER,             # config "owner", else your macOS full name
    "language": "en",
    "threads": 8,
    # cosine similarity against enrolled voiceprints
    "auto_assign_score": 0.62,   # >= this -> name it automatically
    "suggest_score": 0.42,       # >= this -> offer it as a suggestion only
    # NB: sherpa's clustering threshold is a DISTANCE, so higher merges more. 0.55 was
    # far too tight and gave a new speaker for nearly every turn on a real meeting.
    "cluster_threshold": 0.90,
    "min_duration_on": 0.4,
    "min_duration_off": 0.5,
    # second-pass repair (see consolidate())
    "merge_similarity": 0.60,      # merge two clusters that sound this alike
    "min_speaker_seconds": 8.0,    # below this it is a fragment, not a participant
    "absorb_similarity": 0.45,     # fold a fragment into a speaker at/above this
    # Below this, a cluster disagrees with itself and is treated as multiple speakers.
    # Measured: single-speaker clusters 0.78-0.97, confirmed mixtures 0.36 and 0.07.
    "min_self_similarity": 0.60,
}


def config() -> dict:
    cfg = dict(DEFAULTS)
    f = HERE / "config.json"
    if f.exists():
        try:
            cfg.update(json.loads(f.read_text()))
        except Exception as e:
            warn(f"ignoring bad config.json: {e}")
    return cfg


# ---------------------------------------------------------------- output helpers

BOLD, DIM, RED, YEL, GRN, RST = "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[0m"
if not sys.stderr.isatty():
    BOLD = DIM = RED = YEL = GRN = RST = ""


def say(m=""):
    print(m, file=sys.stderr, flush=True)


# (label, wall time, cpu time) of every ==> line, for the queued job's step timings.
STEP_MARKS: list[tuple[str, float, float]] = []


def step(m):
    STEP_MARKS.append((m, time.time(), passqueue.cpu_seconds()))
    say(f"{BOLD}==>{RST} {m}")


def warn(m):
    say(f"{YEL}warning:{RST} {m}")


class ExitWithReason(int):
    """An exit status that still knows what went wrong.

    Plain sys.exit(1) throws the sentence away, and web.py stringifies the SystemExit
    straight into the notes it shows you — a guard that refused for a good reason
    surfaced there as "not enrolled: 1". Handing sys.exit() a string instead would make
    the interpreter print the whole message a second time. An int subclass is both at
    once: the shell still sees status 1, str() gives the explanation.
    """

    def __new__(cls, code: int, message: str):
        o = super().__new__(cls, code)
        o.message = message
        return o

    def __str__(self) -> str:
        return self.message


def fail(m, code=1):
    say(f"{RED}error:{RST} {m}")
    sys.exit(ExitWithReason(code, str(m)))


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return re.sub(r"-{2,}", "-", s) or "meeting"


def hhmmss(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def need(tool: str) -> str:
    p = shutil.which(tool)
    if not p:
        fail(f"`{tool}` not found on PATH. Install it with: brew install {tool}")
    return p


# ---------------------------------------------------------------- audio plumbing

# Gain is BOUNDED on purpose. ffmpeg loudnorm targeting -16 LUFS will happily apply
# +34 dB to a near-silent track, which amplifies the room noise floor and faint speaker
# bleed until whisper transcribes them as speech — that is how a muted mic ended up
# "saying" Thank you thirty times, and how speaker bleed became a garbled second copy
# of the far side under the mic owner's name.
# Rumble and mains-hum rejection. 80 Hz is the standard voice high-pass.
HIGHPASS_HZ = 80
MAX_GAIN_DB = 18.0
TARGET_SPEECH_RMS = 0.06
MIN_SPEECH_RMS = 0.010      # after gain: below this a block is noise, not speech


def frame_rms(x: np.ndarray, sr: int = 16000, frame: float = 0.03) -> np.ndarray:
    fw = int(sr * frame)
    n = len(x) // fw
    if n == 0:
        return np.zeros(1, dtype=np.float32)
    return np.sqrt((x[: n * fw].reshape(n, fw) ** 2).mean(axis=1) + 1e-12)


def speech_level(x: np.ndarray) -> float:
    """Loudness of the loud part of the track, ignoring the silence."""
    return float(np.percentile(frame_rms(x), 90))


def to_16k_mono(src: Path, dst: Path, normalize: bool = True) -> Path:
    """16 kHz mono PCM, with bounded gain applied."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # 80 Hz high-pass before anything else sees the audio. Measured on two real mics: a
    # USB headset through a DisplayLink dock carried 88% of its noise energy below 120 Hz
    # with a 60 Hz mains peak 2253x the median, and removing it gained +6.7 dB SNR; even
    # the clean HyperX gained +3.7 dB. Only ~2% of speech energy sits below 80 Hz (male
    # fundamentals start near 85 Hz), and transcripts agreed 97.2% with every domain term
    # intact. A quieter floor also leaves the bounded gain stage more usable headroom.
    subprocess.run(
        [need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", f"highpass=f={HIGHPASS_HZ}:poles=2",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        check=True, capture_output=True, text=True)
    if not normalize:
        return dst
    x = read_wav16k(dst)
    lvl = speech_level(x)
    if lvl <= 1e-6:
        return dst
    gain = min(TARGET_SPEECH_RMS / lvl, 10 ** (MAX_GAIN_DB / 20.0))
    if gain > 1.02:
        write_wav16k(dst, np.clip(x * gain, -1.0, 1.0))
        say(f"{DIM}   level {20*np.log10(max(lvl,1e-9)):.0f} dBFS -> gain "
            f"+{20*np.log10(gain):.0f} dB{RST}")
    return dst


def read_wav16k(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1:
            raise ValueError(f"{p} is not 16 kHz mono")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def duration_of(p: Path) -> float:
    try:
        out = run([need("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(p)]).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def peak_of(p: Path) -> float:
    try:
        x = read_wav16k(p)
        return float(np.abs(x).max()) if x.size else 0.0
    except Exception:
        return 0.0


def has_signal(p: Path, floor: float = 0.0008) -> bool:
    try:
        x = read_wav16k(p)
        return bool(x.size) and float(np.abs(x).max()) >= floor
    except Exception:
        return False


# ---------------------------------------------------------------- transcription

def write_wav16k(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.clip(x, -1.0, 1.0).__mul__(32767).astype(np.int16).tobytes())


def speech_blocks(turns: list[dict], total: float, gap: float = 2.0,
                  pad: float = 0.25, min_len: float = 0.4) -> list[tuple[float, float]]:
    """Contiguous stretches worth sending to the recogniser."""
    out: list[list[float]] = []
    for a, b in merge_intervals(turns):
        a, b = max(0.0, a - pad), min(total, b + pad)
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out if b - a >= min_len]


def transcribe_speech(x: np.ndarray, blocks: list[tuple[float, float]],
                      work: Path, kind: str, cfg: dict,
                      sep: float = 0.4) -> list[dict]:
    """Transcribe only the speech, then map timings back to the real timeline.

    Whisper run over a long track with big silent gaps falls into a repetition loop —
    on a 33-minute call where the mic owner was quiet for the first 19 minutes it
    emitted "Thank you." roughly thirty times and lost every real sentence. Feeding it
    just the speech, joined by short silences, removes the failure mode entirely and
    is faster besides.
    """
    if not blocks:
        return []
    gap = np.zeros(int(sep * 16000), dtype=np.float32)
    pieces, spans, cursor = [], [], 0.0
    for a, b in blocks:
        seg = x[int(a * 16000):int(b * 16000)]
        if seg.size == 0:
            continue
        pieces.append(seg)
        spans.append({"c0": cursor, "c1": cursor + seg.size / 16000.0, "o0": a, "o1": b})
        cursor += seg.size / 16000.0
        pieces.append(gap)
        cursor += sep
    if not spans:
        return []

    joined = work / f"{kind}.speech.wav"
    write_wav16k(joined, np.concatenate(pieces))
    raw = transcribe(joined, work, cfg)

    out = []
    for r in raw:
        mid = (r["start"] + r["end"]) / 2.0
        sp = next((z for z in spans if z["c0"] <= mid <= z["c1"]), None)
        if sp is None:
            continue                      # landed in a separator: nothing was said there
        shift = sp["o0"] - sp["c0"]
        a = max(sp["o0"], r["start"] + shift)
        b = min(sp["o1"], r["end"] + shift)
        if b <= a:
            b = min(sp["o1"], a + 0.4)
        out.append({"start": a, "end": b, "text": r["text"]})
    return sorted(out, key=lambda z: z["start"])


def drop_repeats(segs: list[dict], limit: int = 2) -> tuple[list[dict], int]:
    """Whisper stuck in a loop repeats one short phrase. Keep a couple, bin the rest."""
    out, dropped, run, prev = [], 0, 0, None
    for s in segs:
        key = re.sub(r"[^a-z0-9]+", "", s["text"].lower())
        short = len(key) <= 24
        run = run + 1 if (key == prev and short) else 0
        prev = key
        if run >= limit:
            dropped += 1
            continue
        out.append(s)
    return out, dropped


def load_asr(json_path: Path) -> list[dict]:
    data = json.loads(json_path.read_text())
    segs = []
    for s in data.get("transcription", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        off = s.get("offsets") or {}
        segs.append({"start": float(off.get("from", 0)) / 1000.0,
                     "end": float(off.get("to", 0)) / 1000.0, "text": text})
    return segs


def transcribe(wav16: Path, work: Path, cfg: dict) -> list[dict]:
    """whisper.cpp -> [{start,end,text}] in seconds."""
    prefix = work / wav16.stem
    model = cfg.get("model_override") or str(WHISPER_MODEL)
    cmd = [need("whisper-cli"), "-m", model, "-f", str(wav16),
           "-t", str(cfg["threads"]), "-oj", "-of", str(prefix), "--no-prints"]
    if cfg.get("language"):
        cmd += ["-l", cfg["language"]]
    if cfg.get("prompt"):
        cmd += ["--prompt", cfg["prompt"]]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    # NB: prefix contains a dot ("system.16k"), so with_suffix() would eat it
    data = json.loads(Path(f"{prefix}.json").read_text())
    segs = []
    for s in data.get("transcription", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        off = s.get("offsets") or {}
        segs.append({
            "start": float(off.get("from", 0)) / 1000.0,
            "end": float(off.get("to", 0)) / 1000.0,
            "text": text,
        })
    return segs


# ---------------------------------------------------------------- diarization

def _diarizer(cfg: dict, num_speakers: int = -1):
    import sherpa_onnx
    for m in (SEG_MODEL, EMB_MODEL):
        if not m.exists():
            fail(f"missing model: {m}\nRun: mt doctor")
    c = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
            num_threads=min(4, cfg["threads"]), debug=False, provider="cpu"),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(EMB_MODEL), num_threads=min(4, cfg["threads"]), provider="cpu"),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=int(num_speakers) if num_speakers and num_speakers > 0 else -1,
            threshold=float(cfg["cluster_threshold"])),
        min_duration_on=float(cfg["min_duration_on"]),
        min_duration_off=float(cfg["min_duration_off"]),
    )
    if not c.validate():
        fail("diarization config rejected by sherpa-onnx")
    return sherpa_onnx.OfflineSpeakerDiarization(c)


def diarize(wav16: Path, cfg: dict, num_speakers: int = -1) -> list[dict]:
    x = read_wav16k(wav16)
    if x.size == 0:
        return []
    res = _diarizer(cfg, num_speakers).process(x).sort_by_start_time()
    return [{"start": float(s.start), "end": float(s.end), "cluster": int(s.speaker)} for s in res]


# TitaNet's encoder has a fixed maximum input length (~12288 frames, about 123 s).
# Feed it more and onnxruntime throws a broadcast error, so the cap is hard-truncated
# rather than just breaking out of the accumulation loop.
EMBED_CAP_SEC = 60.0


def embed_cluster(x: np.ndarray, turns: list[dict], cluster: int,
                  cap_sec: float = EMBED_CAP_SEC) -> np.ndarray | None:
    """One averaged voice embedding for everything a cluster said."""
    import sherpa_onnx
    cfgx = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=2, provider="cpu")
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfgx)
    chunks, total = [], 0.0
    for t in turns:
        if t["cluster"] != cluster:
            continue
        a, b = int(t["start"] * 16000), int(t["end"] * 16000)
        piece = x[max(0, a):min(len(x), b)]
        if piece.size < 16000 * 0.35:          # <0.35 s is not worth embedding
            continue
        chunks.append(piece)
        total += piece.size / 16000.0
        if total >= cap_sec:
            break
    if not chunks:
        return None
    audio = np.concatenate(chunks)[: int(cap_sec * 16000)]   # hard cap, see EMBED_CAP_SEC
    st = ex.create_stream()
    st.accept_waveform(16000, audio)
    st.input_finished()
    v = np.asarray(ex.compute(st), dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else None


# ---------------------------------------------------------------- voiceprints

def embed_audio(chunk: np.ndarray, cap_sec: float = EMBED_CAP_SEC) -> np.ndarray | None:
    """Voice embedding for a plain slice of audio."""
    import sherpa_onnx
    if chunk.size < int(0.5 * 16000):
        return None
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4,
                                                    provider="cpu"))
    st = ex.create_stream()
    st.accept_waveform(16000, chunk[: int(cap_sec * 16000)])
    st.input_finished()
    v = np.asarray(ex.compute(st), dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n else None


def read_index(path: Path) -> dict:
    """Read a voiceprint index (live or retired) as a mutable doc."""
    doc = {"speakers": []}
    if path.exists():
        try:
            doc = json.loads(path.read_text())
        except Exception as e:
            fail(f"unreadable voiceprint index {path}: {e}")
    doc.setdefault("speakers", [])
    return doc


def write_index(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def find_print(people: list[dict], want: str) -> dict | None:
    """Look a record up by display name or by slug — either spelling is accepted."""
    slug = slugify(want)
    for p in people:
        if p.get("slug") in (want, slug):
            return p
        if str(p.get("name", "")).strip().lower() == want.strip().lower():
            return p
    return None


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) or 1) * (np.linalg.norm(b) or 1)))


def load_voiceprints() -> list[dict]:
    idx = VOICEPRINTS / "index.json"
    if not idx.exists():
        return []
    try:
        people = json.loads(idx.read_text()).get("speakers", [])
    except Exception as e:
        warn(f"unreadable voiceprint index: {e}")
        return []
    for p in people:
        f = VOICEPRINTS / p["file"]
        p["vec"] = np.load(f) if f.exists() else None
    return [p for p in people if p.get("vec") is not None]


def enroll_guard(name: str, vec: np.ndarray, replace: bool = False,
                 force: bool = False) -> None:
    """Refuse an enrolment that contradicts the prints already on disk.

    Two ways a voiceprint goes quietly bad, both invisible until months later:

    (a) the name is already enrolled but this sample does not sound like it, so the
        running mean averages a second person into somebody else's print;
    (b) this sample already sounds exactly like a DIFFERENT enrolled person, so the
        voice is about to be filed under the wrong name.

    Sep 9 2026: "Pat Placeholder" and "Patrick Placeholdr" were enrolled two minutes apart
    off one unusable recording, 0.49 apart — two different people, one name, two
    spellings, and nothing said a word. Check (b) is the one that would have caught it.
    """
    people = load_voiceprints()
    if not people:
        return
    thr = float(config()["auto_assign_score"])
    slug = slugify(name)

    same = next((p for p in people if p.get("slug") == slug), None)
    if same is not None and not replace and not force:
        s = cosine(vec, same["vec"])
        if s < thr:
            fail(f"""this sample scores only {s:.2f} against the print already stored for
    "{same['name']}" — anything below {thr:.2f} is a different voice on this machine.
    Either this is a different person who happens to share that name, or one of the two
    recordings is bad. Averaging them together would poison the print for good.
      replace the stored print with this sample:  add --replace
      retire the stored print instead:            mt forget "{same['name']}"
      average them anyway, if you are certain:    add --force""")

    if not force:
        other, best = None, -1.0
        for p in people:
            if p.get("slug") == slug:
                continue
            s = cosine(vec, p["vec"])
            if s > best:
                other, best = p, s
        if other is not None and best >= thr:
            fail(f"""this voice already matches the enrolled print for "{other['name']}"
    at {best:.2f} — at or above the {thr:.2f} auto-assign threshold — but you are
    enrolling it as "{name}". Either the same person is being enrolled twice under two
    names, or this voice is about to be labelled with the wrong person's name.
      same person:       mt forget "{other['name']}"  then enroll again
      really different:  add --force""")


def save_voiceprint(name: str, vec: np.ndarray, source: str, replace: bool = False,
                    force: bool = False) -> dict:
    enroll_guard(name, vec, replace=replace, force=force)
    VOICEPRINTS.mkdir(parents=True, exist_ok=True)
    idx_path = VOICEPRINTS / "index.json"
    doc = {"speakers": []}
    if idx_path.exists():
        try:
            doc = json.loads(idx_path.read_text())
        except Exception:
            pass
    people = doc.setdefault("speakers", [])
    slug = slugify(name)
    rec = next((p for p in people if p.get("slug") == slug), None)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    if rec and replace:
        # --replace overwrites. The running mean below can dilute a bad print but can
        # never get rid of it, which is the whole reason this flag exists.
        np.save(VOICEPRINTS / rec["file"], vec.astype(np.float32))
        rec["samples"] = 1
        rec["sources"] = [source]
        rec["dim"] = int(vec.shape[0])
        rec["updated"] = now
        rec["replaced"] = now
    elif rec:
        # running mean keeps improving the print with each enrolment
        old = np.load(VOICEPRINTS / rec["file"])
        n = int(rec.get("samples", 1))
        merged = (old * n + vec) / (n + 1)
        merged /= (np.linalg.norm(merged) or 1.0)
        np.save(VOICEPRINTS / rec["file"], merged.astype(np.float32))
        rec["samples"] = n + 1
        rec["updated"] = now
        rec.setdefault("sources", []).append(source)
    else:
        rec = {"name": name, "slug": slug, "file": f"{slug}.npy", "dim": int(vec.shape[0]),
               "samples": 1, "added": now, "updated": now, "sources": [source]}
        np.save(VOICEPRINTS / rec["file"], vec.astype(np.float32))
        people.append(rec)

    idx_path.write_text(json.dumps(doc, indent=2) + "\n")
    return rec


def match_voice(vec: np.ndarray, people: list[dict]) -> tuple[str | None, float]:
    best, score = None, -1.0
    for p in people:
        v = p["vec"]
        s = cosine(vec, v)
        if s > score:
            best, score = p["name"], s
    return best, score


# ---------------------------------------------------------------- assembling

def cluster_durations(turns: list[dict]) -> dict[int, float]:
    d: dict[int, float] = {}
    for t in turns:
        d[t["cluster"]] = d.get(t["cluster"], 0.0) + (t["end"] - t["start"])
    return d


UNKNOWN = -1


def spans_self_similarity(x: np.ndarray, spans: list[tuple[float, float]]) -> float | None:
    """How much a cluster agrees with itself: split its own turns into two disjoint
    halves, embed each, compare.

    Catches the failure the voiceprint score cannot. A cluster holding two people
    produces a BLENDED embedding that can still score above the auto-assign threshold —
    measured on a real meeting, a two-person mixture matched one of them at 0.64 against
    a 0.62 threshold and would have been named her. Self-similarity separates the cases
    cleanly: eight human-confirmed single-speaker clusters scored 0.78-0.97, the two
    confirmed mixtures scored 0.36 and 0.07.
    """
    pieces = []
    for st_, en_ in sorted(spans):
        a, b = int(st_ * 16000), int(en_ * 16000)
        pc = x[max(0, a):min(len(x), b)]
        if pc.size >= int(0.6 * 16000):
            pieces.append(pc)
    if len(pieces) < 2:
        return None                      # cannot judge; treat as unknown, not as mixed
    half = len(pieces) // 2

    def emb(ch):
        if not ch:
            return None
        audio = np.concatenate(ch)[: int(EMBED_CAP_SEC * 16000)]
        if audio.size < int(0.6 * 16000):
            return None
        import sherpa_onnx
        ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=4,
                                                        provider="cpu"))
        st = ex.create_stream()
        st.accept_waveform(16000, audio)
        st.input_finished()
        v = np.asarray(ex.compute(st), dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n else None

    v1, v2 = emb(pieces[:half]), emb(pieces[half:])
    if v1 is None or v2 is None:
        return None
    return float(np.dot(v1, v2))


def consolidate(x: np.ndarray, turns: list[dict], cfg: dict) -> tuple[list[dict], dict]:
    """Repair the two ways diarization over-splits a long meeting.

    On 30-second clips the clusterer is near-perfect. On a 33-minute call it splits a
    single person across several clusters (measured: three clusters for one speaker
    with mutual voice similarity 0.89-0.91) and emits a tail of one-second fragments.

    So: merge clusters whose voice embeddings actually agree, using average linkage
    (single linkage chains unrelated speakers together), then fold small leftovers into
    whoever they match. A fragment that matches nobody becomes one shared
    "unidentified" bucket rather than an invented Speaker 34 — never a wrong name.

    Returns (turns with rewritten cluster ids ordered by speaking time, stats).
    """
    if not turns:
        return turns, {}

    ids = sorted(cluster_durations(turns))
    vecs = {c: embed_cluster(x, turns, c) for c in ids}
    ids = [c for c in ids if vecs[c] is not None]
    if not ids:
        return turns, {}

    def sim(a: int, b: int) -> float:
        return float(np.dot(vecs[a], vecs[b]))

    # average-linkage agglomerative merge over cluster embeddings
    groups: list[list[int]] = [[c] for c in ids]
    thr = float(cfg["merge_similarity"])
    merged = True
    while merged and len(groups) > 1:
        merged = False
        best, pair = thr, None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                pairs = [sim(a, b) for a in groups[i] for b in groups[j]]
                avg = sum(pairs) / len(pairs)
                if avg >= best:
                    best, pair = avg, (i, j)
        if pair:
            i, j = pair
            groups[i] = groups[i] + groups[j]
            del groups[j]
            merged = True

    remap = {c: gi for gi, g in enumerate(groups) for c in g}
    staged = [{**t, "cluster": remap.get(t["cluster"], t["cluster"])} for t in turns]

    # fold away groups too small to be a real participant
    gdur = cluster_durations(staged)
    min_sec = float(cfg["min_speaker_seconds"])
    big = [g for g in sorted(gdur, key=lambda k: -gdur[k]) if gdur[g] >= min_sec]
    small = [g for g in gdur if gdur[g] < min_sec]
    absorbed = unknown = 0
    if big and small:
        gvec = {g: embed_cluster(x, staged, g) for g in set(big) | set(small)}
        for g in small:
            v = gvec.get(g)
            cand = {b: float(np.dot(v, gvec[b])) for b in big if v is not None and gvec.get(b) is not None}
            if cand:
                b = max(cand, key=cand.get)
                if cand[b] >= float(cfg["absorb_similarity"]):
                    remap.update({c: b for c in groups[g]})
                    absorbed += 1
                    continue
            remap.update({c: UNKNOWN for c in groups[g]})
            unknown += 1
        staged = [{**t, "cluster": remap.get(t["cluster"], t["cluster"])} for t in turns]

    # renumber so Speaker 1 is whoever talked most
    fdur = cluster_durations(staged)
    order = [g for g in sorted(fdur, key=lambda k: -fdur[k]) if g != UNKNOWN]
    final = {g: i for i, g in enumerate(order)}
    final[UNKNOWN] = UNKNOWN
    out = [{**t, "cluster": final.get(t["cluster"], t["cluster"])} for t in staged]
    return out, {"raw": len(ids), "merged_to": len(groups),
                 "speakers": len(order), "absorbed": absorbed, "unknown": unknown}


NONSPEECH = re.compile(r"^\s*[\(\[\*][^)\]\*]*[\)\]\*]\s*$")
NONSPEECH_WORDS = {"blank_audio", "silence", "music", "applause", "inaudible",
                   "noise", "beep", "clicking", "static", "background noise"}


def strip_artifacts(segs: list[dict], prompt: str | None) -> tuple[list[dict], int]:
    """Remove things whisper emits that nobody said.

    Two kinds. Sound captions — "(air whooshing)", "[BLANK_AUDIO]" — which it writes
    over noise. And regurgitated prompt: given a glossary prompt, on ambiguous audio it
    will echo the prompt's own wording back as if spoken, so a leading label like
    "Terms:" gets prefixed to real speech.
    """
    labels = []
    if prompt:
        labels = [m.group(0) for m in re.finditer(r"\b[A-Z][A-Za-z ]{2,20}:", prompt)]
    out, dropped = [], 0
    for sg in segs:
        t = sg["text"].strip()
        for lab in labels:                       # "Terms:" leaked in front of real words
            if t.startswith(lab):
                t = t[len(lab):].lstrip()
        bare = re.sub(r"[\(\[\*][^)\]\*]*[\)\]\*]", "", t).strip()
        key = re.sub(r"[^a-z_ ]+", "", t.lower()).strip()
        if not bare or NONSPEECH.match(t) or key in NONSPEECH_WORDS:
            dropped += 1
            continue
        out.append({**sg, "text": bare})
    return out, dropped


def drop_bleed(timeline: list[dict], thr: float = 0.55) -> tuple[list[dict], int]:
    """Drop mic lines that are just the far side leaking in through the speakers.

    Without headphones the mic hears the call audio, so whisper transcribes a degraded
    copy of what the other side said and it lands under the mic owner's name. Any mic
    segment that closely matches an overlapping system segment is bleed, not speech.
    """
    import difflib
    sysseg = [e for e in timeline if e.get("track") == "system"]
    if not sysseg:
        return timeline, 0
    out, dropped = [], 0
    for e in timeline:
        if e.get("track") != "mic":
            out.append(e)
            continue
        a, b = e["start"], e["end"]
        mine = re.sub(r"[^a-z0-9 ]+", " ", e["text"].lower()).split()
        if len(mine) < 8:
            out.append(e)          # "Yeah." matches everything; too short to judge
            continue
        best = 0.0
        for c in sysseg:
            if min(b, c["end"]) - max(a, c["start"]) <= 0:
                continue
            theirs = re.sub(r"[^a-z0-9 ]+", " ", c["text"].lower()).split()
            if not mine or not theirs:
                continue
            best = max(best, difflib.SequenceMatcher(None, mine, theirs).ratio())
        if best >= thr:
            dropped += 1
            continue
        out.append(e)
    return out, dropped


def merge_intervals(turns: list[dict], gap: float = 0.05) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in sorted((t["start"], t["end"]) for t in turns):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def snap_to_speech(segs: list[dict], turns: list[dict],
                   pad: float = 0.05) -> tuple[list[dict], int]:
    """Reconcile whisper's timings against detected speech.

    Two separate problems, one fix:
      * whisper stretches segments across silence — its first segment usually claims
        t=0 no matter when speech actually starts, which misorders the two tracks.
      * whisper hallucinates on silence, emitting filler like "So, let's go." or
        "Thank you." A track where nobody speaks must produce nothing, not an
        invented line attributed to a real person.

    Segments overlapping speech get pulled onto it; segments overlapping no speech at
    all are dropped. Returns (segments, dropped_count).
    """
    iv = merge_intervals(turns)
    if not iv:
        return segs, 0
    out, dropped = [], 0
    for s in segs:
        a, b = s["start"], s["end"]
        ov = [(x, y) for x, y in iv if min(b, y) - max(a, x) > 0]
        if not ov:
            dropped += 1          # no speech under it -> whisper made it up
            continue
        na, nb = max(a, ov[0][0] - pad), min(b, ov[-1][1] + pad)
        if nb <= na:
            na, nb = a, b
        out.append({**s, "start": na, "end": nb})
    return out, dropped


def assign(segs: list[dict], turns: list[dict]) -> list[dict]:
    """Attach each transcript segment to the diarization cluster it overlaps most."""
    out = []
    for s in segs:
        acc: dict[int, float] = {}
        for t in turns:
            ov = min(s["end"], t["end"]) - max(s["start"], t["start"])
            if ov > 0:
                acc[t["cluster"]] = acc.get(t["cluster"], 0.0) + ov
        if acc:
            cl = max(acc, key=acc.get)
            span = max(1e-6, s["end"] - s["start"])
            conf = min(1.0, acc[cl] / span)
        else:
            # diarization found nothing here; fall back to the nearest turn
            near = min(turns, key=lambda t: min(abs(t["start"] - s["end"]), abs(s["start"] - t["end"])),
                       default=None) if turns else None
            cl = near["cluster"] if near else -1
            conf = 0.0
        out.append({**s, "cluster": cl, "overlap": round(conf, 3)})
    return out


def render_markdown(session: Path, meta: dict, timeline: list[dict], names: dict) -> str:
    provisional = sorted({e["speaker_key"] for e in timeline
                          if names.get(e["speaker_key"], {}).get("confidence") != "verified"})
    L = []
    L.append(f"# {meta.get('title') or session.name}")
    L.append("")
    L.append(f"- **Recorded:** {meta.get('started_at', 'unknown')}")
    L.append(f"- **Duration:** {hhmmss(meta.get('duration', 0))}")
    if meta.get("customer"):
        L.append(f"- **Customer:** {meta['customer']}")
    L.append(f"- **Source:** {meta.get('source', 'mtcapture dual-track')}")
    tl = meta.get("mic_timeline") or []
    lost = next((e for e in tl if e.get("reason") == "disconnected"), None)
    if meta.get("mic"):
        L.append(f"- **Microphone:** {meta['mic']}"
                 + (f" — ⚠️ **disconnected at {hhmmss(lost.get('t', 0))}**" if lost else ""))
    L.append(f"- **Transcribed by:** whisper.cpp `{WHISPER_MODEL.name}` (local)")
    L.append(f"- **Speakers detected:** {len({e['speaker_key'] for e in timeline})}")
    L.append("")

    roster = []
    for k in sorted({e["speaker_key"] for e in timeline}):
        info = names.get(k, {})
        label = info.get("name", k)
        bits = []
        if info.get("source"):
            bits.append(info["source"])
        if info.get("score") is not None:
            bits.append(f"match {info['score']:.2f}")
        if info.get("confidence") != "verified":
            bits.append("**unconfirmed**")
        roster.append(f"| {label} | `{k}` | {', '.join(bits) or '—'} |")
    if roster:
        L += ["## Speakers", "", "| Name | Key | Basis |", "|---|---|---|"] + roster + [""]

    seen = meta.get("onscreen") or []
    if seen:
        L += ["## Seen on screen", "",
              "Read off the meeting window during the call and matched against your "
              "contacts. This is who was *present* — it does not by itself say which "
              "voice is which.", "",
              "| Name | Role | Frames |", "|---|---|---|"]
        L += [f"| {r['name']} | {r.get('role','')[:60]} | {r['frames']} |" for r in seen]
        L.append("")

    if meta.get("system_dead_at") is not None:
        L.append(f"> 🚨 **No system audio after {hhmmss(meta['system_dead_at'])}, "
                 f"and it never came back before the recording ended.** That pattern "
                 f"suggests the capture died rather than the room going quiet, so the far "
                 f"side may be missing from there on. Worth checking before you rely on "
                 f"this. Your own mic was unaffected.")
        L.append("")

    tk = meta.get("tracks") or {}
    if tk.get("verdict") == "same_room":
        used = meta.get("tracks_used") or []
        cmd = f"`mt process {workspace_rel(session)} --no-mic`"
        if used == ["system"]:
            L.append("> 🔁 **Same room audio on both tracks.** This transcript uses the "
                     "Teams (system) track only, which carries everyone.")
        elif used == ["mic"]:
            L.append(f"> 🔁 **The Teams track carries this whole meeting too.** This "
                     f"transcript uses the room mic. The Teams track may be cleaner: "
                     f"{cmd}, or the button on the review page.")
        else:
            L.append(f"> 🔁 **Same room audio on both tracks, so lines may appear twice.** "
                     f"The Teams (system) track alone carries everyone. Re-process with "
                     f"it only: {cmd}, or the button on the review page.")
        L.append("")
    elif tk.get("verdict") == "overlap" and len(meta.get("tracks_used") or []) == 2:
        fix = (f" Re-process with the room mic only: `mt process {workspace_rel(session)} "
               f"--in-person --no-system`, or the button on the review page."
               if tk.get("recommend") == "mic_only" else "")
        L.append(f"> 🔁 **Some lines may appear twice.** {tk.get('headline', '')}{fix}")
        L.append("")

    if lost:
        L.append(f"> ⚠️ **Your microphone ({meta.get('mic')}) was unplugged at "
                 f"{hhmmss(lost.get('t', 0))}.** macOS cannot hand a running recording a "
                 f"different mic, so your own side is probably missing from that point on. "
                 f"The far side is unaffected.")
        L.append("")

    if provisional:
        L.append("> ⚠️ Speaker names marked **unconfirmed** are the tool's best guess and have "
                 "**not** been verified against your contacts. Confirm them, then edit "
                 "`speakers.json` and run `mt relabel` before this transcript is quoted, "
                 "attributed, or used in a customer-facing artifact.")
        L.append("")

    L += ["## Transcript", ""]
    last = None
    for e in timeline:
        # A line the user reassigned by hand wins over whatever the cluster is called.
        # Diarization will always mislabel some lines; this is the escape hatch.
        label = e.get("override") or names.get(e["speaker_key"], {}).get("name", e["speaker_key"])
        if label != last:
            L.append("")
            L.append(f"**{label}**  `[{hhmmss(e['start'])}]`")
            last = label
        L.append(e["text"])
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- commands

# ---------------------------------------------------------------- live streaming

def wav_data_offset(p: Path) -> int:
    """Byte offset of PCM data. A WAV still being written has a bogus size field in the
    header, so the tail is read by file length rather than by what the header claims."""
    with open(p, "rb") as f:
        head = f.read(8192)
    i = head.find(b"data")
    return i + 8 if i > 0 else 44


def read_tail(p: Path, data_off: int, consumed: int) -> np.ndarray:
    """Samples appended since `consumed` (16-bit mono assumed)."""
    total = (p.stat().st_size - data_off) // 2
    if total <= consumed:
        return np.empty(0, dtype=np.float32)
    with open(p, "rb") as f:
        f.seek(data_off + consumed * 2)
        raw = f.read((total - consumed) * 2)
    n = len(raw) // 2
    if n == 0:
        return np.empty(0, dtype=np.float32)
    return np.frombuffer(raw[: n * 2], dtype=np.int16).astype(np.float32) / 32768.0


# ── salvaging a force-killed recording ───────────────────────────────────────
# mtcapture writes the frame count into the WAV header when it closes the file. A
# recorder that had to be SIGKILLed never reaches that point, so the data chunk claims
# zero bytes and a strict reader sees an empty file — even though every sample is sitting
# on disk immediately after the header. A forced stop must never turn a recoverable
# recording into a lost one, so `mt process` repairs the header before deciding a session
# has no audio in it.


def _wav_layout(head: bytes, size: int) -> dict | None:
    """Where the PCM starts, what the header claims is there, and the byte rate."""
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    off, rate, out = 12, 0, None
    while off + 8 <= len(head):
        cid = head[off:off + 4]
        sz = int.from_bytes(head[off + 4:off + 8], "little")
        if cid == b"fmt " and off + 16 <= len(head):
            rate = int.from_bytes(head[off + 16:off + 20], "little")   # avg bytes/sec
        if cid == b"data":
            out = {"chunk_at": off, "declared": sz, "payload": off + 8}
            break
        nxt = off + 8 + sz + (sz & 1)
        if nxt <= off or nxt > size:          # a nonsense length; stop guessing
            return None
        off = nxt
    if out is None:
        return None
    out["rate"] = rate or 32000               # 16 kHz mono 16-bit, what mtcapture writes
    out["riff"] = int.from_bytes(head[4:8], "little")
    return out


def _same_dir(a, b) -> bool:
    """Same directory, symlinks and /private/var vs /var and all."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, TypeError, ValueError):
        return str(a) == str(b)


def repair_wav_header(p: Path) -> str | None:
    """Finalize an unfinalized WAV in place, without touching a single sample.

    Only the two 32-bit size fields are rewritten — the RIFF length and the data chunk
    length. The PCM is never read, moved, re-encoded or truncated, so there is nothing
    in here that can shorten or damage a recording. Returns a note if it changed
    anything, None if the file was already fine or is not ours to fix.
    """
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            head = f.read(65536)
    except OSError:
        return None
    lay = _wav_layout(head, size)
    if lay is None:
        return None
    available = max(0, size - lay["payload"])
    available -= available % 2                      # whole 16-bit frames only
    if available == 0:
        return None
    if lay["declared"] == available and lay["riff"] == size - 8:
        return None                                 # already finalized
    # Refuse to shrink a header that is already describing real audio: the only sizes we
    # ever write are "exactly what is on disk", and that is never less than the truth.
    if 0 < lay["declared"] < available and lay["riff"] == size - 8:
        return None
    try:
        with open(p, "r+b") as f:
            f.seek(4)
            f.write((lay["payload"] + available - 8).to_bytes(4, "little"))
            f.seek(lay["chunk_at"] + 4)
            f.write(available.to_bytes(4, "little"))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        return None
    secs = available / float(lay["rate"])
    was = "unfinalized (the recorder was force-killed)" if lay["declared"] == 0 else \
          f"claimed {lay['declared']} bytes"
    return f"repaired {p.name}: header was {was} — {secs:.1f}s of audio recovered"


def salvage_track(p: Path) -> tuple[Path, str | None]:
    """Make a force-killed track readable. Returns the path to use, and what was done."""
    note = repair_wav_header(p)
    if duration_of(p) > 0.5:
        return p, note
    # The header is past repairing, but PCM is still PCM. Hand the raw bytes to ffmpeg in
    # the format mtcapture always writes, and keep the result beside the original — the
    # original is never overwritten, so a bad guess here costs nothing.
    try:
        off = wav_data_offset(p)
        if p.stat().st_size - off < 32000:          # under a second; nothing worth saving
            return p, note
        out = p.with_name(p.stem + ".salvaged.wav")
        with open(p, "rb") as f:
            f.seek(off)
            r = subprocess.run([need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0",
                                "-c:a", "pcm_s16le", str(out)],
                               stdin=f, capture_output=True, timeout=600)
        if r.returncode == 0 and duration_of(out) > 0.5:
            return out, (f"{p.name} had an unreadable header — recovered "
                         f"{duration_of(out):.1f}s of raw audio into {out.name}")
    except Exception:
        pass
    return p, note


# Live keeps up or skips — it never falls behind. Oct 7 CAB: with Meet presenting and
# annotating, one small.en run took 60+ s (shared GPU), each chunk then swallowed the whole
# backlog (cut_at_silence finds the LATEST pause), and the next was bigger still: 4.5 min
# behind at minute 18, 6-minute chunks by minute 24, and every line in a chunk wore the one
# name taken from its first minute. Live is disposable — the full pass re-reads every word
# from the FLAC — so being current beats being complete. Idle, a 20 s chunk costs ~2 s.
LIVE_MAX_CHUNK = 20.0    # a chunk ends at the last pause inside this, else is cut here
LIVE_MAX_LAG = 30.0      # unread audio past this is skipped (live view only)
LIVE_RESUME = 10.0       # …and live picks up again this far behind "now"


class LaughSampler:
    """Best Saying's laughter tagger, OFF the live loop. It was the costliest step per chunk
    (0.5 s per 20 s idle, CPU-bound, worse under call load). One chunk at a time: a chunk
    that arrives while the last is still being tagged is not tagged at all, so live laughs
    are a sample — the after-call pass tags every second of the FLAC anyway."""

    def __init__(self, d: Path):
        self.d, self.busy, self.sampled, self.dropped = d, False, 0, 0

    def offer(self, kind: str, at: float, wav: Path) -> None:
        if self.busy:
            self.dropped += 1
            return
        own = wav.with_name(wav.stem + "_laugh.wav")   # the loop rewrites `wav` next chunk
        try:
            shutil.copyfile(wav, own)
        except OSError:
            return
        self.busy = True
        threading.Thread(target=self._run, args=(kind, at, own), daemon=True).start()

    def _run(self, kind: str, at: float, wav: Path) -> None:
        try:
            import awards
            awards.live_laughs(self.d, kind, at, wav)
            self.sampled += 1
        except Exception:
            pass
        finally:
            self.busy = False


def cut_at_silence(x: np.ndarray, sr: int = 16000, frame: float = 0.03,
                   min_sil: float = 0.35) -> int | None:
    """Latest point we can cut without slicing through a word.

    Energy-based on purpose: this runs every few seconds, and loading the neural
    segmenter each tick would cost more than the transcription itself.
    """
    fw = int(sr * frame)
    if len(x) < fw * 8:
        return None
    e = np.array([np.sqrt((x[i:i + fw] ** 2).mean()) for i in range(0, len(x) - fw, fw)])
    if e.size == 0:
        return None
    thr = max(0.006, float(np.percentile(e, 20)) * 2.5)
    need, run, cut = int(min_sil / frame), 0, None
    for i, v in enumerate(e):
        if v < thr:
            run += 1
            if run >= need:
                cut = (i + 1) * fw
        else:
            run = 0
    return cut


# ---------------------------------------------------------------- the recorder's voice

class RecorderVoice:
    """The recorder's stderr, surfaced in the live view instead of discarded.

    Three faults cost 7 minutes of a customer call. This is the one that made it
    invisible: mtcapture printed its warning and cmd_live sent stderr to DEVNULL.

    Reading it has one hard requirement — the pipe must NEVER be left unread, because a
    full pipe blocks the recorder on its next write and stalls the capture. So the
    reading thread's only job is to read: every line handler is inside its own try, and
    all printing happens back on the main loop. If the thread cannot be started at all,
    the fd goes non-blocking and the main loop drains it instead.
    """

    WINDOW = 60.0        # rate-limit window
    PER_WINDOW = 8       # ordinary lines printed per window; the rest only go to the log
    HOT_PER_WINDOW = 20  # lines that sound like the failure jump the queue, within reason
    REPEAT = 120.0       # the same message is not re-printed inside this
    CAP = 2_000_000      # bytes of session log before it stops growing

    # The one line that mattered on Sep 16 was "note: no system audio for 5 minutes".
    # A rate limit that can bury THAT under routine chatter would reintroduce the bug it
    # is here to fix, so anything that sounds like the failure gets its own budget.
    HOT = re.compile(r"(?i)system audio|recaptur|probe|declined|denied|TCC|error|fail|"
                     r"warn|stopped|silent|silence")

    def __init__(self, proc, logfile: Path | None = None):
        self.proc, self.logfile = proc, logfile
        self.q: deque[str] = deque(maxlen=200)
        self.lock = threading.Lock()
        self.seen: dict[str, float] = {}
        self.win0 = 0.0
        self.n = 0
        self.hot_n = 0
        self.suppressed = 0
        self.written = 0
        self.thread = None
        self.polling = False

    def start(self) -> None:
        if getattr(self.proc, "stderr", None) is None:
            return
        try:
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
            return
        except Exception:
            self.thread = None
        try:
            os.set_blocking(self.proc.stderr.fileno(), False)
            self.polling = True
        except Exception:
            self.polling = False

    def _read(self) -> None:
        try:
            for raw in self.proc.stderr:
                try:
                    self._line(raw)
                except Exception:
                    pass                  # one bad line never stops us reading the pipe
        except Exception:
            pass
        finally:
            try:
                self.proc.stderr.close()
            except Exception:
                pass

    def _log(self, text: str) -> None:
        if self.logfile is None or self.written >= self.CAP:
            return
        try:
            with open(self.logfile, "a") as f:
                f.write(text + "\n")
            self.written += len(text) + 1
        except Exception:
            self.logfile = None           # one failure is enough; never retry mid-call

    def _line(self, raw) -> None:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        text = text.rstrip("\r\n")
        self._log(text)
        msg = text.strip()
        if not msg:
            return
        if msg.startswith("[mtcapture]"):
            msg = msg[len("[mtcapture]"):].strip() or msg
        now = time.time()
        with self.lock:
            if now - self.win0 >= self.WINDOW:
                self.win0, self.n, self.hot_n = now, 0, 0
                if self.suppressed:
                    self.q.append(f"({self.suppressed} more recorder message(s) held back "
                                  f"— all of them are in capture_stderr.log)")
                    self.suppressed = 0
            last = self.seen.get(msg)
            if last is not None and now - last < self.REPEAT:
                return
            self.seen[msg] = now
            if len(self.seen) > 200:
                self.seen = {k: v for k, v in self.seen.items() if now - v < self.REPEAT}
            hot = bool(self.HOT.search(msg))
            if hot and self.hot_n < self.HOT_PER_WINDOW:
                self.hot_n += 1
            elif self.n >= self.PER_WINDOW:
                self.suppressed += 1
                return
            else:
                self.n += 1
            self.q.append(msg[:500])

    def drain(self) -> list[str]:
        """Whatever the recorder has said since the last tick. Never blocks."""
        if self.polling:
            try:
                chunk = self.proc.stderr.read()
            except Exception:
                chunk = None
            for raw in (chunk or b"").splitlines() if chunk else []:
                try:
                    self._line(raw)
                except Exception:
                    pass
        with self.lock:
            out = list(self.q)
            self.q.clear()
        return out


class HealthBanner:
    """The persistent alert in the live transcript view.

    The user: *"a visible alert is helpful… I do look at the transcript a few times, not all
    the time."* So it is printed in full when it changes and re-printed as a one-line
    tick every 30s while it is unresolved — a line that scrolls away once is a line they
    will not see. The tick stops the moment the condition resolves or they answer.
    """

    TICK = 30.0

    def __init__(self):
        self.key = None
        self.last = 0.0
        self.alarmed = False

    def poll(self, session: Path) -> list[str]:
        try:
            h = capture_health(session)
        except Exception:
            return []                      # the recording is sacred; say nothing instead
        now = time.time()
        live = bool(h.get("alarm") or h.get("ask"))
        if h.get("alarm"):
            self.alarmed = True
        if live and h.get("key") != self.key:
            self.key, self.last = h.get("key"), now
            return self._banner(h)
        if live and now - self.last >= self.TICK:
            self.last = now
            return [self._tick(h)]
        if not live and self.key is not None:
            self.key, self.last = None, 0.0
            return ["", self._clear(h), ""]
        return []

    @staticmethod
    def _banner(h: dict) -> list[str]:
        rule = "─" * 68
        L = ["", f"{YEL}{rule}{RST}",
             f"{YEL}{BOLD}  ⚠  {h.get('title', '').upper()}{RST}"]
        if h.get("sinceText"):
            L.append(f"{YEL}     first flagged at {h['sinceText']} into the recording{RST}")
        for ln in textwrap.wrap(h.get("detail", ""), 62):
            L.append(f"{YEL}     {ln}{RST}")
        if h.get("question"):
            L.append("")
            L.append(f"{YEL}{BOLD}     {h['question']}{RST}")
            L.append(f"{YEL}     Answer Yes/No in `mt web` — that is where the answer "
                     f"is recorded.{RST}")
        if h.get("alarm"):
            L.append("")
            L.append(f"{YEL}     Rebuild the audio tap without stopping the recording:{RST}")
            L.append(f"{YEL}{BOLD}       mt recapture{RST}"
                     f"{YEL}   (or the button in `mt web`){RST}")
        L += [f"{YEL}{rule}{RST}", ""]
        return L

    @staticmethod
    def _tick(h: dict) -> str:
        what = ("system audio still unconfirmed" if h.get("ask")
                else "system audio still NOT being recorded")
        return (f"{YEL}   ⚠ {what} — answer in `mt web`, or run `mt recapture`{RST}")

    @staticmethod
    def _clear(h: dict) -> str:
        if h.get("dismissed"):
            return f"{DIM}   audio-tap alert dismissed — the recording carries on{RST}"
        if h.get("state") == "recovered":
            return (f"{GRN}   ✓ system audio came back"
                    + (f" at {h['atText']}" if h.get("atText") else "") + f"{RST}")
        return f"{GRN}   ✓ audio-tap check cleared — the cue was captured, the tap is alive{RST}"


def cmd_live(a):
    refuse_if_recording(a)
    """Rolling transcript during the call, then the full-quality pass — queued, so it
    never competes with the next recording (passqueue.py), or inline with --no-queue."""
    # Pause first, then capture: an earlier meeting's pass still running is frozen before
    # this recorder asks the machine for anything.
    try:
        rec_mark = passqueue.recorder_started()
    except Exception as e:
        rec_mark = None
        warn(f"could not pause queued processing ({e})")
    try:
        return _live(a, rec_mark)
    finally:
        try:
            passqueue.recorder_stopped(rec_mark)
        except Exception:
            pass


def _job_args(a) -> dict:
    """The recording's options as plain JSON, to re-create its Namespace in a queued job."""
    return {k: v for k, v in vars(a).items()
            if k != "f" and isinstance(v, (str, int, float, bool, type(None)))}


def _live(a, rec_mark=None):
    cfg = config()
    if a.language:
        cfg["language"] = a.language
    if a.prompt:
        cfg["prompt"] = a.prompt
    if not CAPTURE.exists():
        fail("mtcapture is not built — run ./make.sh")

    model = MODELS / (a.live_model or cfg.get("live_model", "ggml-small.en-q5_1.bin"))
    if not model.exists():
        fail(f"live model missing: {model}\nRun ./make.sh, or set live_model in config.json")

    d = session_dir(a, cfg)
    work = d / "work"
    work.mkdir(exist_ok=True)
    # How this call was recorded, so it can be processed the same way later — by the queue,
    # or by `mt queue add` if it never was (PLAN.md Phase 2b, "recorded, never transcribed").
    try:
        (d / "record_options.json").write_text(json.dumps(_job_args(a), indent=2) + "\n")
    except OSError:
        pass
    owner = a.owner or cfg["owner"]
    people = load_voiceprints()
    # Vocal cues (VOCAL-CUES-PLAN.md): the record-time checkbox. Opting in here also means
    # the meeting is measured once the full pass has finished.
    want_cues, cue_warned, cues_mod = bool(getattr(a, "cues", False)), False, None
    laughs = LaughSampler(d)
    # Live naming (livenames.py) works on any live recording: the Room panel writes your
    # corrections into the session and this loop reads them back each chunk.
    try:
        import livenames as livenames_mod
    except Exception:
        livenames_mod = None
    if want_cues:
        try:
            import cues as cues_mod
            cues_mod.request(d)
        except Exception as e:
            warn(f"vocal cues unavailable this call: {e}")
            want_cues = False

    cmd = [str(CAPTURE), "--out-dir", str(d)]
    step(f"recording to {d}")
    mic_uid = choose_audio(a, cfg)
    if mic_uid:
        cmd += ["--mic-id", mic_uid]
    if a.no_mic:
        cmd += ["--no-mic"]
    if a.no_system and capture_supports("--no-system-watch"):
        cmd += ["--no-system-watch"]
    if a.duration:
        cmd += ["--duration", str(a.duration)]

    say(f"{DIM}   live transcript below — Ctrl-C when the meeting ends{RST}")
    say(f"{DIM}   live model {model.name}; the final pass re-runs with {WHISPER_MODEL.name}{RST}")
    say("")
    watcher = None if a.no_screen else start_screen_watch(d, keep_frames=bool(a.keep_frames))
    # stderr is a PIPE now, not DEVNULL. The recorder's "no system audio" warning went
    # here and straight into /dev/null on Sep 16, which is why 7 minutes of a customer
    # call went missing with nobody the wiser. RecorderVoice drains it on its own thread
    # and the main loop prints what it has to say, rate-limited so a flood cannot bury
    # the transcript the user is reading to follow the meeting.
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    voice = RecorderVoice(proc, d / "capture_stderr.log")
    voice.start()
    banner = HealthBanner()

    tracks: dict[str, dict] = {}
    live_md = d / "live.md"
    lines: list[str] = []
    t0 = time.time()
    interrupted = False

    def flush_line(kind: str, at: float, text: str, who: str) -> None:
        stamp = hhmmss(at)
        colour = GRN if kind == "mic" else ""
        print(f"{DIM}[{stamp}]{RST} {colour}{BOLD}{who}{RST}  {text}", flush=True)
        lines.append(f"**{who}** `[{stamp}]`  {text}")

    try:
        while True:
            if proc.poll() is not None:
                break
            # Anything the recorder said, then the standing alert. Both are wrapped:
            # nothing about reporting a problem may ever be able to stop the recording.
            try:
                for ln in voice.drain():
                    print(f"{YEL}   recorder:{RST} {ln}", flush=True)
                for ln in banner.poll(d):
                    print(ln, flush=True)
            except Exception:
                pass
            for kind in ("system", "mic"):
                f = d / f"{kind}.wav"
                if not f.exists() or f.stat().st_size < 8192:
                    continue
                st = tracks.setdefault(kind, {"off": wav_data_offset(f), "consumed": 0})
                x = read_tail(f, st["off"], st["consumed"])
                if x.size > int(LIVE_MAX_LAG * 16000):
                    # Behind: jump to a pause near "now" rather than chew through the pile.
                    keep = int(LIVE_RESUME * 16000)
                    jump = cut_at_silence(x[: x.size - keep]) or (x.size - keep)
                    heard = float(np.abs(x[:jump]).max()) >= 0.01   # skipping silence is no news
                    st["consumed"] += jump
                    x = x[jump:]
                    if heard:
                        gap = f"{int(jump / 16000) // 60}:{int(jump / 16000) % 60:02d}"
                        msg = (f"⏩ live skipped {gap} of "
                               f"{'your mic' if kind == 'mic' else 'the far end'}"
                               f" to stay current — the full pass keeps every word")
                        print(f"{DIM}   {msg}{RST}", flush=True)
                        lines.append(f"_{msg}_")
                if x.size < 16000 * 4:                      # wait for at least 4 s
                    continue
                cut = cut_at_silence(x[: int(LIVE_MAX_CHUNK * 16000)])
                if cut is None:
                    if x.size < int(LIVE_MAX_CHUNK * 16000):  # still mid-sentence; hold
                        continue
                    cut = int(LIVE_MAX_CHUNK * 16000)       # no pause in 20 s: force it
                chunk = x[:cut]
                at = st["consumed"] / 16000.0
                st["consumed"] += cut
                if float(np.abs(chunk).max() if chunk.size else 0) < 0.01:
                    continue                                # nothing but room noise

                tmp = work / f"live_{kind}.wav"
                write_wav16k(tmp, chunk)
                try:
                    segs = transcribe(tmp, work, {**cfg, "threads": cfg["threads"],
                                                  "model_override": str(model)})
                except subprocess.CalledProcessError:
                    continue
                segs, _ = strip_artifacts(segs, cfg.get("prompt"))
                text = " ".join(sg["text"] for sg in segs).strip()
                if not text:
                    continue
                text = re.sub(r"\s+", " ", text)

                who = owner if kind == "mic" else "Them"
                cue_who, cue_hint, cue_match = (owner if kind == "mic" else None), None, None
                live_named = livenames_mod is not None and (d / livenames_mod.FILE).exists()
                if kind == "system" and (people or live_named) and chunk.size > 16000 * 1.5:
                    # The loaded-once extractor: embed_audio() reloads the model every call.
                    v = None
                    if livenames_mod is not None:
                        try:
                            v = livenames_mod.embed(chunk)
                            v = None if v is None else np.asarray(v, np.float32)
                        except Exception:
                            v = None
                    if v is None:
                        v = embed_audio(chunk)
                    if v is not None:
                        nm, sc = match_voice(v, people) if people else (None, -1.0)
                        cue_match = sc
                        tentative = getattr(livenames_mod, "LIVE_TENTATIVE", 1.0)
                        if sc >= cfg["auto_assign_score"]:
                            who = cue_who = nm
                        elif sc >= tentative:
                            who = f"Them ({nm}?)"         # early guess; firms up at 0.62
                            cue_hint = nm
                        elif sc >= cfg["suggest_score"]:
                            who = f"Them (~{nm})"
                            cue_hint = nm
                        # Live naming: a voice you named during this call outranks the
                        # stored prints (Oct 6: one contact kept matching another at 0.62).
                        if live_named:
                            try:
                                lnm, lsc = livenames_mod.match(d, v)
                                if lnm and lsc >= livenames_mod.LIVE_MATCH and lsc >= sc:
                                    who = cue_who = lnm
                                    cue_hint, cue_match = None, lsc
                                elif lnm and lsc >= tentative and lsc >= sc:
                                    who = f"Them ({lnm}?)"
                                    cue_who, cue_hint, cue_match = None, lnm, lsc
                                else:
                                    w2, h2 = livenames_mod.resolve(d, at, at + chunk.size / 16000,
                                                                   cue_who, cue_hint)
                                    if w2 and w2 != cue_who:
                                        who = cue_who = w2
                                        cue_hint = h2
                            except Exception:
                                pass
                flush_line(kind, at, text, who)
                # Vocal cues, only when opted in. Wrapped: a measurement can never cost
                # the live transcript a line, and it runs after the line is printed.
                if want_cues:
                    try:
                        cues_mod.append_live(d, kind, at, segs, tmp, cue_who,
                                             hint=cue_hint, match=cue_match, people=people)
                        laughs.offer(kind, at, tmp)     # laughter → Best Saying, sampled
                    except Exception as e:
                        if not cue_warned:
                            say(f"{DIM}   vocal cues: skipped a chunk ({e}){RST}")
                            cue_warned = True
            time.sleep(2.0)
    except KeyboardInterrupt:
        interrupted = True
        say("")
        step("stopping capture…")
    finally:
        # The recorder first: it is the one holding the audio files open, and every
        # second it stays up is a second the user cannot start their next meeting. Signalling
        # and moving on is what left an orphan running for five minutes on Sep 17 — this
        # escalates and does not return until each process is genuinely gone.
        for pr, who in ((proc, "mtcapture"), (watcher, "mtwatch")):
            if pr is not None and pr.poll() is None:
                report_stop(force_stop(pr=pr, label=who), who)
        try:
            passqueue.recorder_stopped(rec_mark)   # the machine is free; settle clock starts
        except Exception:
            pass
        # Last word: anything the recorder said on its way out, and one more read of the
        # event log so a failure flagged in the final seconds still reaches live.md.
        try:
            for ln in voice.drain():
                print(f"{YEL}   recorder:{RST} {ln}", flush=True)
            banner.poll(d)
        except Exception:
            pass
        if lines:
            head = ("# Live transcript (rough)\n\n"
                    "Written during the call with a fast model. "
                    "`transcript.md` is the accurate version.\n\n")
            if banner.alarmed:
                head += ("> ⚠️ **The system-audio tap was reported dead during this "
                         "call.** Part of the far side may be missing from the system "
                         "track. `capture_events.jsonl` in this folder has the "
                         "timeline.\n\n")
            live_md.write_text(head + "\n\n".join(lines) + "\n")
            say(f"{DIM}   live notes: {live_md}{RST}")

    say("")
    if banner.alarmed:
        warn("the system-audio tap was reported dead during this recording — check "
             f"{d / 'capture_events.jsonl'} and expect a gap in the far side")
    a.path = str(d)
    a.force_asr = True
    recorded = time.time() - t0
    if not getattr(a, "no_queue", False):
        # Stop means stop: the capture is already released, and this process exits now
        # rather than spending the next 20+ minutes on the pass (Oct 21: laughter alone
        # ran 20 minutes after a customer call, and the machine crawled).
        try:
            job = passqueue.enqueue(
                d, _job_args(a), cues=want_cues, recorded_secs=round(recorded, 1),
                recorded_at=dt.datetime.fromtimestamp(t0, dt.timezone.utc)
                                       .isoformat(timespec="seconds"),
                params={"name": d.name, "customer": a.customer or "", "title": a.title or "",
                        "no_mic": bool(a.no_mic), "cues": want_cues})
            passqueue.ensure_worker()
            step(f"meeting ended after {hhmmss(recorded)} — {QUEUED_MARK}")
            say(f"{DIM}   job {job['id']} — `mt queue` shows it; it starts once nothing is "
                f"recording and the machine is quiet{RST}")
            return 0
        except Exception as e:
            warn(f"could not queue the pass ({e}); running it here instead")
    step(f"meeting ended after {hhmmss(recorded)} — running the full pass")
    return finish_recording(a, d, want_cues)


QUEUED_MARK = "queued for processing"


def finish_recording(a, d: Path, want_cues: bool, job: str | None = None) -> int:
    """Everything after the capture: the full pass, the names given live, vocal cues.

    Runs as a queued job (`mt queue job`), or straight after the call with
    `mt live --no-queue`. `a` is the recording's own Namespace, path and force_asr set.
    """
    try:
        import livenames as livenames_mod
    except Exception:
        livenames_mod = None
    rc = cmd_process(a)
    if not rc and livenames_mod is not None and (d / livenames_mod.FILE).exists():
        # Names you gave during the call pre-fill the review page.
        try:
            changes = livenames_mod.apply_after_pass(d)
            if changes:
                step("applying the names you gave during the call")
                for c in changes:
                    say(f"{DIM}   {c}{RST}")
                cmd_relabel(argparse.Namespace(session=str(d)))
        except Exception as e:
            warn(f"live names: could not pre-fill the review ({e}); they are still in "
                 f"{d / livenames_mod.FILE}")
    if want_cues and not rc:
        # Opted in at record time, so the review page should not need a second click.
        # After the pass, never before: the measurements key off the final timeline.
        try:
            import cues as cues_mod
            if job is not None:
                _wait_for_quiet(job, "vocal cues")
            step("measuring vocal cues")
            cues_mod.extract(d)
            import awards as awards_mod
            if awards_mod.tagger() is not None:
                step("listening for laughter (Best Saying)")
                awards_mod.scan(d)
        except Exception as e:
            warn(f"vocal cues: the post-call measurement failed ({e}); "
                 f"`mt cues {d}` will retry it")
    return rc


def _wait_for_quiet(job: str, what: str) -> None:
    """Hold a heavy optional step while the machine is busy — unless the user asked for
    this job now. A recording needs no check here: the worker freezes the whole job."""
    why = passqueue.machine_busy()
    if not why or passqueue.now_file(job).exists():
        return
    step(f"waiting: {why} — {what} start when it settles")
    while why and not passqueue.now_file(job).exists():
        time.sleep(passqueue.BUSY_RECHECK)
        why = passqueue.machine_busy()


def _run_job(jid: str) -> int:
    """One queued job, start to finish, in this process (the worker's child)."""
    job = passqueue.load(jid)
    if job is None:
        fail(f"no such job: {jid}")
    started = (time.time(), passqueue.cpu_seconds())
    d = Path(job["session"])
    a = argparse.Namespace(**job.get("args") or {})
    for k in OPTIONAL_ARGS:
        if not hasattr(a, k):
            setattr(a, k, None)
    a.path = str(d)
    step(f"{PASS_MARK_JOB} {d}")
    rs = job.get("recorded_secs")
    say(f"{DIM}   recorded {hhmmss(rs) if rs else '?'}, queued {job.get('queued_at', '?')}{RST}")
    rc, why = 1, ""
    try:
        rc = finish_recording(a, d, bool(job.get("cues")), job=jid)
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1
        why = str(getattr(e.code, "message", "") or "")
    except BaseException as e:
        why = f"{type(e).__name__}: {e}"
        raise
    finally:
        end = (time.time(), passqueue.cpu_seconds())
        steps = passqueue.stage_times(STEP_MARKS, end)
        res = passqueue.write_result(jid, rc, started, steps, why)
        say(f"{DIM}   took {hhmmss(res['wall_secs'])} ("
            + " · ".join(f"{s['stage']} {hhmmss(s['wall_secs'])}" for s in steps)
            + f"); cpu {hhmmss(res['cpu_secs'])}{RST}")
        step("finished" if rc == 0 else f"stopped (status {rc})")
    return rc


PASS_MARK_JOB = "running the full pass for"


def cmd_queue(a):
    """The after-call processing queue (passqueue.py)."""
    act = a.action or "list"
    if act == "run":
        return passqueue.Worker(active_capture=active_capture, log=say).run()
    if act == "job":
        return _run_job(a.target or "")
    if act == "add":
        if not a.target:
            fail("mt queue add <session folder>")
        d = Path(a.target).expanduser().resolve()
        if not ((d / "system.flac").exists() or (d / "system.wav").exists()
                or (d / "mic.flac").exists() or (d / "mic.wav").exists()):
            fail(f"no recording in {d}")
        args = {}
        try:
            args = json.loads((d / "record_options.json").read_text())
        except (OSError, ValueError):
            pass
        args.update(path=str(d), force_asr=False)
        cues = (d / "cues_requested.json").exists()
        job = passqueue.enqueue(d, args, cues=cues, params={
            "name": d.name, "customer": args.get("customer") or "",
            "title": args.get("title") or "", "no_mic": bool(args.get("no_mic")), "cues": cues})
        passqueue.ensure_worker()
        say(f"queued {job['id']}")
        return 0
    if act in ("now", "retry", "dismiss"):
        r = getattr(passqueue, {"now": "process_now"}.get(act, act))(a.target or "")
        if not r.get("ok"):
            fail(r.get("why") or "no")
        passqueue.ensure_worker()
        say(f"{act}: {a.target}")
        return 0
    st = passqueue.status()
    if not st["jobs"]:
        say("nothing queued")
        return 0
    for j in st["jobs"]:
        rs = j.get("recorded_secs")
        what = {"running": "frozen while you record" if j["paused"] else "running",
                "queued": f"#{j.get('position')}" + (" (now)" if j["now"] else ""),
                "failed": "failed — " + (j.get("why") or "")}[j["state"]]
        say(f"{j['id']}  {what}" + (f"  · {hhmmss(rs)} recorded" if rs else ""))
        if j.get("waiting"):
            say(f"{DIM}   waiting: {j['waiting']}{RST}")
    if not st["worker"] and passqueue.pending():
        say(f"{YEL}no worker is running — `mt queue run` starts one{RST}")
    return 0


def start_screen_watch(d: Path, interval: float = 2.0, keep_frames: bool = False):
    """OCR the meeting window while we record.

    Runs as its own process against whatever meeting windows exist at each tick, because
    the meeting is frequently not the front app: windows get moved, browser tabs get
    switched, and Meet drops into a pop-out. Occluded windows still capture; minimized
    or off-Space ones come back blank and are simply skipped.
    """
    if not WATCH.exists():
        warn("mtwatch not built — screen-based speaker naming disabled (run ./make.sh)")
        return None
    cmd = [str(WATCH), "--watch", "--out-dir", str(d), "--interval", str(interval)]
    # Teams titles a meeting window "<meeting> | <your org>"; your org is the first label of
    # each internal domain, so those titles count as a live call.
    for dom in P.INTERNAL_DOMAINS:
        org = dom.split(".")[0].strip()
        if org:
            cmd += ["--live-title", f"| {org}"]
    # Teams has no text label for the active speaker, only a ring around the tile. Kept
    # frames are how that ring gets measured. Opt-in per recording (--keep-frames) or
    # standing ("keep_frames": true in config.json); local only, delete when done.
    if keep_frames or config().get("keep_frames"):
        cmd += ["--keep-frames", str(d / "frames"), "--frames-every", "5"]
    try:
        # stderr goes to a file, not /dev/null: two Teams calls produced empty screen
        # logs (Sep 22, Sep 29) and the watcher's own complaints were thrown away.
        err = open(d / "screen_stderr.log", "ab")
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
    except Exception as e:
        warn(f"could not start screen watch: {e}")
        return None


def read_invitees(session: Path) -> list[str]:
    """Calendar attendees saved into the session by an earlier step."""
    f = session / "invitees.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text())
    except Exception:
        return []
    out, seen = [], set()
    for a in d.get("attendees", []):
        n = a.get("name") or a.get("email")
        if n and n not in seen:
            seen.add(n); out.append(n)
    return sorted(out)


def read_screen(d: Path, owner: str, min_frames: int = 3) -> dict:
    """What the meeting UI told us: roster, headcount, and who was talking when."""
    log = d / "screen.jsonl"
    if not log.exists():
        return {}
    try:
        sys.path.insert(0, str(HERE))
        import screen as screenmod
        res = screenmod.build(log, read_invitees(d))
        (d / "screen_roster.json").write_text(json.dumps(res, indent=2, default=list) + "\n")
    except Exception as e:
        warn(f"could not read the screen log: {e}")
        return {}

    own = re.sub(r"[^a-z ]+", " ", owner.lower()).strip()
    roster, selves = [], set()
    for name, e in res.get("people", {}).items():
        flat = re.sub(r"[^a-z ]+", " ", name.lower()).strip()
        if e.get("is_self") or flat == own:
            selves.add(name)
            continue
        if e.get("match") != "contacts" or e.get("frames", 0) < min_frames:
            continue
        roster.append({"name": name, "frames": e["frames"], "role": e.get("role", ""),
                       "first": e.get("first"), "last": e.get("last")})
    return {
        "roster": sorted(roster, key=lambda r: -r["frames"]),
        "expected": res.get("expected_participants"),
        "talking": [o for o in res.get("talking", []) if o.get("name") not in selves],
        "views": res.get("views", {}),
    }


def name_from_screen(turns: list[dict], cluster: int, talking: list[dict],
                     pad: float = 1.5) -> tuple[str | None, int, int]:
    """Whoever the meeting UI showed as speaking while this voice was speaking.

    The screen watcher and the recorder are started together, so their clocks agree to
    within about a second; `pad` absorbs that plus the UI's own lag.
    """
    spans = [(t["start"] - pad, t["end"] + pad) for t in turns if t["cluster"] == cluster]
    if not spans or not talking:
        return None, 0, 0
    votes: dict[str, int] = {}
    total = 0
    for o in talking:
        ts = o.get("t")
        if ts is None:
            continue
        if any(a <= ts <= b for a, b in spans):
            votes[o["name"]] = votes.get(o["name"], 0) + 1
            total += 1
    if not votes:
        return None, 0, 0
    best = max(votes, key=votes.get)
    return best, votes[best], total


def cmd_calendar(a):
    """Show today's calendar and prime the cache the web app reads.

    Reads macOS Calendar directly (mtcal/EventKit) — no password, no OAuth, nothing to
    unlock. `gog` stays wired up only as a fallback; its file-keyring password is not
    recoverable, so do not build on it.
    """
    import web
    step("macOS Calendar")
    ok, why, raw, sources = web.fetch_eventkit()
    if ok:
        say(f"{DIM}   sources: {', '.join(sources) or 'none'}{RST}")
        if not web.has_work_calendar(sources):
            warn("no Google calendar is attached to macOS Calendar")
            say("")
            say("   Add it once — this is the whole fix:")
            say("     System Settings → Internet Accounts → Add Account → Google")
            say("     sign in, then tick Calendars")
            say("")
        else:
            say(f"   ✓ {len(raw)} event(s) today")
        sync = web.calendar_sync()
        if sync:
            m = sync["sync_age_minutes"]
            pretty = f"{m} min" if m < 60 else f"{m // 60}h{m % 60:02d}m"
            say(f"{DIM}   last synced {pretty} ago ({sync['synced_at'][:16]}){RST}")
            if sync.get("stale_why"):
                warn(sync["stale_why"])
    else:
        warn(why.splitlines()[0] if why else "unavailable")

    r = web.refresh_calendar(interactive=True)
    if r.get("ok"):
        step(f"cached {r['count']} event(s) for today")
        say(f"{DIM}   {web.CACHE}{RST}")
        for e in web.shape_events(json.loads(web.CACHE.read_text()).get("events", []))[:25]:
            who = f"{e['attendees']} invited" if e["attendees"] else "no invitees"
            say(f"   {e['start']:>8}  {e['title'][:52]:<52} {DIM}{who}{RST}")
        return 0
    # A failed refresh is not the same as having nothing: the cache may still be good,
    # and the web app will happily serve it, so say so instead of implying Today is dead.
    say("")
    warn(r.get("why") or "could not read the calendar")
    if web.CACHE.exists():
        try:
            cached = json.loads(web.CACHE.read_text())
            n = len(cached.get("events", []))
            step(f"using the cached calendar instead — {n} event(s)")
            say(f"{DIM}   cached at {cached.get('fetched_at', 'unknown')}{RST}")
            if cached.get("source"):
                say(f"{DIM}   source: {cached['source']}{RST}")
            return 0
        except Exception:
            pass
    return 1


def cmd_web(a):
    import web
    web.serve(port=a.port, open_browser=not a.no_open)
    return 0


def cmd_version(a):
    print(f"mt {VERSION}")
    return 0


def cmd_init(a):
    """Create the workspace folders and, on a new install, an empty database.

    Never touches a database that already exists, whoever made it — see dbschema.py.
    Run by make.sh; safe to re-run.
    """
    import dbschema
    for d in (P.WORKSPACE, P.RECORDINGS, P.CUSTOMERS, P.DB.parent):
        d.mkdir(parents=True, exist_ok=True)
    say(f"   workspace  {P.WORKSPACE}  ({P.WORKSPACE_FROM})")
    if dbschema.create(P.DB):
        say(f"   {GRN}✓{RST} created  {P.DB}  (schema v{dbschema.SCHEMA_VERSION})")
    else:
        c = dbschema.check(P.DB)
        say(f"   {'✓' if c['ok'] else YEL + '!' + RST} database {P.DB}  (existing — left as is)")
        if not c["ok"]:
            say(f"     it does not match schema.sql yet; `mt doctor` lists the gaps")
    if not (HERE / "config.json").exists():
        say(f"   {DIM}no config.json — running on defaults; see config.example.json{RST}")
    return 0


def doctor_workspace() -> bool:
    """The `mt doctor` section for where data lives and whether the DB meets the contract."""
    import dbschema
    ok = True
    step("workspace & database")
    for label, path, src in P.summary():
        say(f"   · {label:<10} {path}  {DIM}({src}){RST}")
    c = dbschema.check(P.DB)
    if not c["exists"]:
        say(f"   {RED}✗{RST} no database yet — run:  {HERE / 'mt'} init")
        return False
    if c["ok"]:
        say(f"   ✓ database matches schema v{dbschema.SCHEMA_VERSION}")
    else:
        ok = False
        say(f"   {RED}✗{RST} database is missing parts of schema.sql (see docs/DATABASE.md):")
        for t in c["missing_tables"]:
            say(f"       table   {t}")
        for t, cols in c["missing_columns"].items():
            say(f"       columns {t}: {', '.join(cols)}")
    return ok


def cmd_doctor(a):
    cfg = config()
    ok = True
    say(f"{BOLD}mt {VERSION}{RST}  {DIM}{HERE}{RST}")
    step("binaries")
    for t in ("ffmpeg", "ffprobe", "whisper-cli"):
        p = shutil.which(t)
        say(f"   {'✓' if p else '✗'} {t:<12} {p or 'MISSING — brew install ' + t}")
        ok &= bool(p)
    say(f"   {'✓' if CAPTURE.exists() else '✗'} mtcapture    {CAPTURE if CAPTURE.exists() else 'MISSING — run make.sh'}")
    ok &= CAPTURE.exists()
    say(f"   {'✓' if WATCH.exists() else '○'} mtwatch      "
        + (str(WATCH) if WATCH.exists() else "not built — screen-based naming off"))
    say(f"   {'✓' if CAL.exists() else '○'} mtcal        "
        + (str(CAL) if CAL.exists() else "not built — calendar off (run ./make.sh)"))
    say(f"   {'✓' if audioprefs.available() else '○'} mtaudio      "
        + (str(audioprefs.AUDIO) if audioprefs.available()
           else "not built — speaker picker off (run ./make.sh)"))

    step("models")
    live = MODELS / cfg.get("live_model", "ggml-small.en-q5_1.bin")
    for m in (WHISPER_MODEL, live, SEG_MODEL, EMB_MODEL):
        e = m.exists()
        sz = f"{m.stat().st_size/1e6:.0f} MB" if e else "MISSING"
        say(f"   {'✓' if e else '✗'} {m.name:<42} {sz}")
        ok &= e

    step("python")
    try:
        import sherpa_onnx
        say(f"   ✓ sherpa-onnx {sherpa_onnx.__version__}")
        say(f"   ✓ numpy {np.__version__}")
    except Exception as e:
        say(f"   ✗ {e}")
        ok = False

    ok &= doctor_workspace()

    step("voiceprints")
    people = load_voiceprints()
    say(f"   {len(people)} enrolled" + (": " + ", ".join(p["name"] for p in people) if people else ""))

    step("screen & system audio recording permission")
    r = subprocess.run([str(CAPTURE), "--out-dir", str(Path(os.environ.get("TMPDIR", "/tmp")) / "mt-probe"),
                        "--no-mic", "--duration", "1"],
                       capture_output=True, text=True) if CAPTURE.exists() else None
    if r is not None and "declined TCC" in (r.stderr or ""):
        ok = False
        say(f"   {RED}✗ DENIED{RST} — this is the one thing you have to fix by hand:")
        say("")
        say("     1. Open System Settings > Privacy & Security >")
        say("        Screen & System Audio Recording")
        say("     2. Turn ON the terminal app you run Claude Code / mt in (iTerm, Terminal, VS Code)")
        say("     3. Fully QUIT that app (Cmd-Q, not just close the window) and reopen it")
        say("")
        say("     macOS never re-prompts once a request has been declined, so the toggle is the only way.")
    elif r is not None and r.returncode == 0:
        say("   ✓ granted")
    elif r is not None:
        say(f"   {YEL}? inconclusive{RST}: {(r.stderr or '').strip().splitlines()[-1:] or ''}")

    say("")
    say(f"{GRN}all good{RST}" if ok else f"{YEL}fix the ✗ items above{RST}")
    return 0 if ok else 1


def list_mics() -> list[dict]:
    if not CAPTURE.exists():
        fail("mtcapture is not built — run ./make.sh")
    r = subprocess.run([str(CAPTURE), "--list-mics"], capture_output=True, text=True)
    mics = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            mics.append({"uid": parts[0], "name": parts[1],
                         "default": len(parts) > 2 and parts[2].strip() == "default"})
    return mics


def pick_mic(cfg: dict, requested: str | None) -> tuple[str | None, str]:
    """Which microphone to use, and how that was decided.

    A laptop moves between home and the office, so the system default silently changes.
    `mic_priority` in config.json is an ordered list of name fragments — the first one
    actually plugged in wins, which keeps the good mic selected in both places without
    a per-meeting flag.
    """
    if requested:
        return requested, "you asked for it"
    mics = list_mics()
    # A pick made by hand at this desk beats the global order — see audioprefs.py.
    hit, why = audioprefs.recall("mic", mics)
    if hit:
        return hit["uid"], why
    for want in cfg.get("mic_priority") or []:
        for m in mics:
            if want.lower() in m["name"].lower():
                return m["uid"], f"first match in mic_priority ({want})"
    d = next((m for m in mics if m["default"]), None)
    if d:
        return None, f"system default ({d['name']})"
    return None, "system default"


def pick_speaker(requested: str | None) -> tuple[dict | None, str]:
    """Which output device to switch to before recording, or None to leave macOS alone.

    There is no speaker_priority: with nothing picked or remembered, the speaker is not
    mt's business. ScreenCaptureKit records the call whichever device it plays through.
    """
    outs = audioprefs.speakers()
    if requested:
        hit = (next((d for d in outs if d["uid"] == requested), None)
               or next((d for d in outs if requested.lower() in d["name"].lower()), None))
        return (hit, "you asked for it") if hit else (None, f"no speaker matches '{requested}'")
    hit, why = audioprefs.recall("speaker", outs)
    if hit:
        return hit, why
    return None, "left as is"


def choose_audio(a, cfg) -> str | None:
    """Pick the mic, remember explicit picks for this desk, and switch the speaker.

    Runs before mtcapture starts — the speaker is never touched during a recording.
    Returns the mic UID for --mic-id (None = system default). Nothing here may stop a
    recording: a memory file that cannot be written or a speaker that will not switch is
    a warning.
    """
    mic_uid, mic_why = (None, "disabled") if a.no_mic else pick_mic(cfg, a.mic_id)
    if a.mic_id and not a.no_mic:
        mics = list_mics()
        m = (next((x for x in mics if x["uid"] == a.mic_id), None)
             or next((x for x in mics if a.mic_id.lower() in x["name"].lower()), None))
        if m:
            mic_uid = m["uid"]      # a name fragment, resolved: same device, clearer log
            try:
                audioprefs.remember("mic", m["uid"], m["name"])
            except Exception as e:
                warn(f"could not remember the mic pick: {e}")
    req = getattr(a, "speaker_id", None)
    spk, spk_why = pick_speaker(req)
    if spk:
        if req:
            try:
                audioprefs.remember("speaker", spk["uid"], spk["name"])
            except Exception as e:
                warn(f"could not remember the speaker pick: {e}")
        ok, msg = audioprefs.set_output(spk["uid"])
        if not ok:
            warn(f"speaker not switched to {spk['name']}: {msg}")
    elif req:
        warn(f"speaker: {spk_why} — leaving it as is")
        spk_why = "left as is"
    if not a.no_mic:
        say(f"{DIM}   mic: {mic_label(mic_uid)}  ({mic_why}){RST}")
    cur = next((d["name"] for d in audioprefs.speakers() if d["default"]), None)
    if cur:
        say(f"{DIM}   speaker: {cur}  ({spk_why}){RST}")
    return mic_uid


def mic_label(uid: str | None) -> str:
    mics = list_mics()
    if uid:
        return next((m["name"] for m in mics if m["uid"] == uid), uid)
    return next((m["name"] for m in mics if m["default"]), "system default")


def active_session() -> Path | None:
    """The directory the running recorder is writing to, from its own argv."""
    cur = active_capture()
    return cur[1] if cur else None


def system_died_at(session: Path, min_trailing: float = 120.0) -> float | None:
    """When the system tap plausibly died — judged AFTER the fact, which is the only way.

    Live, a dead tap and a muted far end are indistinguishable: both are exact zeros.
    The user held the floor for 15 straight minutes on one real call and the system track was
    all zeros throughout, yet audio returned at minute 45 — proving the tap was fine. So
    the only honest signal is silence that runs to the END of the recording and never
    recovers. Returns the last moment real audio was seen, or None.
    """
    src = next((session / f"system{e}" for e in (".flac", ".wav")
                if (session / f"system{e}").exists()), None)
    if src is None:
        return None
    tmp = session / "work" / "deadcheck.16k.wav"
    try:
        to_16k_mono(src, tmp, normalize=False)
        x = read_wav16k(tmp)
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)
    if x.size == 0:
        return None
    nz = np.nonzero(x)[0]
    if nz.size == 0:
        return 0.0                              # never captured anything at all
    last = float(nz[-1]) / 16000.0
    total = x.size / 16000.0
    return last if (total - last) >= min_trailing else None


# ---------------------------------------------------------------- capture health
#
# The recorder has always known when the system tap went quiet. On Sep 16 that knowledge
# went two places and neither was a place anyone looks: stderr, which cmd_live sent to
# DEVNULL, and capture_events.jsonl, which nothing read. Seven minutes of a customer call
# were lost in silence.
#
# Everything below turns that file into something visible and actionable, and it is
# deliberately a PURE FUNCTION OF TWO FILES ON DISK — the recorder's append-only
# capture_events.jsonl, and the user's own answers in capture_health.json. Nothing is held
# in memory, so a page reload, a server restart, or a glance an hour later all read the
# same. Nothing here may raise into a running capture: every caller wraps it, and every
# reader degrades to "ok, nothing known" on failure.

HEALTH_FILE = "capture_health.json"      # the user's answers. NEVER the recorder's own log.
EVENTS_FILE = "capture_events.jsonl"     # the recorder's log. Append-only, never ours.

# What the recorder's own log means, in one place, so the terminal and the web app cannot
# drift into telling the user two different stories about the same event.
HEALTH_TEXT = {
    "ask": ("System audio may not be recording",
            "No system audio for several minutes while your mic was live. That is normal "
            "if you are presenting and nobody else is speaking — and it is exactly what a "
            "dead tap looks like too."),
    "dead_probe": ("System audio is NOT being recorded",
                   "A cue played on this Mac never appeared in the captured track. The tap "
                   "is dead: the far side of this call is not reaching the recording."),
    "dead_answer": ("System audio is NOT being recorded",
                    "You confirmed people are talking and nothing is reaching the system "
                    "track. Recapture rebuilds the audio tap without stopping the recording."),
    "recovered": ("System audio came back",
                  "The tap recovered. Whatever ran silent before that point is missing from "
                  "the system track — the mic track is unaffected."),
}
HEALTH_QUESTION = "Is anyone talking on the call right now?"


def _looks_like_mtcapture(args: str) -> bool:
    """Is this argv really the recorder, and not something merely mentioning it?

    A grep, an editor, or this very script can all carry the word "mtcapture" in their
    command line. Nothing is ever signalled on the strength of a substring match.
    """
    args = (args or "").strip()
    if not args:
        return False
    if args.startswith(str(CAPTURE)):
        return True
    return Path(args.split(" ", 1)[0]).name == "mtcapture"


def active_capture() -> tuple[int, Path] | None:
    """(pid, out-dir) of the running recorder, read from its own argv."""
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,args="], capture_output=True, text=True,
                             timeout=15).stdout
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if "mtcapture" not in line or "--out-dir" not in line:
            continue
        head, _, argv = line.partition(" ")
        try:
            pid = int(head)
        except ValueError:
            continue
        if not _looks_like_mtcapture(argv):
            continue
        # rejoin any path containing spaces
        rest = argv.split("--out-dir", 1)[1].strip()
        for flag in (" --mic-id", " --no-mic", " --no-system-watch", " --duration"):
            if flag in rest:
                rest = rest.split(flag)[0]
        d = Path(rest.strip())
        if d.exists():
            return pid, d
    return None


def capture_supports(flag: str) -> bool:
    """Whether the built mtcapture knows `flag`. It dies on an unknown argument, so a
    new flag must never reach a binary that predates it — a merge without ./make.sh
    would otherwise turn every such recording into a failed start."""
    try:
        out = subprocess.run([str(CAPTURE), "--help"], capture_output=True, text=True,
                             timeout=10)
    except Exception:
        return False
    return flag in (out.stdout + out.stderr)


def is_mtcapture_pid(pid) -> bool:
    """Confirm, by pid, that this process is an mtcapture. Checked before every signal."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return False
    lines = [x for x in out.splitlines() if x.strip()]
    return bool(lines) and _looks_like_mtcapture(lines[-1])


# ── stopping the recorder, for certain ───────────────────────────────────────
# Sep 17, on a real call: SCStream reported its daemon connection interrupted, mtcapture
# wedged in teardown, and mt.py signalled it and moved on. The orphan outlived the whole
# transcription pass by five minutes — active_capture() kept finding it, the UI kept
# saying "Recording 31:30", and the user could not start their next meeting. Nothing in here
# returns while the process is still alive.
#
# The intervals. A healthy mtcapture stop was measured at 0.45-0.51 s on this machine
# (stop the stream, let 400 ms of in-flight buffers land, finalize both WAV headers), and
# mtcapture now holds itself to a 4 s exit deadline from the moment a stop is requested.
#   SIGINT  8 s — long enough to outlast mtcapture's own 4 s deadline with room to spare,
#                 so the recorder always gets to finish its own clean shutdown and write
#                 its summary rather than being cut off. ~16x a healthy stop.
#   SIGTERM 4 s — the same handler inside mtcapture; this rung only matters if the SIGINT
#                 was lost or the handler was never installed (a crash during startup).
#   SIGKILL 5 s — cannot be caught or ignored. We still poll rather than assume: a thread
#                 stuck in an uninterruptible kernel wait (which is what a dead
#                 ScreenCaptureKit connection can produce) outlives even this, and that
#                 case has to be reported, not silently believed.
# None of these delays are paid by a healthy stop: each rung returns the moment the
# process is gone, which for a healthy recorder is about half a second.
STOP_WAIT_SIGINT = 8.0
STOP_WAIT_SIGTERM = 4.0
STOP_WAIT_SIGKILL = 5.0


def _is_zombie(pid) -> bool:
    """Exited but not yet reaped. os.kill(pid, 0) succeeds on one of these forever."""
    try:
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "stat="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return False
    return out.startswith("Z")


def _proc_gone(pr, pid) -> bool:
    """Is it really gone? A Popen we own reaps itself here; a stranger goes via kill(0)."""
    if pr is not None:
        return pr.poll() is not None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False              # alive, just not ours — still a blocker
    except (OSError, TypeError, ValueError):
        return True
    return _is_zombie(pid)


def _wait_gone(pr, pid, timeout: float) -> bool:
    """Poll until it dies or `timeout` runs out. A second Ctrl-C cannot break us out.

    KeyboardInterrupt here would abandon a half-signalled recorder, which is the exact
    outcome this whole function exists to prevent — so it is swallowed and we keep going.
    """
    end = time.monotonic() + timeout
    while True:
        if _proc_gone(pr, pid):
            return True
        if time.monotonic() >= end:
            return False
        try:
            time.sleep(0.05)
        except BaseException:
            pass


def force_stop(pr=None, pid=None, label: str = "mtcapture") -> dict:
    """Signal, escalate, and do not return until the process is actually gone.

    SIGINT, then SIGTERM, then SIGKILL, with a bounded wait after each. Returns
    {"ok": True, "escalated": [...], "seconds": n} once it is gone, or ok=False with a
    reason if it survived SIGKILL — which the caller must say out loud, never swallow.
    """
    if pr is not None and pid is None:
        pid = pr.pid
    if pid is None:
        return {"ok": True, "escalated": [], "seconds": 0.0, "why": "nothing to stop"}
    if _proc_gone(pr, pid):
        return {"ok": True, "pid": pid, "escalated": [], "seconds": 0.0}
    # A pid we did not spawn may have been recycled since we read it. Never signal one
    # that is not the thing we think it is.
    if pr is None and label == "mtcapture" and not is_mtcapture_pid(pid):
        return {"ok": True, "pid": pid, "escalated": [], "seconds": 0.0,
                "why": "that pid is not an mtcapture any more"}
    t0 = time.monotonic()
    sent: list[str] = []
    ladder = ((signal.SIGINT, "SIGINT", STOP_WAIT_SIGINT),
              (signal.SIGTERM, "SIGTERM", STOP_WAIT_SIGTERM),
              (signal.SIGKILL, "SIGKILL", STOP_WAIT_SIGKILL))
    for sig, name, wait in ladder:
        try:
            if pr is not None:
                pr.send_signal(sig)
            else:
                os.kill(int(pid), sig)
        except ProcessLookupError:
            return {"ok": True, "pid": pid, "escalated": sent,
                    "seconds": round(time.monotonic() - t0, 2)}
        except Exception as e:
            return {"ok": False, "pid": pid, "escalated": sent,
                    "seconds": round(time.monotonic() - t0, 2),
                    "why": f"could not send {name} to {label} (pid {pid}): {e}"}
        sent.append(name)
        if _wait_gone(pr, pid, wait):
            return {"ok": True, "pid": pid, "escalated": sent,
                    "seconds": round(time.monotonic() - t0, 2)}
    return {"ok": False, "pid": pid, "escalated": sent,
            "seconds": round(time.monotonic() - t0, 2),
            "why": f"{label} (pid {pid}) is STILL ALIVE after SIGKILL — it is stuck in "
                   f"the kernel. Nothing in user space can end it; it usually clears on "
                   f"its own within a few minutes."}


def report_stop(r: dict, label: str = "mtcapture") -> None:
    """Say something only when the stop was not routine. Silence means it went cleanly."""
    if not r.get("ok"):
        warn(r.get("why") or f"could not stop {label}")
        return
    esc = r.get("escalated") or []
    if len(esc) > 1:
        warn(f"{label} ignored {esc[0]} — escalated to {esc[-1]}, gone after "
             f"{r.get('seconds')}s. Its WAV may need repairing; `mt process` does that.")


# ── is that mtcapture actually recording, or just refusing to die? ───────────
# A wedged recorder is not a recording, but active_capture() cannot tell the difference
# and everything downstream believes it. These are the two pieces of positive evidence
# that it has stopped for good.
CAPTURE_WEDGED_AFTER = 15.0    # seconds since it logged its own shutdown
CAPTURE_STALE_AFTER = 240.0    # seconds since anything at all was written


def capture_liveness(pid, session) -> dict:
    """Evidence that the recorder holding this session is really still recording.

    Conservative by construction: anything that cannot be established counts as LIVE.
    Being wrong in the other direction lets a second capture start on top of a live
    meeting, and a second ScreenCaptureKit capture kills the first one — that cost the user
    the back half of a real call on Sep 4. A leftover only ever loses the benefit of the
    doubt on positive evidence that it has already stopped.
    """
    live = {"live": True, "why": ""}
    try:
        session = Path(session)
        now = time.time()
    except Exception:
        return live

    # 1. It said so itself. mtcapture writes a `shutdown` event the instant a stop is
    #    requested, from any source, and then holds itself to a 4 s exit deadline. Still
    #    here well after that means teardown is wedged — and a recorder in teardown has
    #    stopped recording whatever the process table says.
    try:
        ev = session / EVENTS_FILE
        if ev.exists():
            age = now - ev.stat().st_mtime
            for ln in reversed(ev.read_text(errors="replace").splitlines()[-60:]):
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                if not isinstance(o, dict):
                    continue
                if o.get("event") in ("shutdown", "forced_exit"):
                    if age >= CAPTURE_WEDGED_AFTER:
                        return {"live": False, "wedged": True, "pid": pid,
                                "why": f"it logged its own shutdown {int(age)}s ago and "
                                       f"is still running"}
                    break
                break          # only the LAST event counts
    except OSError:
        return live

    # 2. Nothing is being written. Four minutes without a single byte reaching either
    #    track or the event log is not a quiet meeting — a live tap delivers buffers
    #    (silence included) continuously, and the watchdog logs every few seconds.
    newest, seen = 0.0, False
    for n in ("system.wav", "mic.wav", EVENTS_FILE, "capture.json"):
        f = session / n
        try:
            if f.exists():
                seen = True
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            return live
    if not seen:
        return live
    idle = now - newest
    if idle >= CAPTURE_STALE_AFTER:
        return {"live": False, "wedged": True, "pid": pid,
                "why": f"nothing has been written to {session.name} for {int(idle)}s"}
    return live


def live_capture() -> tuple[int, Path] | None:
    """active_capture(), minus anything that has provably stopped recording."""
    cur = active_capture()
    if cur is None:
        return None
    return cur if capture_liveness(cur[0], cur[1]).get("live") else None


def read_capture_events(session: Path, limit: int = 2000) -> list[dict]:
    """The recorder's event log, parsed leniently. A bad line is skipped, never fatal."""
    try:
        f = session / EVENTS_FILE
        if not f.exists():
            return []
        raw = f.read_text(errors="replace").splitlines()
    except (OSError, TypeError):
        return []
    out = []
    for ln in raw[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if isinstance(o, dict) and isinstance(o.get("event"), str):
            out.append(o)
    return out


def _fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_health_state(session: Path) -> dict:
    """The user's answers and manual recaptures. Ours to write; the recorder never sees it."""
    blank = {"answers": {}, "manual": []}
    try:
        f = session / HEALTH_FILE
        if not f.exists():
            return blank
        d = json.loads(f.read_text(errors="replace"))
    except Exception:
        return blank
    if not isinstance(d, dict):
        return blank
    ans = d.get("answers")
    man = d.get("manual")
    return {"answers": ans if isinstance(ans, dict) else {},
            "manual": man if isinstance(man, list) else []}


def _write_health_state(session: Path, state: dict) -> bool:
    """Replace our own answers file. Written via a temp file so a crash cannot leave a
    half-written one, and never touching anything the recorder owns."""
    try:
        tmp = session / (HEALTH_FILE + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(tmp, session / HEALTH_FILE)
        return True
    except Exception:
        return False


def record_health_answer(session: Path, key: str, answer: str) -> bool:
    """Record yes/no against ONE alert. Keyed per episode, so answering this one never
    silences the next."""
    if answer not in ("yes", "no"):
        return False
    st = read_health_state(session)
    st["answers"][str(key)[:120]] = {
        "answer": answer,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    return _write_health_state(session, st)


def note_manual_recapture(session: Path, pid: int) -> None:
    """A best-effort breadcrumb for a button press. The recorder logs the real attempt."""
    try:
        st = read_health_state(session)
        st["manual"] = (st["manual"] + [{
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "pid": int(pid), "requested": True}])[-50:]
        _write_health_state(session, st)
    except Exception:
        pass


def _ev_text(o: dict) -> str:
    """One readable line per event, for the history list. Free text is truncated here."""
    e = o.get("event")
    if e == "probe_result":
        return ("cue heard — the tap is alive" if o.get("heard")
                else "cue NOT heard — the tap is not recording")
    if e == "probe_start":
        return "playing a cue to test the tap"
    if e == "system_audio_dead":
        s = _fnum(o.get("since"))
        return "system tap reported dead" + (f" (silent since {hhmmss(s)})" if s else "")
    if e == "system_audio_back":
        a = _fnum(o.get("at"))
        return "system audio came back" + (f" at {hhmmss(a)}" if a else "")
    if e == "recapture":
        bits = [f"recapture #{o.get('attempt')}" if o.get("attempt") else "recapture",
                str(o.get("reason") or "")[:20],
                "ok" if o.get("ok") else "FAILED"]
        g = _fnum(o.get("gap_ms"))
        if g is not None:
            bits.append(f"gap {int(g)} ms")
        return "  ·  ".join(b for b in bits if b)
    if e == "system_audio_quiet":
        return str(o.get("note") or "no system audio while the mic was active")[:300]
    return (str(e)[:60] + (("  ·  " + str(o.get("note"))[:240]) if o.get("note") else ""))


def _health_blank() -> dict:
    return {"state": "ok", "alarm": False, "ask": False, "key": None, "answer": None,
            "confirmed": False, "dismissed": False,
            "evidence": "", "title": "", "detail": "", "question": "",
            "since": None, "sinceText": "", "at": None, "atText": "",
            "recaptures": [], "last_recapture": None, "attempts": 0,
            "events": [], "counts": {}}


def capture_health(session: Path | None) -> dict:
    """Fold the recorder's event log plus the user's answers into one state.

    States: ok · ask (a question, nothing asserted) · dead (asserted, and only ever on
    the probe's evidence or his own answer) · recovered.

    A `probe_result` with heard:true is the HEALTHY case — the cue was captured, so the
    tap is alive and the far end is simply quiet. It clears the state and never alarms.
    Alarming on it would teach him to ignore the alert, which is the whole failure this
    is here to prevent.
    """
    h = _health_blank()
    if session is None:
        return h
    try:
        events = read_capture_events(session)
    except Exception:
        return h
    if not events:
        return h

    state, key_at, evidence = "ok", None, ""
    since = None          # last moment audio was known good, per the recorder
    back_at = None
    recaps: list[dict] = []
    counts = {"probe": 0, "quiet": 0, "dead": 0, "recapture": 0, "back": 0}

    def open_episode(t):
        nonlocal key_at, since
        if key_at is None:
            key_at = t

    for o in events:
        e = o.get("event")
        t = _fnum(o.get("t"), 0.0) or 0.0
        if e == "system_audio_quiet":
            counts["quiet"] += 1
            if state != "dead":
                state = "ask"
                evidence = "watchdog"
                open_episode(t)
        elif e == "probe_result":
            counts["probe"] += 1
            heard = o.get("heard")
            if heard is None:
                continue          # a probe that reports nothing is evidence of nothing
            if heard:
                # Positive proof the tap works. Silent resolution, by design.
                state, key_at, evidence = "ok", None, ""
            else:
                state, evidence = "dead", "probe"
                open_episode(t)
        elif e == "system_audio_dead":
            counts["dead"] += 1
            state, evidence = "dead", "probe"
            open_episode(t)
            since = _fnum(o.get("since"), since)
        elif e == "system_audio_back":
            counts["back"] += 1
            state, key_at, evidence = "recovered", None, ""
            back_at = _fnum(o.get("at"), t)
        elif e == "recapture":
            counts["recapture"] += 1
            recaps.append({"t": t, "tText": hhmmss(t),
                           "attempt": o.get("attempt"),
                           "reason": str(o.get("reason") or "")[:20],
                           "ok": bool(o.get("ok")),
                           "gap_ms": _fnum(o.get("gap_ms"))})

    # Keyed by the EPISODE, not by the line: an answer settles this occurrence and never
    # the next one, and an episode that escalates from a question to hard evidence gets a
    # new key, so a "no" to the question cannot silence the probe's verdict.
    answers = read_health_state(session)["answers"]

    def answer_for(k):
        rec = answers.get(k) if k else None
        if isinstance(rec, dict) and rec.get("answer") in ("yes", "no"):
            return rec["answer"]
        return None

    key = None if key_at is None else f"{state}@{key_at:.1f}"
    answer = answer_for(key)

    # His answer is the backstop when the probe could not settle it. "yes, they are
    # talking" over a silent system track is a confirmation; "no" is a dismissal, and he
    # is allowed to dismiss a probe-confirmed one too — he decides what to act on.
    confirmed = False
    if state == "ask" and answer == "yes":
        state, evidence, confirmed = "dead", "answer", True
        key = f"dead@{key_at:.1f}"
        answer = answer_for(key) or answer   # dismissing the escalated alert still works
    dismissed = answer == "no"

    h.update({
        "state": state,
        "alarm": state == "dead" and not dismissed,
        "ask": state == "ask" and answer is None,
        "key": key,
        "answer": answer,
        "confirmed": confirmed,
        "dismissed": dismissed,
        "evidence": evidence,
        "since": since if since is not None else key_at,
        "at": back_at,
        "recaptures": recaps[-12:],
        "last_recapture": recaps[-1] if recaps else None,
        "attempts": len(recaps),
        "counts": counts,
        "events": [{"t": _fnum(o.get("t"), 0.0) or 0.0,
                    "tText": hhmmss(_fnum(o.get("t"), 0.0) or 0.0),
                    "event": str(o.get("event"))[:40],
                    "text": _ev_text(o)} for o in events[-14:]],
    })
    if h["since"] is not None:
        h["sinceText"] = hhmmss(h["since"])
    if h["at"] is not None:
        h["atText"] = hhmmss(h["at"])

    if state == "dead":
        h["title"], h["detail"] = HEALTH_TEXT["dead_answer" if evidence == "answer"
                                              else "dead_probe"]
    elif state == "ask":
        h["title"], h["detail"] = HEALTH_TEXT["ask"]
        h["question"] = HEALTH_QUESTION
    elif state == "recovered":
        h["title"], h["detail"] = HEALTH_TEXT["recovered"]
    return h


def signal_recapture(pid=None) -> dict:
    """SIGUSR1 the running recorder: tear down and rebuild the SCStream in place.

    Two independent confirmations that the target is really an mtcapture — argv from the
    process table, then a second check by pid — because a stray signal to the wrong
    process is exactly the class of bug this whole feature exists to avoid.
    """
    cur = active_capture()
    if cur is None:
        return {"ok": False, "why": "nothing is recording"}
    live_pid, session = cur
    if pid is not None:
        try:
            if int(pid) != live_pid:
                return {"ok": False, "why": "that is not the recorder that is running"}
        except (TypeError, ValueError):
            return {"ok": False, "why": "bad pid"}
    if not is_mtcapture_pid(live_pid):
        return {"ok": False, "why": "that process is not an mtcapture — refusing to signal it"}
    try:
        os.kill(live_pid, signal.SIGUSR1)
    except ProcessLookupError:
        return {"ok": False, "why": "the recorder is already gone"}
    except PermissionError:
        return {"ok": False, "why": "not allowed to signal that process"}
    except Exception as e:
        return {"ok": False, "why": str(e)}
    note_manual_recapture(session, live_pid)
    return {"ok": True, "pid": live_pid, "session": session.name,
            "note": "recapture requested — the recorder logs the attempt and keeps "
                    "writing the same files"}


def cmd_recapture(a):
    """Rebuild the system-audio tap on the running recording, from the terminal."""
    r = signal_recapture()
    if not r.get("ok"):
        fail(r.get("why") or "could not request a recapture")
    step(f"recapture requested (pid {r['pid']}, {r['session']})")
    say(f"{DIM}   the recording keeps going — watch capture_events.jsonl for the result{RST}")
    return 0


def capture_event_time(session: Path, event: str) -> float | None:
    """When the recorder flagged something going wrong mid-capture, if it did."""
    f = session / "capture_events.jsonl"
    if not f.exists():
        return None
    for ln in f.read_text(errors="replace").splitlines():
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("event") == event:
            return float(o.get("t") or 0)
    return None


def mic_timeline(session: Path) -> list[dict]:
    f = session / "mic.jsonl"
    if not f.exists():
        return []
    out = []
    for ln in f.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def cmd_devices(a):
    if getattr(a, "forget", False):
        n = audioprefs.forget()
        say(f"forgot remembered picks for {n} setup(s); mic_priority decides again")
        return 0
    mics = list_mics()
    if not mics:
        say("no microphones found")
        return 1
    prio = config().get("mic_priority") or []
    say("microphones — ▸ marks the current system default:")
    for m in mics:
        mark = "▸" if m["default"] else " "
        hit = next((w for w in prio if w.lower() in m["name"].lower()), None)
        tag = f"   [mic_priority: {hit}]" if hit else ""
        say(f" {mark} {m['name']}{tag}")
        say(f"     {DIM}--mic-id {m['uid']}{RST}")
    say("")
    uid, why = pick_mic(config(), None)
    chosen = next((m["name"] for m in mics if m["uid"] == uid), None) or \
             next((m["name"] for m in mics if m["default"]), "?")
    say(f"mt would use: {BOLD}{chosen}{RST}  ({why})")
    say("")
    if not audioprefs.available():
        say(f"{YEL}speakers: mtaudio is not built — run ./make.sh{RST}")
    else:
        say("speakers — ▸ marks the current system default:")
        for s in audioprefs.speakers():
            say(f" {'▸' if s['default'] else ' '} {s['name']}")
            say(f"     {DIM}--speaker-id {s['uid']}{RST}")
        spk, swhy = pick_speaker(None)
        say(f"mt would switch to: {BOLD}{spk['name']}{RST}  ({swhy})" if spk
            else f"mt would leave the speaker as is")
        say("")
        say(f"this desk: {audioprefs.describe(audioprefs.station())}")
        for r in audioprefs.saved_stations():
            picks = ", ".join(f"{k} {r[k]['name']}" for k in ("mic", "speaker") if r.get(k))
            say(f"{DIM}   remembered at [{audioprefs.describe(r.get('devices') or [])}]: "
                f"{picks}  ({r.get('updated', '')}){RST}")
        say(f"{DIM}   a pick made with --mic-id/--speaker-id or in mt web is remembered per "
            f"desk; `mt devices --forget` clears them{RST}")
    if not prio:
        say(f"{DIM}Set \"mic_priority\" in config.json to a list of name fragments, e.g."
            f" [\"HyperX\", \"Logitech USB Headset\"], and the first one present wins.{RST}")
    return 0


def session_dir(a, cfg) -> Path:
    now = dt.datetime.now()
    slug = slugify(a.name) if a.name else "meeting"
    if a.customer:
        base = P.CUSTOMERS / a.customer / "meetings"
        if not base.parent.exists():
            known = sorted(p.name for p in P.CUSTOMERS.iterdir() if p.is_dir()) \
                if P.CUSTOMERS.is_dir() else []
            fail(f"unknown customer folder: {base.parent}\n"
                 + (f"expected one of: {', '.join(known)}" if known else
                    f"create it first:  mkdir -p '{base.parent}'"))
        root, stamp = base, f"{now:%Y-%m-%d}"
    else:
        root, stamp = RECORDINGS, f"{now:%Y-%m-%d-%H%M}"
    # Never share a folder with an earlier start. Oct 5 2026: a start on the wrong mic was
    # killed and restarted inside the same minute, both landed in one folder, and the dead
    # one's shutdown event and 4-second archives made the live recording look stopped. A
    # second start gets "-r2-" (then r3…) between the stamp and the name, so calendar
    # matching (starts with today, ends with -<slug>) still finds it.
    d, n = root / f"{stamp}-{slug}", 2
    while d.exists() and any(d.iterdir()):
        d, n = root / f"{stamp}-r{n}-{slug}", n + 1
    d.mkdir(parents=True, exist_ok=True)
    inv = getattr(a, "invitees", None)
    if inv:
        names = [x.strip() for x in inv.split(",") if x.strip()]
        if names:
            (d / "invitees.json").write_text(json.dumps(
                {"source": "calendar invite (saved at record time)",
                 "attendees": [{"name": n} for n in names]}, indent=2) + "\n")
    return d


def refuse_if_recording(a) -> None:
    """Never start a second capture while one is live.

    Starting a competing ScreenCaptureKit capture kills the running one. This cost the user
    the back half of a real meeting on Sep 4: a test recording started at 16:18:03 and
    his capture, 18 minutes in, died at 16:18:09. The audio already written survives, but
    everything after that moment is gone for good. There is no reason to allow it.
    """
    if getattr(a, "allow_concurrent", False):
        return
    cur = active_capture()
    if cur is None:
        return
    pid, live = cur
    verdict = capture_liveness(pid, live)
    if verdict.get("live"):
        fail(f"""a recording is already running — refusing to start another.
    Starting a second capture kills the first one, losing the rest of that meeting.
    In progress: {live}
    Stop it with Ctrl-C in its own terminal, then start this one.
    (--allow-concurrent overrides this, but it will almost certainly break both.)""")
    # A recorder that has provably stopped recording but is still in the process table.
    # Left alone it blocks every future recording, which is precisely how the user lost the
    # start of their next meeting on Sep 17. Clear it and carry on.
    warn(f"a previous recorder (pid {pid}) is stuck and is not recording — "
         f"{verdict.get('why')}")
    r = force_stop(pid=pid, label="mtcapture")
    if not r.get("ok"):
        fail(f"""{r.get('why')}
    It is not recording, but it still holds the recorder slot. Wait a minute and try
    again, or start with --allow-concurrent if you cannot wait.""")
    still = active_capture()
    if still is not None and capture_liveness(still[0], still[1]).get("live"):
        fail(f"another recorder (pid {still[0]}) is recording to {still[1]} — "
             f"refusing to start a second capture.")
    step(f"cleared the stuck recorder (pid {pid})")


def cmd_record(a):
    refuse_if_recording(a)
    cfg = config()
    if not CAPTURE.exists():
        fail("mtcapture is not built — run ./make.sh")
    d = session_dir(a, cfg)

    cmd = [str(CAPTURE), "--out-dir", str(d)]
    step(f"recording to {d}")
    mic_uid = choose_audio(a, cfg)
    if mic_uid:
        cmd += ["--mic-id", mic_uid]
    if a.no_mic:
        cmd += ["--no-mic"]
    if a.no_system and capture_supports("--no-system-watch"):
        cmd += ["--no-system-watch"]
    if a.duration:
        cmd += ["--duration", str(a.duration)]

    say(f"{DIM}   press Ctrl-C when the meeting ends{RST}")
    watcher = None if a.no_screen else start_screen_watch(d, keep_frames=bool(a.keep_frames))
    if watcher:
        say(f"{DIM}   watching the meeting window for participant names{RST}")
    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        report_stop(force_stop(pr=proc, label="mtcapture"), "mtcapture")
    if proc.poll() is None:                 # a stray exception, or a wedged recorder
        report_stop(force_stop(pr=proc, label="mtcapture"), "mtcapture")
    if watcher and watcher.poll() is None:
        report_stop(force_stop(pr=watcher, label="mtwatch"), "mtwatch")
    if proc.returncode not in (0, None) and not (d / "system.wav").exists():
        fail("capture failed — run `mt doctor`")

    if a.no_process:
        say(f"raw audio in {d}. Transcribe later with:  mt process {d}")
        return 0

    a.path = str(d)
    return cmd_process(a)


def cmd_process(a):
    cfg = config()
    if a.language:
        cfg["language"] = a.language
    if a.prompt:
        cfg["prompt"] = a.prompt

    src = Path(a.path).expanduser().resolve()
    if not src.exists():
        fail(f"no such path: {src}")

    track_check = None
    # A session dir (from `mt record`) or a single media file (Zoom/Gong/Teams export).
    if src.is_dir():
        session = src
        tracks = []
        # Either track can be set aside after the fact; both stay on disk regardless.
        want_kinds = (("mic",) if a.no_system
                      else ("system",) if getattr(a, "no_mic", False)
                      else ("system", "mic"))
        # Is the recorder still writing these? If it is, leave the headers alone —
        # an in-progress WAV is *supposed* to say zero until it closes.
        cur = active_capture()
        writing = cur is not None and _same_dir(cur[1], session)
        for kind in want_kinds:
            f = next((session / f"{kind}{e}" for e in (".wav", ".flac", ".m4a")
                      if (session / f"{kind}{e}").exists()), None)
            if f is not None and f.suffix == ".wav" and not writing:
                f, note = salvage_track(f)
                if note:
                    warn(note)
            if f is not None and duration_of(f) > 0.5:
                tracks.append((kind, f))
        if not tracks:
            fail(f"no system/mic audio track in {session}")
        meta = {}
        cj = session / "capture.json"
        if cj.exists():
            try:
                meta = json.loads(cj.read_text())
            except Exception:
                pass
        source = "mtcapture dual-track" if len(tracks) == 2 else f"mtcapture ({tracks[0][0]} only)"
        # Do both tracks hear the same audio? Asked of the files, not of `tracks`: a
        # mic-only pass of a room meeting is exactly when the Teams track may be better.
        # Audio never changes after the recording, so the answer is cached.
        both = {k: next((session / f"{k}{e}" for e in (".flac", ".wav")
                         if (session / f"{k}{e}").exists()), None) for k in ("mic", "system")}
        if all(both.values()) and not writing:
            try:
                import tracks as tracks_mod
                track_check = tracks_mod.read(session)
                newest = max(f.stat().st_mtime for f in both.values())
                if (not track_check or track_check.get("version") != tracks_mod.VERSION
                        or (session / "tracks.json").stat().st_mtime < newest):
                    step("tracks: checking whether both hear the same audio")
                    track_check = tracks_mod.write(session, both["mic"], both["system"])
                say(f"{DIM}   {track_check['headline']}{RST}")
            except Exception as e:
                track_check = None
                warn(f"track check skipped: {e}")
    else:
        if src.suffix.lower() not in AUDIO_EXT:
            fail(f"unsupported file type: {src.suffix}")
        if a.customer or a.name:
            ns = argparse.Namespace(customer=a.customer, name=a.name or src.stem)
            session = session_dir(ns, cfg)
        else:
            session = RECORDINGS / f"{dt.datetime.now():%Y-%m-%d-%H%M}-{slugify(src.stem)}"
            session.mkdir(parents=True, exist_ok=True)
        tracks = [("system", src)]
        meta = {}
        source = f"imported file ({src.name})"

    work = session / "work"
    work.mkdir(exist_ok=True)
    people = load_voiceprints()
    owner = a.owner or cfg["owner"]
    screen = read_screen(session, owner)
    onscreen = screen.get("roster", [])
    talking = screen.get("talking", [])
    expected = screen.get("expected")
    if screen:
        step(f"screen: {', '.join(f'{k}={v}' for k, v in screen.get('views', {}).items()) or 'no meeting window seen'}")
        if expected:
            say(f"{DIM}   the meeting UI reported {expected} participant(s){RST}")
        for r in onscreen:
            say(f"{DIM}   {r['name']}  ({r['frames']} frames)"
                + (f"  [{r['role'][:42]}]" if r["role"] else "") + RST)
        if talking:
            say(f"{DIM}   {len(talking)} active-speaker observation(s) to attribute with{RST}")
    onscreen_names = {r["name"] for r in onscreen}

    timeline: list[dict] = []
    names: dict[str, dict] = {}
    total_dur = 0.0

    for kind, path in tracks:
        w16 = work / f"{kind}.16k.wav"
        if w16.exists() and w16.stat().st_mtime >= path.stat().st_mtime:
            say(f"{DIM}   {kind}: normalised 16 kHz copy already current{RST}")
        else:
            step(f"{kind}: converting + normalising")
            to_16k_mono(path, w16)
        dur = duration_of(w16)
        total_dur = max(total_dur, dur)
        x = read_wav16k(w16)

        # Segment first: the speech map drives BOTH what gets transcribed and who said it.
        diarize_this = (kind == "system") or a.diarize_mic or a.in_person
        # Far-end voices = everyone in the meeting minus the user, whose voice is on the
        # mic track. A headcount read off the UI beats guessing.
        from_ui = (expected - 1) if (expected and expected > 1 and kind == "system"
                                     and len(tracks) > 1) else -1
        want = (a.speakers if a.speakers
                else (len(a.roster.split(",")) if (a.roster and diarize_this)
                      else from_ui)) if diarize_this else 1
        step(f"{kind}: finding speech" + (f" (expecting {want} voices)" if want and want > 0 and diarize_this else ""))
        turns = diarize(w16, cfg, want)
        if not turns:
            lvl = peak_of(w16)
            warn(f"{kind}: no speech detected (peak {lvl:.3f}) — dropping this track"
                 f"{' (you never spoke)' if kind == 'mic' else ''}")
            continue

        blocks = speech_blocks(turns, dur)
        # The segmenter is relative; it will happily call amplified noise "speech".
        # Require real level before we hand a block to the recogniser.
        kept = [(s0, e0) for s0, e0 in blocks
                if float(np.sqrt((x[int(s0*16000):int(e0*16000)] ** 2).mean() + 1e-12)) >= MIN_SPEECH_RMS]
        if len(kept) < len(blocks):
            say(f"{DIM}   ignored {len(blocks)-len(kept)} below-threshold block(s) "
                f"(noise, not speech){RST}")
        blocks = kept
        if not blocks:
            warn(f"{kind}: no block loud enough to be speech — dropping this track"
                 f"{' (you never spoke)' if kind == 'mic' else ''}")
            continue
        spoken = sum(b - a2 for a2, b in blocks)
        say(f"{DIM}   {hhmmss(spoken)} of speech in {len(blocks)} block(s) "
            f"({100*spoken/max(dur,1):.0f}% of the track){RST}")

        cache = work / f"{kind}.segments.json"
        if cache.exists() and cache.stat().st_mtime >= w16.stat().st_mtime and not a.force_asr:
            step(f"{kind}: reusing existing transcription")
            segs = json.loads(cache.read_text())
        else:
            step(f"{kind}: transcribing {hhmmss(spoken)} with {WHISPER_MODEL.name}")
            segs = transcribe_speech(x, blocks, work, kind, cfg)
            cache.write_text(json.dumps(segs, indent=1) + "\n")
        segs, looped = drop_repeats(segs)
        segs, junk = strip_artifacts(segs, cfg.get("prompt"))
        if looped or junk:
            say(f"{DIM}   dropped {looped} looped + {junk} non-speech/prompt-echo segment(s){RST}")
        say(f"{DIM}   {len(segs)} segments{RST}")
        if not segs:
            warn(f"{kind}: nothing intelligible — dropping this track")
            continue

        segs, unaligned = snap_to_speech(segs, turns)
        if unaligned:
            say(f"{DIM}   dropped {unaligned} segment(s) with no speech under them{RST}")

        if not diarize_this:
            key = "mic"
            names[key] = {"name": owner, "source": "mic track (you)", "confidence": "verified"}
            timeline += [{**sg, "track": kind, "speaker_key": key, "overlap": 1.0} for sg in segs]
            continue

        step(f"{kind}: consolidating voices")
        turns, cstat = consolidate(x, turns, cfg)
        if cstat:
            say(f"{DIM}   {cstat['raw']} raw clusters -> {cstat['speakers']} speaker(s)"
                + (f", {cstat['absorbed']} fragment(s) absorbed" if cstat['absorbed'] else "")
                + (f", {cstat['unknown']} unidentified" if cstat['unknown'] else "") + RST)

        clusters = sorted({t["cluster"] for t in turns})
        placed = assign(segs, turns)
        roster = [r.strip() for r in a.roster.split(",")] if a.roster else []

        for i, cl in enumerate(clusters):
            key = f"{kind}-spk{cl}" if cl != UNKNOWN else f"{kind}-unknown"
            if cl == UNKNOWN:
                names[key] = {"name": "Unidentified voice", "source": "brief/unmatched speech",
                              "confidence": "unconfirmed"}
                continue
            vec = embed_cluster(x, turns, cl)
            entry: dict = {"name": f"Speaker {cl + 1}" if kind == "system" else f"{owner}'s room {cl + 1}",
                           "source": "unidentified", "confidence": "unconfirmed"}

            # A cluster that disagrees with itself holds more than one person. Never put
            # a name on it and never let it be enrolled — a blended voiceprint would
            # corrupt both people for every future meeting.
            self_sim = spans_self_similarity(
                x, [(t["start"], t["end"]) for t in turns if t["cluster"] == cl])
            if self_sim is not None:
                entry["self_similarity"] = round(self_sim, 3)
                if self_sim < float(cfg["min_self_similarity"]):
                    entry.update(name="Multiple voices (unresolved)",
                                 source=f"two or more speakers merged (self-similarity "
                                        f"{self_sim:.2f}); not safe to name or enrol",
                                 mixed=True)
                    names[key] = entry
                    continue
            if vec is not None:
                np.save(work / f"{key}.npy", vec)
                if people:
                    who, score = match_voice(vec, people)
                    entry["score"] = round(score, 3)
                    # The next-best print, so "clearly this person" can be told apart from
                    # "barely ahead of someone else" (cues.strong_match uses the gap).
                    others = [cosine(vec, p["vec"]) for p in people if p["name"] != who]
                    if others:
                        entry["runner_up"] = round(max(others), 3)
                    if score >= cfg["auto_assign_score"]:
                        # Roster presence is NOT speaker attribution. Say exactly what
                        # the evidence is so the label cannot be over-read.
                        corroborated = who in onscreen_names
                        entry.update(name=who, confidence="unconfirmed",
                                     source=("voiceprint match; also in the on-screen roster"
                                             if corroborated else "voiceprint match"))
                    elif score >= cfg["suggest_score"]:
                        entry["suggestion"] = {"name": who, "score": round(score, 3)}
            # Strongest available evidence: the meeting UI named a speaker while this
            # voice was talking.
            who_scr, hits, tot = name_from_screen(turns, cl, talking)
            if who_scr and hits >= 2 and hits >= 0.5 * max(tot, 1):
                prior = entry.get("name", "")
                agrees = prior == who_scr
                entry.update(name=who_scr, confidence="unconfirmed",
                             source=("voiceprint + on-screen speaker" if agrees and vec is not None
                                     else "on-screen active speaker"),
                             screen_votes=f"{hits}/{tot}")
            elif roster and i < len(roster) and entry["source"] == "unidentified":
                entry.update(name=roster[i], source="roster order (guess)", confidence="unconfirmed")
            names[key] = entry

        timeline += [{**pl, "track": kind,
                      "speaker_key": (f"{kind}-spk{pl['cluster']}" if pl["cluster"] != UNKNOWN
                                      else f"{kind}-unknown")} for pl in placed]

    if not timeline:
        fail("nothing was transcribed — the audio may be silent")

    timeline, bled = drop_bleed(timeline)
    if bled:
        say(f"{DIM}   dropped {bled} mic segment(s) that were speaker bleed "
            f"(use headphones to avoid this){RST}")
    timeline.sort(key=lambda e: (e["start"], e["track"]))

    started = meta.get("started_at") or dt.datetime.fromtimestamp(
        session.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    doc_meta = {
        "title": a.title or session.name.replace("-", " "),
        "started_at": started,
        "duration": total_dur,
        "customer": a.customer,
        "source": source,
        "mic": meta.get("mic_name"),
        "mic_timeline": mic_timeline(session),
        # Mic only: the system track is silent on purpose, so its silence is not a death.
        "system_dead_at": (system_died_at(session)
                           if any(k == "system" for k, _ in tracks) else None),
        "model": WHISPER_MODEL.name,
        "owner": owner,
        "onscreen": onscreen,
        "tracks_used": sorted({e.get("track") for e in timeline if e.get("track")}),
        "tracks": ({k: track_check.get(k) for k in ("verdict", "headline", "recommend")}
                   if track_check else None),
    }

    (session / "speakers.json").write_text(json.dumps(
        {"owner": owner,
         "_help": "Fix any 'name', then set confidence to 'verified' and run: mt relabel <this dir>",
         "map": names}, indent=2) + "\n")
    (session / "transcript.json").write_text(json.dumps(
        {"meta": doc_meta, "speakers": names, "timeline": timeline}, indent=2) + "\n")
    md = render_markdown(session, doc_meta, timeline, names)
    (session / "transcript.md").write_text(md)
    (session / "transcript.txt").write_text("\n".join(
        f"[{hhmmss(e['start'])}] "
        f"{e.get('override') or names.get(e['speaker_key'],{}).get('name', e['speaker_key'])}: {e['text']}"
        for e in timeline) + "\n")

    # Fold this meeting into the search index. Deliberately best-effort: a missing or
    # broken index is a minor annoyance, a crashed pass loses a call nobody can re-record.
    try:
        n_idx = _search_module().index_session(session)
        say(f"{DIM}   indexed {n_idx} doc(s) for `mt search`{RST}")
    except Exception as e:
        warn(f"could not add {session.name} to the search index: {e}")

    if not a.keep_wav:
        # purely transient: regenerated on demand, and the biggest thing in work/
        for junk in list(work.glob("*.speech.wav")) + list(work.glob("live_*.wav")):
            try:
                junk.unlink()
            except OSError:
                pass
        for kind, path in tracks:
            if path.suffix.lower() != ".wav":
                continue
            flac = path.with_suffix(".flac")
            try:
                subprocess.run([need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                                "-i", str(path), "-c:a", "flac", str(flac)],
                               check=True, capture_output=True, text=True)
                before, after = path.stat().st_size, flac.stat().st_size
                if after > 0:
                    path.unlink()
                    say(f"{DIM}   archived {kind}: {before/1e6:.0f} MB wav -> "
                        f"{after/1e6:.0f} MB flac (lossless){RST}")
            except subprocess.CalledProcessError:
                warn(f"could not archive {path.name}; leaving the wav in place")

    say("")
    step("done")
    say(f"   {session/'transcript.md'}")
    n_unconf = sum(1 for v in names.values() if v.get("confidence") != "verified")
    for k, v in names.items():
        sug = v.get("suggestion")
        extra = f"  (maybe {sug['name']}, {sug['score']:.2f})" if sug else ""
        sc = f" [{v['score']:.2f}]" if v.get("score") is not None else ""
        say(f"   {k:<16} {v['name']}{sc}{extra}")
    if n_unconf:
        say("")
        say(f"{YEL}{n_unconf} speaker label(s) are guesses.{RST} Name them for good with:")
        say(f"   mt enroll {session} --speaker 1 --name \"Full Name\"")
    return 0


def has_signal_wav(p: Path) -> bool:
    """Silence check that tolerates non-16k input."""
    try:
        with wave.open(str(p)) as w:
            if w.getnframes() == 0:
                return False
            if w.getframerate() == 16000 and w.getnchannels() == 1:
                return has_signal(p)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            t = to_16k_mono(p, Path(td) / "x.wav")
            return has_signal(t)
    except Exception:
        return duration_of(p) > 0.5


def cmd_enroll(a):
    session = Path(a.session).expanduser().resolve()
    tj = session / "transcript.json"
    if not tj.exists():
        fail(f"{session} has no transcript.json — run `mt process` on it first")
    doc = json.loads(tj.read_text())
    keys = sorted(doc["speakers"].keys())

    # --speaker accepts the display number (1-based), the cluster id, or the full key
    want = str(a.speaker)
    key = None
    if want in doc["speakers"]:
        key = want
    else:
        for k in keys:
            m = re.search(r"spk(\d+)$", k)
            if m and (want == m.group(1) or want == str(int(m.group(1)) + 1)):
                key = k
                break
    if not key:
        fail(f"no speaker {want!r} in {tj}. Available: {', '.join(keys)}")

    entry = doc["speakers"].get(key, {})
    # Verify from the audio rather than trusting stored metadata: sessions processed
    # before this check existed carry no flag, and an unflagged mixture silently blended
    # two people's voiceprints once already. Measure it every time.
    spans = [(e["start"], e["end"]) for e in doc.get("timeline", [])
             if e.get("speaker_key") == key]
    audio = next((session / f"system{x}" for x in (".flac", ".wav")
                  if (session / f"system{x}").exists()), None)
    sim = None
    if spans and audio is not None and not key.startswith("mic"):
        w16 = session / "work" / "enrolcheck.16k.wav"
        try:
            to_16k_mono(audio, w16, normalize=False)
            sim = spans_self_similarity(read_wav16k(w16), spans)
        except Exception as e:
            warn(f"could not verify {key} before enrolling: {e}")
        finally:
            w16.unlink(missing_ok=True)
    # Guard: another cluster in THIS session already carries this name. If the two do not
    # sound alike, one of the two labels is wrong and enrolling would average two people
    # into one print. This is exactly how one contact's print got a colleague's voice in it
    # (spk0 vs spk1 scored 0.25) and another's got someone else's (0.45).
    twin = None
    try:
        mine = np.load(session / "work" / f"{key}.npy")
        mine = mine / (np.linalg.norm(mine) or 1.0)
        for other, ov in doc["speakers"].items():
            if other == key or ov.get("name") != a.name:
                continue
            f2 = session / "work" / f"{other}.npy"
            if not f2.exists():
                continue
            v2 = np.load(f2)
            v2 = v2 / (np.linalg.norm(v2) or 1.0)
            agree = float(np.dot(mine, v2))
            if agree < float(config()["auto_assign_score"]):
                twin = (other, agree)
                break
    except Exception:
        pass
    if twin:
        other, agree = twin
        fail(f"""{key} and {other} are BOTH labelled "{a.name}" but only score {agree:.2f}
    against each other — they are different people, so one of those labels is wrong.
    Enrolling would average two voices into one print and mislabel both from now on.
    Listen to each and give them different names first.""")

    if entry.get("mixed") or (sim is not None and sim < float(config()["min_self_similarity"])):
        shown = entry.get("self_similarity") if sim is None else round(sim, 3)
        fail(f"{key} holds more than one voice (self-similarity {shown}).\n"
             f"    Enrolling it would blend two people's voiceprints and mislabel both "
             f"from now on. Leave it unnamed.")
    if sim is not None:
        say(f"{DIM}   {key} self-similarity {sim:.2f} — single speaker{RST}")

    vec_file = session / "work" / f"{key}.npy"
    if not vec_file.exists():
        fail(f"no voiceprint captured for {key} (too little speech?): {vec_file}")

    # getattr(): web.py builds its namespace by hand and never sets these.
    replace = bool(getattr(a, "replace", False))
    force = bool(getattr(a, "force", False))
    rec = save_voiceprint(a.name, np.load(vec_file), source=str(session),
                          replace=replace, force=force)
    how = "replaced" if replace else "enrolled"
    step(f"{how} {a.name}  ({rec['samples']} sample(s), dim {rec['dim']})")

    sp = session / "speakers.json"
    if sp.exists():
        d = json.loads(sp.read_text())
        if key in d.get("map", {}):
            d["map"][key].update(name=a.name, source="enrolled by you", confidence="verified")
            d["map"][key].pop("suggestion", None)
            sp.write_text(json.dumps(d, indent=2) + "\n")
            cmd_relabel(argparse.Namespace(session=str(session)))
    say(f"{GRN}{a.name} will now be recognised automatically in future meetings.{RST}")
    return 0


def cmd_speakers(a):
    people = load_voiceprints()
    retired = read_index(RETIRED / "index.json")["speakers"]
    if not people:
        say("no voiceprints enrolled yet.")
        say("after a meeting:  mt enroll <session-dir> --speaker 1 --name \"Full Name\"")
    else:
        say(f"{len(people)} enrolled voiceprint(s):")
        for p in people:
            say(f"   {p['name']:<28} {p.get('samples',1)} sample(s)   "
                f"updated {p.get('updated','?')}")
    if retired:
        say("")
        say(f"{DIM}{len(retired)} retired — put one back with: mt restore \"Full Name\"{RST}")
        for r in retired:
            why = f"  — {r['reason']}" if r.get("reason") else ""
            say(f"{DIM}   {str(r.get('name','?')):<28} retired "
                f"{str(r.get('retired_at','?'))[:10]}{why}{RST}")
    return 0


def cmd_forget(a):
    """Retire a voiceprint. It is moved aside and indexed, never unlinked."""
    live = VOICEPRINTS / "index.json"
    doc = read_index(live)
    rec = find_print(doc["speakers"], a.name)
    if not rec:
        known = ", ".join(sorted(str(p.get("name") or p.get("slug") or "?")
                                 for p in doc["speakers"]))
        fail(f"no enrolled voiceprint called {a.name!r}.\n    "
             + (f"Enrolled: {known}" if known else "Nothing is enrolled yet."))

    RETIRED.mkdir(parents=True, exist_ok=True)
    rdoc = read_index(RETIRED / "index.json")

    src = VOICEPRINTS / rec["file"]
    stem, suffix = Path(rec["file"]).stem, Path(rec["file"]).suffix
    dest, n = RETIRED / rec["file"], 2
    while dest.exists():          # this name has been retired before — keep both
        dest = RETIRED / f"{stem}-{n}{suffix}"
        n += 1

    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    out = {k: v for k, v in rec.items() if k != "vec"}
    out["retired_at"] = now
    out["retired_from"] = rec["file"]
    out["file"] = dest.name
    if getattr(a, "reason", None):
        out["reason"] = a.reason

    if src.exists():
        shutil.move(str(src), str(dest))
    else:
        warn(f"{rec['file']} was already missing — retiring the index entry only")
        out["file"] = None

    rdoc["speakers"].append(out)
    write_index(RETIRED / "index.json", rdoc)
    doc["speakers"] = [p for p in doc["speakers"] if p is not rec]
    write_index(live, doc)

    step(f"retired {rec['name']}" + (f" — {a.reason}" if getattr(a, "reason", None) else ""))
    if out["file"]:
        say(f"{DIM}   vector kept at {dest}{RST}")
    say(f"   put it back with:  mt restore \"{rec['name']}\"")
    return 0


def cmd_restore(a):
    """The exact inverse of `mt forget`."""
    rpath = RETIRED / "index.json"
    rdoc = read_index(rpath)
    matches = [r for r in rdoc["speakers"] if find_print([r], a.name)]
    if not matches:
        known = ", ".join(sorted(str(r.get("name") or r.get("slug") or "?")
                                 for r in rdoc["speakers"]))
        fail(f"no retired voiceprint called {a.name!r}.\n    "
             + (f"Retired: {known}" if known else "Nothing has been retired."))
    rec = matches[-1]                      # most recently retired wins

    live = VOICEPRINTS / "index.json"
    doc = read_index(live)
    if find_print(doc["speakers"], rec.get("slug") or a.name):
        fail(f"{rec['name']} is already enrolled — retire that print first:\n"
             f"    mt forget \"{rec['name']}\"")
    if not rec.get("file"):
        fail(f"the retired entry for {rec['name']} has no vector file to put back.")

    src = RETIRED / rec["file"]
    dest = VOICEPRINTS / (rec.get("retired_from") or rec["file"])
    if not src.exists():
        fail(f"the retired vector is missing: {src}")
    if dest.exists():
        fail(f"{dest.name} already exists but is not in the index. Move it aside "
             f"yourself — nothing was changed.")

    shutil.move(str(src), str(dest))
    out = {k: v for k, v in rec.items()
           if k not in ("retired_at", "retired_from", "reason")}
    out["file"] = dest.name
    doc["speakers"].append(out)
    write_index(live, doc)
    rdoc["speakers"] = [r for r in rdoc["speakers"] if r is not rec]
    write_index(rpath, rdoc)

    step(f"restored {out['name']}  ({out.get('samples', 1)} sample(s))")
    return 0


def workspace_rel(p: Path) -> str:
    """A session path as the user thinks of it — relative to the workspace when inside it."""
    try:
        return str(p.relative_to(WORKSPACE))
    except ValueError:
        return str(p)


def clean_name(raw, what: str) -> str:
    """Validate a name typed on the command line before it can reach the filesystem.

    slugify() is deliberately lossy, which makes it a terrible gatekeeper on its own:
    "Alex Example\x00" slugs to `alex-example`, exactly like the real thing, and a
    retire once landed on the wrong print because of it. An invisible character is not a
    spelling — refuse the name outright rather than quietly normalise it into somebody
    else's voiceprint.
    """
    s = "" if raw is None else str(raw)
    bad = sorted({c for c in s if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp")})
    if bad:
        fail(f"{what} contains invisible control character(s) "
             f"({', '.join('U+%04X' % ord(c) for c in bad)}). Two names that look "
             f"identical on screen would share one voiceprint file. Retype it.")
    if "/" in s or "\\" in s:
        fail(f"{what} contains a path separator. A voiceprint name is a person, "
             f"not a path: {s!r}")
    s = " ".join(s.split())
    if not s:
        fail(f"{what} is empty.")
    if len(s) > 120:
        fail(f"{what} is {len(s)} characters long — that is not a name (limit 120).")
    # the same transformation slugify() applies, asked only whether anything survives it
    if not re.sub(r"[^a-zA-Z0-9]+", "",
                  unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()):
        fail(f"{what} has no letters or digits to build a filename from: {s!r}")
    return s


def pick_print(people: list[dict], want: str, stems: bool = False) -> dict | None:
    """find_print, but it refuses to guess when two records answer to one name.

    The retired store is allowed to hold repeats — `jonathan-holiday.npy` and
    `jonathan-holiday-2.npy` both sit there today under the identical display name. Both
    are real, separate people's vectors. Picking "the most recent" the way `mt restore`
    does is fine for putting one back; it is not fine for renaming, where the wrong pick
    writes a name onto a stranger. `stems` lets you disambiguate by file stem.
    """
    hits = [p for p in people if find_print([p], want)]
    if not hits and stems:
        hits = [p for p in people if Path(str(p.get("file") or "")).stem == want]
    if len(hits) > 1:
        listed = "\n    ".join(
            f"{Path(str(h.get('file') or '?')).stem:<28} "
            f"{h.get('samples', 1)} sample(s), added {str(h.get('added', '?'))[:10]}"
            for h in hits)
        fail(f"{len(hits)} voiceprints answer to {want!r} — name the one you mean by its "
             f"file stem:\n    {listed}")
    return hits[0] if hits else None


def contact_note(name: str) -> str:
    """One line on whether the contacts DB already knows this name. READ-ONLY, always.

    mt.py owns voiceprints; the database owns people, and until now the two had no code
    path between them at all. This one looks across that line and reports — it opens the
    database `mode=ro` and never writes. Whether a person belongs in the contact list is
    the user's call, not a side effect of fixing a typo.
    """
    db = P.DB
    if not db.exists():
        return ""
    try:
        import sqlite3          # not needed to record a meeting; imported only here
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute(
                "select id, name, customer_id from contacts "
                "where lower(trim(name)) = lower(trim(?)) and merged_into is null "
                "order by id", (name,)).fetchall()
            cust = dict(con.execute("select id, name from customers").fetchall())
        finally:
            con.close()
    except Exception as e:
        return f"{DIM}   could not check contacts: {e}{RST}"
    if not rows:
        return (f"{YEL}   no contact called \"{name}\" — add one at "
                f"http://127.0.0.1:8765/contacts{RST}")
    shown = ", ".join(f"#{i} {n}" + (f" ({cust[c]})" if c in cust else "")
                      for i, n, c in rows)
    word = "contact" if len(rows) == 1 else f"{len(rows)} contacts"
    return f"{GRN}   matches {word} {shown}{RST}"


def session_dirs() -> list[Path]:
    """Every session directory in the workspace that carries a speakers.json."""
    roots = list(P.CUSTOMERS.glob("*/meetings/*"))
    roots += list(P.TEAM_MEETINGS.glob("*"))
    roots += list(RECORDINGS.glob("*"))
    return sorted((d for d in roots if (d / "speakers.json").is_file()), key=str)


def cmd_rename(a):
    """Re-spell a voiceprint. The vector, the sample count and the history all stay put.

    Before this existed, fixing "Alex Exmaple" cost a `forget` plus a fresh `enroll`: you
    needed the original recording back and the sample count started at one again. So
    typos simply got left alone, and by Sep 2026 six enrolled prints matched no contact,
    half of them pure spelling drift — `Alex Exmaple` against Alex Example, `Sam Sampel`
    against Sam Sample. Renaming is the cheap fix, and it only ever moves a label.

    It never proposes a spelling. Both names are the user's.
    """
    old = clean_name(a.old, "the current name")
    new = clean_name(a.new, "the new name")
    retired = bool(getattr(a, "retired", False))
    do_sessions = bool(getattr(a, "sessions", False))
    store = RETIRED if retired else VOICEPRINTS
    where = "retired" if retired else "enrolled"
    idx_path = store / "index.json"
    doc = read_index(idx_path)
    people = doc["speakers"]

    rec = pick_print(people, old, stems=retired)
    if rec is None:
        # Idempotence, on purpose: a default run prints "re-run with --sessions", and by
        # the time you do the print already carries the new name. That second run is the
        # advertised path, so it must not be an error.
        rec = pick_print(people, new, stems=retired)
        if rec is None:
            known = ", ".join(sorted(str(p.get("name") or p.get("slug") or "?")
                                     for p in people))
            fail(f"no {where} voiceprint called {old!r}.\n    "
                 + (f"{where.capitalize()}: {known}" if known
                    else f"Nothing is {where} yet."))
        old_name = old
        say(f"{DIM}the voiceprint is already called \"{rec['name']}\" — "
            f"nothing to rename.{RST}")
    else:
        old_name = str(rec.get("name") or "")
        new_slug = slugify(new)
        if old_name.strip() == new and rec.get("slug") == new_slug:
            fail(f"that print is already called \"{new}\" — nothing to do.")

        clash = next((p for p in people if p is not rec and find_print([p], new)), None)
        if retired and find_print(read_index(VOICEPRINTS / "index.json")["speakers"], new):
            # Not an error — `mt forget` produces exactly this state. But `mt restore`
            # refuses to put a print back on top of a live one, so say so now rather than
            # let it surface months later when the restore is the urgent thing.
            warn(f"\"{new}\" is also enrolled live — `mt restore` will refuse this "
                 f"retired print until that one is retired too")
        if clash is not None and not retired:
            fail(f"\"{clash['name']}\" is already enrolled ({clash.get('file')}). "
                 f"Renaming onto it would\n    put two people's voices under one name "
                 f"and there would be nothing left to\n    tell them apart. If you mean "
                 f"to replace that print:\n      mt forget \"{clash['name']}\"")

        src = store / rec["file"] if rec.get("file") else None
        dest = store / f"{new_slug}.npy"
        if src is not None and dest != src and dest.exists():
            if retired:
                # `mt forget` already suffixes rather than overwrite, because the retired
                # tree is an archive of distinct vectors that happen to share a spelling.
                # Same reasoning here: keep both, never clobber one with the other.
                n = 2
                while dest.exists():
                    dest = store / f"{new_slug}-{n}.npy"
                    n += 1
                warn(f"{new_slug}.npy is already retired — keeping both, this one "
                     f"becomes {dest.name}")
            else:
                fail(f"{dest.name} already exists in {store} but nothing in the index "
                     f"claims it.\n    Move it aside yourself — nothing was changed.")

        before = dict(rec)
        moved = False
        if src is not None and dest != src:
            if not src.exists():
                fail(f"the vector for {old_name} is missing: {src}\n"
                     f"    Nothing was changed.")
            src.rename(dest)      # a move inside the store; nothing is ever unlinked
            moved = True

        rec["name"] = new
        rec["slug"] = new_slug
        if src is not None:
            rec["file"] = dest.name
        if retired and before.get("retired_from"):
            # `mt restore` puts the vector back as retired_from. Leave that pointing at
            # the old misspelling and a restored print lands in the index under one name
            # and on disk under another.
            rec["retired_from"] = f"{new_slug}.npy"
        rec["updated"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            write_index(idx_path, doc)
        except Exception as e:
            if moved:
                dest.rename(src)
            rec.clear()
            rec.update(before)
            fail(f"could not write {idx_path}: {e}\n    Nothing was changed.")

        step(f"renamed {old_name} → {new}" + ("  (retired)" if retired else ""))
        if moved:
            say(f"{DIM}   {Path(before['file']).name} → {dest.name}{RST}")
        say(f"{DIM}   {rec.get('samples', 1)} sample(s), added "
            f"{str(rec.get('added', '?'))[:10]} — vector untouched{RST}")

    note = contact_note(new)
    if note:
        say(note)

    # ---- which sessions still say the old name
    wanted = {old.strip().lower(), old_name.strip().lower()} - {new.strip().lower(), ""}
    targets = []
    for d in session_dirs():
        sp = d / "speakers.json"
        try:
            sdoc = json.loads(sp.read_text())
        except Exception as e:
            warn(f"unreadable {sp}: {e}")
            continue
        keys = [k for k, v in (sdoc.get("map") or {}).items()
                if str((v or {}).get("name", "")).strip().lower() in wanted]
        if keys:
            targets.append((d, sdoc, keys))

    if not targets:
        say(f"{DIM}no session transcript carries that name.{RST}")
        return 0

    if not do_sessions:
        # Deliberately not automatic. Renaming a print changes a label; rewriting sessions
        # rewrites transcripts the user quotes to customers. They get to see the blast radius
        # before it happens.
        say("")
        step(f"{len(targets)} session(s) still label this voice \"{old_name}\" — "
             f"none were changed")
        for d, _, keys in targets:
            say(f"   {workspace_rel(d):<62} {', '.join(keys)}")
        say("")
        say(f"{DIM}   rewrite and re-render all of them:{RST}")
        say(f"     mt rename \"{old_name}\" \"{new}\" --sessions"
            + (" --retired" if retired else ""))
        say(f"{DIM}   or fix one by hand — edit its speakers.json, then:{RST}")
        for d, _, keys in targets:
            say(f"     mt relabel \"{d}\"")
        return 0

    step(f"rewriting {len(targets)} session(s)")
    for d, sdoc, keys in targets:
        for k in keys:
            # the label only. `source` and `confidence` are the user's judgement about WHO
            # this voice is, and a respelling does not reopen that question.
            sdoc["map"][k]["name"] = new
        (d / "speakers.json").write_text(json.dumps(sdoc, indent=2) + "\n")
        say(f"   {workspace_rel(d):<62} {', '.join(keys)}")
        if (d / "transcript.json").exists():
            cmd_relabel(argparse.Namespace(session=str(d)))
        else:
            warn(f"no transcript.json in {workspace_rel(d)} — speakers.json updated, "
                 f"nothing re-rendered")
    return 0


def cmd_cues(a):
    """Vocal cues for one meeting, or for every meeting with --all. Opt-in by design: a
    meeting is only ever measured because the user asked for it here or in `mt web`, and only
    measured meetings feed anyone's baseline. See REFERENCE.md."""
    import cues
    if not a.all and not a.session:
        fail("give a session folder, or --all")
    cur = active_capture()
    if cur is not None and capture_liveness(*cur).get("live") and not a.clear:
        fail("a recording is running — vocal cues wait until it ends, so they can never "
             "slow the live transcript")
    targets = session_dirs() if a.all else [Path(a.session).expanduser().resolve()]
    for d in targets:
        if not (d / "transcript.json").exists():
            if not a.all:
                fail(f"no transcript.json in {d}")
            continue
        if a.clear:
            say(f"{d.name}: " + ("cleared" if cues.clear(d) else "nothing to clear"))
            continue
        s = cues.score(d) if (d / cues.FILE).exists() else {"analyzed": False}
        if a.force or not s.get("analyzed") or s.get("stale"):
            step(f"measuring {d.name}")
            last = [-1]
            def prog(i, n):
                pct = int(100 * i / max(n, 1))
                if pct // 10 != last[0]:
                    last[0] = pct // 10
                    print(f"progress {i}/{n}", flush=True)
            cues.extract(d, progress=prog)
        # Laughter for Best Saying (step 4) — once per meeting, or again with --force.
        # Skipped quietly when the tagging model is not installed; ./make.sh fetches it.
        import awards
        if (a.force or not (d / awards.LAUGHS).exists()) and awards.tagger() is not None:
            print("progress laughter", flush=True)
            awards.scan(d)
        if not a.all:
            say(cues.summary_text(cues.score(d)))
            say(awards.summary_text(awards.view(d)))
    if a.all and not a.clear:
        say(f"measured {len(targets)} meeting(s); baselines now draw on all of them")
    return 0


def cmd_relabel(a):
    session = Path(a.session).expanduser().resolve()
    tj, sp = session / "transcript.json", session / "speakers.json"
    for f in (tj, sp):
        if not f.exists():
            fail(f"missing {f}")
    doc = json.loads(tj.read_text())
    names = json.loads(sp.read_text()).get("map", {})
    doc["speakers"] = names
    # Refresh the derived meta so a re-render picks up anything discovered since the
    # original pass (a dead system tap, a mic unplugged) instead of silently using the
    # stale copy stored in transcript.json.
    doc.setdefault("meta", {})
    # A mic-only transcript never used the system track; its silence is not a death.
    used_system = any(e.get("track") == "system" for e in doc.get("timeline", []))
    doc["meta"]["system_dead_at"] = system_died_at(session) if used_system else None
    doc["meta"]["mic_timeline"] = mic_timeline(session)
    tj.write_text(json.dumps(doc, indent=2) + "\n")
    (session / "transcript.md").write_text(
        render_markdown(session, doc["meta"], doc["timeline"], names))
    (session / "transcript.txt").write_text("\n".join(
        f"[{hhmmss(e['start'])}] "
        f"{e.get('override') or names.get(e['speaker_key'],{}).get('name', e['speaker_key'])}: {e['text']}"
        for e in doc["timeline"]) + "\n")
    step(f"re-rendered {session/'transcript.md'}")
    return 0


# ---------------------------------------------------------------- search

def _search_module():
    """Import search.py lazily.

    It is never imported at module scope: recording must keep working even if the
    index module is missing or broken. Raises ImportError for the caller to handle.
    """
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import search
    return search


def _unmark(snippet: str) -> str:
    """fts5 hands back HTML-escaped text with the matches wrapped in <mark>.

    Terminals do not speak HTML: lift the marks out, un-escape the entities, and
    bold the matched span instead (a no-op when stderr is not a tty).
    """
    s = snippet.replace("<mark>", "\x00").replace("</mark>", "\x01")
    s = " ".join(html.unescape(s).split())
    return s.replace("\x00", BOLD).replace("\x01", RST)


def cmd_search(a):
    try:
        search = _search_module()
    except Exception as e:
        fail(f"search is unavailable: {e}\n"
             f"       search.py should live next to mt.py in {HERE}")

    st = {}
    try:
        st = search.stats()
    except Exception:
        pass
    if not st.get("docs"):
        if a.json:
            print("[]")
        say("the search index is empty or missing. Build it with:")
        say("   mt index")
        return 0

    try:
        hits = search.search(a.query, limit=a.limit, kind=a.kind,
                             customer=a.customer, since=a.since)
    except Exception as e:
        fail(f"search failed: {e}")

    if a.json:
        print(json.dumps(hits, indent=2))
        return 0

    if not hits:
        say(f"no matches for {a.query!r}"
            f"{' with those filters' if (a.kind or a.customer or a.since) else ''}.")
        return 0

    say("")
    for i, h in enumerate(hits, 1):
        where = " · ".join(x for x in (h.get("customer"), h.get("date")) if x)
        say(f"{BOLD}{i:>2}. {h.get('title') or h.get('path')}{RST}"
            + (f"  {DIM}{where}{RST}" if where else ""))

        bits = [h.get("kind") or "?"]
        if h.get("kind") == "transcript_segment":
            if h.get("speaker"):
                bits.append(h["speaker"])
            if h.get("t_start") is not None:
                bits.append(hhmmss(h["t_start"]))
        say(f"{DIM}    {' · '.join(bits)}{RST}")

        if h.get("snippet"):
            say(f"    {_unmark(h['snippet'])}")

        loc = h.get("path") or ""
        if h.get("kind") == "transcript_segment" and loc:
            loc = str(Path(loc).parent)          # the session dir, not transcript.json
        if loc:
            say(f"{DIM}    {loc}{RST}")
        say("")

    say(f"{DIM}{len(hits)} hit(s){RST}")
    return 0


def cmd_index(a):
    try:
        search = _search_module()
    except Exception as e:
        fail(f"search is unavailable: {e}\n"
             f"       search.py should live next to mt.py in {HERE}")

    step("rebuilding the search index from scratch" if a.rebuild
         else "updating the search index")
    try:
        r = search.index_all(rebuild=a.rebuild, verbose=True)
    except Exception as e:
        fail(f"indexing failed: {e}")

    say("")
    step("done")
    say(f"   {r.get('docs', 0)} doc(s) indexed in {r.get('seconds', 0):.1f}s"
        f"  ({r.get('added', 0)} added, {r.get('updated', 0)} updated, "
        f"{r.get('removed', 0)} removed)")

    st = search.stats()
    for kind, n in sorted(st.get("by_kind", {}).items(), key=lambda kv: (-kv[1], kv[0])):
        say(f"   {kind:<20} {n}")
    say(f"{DIM}   {st.get('db_bytes', 0) / 1e6:.1f} MB   {search.INDEX_DB}{RST}")
    if st.get("indexed_at"):
        say(f"{DIM}   last indexed {st['indexed_at']}{RST}")
    return 0


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    """The whole CLI in one place.

    Split out of main() so `mt web`'s /help page can introspect the real parser rather
    than carry a second, hand-written list of commands that goes stale the first time a
    flag is added. The page and `mt --help` therefore cannot disagree.
    """
    p = argparse.ArgumentParser(prog="mt", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def shared(q):
        q.add_argument("--customer", help="file under Customers/<name>/meetings/ (acme, globex, …)")
        q.add_argument("--name", help="short slug for the folder, e.g. acme-weekly")
        q.add_argument("--title", help="human title for the transcript header")
        q.add_argument("--roster", help='comma-separated attendees, in speaking order if you know it')
        q.add_argument("--speakers", type=int, help="exact number of far-end voices, if you know it")
        q.add_argument("--owner", help="your name for the mic track")
        q.add_argument("--language", help="language code (default en)")
        q.add_argument("--prompt", help="hint whisper with names/jargon to spell correctly")
        q.add_argument("--diarize-mic", action="store_true", help="split the mic track too")
        q.add_argument("--in-person", action="store_true", help="several people share your mic")
        q.add_argument("--invitees", help="comma-separated attendee names; saved as "
                                          "invitees.json and used to resolve the initials "
                                          "Teams shows while someone screen-shares")
        q.add_argument("--no-system", action="store_true",
                       help="transcribe only your mic (in-person). The system track is still "
                            "recorded but ignored, so nothing is lost if you change your mind")
        q.add_argument("--no-screen", action="store_true",
                       help="do not read participant names off the meeting window")
        q.add_argument("--keep-frames", action="store_true",
                       help="save a Teams frame every 5 s into <session>/frames, for tuning "
                            "active-speaker detection (local only; delete afterwards)")
        q.add_argument("--keep-wav", action="store_true",
                       help="keep the raw wav instead of archiving it to flac")
        q.add_argument("--force-asr", action="store_true",
                       help="re-run transcription instead of reusing a previous pass")

    q = sub.add_parser("doctor", help="check install + permissions"); q.set_defaults(f=cmd_doctor)
    q = sub.add_parser("version", help="print the installed version"); q.set_defaults(f=cmd_version)
    q = sub.add_parser("init", help="create the workspace folders and a new database "
                                    "(never touches an existing one)")
    q.set_defaults(f=cmd_init)
    q = sub.add_parser("devices", help="list microphones and speakers, and what mt "
                                       "would pick at this desk")
    q.add_argument("--forget", action="store_true",
                   help="drop every remembered mic/speaker pick")
    q.set_defaults(f=cmd_devices)

    q = sub.add_parser("recapture", help="rebuild the system-audio tap on the running "
                                         "recording (the same button mt web offers)")
    q.set_defaults(f=cmd_recapture)

    q = sub.add_parser("calendar", help="fetch today's calendar and cache it "
                                        "(answers gog's keyring prompt on this terminal)")
    q.set_defaults(f=cmd_calendar)

    q = sub.add_parser("web", help="local app: today's meetings, record, review")
    q.add_argument("--port", type=int, default=8765)
    q.add_argument("--no-open", action="store_true", help="do not open a browser")
    q.set_defaults(f=cmd_web)

    q = sub.add_parser("record", help="record a meeting then transcribe it")
    q.add_argument("--allow-concurrent", action="store_true",
                   help=argparse.SUPPRESS)
    shared(q)
    q.add_argument("--mic-id", help="microphone id or name fragment from `mt devices`; "
                                    "remembered for this desk")
    q.add_argument("--speaker-id", help="speaker (output) id or name fragment from "
                                        "`mt devices`; set before recording and remembered "
                                        "for this desk")
    q.add_argument("--no-mic", action="store_true", help="system audio only")
    q.add_argument("--duration", type=int, help="auto-stop after N seconds")
    q.add_argument("--no-process", action="store_true", help="record only, transcribe later")
    q.set_defaults(f=cmd_record)

    q = sub.add_parser("live", help="rolling transcript during the call, full pass after")
    q.add_argument("--allow-concurrent", action="store_true",
                   help=argparse.SUPPRESS)
    shared(q)
    q.add_argument("--mic-id", help="microphone id or name fragment from `mt devices`; "
                                    "remembered for this desk")
    q.add_argument("--speaker-id", help="speaker (output) id or name fragment from "
                                        "`mt devices`; set before recording and remembered "
                                        "for this desk")
    q.add_argument("--no-mic", action="store_true", help="system audio only")
    q.add_argument("--duration", type=int, help="auto-stop after N seconds")
    q.add_argument("--live-model", help="ggml model filename for the live pass")
    q.add_argument("--cues", action="store_true",
                   help="vocal cues (experimental, opt-in): measure delivery live for the "
                        "Room panel, and again after the full pass")
    q.add_argument("--no-queue", action="store_true",
                   help="run the full pass in this process as soon as the call ends, "
                        "instead of queueing it behind any recording (`mt queue`)")
    q.set_defaults(f=cmd_live)

    q = sub.add_parser("process", help="transcribe a session dir or media file")
    q.add_argument("path")
    shared(q)
    q.add_argument("--no-mic", action="store_true",
                   help="ignore the mic track and transcribe only the system track (e.g. a "
                        "room meeting whose Teams audio already carries everyone)")
    q.set_defaults(f=cmd_process)

    q = sub.add_parser("queue", help="after-call processing: list what is waiting, or "
                                     "process a meeting now")
    q.add_argument("action", nargs="?", default="list",
                   choices=("list", "now", "retry", "dismiss", "add", "run", "job"),
                   help="list (default) · now <id>: skip the busy/calendar holds · "
                        "retry <id> · dismiss <id>: drop a failed job (the recording "
                        "stays) · add <session>: queue a recording that was never "
                        "processed · run: start the worker by hand · job: internal")
    q.add_argument("target", nargs="?", help="job id from `mt queue`, or a session folder")
    q.set_defaults(f=cmd_queue)

    q = sub.add_parser("enroll", help="attach a real name to a detected voice, permanently")
    q.add_argument("session")
    q.add_argument("--speaker", required=True, help="speaker number from the transcript, or its key")
    q.add_argument("--name", required=True, help="full name")
    q.add_argument("--replace", action="store_true",
                   help="overwrite the stored print with this sample instead of "
                        "averaging into it")
    q.add_argument("--force", action="store_true",
                   help="enroll even when this sample contradicts the prints already stored")
    q.set_defaults(f=cmd_enroll)

    q = sub.add_parser("speakers", help="list enrolled voiceprints"); q.set_defaults(f=cmd_speakers)

    q = sub.add_parser("forget", help="retire an enrolled voiceprint (reversible, never deleted)")
    q.add_argument("name", help="display name or slug")
    q.add_argument("--reason", help="why it is being retired, recorded alongside it")
    q.set_defaults(f=cmd_forget)

    q = sub.add_parser("restore", help="put a retired voiceprint back")
    q.add_argument("name", help="display name or slug")
    q.set_defaults(f=cmd_restore)

    q = sub.add_parser("rename", help="fix the spelling of an enrolled name "
                                      "(keeps the vector and the sample count)")
    q.add_argument("old", help="the name as it is stored now — display name or slug")
    q.add_argument("new", help="the name it should be")
    q.add_argument("--retired", action="store_true",
                   help="rename a retired print instead of a live one")
    q.add_argument("--sessions", action="store_true",
                   help="also rewrite the name in every session that uses it, and "
                        "re-render those transcripts")
    q.set_defaults(f=cmd_rename)

    q = sub.add_parser("relabel", help="re-render after editing speakers.json")
    q.add_argument("session")
    q.set_defaults(f=cmd_relabel)

    q = sub.add_parser("cues", help="vocal cues: measure how each confirmed speaker's "
                                    "delivery moves against their own normal (opt-in)")
    q.add_argument("session", nargs="?", help="session folder")
    q.add_argument("--all", action="store_true",
                   help="measure every meeting — you are opting all of them in, and they "
                        "all become baseline")
    q.add_argument("--force", action="store_true", help="re-measure even if already done")
    q.add_argument("--clear", action="store_true",
                   help="delete the measurements (cues.json); nothing else is touched")
    q.set_defaults(f=cmd_cues)

    q = sub.add_parser("search", help="full-text search across meetings, recaps and notes")
    q.add_argument("query")
    q.add_argument("--kind", help="summary, transcript_segment, transcript, recap, memory, note")
    q.add_argument("--customer", help="limit to one account (acme, globex, …)")
    q.add_argument("--limit", type=int, default=20, help="max hits (default 20)")
    q.add_argument("--since", help="only hits dated on or after YYYY-MM-DD")
    q.add_argument("--json", action="store_true", help="print the raw hit list as JSON")
    q.set_defaults(f=cmd_search)

    q = sub.add_parser("index", help="(re)build the search index")
    q.add_argument("--rebuild", action="store_true",
                   help="drop the index and rebuild it instead of updating by mtime")
    q.set_defaults(f=cmd_index)

    return p


# Options some commands lack; every handler may read them, so they default to None.
OPTIONAL_ARGS = ("customer", "name", "title", "roster", "speakers", "owner", "language",
                 "prompt", "diarize_mic", "in_person", "no_process", "force_asr",
                 "mic_id", "speaker_id", "no_mic", "duration", "live_model", "keep_wav",
                 "no_screen", "keep_frames", "allow_concurrent", "no_system", "invitees",
                 "replace", "force", "reason", "old", "new", "retired", "sessions")


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    for k in OPTIONAL_ARGS:
        if not hasattr(a, k):
            setattr(a, k, None)
    try:
        return a.f(a)
    except subprocess.CalledProcessError as e:
        fail(f"{Path(e.cmd[0]).name} failed:\n{(e.stderr or e.stdout or '').strip()[-1500:]}")
    except KeyboardInterrupt:
        say("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())

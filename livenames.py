"""Live naming — tell mt who is talking DURING the call, and have it stick.

Born on an Oct 6 training call: 33 far-end chunks were labelled "Alex Example" (3 auto,
30 'maybe') and every one was Sam Sample, who has no voiceprint and sounds like Alex
at 0.64 — right at the 0.62 auto-name line. The user could see it was wrong and had no way
to say so until the review page.

How it works:
  * In the Room panel the user corrects a card: confirm a near-miss ("✓ it's Alex"), rename
    a match or a near-miss ("✎ actually Sam"), or name the blended far end.
  * The audio behind that card becomes an IN-CALL voice sample for the name, kept in
    <session>/live_names.json (same class of data as the work/*-spk*.npy embeddings that
    already sit in every session folder — never in voiceprints/, never shared).
  * The live loop reloads the file each chunk: later chunks that match a sample are named,
    and the live transcript shows the name too.
  * After the full pass, every diarized cluster is compared with the samples and the
    review page opens with those names filled in.

What it never does: enrol a voiceprint (that stays a deliberate step on the review page,
behind enroll_guard), or edit the people map. A live sample can be undone.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import numpy as np

import mt as MT

FILE = "live_names.json"
# Measured on a snapshot of the Oct 6 call after naming "maybe Alex" → Sam Sample:
#   live threshold  Sam caught      others wrongly named      (0.70: 47% / 0.6%,
#   0.62            74%             1.1%                       0.55: 94% / 2.2%)
# 0.62 is also the stored-print auto-name line, so one number means one thing. The rest
# of their chunks are caught anyway by the correction rule ("maybe Alex" → Sam).
LIVE_MATCH = 0.62
# Two tiers, the owner's call Oct 8: a NAME? as soon as a voice clears 0.55 (94% caught in the
# table above), shown as a guess with a one-tap ✓, never as a name; the name firms up at
# 0.62. Firming mostly comes from the voice GROUP's average, not one line — on the Oct 7
# call, single lines scored a median 0.57 for one speaker (4 of 21 over 0.62) while their
# group's average scored 0.69, and every group average picked the person the full pass did.
LIVE_TENTATIVE = 0.55
GROUP_FIRM_SEC = 8.0   # …and a group average only names a voice once it holds this much speech
POST_SUGGEST = 0.72    # after the pass: a cluster this close is pre-filled for review
POST_STRONG = 0.85     # …marked confirmed at this, whatever the other clusters score;
POST_CONFIRM = 0.75    # …or at this when it is also clearly the one:
POST_MARGIN = 0.20     # ahead of every other cluster for that name by this much, and the
                       # name is a verified contact. (Same-call cluster vs sample: 0.81.)
THEM_WINDOW = 60.0     # naming the blended far end names its last minute of speech


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load(d: Path) -> dict:
    try:
        doc = json.loads((d / FILE).read_text())
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("rules", [])      # {"kind": match|maybe|them, "from": name|None, "to": name,
                                     #  "segments": [[s, e], …], "at": iso}
    doc.setdefault("prints", {})     # name -> {"vec": [...], "secs": float}
    return doc


def save(d: Path, doc: dict) -> None:
    tmp = d / (FILE + ".tmp")
    tmp.write_text(json.dumps(doc) + "\n")
    os.replace(tmp, d / FILE)        # the live loop must never read half a file


# ── audio ─────────────────────────────────────────────────────────────────────

def _segment_audio(d: Path, segs: list) -> np.ndarray:
    """The far-end audio for these [start, end] spans: from the wav still being written,
    or the archived flac once the call is over."""
    wav = d / "system.wav"
    parts = []
    if wav.exists():
        off = MT.wav_data_offset(wav)
        with open(wav, "rb") as f:
            for s, e in segs:
                f.seek(off + int(s * 16000) * 2)
                raw = f.read(max(0, int((e - s) * 16000)) * 2)
                parts.append(np.frombuffer(raw[: len(raw) // 2 * 2], np.int16).astype(np.float32) / 32768)
    else:
        import cues
        src = cues.track_file(d, "system")
        if src is None:
            return np.zeros(0, np.float32)
        x = cues.decode(src)
        parts = [x[int(s * 16000):int(e * 16000)] for s, e in segs]
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def _rebuild_prints(d: Path, doc: dict) -> None:
    """One in-call sample per thing you NAMED — not one blend per person. On the Oct 6
    call a guest dropped and rang back from another phone; the three groups the user
    named sat 0.63 apart, and one blended sample scored 0.67 against their own cluster
    (under the pre-fill line) where the best single group scored 0.94."""
    prints: dict = {}
    for r in doc["rules"]:
        x = _segment_audio(d, r["segments"])
        v = MT.embed_audio(x) if x.size >= 16000 else None
        p = prints.setdefault(r["to"], {"vecs": [], "secs": 0.0})
        p["secs"] = round(p["secs"] + sum(e - s for s, e in r["segments"]), 1)
        if v is not None:
            p["vecs"].append([round(float(a), 6) for a in v])
    doc["prints"] = {n: p for n, p in prints.items() if p["vecs"]}


def _vecs(p: dict) -> list:
    """A name's samples; files written before per-group samples hold a single 'vec'."""
    return p.get("vecs") or ([p["vec"]] if p.get("vec") else [])


def best(vec: np.ndarray, p: dict) -> float:
    """How close a voice is to a name: the closest of that name's samples."""
    arrs = p.get("np") or [np.asarray(v, np.float32) for v in _vecs(p)]
    return max((MT.cosine(vec, a) for a in arrs), default=-1.0)


# ── separating the far end into Speaker 1, 2, … ───────────────────────────────
# Measured on the Oct 6 snapshot: 198 unmatched far-end segments (≥ 1.5 s) grouped by voice
# at 0.55 into 4 groups of 3+ covering 91% of them — and the biggest (123) was one voice
# the stored prints had been calling both "maybe Alex Example" and "maybe Sam Sample".
# So cards are grouped by VOICE, with the prints' best guess shown as a hint.
VOICE_GROUP = 0.55
# 2, not 3 (Oct 8): someone who speaks twice in a call still gets a card to name while
# the user remembers who it was. On the Oct 7 call, 13 voices said ONE line each (mostly
# fragments of known voices) and stay in "Them"; 2 lines added one card.
VOICE_MIN_SEGS = 2
VOICE_MIN_SEC = 1.5
_extractor = None


def embed(x: np.ndarray) -> list | None:
    """One segment's voice embedding, from the raw audio like every voiceprint. The model
    is loaded once (MT.embed_audio reloads it per call: fine for a pass, too slow live)."""
    global _extractor
    if x.size < int(VOICE_MIN_SEC * 16000):
        return None
    if _extractor is None:
        import sherpa_onnx
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MT.EMB_MODEL),
                                                        num_threads=2, provider="cpu"))
    st = _extractor.create_stream()
    st.accept_waveform(16000, x[: int(MT.EMBED_CAP_SEC * 16000)])
    st.input_finished()
    v = np.asarray(_extractor.compute(st), np.float32)
    n = float(np.linalg.norm(v))
    return [round(float(a), 4) for a in v / n] if n else None


def assign_voices(d: Path, rows: list[dict]) -> None:
    """Split the far end that no stored voiceprint claimed into "Speaker N" groups, online
    and in time order. Run on what the LOOP saw, before your names are applied, so naming
    Speaker 2 never renumbers Speaker 3 (a first cut did). Sets rows["voice"]."""
    cents, counts, first = [], [], []
    member = {}
    for i, r in enumerate(rows):
        if r.get("track") != "system" or r.get("who") or not r.get("v"):
            continue
        v = np.asarray(r["v"], np.float32)
        sims = [MT.cosine(v, c) for c in cents]
        k = int(np.argmax(sims)) if sims else -1
        if k >= 0 and sims[k] >= VOICE_GROUP:
            cents[k] = cents[k] * counts[k] / (counts[k] + 1) + v / (counts[k] + 1)
            counts[k] += 1
        else:
            cents.append(v.copy()); counts.append(1); first.append(r["s"]); k = len(cents) - 1
        member[i] = k
    big = sorted((k for k, n in enumerate(counts) if n >= VOICE_MIN_SEGS), key=lambda k: first[k])
    number = {k: j + 1 for j, k in enumerate(big)}
    for i, k in member.items():
        if k in number:
            rows[i]["voice"] = f"Speaker {number[k]}"


def learned(d: Path, rows: list[dict]) -> None:
    """After your names are applied: any far-end row still unnamed that sounds like a voice
    you named this call (≥ LIVE_MATCH) takes that name."""
    doc = cached(d)
    # A name you gave "Speaker N" follows that voice group from then on (the numbering is
    # stable, see assign_voices) — newest decision wins.
    by_voice = {}
    for r in doc["rules"]:
        if r["kind"] == "voice" and r.get("from"):
            by_voice[r["from"]] = r["to"]
    for r in rows:
        if r.get("track") == "system" and not r.get("who") and r.get("voice") in by_voice:
            r["who"], r["hint"] = by_voice[r["voice"]], None
    if not doc["prints"]:
        return
    for r in rows:
        if r.get("track") != "system" or r.get("who") or not r.get("v"):
            continue
        v = np.asarray(r["v"], np.float32)
        nm, sc = max(((n, best(v, p)) for n, p in doc["prints"].items()), key=lambda t: t[1])
        if sc >= LIVE_MATCH:
            r["who"], r["hint"] = nm, None
        elif sc >= LIVE_TENTATIVE:
            r["hint"] = nm               # "maybe Alex?" on the card, one tap to confirm


_stored: dict = {}


def _stored_prints() -> list:
    """The enrolled voiceprints, re-read only when index.json changes (the Room polls)."""
    try:
        m = (MT.VOICEPRINTS / "index.json").stat().st_mtime
    except OSError:
        return []
    if _stored.get("m") != m:
        _stored.update(m=m, people=MT.load_voiceprints())
    return _stored["people"]


def group_names(d: Path, rows: list[dict]) -> list[dict]:
    """The more a voice says, the surer its name. Each still-unnamed "Speaker N" group is
    averaged and matched as one voice: against what you named this call, then the stored
    voiceprints. ≥ 0.62 with GROUP_FIRM_SEC of speech names the whole group; ≥ 0.55 (or a
    firm score on too little speech) puts the name on its card as a guess. Never final —
    the review page asks again after the pass."""
    groups: dict = {}
    for r in rows:
        if r.get("track") == "system" and not r.get("who") and r.get("voice") and r.get("v"):
            groups.setdefault(r["voice"], []).append(r)
    if not groups:
        return []
    touched = []
    live = cached(d)["prints"]
    people = _stored_prints()
    for rs in groups.values():
        c = np.mean([np.asarray(r["v"], np.float32) for r in rs], axis=0)
        n = float(np.linalg.norm(c))
        if not n:
            continue
        c = c / n
        cands = [(nm, best(c, p)) for nm, p in live.items()]
        cands += [(p["name"], MT.cosine(c, p["vec"])) for p in people]
        if not cands:
            continue
        nm, sc = max(cands, key=lambda t: t[1])
        secs = sum(r["e"] - r["s"] for r in rs)
        if sc >= LIVE_MATCH and secs >= GROUP_FIRM_SEC:
            for r in rs:
                r["who"], r["hint"], r["match"] = nm, None, round(float(sc), 3)
            touched += rs
        elif sc >= LIVE_TENTATIVE:
            for r in rs:
                r["hint"] = nm
            touched += rs
    return touched


# ── decisions ─────────────────────────────────────────────────────────────────

def add_rule(d: Path, kind: str, frm: str | None, to: str, segments: list) -> dict:
    to = (to or "").strip()
    if not to:
        return {"ok": False, "why": "no name given"}
    if not segments:
        return {"ok": False, "why": "no speech to learn this voice from yet"}
    doc = load(d)
    doc["rules"].append({"kind": kind, "from": frm, "to": to,
                         "segments": [[round(s, 2), round(e, 2)] for s, e in segments],
                         "at": _now()})
    _rebuild_prints(d, doc)
    save(d, doc)
    p = doc["prints"].get(to)
    return {"ok": True, "name": to, "learned_secs": p["secs"] if p else 0,
            "note": None if p else "too little audio to learn the voice yet — the label "
                                   "is kept, and later chunks won't be auto-named until "
                                   "you name a longer stretch"}


def rename(d: Path, old: str, new: str | None) -> dict:
    """Change your mind about a name, mid-call: every decision that named `old` now names
    `new` — or, with no new name, is dropped (the voice goes back to its Speaker number).
    A live name is never final; the review page asks again after the pass."""
    doc = load(d)
    hit = [r for r in doc["rules"] if r["to"] == old]
    if not hit:
        return {"ok": False, "why": f"nothing was named {old} this call"}
    new = (new or "").strip()
    if new:
        for r in hit:
            r["to"] = new
    else:
        doc["rules"] = [r for r in doc["rules"] if r["to"] != old]
    _rebuild_prints(d, doc)
    save(d, doc)
    return {"ok": True, "renamed": len(hit), "to": new or None}


def undo(d: Path) -> dict:
    doc = load(d)
    if not doc["rules"]:
        return {"ok": False, "why": "nothing to undo"}
    gone = doc["rules"].pop()
    _rebuild_prints(d, doc)
    save(d, doc)
    return {"ok": True, "undone": gone}


# ── reading it back ───────────────────────────────────────────────────────────

_cache: dict = {}


def cached(d: Path) -> dict:
    """load(), but only re-read when the file changes — the live loop asks every chunk."""
    f = d / FILE
    try:
        m = f.stat().st_mtime
    except OSError:
        return {"rules": [], "prints": {}}
    hit = _cache.get(str(d))
    if hit and hit[0] == m:
        return hit[1]
    doc = load(d)
    for p in doc["prints"].values():
        p["np"] = [np.asarray(v, dtype=np.float32) for v in _vecs(p)]
    _cache[str(d)] = (m, doc)
    return doc


def match(d: Path, vec: np.ndarray) -> tuple[str | None, float]:
    """The name whose closest in-call sample is nearest this chunk's embedding."""
    top, score = None, -1.0
    for name, p in cached(d)["prints"].items():
        s = best(vec, p)
        if s > score:
            top, score = name, s
    return top, score


def resolve(d: Path, s: float, e: float, who: str | None, hint: str | None,
            track: str = "system") -> tuple[str | None, str | None]:
    """(who, hint) for a live row after your decisions. A correction only ever touches the
    far-end rows that were on the card you corrected: "voice match Sam" rows, "maybe
    Sam" rows, or — for the blended far end — the unmatched rows in the minute you named.
    (A first cut matched any row by time alone and relabelled the user's own mic lines that
    overlapped the far end's.) Newest decision wins, so a correction can itself be corrected."""
    if track != "system":
        return who, hint
    mid = (s + e) / 2
    for r in reversed(cached(d)["rules"]):
        if r["kind"] == "match" and who and who == r["from"]:
            return r["to"], None
        if r["kind"] == "maybe" and not who and hint and hint == r["from"]:
            return r["to"], None
        if r["kind"] == "them" and not who and not hint and \
                any(a - 0.05 <= mid <= b + 0.05 for a, b in r["segments"]):
            return r["to"], None
        if r["kind"] == "voice" and not who and \
                any(a - 0.05 <= mid <= b + 0.05 for a, b in r["segments"]):
            return r["to"], None     # the rows that were "Speaker N" when you named it
    return who, hint


def summary(d: Path) -> list[dict]:
    doc = cached(d)
    out = []
    for r in doc["rules"]:
        what = {"match": f"voice match “{r['from']}”", "maybe": f"“maybe {r['from']}”",
                "them": "the blended far end", "voice": r.get("from") or "a voice"}.get(r["kind"], r["kind"])
        out.append({"what": what, "to": r["to"], "secs": round(sum(b - a for a, b in r["segments"]), 1),
                    "learned": r["to"] in doc["prints"]})
    return out


# ── after the full pass ───────────────────────────────────────────────────────

def verified_names() -> set:
    """Names (and aliases) of verified contacts. Person Verification Protocol: a name you
    typed live that is not in the people map may be pre-filled, never marked confirmed."""
    import sqlite3
    import paths as P
    try:
        con = sqlite3.connect(f"file:{P.DB}?mode=ro", uri=True, timeout=2.0)
        rows = con.execute("select name, aliases from contacts where merged_into is null "
                           "and archived_at is null and verification_status = 'verified'").fetchall()
        con.close()
    except Exception:
        return set()
    out = set()
    for name, aliases in rows:
        out.add((name or "").strip().lower())
        for a in (aliases or "").splitlines():
            if a.strip():
                out.add(a.strip().lower())
    return out


def apply_after_pass(d: Path) -> list[str]:
    """Pre-fill speakers.json from the in-call samples. Only far-end clusters that are not
    mixed, not already confirmed by you, and clearly closest to one sample are touched.
    Returns one line per change, for the log."""
    doc = load(d)
    if not doc["prints"]:
        return []
    spf = d / "speakers.json"
    sp = json.loads(spf.read_text())
    m = sp.get("map", {})
    prints = doc["prints"]
    known = verified_names()
    # Score every far-end cluster against every sample first, so "clearly the one" can be
    # judged against the other clusters, not just against the other names.
    vecs = {}
    for key, v in m.items():
        if key == "mic" or v.get("mixed"):
            continue
        f = d / "work" / f"{key}.npy"
        if f.exists():
            cv = np.load(f)
            vecs[key] = cv.mean(0) if cv.ndim > 1 else cv
    score = {(k, n): best(cv, p) for k, cv in vecs.items() for n, p in prints.items()}
    changes = []
    for key in vecs:
        v = m[key]
        if v.get("confidence") == "verified":
            continue                 # you already confirmed this one; never overwrite it
        scored = sorted(((score[(key, n)], n) for n in prints), reverse=True)
        sc, name = scored[0]
        runner = scored[1][0] if len(scored) > 1 else -1.0
        if sc < POST_SUGGEST or sc - runner < 0.05:
            continue                 # not clearly anyone you named
        other_clusters = [score[(k, name)] for k in vecs if k != key]
        clear_lead = sc - max(other_clusters, default=-1.0) >= POST_MARGIN
        old = v.get("name")
        # Strong on its own (another cluster may be the same person on another phone —
        # one guest's two clusters were 0.94 and 0.78), or merely good and clearly
        # the one; and a verified contact (Person Verification Protocol).
        strong = (sc >= POST_STRONG or (sc >= POST_CONFIRM and clear_lead)) \
            and name.strip().lower() in known
        # PRE-FILL, never confirm. The owner, Oct 6: a live click shouldn't be "a done deal" —
        # the review page is the second chance. (Until then a strong match was marked
        # verified here, so one speaker skipped review entirely.)
        v["name"] = name
        v["source"] = (f"named live by you — {'strong' if strong else 'possible'} match "
                       f"({sc:.2f}); confirm or change")
        v["live_named"] = round(sc, 3)
        v["live_strong"] = bool(strong)    # counts for cues/awards like a strong voiceprint
        v["confidence"] = "unconfirmed"
        changes.append(f"{key}: {old} → {name} ({sc:.2f}, {'strong' if strong else 'possible'}, "
                       f"for you to confirm)")
    if changes:
        spf.write_text(json.dumps(sp, indent=2) + "\n")
    return changes

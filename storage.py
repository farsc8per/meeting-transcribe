#!/usr/bin/env python3
"""
storage.py — how much room each meeting takes, and giving it back.

A session folder holds three kinds of thing, sorted by what losing them costs:

  rebuildable  work/*.wav — the 16 kHz downmixes the pass makes from the FLAC. Only the
               pass reads them, and it rebuilds them when they are missing. Measured Oct
               2026: 13 of 19 GB across 115 meetings. Losing them costs one ffmpeg convert.
  audio        system.* / mic.* at the top of the folder. Playback, re-processing and
               teaching mt a voice all need it. The transcript does not.
  text         transcript, summary, handoff, speaker map, the small cached results in
               work/ — what the meeting *said*. Kilobytes.

Anything else in the folder (your notes, attachments) is "other": counted, shown, and
never moved by anything in this module.

Rebuildable files are deleted outright — nothing is lost. Removing audio or deleting a
meeting never unlinks on the spot: the files move into TRASH and are purged after
`trash_days`. TRASH sits outside the workspace on purpose — a backup that mirrors the
workspace propagates a delete without keeping a copy, so the trash is the undo.

Stdlib only. The caller (web.py) decides which sessions are busy and passes them in; this
module never looks at recorder state itself.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import paths as P

TRASH = P.TRASH

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aiff", ".aif",
             ".mp4", ".mov", ".m4v", ".mkv", ".webm", ".caf"}
AUDIO_STEMS = ("system", "mic")

# Every top-level name mt (or your assistant, for summary.md) writes into a session. A file
# not on this list was put there by someone else and is left alone by delete.
MT_NAMES = {
    "transcript.json", "transcript.md", "transcript.txt", "speakers.json", "summary.md",
    "handoff.json", "archived.json", "invitees.json", "capture.json", "capture_events.jsonl",
    "capture_stderr.log", "capture_health.json", "screen.jsonl", "screen_roster.json",
    "screen_windows.jsonl", "screen_stderr.log", "mic.jsonl", "live.md", "cues.json",
    "cues_requested.json", "cues_live.jsonl", "laughs.json", "laughs_live.jsonl",
    "awards.json", "live_names.json", "tracks.json", "audio_removed.json",
}
MT_DIRS = {"work", "reprocess-backups"}

AUDIO_MARKER = "audio_removed.json"
ENTRY_FILE = "entry.json"
ENTRY_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

# A meeting this short, or with "test" in its name, is offered as a test recording.
TEST_MAX_SECONDS = 90
TEST_WORD = re.compile(r"\btest", re.I)


# ---------------------------------------------------------------- what is what

def _is_audio(rel: Path) -> bool:
    return (len(rel.parts) == 1 and rel.stem in AUDIO_STEMS
            and rel.suffix.lower() in AUDIO_EXT)


def kind_of(rel: Path) -> str:
    """'rebuildable' | 'audio' | 'text' | 'other' for a path relative to the session."""
    top = rel.parts[0]
    if top == "work":
        return "rebuildable" if rel.suffix.lower() == ".wav" else "text"
    if _is_audio(rel):
        return "audio"
    if top in MT_DIRS or (len(rel.parts) == 1 and top in MT_NAMES):
        return "text"
    return "other"


def _files(d: Path):
    """(relative path, size) for every regular file under d. Symlinks are not followed
    and not counted — moving a link would move the link, not the space."""
    for root, dirs, names in os.walk(d):
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(root, x))]
        for n in names:
            p = Path(root) / n
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                yield p.relative_to(d), p.stat().st_size
            except OSError:
                continue


def usage(d: Path) -> dict:
    """Bytes per kind, plus the top-level names of anything mt did not write."""
    out = {"rebuildable": 0, "audio": 0, "text": 0, "other": 0}
    other_names: set[str] = set()
    for rel, size in _files(d):
        k = kind_of(rel)
        out[k] += size
        if k == "other":
            other_names.add(rel.parts[0] + ("/" if len(rel.parts) > 1 else ""))
    out["total"] = sum(out[k] for k in ("rebuildable", "audio", "text", "other"))
    # What trim() would actually free: rebuildable copies whose FLAC is still there.
    out["trimmable"] = sum(f.stat().st_size for f in _trimmable(d))
    out["other_files"] = sorted(other_names)[:20]
    out["audio_removed"] = _marker(d)
    return out


def _marker(d: Path) -> dict | None:
    try:
        return json.loads((d / AUDIO_MARKER).read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- what each action does

def plan(d: Path, s: dict, busy: bool) -> dict:
    """What each of the three actions would do to this session, and whether it may.

    `s` is the session row web.py already builds (tracked, needs_review, …). Refusals are
    answers with a reason, never exceptions — the page shows the reason next to the option.
    """
    u = usage(d)
    why_busy = "this meeting is recording or being processed right now" if busy else ""
    warn_audio = []
    if s.get("needs_review"):
        n = s["needs_review"]
        warn_audio.append(f"{n} voice{'s' if n != 1 else ''} still unnamed — once the audio "
                          f"is gone you can't listen to them or teach mt who they are")
    kept = u["rebuildable"] - u["trimmable"]
    trim = {"ok": not busy and u["trimmable"] > 0, "bytes": u["trimmable"],
            "why": why_busy or ("" if u["trimmable"] else
                                "kept: no FLAC to rebuild them from" if kept else
                                "nothing to free")}
    remove = {"ok": not busy and u["audio"] > 0, "bytes": u["audio"] + u["rebuildable"],
              "why": why_busy or ("" if u["audio"] else "the audio is already gone"),
              "warn": warn_audio}
    why_del = why_busy
    if not why_del and s.get("tracked"):
        n = s["tracked"]
        why_del = (f"{n} action item{'s point' if n != 1 else ' points'} at this meeting — "
                   f"remove the audio instead to keep the transcript they cite")
    delete = {"ok": not why_del, "bytes": u["total"] - u["other"], "why": why_del,
              "kept": u["other_files"]}
    return {"usage": u, "trim": trim, "remove_audio": remove, "delete": delete}


# ---------------------------------------------------------------- doing it

def _trimmable(d: Path, force: bool = False) -> list[Path]:
    """work/*.wav that can go. A copy of the system track needs system.flac at the top of
    the folder to rebuild from, a mic copy needs mic.flac, anything else needs either —
    otherwise that WAV may be the last audio this meeting has, and it stays (the owner,
    Oct 27: "does the free space check to make sure that the FLAC files exist?").
    `force` skips the check: remove_audio, where the audio is leaving anyway."""
    work = d / "work"
    if not work.is_dir():
        return []
    flac = {t for t in AUDIO_STEMS if (d / f"{t}.flac").is_file()}
    out = []
    for f in work.glob("*.wav"):
        if f.is_symlink() or not f.is_file():
            continue
        track = next((t for t in AUDIO_STEMS if f.name.startswith(t)), None)
        if force or (track in flac if track else bool(flac)):
            out.append(f)
    return out


def trim(d: Path, force: bool = False) -> int:
    """Delete the work/*.wav that _trimmable() allows. Returns bytes freed. Nothing is lost
    — each one is rebuilt from its FLAC when a pass needs it (see the module doc)."""
    freed = 0
    for f in _trimmable(d, force):
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError:
            continue
    return freed


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    # os.replace is instant on one volume; shutil.move copies across volumes.
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _new_entry(kind: str, d: Path, title: str) -> tuple[Path, dict]:
    eid = f"{_now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    e = TRASH / eid
    (e / "files").mkdir(parents=True)
    return e, {"id": eid, "kind": kind, "session": d.name, "origin": str(d),
               "title": title or d.name, "at": _now().isoformat(timespec="seconds"),
               "files": [], "bytes": 0}


def _write_entry(e: Path, meta: dict) -> None:
    (e / ENTRY_FILE).write_text(json.dumps(meta, indent=2) + "\n")


def remove_audio(d: Path, title: str = "") -> dict:
    """Move system.* / mic.* to the trash and drop the rebuildable copies made from them.
    Leaves audio_removed.json so every page can say why there is nothing to play."""
    moving = [(rel, size) for rel, size in _files(d) if kind_of(rel) == "audio"]
    if not moving:
        return {"ok": False, "why": "the audio is already gone"}
    e, meta = _new_entry("audio", d, title)
    _write_entry(e, meta)                     # an interrupted move still leaves a record
    for rel, size in moving:
        _move(d / rel, e / "files" / rel)
        meta["files"].append(str(rel))
        meta["bytes"] += size
    _write_entry(e, meta)
    freed = trim(d, force=True)          # the FLACs just went to the trash; copies go too
    (d / AUDIO_MARKER).write_text(json.dumps({
        "removed_at": meta["at"], "files": meta["files"], "bytes": meta["bytes"],
        "trash_entry": meta["id"]}, indent=2) + "\n")
    return {"ok": True, "entry": meta["id"], "trashed": meta["bytes"], "freed": freed}


def delete_session(d: Path, title: str = "") -> dict:
    """Move everything mt wrote to the trash. Files mt did not write stay where they are;
    the folder itself goes only once nothing is left in it."""
    moving, kept = [], set()
    for rel, size in _files(d):
        if kind_of(rel) == "other":
            kept.add(rel.parts[0])
        else:
            moving.append((rel, size))
    if not moving:
        return {"ok": False, "why": "nothing of mt's left in this folder"}
    # transcript.json last: it is what makes the folder a session, so an interrupted
    # delete leaves something the list still shows rather than a half-gone ghost.
    moving.sort(key=lambda x: str(x[0]) == "transcript.json")
    e, meta = _new_entry("meeting", d, title)
    _write_entry(e, meta)
    for rel, size in moving:
        _move(d / rel, e / "files" / rel)
        meta["files"].append(str(rel))
        meta["bytes"] += size
    meta["kept"] = sorted(kept)
    _write_entry(e, meta)
    _prune_empty(d)
    return {"ok": True, "entry": meta["id"], "trashed": meta["bytes"],
            "kept": meta["kept"]}


def _prune_empty(d: Path) -> None:
    """Remove empty directories under d, then d itself if it ended up empty."""
    for root, dirs, files in os.walk(d, topdown=False):
        for x in dirs:
            try:
                (Path(root) / x).rmdir()
            except OSError:
                pass
    try:
        d.rmdir()
    except OSError:
        pass


# ---------------------------------------------------------------- the trash

def _entry_dir(eid: str) -> Path | None:
    if not ENTRY_ID.match(eid or ""):
        return None
    e = TRASH / eid
    return e if (e / ENTRY_FILE).is_file() else None


def _read_entry(e: Path) -> dict | None:
    try:
        return json.loads((e / ENTRY_FILE).read_text())
    except (OSError, ValueError):
        return None


def trash_list() -> list[dict]:
    out = []
    if not TRASH.is_dir():
        return out
    for e in sorted(TRASH.iterdir(), reverse=True):
        if not ENTRY_ID.match(e.name):
            continue
        meta = _read_entry(e)
        if meta:
            out.append({k: meta.get(k) for k in
                        ("id", "kind", "session", "title", "at", "bytes", "kept")})
    return out


def _safe_rel(r: str) -> Path | None:
    p = Path(r)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p


def restore(eid: str) -> dict:
    """Put a trash entry back where it came from. Refuses to overwrite anything."""
    e = _entry_dir(eid)
    if e is None:
        return {"ok": False, "why": "no such item in the trash"}
    meta = _read_entry(e) or {}
    d = Path(meta.get("origin") or "")
    if not d.is_absolute():
        return {"ok": False, "why": "the trash entry has no origin"}
    if meta.get("kind") == "meeting":
        if (d / "transcript.json").exists():
            return {"ok": False, "why": f"a meeting already exists at {d.name}"}
        d.mkdir(parents=True, exist_ok=True)
    elif not (d / "transcript.json").exists():
        return {"ok": False, "why": "the meeting this audio belongs to is gone — "
                                    "restore the meeting first"}
    clashes = []
    for r in meta.get("files", []):
        rel = _safe_rel(r)
        src = e / "files" / rel if rel else None
        if rel is None or not src.exists():
            continue
        if (d / rel).exists():
            clashes.append(r)
            continue
        _move(src, d / rel)
    if clashes:
        _write_entry(e, {**meta, "files": clashes})
        return {"ok": False, "why": "some files are back; these would have overwritten "
                                    "something and stayed in the trash: "
                                    + ", ".join(clashes[:5])}
    if meta.get("kind") == "audio":
        (d / AUDIO_MARKER).unlink(missing_ok=True)
    shutil.rmtree(e, ignore_errors=True)
    return {"ok": True, "session": meta.get("session"), "path": str(d)}


def empty(eid: str | None = None) -> dict:
    """Permanently delete one trash entry, or all of them."""
    if eid:
        e = _entry_dir(eid)
        if e is None:
            return {"ok": False, "why": "no such item in the trash"}
        targets = [e]
    else:
        targets = [x for x in (TRASH.iterdir() if TRASH.is_dir() else [])
                   if ENTRY_ID.match(x.name) and x.is_dir()]
    freed = 0
    for e in targets:
        freed += (_read_entry(e) or {}).get("bytes") or 0
        shutil.rmtree(e, ignore_errors=True)
    return {"ok": True, "emptied": len(targets), "freed": freed}


def purge_old(days: float) -> int:
    """Empty trash entries older than `days`. Returns how many went."""
    if not TRASH.is_dir() or days is None or days < 0:
        return 0
    cutoff = _now() - dt.timedelta(days=days)
    n = 0
    for e in list(TRASH.iterdir()):
        if not ENTRY_ID.match(e.name):
            continue
        try:
            at = dt.datetime.fromisoformat((_read_entry(e) or {})["at"])
        except (KeyError, TypeError, ValueError):
            continue
        if at < cutoff:
            shutil.rmtree(e, ignore_errors=True)
            n += 1
    return n


# ---------------------------------------------------------------- suggestions

def looks_like_test(s: dict) -> str:
    """Why this session looks like a test recording, or "" if it does not. Never offers a
    meeting that action items point at."""
    if s.get("tracked") or s.get("busy"):
        return ""
    if TEST_WORD.search(s.get("title") or ""):
        return "\"test\" in the name"
    secs = s.get("duration_sec") or 0
    if secs and secs < TEST_MAX_SECONDS:
        return f"only {int(secs)} s long"
    if not s.get("lines"):
        return "nothing was transcribed"
    return ""


def old_audio(s: dict, keep_days: float, today: dt.date | None = None) -> bool:
    """Archived, written up, and older than keep_days — the audio has done its job."""
    if not s.get("archived") or s.get("busy") or not (s.get("bytes") or {}).get("audio"):
        return False
    if not (s.get("summarized") or s.get("handed_off")):
        return False
    try:
        day = dt.date.fromisoformat((s.get("day") or "")[:10])
    except ValueError:
        return False
    return ((today or dt.date.today()) - day).days >= keep_days


# ---------------------------------------------------------------- self-test

def _selftest() -> int:
    global TRASH
    tmp = Path(tempfile.mkdtemp(prefix="mt-storage-test-"))
    TRASH = tmp / "trash"
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ok  " if cond else "  FAIL ") + msg)
        ok = ok and bool(cond)

    def make(name):
        d = tmp / "ws" / name
        (d / "work").mkdir(parents=True)
        (d / "transcript.json").write_text('{"timeline":[]}')
        (d / "summary.md").write_text("# s")
        (d / "system.flac").write_bytes(b"a" * 1000)
        (d / "mic.flac").write_bytes(b"b" * 500)
        (d / "work" / "system.16k.wav").write_bytes(b"c" * 4000)
        (d / "work" / "system-spk0.npy").write_bytes(b"d" * 10)
        (d / "notes.md").write_text("mine")
        return d

    d = make("2026-01-01-a")
    u = usage(d)
    check(u["rebuildable"] == 4000 and u["audio"] == 1500, "usage splits kinds")
    check(u["other_files"] == ["notes.md"], "notes.md counted as other")
    check(plan(d, {"tracked": 2}, False)["delete"]["ok"] is False, "tracked refuses delete")
    check(plan(d, {}, True)["remove_audio"]["ok"] is False, "busy refuses remove")

    r = remove_audio(d, "A")
    check(r["ok"] and r["trashed"] == 1500 and r["freed"] == 4000, "remove_audio moves + trims")
    check(not (d / "system.flac").exists() and (d / AUDIO_MARKER).exists(), "marker written")
    check((d / "work" / "system-spk0.npy").exists(), "npy kept")
    check(remove_audio(d)["ok"] is False, "second remove refused")
    r2 = restore(r["entry"])
    check(r2["ok"] and (d / "system.flac").exists() and not (d / AUDIO_MARKER).exists(),
          "restore audio")
    check(trash_list() == [], "trash empty after restore")

    r = delete_session(d, "A")
    check(r["ok"] and r["kept"] == ["notes.md"], "delete keeps foreign file")
    check(d.exists() and not (d / "transcript.json").exists(), "folder stays for notes.md")
    check(restore(r["entry"])["ok"] and (d / "work" / "system-spk0.npy").exists(),
          "restore meeting")

    (d / "notes.md").unlink()
    r = delete_session(d)
    check(not d.exists(), "folder removed when nothing else is in it")
    check(len(trash_list()) == 1, "one entry in trash")
    check(_entry_dir("../../etc") is None and restore("nope")["ok"] is False,
          "entry ids validated")
    meta = _read_entry(TRASH / r["entry"])
    meta["at"] = (_now() - dt.timedelta(days=10)).isoformat()
    _write_entry(TRASH / r["entry"], meta)
    check(purge_old(7) == 1 and trash_list() == [], "purge after trash_days")

    d2 = make("2026-01-02-b")
    check(trim(d2) == 4000 and trim(d2) == 0, "trim frees work wavs once")
    r = delete_session(d2)
    check(empty()["freed"] == r["trashed"], "empty all")

    check(looks_like_test({"title": "Test meeting", "lines": 5}) != "", "test by name")
    check(looks_like_test({"title": "Weekly", "duration_sec": 40, "lines": 5}) != "",
          "test by length")
    check(looks_like_test({"title": "Test", "tracked": 1}) == "", "tracked never a test")
    check(old_audio({"archived": True, "summarized": True, "bytes": {"audio": 1},
                     "day": "2026-01-01"}, 60, dt.date(2026, 6, 1)), "old audio")
    check(not old_audio({"archived": True, "bytes": {"audio": 1}, "day": "2026-01-01"},
                        60, dt.date(2026, 6, 1)), "not written up → not offered")

    shutil.rmtree(tmp, ignore_errors=True)
    print("storage selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if sys.argv[1:] == ["--selftest"] else
             (print("usage: storage.py --selftest") or 2))

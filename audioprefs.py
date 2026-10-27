"""Remembered mic and speaker picks, per "station".

The user works from several desks (home office, the office, travelling, plus whatever gets
plugged in on the day). macOS re-picks the default input and output every time they dock
or undocks, and mic_priority can only ever hold one global order — so a pick made by hand
at one desk was lost by the next meeting.

A station is the set of EXTERNAL audio devices connected right now (USB, Bluetooth,
Thunderbolt/dock, HDMI…). Built-in, virtual (Teams/Zoom loopbacks), aggregate,
Continuity (iPhone mic) and AirPlay devices are left out: they are either always there or
come and go on their own, and would make the same desk look like a different one.

Lookup, for each kind ("mic" / "speaker") separately:
  1. the exact same set of devices, if a pick was saved there and that device is connected;
  2. otherwise the most similar saved station (Jaccard ≥ 0.5 on device names) whose
     pick is connected — so plugging in AirPods at home still finds the home desk;
  3. otherwise nothing, and the caller falls back to mic_priority / the system default.

Only an explicit pick is ever saved. Stdlib-only; the file is per-install state and is
never committed.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIO = HERE / "build" / "mtaudio"
PREFS = HERE / "audio_prefs.json"

# CoreAudio transport codes that never describe which desk this is.
_NOT_A_STATION = {"bltn", "virt", "grup", "airp"}
MIN_SIMILARITY = 0.5


def available() -> bool:
    return AUDIO.exists()


def devices() -> list[dict]:
    """Every audio device, one row per direction: uid, name, dir (in|out), default, transport."""
    if not AUDIO.exists():
        return []
    try:
        out = subprocess.run([str(AUDIO), "--list"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return []
    rows = []
    for ln in out.splitlines():
        p = ln.split("\t")
        if len(p) >= 3:
            rows.append({"uid": p[0], "name": p[1], "dir": p[2],
                         "default": len(p) > 3 and p[3] == "default",
                         "transport": p[4] if len(p) > 4 else ""})
    return rows


def speakers(devs: list[dict] | None = None) -> list[dict]:
    return [d for d in (devices() if devs is None else devs) if d["dir"] == "out"]


def station(devs: list[dict] | None = None) -> list[str]:
    """The sorted external device names that identify where the user is sitting."""
    devs = devices() if devs is None else devs
    return sorted({d["name"] for d in devs
                   if d["transport"] not in _NOT_A_STATION and not d["transport"].startswith("cc")})


def describe(st: list[str]) -> str:
    return ", ".join(st) if st else "laptop only"


def _load() -> dict:
    try:
        return json.loads(PREFS.read_text())
    except Exception:
        return {}


def _key(st: list[str]) -> str:
    return "|".join(st)


def _similar(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def recall(kind: str, present: list[dict], devs: list[dict] | None = None) -> tuple[dict | None, str]:
    """The remembered pick of `kind` that is connected now, and why it was chosen.

    `present` is the list of candidate devices (uid, name) the pick must be among.
    """
    st = station(devs)
    saved = _load().get("stations", {})

    def connected(pick):
        if not pick:
            return None
        return (next((d for d in present if d["uid"] == pick.get("uid")), None)
                or next((d for d in present if d["name"] == pick.get("name")), None))

    exact = saved.get(_key(st))
    if exact and (hit := connected(exact.get(kind))):
        return hit, "your pick last time with this setup"
    best, best_score = None, 0.0
    for rec in saved.values():
        hit = connected(rec.get(kind))
        if not hit:
            continue
        score = _similar(st, rec.get("devices") or [])
        if score > best_score or (score == best_score and best and
                                  rec.get("updated", "") > best[1].get("updated", "")):
            best, best_score = (hit, rec), score
    if best and best_score >= MIN_SIMILARITY:
        return best[0], f"your pick at a similar setup ({describe(best[1].get('devices') or [])})"
    return None, ""


def remember(kind: str, uid: str, name: str, devs: list[dict] | None = None) -> None:
    """Save an explicit pick for the station connected right now."""
    st = station(devs)
    data = _load()
    rec = data.setdefault("stations", {}).setdefault(_key(st), {})
    rec["devices"] = st
    rec[kind] = {"uid": uid, "name": name}
    rec["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = PREFS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(PREFS)


def forget() -> int:
    """Drop every remembered pick. Returns how many stations were cleared."""
    n = len(_load().get("stations", {}))
    PREFS.unlink(missing_ok=True)
    return n


def saved_stations() -> list[dict]:
    return sorted(_load().get("stations", {}).values(),
                  key=lambda r: r.get("updated", ""), reverse=True)


def set_output(uid: str) -> tuple[bool, str]:
    """Make `uid` the macOS default output. Never raises: a speaker that could not be set
    must not stop a recording."""
    if not AUDIO.exists():
        return False, "mtaudio is not built — run ./make.sh"
    try:
        r = subprocess.run([str(AUDIO), "--set-output", uid], capture_output=True,
                           text=True, timeout=10)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or "").strip() or "mtaudio failed"
    return True, r.stdout.strip()

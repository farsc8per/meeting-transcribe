#!/usr/bin/env python3
"""web.py — the local app behind `mt web`.

Runs as the user on their own machine, which is the whole point: it can start a
recording, read their calendar, and write to the workspace — none of which an artifact
can do.

Binds 127.0.0.1 only. It can start recordings, so it never leaves localhost and has no
auth beyond that. App._guard() also refuses requests from other web pages (bad Host or
Origin, cross-site fetches), since any page in the browser can reach localhost.

Deliberately stdlib-only, and index.html is re-read from disk on every request so the UI
can be iterated on without restarting anything.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pathlib
from pathlib import Path

import feedback as FB
import mt as MT
import passqueue as PQ
import paths as P

HERE = Path(__file__).resolve().parent
WEBAPP = HERE / "webapp"
RUN = HERE / "run"
RUN.mkdir(exist_ok=True)
# LEGACY, read-only. Until Sep 15 2026 every pass shared this one pair, so starting a
# second recording while the first was still transcribing truncated the log and
# overwrote the json — the working pass went invisible and the new one took its card.
# Still READ so a pass in flight across the upgrade is not lost; never written again.
# Not deleted either: deletions under cc propagate to Google Drive.
CURRENT = RUN / "current.json"
LIVELOG = RUN / "current.log"
MTCAL = HERE / "build" / "mtcal"   # EventKit reader; no password, no OAuth

_lock = threading.Lock()
_child: subprocess.Popen | None = None

# The gap between "Start" and mtcapture existing. `mt live` takes ~10s to resolve the
# mic and launch the recorder, and active_session() is blind until then — so on Sep 28
# a second click 4s after the first started a second recorder into the SAME folder, and
# the two tore each other down 85s into a customer call. While a start is pending, no
# other start is accepted; if capture has not appeared by STARTUP_TIMEOUT the start is
# declared failed, the stalled process is killed, and the page says so.
STARTUP_TIMEOUT = 45
START_ERROR_TTL = 15 * 60
_starting: dict | None = None      # {"pid", "t", "log"} while a start is pending
_start_error: dict | None = None   # {"why", "t", "log"} from the last failed start

# Every process we spawn, held so poll() can reap it. An unreaped child lingers in the
# process table as a ZOMBIE, and os.kill(pid, 0) SUCCEEDS on a zombie — which is how a
# dead 20-second session reported itself as "processing" indefinitely.
_kids: dict[int, subprocess.Popen] = {}
_kids_lock = threading.Lock()


def _track(p: subprocess.Popen) -> None:
    _reap()
    with _kids_lock:
        _kids[p.pid] = p


def _reap() -> None:
    """Collect any child that has exited, so it never becomes a zombie in the first place.

    Only covers children of THIS server process. The Z check in _pid_alive() is the
    other half: it catches zombies that predate a restart, when there is no handle left.
    """
    with _kids_lock:
        for pid, p in list(_kids.items()):
            try:
                if p.poll() is not None:
                    _kids.pop(pid, None)
            except Exception:
                _kids.pop(pid, None)


# One state-file pair per pass, keyed by pid. pid is the only handle web.py has at spawn
# time — it does not know the session directory, mt.py derives it. The session path is
# recovered afterwards from the log's own "==> recording to <path>" line.
def _pass_json(pid) -> Path:
    return RUN / f"pass-{int(pid)}.json"


def _pass_logfile(pid) -> Path:
    return RUN / f"pass-{int(pid)}.log"


# ---------------------------------------------------------------- helpers

def account() -> str:
    """The work Google account, or "@<company domain>" when none is configured.

    Never empty: callers test `acct in name`, and "" is in every string, which would make
    any calendar at all look like the work one.
    """
    if P.GOOGLE_ACCOUNT:
        return P.GOOGLE_ACCOUNT
    return "@" + P.INTERNAL_DOMAINS[0] if P.INTERNAL_DOMAINS else "@"


def slugify(s: str) -> str:
    return MT.slugify(s or "meeting")


def customers() -> list[str]:
    d = P.CUSTOMERS
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []


CACHE = RUN / "today.json"

# Calendar has two providers, tried in that order.
#
# 1. mtcal (EventKit) — reads macOS Calendar directly. No password, no OAuth client, no
#    token to refresh. This is the supported path.
# 2. gog — kept only as a fallback. It needs a local *file* keyring password that is not
#    recoverable: both the refresh token AND the OAuth client secret are encrypted inside
#    it, and credential handling here is blocked outright by the permission classifier.
#    Do not build anything new on this path.
CALENDAR_HINT = (
    "Calendar not connected.\n"
    "mt now reads macOS Calendar directly — no password, nothing to unlock — but your\n"
    "work Google calendar is not attached to macOS yet. One-time fix, about a minute:\n"
    "  System Settings → Internet Accounts → Add Account → Google\n"
    f"  sign in as {P.GOOGLE_ACCOUNT or 'your work Google account'}, then tick Calendars\n"
    "Reload this page afterwards. It keeps working from then on.")

GOG_HINT = ("gog's local keyring is locked and its password is not recoverable here.\n"
            "Do not chase this path — use macOS Calendar instead:\n"
            "  System Settings → Internet Accounts → Add Account → Google\n"
            "Do not switch the keyring backend — that orphans the token.")


_keyring_pw: str | None = None       # held in memory only, never written to disk


def have_keyring_password() -> bool:
    return bool(_keyring_pw or os.environ.get("GOG_KEYRING_PASSWORD"))


def ask_keyring_password() -> bool:
    """Prompt for gog's keyring password ourselves and pass it through the environment.

    Fighting gog's own TTY detection was a dead end: it decides it is non-interactive
    from stdout, and stdout has to be redirected to collect the JSON. Asking here works
    regardless, and GOG_KEYRING_PASSWORD is gog's own documented mechanism.
    """
    global _keyring_pw
    if have_keyring_password():
        return True
    import getpass
    try:
        if not sys.stdin.isatty():
            return False
        pw = getpass.getpass("gog keyring password (not stored, this session only): ")
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    pw = pw.strip()
    if not pw:
        return False
    _keyring_pw = pw
    return True


def gog_env() -> dict:
    env = dict(os.environ)
    if _keyring_pw:
        env["GOG_KEYRING_PASSWORD"] = _keyring_pw
    return env


def fetch_gog(interactive: bool = False) -> tuple[bool, str, list]:
    """Fallback only: ask gog for today's events.

    `interactive` inherits the terminal so gog's keyring prompt can actually be answered.
    The non-interactive path used to pass --no-input AND close stdin, which meant it could
    never succeed no matter where it was started from — that was the real reason Today
    stayed broken.
    """
    if not P.GOOGLE_ACCOUNT:
        return False, 'gog fallback needs "google_account" in config.json', []
    cmd = ["gog", "calendar", "events", "list", "--today", "--json", "--no-input",
           "--account", account(), "--max", "25"]
    if interactive:
        ask_keyring_password()
    # INTERACTIVE PATH: stdout goes to a real file, but stderr and stdin stay attached to
    # the terminal so gog's keyring password prompt is actually VISIBLE and answerable.
    # Capturing both (the previous version) sent the prompt into a pipe nobody could see,
    # which is why "start it from a terminal" never worked.
    out_text, err_text = "", ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, env=gog_env())
        out_text, err_text = r.stdout, r.stderr
    except FileNotFoundError:
        return False, "gog is not installed", []
    except subprocess.TimeoutExpired:
        return False, "gog timed out", []
    if r.returncode != 0:
        tail = (err_text or out_text or "").strip().splitlines()[-1:]
        why = tail[0] if tail else ""
        if "KeyUnwrap" in why or "integrity check" in why:
            # Distinguish a WRONG password from a MISSING one — otherwise you cannot tell
            # whether to retype it or go looking for plumbing problems.
            global _keyring_pw
            _keyring_pw = None
            why = ("that keyring password was wrong (gog: integrity check failed).\n"
                   "Run `mt calendar` again and retype it.")
        elif (not why) or "TTY" in why or "keyring" in why or "GOG_KEYRING_PASSWORD" in why:
            why = GOG_HINT
        return False, why, []
    try:
        doc = json.loads(out_text)
    except Exception:
        return False, "could not parse gog output", []
    raw = doc.get("events") or doc.get("items") or (doc if isinstance(doc, list) else [])
    return True, "", raw


CAL_STORE = (pathlib.Path.home() / "Library" / "Group Containers"
             / "group.com.apple.calendar" / "Calendar.sqlitedb")
COREDATA_EPOCH = 978307200          # Core Data counts seconds from 2001-01-01 UTC
STALE_AFTER_MIN = 180


def calendar_sync() -> dict:
    """When macOS last actually synced the work calendar, and whether it errored.

    This exists because EventKit reads a purely LOCAL store, so a successful read says
    nothing about freshness. Measured Sep 9 2026: on the corporate network Google CalDAV
    sync is blocked, so events simply stop updating while every read keeps succeeding —
    "calendar cached 0 min ago" would be a lie. `Store.last_sync_end` is the only honest
    signal, and it is what goes stale at the office.

    Best effort by design: any failure here returns {} and the caller carries on.
    """
    if not CAL_STORE.exists():
        return {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{CAL_STORE}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute(
                "select name, last_sync_end, error_id from Store "
                "where last_sync_end is not null").fetchall()
        finally:
            con.close()
    except Exception:
        return {}

    acct = account().lower()
    newest, err = None, None
    for name, end, error_id in rows:
        low = (name or "").lower()
        if "google" not in low and acct not in low:
            continue
        if not end:
            continue
        when = dt.datetime.fromtimestamp(float(end) + COREDATA_EPOCH, dt.timezone.utc)
        if newest is None or when > newest:
            newest, err = when, error_id
    if newest is None:
        return {}
    age = int((dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 60)
    out = {"synced_at": newest.astimezone().isoformat(timespec="seconds"),
           "sync_age_minutes": max(age, 0),
           "sync_error": bool(err),
           "stale": age > STALE_AFTER_MIN or bool(err)}
    if out["stale"]:
        out["stale_why"] = (
            f"macOS last synced this calendar {age // 60}h{age % 60:02d}m ago."
            " On the corporate network Google Calendar sync is blocked, so a meeting"
            " added since then will not appear here — type the name by hand when you"
            " record.")
    return out


def fetch_eventkit() -> tuple[bool, str, list, list]:
    """Read today from macOS Calendar. Returns (ok, why, events, sources).

    mtcal always answers in JSON, including its failures, so a permission problem arrives
    as a readable reason instead of a parse error.
    """
    if not MTCAL.exists():
        return False, "mtcal is not built (run ./make.sh)", [], []
    try:
        r = subprocess.run([str(MTCAL), "--days", "1"], capture_output=True, text=True,
                           timeout=150, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return False, "mtcal is missing", [], []
    except subprocess.TimeoutExpired:
        # Only happens on a first run where nobody answered the macOS permission dialog.
        return False, "timed out waiting for the macOS calendar permission dialog", [], []
    try:
        doc = json.loads(r.stdout)
    except Exception:
        tail = (r.stderr or "").strip().splitlines()[-1:]
        return False, tail[0] if tail else "mtcal produced no readable output", [], []
    if not doc.get("ok"):
        why = doc.get("why") or "calendar unavailable"
        if doc.get("hint"):
            why = f"{why}\n{doc['hint']}"
        return False, why, [], []
    return True, "", doc.get("events") or [], doc.get("sources") or []


def has_work_calendar(sources: list) -> bool:
    """Is the work Google account actually attached to macOS Calendar?

    Without this, an unlinked account and a genuinely clear day look identical — both are
    zero events — and the page would claim "nothing today" on a day full of meetings.
    """
    acct = account().lower()
    local = acct.split("@")[0]
    domain = acct.split("@")[-1]
    for src in sources:
        low = (src or "").lower()
        if not low:
            continue
        if "google" in low or "gmail" in low or acct in low:
            return True
        if domain and domain in low:
            return True
        if local and local in low:
            return True
    return False


def fetch_calendar(interactive: bool = False) -> tuple[bool, str, list]:
    """EventKit first, gog only as a fallback."""
    ok, why, raw, sources = fetch_eventkit()
    if ok and raw:
        return True, "", raw
    linked = ok and has_work_calendar(sources)

    gok, gwhy, graw = fetch_gog(interactive)
    if gok:
        return True, "", graw

    if ok:
        # EventKit answered cleanly. Either the day really is clear, or the account is
        # not linked yet — and those need opposite responses, so never conflate them.
        if linked:
            return True, "", []
        return False, CALENDAR_HINT, []
    return False, why or gwhy or CALENDAR_HINT, []


def refresh_calendar(interactive: bool = False) -> dict:
    ok, why, raw = fetch_calendar(interactive)
    if not ok:
        return {"ok": False, "why": why}
    CACHE.write_text(json.dumps({
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "events": raw}, indent=2))
    return {"ok": True, "count": len(raw)}


def todays_session_dirs() -> set[str]:
    """Every session folder started today — processed or not.

    `existing_session_dirs()` needs a transcript.json, which does not exist for the
    30+ minutes a pass takes. Without these, a meeting still transcribing would be
    offered for recording all over again.
    """
    today = dt.date.today().isoformat()
    out: set[str] = set()
    for root in (P.RECORDINGS, P.CUSTOMERS):
        if not root.exists():
            continue
        for p in root.glob(f"*/meetings/{today}*"):
            if p.is_dir():
                out.add(p.name)
        for p in root.glob(f"{today}*"):
            if p.is_dir():
                out.add(p.name)
    return out


def shape_events(raw: list) -> list:
    done = {p.name for p in existing_session_dirs()} | todays_session_dirs()
    out = []
    for e in raw:
        st = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date") or ""
        en = (e.get("end") or {}).get("dateTime") or ""
        att = e.get("attendees") or []
        title = e.get("summary") or "(no title)"
        slug = slugify(title)
        out.append({
            "id": e.get("id"), "title": title,
            "start": st[11:16] if "T" in st else "all day",
            "end": en[11:16] if "T" in en else "",
            "attendees": len(att),
            "invitees": [(a.get("displayName") or a.get("email") or "") for a in att],
            "conference": e.get("conferenceUrl") or "",
            "slug": slug,
            "guess_customer": guess_customer(title, att),
            # TODAY's recordings only. Matching the slug across all time permanently
            # suppressed the Record button on every recurring meeting: one CAB recording
            # from Sep 9 meant the daily CAB call could never be started from the page
            # again. Session names begin with the date, so scoping by prefix is enough.
            "recorded": [n for n in done if n.endswith("-" + slug)
                         and n.startswith(dt.date.today().isoformat())],
        })
    return out


def today_events() -> dict:
    """Serve today from cache, refreshing quietly when possible."""
    cached, age_min = None, None
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
            t0 = dt.datetime.fromisoformat(cached["fetched_at"])
            age_min = int((dt.datetime.now(dt.timezone.utc) - t0).total_seconds() / 60)
        except Exception:
            cached = None
    if cached is None or (age_min is not None and age_min > 240):
        r = refresh_calendar(interactive=False)
        if r.get("ok"):
            cached = json.loads(CACHE.read_text())
            age_min = 0
        elif cached is None:
            return {"ok": False, "why": r.get("why") or "calendar unavailable",
                    "sync": calendar_sync(), "events": []}
    return {"ok": True, "age_minutes": age_min, "sync": calendar_sync(),
            "events": shape_events(cached.get("events", []))}


# config.json "customer_hints": {"<customer folder>": ["title fragment", ...]}. The first
# fragment's first word is also matched against attendee email domains.
CUSTOMER_HINTS = P.CUSTOMER_HINTS


def guess_customer(title: str, attendees: list) -> str:
    low = (title or "").lower()
    for slug, hints in CUSTOMER_HINTS.items():
        if any(h in low for h in hints):
            return slug
    domains = {(a.get("email") or "").split("@")[-1].lower() for a in attendees}
    for slug, hints in CUSTOMER_HINTS.items():
        for d in domains:
            if d and d not in P.INTERNAL_DOMAINS and hints and hints[0].split()[0] in d:
                return slug
    return ""


def existing_session_dirs() -> list[Path]:
    out = []
    for root in (P.RECORDINGS, P.CUSTOMERS):
        if root.exists():
            out +=[p.parent for p in root.rglob("transcript.json")
                   if REPROCESS_BACKUPS not in p.parts]
    return sorted(set(out), key=lambda p: p.name, reverse=True)


# ── where a session is in its life ───────────────────────────────────────────
# recorded → reviewed (voices named, handoff.json) → summarised (summary.md, written by
# the assistant) → tracked (action items in the DB) → archived (archived.json, the user's
# call).
# Every one of these is a file or a row that already existed; nothing new is invented to
# hold state, so the tags cannot disagree with the thing they describe.
STEWART_DB = P.DB


def tracked_counts() -> dict[str, int]:
    """session id → number of action items raised from it.

    `action_items.source` already carries `meeting:<session-id>` by convention (sometimes
    with a trailing " | Monday:REQ-nnn"), so this reads the real tracker rather than a
    second copy of the truth. Read-only and best effort: any failure returns {} and the
    page simply shows no tracked counts.
    """
    out: dict[str, int] = {}
    if not STEWART_DB.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{STEWART_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = con.execute(
                "select source, count(*) from action_items "
                "where source like 'meeting:%' group by 1").fetchall()
        finally:
            con.close()
    except Exception:
        return out
    # SQLite LIKE is case-insensitive, so this also catches legacy free-text sources like
    # "Meeting: 2026-07-10 Pre-GOL Internal". Those are prose, not session ids — keep only
    # what actually looks like one (a date-prefixed slug) and drop the rest silently.
    for src, n in rows:
        m = re.match(r"\s*meeting:\s*(\d{4}-\d{2}-\d{2}[\w.-]*)", src or "", re.I)
        if m:
            sid = m.group(1)
            out[sid] = out.get(sid, 0) + n
    return out


def _strong(v: dict) -> bool:
    """cues.strong_match, imported lazily like the rest of the cues code."""
    import cues
    try:
        return cues.strong_match(v)
    except Exception:
        return False


def session_summary(d: Path, tracked: dict[str, int] | None = None) -> dict:
    try:
        doc = json.loads((d / "transcript.json").read_text())
    except Exception:
        return {}
    sp = {k: v for k, v in doc.get("speakers", {}).items() if k != "mic"}
    verified = sum(1 for v in sp.values() if v.get("confidence") == "verified")
    # A strong voiceprint match is not waiting on anyone: it counts for cues and awards
    # already, and Confirm on it is optional. Only the rest is "to review".
    matched = sum(1 for v in sp.values() if _strong(v))
    meta = doc.get("meta", {})
    try:
        rel = str(d.relative_to(MT.WORKSPACE))
    except ValueError:
        rel = str(d)
    # started_at is stored UTC ("…Z"). Slicing the string straight into the page showed
    # a 14:03 meeting as 19:03 — and anything after 7pm CT landed on TOMORROW'S date.
    # Convert once, here, so every caller gets wall-clock time as the user lived it.
    iso = meta.get("started_at") or ""
    day, clock, sort_key = "", "", d.name
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        day, clock, sort_key = t.strftime("%Y-%m-%d"), t.strftime("%H:%M"), t.isoformat()
    except ValueError:
        day = iso[:10]
    return {
        "id": d.name, "path": rel,
        "title": meta.get("title") or d.name.replace("-", " "),
        "recorded": (day + " " + clock).strip(),
        "day": day, "started": clock, "sort_key": sort_key,
        "duration": MT.hhmmss(meta.get("duration")),
        "customer": meta.get("customer") or "",
        "mic": meta.get("mic") or "",
        "voices": len(sp), "verified": verified, "matched": matched,
        "needs_review": len(sp) - verified - matched,
        "handed_off": (d / "handoff.json").exists(),
        "summarized": (d / "summary.md").exists(),
        # Absent action items is NOT a gap — plenty of meetings legitimately raise none.
        # Shown only as a positive count, never as a missing-something warning.
        "tracked": (tracked or {}).get(d.name, 0),
        "archived": (d / "archived.json").exists(),
        "duration_sec": meta.get("duration") or 0,
        "lines": len(doc.get("timeline") or []),
        "audio_removed": (d / "audio_removed.json").exists(),
    }


# ── live audio glance ────────────────────────────────────────────────────────
# mtcapture logs `mm:ss  system Ns (peak x) | mic Ns (peak x)` every 5s. On the Oct 5 CAB
# call that was 193 of 519 lines in the live box — it buried the transcript whenever the
# room went quiet. And `peak` is the loudest sample SINCE THE START (mtcapture only ever
# raises it), so it never drops when audio stops: it cannot answer "is audio arriving
# now". The heartbeat stays in the log; the box drops it, and the dots below answer the
# real question from the audio itself.
HEARTBEAT = re.compile(r"^\s*(?:recorder:\s*)?(?:\[mtcapture\]\s*)?\d+:\d{2}\s+system\s+"
                       r"\d+s\s+\(peak\s+[\d.]+\)\s+\|\s+mic\s")
LEVEL_WINDOW_SEC = 60       # how far back "last sound" looks
LEVEL_FRAME_SEC = 0.1
SOUND_RMS = 0.00316         # -50 dBFS, the same floor as the silencedetect check
QUIET_AFTER_SEC = 5         # no sound for this long → amber
STALLED_AFTER_SEC = 8       # file has not grown for this long → "no new audio"


def split_heartbeat(lines: list[str]) -> tuple[list[str], str]:
    """(lines without heartbeats, the newest heartbeat line or "")."""
    keep, last = [], ""
    for ln in lines:
        if HEARTBEAT.match(ln):
            last = ln.strip()
        else:
            keep.append(ln)
    return keep, last


def track_levels(live: Path) -> dict:
    """Per track, is sound arriving right now? Read-only: the tail of the wav being
    written, the same way the live loop reads it. Never touches the recorder.

    A glance, not an alarm. A muted far end and a dead tap both read as silence here;
    telling them apart is the health card's job, and it asks rather than assumes.
    """
    import numpy as np
    out = {}
    now = time.time()
    for kind in ("mic", "system"):
        f = live / f"{kind}.wav"
        r = {"state": "waiting"}
        try:
            st = f.stat()
            off = MT.wav_data_offset(f)
            total = max(0, (st.st_size - off) // 2)
            if total >= 16000 // 2:
                x = MT.read_tail(f, off, max(0, total - LEVEL_WINDOW_SEC * 16000))
                rms = MT.frame_rms(x, frame=LEVEL_FRAME_SEC)
                loud = np.flatnonzero(rms >= SOUND_RMS)
                if loud.size:
                    quiet_for = (len(rms) - 1 - int(loud[-1])) * LEVEL_FRAME_SEC
                else:
                    quiet_for = None                 # nothing in the whole window
                recent = rms[-int(2 / LEVEL_FRAME_SEC):]
                db = 20 * np.log10(max(float(recent.max()), 1e-9))
                stalled = now - st.st_mtime > STALLED_AFTER_SEC
                if stalled:
                    state = "stalled"
                elif quiet_for is not None and quiet_for < QUIET_AFTER_SEC:
                    state = "sound"
                else:
                    state = "quiet"
                r = {"state": state, "db": round(db),
                     "quiet_for": None if quiet_for is None else round(quiet_for),
                     "window": LEVEL_WINDOW_SEC}
        except (OSError, ValueError):
            pass
        out[kind] = r
    return out


def running() -> dict:
    """Is a recording live? Survives a server restart by re-reading run/pass-<pid>.json."""
    try:
        cur = MT.active_capture()
    except Exception:
        cur = None
    if cur is None:
        return {"recording": False}
    cap_pid, live = cur
    # An mtcapture in the process table is not the same thing as a recording. On Sep 17
    # one wedged in teardown and sat there for five minutes; this page kept counting up
    # and the start button stayed locked. capture_liveness() only ever says "not live"
    # on positive evidence that it has stopped — it defaults to live, because wrongly
    # unlocking the button during a real meeting would cost the meeting.
    try:
        verdict = MT.capture_liveness(cap_pid, live)
    except Exception:
        verdict = {"live": True}
    if not verdict.get("live"):
        return {"recording": False, "stuck_recorder":
                {"pid": cap_pid, "session": live.name, "why": verdict.get("why", "")}}
    entry = _live_entry(live)
    info = (entry or {}).get("info") or {}
    logf = (entry or {}).get("log")
    started = info.get("started_at")
    elapsed = None
    cj = live / "capture.json"
    if cj.exists():
        try:
            cap = json.loads(cj.read_text())
            started = started or cap.get("started_at")
            info.setdefault("mic", cap.get("mic_name"))
        except Exception:
            pass
    if started:
        try:
            t0 = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            elapsed = int((dt.datetime.now(dt.timezone.utc) - t0).total_seconds())
        except Exception:
            pass
    tail, heartbeat = "", ""
    if logf is not None and logf.exists():
        try:
            lines, heartbeat = split_heartbeat(logf.read_text(errors="replace").splitlines())
            tail = "\n".join(lines[-40:])
        except Exception:
            pass
    try:
        levels = track_levels(live)
    except Exception:
        levels = {}
    # NOT "the events file exists" — it existed on one long customer call and said nothing
    # anybody read. This is the recorder's own log folded into a state, plus whatever
    # the user has already answered, rebuilt from disk on every single request so a reload
    # or a server restart cannot lose an alert.
    try:
        health = MT.capture_health(live)
    except Exception:
        health = {"state": "ok", "alarm": False, "ask": False}
    return {"recording": True, "session": live.name, "path": str(live),
            "capture_pid": cap_pid,
            "health": health,
            "system_dead": bool(health.get("alarm")),
            "elapsed": elapsed, "elapsedText": MT.hhmmss(elapsed or 0),
            "mic": info.get("mic") or "", "params": info.get("params") or {},
            "live": tail, "heartbeat": heartbeat, "levels": levels}


# ── the processing pass ──────────────────────────────────────────────────────
# Between "stop" and a finished transcript the session is INVISIBLE: mtcapture has
# already exited so running() reports nothing, and transcript.json does not exist yet
# so it is absent from the session list too. On a long call that blind spot lasts the
# best part of an hour — precisely when the user is in the next meeting wondering whether
# reloading the page is worth it. Phases are read back out of the recorder's own stdout
# (the same ==> lines it prints to the terminal), so the pipeline reports nothing new
# and this cannot drift out of sync with it.
PASS_MARK = "running the full pass"


def _phase_plan(mic_used: bool, cues: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """Every ==> line the full pass prints, in order, as (label, matching fragments).

    Transcribing has two spellings — a fresh run and "reusing existing transcription"
    on a re-run — and both mean the same step, so they share one entry. The steps after
    the transcript are listed too: on Oct 21 the laughter scan ran 20 minutes after the
    transcript was written, and this page showed nothing running at all.
    """
    plan: list[tuple[str, tuple[str, ...]]] = [("reading the screen", ("screen:",))]
    tracks = [("system", "the room")] + ([("mic", "your mic")] if mic_used else [])
    for kind, who in tracks:
        plan += [(f"converting {who}",         (f"{kind}: converting",)),
                 (f"finding speech in {who}",  (f"{kind}: finding speech",)),
                 (f"transcribing {who}",       (f"{kind}: transcribing", f"{kind}: reusing")),
                 (f"matching voices in {who}", (f"{kind}: consolidating",))]
    plan.append(("writing the transcript", ("done",)))
    plan.append(("applying the names you gave live", ("applying the names",)))
    if cues:
        plan += [("waiting for a quiet machine", ("waiting:",)),
                 ("measuring vocal cues", ("measuring vocal cues",)),
                 ("listening for laughter", ("listening for laughter",))]
    plan.append(("finishing up", ("finished",)))
    return plan


# Relative cost of each step, so the bar tracks time rather than step count. Timed on a
# 2h47m customer call: prep ran ~8 min and transcription alone ran past 20. An evenly-spaced
# bar sat at 30% for the whole transcribe step and then jumped — worse than no bar.
_WEIGHT = {"reading": 1, "converting": 2, "finding": 6, "transcribing": 15,
           "matching": 1, "writing": 1, "applying": 0, "waiting": 0, "measuring": 1,
           "listening": 2, "finishing": 0}


def _weight(label: str) -> int:
    return next((w for k, w in _WEIGHT.items() if label.startswith(k)), 1)


def _percent(plan, idx: int) -> int:
    """Fraction of the pass's estimated WORK completed before the current step."""
    total = sum(_weight(lbl) for lbl, _ in plan) or 1
    done = sum(_weight(lbl) for lbl, _ in plan[:max(0, idx)])
    return max(2, min(99, round(done * 100 / total)))


def _not_ours(pid) -> bool:
    """The pid exists but is not a running mt process: a zombie, or a recycled pid.

    A zombie has exited but not been waited for, and os.kill(pid, 0) succeeds on one.
    A recycled pid is the same trap at a longer range: pass-*.json files are never
    deleted, so once macOS hands an old pass's pid to something else, that pass comes
    back to life. On Oct 12 a Sep 17 pass (pid 74722, dead within a
    minute) reappeared as "stopping capture…" because installd had been given 74722.
    Every pid this file asks about is `mt live` — spawned as ./mt, which execs mt.py
    in the same pid — so anything whose command line names neither is not ours.

    ps rather than a Popen handle, because after a server restart there is no handle:
    the pass keeps running, we adopt it from disk, and it is not our child any more.
    """
    try:
        out = subprocess.run(["ps", "-ww", "-p", str(int(pid)), "-o", "stat=", "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return False           # cannot tell; fall back to what kill() said
    if not out:
        return False
    stat, _, cmd = out.partition(" ")
    if stat.startswith("Z"):
        return True
    return "mt.py" not in cmd and str(HERE / "mt") not in cmd


def _pid_alive(pid) -> bool:
    _reap()                    # our own exited children stop existing here, not later
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                   # exists, just not ours to signal — still check what it is
    except OSError:
        return False
    return not _not_ours(pid)


def _session_of(logf: Path | None) -> Path | None:
    """Where the recorder said it was writing, from its own stdout.

    Read back rather than rebuilt from the slug, because the slug rules live in mt.py
    and would drift. It is also the ONLY way to attach a session to a pass: web.py never
    learns the directory at spawn time.
    """
    if logf is None or not logf.exists():
        return None
    try:
        lines = logf.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        # A queued job names its session the same way the recorder does.
        for mark in ("recording to ", MT.PASS_MARK_JOB + " "):
            if mark in ln:
                cand = pathlib.Path(ln.split(mark, 1)[1].strip())
                return cand if cand.exists() else None
    return None


def _same_path(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _pass_entries() -> list[dict]:
    """Every pass state file on disk, newest first. The whole of the server's knowledge.

    Nothing here comes from memory: restart `mt web` mid-pass and this rebuilds
    identically, which is the point of keying the files by pid.
    """
    out, seen = [], set()
    for p in RUN.glob("pass-*.json"):
        m = re.fullmatch(r"pass-(\d+)\.json", p.name)
        if not m:
            continue
        pid = int(m.group(1))
        try:
            info = json.loads(p.read_text())
        except Exception:
            continue
        out.append({"pid": pid, "info": info, "log": _pass_logfile(pid), "json": p})
        seen.add(pid)
    # Upgrade safety: a pass that was in flight when this change landed still lives in
    # the old shared pair. Include it while its pid is alive so it does not vanish.
    if CURRENT.exists():
        try:
            info = json.loads(CURRENT.read_text())
        except Exception:
            info = None
        if info:
            try:
                pid = int(info.get("pid"))
            except (TypeError, ValueError):
                pid = None
            if pid is not None and pid not in seen and _pid_alive(pid):
                out.append({"pid": pid, "info": info, "log": LIVELOG, "json": CURRENT})
    out.sort(key=lambda e: ((e["info"].get("started_at") or ""), e["pid"]), reverse=True)
    return out


def _live_entry(live: Path) -> dict | None:
    """The entry for the recorder that is capturing right now."""
    entries = _pass_entries()
    for e in entries:
        if _same_path(_session_of(e["log"]), live):
            return e
    # The first second of a recording, before "recording to" has been flushed. Newest
    # first, so this is the one just started rather than anything still transcribing.
    return next((e for e in entries
                 if not (e["info"] or {}).get("job") and _pid_alive(e["pid"])), None)


def _pass_log(logf: Path | None) -> list[str]:
    """The ==> lines of the CURRENT full pass in this log only.

    The log is appended to across a session, and a live rolling pass writes the same
    step names while the meeting is still going. Everything before the last
    "running the full pass" marker belongs to an earlier phase and would otherwise
    make the pass look further along than it is.
    """
    if logf is None or not logf.exists():
        return []
    try:
        lines = logf.read_text(errors="replace").splitlines()
    except OSError:
        return []
    # `mt live` prints the marker when the call ends; `mt record` drops straight into the
    # pass without one. So fall back to the last "recording to" line, which both emit.
    cut = None
    for i in range(len(lines) - 1, -1, -1):
        if PASS_MARK in lines[i]:
            cut = i
            break
    if cut is None:
        for i in range(len(lines) - 1, -1, -1):
            if "recording to " in lines[i]:
                cut = i + 1
                break
    if cut is None:
        return []
    return [ln for ln in lines[cut:] if ln.lstrip().startswith("==>")]


def _spoken_seconds(line: str) -> int | None:
    """The "transcribing 22:18 with …" duration, in seconds."""
    m = re.search(r"transcribing\s+(?:(\d+):)?(\d{1,2}):(\d{2})", line)
    if not m:
        return None
    h, mm, ss = m.group(1) or 0, m.group(2), m.group(3)
    return int(h) * 3600 + int(mm) * 60 + int(ss)


# Speech-seconds transcribed per wall-second. Timed end to end on a 2h47m customer call
# (Sep 14 2026): 1:36:04 of speech took 14m06s = 6.8x.
#
# NOT the 12.3x in config.json, and the difference is not machine load. That figure is
# measured against the WHOLE TRACK; whisper only ever sees the speech blocks the VAD
# kept, which was 58% of this track — 2:47:03 / 14m06s = 11.8x, which agrees with it.
# The log line this divides states SPEECH duration, so 12.3x would under-estimate the
# wait by roughly half. Do not "correct" it back.
#
# Only ever rendered as "about N minutes", and only for transcription, which is the one
# step whose size the log states. Nothing else in the pass is estimated.
TURBO_REALTIME = 6.8


def _mins(secs) -> str:
    """Estimates get minute granularity. "7:48" claims a precision we do not have."""
    if not secs:
        return ""
    m = max(1, round(secs / 60))
    return f"{m} min" if m < 60 else f"{m // 60}h{m % 60:02d}m"


# How long a pass that died partway keeps its "stopped during: …" card. The failure is
# worth showing — a bar that never moves tells the user nothing — but a pass that broke
# yesterday must not still be on the page today, and its files stay on disk forever.
# Measured from the log's last write, so it survives a server restart like everything else.
FAILED_CARD_TTL = 2 * 3600


def _pass_state(entry: dict, live: Path | None) -> dict | None:
    """One in-flight pass, or None when there is nothing to report for it.

    Same logic as the old single-pass reader, scoped to this pass's own files.
    """
    info = entry.get("info") or {}
    logf = entry.get("log")
    session = _session_of(logf)

    # While mtcapture is writing to THIS session the meeting is still going and running()
    # owns the display. The live flow writes real pipeline lines mid-call (a rolling
    # transcript), so without this guard a call in progress reads as a pass underway.
    # Scoped to the one session now: another pass transcribing alongside it is real.
    if live is not None and _same_path(session, live):
        return None

    log = _pass_log(logf)
    if not log:
        return None                    # nothing has started yet
    if MT.QUEUED_MARK in log[-1]:
        return None                    # the recorder handed its pass to the queue

    alive = _pid_alive(entry.get("pid"))
    if session is not None and (session / "transcript.json").exists() and not alive:
        return None                    # finished — it belongs in the session list now
    job = info.get("job")
    if job and not alive:
        return None                    # a queued job that ended: the queue card says how

    params = info.get("params") or {}
    mic_used = not params.get("no_mic")
    plan = _phase_plan(mic_used, bool(params.get("cues")))

    last = log[-1]
    idx, label = None, None
    for i, (lbl, frags) in enumerate(plan):
        if any(f in last for f in frags):
            idx, label = i, lbl
    if label is None:                  # a line we do not model; stay honest about it
        label = last.split("==>", 1)[-1].strip() or "working"
        idx = 0

    done = "done" in last

    # Pass start = when the meeting ended. started_at + the recorded length, both of
    # which we already have, is exact; the log carries no timestamps of its own.
    elapsed = None
    started = info.get("started_at")
    rec_secs = None
    for ln in log:
        m = re.search(r"ended after\s+(?:(\d+):)?(\d{1,2}):(\d{2})", ln)
        if m:
            h, mm, ss = m.group(1) or 0, m.group(2), m.group(3)
            rec_secs = int(h) * 3600 + int(mm) * 60 + int(ss)
            break
    if rec_secs is None and info.get("recorded_secs"):
        rec_secs = int(info["recorded_secs"])
    if info.get("pass_started_at"):    # a queued job: it began when the worker started it
        started, began_at = info["pass_started_at"], 0
    else:
        began_at = rec_secs
    if started and began_at is not None:
        try:
            t0 = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            began = t0 + dt.timedelta(seconds=began_at)
            elapsed = max(0, int((dt.datetime.now(dt.timezone.utc) - began).total_seconds()))
        except Exception:
            pass

    # How long THIS step has been running, which is a fact rather than an estimate: the
    # log's mtime is the moment its last line was written, and whisper prints nothing at
    # all while it works, so the file simply stops changing for the whole step.
    step_elapsed = None
    try:
        step_elapsed = max(0, int(dt.datetime.now().timestamp() - logf.stat().st_mtime))
    except (OSError, AttributeError):
        pass

    # Only ever an estimate, and only for the one step whose size the log states.
    step_eta = None
    if "transcribing" in (label or ""):
        sp = _spoken_seconds(last)
        if sp:
            step_eta = int(sp / TURBO_REALTIME)

    out = {
        "processing": True,
        "pid": entry.get("pid"),
        "alive": alive,
        "session": session.name if session else (params.get("name") or ""),
        "path": str(session) if session else "",
        "title": params.get("title") or "",
        "customer": params.get("customer") or "",
        "phase": label,
        "step": (idx or 0) + 1,
        "steps": len(plan),
        "percent": _percent(plan, idx or 0),
        "elapsed": elapsed,
        "elapsedText": MT.hhmmss(elapsed) if elapsed is not None else "",
        "step_eta": step_eta,
        "step_etaText": _mins(step_eta),
        "step_elapsed": step_elapsed,
        "step_elapsedText": MT.hhmmss(step_elapsed) if step_elapsed is not None else "",
        "recordedText": MT.hhmmss(rec_secs) if rec_secs else "",
        "tail": "\n".join(log[-6:]),
    }
    if job:
        out["job"] = job
        out["paused"] = bool(PQ.run_state(job).get("paused"))
        out["now"] = PQ.now_file(job).exists()
    if done and not alive:
        return None                    # finished and cleaned up between our two reads
    if not alive:
        # The pass died partway. Saying so beats a progress bar that never moves — but
        # only while it is still news: pass-*.json/.log are never deleted, so without the
        # cut-off every pass that ever broke would be on the page forever.
        if step_elapsed is not None and step_elapsed > FAILED_CARD_TTL:
            return None
        out["failed"] = True
        out["phase"] = f"stopped during: {label}"
    return out


def processing() -> list[dict]:
    """Every pass in flight, newest first. [] when there is nothing to report.

    A LIST because back-to-back meetings are normal: the previous call can still be
    transcribing while the next one records, and both have to be visible at once.
    """
    live = MT.active_session()
    out = []
    for e in _pass_entries():
        try:
            st = _pass_state(e, live)
        except Exception:
            st = None
        if st:
            out.append(st)
    return out


def queue_state() -> dict:
    """The after-call queue (passqueue.py) for the Today view. Also the worker's safety
    net: polled every 2 s, so work left over from a crash or a reboot gets picked up."""
    try:
        PQ.ensure_worker(track=_track)
    except Exception:
        pass
    try:
        return PQ.status()
    except Exception:
        return {"jobs": [], "worker": False}


def queue_action(act: str, jid) -> dict:
    fn = {"now": PQ.process_now, "retry": PQ.retry, "dismiss": PQ.dismiss}[act]
    r = fn(jid if isinstance(jid, str) else "")
    if r.get("ok"):
        try:
            PQ.ensure_worker(track=_track)
        except Exception:
            pass
    return r


def meeting_now() -> dict | None:
    """The calendar event happening right now, if any."""
    t = today_events()
    if not t.get("ok"):
        return None
    now = dt.datetime.now().strftime("%H:%M")
    for e in t["events"]:
        st, en = e.get("start") or "", e.get("end") or ""
        if len(st) == 5 and len(en) == 5 and st <= now <= en:
            return e
    return None


def _log_tail(logf, n: int = 3) -> str:
    try:
        lines = [x for x in Path(logf).read_text(errors="replace").splitlines() if x.strip()]
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def _startup_status() -> dict | None:
    """Resolve a pending start. Call with _lock held. Returns the pending start or None.

    Settles it one of three ways: capture appeared (success, forget it), the process
    died first, or STARTUP_TIMEOUT passed with no capture — then the whole process group
    is killed, so a recorder that finally wakes up late cannot become a second recorder
    alongside the retry.
    """
    global _starting, _start_error
    s = _starting
    if s is None:
        return None
    if MT.active_session() is not None:
        _starting = None               # recording is live — the normal ending
        return None
    age = time.time() - s["t"]
    if not _pid_alive(s["pid"]):
        _starting = None
        _start_error = {"why": "the recorder exited before capture began",
                        "t": time.time(), "log": str(s["log"]),
                        "tail": _log_tail(s["log"])}
        return None
    if age > STARTUP_TIMEOUT:
        try:
            os.killpg(s["pid"], signal.SIGTERM)   # start_new_session=True: pgid == pid
        except Exception:
            pass
        _starting = None
        _start_error = {"why": f"recording did not start within {STARTUP_TIMEOUT}s — "
                               "stopped it so you can try again",
                        "t": time.time(), "log": str(s["log"]),
                        "tail": _log_tail(s["log"])}
        return None
    return {"pid": s["pid"], "elapsed": int(age), "timeout": STARTUP_TIMEOUT}


def startup_state() -> dict:
    """The `starting` / `start_error` fields for /api/state (polled every 2s, which is
    also what makes the timeout fire when nobody is clicking)."""
    global _start_error
    with _lock:
        st = _startup_status()
        err = _start_error
        if err and time.time() - err["t"] > START_ERROR_TTL:
            _start_error = err = None
    out = {}
    if st:
        out["starting"] = st
    if err:
        out["start_error"] = {k: err[k] for k in ("why", "log", "tail")}
    return out


def start_recording(p: dict) -> dict:
    global _child, _starting, _start_error
    with _lock:
        if _startup_status() is not None:
            return {"ok": False, "why": "a recording is already starting — give it a moment"}
        if MT.active_session() is not None:
            return {"ok": False, "why": "a recording is already running"}
        _start_error = None

        # An unnamed recording becomes "…-1600-meeting" and is then invisible when you go
        # looking for it — this happened with a real CAB call. Fill the blanks from the
        # calendar event in progress, then from the meeting window on screen.
        derived = None
        if not (p.get("name") or "").strip():
            ev = meeting_now()
            if ev:
                p["name"] = ev["slug"]
                p.setdefault("title", "") or p.update(title=ev["title"])
                if not p.get("customer"):
                    p["customer"] = ev.get("guess_customer") or ""
                derived = f"calendar: {ev['title']}"
            else:
                det = detect_meeting()
                t = (det.get("title") or "").split("|")[0].strip()
                if det.get("live") and t:
                    p["name"] = slugify(t)
                    p.setdefault("title", "") or p.update(title=t)
                    derived = f"meeting window: {t}"

        cmd = [str(HERE / "mt"), "live"]
        name = (p.get("name") or "").strip()
        if name:
            cmd += ["--name", slugify(name)]
        if p.get("customer"):
            cmd += ["--customer", p["customer"]]
        if p.get("title"):
            cmd += ["--title", p["title"]]
        if p.get("mic_only"):
            cmd += ["--no-system"]
        if p.get("no_mic"):
            cmd += ["--no-mic"]
        # An explicit pick from the web picker. Blank means "let mic_priority decide",
        # which is the same path the CLI takes with no --mic-id at all, so the default
        # behaviour is unchanged. Meaningless alongside --no-mic, so it is not sent
        # there — mt would ignore it, but a command line that contradicts itself is a
        # bad thing to find in a capture log six weeks later.
        if p.get("mic_id") and not p.get("no_mic"):
            cmd += ["--mic-id", p["mic_id"]]
        # Same for the speaker: blank means "remembered pick, else leave it alone".
        if p.get("speaker_id"):
            cmd += ["--speaker-id", p["speaker_id"]]
        if p.get("in_person"):
            cmd += ["--in-person"]
        if p.get("cues"):
            cmd += ["--cues"]       # vocal cues: opted in at record time
        inv = p.get("invitees")
        if not inv:
            ev = meeting_now()
            inv = (ev or {}).get("invitees") or []
        inv = [x for x in inv if x]
        if inv:
            cmd += ["--invitees", ",".join(inv[:80])]
        # The log has to exist before the pid does, so it is opened under a staging name
        # and renamed once Popen hands the pid back. Renaming does not disturb the child's
        # already-open descriptor — same inode, same directory, no window where output
        # could land in the wrong file.
        # Pause first, then capture: a previous meeting's queued pass is frozen before
        # the recorder process even exists (it marks itself a second or two later).
        try:
            PQ.hold_for_recording()
        except Exception:
            pass
        staging = RUN / "pass-starting.log"
        staging.write_text("")
        log = open(staging, "a", buffering=1)
        try:
            _child = subprocess.Popen(cmd, stdout=log, stderr=log,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            log.close()
            return {"ok": False, "why": str(e)}
        _track(_child)
        log.close()                   # the child holds its own copy
        logf = _pass_logfile(_child.pid)
        try:
            os.replace(staging, logf)   # only ever overwrites a recycled pid's stale log
        except OSError:
            logf = staging
        # Hold the machine awake for exactly as long as the recorder lives. -w means
        # caffeinate exits by itself, so there is no assertion to leak.
        try:
            _track(subprocess.Popen(["caffeinate", "-dimsu", "-w", str(_child.pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except Exception:
            pass
        _starting = {"pid": _child.pid, "t": time.time(), "log": logf}
        _pass_json(_child.pid).write_text(json.dumps({
            "pid": _child.pid, "params": p, "name_from": derived,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }, indent=2))
        return {"ok": True, "pid": _child.pid, "name": name or "meeting",
                "name_from": derived}


def stop_recording() -> dict:
    """SIGINT the recorder, exactly as Ctrl-C would, so it runs its full pass.

    `mt live` is ONE process: it records, and on SIGINT runs the pass in the same pid.
    So a transcribing pass answers to `pgrep -f "mt.py live"` exactly like a recorder
    does, and a stray SIGINT would abort somebody's transcription. Every path here is
    therefore anchored to the session mtcapture is actually writing to right now.
    """
    live = MT.active_session()
    if live is None:
        return {"ok": False, "why": "nothing is recording"}

    pid = None
    others = set()
    for e in _pass_entries():
        if _same_path(_session_of(e["log"]), live):
            if pid is None and _pid_alive(e["pid"]):
                pid = e["pid"]
        else:
            others.add(e["pid"])       # a pass, or an old recorder — never signal these
    if pid is None:
        # No state file for it: adopted from a previous server, or started from a
        # terminal. Take the pgrep hit only when it is unambiguous.
        try:
            hits = [int(x) for x in subprocess.run(
                ["pgrep", "-f", "mt.py live"], capture_output=True, text=True
            ).stdout.split()]
        except Exception:
            hits = []
        hits = [h for h in hits if h not in others and _pid_alive(h)]
        if len(hits) == 1:
            pid = hits[0]
        elif len(hits) > 1:
            return {"ok": False, "why": "several recorders are running and none of them "
                                        "is tracked — stop it with Ctrl-C in its terminal"}
    if pid is None:
        return {"ok": False, "why": "could not find the recorder process"}
    try:
        os.kill(int(pid), signal.SIGINT)
    except ProcessLookupError:
        return {"ok": False, "why": "the recorder is already gone"}
    except Exception as e:
        return {"ok": False, "why": str(e)}
    return {"ok": True, "note": "stopping — transcription runs next, watch the log"}


def _session_by_name(sid) -> Path | None:
    """A session id from the page, resolved to a real directory by NAME MATCH ONLY.

    Never joined onto a path: "../../etc", "/etc/passwd" and "" all simply fail to match
    anything in the list, so there is no traversal to defend against in the first place.
    The live session is included because it has no transcript.json yet and so is absent
    from existing_session_dirs() — and it is precisely the one an alert is about.
    """
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    if not SESSION_ID_RE.match(sid):     # the same gate /api/summary already applies
        return None
    try:
        live = MT.active_session()
    except Exception:
        live = None
    if live is not None and live.name == sid:
        return live
    try:
        return next((x for x in existing_session_dirs() if x.name == sid), None)
    except Exception:
        return None


def capture_answer(body: dict) -> dict:
    """POST /api/capture/answer — the user's yes/no on "is anyone talking on the call?".

    The question is the point: a muted far end and a dead tap look identical from here,
    so the tool asks rather than asserts. "yes, they are talking" over a silent system
    track is a confirmation and turns the card into a real alert; "no" dismisses it.
    His answer is the only thing this writes, and it goes in OUR file — the recorder's
    capture_events.jsonl is append-only and never written here.
    """
    try:
        d = _session_by_name(body.get("session"))
        if d is None:
            return {"ok": False, "why": "no such session"}
        if body.get("answer") not in ("yes", "no"):
            return {"ok": False, "why": "answer must be yes or no"}
        key = body.get("key")
        if not isinstance(key, str) or not key.strip():
            return {"ok": False, "why": "no alert to answer"}
        h = MT.capture_health(d)
        # Only ever answers the alert that is current. A page left open since the last
        # alert must not be able to dismiss a new one it has never shown them.
        if h.get("key") != key:
            return {"ok": False, "why": "that alert is no longer the current one",
                    "health": h}
        ok = MT.record_health_answer(d, key, body["answer"])
        return {"ok": bool(ok), "health": MT.capture_health(d),
                "why": "" if ok else "could not write the answer"}
    except Exception as e:
        return {"ok": False, "why": str(e)}


def capture_recapture(body: dict) -> dict:
    """POST /api/capture/recapture — tear down and rebuild the SCStream, on demand.

    The user asked for this as a STANDARD button, not an error-state one: *"I can just click
    it even if there isn't an issue."* So it is enabled whenever a capture is running and
    hidden behind nothing. It sends SIGUSR1, and only ever to a pid that has just been
    confirmed twice over to be an mtcapture — from its argv in the process table, and
    again by pid. A pid in the request body is ignored outright — the only pid this will
    ever signal is the one it just read out of the process table itself. The recording is
    never stopped and never restarted.
    """
    try:
        live = MT.active_session()
        if live is None:
            return {"ok": False, "why": "nothing is recording"}
        want = body.get("session")
        if isinstance(want, str) and want.strip() and want.strip() != live.name:
            return {"ok": False, "why": "that is not the recording that is running"}
        r = MT.signal_recapture()
        if not isinstance(r, dict):
            return {"ok": False, "why": "could not request a recapture"}
        if r.get("ok"):
            r["health"] = MT.capture_health(live)
        return r
    except Exception as e:
        return {"ok": False, "why": str(e)}


def detect_meeting() -> dict:
    """Is a live call on screen right now? Phase 2 builds the prompt on this."""
    if not MT.WATCH.exists():
        return {"live": False}
    try:
        r = subprocess.run([str(MT.WATCH), "--list"], capture_output=True, text=True,
                           timeout=20)
    except Exception:
        return {"live": False}
    best = None
    for line in r.stdout.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 8:
            continue
        reason, title = f[6], f[7]
        if reason == "active meeting" and title:
            best = title
            break
    return {"live": bool(best), "title": best or ""}



# ---------------------------------------------------------------- re-process

# Where a re-process keeps the transcript it replaces. It holds a transcript.json, so the
# session list (rglob) and the search walk (SKIP_DIRS in search.py) both skip it by name.
REPROCESS_BACKUPS = "reprocess-backups"

# Which tracks a re-process transcribes. Both are always on disk; this only picks.
REPROCESS_MODES = {
    "both":   [],
    "system": ["--no-mic"],                       # the Teams track carries everyone
    "mic":    ["--no-system", "--in-person"],     # the room mic, split into voices
}


def _track_check(d: Path) -> dict | None:
    try:
        sys.path.insert(0, str(HERE))
        import tracks as tracksmod
        r = tracksmod.read(d)
    except Exception:
        return None
    return {k: r.get(k) for k in ("verdict", "headline", "recommend")} if r else None


# Kept apart from the pass-*.json pair on purpose: that pair is how the server finds the
# RECORDER, and _live_entry() takes "the newest live pass" as the recorder in a
# recording's first second. A re-process must never be mistaken for one.
def _reprocess_json(d: Path) -> Path:
    return RUN / f"reprocess-{d.name}.json"


def _reprocess_log(d: Path) -> Path:
    return RUN / f"reprocess-{d.name}.log"


def reprocess_status(d: Path) -> dict | None:
    try:
        info = json.loads(_reprocess_json(d).read_text())
    except Exception:
        return None
    running = _pid_alive(info.get("pid", 0))
    step = ""
    try:
        steps = [ln for ln in _reprocess_log(d).read_text(errors="replace").splitlines()
                 if ln.startswith("==>") or "error:" in ln]
        step = _ANSI.sub("", steps[-1]) if steps else ""
    except Exception:
        pass
    done = (not running and (d / "transcript.json").exists()
            and dt.datetime.fromtimestamp((d / "transcript.json").stat().st_mtime,
                                          dt.timezone.utc).isoformat(timespec="seconds")
            >= (info.get("started_at") or ""))
    return {"running": running, "mode": info.get("mode"), "started_at": info.get("started_at"),
            "backup": info.get("backup"), "step": step,
            "state": "running" if running else ("done" if done else "failed")}


def start_reprocess(body: dict) -> dict:
    d = next((x for x in existing_session_dirs() if x.name == body.get("id")), None)
    if d is None:
        return {"ok": False, "why": "no such session"}
    mode = body.get("mode")
    if mode not in REPROCESS_MODES:
        return {"ok": False, "why": f"unknown mode {mode!r}"}
    if (d / "audio_removed.json").exists():
        return {"ok": False, "why": "this meeting's audio was removed — restore it from the "
                                    "trash on the Archive page to re-process"}
    # A pass competes with a live recording for the GPU, and on Oct 7 that contention is
    # what made the live transcript fall minutes behind. The recording comes first.
    if MT.active_session() is not None:
        return {"ok": False, "why": "a meeting is recording — re-process once it ends"}
    st = reprocess_status(d)
    if st and st["running"]:
        return {"ok": False, "why": "already re-processing this meeting"}
    for e in _pass_entries():
        if _pid_alive(e["pid"]) and _same_path(_session_of(e["log"]), d):
            return {"ok": False, "why": "this meeting's first pass is still running"}

    # Re-processing rewrites the transcript and the speaker map, and the clusters it finds
    # get new keys, so names confirmed on this page do not carry over. Keep the old ones.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = d / REPROCESS_BACKUPS / stamp
    backup.mkdir(parents=True, exist_ok=True)
    for f in ("transcript.json", "transcript.md", "transcript.txt", "speakers.json"):
        if (d / f).exists():
            shutil.copy2(d / f, backup / f)

    meta = {}
    try:
        meta = json.loads((d / "transcript.json").read_text()).get("meta", {})
    except Exception:
        pass
    cmd = [str(HERE / "mt"), "process", str(d), *REPROCESS_MODES[mode]]
    if meta.get("title"):
        cmd += ["--title", meta["title"]]
    if meta.get("customer"):
        cmd += ["--customer", meta["customer"]]
    logf = _reprocess_log(d)
    with open(logf, "w", buffering=1) as log:
        try:
            child = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
        except Exception as e:
            return {"ok": False, "why": str(e)}
    _track(child)
    _reprocess_json(d).write_text(json.dumps({
        "pid": child.pid, "mode": mode, "cmd": cmd,
        "backup": str(backup.relative_to(d)),
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }, indent=2))
    return {"ok": True, "pid": child.pid, "backup": str(backup.relative_to(d))}


def session_detail(sid: str) -> dict:
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None:
        return {"ok": False, "why": "no such session"}
    # Same fold as the live view. A tap that died mid-call is exactly the thing to know
    # BEFORE quoting a transcript, so it travels with the session rather than living
    # only in the banner that was on screen at the time.
    try:
        health = MT.capture_health(d)
    except Exception:
        health = None
    doc = json.loads((d / "transcript.json").read_text())
    meta, names = doc.get("meta", {}), doc.get("speakers", {})
    tl = doc.get("timeline", [])

    secs, lines = {}, {}
    for e in tl:
        k = e["speaker_key"]
        secs[k] = secs.get(k, 0.0) + (e["end"] - e["start"])
        lines[k] = lines.get(k, 0) + 1

    speakers = []
    for k, v in names.items():
        # Does the name this cluster currently carries have a voiceprint behind it?
        # The page only offers "retire the print" where there is a print to retire —
        # otherwise the global control appears on a label that means nothing globally.
        nm = v.get("name", k)
        pr = None if k == "mic" else voiceprint_of(nm)
        rt = None if (k == "mic" or pr is not None) else retired_of(nm)
        speakers.append({
            "key": k, "name": nm, "basis": v.get("source", ""),
            "score": v.get("score"), "selfsim": v.get("self_similarity"),
            "mixed": bool(v.get("mixed")), "verified": v.get("confidence") == "verified",
            "matched": k != "mic" and _strong(v), "runner_up": v.get("runner_up"),
            "secs": round(secs.get(k, 0)), "secsText": MT.hhmmss(secs.get(k, 0)),
            "lines": lines.get(k, 0), "is_mic": k == "mic",
            "print": _vp_card(pr) if pr else None,
            "retired_print": _vp_card(rt, retired=True) if rt else None,
            "unassigned": sum(1 for e in tl
                              if e["speaker_key"] == k and not e.get("override")),
        })
    speakers.sort(key=lambda s: (s["is_mic"], -s["secs"]))

    # candidates: calendar invite first, then the OCR roster, then everyone enrolled
    cands, seen = [], set()
    def add(n):
        if n and n not in seen:
            seen.add(n); cands.append(n)
    for n in MT.read_invitees(d):
        add(n)
    for r in (meta.get("onscreen") or []):
        add(r.get("name") if isinstance(r, dict) else r)
    for n in (s["name"] for s in MT.load_voiceprints()):
        add(n)

    # Initials the meeting UI showed. Teams renders these instead of names during a
    # screen share, and they are an independent check on a label: if you type a name
    # whose initials were never on screen, that is worth a second listen.
    seen_initials = []
    log = d / "screen.jsonl"
    if log.exists():
        try:
            sys.path.insert(0, str(HERE))
            import screen as screenmod
            got = set()
            for ln in log.read_text(errors="replace").splitlines():
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                for x in rec.get("lines") or []:
                    tx = (x.get("text") or "").strip()
                    if screenmod.INITIALS.match(tx):
                        got.add(tx)
            seen_initials = sorted(got)
        except Exception:
            pass

    tracks = [t for t in ("system", "mic")
              if any((d / f"{t}{e}").exists() for e in (".flac", ".wav"))]
    # Cluster quality thresholds go to the page rather than living in the JS: they are
    # tuning, they are already in config.json, and a number baked into a template is a
    # number that silently disagrees with the one mt actually enforces.
    cfg = MT.config()
    try:
        thr = {"min_self": float(cfg["min_self_similarity"]),
               "caution_self": float(cfg.get("caution_self_similarity") or 0.88)}
    except (TypeError, ValueError):
        thr = {"min_self": 0.60, "caution_self": 0.88}
    return {"ok": True, "id": d.name, "path": str(d.relative_to(MT.WORKSPACE)),
            "title": meta.get("title") or d.name, "duration": meta.get("duration") or 0,
            "durationText": MT.hhmmss(meta.get("duration")), "mic": meta.get("mic") or "",
            "customer": meta.get("customer") or "", "tracks": tracks,
            "audio_removed": _audio_removed(d),
            "tracks_used": meta.get("tracks_used")
                           or sorted({e.get("track", "system") for e in tl}),
            "track_check": _track_check(d), "reprocess": reprocess_status(d),
            "handed_off": (d / "handoff.json").exists(), "thresholds": thr,
            "seen_initials": seen_initials, "capture_health": health,
            "speakers": speakers, "candidates": cands,
            "enrolled": [_vp_card(r) for r in live_prints()],
            "retired": [_vp_card(r, retired=True) for r in retired_prints()],
            "timeline": [{"start": round(e["start"], 2), "end": round(e["end"], 2),
                          "key": e["speaker_key"], "track": e.get("track", "system"),
                          "override": e.get("override"), "text": e["text"]} for e in tl]}


# ── vocal cues (VOCAL-CUES-PLAN.md) ──────────────────────────────────────────
# Measuring runs as `mt cues <dir>` in its own process at low priority: numpy work in
# this server's thread would stall every page while it ran. It is refused while a
# recording is live, so it can never slow the live transcript. Scoring is cheap and
# happens per request, against whatever speakers are confirmed right now.
_cue_jobs: dict = {}       # session id -> {"proc", "log"}


def _cue_dir(sid: str) -> Path | None:
    return next((x for x in existing_session_dirs() if x.name == sid), None)


def _cue_job(sid: str) -> dict | None:
    j = _cue_jobs.get(sid)
    if not j:
        return None
    try:
        lines = j["log"].read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    prog = next((ln for ln in reversed(lines) if ln.startswith("progress ")), "")
    done, _, total = prog[len("progress "):].partition("/")
    rc = j["proc"].poll()
    if rc is None:
        return {"state": "running",
                "done": int(done) if done.isdigit() else 0,
                "total": int(total) if total.isdigit() else 0}
    if rc != 0:
        return {"state": "failed", "why": "\n".join(lines[-6:]) or f"exit {rc}"}
    return None


def cues_view(sid: str) -> dict:
    d = _cue_dir(sid)
    if d is None:
        return {"ok": False, "why": "no such session"}
    import cues
    out = {"ok": True, "job": _cue_job(sid), "guide": cues.GUIDE}
    try:
        out.update(cues.score(d))
    except Exception as e:
        out.update({"analyzed": False, "error": f"could not read the cues: {e}"})
    return out


def cues_analyze(p: dict) -> dict:
    sid = p.get("session") or ""
    d = _cue_dir(sid)
    if d is None:
        return {"ok": False, "why": "no such session"}
    if (_cue_job(sid) or {}).get("state") == "running":
        return {"ok": True, "note": "already measuring"}
    if (d / "audio_removed.json").exists():
        return {"ok": False, "why": "this meeting's audio was removed, so there is nothing "
                                    "to measure"}
    if running().get("recording"):
        return {"ok": False, "why": "A recording is running. Vocal cues wait until it ends, "
                                    "so they can never slow the live transcript."}
    log = RUN / f"cues-{sid}.log"
    fh = open(log, "w", buffering=1)
    cmd = [str(HERE / "mt"), "cues", str(d)] + (["--force"] if p.get("force") else [])
    proc = subprocess.Popen(cmd, stdout=fh, stderr=fh, start_new_session=True,
                            preexec_fn=lambda: os.nice(10))
    _cue_jobs[sid] = {"proc": proc, "log": log}
    return {"ok": True}


def _live_dir(sid: str):
    """(session dir, is_live) for the Room panel. A session still recording has no
    speakers.json yet, so it is found through the running capture, not the session list."""
    st = running()
    live = bool(st.get("recording")) and (not sid or st.get("session") == sid)
    if live:
        return Path(st["path"]), True, st
    return _cue_dir(sid), False, st


def _name_candidates(d: Path) -> list[str]:
    """Who you can pick when naming a voice live: this meeting's invitees first, then
    verified contacts, then enrolled voices. Typing any other name is always allowed."""
    import livenames
    out, seen = [], set()
    def add(n):
        k = (n or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(n.strip())
    verified, by_email = [], {}
    try:
        con = sqlite3.connect(f"file:{P.DB}?mode=ro", uri=True, timeout=2.0)
        for n, em, vs in con.execute("select name, email, verification_status from contacts "
                                     "where merged_into is null and archived_at is null "
                                     "order by name"):
            if em:
                by_email[em.strip().lower()] = n
            if vs == "verified":
                verified.append(n)
        con.close()
    except Exception:
        pass
    # Calendar invitees without a display name arrive as bare emails; show the person.
    for n in MT.read_invitees(d):
        add(by_email.get(n.strip().lower(), n))
    for n in verified:
        add(n)
    for p in MT.load_voiceprints():
        add(p.get("name"))
    return out


def cues_live_name(p: dict) -> dict:
    """Name a Room card. kind: 'maybe' (a near-miss, `from` = the guessed name), 'match'
    (an auto-matched name), or 'them' (the blended far end — names its last minute)."""
    import cues, livenames
    d, _, _ = _live_dir(p.get("session") or "")
    if d is None:
        return {"ok": False, "why": "That recording has ended or does not exist."}
    kind, frm, to = p.get("kind"), p.get("from"), (p.get("to") or "").strip()
    if kind not in ("maybe", "match", "them", "voice"):
        return {"ok": False, "why": "unknown card"}
    rows = cues._live_rows(d, cues.cfg())      # already reflects earlier decisions
    far = [r for r in rows if r.get("track") == "system"]
    if kind == "voice":                         # a "Speaker N" card: all of that voice
        segs = [(r["s"], r["e"]) for r in far if r.get("name") == frm]
    elif kind == "maybe":
        segs = [(r["s"], r["e"]) for r in far if not r.get("who") and r.get("hint") == frm]
    elif kind == "match":
        segs = [(r["s"], r["e"]) for r in far if r.get("who") == frm]
    else:
        them = [r for r in far if not r.get("who") and not r.get("hint")]
        end = max((r["e"] for r in them), default=0)
        segs = [(r["s"], r["e"]) for r in them if r["e"] >= end - livenames.THEM_WINDOW]
    return livenames.add_rule(d, kind, frm, to, segs)


def cues_live_rename(p: dict) -> dict:
    """Rename (or, with no `to`, unname) a voice you named earlier in this call."""
    import livenames
    d, _, _ = _live_dir(p.get("session") or "")
    if d is None:
        return {"ok": False, "why": "That recording has ended or does not exist."}
    return livenames.rename(d, p.get("from") or "", p.get("to"))


def cues_live_undo(p: dict) -> dict:
    import livenames
    d, _, _ = _live_dir(p.get("session") or "")
    if d is None:
        return {"ok": False, "why": "That recording has ended or does not exist."}
    return livenames.undo(d)


def cues_live(sid: str) -> dict:
    """The Room panel's feed."""
    import cues
    d, live, st = _live_dir(sid)
    if d is None:
        return {"ok": False, "recording": False,
                "why": "That recording has ended or does not exist."}
    try:
        out = cues.live_score(d, owner=MT.config().get("owner"))
    except Exception as e:
        return {"ok": False, "recording": live, "why": f"could not read the live cues: {e}"}
    out.update({"ok": True, "recording": live, "session": d.name,
                "elapsedText": st.get("elapsedText") if live else None,
                "analyzed": (d / cues.FILE).exists()})
    try:
        import awards
        out["awards"] = awards.live_view(d, MT.config().get("owner"))
    except Exception:
        out["awards"] = {"awards": []}
    try:
        import livenames
        out["named"] = livenames.summary(d)
        out["candidates"] = _name_candidates(d)
    except Exception:
        out["named"], out["candidates"] = [], []
    # Room's field guide: the card labels first, then the gauges they share with review.
    out["guide"] = cues.ROOM_GUIDE + [g for g in cues.GUIDE
                                      if g[0] in ("Energy", "Friction", "Colours",
                                                  "Baseline dots")]
    return out


def awards_view(sid: str) -> dict:
    """Conversation dynamics + meeting awards for one meeting. Dynamics come from the
    transcript's own timing, so they show for every meeting; Best Saying and Spice Level
    appear only where the meeting was opted into vocal cues."""
    d = _cue_dir(sid)
    if d is None:
        return {"ok": False, "why": "no such session"}
    import awards
    try:
        return dict(awards.view(d), ok=True, job=_cue_job(sid), guide=awards.GUIDE)
    except Exception as e:
        return {"ok": False, "why": f"could not work out the awards: {e}"}


def awards_crown(p: dict) -> dict:
    d = _cue_dir(p.get("session") or "")
    if d is None:
        return {"ok": False, "why": "no such session"}
    import awards
    i = p.get("i")
    return awards.crown(d, None if i is None else int(i))


def cues_clear(p: dict) -> dict:
    d = _cue_dir(p.get("session") or "")
    if d is None:
        return {"ok": False, "why": "no such session"}
    import cues
    return {"ok": True, "cleared": cues.clear(d)}


# merge_suggestions() was here and is deliberately gone. Measured on two real meetings:
# the highest SAME-person cluster pair scored 0.62 and the highest DIFFERENT-person pair
# 0.60 — no separation, so any threshold is noise. It is also structurally redundant:
# consolidate() already merges every pair above merge_similarity (0.60), so whatever
# survives is either genuinely a different person or a mixed cluster, and mixed clusters
# are caught by split-half self-similarity instead (clean 0.78-0.97 vs mixed 0.36/0.07).
# Do not re-add it without a signal that actually separates.


# A meeting-folder name and nothing else: one path segment, no separators, no leading
# dot (which also rules out ".."). The id arrives off a URL, so it is hostile until it
# matches this — same posture as the /static/ handler.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def meeting_dir(sid: str) -> Path | None:
    """Resolve a meeting-folder name to a directory, WITHOUT requiring transcript.json.

    existing_session_dirs() defines a session as "has transcript.json". That is right for
    the recording pipeline and for /review, and wrong as a gate on reading a summary: most
    of the corpus predates mt and is a hand-written summary.md with no transcript at all
    (105 of 114 indexed summaries). This widens the lookup for that one case.

    Belt and braces on the way in: the name must be a single safe segment AND the resolved
    directory must still sit inside the workspace.
    """
    sid = (sid or "").strip()
    if not SESSION_ID_RE.match(sid):
        return None
    ws = MT.WORKSPACE.resolve()
    roots = sorted(P.CUSTOMERS.glob("*/meetings"))
    roots.append(P.TEAM_MEETINGS)
    for r in roots:
        try:
            d = (r / sid).resolve()
            d.relative_to(ws)
        except (ValueError, OSError):
            continue
        if d.is_dir():
            return d
    return None


def read_summary(sid: str) -> dict:
    """The summary the assistant wrote for a session, as raw markdown."""
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None:
        # Not a recorded session — but it may still be a pre-mt meeting folder with a
        # summary in it. Search finds those; this is what lets the user open them.
        d = meeting_dir(sid)
    if d is None:
        return {"ok": False, "why": "no such session"}
    f = d / "summary.md"
    if not f.exists():
        return {"ok": False, "why": "not summarised yet",
                "hint": "Ask your assistant to summarise this one — the transcript is ready."}
    try:
        return {"ok": True, "id": sid, "markdown": f.read_text(errors="replace")}
    except OSError as e:
        return {"ok": False, "why": str(e)}


SEARCH_MAX = 100      # a hand-typed ?limit= must not be able to ask for the whole index


def run_search(q: str, kind: str = "", customer: str = "", limit="20") -> dict:
    """GET /api/search — the full-text index, behind a deliberately paranoid wrapper.

    search.py is a separate module with its own SQLite index, and this server has one
    job that matters more than search: being able to start a recording. So the import
    is lazy and every failure — module missing, index missing, bad FTS syntax that
    escaped search.py's own guard — comes back as HTTP 200 with `error` set. The page
    degrades to a line of text; it never sees a 500.
    """
    q = (q or "").strip()
    if not q:
        return {"q": "", "count": 0, "hits": [], "error": None}
    try:
        n = int(str(limit).strip() or SEARCH_MAX)
    except ValueError:
        n = 20
    n = max(1, min(n, SEARCH_MAX))
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))   # search.py sits next to web.py
        import search as SEARCH             # lazy on purpose — see docstring
        # search() takes one kind. "Transcripts" is two kinds — the segments mt records
        # and the pasted transcript.md files that predate it — and a filter that silently
        # drops one of them is worse than no filter. So fan out here and merge. Scores
        # come from the same query string, so they are comparable and a plain descending
        # sort is the right merge.
        kinds = [k.strip() for k in (kind or "").split(",") if k.strip()] or [None]
        hits = []
        for k in kinds:
            hits += list(SEARCH.search(q, limit=n, kind=k,
                                       customer=(customer or None)) or [])
        if len(kinds) > 1:
            hits.sort(key=lambda h: float(h.get("score") or 0.0), reverse=True)
            del hits[n:]
        # Round-trip now rather than inside _json(): a hit carrying something
        # unserialisable would otherwise blow up mid-response, after the headers.
        hits = json.loads(json.dumps(hits, default=str))
    except Exception as e:
        return {"q": q, "count": 0, "hits": [],
                "error": f"search unavailable — {type(e).__name__}: {e}"[:300]}
    return {"q": q, "count": len(hits), "hits": hits, "error": None}


# ---------------------------------------------------------------- contacts
#
# The people page. contacts.py owns every DB access behind these handlers, including the
# only writes this server makes to the canonical workspace DB. Same posture as search,
# for the same reason: the module is imported lazily INSIDE each handler, so a missing
# or half-written contacts.py cannot stop `mt web` from serving. The one job that
# matters more than looking someone up is being able to start a recording.
#
# Nothing here gates anything. The page surfaces verification state; naming a speaker in
# `mt` stays free-form. The owner was explicit about that.
#
# ensure_schema() is a WRITE (ALTER TABLE ... ADD COLUMN), so it is called from the three
# POST handlers only, once per process — never from a read. `mt web` browsing contacts
# must not be able to modify the 68 MB canonical DB just by being opened. The assistant applies
# the migration by hand; the first save self-heals it if that has not happened yet.

CONTACT_LIMIT = 1000     # ~274 rows today: the page fetches the lot and filters locally
CONTACT_Q_MAX = 200      # ?q= is a search box, not a payload
STATUSES = ("verified", "needs_confirmation")

# Every field the page is allowed to write, with the cap applied before it reaches SQL.
# Anything not named here is dropped without comment — an unknown key is either a stale
# page or someone poking at the port, and neither gets to pick column names.
CONTACT_TEXT_FIELDS = {
    "name": 200, "email": 200, "role": 200, "phone": 60, "company": 200,
    "slack_user_id": 60, "source": 200, "aliases": 4000, "notes": 20000,
}

_schema_done = False


def _contacts():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))   # contacts.py sits next to web.py
    import contacts as C                # lazy on purpose — see the note above
    return C


def _ensure_once(C) -> None:
    """Apply the added columns at most once per process, and only on a write path."""
    global _schema_done
    if _schema_done:
        return
    C.ensure_schema()
    _schema_done = True


def _cwhy(e: Exception) -> str:
    """The page shows this verbatim, so a refusal contacts.py raised on purpose — "#93 was
    already merged into #31" — reads as a sentence, not as a stack trace. Anything else
    keeps its exception class, because an unexpected failure should look unexpected."""
    s = str(e) if isinstance(e, (ValueError, KeyError)) else f"{type(e).__name__}: {e}"
    s = s.strip("'\"") or type(e).__name__
    if "no such column" in str(e).lower():
        s += " — the contacts schema migration has not been applied to this DB yet"
    return s[:300]


def _cid(v) -> int | None:
    """An id is a positive integer or it is nothing.

    '1 OR 1=1', 'abc', '-1', '12.5' and a 300-character blob all come back None and the
    caller answers "no such contact" with HTTP 200. Nothing unparsed reaches contacts.py.
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    else:
        s = str(v if v is not None else "").strip()
        if not re.fullmatch(r"[0-9]{1,9}", s):
            return None
        n = int(s)
    return n if n > 0 else None


def _clean_fields(body: dict) -> dict:
    """Whitelist, coerce and clamp everything arriving in a POST body."""
    src = body.get("fields") if isinstance(body.get("fields"), dict) else body
    out: dict = {}
    for k, cap in CONTACT_TEXT_FIELDS.items():
        if k not in src:
            continue
        v = src[k]
        if v is None:
            out[k] = None
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str):
            continue                     # a list or dict in a text column is a bug, not input
        out[k] = v.replace("\r\n", "\n").strip()[:cap]
    if "customer_id" in src:
        v = src["customer_id"]
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none")):
            out["customer_id"] = None    # deliberately clearable: people change accounts
        else:
            n = _cid(v)
            if n is not None:
                out["customer_id"] = n   # unparseable is dropped, never silently cleared
    if "verification_status" in src:
        v = str(src["verification_status"] or "").strip()
        if v in STATUSES:
            out["verification_status"] = v
    return out


def _safe(obj):
    """Round-trip now rather than inside _json(): an unserialisable value coming back
    from the DB would otherwise blow up mid-response, after the headers are out."""
    return json.loads(json.dumps(obj, default=str))


def version_info() -> dict:
    """GET /api/version — one product version, plus what is inside it (for the About panel
    and bug reports). The parts are never released on their own, so they get no version
    numbers of their own; this lists what each one actually is on this machine."""
    def git(*a):
        try:
            r = subprocess.run(["git", "-C", str(HERE), *a], capture_output=True, text=True,
                               timeout=3)
            return r.stdout.strip() or None
        except Exception:
            return None
    try:
        import dbschema
        schema = dbschema.SCHEMA_VERSION
    except Exception:
        schema = None
    models = []
    try:
        for line in (HERE / "models.sha256").read_text().splitlines():
            if line.strip():
                sha, name = line.split(None, 1)
                models.append({"name": name.strip(), "sha256": sha[:12],
                               "present": (HERE / "models" / name.strip()).exists()})
    except Exception:
        pass
    binaries = []
    for b in ("mtcapture", "mtwatch", "mtaudio", "mtcal"):
        p = HERE / "build" / b
        binaries.append({"name": b, "built": dt.datetime.fromtimestamp(p.stat().st_mtime)
                         .strftime("%Y-%m-%d %H:%M") if p.exists() else None})
    packages = {}
    try:
        from importlib import metadata
        for n in ("sherpa-onnx", "numpy"):
            try:
                packages[n] = metadata.version(n)
            except Exception:
                packages[n] = None
    except Exception:
        pass
    return {"version": MT.VERSION, "git": git("describe", "--tags", "--always", "--dirty"),
            "schema": schema, "models": models, "binaries": binaries, "packages": packages,
            "python": sys.version.split()[0], "policy_url": P.RECORDING_POLICY_URL or None,
            "macos": FB.macos(), "arch": platform.machine() or None,
            "feedback_repo": P.FEEDBACK_REPO or None}


def contacts_list(q: str = "", customer: str = "", status: str = "", limit="",
                  archived: str = "live") -> dict:
    """GET /api/contacts — the whole list plus the header counts, in one round trip."""
    q = (q or "").strip()[:CONTACT_Q_MAX]
    cust = (customer or "").strip()[:60] or None
    st = (status or "").strip()
    st = st if st in STATUSES else None
    try:
        n = int(str(limit).strip() or CONTACT_LIMIT)
    except ValueError:
        n = CONTACT_LIMIT
    n = max(1, min(n, CONTACT_LIMIT))
    try:
        C = _contacts()
        arch = archived if archived in ("live", "archived", "all") else "live"
        rows = _safe(list(C.list_contacts(q=q, customer=cust, status=st, limit=n,
                                          archived=arch) or []))
        counts = _safe(dict(C.stats() or {}))
        # Additive to the pinned contract's shape: the page reads it when present and is
        # unaffected when it is not, so an older contacts.py cannot break this handler.
        guide = _safe(dict(getattr(C, "field_help", dict)() or {}))
        accounts = _safe(dict(getattr(C, "accounts", dict)() or {}))
    except Exception as e:
        return {"contacts": [], "stats": {}, "error": f"contacts unavailable — {_cwhy(e)}"}
    return {"contacts": rows, "stats": counts, "field_help": guide, "accounts": accounts,
            "error": None}


def contacts_one(cid_raw) -> dict:
    """GET /api/contacts/one — one person, plus the meetings they turn up in."""
    cid = _cid(cid_raw)
    if cid is None:
        return {"contact": None, "seen": [], "error": "no such contact"}
    try:
        C = _contacts()
        row = C.get_contact(cid)
        if not row:
            return {"contact": None, "seen": [], "error": "no such contact"}
        row = dict(row)
        if "has_voiceprint" not in row:
            try:
                row["has_voiceprint"] = bool((C.voiceprint_status(row.get("name") or "")
                                              or {}).get("enrolled"))
            except Exception:
                row["has_voiceprint"] = False
        # seen_in() reaches the search index, which is a separate module and a separate
        # DB file. It is contracted not to raise, but a person's card is still worth
        # showing without it, so a failure here costs the list and nothing else.
        try:
            seen = _safe(list(C.seen_in(cid, limit=20) or []))
        except Exception:
            seen = []
    except Exception as e:
        return {"contact": None, "seen": [], "error": _cwhy(e)}
    return {"contact": _safe(row), "seen": seen, "error": None}


def contacts_dupes(include_dismissed="") -> dict:
    """GET /api/contacts/duplicates — exact duplicate names, grouped.

    ?dismissed=1 also returns the groups already ruled "different people", flagged, so
    the page can offer to show them again. Default is the plain contract call.
    """
    want = str(include_dismissed or "").strip().lower() in ("1", "true", "yes")
    try:
        C = _contacts()
        groups = _safe(list(C.duplicates(include_dismissed=want) or []))
    except Exception as e:
        return {"groups": [], "error": _cwhy(e)}
    return {"groups": groups, "error": None}


def contacts_triage() -> dict:
    """GET /api/contacts/triage — rows that look like a placeholder rather than a person.

    A worklist, never a verdict. Half of these want a surname or an email rather than
    archiving, so the page says "needs a real name", not "archive these"."""
    try:
        rows = _safe(list(_contacts().triage() or []))
    except Exception as e:
        return {"rows": [], "error": f"contacts unavailable — {_cwhy(e)}"}
    return {"rows": rows, "error": None}


def contacts_rename_check(cid_raw="", name_raw="") -> dict:
    """GET /api/contacts/rename-check — what would a rename actually do. READ-ONLY.

    Answers the two things the form cannot see: whether an enrolled voiceprint is keyed
    to the OLD name (renaming here does not move it — `mt rename` does), and whether the
    NEW name already belongs to someone, which makes this a merge rather than a rename."""
    cid = _cid(cid_raw)
    if cid is None:
        return {"changed": False, "error": "no such contact"}
    try:
        r = _safe(_contacts().rename_impact(cid, _person_name(name_raw)))
    except Exception as e:
        return {"changed": False, "error": _cwhy(e)}
    r["error"] = None
    return r


def contacts_archive(body: dict) -> dict:
    """POST /api/contacts/archive — hide a contact, or bring one back. Never deletes."""
    b = body if isinstance(body, dict) else {}
    cid = _cid(b.get("id"))
    if cid is None:
        return {"ok": False, "contact": None, "why": "no such contact"}
    want = b.get("archived") is not False
    reason = str(b.get("reason") or "")[:400]
    try:
        C = _contacts()
        _ensure_once(C)
        row = C.set_archived(cid, want, reason)
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    return {"ok": True, "contact": _safe(row), "why": None}


def contacts_save(body: dict) -> dict:
    """POST /api/contacts/save — create when there is no id, partial update when there is."""
    b = body if isinstance(body, dict) else {}
    fields = _clean_fields(b)
    raw_id = b.get("id")
    cid = _cid(raw_id)
    if cid is None and raw_id not in (None, "", 0):
        return {"ok": False, "contact": None, "why": "no such contact"}
    try:
        C = _contacts()
        _ensure_once(C)
        if cid is None:
            if not (fields.get("name") or "").strip():
                return {"ok": False, "contact": None, "why": "a name is required"}
            row = C.create_contact(fields)
        else:
            if not C.get_contact(cid):
                return {"ok": False, "contact": None, "why": "no such contact"}
            if "name" in fields and not (fields["name"] or "").strip():
                return {"ok": False, "contact": None, "why": "a name cannot be blank"}
            if not fields:
                return {"ok": False, "contact": None, "why": "nothing to save"}
            row = C.update_contact(cid, fields)
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    return {"ok": True, "contact": _safe(row), "why": None}


def contacts_set_status(body: dict) -> dict:
    """POST /api/contacts/status — the one-click verify behind the confirmation queue."""
    b = body if isinstance(body, dict) else {}
    cid = _cid(b.get("id"))
    status = str(b.get("status") or "").strip()
    if cid is None:
        return {"ok": False, "contact": None, "why": "no such contact"}
    if status not in STATUSES:
        return {"ok": False, "contact": None, "why": "status must be verified or needs_confirmation"}
    try:
        C = _contacts()
        _ensure_once(C)
        if not C.get_contact(cid):
            return {"ok": False, "contact": None, "why": "no such contact"}
        row = C.set_status(cid, status)
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    return {"ok": True, "contact": _safe(row), "why": None}


def contacts_merge(body: dict) -> dict:
    """POST /api/contacts/merge — fold drop into keep. Never deletes: drop is tombstoned."""
    b = body if isinstance(body, dict) else {}
    keep, drop = _cid(b.get("keep_id")), _cid(b.get("drop_id"))
    if keep is None or drop is None:
        return {"ok": False, "contact": None, "why": "a merge needs two contact ids"}
    if keep == drop:
        return {"ok": False, "contact": None, "why": "cannot merge a contact into itself"}
    try:
        C = _contacts()
        _ensure_once(C)
        if not C.get_contact(keep) or not C.get_contact(drop):
            return {"ok": False, "contact": None, "why": "no such contact"}
        row = C.merge(keep, drop)
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    return {"ok": True, "contact": _safe(row), "why": None}


def contacts_not_duplicate(body: dict, dismiss: bool) -> dict:
    """POST /api/contacts/dismiss and /undismiss — "these two are different people".

    The ruling is written to the DB, symmetrically, rather than kept in the browser:
    the assistant reads the same table, and four of the twelve duplicate-name groups are
    one person deliberately carried once per account, so a ruling only one browser knew
    about would be re-raised forever.
    """
    b = body if isinstance(body, dict) else {}
    a_id, b_id = _cid(b.get("a_id")), _cid(b.get("b_id"))
    if a_id is None or b_id is None:
        return {"ok": False, "a": None, "b": None, "why": "this needs two contact ids"}
    if a_id == b_id:
        return {"ok": False, "a": None, "b": None,
                "why": "a contact cannot be a different person from itself"}
    try:
        C = _contacts()
        _ensure_once(C)
        fn = C.dismiss_duplicate if dismiss else C.undismiss_duplicate
        r = fn(a_id, b_id)
    except Exception as e:
        return {"ok": False, "a": None, "b": None, "why": _cwhy(e)}
    return {"ok": True, "a": _safe(r.get("a")), "b": _safe(r.get("b")),
            "dismissed": bool(r.get("dismissed")), "changed": bool(r.get("changed")),
            "why": None}


# ------------------------------------------------ enrolment <-> contacts bridge
#
# Enrolling a voice and having a contact row were two unconnected facts: enrolling
# "Alex Example" created nothing, and nothing said so. These handlers put a
# question between the two, never a write.
#
# The one rule the whole section is built around: NOTHING here is automatic. A name typed
# mid-call is the user's fastest guess, not a record. If enrolment had quietly created
# contacts, one typo would now be a permanent row — "Sam Sampel" sitting
# beside Sam Sample, and nothing to tell you which one the workspace means. So the
# server resolves the name, shows what it found, and stops. Every write below is one
# explicit click, and which write it is (rename / alias / create) is a question only
# the user can answer, because the three cases look identical from here:
#
#   a typo               -> rename the voiceprint (mt rename; the vector survives)
#   the contact is wrong -> fix the row, alias the old spelling
#   nickname/legal name  -> alias; both spellings are genuinely them
#
# The matching itself, and the 0.90 threshold, live in contacts.py with the calibration
# that produced them.

def _person_name(v) -> str:
    """A typed name, or "". Whitespace-folded; never long enough to be a payload."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return ""
    return " ".join(v.split())[:CONTACT_TEXT_FIELDS["name"]]


def _session_source(sid) -> tuple[object, str]:
    """(session dir or None, the `source` string a contact created here gets).

    The source is built from a session directory this server has VERIFIED exists, never
    from the string the browser sent: `source` is a traceable provenance note, and it is
    the one place where echoing client text back into a DB row would turn a stray "../.."
    into a permanent, quotable lie about where a person came from.
    """
    sid = sid if isinstance(sid, str) else ""
    d = next((x for x in existing_session_dirs() if x.name == sid), None) if sid else None
    if d is None:
        return None, "mt — enrolled from a recording (contacts page)"
    try:
        rel = str(d.relative_to(MT.WORKSPACE))
    except ValueError:
        rel = d.name
    return d, f"mt — enrolled from {rel}"[:CONTACT_TEXT_FIELDS["source"]]


def _resolved(r: dict) -> dict:
    """Shape a contacts.resolve_name() result for the page."""
    return {
        "query": r.get("query"),
        "exact": _safe(list(r.get("exact") or [])),
        "close": _safe([{"contact": h["contact"], "score": h["score"], "why": h["why"],
                         "matched": h.get("matched")} for h in (r.get("close") or [])]),
        "checked": r.get("checked") or 0,
    }


def contacts_resolve(name_raw, session_raw="") -> dict:
    """GET /api/contacts/resolve — "does the contact list know this name?" Read-only.

    Called straight after an enrolment succeeds. It reads; it never migrates the schema
    and never writes, so opening the review page cannot touch the canonical DB.
    """
    name = _person_name(name_raw)
    if not name:
        return {"ok": False, "why": "no name", "name": "", "exact": [], "close": [],
                "checked": 0, "error": None}
    try:
        C = _contacts()
        r = _resolved(C.resolve_name(name))
    except Exception as e:
        return {"ok": False, "name": name, "exact": [], "close": [], "checked": 0,
                "why": None, "error": f"contacts unavailable — {_cwhy(e)}"}
    r.update(ok=True, name=name, why=None, error=None,
             source=_session_source(session_raw)[1])
    return r


def contacts_unlinked() -> dict:
    """GET /api/contacts/unlinked — enrolled voiceprints no contact row answers to.

    The backlog the old silence was building up. Three today. Each one carries its own
    suggestions so the worklist offers the same choices the review page does.
    """
    try:
        C = _contacts()
        rows = [{"name": u["name"], "slug": u["slug"], "samples": u["samples"],
                 "updated": u["updated"],
                 "close": _safe(u["close"])} for u in (C.unlinked_voiceprints() or [])]
    except Exception as e:
        return {"ok": False, "unlinked": [], "count": 0,
                "error": f"contacts unavailable — {_cwhy(e)}"}
    return {"ok": True, "unlinked": rows, "count": len(rows), "error": None}


def contacts_add_alias(body: dict) -> dict:
    """POST /api/contacts/alias — record that this person also answers to that spelling.

    The right fix for exactly one of the three cases: the typed name and the stored name
    are BOTH correct (Steve/Stephen, Dan/Daniel). It appends; a contact's aliases are an
    accumulated history of how their name gets garbled and replacing the field would
    throw the rest of that away.
    """
    b = body if isinstance(body, dict) else {}
    cid = _cid(b.get("id"))
    alias = _person_name(b.get("alias"))
    if cid is None:
        return {"ok": False, "contact": None, "why": "no such contact"}
    if not alias:
        return {"ok": False, "contact": None, "why": "an alias cannot be blank"}
    try:
        # An alias is a spelling of a person's name, so it gets the same gate a
        # voiceprint name gets: no NULs, no bidi overrides, no 300-character blob. An
        # invisible character is not a spelling, and this field is rendered back to
        # the user as the record of how this person's name gets garbled.
        alias = MT.clean_name(alias, "the alias")
    except SystemExit as e:
        return {"ok": False, "contact": None, "why": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    try:
        C = _contacts()
        _ensure_once(C)
        before = C.get_contact(cid)
        if not before:
            return {"ok": False, "contact": None, "why": "no such contact"}
        known = {C.normalize_name(x) for x in
                 (before.get("aliases") or "").splitlines() if x.strip()}
        known.add(C.normalize_name(before.get("name")))
        already = C.normalize_name(alias) in known
        row = C.add_alias(cid, alias)
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    return {"ok": True, "contact": _safe(row), "already": already, "why": None}


def contacts_from_enrollment(body: dict) -> dict:
    """POST /api/contacts/from-enrollment — create the row this enrolment had no home in.

    Two things are deliberate and neither is negotiable:

    `needs_confirmation`, not verified. The workspace rule is that nobody joins the
    people map as verified without the owner confirming them, and a name typed in a hurry
    while a call is running is the opposite of that. It lands in the confirmation queue
    on /contacts, one click from verified once they have checked it.

    `source` names the session it came from, so six months later the row can be traced
    back to the recording that produced it instead of appearing from nowhere.
    """
    b = body if isinstance(body, dict) else {}
    name = _person_name(b.get("name"))
    if not name:
        return {"ok": False, "contact": None, "why": "a name is required"}
    try:
        name = MT.clean_name(name, "the name")      # mt's own gate: no NULs, no separators
    except SystemExit as e:
        return {"ok": False, "contact": None, "why": str(e)[:300]}
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    _, source = _session_source(b.get("session"))
    try:
        C = _contacts()
        _ensure_once(C)
        r = C.resolve_name(name)
        if r["exact"]:
            names = ", ".join(f"#{c['id']} {c['name']}" for c in r["exact"][:3])
            return {"ok": False, "contact": None, "exact": _safe(r["exact"]),
                    "why": f"{names} already answers to that name — "
                           f"creating another would just be a duplicate"}
        row = C.create_contact({"name": name,
                                "verification_status": "needs_confirmation",
                                "source": source})
    except Exception as e:
        return {"ok": False, "contact": None, "why": _cwhy(e)}
    return {"ok": True, "contact": _safe(row), "why": None}


def rename_voiceprint(body: dict) -> dict:
    """POST /api/voiceprints/rename — re-spell an enrolled print, via mt's own command.

    This is the fix for a typo, and it is the one that loses nothing: `mt rename` moves
    the label and the .npy, and the vector, the sample count and the enrolment history
    all stay put. Re-enrolling would need the original recording back and would restart
    the sample count at one.

    It calls MT.cmd_rename rather than reimplementing any of it — every guard that
    command has (a name that would collide with another print, a missing vector, an
    index write that fails and rolls the file move back) applies here unchanged. Its
    output is written to stderr, so it is captured and handed back verbatim: mt already
    explains itself better than a paraphrase would.

    `sessions` is a SECOND, separate call on purpose. Renaming the print changes a label;
    rewriting sessions rewrites transcripts the user quotes to customers, and mt deliberately
    shows that blast radius before touching it. The page does the same.
    """
    import contextlib  # noqa: PLC0415 — only this handler needs it
    import io          # noqa: PLC0415
    b = body if isinstance(body, dict) else {}
    old, new = _person_name(b.get("old")), _person_name(b.get("new"))
    if not old or not new:
        return {"ok": False, "why": "a rename needs the current name and the new one"}
    if old == new:
        return {"ok": False, "why": "those are the same name"}
    do_sessions = b.get("sessions") is True
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            MT.cmd_rename(SimpleNS(old=old, new=new, sessions=do_sessions, retired=False))
    except SystemExit as e:
        return {"ok": False, "why": str(e)[:400], "detail": _plain(buf.getvalue())}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"[:400],
                "detail": _plain(buf.getvalue())}
    return {"ok": True, "old": old, "new": new, "sessions_rewritten": do_sessions,
            "pending_sessions": [] if do_sessions else _sessions_naming(old, new),
            "detail": _plain(buf.getvalue()), "why": None}


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> list[str]:
    """mt's stderr, stripped of colour and shown to the user as it was written."""
    return [ln.rstrip() for ln in _ANSI.sub("", s or "").splitlines() if ln.strip()][:40]


def _sessions_naming(old: str, new: str) -> list[str]:
    """Which session transcripts still carry the OLD name. READ-ONLY — it opens
    speakers.json and nothing else, and only decides whether to offer a button.

    The subtraction matters: `mt rename` builds the same set and takes the new spelling
    out of it, so a rename that only changes capitalisation has nothing to rewrite. Left
    in, the page would offer a "rewrite them too" button that mt then declines to act on.
    """
    want = (old or "").strip().lower()
    if not want or want == (new or "").strip().lower():
        return []
    out = []
    try:
        dirs = MT.session_dirs()
    except (Exception, SystemExit):
        return []
    for d in dirs:
        try:
            doc = json.loads((d / "speakers.json").read_text())
        except Exception:
            continue
        if any(str((v or {}).get("name", "")).strip().lower() == want
               for v in (doc.get("map") or {}).values()):
            try:
                out.append(str(d.relative_to(MT.WORKSPACE)))
            except ValueError:
                out.append(d.name)
    return out[:60]


def set_archived(p: dict) -> dict:
    """Archive or restore a session.

    A marker file, not a move: the session stays exactly where it is, so every path
    already written into the DB, a summary, or a Jira comment keeps resolving. Archiving
    only decides which list it appears in, and is reversible from the archive page.
    """
    sid = p.get("session")
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None:
        return {"ok": False, "why": "no such session"}
    f = d / "archived.json"
    if p.get("archived") is False:
        f.unlink(missing_ok=True)
        return {"ok": True, "archived": False}
    try:
        rel = str(d.relative_to(MT.WORKSPACE))
    except ValueError:
        rel = str(d)
    f.write_text(json.dumps({
        "archived_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session": d.name, "path": rel,
        "summarized": (d / "summary.md").exists(),
    }, indent=2) + "\n")
    freed = 0
    if MT.config().get("trim_on_archive", True) and not _is_busy(d, _live_names()):
        try:
            freed = _storage().trim(d)
        except Exception:
            freed = 0                       # archiving never fails over housekeeping
    return {"ok": True, "archived": True, "freed": freed}


# ── storage: what each meeting takes, and giving it back ─────────────────────
# storage.py owns the files; this part only decides what is busy and which sessions a
# request names. Lazy import, same posture as search and contacts: a broken storage.py
# must never stop the server from starting a recording.

def _storage():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import storage as S
    return S


def _audio_removed(d: Path) -> dict | None:
    try:
        return json.loads((d / "audio_removed.json").read_text())
    except (OSError, ValueError):
        return None


def _live_names() -> set[str]:
    """Sessions being written right now: the recording, and every pass in flight."""
    out = set()
    try:
        r = running()
        if r.get("session"):
            out.add(r["session"])
        if (r.get("stuck_recorder") or {}).get("session"):
            out.add(r["stuck_recorder"]["session"])
    except Exception:
        live = MT.active_session()           # can't tell — assume the live one is busy
        if live is not None:
            out.add(live.name)
    try:
        out |= {p["session"] for p in processing() if p.get("session")}
    except Exception:
        pass
    return out


def _is_busy(d: Path, live: set[str]) -> bool:
    if d.name in live:
        return True
    st = reprocess_status(d)
    if st and st.get("running"):
        return True
    return (_cue_job(d.name) or {}).get("state") == "running"


def _storage_rows() -> tuple[list[dict], dict[str, Path]]:
    S = _storage()
    tracked = tracked_counts()
    live = _live_names()
    rows, dirs = [], {}
    for d in existing_session_dirs():
        s = session_summary(d, tracked)
        if not s:
            continue
        s["busy"] = _is_busy(d, live)
        s["bytes"] = S.usage(d)
        rows.append(s)
        dirs[s["id"]] = d
    rows.sort(key=lambda s: s.get("sort_key") or s.get("id") or "", reverse=True)
    return rows, dirs


def storage_report() -> dict:
    """GET /api/storage — sizes, suggestions and the trash, in one read."""
    S = _storage()
    cfg = MT.config()
    try:
        S.purge_old(float(cfg.get("trash_days", 7)))
    except Exception:
        pass
    rows, _ = _storage_rows()
    keep_days = float(cfg.get("audio_keep_days", 60))
    tot = {k: sum(r["bytes"][k] for r in rows)
           for k in ("rebuildable", "audio", "text", "other", "total")}
    tests, old = [], []
    for r in rows:
        why = S.looks_like_test(r)
        if why:
            tests.append({**_slim(r), "why": why})
        elif S.old_audio(r, keep_days):
            old.append(_slim(r))
    trash = S.trash_list()
    try:
        du = shutil.disk_usage(MT.WORKSPACE)
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = None
    # Only copies trim() would really free: a WAV with no FLAC to rebuild from is kept.
    free_now = [r for r in rows if r["bytes"]["trimmable"] and not r["busy"]]
    return {"ok": True, "meetings": len(rows), "totals": tot, "disk": disk,
            "sizes": {r["id"]: {"total": r["bytes"]["total"], "audio": r["bytes"]["audio"],
                                "audio_removed": bool(r["bytes"]["audio_removed"])}
                      for r in rows},
            "rebuildable": {"bytes": sum(r["bytes"]["trimmable"] for r in free_now),
                            "meetings": len(free_now)},
            "tests": tests, "old_audio": old, "audio_keep_days": keep_days,
            "trash": trash, "trash_bytes": sum(t.get("bytes") or 0 for t in trash),
            "trash_days": cfg.get("trash_days", 7)}


def _slim(r: dict) -> dict:
    out = {k: r.get(k) for k in ("id", "title", "day", "duration", "customer",
                                 "archived", "summarized", "needs_review")}
    out.update(bytes=r["bytes"]["total"], audio=r["bytes"]["audio"])
    return out


def storage_plan(sid: str) -> dict:
    """GET /api/storage/plan?id= — what each action would do to one meeting."""
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None:
        return {"ok": False, "why": "no such session"}
    s = session_summary(d, tracked_counts())
    return {"ok": True, "id": sid, "title": s.get("title") or sid,
            "trash_days": MT.config().get("trash_days", 7),
            **_storage().plan(d, s, _is_busy(d, _live_names()))}


STORAGE_ACTIONS = ("trim", "remove_audio", "delete")
STORAGE_MAX = 500          # one request, one page's worth of sessions at most


def storage_apply(body: dict) -> dict:
    """POST /api/storage/apply {action, sessions: [...] | all: true}

    Every session is re-checked here against the same plan() the page showed, so a stale
    page can't delete a meeting that has since gained action items or started a pass.
    """
    S = _storage()
    b = body if isinstance(body, dict) else {}
    action = b.get("action")
    if action not in STORAGE_ACTIONS:
        return {"ok": False, "why": f"unknown action {action!r}"}
    rows, dirs = _storage_rows()
    if b.get("all") is True:
        if action != "trim":
            return {"ok": False, "why": "only freeing working files applies to every meeting"}
        ids = list(dirs)
    else:
        ids = b.get("sessions") or ([b["session"]] if b.get("session") else [])
        if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
            return {"ok": False, "why": "sessions must be a list of ids"}
        ids = ids[:STORAGE_MAX]
    by_id = {r["id"]: r for r in rows}
    done, refused, freed, trashed = [], [], 0, 0
    for sid in ids:
        d, r = dirs.get(sid), by_id.get(sid)
        if d is None:
            refused.append({"id": sid, "why": "no such session"})
            continue
        pl = S.plan(d, r, r["busy"])[action]
        if not pl["ok"]:
            if not (b.get("all") and pl["why"] == "nothing to free"):
                refused.append({"id": sid, "why": pl["why"]})
            continue
        try:
            if action == "trim":
                freed += S.trim(d)
                done.append(sid)
                continue
            fn = S.remove_audio if action == "remove_audio" else S.delete_session
            res = fn(d, r.get("title") or sid)
        except Exception as e:
            refused.append({"id": sid, "why": f"{type(e).__name__}: {e}"[:200]})
            continue
        if res.get("ok"):
            done.append(sid)
            freed += res.get("freed") or 0
            trashed += res.get("trashed") or 0
        else:
            refused.append({"id": sid, "why": res.get("why")})
    if action == "delete" and done:
        _reindex_later()
    return {"ok": bool(done) or not refused, "done": done, "refused": refused,
            "freed": freed, "trashed": trashed}


def storage_trash(body: dict) -> dict:
    """POST /api/storage/trash {op: restore|empty, entry?}"""
    S = _storage()
    b = body if isinstance(body, dict) else {}
    op, eid = b.get("op"), b.get("entry")
    if op == "restore":
        r = S.restore(str(eid or ""))
        if r.get("ok"):
            _reindex_later(r.get("path"))
        return r
    if op == "empty":
        return S.empty(str(eid) if eid else None)
    return {"ok": False, "why": f"unknown op {op!r}"}


def _reindex_later(session_path: str | None = None) -> None:
    """Keep search in step without making the click wait. A full walk drops rows for
    files that are gone; index_session puts a restored meeting back."""
    def go():
        try:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            import search as SEARCH
            if session_path:
                SEARCH.index_session(session_path)
            else:
                SEARCH.index_all()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def confirm_speaker(p: dict) -> dict:
    sid, key = p.get("session"), p.get("key")
    name, status = (p.get("name") or "").strip(), p.get("status") or "confirmed"
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None or not key:
        return {"ok": False, "why": "bad session or key"}
    sp = d / "speakers.json"
    try:
        doc = json.loads(sp.read_text())
    except Exception as e:
        return {"ok": False, "why": f"cannot read speakers.json: {e}"}
    if key not in doc.get("map", {}):
        return {"ok": False, "why": "no such speaker"}
    e = doc["map"][key]
    # A cluster known to hold several voices must not take ONE name: every line you have
    # not personally reassigned would silently be attributed to that person. Measured
    # case: spk4 (self-similarity 0.55) was named Pat Placeholder and 12 of its 18 lines
    # were not theirs. Per-line assignment is the right tool here.
    if status == "confirmed" and name and e.get("mixed"):
        unassigned = sum(1 for t in json.loads((d / "transcript.json").read_text())["timeline"]
                         if t.get("speaker_key") == key and not t.get("override"))
        return {"ok": False, "why":
                f"{key} holds more than one voice (self-similarity "
                f"{e.get('self_similarity')}). Naming the whole cluster \"{name}\" would "
                f"attribute {unassigned} line(s) you have not checked to them. Use "
                f"\"not them?\" on the individual lines instead — then \"Enroll from these\"."}
    if status == "confirmed" and name:
        e.update(name=name, source=f"confirmed by {P.OWNER} (listened)", confidence="verified")
        e.pop("suggestion", None)
    elif status == "multiple":
        e.update(name="Multiple voices (unresolved)",
                 source=f"{P.OWNER} listened: more than one speaker here",
                 confidence="unconfirmed", mixed=True)
    elif status == "notthem":
        # "This was wrong" on a voiceprint/live match. Un-name it here and keep what was
        # rejected, so the page can say whose print reached for someone else.
        wrong, sc = e.get("name"), e.get("score")
        e.update(name="Unidentified voice", confidence="unconfirmed",
                 source=f"{P.OWNER}: not {wrong}", rejected=wrong)
    else:
        e.update(name="Unidentified voice", source=f"{P.OWNER} listened: could not place it",
                 confidence="unconfirmed")
    if status != "confirmed":
        e.pop("live_strong", None)
    sp.write_text(json.dumps(doc, indent=2) + "\n")

    notes = []
    if status == "notthem" and wrong:
        notes.append(f"un-named — not {wrong}")
        if voiceprint_of(wrong) is not None and sc is not None:
            notes.append(f"{wrong}'s voiceprint scored this voice {float(sc):.2f}; if it keeps "
                         f"grabbing other people, retire that print from one of their own "
                         f"meetings")
    elif status == "confirmed" and name and voiceprint_of(name) is not None and key != "mic":
        notes.append("this meeting's sample is averaged into their voiceprint")
    try:
        MT.cmd_relabel(SimpleNS(session=str(d)))
        notes.append("transcript relabelled")
    except SystemExit as ex:
        notes.append(f"relabel failed: {ex}")
    except Exception as ex:
        notes.append(f"relabel failed: {ex}")

    enrolled = False
    if status == "confirmed" and name and key != "mic":
        try:
            MT.cmd_enroll(SimpleNS(session=str(d), speaker=key, name=name))
            enrolled = True
            notes.append(f"{name} enrolled")
        except SystemExit as ex:
            notes.append(f"not enrolled: {ex}")
        except Exception as ex:
            notes.append(f"not enrolled: {ex}")
    return {"ok": True, "enrolled": enrolled, "notes": notes}


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)
    def __getattr__(self, k):
        return None


def handoff(p: dict) -> dict:
    sid = p.get("session")
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None:
        return {"ok": False, "why": "no such session"}
    doc = json.loads((d / "transcript.json").read_text())
    sp = {k: v for k, v in doc.get("speakers", {}).items() if k != "mic"}
    open_ = [k for k, v in sp.items() if v.get("confidence") != "verified"
             and not v.get("mixed")]
    rel = str(d.relative_to(MT.WORKSPACE))
    (d / "handoff.json").write_text(json.dumps({
        "ready_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session": d.name, "path": rel,
        "voices_verified": sum(1 for v in sp.values() if v.get("confidence") == "verified"),
        "voices_open": open_,
    }, indent=2) + "\n")
    return {"ok": True, "open": open_,
            "line": f"{d / 'transcript.md'} is reviewed — summarise it, "
                    f"pull the action items, and track them."}



def reassign_line(p: dict) -> dict:
    """Point one transcript line at a different person.

    Cluster-level naming cannot fix everything: on a real call one cluster held one person
    while its label said another. This writes an `override` onto the individual timeline
    entry, which both renderers prefer over the cluster's name.
    """
    sid = p.get("session")
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None:
        return {"ok": False, "why": "no such session"}
    try:
        start = float(p.get("start"))
    except (TypeError, ValueError):
        return {"ok": False, "why": "bad start time"}
    name = (p.get("name") or "").strip()
    tj = d / "transcript.json"
    doc = json.loads(tj.read_text())
    hit = None
    for e in doc.get("timeline", []):
        if abs(float(e.get("start", -1)) - start) < 0.01 and \
           (not p.get("track") or e.get("track") == p.get("track")):
            hit = e
            break
    if hit is None:
        return {"ok": False, "why": "could not find that line"}
    if name:
        hit["override"] = name
    else:
        hit.pop("override", None)
    tj.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        MT.cmd_relabel(SimpleNS(session=str(d)))
    except Exception as e:
        return {"ok": True, "note": f"saved, but re-render failed: {e}"}
    n = sum(1 for e in doc["timeline"] if e.get("override"))
    return {"ok": True, "overrides": n,
            "note": f"line assigned to {name}" if name else "override cleared"}


def enroll_from_lines(p: dict) -> dict:
    """Enrol someone from exactly the lines the user reassigned to them.

    This is how a mixed cluster gets broken out: name the individual lines, then build the
    voiceprint from only those. Refuses if the selected audio does not agree with itself,
    for the same reason cluster enrolment does.
    """
    import numpy as np
    sid, name = p.get("session"), (p.get("name") or "").strip()
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None or not name:
        return {"ok": False, "why": "need a session and a name"}
    doc = json.loads((d / "transcript.json").read_text())
    spans = [(e["start"], e["end"]) for e in doc.get("timeline", [])
             if e.get("override") == name and e.get("track") != "mic"]
    total = sum(b - a for a, b in spans)
    if total < 4.0:
        return {"ok": False, "why": f"only {total:.1f}s assigned to {name} — "
                                    f"need at least 4s to build a usable voiceprint"}
    audio = next((d / f"system{e}" for e in (".flac", ".wav")
                  if (d / f"system{e}").exists()), None)
    if audio is None:
        return {"ok": False, "why": "no system audio"}
    w16 = d / "work" / "lines.16k.wav"
    try:
        MT.to_16k_mono(audio, w16, normalize=False)
        x = MT.read_wav16k(w16)
        sim = MT.spans_self_similarity(x, spans)
        if sim is not None and sim < float(MT.config()["min_self_similarity"]):
            return {"ok": False, "why": f"those lines only agree with themselves at "
                                        f"{sim:.2f} — they are not all one voice"}
        chunks = []
        for a, b in spans:
            pc = x[int(a * 16000):int(b * 16000)]
            if pc.size >= int(0.35 * 16000):
                chunks.append(pc)
        if not chunks:
            return {"ok": False, "why": "segments too short to embed"}
        vec = MT.embed_audio(np.concatenate(chunks))
        if vec is None:
            return {"ok": False, "why": "could not build an embedding"}
        rec = MT.save_voiceprint(name, vec, source=f"{d}#lines")
    finally:
        w16.unlink(missing_ok=True)
    return {"ok": True, "name": name, "samples": rec.get("samples"),
            "seconds": round(total, 1), "selfsim": None if sim is None else round(sim, 3)}



def reassign_rest(p: dict) -> dict:
    """Give every not-yet-reassigned line in a cluster the same owner.

    This is what a mixed cluster needs after you have picked out the odd lines: the
    remainder is usually one person, and saying so explicitly is honest where naming the
    whole cluster was not. Every line ends up with an owner, so the cluster's own label
    stops mattering.
    """
    sid, key = p.get("session"), p.get("key")
    name = (p.get("name") or "").strip()
    d = next((x for x in existing_session_dirs() if x.name == sid), None)
    if d is None or not key or not name:
        return {"ok": False, "why": "need a session, a cluster and a name"}
    tj = d / "transcript.json"
    doc = json.loads(tj.read_text())
    n = 0
    for e in doc.get("timeline", []):
        if e.get("speaker_key") == key and not e.get("override"):
            e["override"] = name
            n += 1
    if not n:
        return {"ok": False, "why": "every line in that group already has an owner"}
    tj.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        MT.cmd_relabel(SimpleNS(session=str(d)))
    except Exception as e:
        return {"ok": True, "assigned": n, "note": f"saved, re-render failed: {e}"}
    return {"ok": True, "assigned": n,
            "note": f"{n} remaining line(s) assigned to {name}"}


# ------------------------------------------------- voiceprints (global, not per-meeting)
#
# Everything above this line edits ONE meeting. Everything below edits the voiceprint
# store, which every future meeting is matched against. The review page keeps the two
# apart on purpose: the user asked to un-name a speaker and got a global retirement in
# their head, which is exactly the confusion that made this endpoint necessary.
#
# Retiring never unlinks a file — mt.cmd_forget moves the vector into voiceprints/retired/
# and indexes it there. This whole tree syncs to Drive, so a delete is permanent twice.

VP_NAME_MAX = 200
VP_REASON_MAX = 500

# Both indexes are read-modify-write JSON files and this is a ThreadingHTTPServer, so two
# requests in flight at once is not theoretical: four concurrent /api/forget calls for one
# name all passed the "is it enrolled?" check, all reported success, and the last writer's
# copy of retired/index.json won — leaving the vector orphaned in retired/ with nothing
# pointing at it. Check and act under one lock.
VP_LOCK = threading.Lock()


def _vp_text(v, limit: int) -> str | None:
    """A field off a POST body is a short string or it is nothing.

    A list, a dict, a number, a 300-character blob and an empty string all come back
    None and the caller answers with HTTP 200 and a sentence. Nothing unparsed reaches
    mt.py — and since mt only ever touches paths it read out of its own index, a name
    like "../../etc/passwd" can at worst match nothing.
    """
    if not isinstance(v, str):
        return None
    # Control characters are never part of a person's name, and mt's slugify() turns them
    # into separators — "Pat Placeholder\x00" normalised straight onto the real Pat
    # Placeholder and retired them. A name that cannot be shown back to you verbatim is
    # refused.
    if any(ch < " " or ch == "\x7f" for ch in v):
        return None
    s = v.strip()
    return s[:limit] if s else None


def _vp_why(ex) -> str:
    """mt's refusals are deliberately multi-line for a terminal. The page shows this in a
    one-line status strip, so flatten the indentation but keep the sentence."""
    s = re.sub(r"\s+", " ", str(ex)).strip()
    return (s[:597] + "…") if len(s) > 600 else (s or "refused")


def live_prints() -> list[dict]:
    """The enrolled index only — no .npy loads. This is the same list mt.cmd_forget
    works from, including any entry whose vector has gone missing underneath it."""
    try:
        return MT.read_index(MT.VOICEPRINTS / "index.json")["speakers"]
    except (Exception, SystemExit):
        return []


def retired_prints() -> list[dict]:
    try:
        return MT.read_index(MT.RETIRED / "index.json")["speakers"]
    except (Exception, SystemExit):
        return []


def _vp_card(r: dict, retired: bool = False) -> dict:
    return {"name": str(r.get("name") or r.get("slug") or "?"),
            "slug": str(r.get("slug") or ""),
            "samples": r.get("samples"),
            "updated": r.get("updated"),
            "retired_at": r.get("retired_at") if retired else None,
            "reason": r.get("reason") if retired else None}


def voiceprint_of(name: str) -> dict | None:
    """The live record for a display name, or None. Slug-aware, via mt's own matcher."""
    if not name:
        return None
    try:
        return MT.find_print(live_prints(), name)
    except (Exception, SystemExit):
        return None


def retired_of(name: str) -> dict | None:
    if not name:
        return None
    try:
        hits = [r for r in retired_prints() if MT.find_print([r], name)]
    except (Exception, SystemExit):
        return None
    return hits[-1] if hits else None          # most recently retired wins, as mt does


def forget_voiceprint(p: dict) -> dict:
    """POST /api/forget — retire a voiceprint. GLOBAL: every future meeting.

    Deliberately does NOT touch the current session's speakers.json. Un-naming a speaker
    here and retiring the print are different blast radii, and the page makes the caller
    ask for each one explicitly rather than inferring the big one from the small one.
    """
    if not isinstance(p, dict):
        p = {}
    name = _vp_text(p.get("name"), VP_NAME_MAX)
    reason = _vp_text(p.get("reason"), VP_REASON_MAX)
    if not name:
        return {"ok": False, "why": "need the name of an enrolled voiceprint"}
    with VP_LOCK:
        if voiceprint_of(name) is None:
            r = retired_of(name)
            if r is not None:
                return {"ok": False, "why": f"{r.get('name') or name} is already retired "
                                            f"({str(r.get('retired_at') or '')[:10]})"}
            return {"ok": False, "why": f"no enrolled voiceprint called “{name}”"}
        try:
            MT.cmd_forget(SimpleNS(name=name, reason=reason))
        except SystemExit as ex:
            return {"ok": False, "why": _vp_why(ex)}
        except Exception as ex:
            return {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:300]}
        rec = retired_of(name)
    if rec is None:                            # should not happen; do not claim success
        return {"ok": False, "why": "mt reported no error but the print is not in "
                                    "voiceprints/retired — check the store by hand"}
    return {"ok": True, "retired": _vp_card(rec, retired=True),
            "note": f"{rec.get('name')} retired — no longer matched in any future "
                    f"meeting. Reversible from this page."}


def restore_voiceprint(p: dict) -> dict:
    """POST /api/restore — put a retired voiceprint back. The exact inverse."""
    if not isinstance(p, dict):
        p = {}
    name = _vp_text(p.get("name"), VP_NAME_MAX)
    if not name:
        return {"ok": False, "why": "need the name of a retired voiceprint"}
    with VP_LOCK:
        if retired_of(name) is None:
            return {"ok": False, "why": f"no retired voiceprint called “{name}”"}
        try:
            MT.cmd_restore(SimpleNS(name=name))
        except SystemExit as ex:
            return {"ok": False, "why": _vp_why(ex)}
        except Exception as ex:
            return {"ok": False, "why": f"{type(ex).__name__}: {ex}"[:300]}
        rec = voiceprint_of(name)
    if rec is None:
        return {"ok": False, "why": "mt reported no error but the print is not back in "
                                    "the live index — check the store by hand"}
    return {"ok": True, "print": _vp_card(rec),
            "note": f"{rec.get('name')} restored — matched again from the next meeting on."}


# ---------------------------------------------------------------- tracking
#
# action_items + projects, over the same canonical DB the contacts page
# writes. Same posture: tracking.py is imported lazily so a broken module cannot stop
# `mt web` serving, reads never migrate, and every failure comes back as HTTP 200 with
# a sentence rather than a 500.

# "All" on the page asks for this many. It was 1000 — which would have silently cut the
# list off once action_items passed 1000 rows (753 today). The page always shows the true
# total next to what it drew, so truncation can never be invisible either way.
TRACK_LIMIT = 2000

_tracking_schema_done = False


def _tracking():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))   # tracking.py sits next to web.py
    import tracking as TR
    return TR


def _tracking_ensure_once(TR) -> None:
    """Apply the added columns at most once per process, and only on a write path.
    Browsing the tracking page must not be able to alter the 68 MB canonical DB."""
    global _tracking_schema_done
    if _tracking_schema_done:
        return
    TR.ensure_schema()
    _tracking_schema_done = True


def _track_limit(v) -> int:
    try:
        n = int(str(v).strip() or TRACK_LIMIT)
    except ValueError:
        n = TRACK_LIMIT
    return max(1, min(n, TRACK_LIMIT))


def tracking_items(q: str = "", customer: str = "", status: str = "",
                   priority: str = "", scope: str = "", owner: str = "",
                   limit="", hide_done="", offset="", ids="", project="", mine="",
                   due="", sort="", direction="", dep="") -> dict:
    """GET /api/tracking/items — one page of the filtered list, the total it came from,
    and the header counts. Paged here rather than in the browser because a row is ~2 KB:
    all 753 is 1.5 MB, re-sent on every filter change. `ids=1` adds every matching id,
    for "select all matching" without shipping every description to get them."""
    try:
        off = max(0, int(str(offset).strip() or 0))
    except ValueError:
        off = 0
    try:
        TR = _tracking()
        r = TR.page_items(
            q=(q or "").strip(), customer=(customer or "").strip() or None,
            status=(status or "").strip() or None,
            priority=(priority or "").strip() or None,
            scope=(scope or "").strip() or None,
            owner=(owner or "").strip() or None,
            limit=_track_limit(limit), offset=off,
            hide_done=str(hide_done).strip() in ("1", "true", "yes"),
            with_ids=str(ids).strip() in ("1", "true", "yes"),
            project=(project or "").strip() or None,
            mine=str(mine).strip() in ("1", "true", "yes"),
            due=(due or "").strip() or None,
            sort=(sort or "").strip() or TR.SORT_DEFAULT,
            direction=(direction or "").strip() or None,
            dep=(dep or "").strip() or None)
        rows = _safe(list(r.get("items") or []))
        counts = _safe(dict(TR.stats() or {}))
    except Exception as e:
        return {"items": [], "total": 0, "offset": 0, "stats": {},
                "error": f"tracking unavailable — {_cwhy(e)}"}
    out = {"items": rows, "total": r.get("total", len(rows)), "offset": off,
           "stats": counts, "sort": r.get("sort"), "dir": r.get("dir"),
           "steps": r.get("steps", False), "error": None}
    if r.get("ids") is not None:
        out["ids"] = r["ids"]
    return out


def tracking_item(iid="") -> dict:
    """GET /api/tracking/item?id="""
    n = _cid(iid)
    if n is None:
        return {"item": None, "error": "no such item"}
    try:
        row = _tracking().get_item(n)
    except Exception as e:
        return {"item": None, "error": _cwhy(e)}
    if row is None:
        return {"item": None, "error": "no such item"}
    return {"item": _safe(row), "error": None}


def tracking_meta() -> dict:
    """GET /api/tracking/meta — everything the pickers need, in one round trip, so the
    page never hardcodes a vocabulary. If the two ever disagree, this one wins."""
    try:
        TR = _tracking()
        return {"vocab": _safe(TR.vocab()),
                "customers": _safe(TR.customers()),
                "owners": _safe(TR.owner_groups()),
                "projects": _safe(TR.list_projects()),
                "no_project": _safe(TR.no_project_rollup()),
                "title_drafts": TR.title_draft_count(),
                "error": None}
    except Exception as e:
        return {"vocab": {}, "customers": [], "owners": {}, "projects": [],
                "no_project": {}, "error": f"tracking unavailable — {_cwhy(e)}"}


def tracking_bulk(body: dict) -> dict:
    """POST /api/tracking/bulk — set the same controlled field on many rows.

    This is the only endpoint in the tool that rewrites rows in bulk against the
    canonical DB, so it stays narrow: three fields, canonical values only, capped in
    tracking.py, and it reports exactly which ids moved rather than just "ok"."""
    b = body if isinstance(body, dict) else {}
    ids = b.get("ids")
    fields = b.get("fields")
    if not isinstance(ids, list) or not isinstance(fields, dict):
        return {"ok": False, "changed": [], "skipped": [], "why": "nothing selected"}
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        r = TR.bulk_update(ids, fields, by="mt")
    except Exception as e:
        return {"ok": False, "changed": [], "skipped": [], "why": _cwhy(e)}
    return {"ok": True, "changed": r["changed"], "skipped": r["skipped"],
            "carried": r.get("carried") or [], "fields": r["fields"], "why": None}


def tracking_projects(archived="") -> dict:
    """GET /api/tracking/projects?archived=1"""
    want = str(archived).strip() in ("1", "true", "yes")
    try:
        rows = _safe(list(_tracking().list_projects(archived=want) or []))
    except Exception as e:
        return {"projects": [], "error": f"tracking unavailable — {_cwhy(e)}"}
    return {"projects": rows, "error": None}


def tracking_project_save(body: dict) -> dict:
    """POST /api/tracking/project/save — create when there is no id, else partial update."""
    b = body if isinstance(body, dict) else {}
    fields = b.get("fields")
    if not isinstance(fields, dict):
        return {"ok": False, "project": None, "why": "nothing to save"}
    raw = b.get("id")
    pid = _cid(raw)
    if pid is None and raw not in (None, "", 0):
        return {"ok": False, "project": None, "why": "no such project"}
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        row = (TR.create_project(fields, by="mt") if pid is None
               else TR.update_project(pid, fields, by="mt"))
    except Exception as e:
        return {"ok": False, "project": None, "why": _cwhy(e)}
    return {"ok": True, "project": _safe(row), "why": None}


def tracking_project_archive(body: dict) -> dict:
    """POST /api/tracking/project/archive — hide or restore. Never deletes: action items
    carry a project_id and the items themselves are still real work."""
    b = body if isinstance(body, dict) else {}
    pid = _cid(b.get("id"))
    if pid is None:
        return {"ok": False, "project": None, "why": "no such project"}
    want = b.get("archived") is not False
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        row = TR.set_project_archived(pid, want, by="mt")
    except Exception as e:
        return {"ok": False, "project": None, "why": _cwhy(e)}
    return {"ok": True, "project": _safe(row), "why": None}


def tracking_save(body: dict) -> dict:
    """POST /api/tracking/save — create when there is no id, partial update when there
    is. A refusal from tracking.py ("status must be one of: ...") is the feature, not an
    error, so it comes back in `why` for the page to show verbatim."""
    b = body if isinstance(body, dict) else {}
    fields = b.get("fields")
    if not isinstance(fields, dict):
        return {"ok": False, "item": None, "why": "nothing to save"}
    raw_id = b.get("id")
    iid = _cid(raw_id)
    if iid is None and raw_id not in (None, "", 0):
        return {"ok": False, "item": None, "why": "no such item"}
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        row = (TR.create_item(fields, by="mt") if iid is None
               else TR.update_item(iid, fields, by="mt", base_rev=b.get("rev")))
    except Exception as e:
        # R4.2's optimistic check: the row moved since the page loaded it. Not an error
        # in the 500 sense — the page shows what changed and offers keep mine / theirs.
        if type(e).__name__ == "Conflict":
            return {"ok": False, "item": None, "why": _cwhy(e),
                    "conflict": {"rev": e.rev, "changes": _safe(e.changes),
                                 "item": _safe(TR.get_item(e.iid))}}
        return {"ok": False, "item": None, "why": _cwhy(e)}
    return {"ok": True, "item": _safe(row), "why": None}


def tracking_promote_preview(iid="", name="") -> dict:
    """GET /api/tracking/promote/preview?id=&name= — what promoting this item would do.
    Read-only in effect (it opens the DB for writing only to share promote's checks)."""
    n = _cid(iid)
    if n is None:
        return {"ok": False, "why": "no such item"}
    try:
        return dict(_safe(_tracking().promote_preview(n, name or "")), ok=True, why=None)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_promote(body: dict) -> dict:
    """POST /api/tracking/promote {id, sha, name, type} — applies only the previewed plan."""
    b = body if isinstance(body, dict) else {}
    n = _cid(b.get("id"))
    if n is None:
        return {"ok": False, "why": "no such item"}
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        p = TR.promote_item(n, str(b.get("sha") or ""), name=str(b.get("name") or ""),
                            ptype=str(b.get("type") or "other"), by="mt")
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}
    return {"ok": True, "project": _safe(p), "why": None}


def tracking_demote_preview(pid="") -> dict:
    """GET /api/tracking/demote/preview?id="""
    n = _cid(pid)
    if n is None:
        return {"ok": False, "why": "no such project"}
    try:
        return dict(_safe(_tracking().demote_preview(n)), ok=True, why=None)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_demote(body: dict) -> dict:
    """POST /api/tracking/demote {id, sha, choices: {item id: "flatten" | project id}}"""
    b = body if isinstance(body, dict) else {}
    n = _cid(b.get("id"))
    if n is None:
        return {"ok": False, "why": "no such project"}
    ch = b.get("choices") if isinstance(b.get("choices"), dict) else {}
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        it = TR.demote_project(n, str(b.get("sha") or ""), ch, by="mt")
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}
    return {"ok": True, "item": _safe(it), "why": None}


def tracking_gantt(project="", customer="", hide_done="1") -> dict:
    """GET /api/tracking/gantt?project=<id|none>&customer=<short>&hide_done=0|1 — the
    Gantt window's slim projection (R4.4). Read-only."""
    try:
        return _safe(_tracking().gantt(
            project=(project or "").strip() or None,
            customer=(customer or "").strip() or None,
            hide_done=str(hide_done).strip() not in ("0", "false", "no")))
    except Exception as e:
        return {"rows": [], "links": [], "groups": [], "range": None,
                "error": f"tracking unavailable — {_cwhy(e)}"}


def tracking_changes(since="") -> dict:
    """GET /api/tracking/changes?since=<item_events id> — what moved since then (R4.4b).
    Cheap on purpose: a visible page asks every 15 s."""
    try:
        n = int(str(since).strip()) if str(since).strip() else -1
    except ValueError:
        n = -1
    try:
        return _safe(_tracking().changes(n))
    except Exception as e:
        return {"last": 0, "ids": [], "more": False, "sig": "", "ready": False,
                "error": _cwhy(e)}


def tracking_children(iid="") -> dict:
    """GET /api/tracking/children?id= — one item's sub-items (the sheet's quick add)."""
    n = _cid(iid)
    if n is None:
        return {"items": [], "error": "no such item"}
    try:
        return {"items": _safe(_tracking().children_of(n)), "error": None}
    except Exception as e:
        return {"items": [], "error": _cwhy(e)}


def tracking_titles(limit="", offset="", decided="") -> dict:
    """GET /api/tracking/titles — drafted titles waiting for a decision (R4.4c)."""
    try:
        return _safe(_tracking().title_drafts(
            limit=int(str(limit).strip() or 25), offset=int(str(offset).strip() or 0),
            decided=str(decided).strip() in ("1", "true", "yes")))
    except Exception as e:
        return {"drafts": [], "total": 0, "done": 0, "ready": False, "error": _cwhy(e)}


def tracking_title_decide(body: dict) -> dict:
    """POST /api/tracking/title/decide {id: draft id, decision: accept|edit|skip, final}"""
    try:
        TR = _tracking()
        r = TR.decide_title(int(body.get("id")), str(body.get("decision") or ""),
                            str(body.get("final") or ""), by="mt")
        return dict(_safe(r), why=None)
    except (ValueError, TypeError) as e:
        return {"ok": False, "why": str(e)}
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_sequence(iid="") -> dict:
    """GET /api/tracking/sequence?id= — one item's predecessors and successors."""
    n = _cid(iid)
    if n is None:
        return {"ok": False, "why": "no such item"}
    try:
        return dict(_safe(_tracking().item_sequence(n)), ok=True, why=None)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_dep(body: dict) -> dict:
    """POST /api/tracking/dep — {add: {pred, succ, lag}} | {remove: id} |
    {met: id, on: bool, note}. Links are tombstoned, never deleted."""
    b = body if isinstance(body, dict) else {}
    try:
        TR = _tracking()
        if isinstance(b.get("add"), dict):
            a = b["add"]
            r = TR.add_dep(a.get("pred"), a.get("succ"), by="mt", lag_days=a.get("lag") or 0)
        elif b.get("remove") is not None:
            r = TR.remove_dep(int(b["remove"]), by="mt")
        elif b.get("met") is not None:
            r = TR.set_dep_met(int(b["met"]), bool(b.get("on")), str(b.get("note") or ""),
                               by="mt")
        else:
            return {"ok": False, "why": "nothing to do"}
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}
    return dict(_safe(r), ok=True, why=None)


def tracking_push_preview(iid="") -> dict:
    """GET /api/tracking/push/preview?id=<the predecessor that slipped>"""
    n = _cid(iid)
    if n is None:
        return {"ok": False, "why": "no such item"}
    try:
        return dict(_safe(_tracking().push_preview(n)), ok=True, why=None)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_plan(body: dict) -> dict:
    """POST /api/tracking/plan {changes: [{id, rev, start_date?, end_date?, due_date?}]}
    — the Gantt tray's Save (R4.5), one transaction. {undo: [...]} puts a save back."""
    b = body if isinstance(body, dict) else {}
    try:
        TR = _tracking()
        u = b.get("undo")
        if isinstance(u, dict):                       # R4.5b: {changes, links}
            batch, links = u.get("changes") or [], u.get("links") or []
        elif isinstance(u, list):
            batch, links = u, []
        else:
            batch, links = b.get("changes") or [], b.get("links") or []
        if not isinstance(batch, list) or not isinstance(links, list):
            return {"ok": False, "why": "nothing to save"}
        return dict(_safe(TR.apply_plan(batch, by="mt", links=links)), ok=True, why=None)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_people(body: dict) -> dict:
    """POST /api/tracking/people {item, add: contact id} | {item, remove: item_people id}
    — the "with" people on an item (Phase 7)."""
    b = body if isinstance(body, dict) else {}
    try:
        return _safe(_tracking().set_item_person(b.get("item"), contact_id=b.get("add"),
                                                 remove_pid=b.get("remove"), by="mt"))
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_lease(body: dict) -> dict:
    """POST /api/tracking/lease {holder, ids} — the Gantt's soft lease on items it holds
    unsaved edits for (R4.5b). An empty ids list releases. Never blocks anything."""
    b = body if isinstance(body, dict) else {}
    try:
        return _safe(_tracking().set_leases(str(b.get("holder") or ""),
                                            list(b.get("ids") or []),
                                            str(b.get("label") or "Gantt")))
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_push(body: dict) -> dict:
    """POST /api/tracking/push {id, sha} — applies only the previewed moves, in one
    transaction. POST /api/tracking/push {undo: [...]} puts a batch back."""
    b = body if isinstance(body, dict) else {}
    try:
        TR = _tracking()
        if isinstance(b.get("undo"), list):
            return {"ok": True, "undo": _safe(TR.apply_dates(b["undo"], by="mt")),
                    "why": None}
        n = _cid(b.get("id"))
        if n is None:
            return {"ok": False, "why": "no such item"}
        return dict(_safe(TR.push_dependents(n, str(b.get("sha") or ""), by="mt")),
                    ok=True, why=None)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


# ---------------------------------------------------------------- files
#
# The markdown editor's HTTP surface. docs.py owns every path decision — confinement,
# which files are writable, the version vault — and nothing here second-guesses it.

_docs_mod = None


def _docs():
    global _docs_mod
    if _docs_mod is None:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import docs as D
        _docs_mod = D
    return _docs_mod


def files_list(q: str = "", kind: str = "", customer: str = "",
               root: str = "workspace") -> dict:
    root = "memory" if root == "memory" else "workspace"
    if root == "memory" and P.MEMORY_DIR is None:
        return {"root": root, "files": [], "kinds": [], "off": True, "error": None}
    try:
        D = _docs()
        rows = _safe(list(D.list_files(q=q, kind=kind, customer=customer,
                                       root=root) or []))
        out = {"root": root, "files": rows, "kinds": D.kinds(root), "error": None}
        if root == "memory":
            # For resolving [[wiki-links]] in preview. The full map, not the filtered
            # list: a link has to resolve even when its target is filtered out.
            out["names"] = D.memory_names()
        return out
    except Exception as e:
        return {"root": root, "files": [], "kinds": [],
                "error": f"files unavailable — {_cwhy(e)}"}


def file_read(root: str = "workspace", path: str = "") -> dict:
    try:
        return _safe(_docs().read_file(root or "workspace", path))
    except Exception as e:
        return {"ok": False, "error": _cwhy(e)}


def file_save(body: dict) -> dict:
    b = body if isinstance(body, dict) else {}
    try:
        fields = b.get("fields") if isinstance(b.get("fields"), dict) else None
        return _safe(_docs().write_file(b.get("root") or "workspace",
                                        b.get("path") or "",
                                        b.get("text"),
                                        b.get("mtime"),
                                        body=b.get("body"),
                                        fields=fields))
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def file_versions(root: str = "workspace", path: str = "") -> dict:
    try:
        return {"versions": _safe(list(_docs().versions(root or "workspace", path))),
                "error": None}
    except Exception as e:
        return {"versions": [], "error": _cwhy(e)}


def file_version(root: str = "workspace", path: str = "", stamp: str = "") -> dict:
    try:
        return _safe(_docs().read_version(root or "workspace", path, stamp))
    except Exception as e:
        return {"ok": False, "error": _cwhy(e)}


def file_restore(body: dict) -> dict:
    b = body if isinstance(body, dict) else {}
    try:
        return _safe(_docs().restore_version(b.get("root") or "workspace",
                                             b.get("path") or "",
                                             b.get("stamp") or ""))
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def tracking_links(table: str = "action_items", id_raw="") -> dict:
    """GET /api/tracking/links — what this row points at, plus paths found in its own
    free text that are not linked yet."""
    n = _cid(id_raw)
    if n is None:
        return {"links": [], "suggest": [], "roles": [], "error": "no such row"}
    try:
        TR = _tracking()
        return {"links": _safe(TR.links_for(table, n)),
                "suggest": _safe(TR.suggest_links(table, n)),
                "roles": list(TR.FILE_ROLES), "error": None}
    except Exception as e:
        return {"links": [], "suggest": [], "roles": [], "error": _cwhy(e)}


def tracking_link(body: dict) -> dict:
    """POST /api/tracking/link — add one, or remove one with {"remove": <link id>}."""
    b = body if isinstance(body, dict) else {}
    try:
        TR = _tracking()
        _tracking_ensure_once(TR)
        rid = b.get("remove")
        if rid not in (None, ""):
            n = _cid(rid)
            if n is None:
                return {"ok": False, "why": "no such link"}
            TR.remove_link(n)
            return {"ok": True, "why": None}
        lid = _cid(b.get("id"))
        if lid is None:
            return {"ok": False, "why": "no such row"}
        TR.add_link(b.get("table") or "action_items", lid,
                    b.get("path") or "", b.get("role") or "reference",
                    b.get("root") or "workspace", b.get("notes") or "")
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}
    return {"ok": True, "why": None}


# ---------------------------------------------------------------- weekly updates
#
# R2 phase 5. weekly.py owns parsing, the RAG rewrite, the hand-off and the DB row; these
# are thin wrappers that turn any exception into a readable `why`. Direct posting (Path B,
# phase 6) and Resync go through poster.py — the only module holding secrets or making
# network calls — imported lazily, so a problem there can never break the rest of mt web.

_weekly_mod = None
_poster_mod = None


def _poster():
    global _poster_mod
    if _poster_mod is None:
        _weekly()
        import poster as PO
        _poster_mod = PO
    return _poster_mod


def weekly_post_status() -> dict:
    try:
        return _safe(_poster().status())
    except Exception as e:
        why = f"direct posting unavailable — {_cwhy(e)}"
        return {"jira": {"ok": False, "why": why}, "slack": {"ok": False, "why": why}}


def _weekly():
    global _weekly_mod
    if _weekly_mod is None:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import weekly as WK
        _weekly_mod = WK
    return _weekly_mod


def weekly_board(week: str = "") -> dict:
    try:
        return _safe(_weekly().board(week or None))
    except Exception as e:
        return {"cards": [], "error": f"weekly updates unavailable — {_cwhy(e)}"}


def weekly_one(path: str = "") -> dict:
    try:
        r = _weekly().one(path)
        # Posted, but no Slack ts yet (no Slack token, so Slack is done by hand): hand the
        # page the exact Slack text again, so closing the editor does not lose it.
        row = r.get("row") if isinstance(r, dict) else None
        if row and not row.get("slack_ts") and r.get("bodies"):
            try:
                r["slack_copy"] = {"text": _poster().md_to_slack(r["bodies"]["slack"]),
                                   "channel": row.get("slack_channel"),
                                   "channel_name": _weekly().channel_name(r["customer"])}
            except Exception:
                pass
        if isinstance(r, dict):
            # The Jira link on a posted update; blank when jira_site is not configured.
            r["jira_base"] = f"{P.JIRA_SITE}/browse/" if P.JIRA_SITE else ""
        return _safe(r)
    except Exception as e:
        return {"ok": False, "error": _cwhy(e)}


def weekly_fields() -> dict:
    try:
        return _safe(_weekly().jira_fields())
    except Exception as e:
        return {"health": [], "reasons": [], "error": _cwhy(e)}


def weekly_post(action: str, body: dict) -> dict:
    """POST /api/weekly/<action>. Every refusal comes back in `why`, verbatim."""
    b = body if isinstance(body, dict) else {}
    try:
        WK = _weekly()
        if action == "save":
            r = WK.save(b.get("path") or "", b.get("text"), b.get("mtime"),
                        b.get("rag_status") or "", b.get("rag_reason_code") or "")
        elif action == "preview":
            r = WK.preview(b.get("text") or "", b.get("path") or "")
        elif action == "new":
            r = WK.create_draft(b.get("customer") or "", b.get("week") or None)
        elif action == "handoff":
            r = WK.handoff(b.get("path") or "")
        elif action == "sendback":
            r = WK.send_back(b.get("path") or "", b.get("notes") or "")
        elif action == "record":
            r = WK.record_posted(b.get("path") or "", b.get("jira_comment") or "",
                                 b.get("slack_ts") or "", "mt")
        elif action == "plan":
            r = _poster().plan(b.get("path") or "")
        elif action == "post":
            r = _poster().post(b.get("path") or "", b.get("sha256") or "")
        elif action == "correct-plan":
            r = _poster().correct_plan(b.get("path") or "")
        elif action == "correct":
            r = _poster().correct(b.get("path") or "", b.get("sha256") or "")
        elif action == "resync":
            r = _poster().resync()
        else:
            r = {"ok": False, "why": "no such action"}
        return _safe(r)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


# ---------------------------------------------------------------- hygiene
#
# Phase 3a, the Data area of /hygiene. hygiene.py owns every read and write; this is only
# routing. GETs are pure reads (hygiene.data_groups creates no table). Every refusal is
# HTTP 200 with a `why`, like the rest of the app.

def _hygiene():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import hygiene as HY
    return HY


def _hygiene_mem():
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import hygiene_memory as HM
    return HM


def hygiene_get(what: str, pid: str = "") -> dict:
    try:
        if what.startswith("memory"):
            if P.MEMORY_DIR is None:
                return {"off": True, "findings": [], "proposals": [], "rows": [],
                        "error": None}
            HM = _hygiene_mem()
            if what == "memory":
                return _safe(HM.overview())
            if what == "memory-plan":
                return _safe(HM.plan_public(pid))
            if what == "memory-log":
                return _safe(HM.log())
        HY = _hygiene()
        if what == "data":
            return _safe(HY.data_groups())
        if what == "counts":
            return _safe(HY.counts())
        if what == "log":
            return _safe(HY.log())
        return {"ok": False, "why": "no such view"}
    except Exception as e:
        return {"ok": False, "groups": [], "error": f"hygiene unavailable — {_cwhy(e)}"}


def hygiene_owners() -> dict:
    """GET /api/hygiene/owners — Phase 7b's review queue."""
    try:
        return _safe(_hygiene().owner_queue())
    except Exception as e:
        return {"ready": False, "items": [], "error": _cwhy(e)}


def hygiene_owner_decide(body: dict) -> dict:
    """POST /api/hygiene/owners/decide {key, kind, owner_id, with_ids, note}"""
    b = body if isinstance(body, dict) else {}
    try:
        return _safe(_hygiene().decide_owner(str(b.get("key") or ""), str(b.get("kind") or ""),
                                             b.get("owner_id"), b.get("with_ids") or [],
                                             str(b.get("note") or ""), by="mt"))
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


def hygiene_post(action: str, body: dict) -> dict:
    b = body if isinstance(body, dict) else {}
    try:
        if action.startswith("mem-"):
            HM = _hygiene_mem()
            pid = b.get("id") or ""
            if action == "mem-apply":
                r = HM.apply(pid, b.get("sha") or "", "mt")
            elif action == "mem-undo":
                r = HM.undo(b.get("log_id"), "mt")
            elif action == "mem-edit":
                r = HM.edit(pid, b.get("path") or "", b.get("content"))
            elif action == "mem-sendback":
                r = HM.send_back(pid, b.get("notes") or "")
            elif action == "mem-reject":
                r = HM.reject(pid, b.get("notes") or "")
            else:
                r = {"ok": False, "why": "no such action"}
            return _safe(r)
        HY = _hygiene()
        if action == "apply":
            r = HY.apply(b.get("key") or "", b.get("sha") or "",
                         b.get("exclude") or [], b.get("choice") or "", "mt")
        elif action == "undo":
            r = HY.undo(b.get("log_id"), "mt")
        elif action == "skip":
            r = HY.skip(b.get("key") or "", b.get("sha") or "")
        elif action == "unskip":
            r = HY.unskip(b.get("key") or "")
        elif action == "keep":
            r = HY.keep(b.get("key") or "", "mt")
        else:
            r = {"ok": False, "why": "no such action"}
        return _safe(r)
    except Exception as e:
        return {"ok": False, "why": _cwhy(e)}


# ---------------------------------------------------------------- http

class App(BaseHTTPRequestHandler):
    server_version = "mtweb"

    def log_message(self, *a):
        pass                                     # keep the terminal for the recorder

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _guard(self) -> bool:
        """Refuse requests that a web page, not the user, sent. True = refused.

        Binding to 127.0.0.1 keeps other machines out, but any page open in the browser
        can still send requests here. A form or text/plain POST needs no CORS preflight
        and do_POST parses the body as JSON regardless of Content-Type, so without this a
        page could start a recording. A rebound DNS name (evil.example → 127.0.0.1) could
        even read /api/* responses, because the browser treats it as same-origin.
        - Host must be this server by IP or by localhost, which defeats DNS rebinding.
        - Origin, when sent, must be one of the same two, which stops cross-site POSTs.
        - Sec-Fetch-Site: cross-site is refused, except for a top-level navigation to a
          page (a link to /review pasted in chat). Pages render from the API, which a
          cross-site context still can't call, and /api/* is never a navigation target.
        curl and scripts send a matching Host and no Origin, so they keep working.
        """
        port = self.server.server_address[1]
        ok = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = (self.headers.get("Host") or "").strip().lower()
        origin = (self.headers.get("Origin") or "").lower()
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        why = None
        if host not in ok:
            why = "bad host"
        elif origin and origin not in {f"http://{h}" for h in ok}:
            why = "cross-origin"
        elif (not origin and self.command == "POST"
              and site not in ("", "same-origin", "none")):
            # Browsers send Origin on every POST, so this is only a backstop.
            why = "missing origin"
        elif site == "cross-site" and not (
                self.command == "GET"
                and (self.headers.get("Sec-Fetch-Mode") or "").lower() == "navigate"
                and not urllib.parse.urlparse(self.path).path.startswith("/api/")):
            why = "cross-site"
        if why:
            self._send(403, f"forbidden: {why}".encode(), "text/plain")
            return True
        return False

    def _file(self, p: Path):
        if not p.exists() or not p.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._send(200, p.read_bytes(), ctype)

    def _range_file(self, p: Path, ctype: str):
        """Serve audio with Range support so the browser can seek in one big file."""
        if not p.exists():
            return self._send(404, b"not found", "text/plain")
        size = p.stat().st_size
        rng = self.headers.get("Range")
        if not rng:
            with open(p, "rb") as f:
                data = f.read()
            return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if not m:
            return self._send(416, b"bad range", "text/plain")
        a = int(m.group(1)) if m.group(1) else 0
        b = int(m.group(2)) if m.group(2) else size - 1
        a, b = max(0, a), min(size - 1, b)
        if a > b:
            return self._send(416, b"bad range", "text/plain")
        with open(p, "rb") as f:
            f.seek(a)
            data = f.read(b - a + 1)
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path
        if path == "/":
            return self._file(WEBAPP / "index.html")
        if path.startswith("/static/"):
            # Resolve and confine to webapp/: this server has no auth by design, and any
            # page in the browser can reach localhost, so "/static/../../.." must not walk
            # out of the directory.
            try:
                t = (WEBAPP / path[len("/static/"):]).resolve()
                t.relative_to(WEBAPP.resolve())
            except (ValueError, OSError):
                return self._send(404, b"not found", "text/plain")
            return self._file(t)
        if path == "/api/version":
            return self._json(version_info())
        if path == "/api/state":
            # One endpoint for "what is this machine doing right now". The page already
            # polls it every 2s, so the processing passes need no second poller.
            #
            # `passes` is a LIST and is reported ALONGSIDE `recording`, not instead of it:
            # recording the next meeting while the last one transcribes is the point, and
            # folding a single pass into the top level is what hid the real one on Sep 15.
            st = running()
            st.update(startup_state())
            st["passes"] = processing()
            st["queue"] = queue_state()
            return self._json(st)
        if path == "/api/today":
            return self._json(today_events())
        if path == "/api/today/refresh":
            return self._json(refresh_calendar(interactive=False))
        if path == "/api/mics":
            # `chosen` is MT.pick_mic's own answer, never a re-derivation of
            # mic_priority here: the picker has to show the decision the recorder will
            # actually make, and a second copy of the priority match is exactly how the
            # two would drift apart. A blank `chosen` means nothing in mic_priority is
            # plugged in, so mt falls back to the system default.
            mics = MT.list_mics()
            chosen, why = MT.pick_mic(MT.config(), None)
            name = (next((m["name"] for m in mics if m["uid"] == chosen), chosen) if chosen
                    else next((m["name"] for m in mics if m["default"]), None))
            # Speakers follow the same rule: `speaker_chosen` is MT.pick_speaker's answer.
            # Blank means mt leaves the macOS output alone. `speakers_ok` is false when
            # mtaudio is not built, and the picker is hidden rather than shown empty.
            spk, spk_why = MT.pick_speaker(None)
            outs = MT.audioprefs.speakers()
            cur = next((d["name"] for d in outs if d["default"]), None)
            return self._json({"mics": mics, "chosen": chosen or "",
                               "chosen_name": name or "system default", "why": why,
                               "speakers_ok": MT.audioprefs.available(),
                               "speakers": [{"uid": d["uid"], "name": d["name"],
                                             "default": d["default"]} for d in outs],
                               "speaker_chosen": spk["uid"] if spk else "",
                               "speaker_chosen_name": spk["name"] if spk else (cur or ""),
                               "speaker_why": spk_why,
                               "station": MT.audioprefs.describe(MT.audioprefs.station())})
        if path == "/api/customers":
            return self._json({"customers": customers()})
        if path == "/api/sessions":
            # Newest first by when the meeting actually STARTED. Directory names carry a
            # time for some sessions and not others, so sorting by name put two meetings
            # on the same day in arbitrary order.
            tracked = tracked_counts()
            rows = [s for s in (session_summary(d, tracked)
                                for d in existing_session_dirs()) if s]
            rows.sort(key=lambda s: s.get("sort_key") or s.get("id") or "", reverse=True)
            # The working list is what is still live. ?archived=1 gets the other pile.
            want = (q.get("archived") or ["0"])[0] == "1"
            shown = [r for r in rows if bool(r.get("archived")) == want]
            return self._json({"sessions": shown,
                               "archived_count": sum(1 for r in rows if r.get("archived"))})
        if path == "/review":
            return self._file(WEBAPP / "review.html")
        if path == "/archive":
            return self._file(WEBAPP / "archive.html")
        if path == "/api/summary":
            return self._json(read_summary((q.get("id") or [""])[0]))
        if path == "/api/session":
            return self._json(session_detail((q.get("id") or [""])[0]))
        if path == "/api/cues":
            return self._json(cues_view((q.get("id") or [""])[0]))
        if path == "/api/cues/live":
            return self._json(cues_live((q.get("session") or [""])[0]))
        if path == "/api/awards":
            return self._json(awards_view((q.get("id") or [""])[0]))
        if path == "/room":
            return self._file(WEBAPP / "room.html")
        if path == "/api/search":
            return self._json(run_search((q.get("q") or [""])[0],
                                         (q.get("kind") or [""])[0],
                                         (q.get("customer") or [""])[0],
                                         (q.get("limit") or ["20"])[0]))
        if path == "/help":
            return self._file(WEBAPP / "help.html")
        if path == "/api/help":
            return self._json(cli_reference())
        if path == "/contacts":
            return self._file(WEBAPP / "contacts.html")
        if path == "/tracking":
            return self._file(WEBAPP / "tracking.html")
        if path == "/tracking/gantt":
            return self._file(WEBAPP / "gantt.html")
        if path == "/tracking/owners":
            return self._file(WEBAPP / "owners.html")
        if path == "/api/hygiene/owners":
            return self._json(hygiene_owners())
        if path == "/api/tracking/children":
            return self._json(tracking_children((q.get("id") or [""])[0]))
        if path == "/tracking/titles":
            return self._file(WEBAPP / "titles.html")
        if path == "/api/tracking/titles":
            return self._json(tracking_titles((q.get("limit") or [""])[0],
                                              (q.get("offset") or [""])[0],
                                              (q.get("decided") or [""])[0]))
        if path == "/api/tracking/changes":
            return self._json(tracking_changes((q.get("since") or [""])[0]))
        if path == "/api/tracking/gantt":
            return self._json(tracking_gantt((q.get("project") or [""])[0],
                                             (q.get("customer") or [""])[0],
                                             (q.get("hide_done") or ["1"])[0]))
        if path == "/api/tracking/items":
            return self._json(tracking_items((q.get("q") or [""])[0],
                                             (q.get("customer") or [""])[0],
                                             (q.get("status") or [""])[0],
                                             (q.get("priority") or [""])[0],
                                             (q.get("scope") or [""])[0],
                                             (q.get("owner") or [""])[0],
                                             (q.get("limit") or [""])[0],
                                             (q.get("hide_done") or [""])[0],
                                             (q.get("offset") or [""])[0],
                                             (q.get("ids") or [""])[0],
                                             (q.get("project") or [""])[0],
                                             (q.get("mine") or [""])[0],
                                             (q.get("due") or [""])[0],
                                             (q.get("sort") or [""])[0],
                                             (q.get("dir") or [""])[0],
                                             (q.get("dep") or [""])[0]))
        if path == "/api/tracking/item":
            return self._json(tracking_item((q.get("id") or [""])[0]))
        if path == "/api/tracking/promote/preview":
            return self._json(tracking_promote_preview((q.get("id") or [""])[0],
                                                       (q.get("name") or [""])[0]))
        if path == "/api/tracking/sequence":
            return self._json(tracking_sequence((q.get("id") or [""])[0]))
        if path == "/api/tracking/push/preview":
            return self._json(tracking_push_preview((q.get("id") or [""])[0]))
        if path == "/api/tracking/demote/preview":
            return self._json(tracking_demote_preview((q.get("id") or [""])[0]))
        if path == "/api/tracking/projects":
            return self._json(tracking_projects((q.get("archived") or [""])[0]))
        if path == "/hygiene":
            return self._file(WEBAPP / "hygiene.html")
        if path in ("/api/hygiene/data", "/api/hygiene/counts", "/api/hygiene/log",
                    "/api/hygiene/memory", "/api/hygiene/memory-plan",
                    "/api/hygiene/memory-log"):
            return self._json(hygiene_get(path.rsplit("/", 1)[1],
                                          (q.get("id") or [""])[0]))
        if path == "/weekly":
            return self._file(WEBAPP / "weekly.html")
        if path == "/api/weekly/board":
            return self._json(weekly_board((q.get("week") or [""])[0]))
        if path == "/api/weekly/one":
            return self._json(weekly_one((q.get("path") or [""])[0]))
        if path == "/api/weekly/jira-fields":
            return self._json(weekly_fields())
        if path == "/api/weekly/post-status":
            return self._json(weekly_post_status())
        if path == "/files":
            return self._file(WEBAPP / "files.html")
        if path == "/api/files":
            return self._json(files_list((q.get("q") or [""])[0],
                                         (q.get("kind") or [""])[0],
                                         (q.get("customer") or [""])[0],
                                         (q.get("root") or ["workspace"])[0]))
        if path == "/api/file":
            return self._json(file_read((q.get("root") or [""])[0],
                                        (q.get("path") or [""])[0]))
        if path == "/api/file/versions":
            return self._json(file_versions((q.get("root") or [""])[0],
                                            (q.get("path") or [""])[0]))
        if path == "/api/file/version":
            return self._json(file_version((q.get("root") or [""])[0],
                                           (q.get("path") or [""])[0],
                                           (q.get("stamp") or [""])[0]))
        if path == "/api/tracking/links":
            return self._json(tracking_links((q.get("table") or ["action_items"])[0],
                                             (q.get("id") or [""])[0]))
        if path == "/api/tracking/meta":
            # Read-only: must not migrate the canonical DB just by being asked.
            return self._json(tracking_meta())
        if path == "/api/contacts":
            return self._json(contacts_list((q.get("q") or [""])[0],
                                            (q.get("customer") or [""])[0],
                                            (q.get("status") or [""])[0],
                                            (q.get("limit") or [""])[0],
                                            (q.get("archived") or [""])[0] or "live"))
        if path == "/api/contacts/triage":
            return self._json(contacts_triage())
        if path == "/api/contacts/rename-check":
            # Read-only: it must not be able to migrate the canonical DB just by asking.
            return self._json(contacts_rename_check((q.get("id") or [""])[0],
                                                    (q.get("name") or [""])[0]))
        if path == "/api/contacts/one":
            return self._json(contacts_one((q.get("id") or [""])[0]))
        if path == "/api/contacts/duplicates":
            return self._json(contacts_dupes((q.get("dismissed") or [""])[0]))
        if path == "/api/contacts/resolve":
            # Read-only, and called on every successful enrolment: it must not be able to
            # migrate or write the canonical DB just by being asked a question.
            return self._json(contacts_resolve((q.get("name") or [""])[0],
                                               (q.get("session") or [""])[0]))
        if path == "/api/contacts/unlinked":
            return self._json(contacts_unlinked())
        if path == "/api/voiceprints":
            return self._json({"ok": True,
                               "enrolled": [_vp_card(r) for r in live_prints()],
                               "retired": [_vp_card(r, retired=True)
                                           for r in retired_prints()]})
        if path == "/api/detect":
            return self._json(detect_meeting())
        if path == "/api/storage":
            return self._json(storage_report())
        if path == "/api/storage/plan":
            return self._json(storage_plan((q.get("id") or [""])[0]))
        if path == "/api/audio":
            sid = (q.get("session") or [""])[0]
            track = (q.get("track") or ["system"])[0]
            d = next((x for x in existing_session_dirs() if x.name == sid), None)
            if d is None or track not in ("system", "mic"):
                return self._send(404, b"no such track", "text/plain")
            f = next((d / f"{track}{e}" for e in (".flac", ".wav")
                      if (d / f"{track}{e}").exists()), None)
            if f is None:
                return self._send(404, b"no audio", "text/plain")
            return self._range_file(f, "audio/flac" if f.suffix == ".flac" else "audio/wav")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        # `[]`, `null`, `"x"` and `7` are all valid JSON and none of them is a body.
        # Every handler below does body.get(...), so without this a one-byte POST took
        # the whole request thread out with an AttributeError instead of an answer.
        if not isinstance(body, dict):
            body = {}
        if u.path == "/api/feedback/check":
            return self._json({"hits": FB.check(str(body.get("text") or ""))})
        if u.path == "/api/record/start":
            return self._json(start_recording(body))
        if u.path == "/api/record/stop":
            return self._json(stop_recording())
        if u.path in ("/api/queue/now", "/api/queue/retry", "/api/queue/dismiss"):
            return self._json(queue_action(u.path.rsplit("/", 1)[1], body.get("id")))
        if u.path == "/api/capture/answer":
            return self._json(capture_answer(body))
        if u.path == "/api/capture/recapture":
            return self._json(capture_recapture(body))
        if u.path == "/api/confirm":
            return self._json(confirm_speaker(body))
        if u.path == "/api/reassign":
            return self._json(reassign_line(body))
        if u.path == "/api/reassign-rest":
            return self._json(reassign_rest(body))
        if u.path == "/api/enroll-lines":
            return self._json(enroll_from_lines(body))
        if u.path == "/api/forget":
            return self._json(forget_voiceprint(body))
        if u.path == "/api/restore":
            return self._json(restore_voiceprint(body))
        if u.path == "/api/handoff":
            return self._json(handoff(body))
        if u.path == "/api/reprocess":
            return self._json(start_reprocess(body))
        if u.path == "/api/cues/analyze":
            return self._json(cues_analyze(body))
        if u.path == "/api/cues/clear":
            return self._json(cues_clear(body))
        if u.path == "/api/awards/crown":
            return self._json(awards_crown(body))
        if u.path == "/api/cues/live/name":
            return self._json(cues_live_name(body))
        if u.path == "/api/cues/live/undo":
            return self._json(cues_live_undo(body))
        if u.path == "/api/cues/live/rename":
            return self._json(cues_live_rename(body))
        if u.path == "/api/contacts/save":
            return self._json(contacts_save(body))
        if u.path == "/api/contacts/archive":
            return self._json(contacts_archive(body))
        if u.path == "/api/tracking/save":
            return self._json(tracking_save(body))
        if u.path.startswith("/api/weekly/"):
            act = u.path[len("/api/weekly/"):]
            if act == "jira-fields/resync":
                act = "resync"
            if act in ("save", "preview", "new", "handoff", "sendback", "record",
                       "resync", "plan", "post", "correct-plan", "correct"):
                return self._json(weekly_post(act, body))
        if u.path == "/api/file/save":
            return self._json(file_save(body))
        if u.path == "/api/file/restore":
            return self._json(file_restore(body))
        if u.path == "/api/hygiene/owners/decide":
            return self._json(hygiene_owner_decide(body))
        if u.path.startswith("/api/hygiene/"):
            return self._json(hygiene_post(u.path.rsplit("/", 1)[1], body))
        if u.path == "/api/tracking/link":
            return self._json(tracking_link(body))
        if u.path == "/api/tracking/dep":
            return self._json(tracking_dep(body))
        if u.path == "/api/tracking/title/decide":
            return self._json(tracking_title_decide(body))
        if u.path == "/api/tracking/people":
            return self._json(tracking_people(body))
        if u.path == "/api/tracking/lease":
            return self._json(tracking_lease(body))
        if u.path == "/api/tracking/plan":
            return self._json(tracking_plan(body))
        if u.path == "/api/tracking/push":
            return self._json(tracking_push(body))
        if u.path == "/api/tracking/promote":
            return self._json(tracking_promote(body))
        if u.path == "/api/tracking/demote":
            return self._json(tracking_demote(body))
        if u.path == "/api/tracking/bulk":
            return self._json(tracking_bulk(body))
        if u.path == "/api/tracking/project/save":
            return self._json(tracking_project_save(body))
        if u.path == "/api/tracking/project/archive":
            return self._json(tracking_project_archive(body))
        if u.path == "/api/contacts/status":
            return self._json(contacts_set_status(body))
        if u.path == "/api/contacts/merge":
            return self._json(contacts_merge(body))
        if u.path == "/api/contacts/dismiss":
            return self._json(contacts_not_duplicate(body, True))
        if u.path == "/api/contacts/undismiss":
            return self._json(contacts_not_duplicate(body, False))
        if u.path == "/api/contacts/alias":
            return self._json(contacts_add_alias(body))
        if u.path == "/api/contacts/from-enrollment":
            return self._json(contacts_from_enrollment(body))
        if u.path == "/api/voiceprints/rename":
            return self._json(rename_voiceprint(body))
        if u.path == "/api/archive":
            return self._json(set_archived(body))
        if u.path == "/api/storage/apply":
            return self._json(storage_apply(body))
        if u.path == "/api/storage/trash":
            return self._json(storage_trash(body))
        return self._send(404, b"not found", "text/plain")


def cli_reference() -> dict:
    """Every command and flag, read out of mt's REAL argparse parser.

    Hand-copying the command list into a help page is how a help page starts lying:
    the flag gets added, the page does not, and the page is what gets trusted. This
    walks MT.build_parser() instead, so /help cannot disagree with `mt --help`.

    argparse.SUPPRESS'd flags stay hidden — `--allow-concurrent` is the escape hatch
    that starts a second capture and kills the running one, and it is suppressed on
    purpose.
    """
    p = MT.build_parser()
    subs = next((a for a in p._actions
                 if isinstance(a, argparse._SubParsersAction)), None)
    if subs is None:
        return {"commands": []}
    blurb = {c.dest: (c.help or "") for c in (subs._choices_actions or [])}

    def render(act) -> dict:
        takes = act.nargs != 0 and not isinstance(
            act, (argparse._StoreTrueAction, argparse._StoreFalseAction,
                  argparse._CountAction))
        arg = ""
        if takes:
            arg = " " + (act.metavar or act.dest.upper())
        return {"flags": list(act.option_strings),
                "display": (", ".join(act.option_strings) + arg).strip(),
                "help": act.help or "", "takes_value": bool(takes),
                "default": None if act.default in (None, False) else str(act.default)}

    cmds = []
    for name, sp in subs.choices.items():
        opts, pos = [], []
        for act in sp._actions:
            if isinstance(act, argparse._HelpAction) or act.help == argparse.SUPPRESS:
                continue
            if act.option_strings:
                opts.append(render(act))
            else:
                pos.append({"name": act.metavar or act.dest, "help": act.help or "",
                            "required": act.nargs not in ("?", "*")})
        cmds.append({"name": name, "help": blurb.get(name, ""),
                     "positional": pos, "options": opts})
    cmds.sort(key=lambda c: c["name"])

    # The thresholds come from the live config for the same anti-drift reason. These are
    # the numbers every naming decision in the tool is made against, so the help page
    # showing a stale 0.62 after the config moved would be worse than not showing it.
    cfg = MT.config()
    keys = ("auto_assign_score", "suggest_score", "min_self_similarity",
            "caution_self_similarity", "merge_similarity", "cluster_threshold",
            "min_speaker_seconds")
    return {"commands": cmds,
            "thresholds": {k: cfg.get(k) for k in keys if cfg.get(k) is not None},
            "mic_priority": cfg.get("mic_priority") or [],
            "paths": {"voiceprints": str(MT.VOICEPRINTS), "recordings": str(MT.RECORDINGS)
                      if hasattr(MT, "RECORDINGS") else "", "config": str(HERE / "config.json"),
                      "tool": str(HERE)}}


def serve(port: int = 8765, open_browser: bool = True):
    # Do this BEFORE serving, in the foreground, so gog can prompt on the terminal that
    # launched us. One prompt at startup beats a permanently broken Today panel.
    MT.step("fetching today's calendar")
    r = refresh_calendar(interactive=True)
    if r.get("ok"):
        MT.say(f"{MT.DIM}   {r['count']} event(s) cached{MT.RST}")
    else:
        MT.warn("calendar unavailable — Today will be empty")
        MT.say(f"{MT.DIM}{r.get('why','')}{MT.RST}")

    srv = ThreadingHTTPServer(("127.0.0.1", port), App)
    url = f"http://127.0.0.1:{port}/"
    MT.step(f"mt web on {url}")
    MT.say(f"{MT.DIM}   localhost only — it can start recordings, so it is never exposed{MT.RST}")
    MT.say(f"{MT.DIM}   Ctrl-C to stop the server (a running recording keeps going){MT.RST}")
    if open_browser:
        try:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        MT.say("")
        MT.step("server stopped")
    finally:
        srv.server_close()

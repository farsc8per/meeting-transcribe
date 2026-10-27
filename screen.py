#!/usr/bin/env python3
"""screen.py — read the meeting UI: who is present, how many, and who is talking.

Parsers are grounded in a real capture of Zoom, Teams and Google Meet (Sep 3 2026),
not in guesses. Three signals, in increasing order of value:

  roster   participant-panel entries        -> who was present
  count    "Participants (N)"               -> how many voices to expect
  talking  Zoom's pop-out "Talking: X", and
           the Meet pop-out / spotlight
           tile label                       -> WHO IS SPEAKING, with a timestamp

Only names that match the contacts table are ever attributed. Everything else is kept
in the log for debugging but never put in someone's mouth.
"""
from __future__ import annotations
import json, re, sqlite3, sys
from pathlib import Path
from collections import defaultdict

import paths as P

HERE = Path(__file__).resolve().parent
WORKSPACE = P.WORKSPACE
DB = P.DB

# ---- views that are NOT a live call (observed and confirmed) -------------------
DEAD_TITLES = {"google meet", "microsoft teams", "zoom workplace", "dialpad", "zoom"}
DEAD_PREFIX = ("calendar |", "chat |", "activity |", "meeting join |",
               "you have left", "breakout rooms", "select a window", "menu window")

UI_WORDS = {
    "mute", "unmute", "camera", "chat", "share", "leave", "join", "people", "present",
    "more", "raise", "hand", "record", "recording", "host", "co-host", "meeting",
    "details", "captions", "reactions", "view", "layout", "settings", "search", "home",
    "hub", "activity", "calendar", "teams", "zoom", "meet", "dialpad", "transcript",
    "speaker", "gallery", "participants", "contributors", "organizer", "invite",
    "upgrade", "apps", "notes", "rooms", "react", "controls", "mic", "add", "type",
    "name", "in", "this", "the", "work", "open", "app", "ask", "gemini", "users",
    "talking", "everywhere", "presenter", "options", "optimize", "sound", "video",
    # audio/display hardware shows up in Zoom's device menus
    "microphone", "speakers", "quadcast", "hyperx", "macbook", "air", "dell", "webcam",
    "airpods", "headphones", "display", "built-in", "default", "same", "as", "system",
}
SELF_MARKS = ("(you)", "(me)", "(host, me)", "(me, host)")
ROLE_SUFFIX = re.compile(
    r"\s*\((?:you|me|host,\s*me|me,\s*host|host|guest|organizer|presenter)\)\s*$", re.I)
COUNT_RE = re.compile(r"(?:participants|in this meeting|contributors)\s*\(?(\d+)\)?", re.I)
TALKING_RE = re.compile(r"^\s*talking\s*:\s*(.*)$", re.I)


def view_of(rec: dict) -> str | None:
    """Which live-meeting surface this frame is, or None if it is not a call."""
    title = (rec.get("title") or "").strip()
    low = title.lower()
    app = rec.get("app") or ""
    w, h = rec.get("w", 0), rec.get("h", 0)
    if low in DEAD_TITLES or low.startswith(DEAD_PREFIX):
        return None
    if low == "zoom floating video window":
        return "zoom_pip"
    if low == "zoom meeting":
        return "zoom_meeting"
    if app.startswith("Microsoft Teams"):
        return "teams_meeting"        # dead Teams views already filtered above
    if low.startswith("meet - ") or "meet.google.com/" in low:
        return "meet_pip" if (w < 700 and h < 700) else "meet_call"
    if app == "Dialpad":
        return "dialpad"
    if app == "Zoom":
        return "zoom_meeting"
    return None


def load_contacts() -> dict[str, tuple[str, str]]:
    if not DB.exists():
        return {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute("select name, coalesce(role,'') from contacts "
                           "where name is not null").fetchall()
    finally:
        con.close()
    out = {}
    for n, r in rows:
        n = (n or "").strip()
        if len(n) < 4:
            continue
        out[norm(n)] = (n, r)
        parts = norm(n).split()
        if len(parts) > 2:                      # tolerate a dropped middle name
            out.setdefault(f"{parts[0]} {parts[-1]}", (n, r))
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]+", " ", s.lower())).strip()


def clean_name(raw: str) -> str | None:
    """Turn a panel/tile line into a bare person name, or None."""
    s = raw.strip()
    s = re.sub(r"^[^A-Za-z]+", "", s)
    s = ROLE_SUFFIX.sub("", s)
    s = re.sub(r"\([A-Z]{1,4}\)", "", s).strip()   # "Alex Example (AE)"
    s = s.split("|")[0].strip()
    # Zoom display names are often "AE // Acme"
    s = re.split(r"\s*//\s*", s)[0].strip()
    if not (3 < len(s) <= 44):
        return None
    words = s.split()
    if not (2 <= len(words) <= 4):
        return None
    if any(w.lower().strip(".,") in UI_WORDS for w in words):
        return None
    if not (all(re.match(r"^[A-Z][A-Za-z'’.\-]*$", w) for w in words) or s.isupper()):
        return None
    return s


INITIALS = re.compile(r"^[A-Z]{2,3}$")


def initials_map(invitees: list[str]) -> dict[str, str]:
    """Initials -> name, but ONLY where they are unambiguous among the invitees.

    Teams collapses participants to avatar circles showing initials whenever someone
    shares their screen — measured on a real call, 388 frames of the meeting window
    carried no names at all, just `JJ AO BM GL MB ER AW`. With the invite list in hand
    those become names; without it they are unusable.
    """
    out: dict[str, list[str]] = {}
    for n in invitees:
        parts = [w for w in re.split(r"[\s.]+", n) if w and w[0].isalpha()]
        if len(parts) < 2:
            continue
        key = (parts[0][0] + parts[-1][0]).upper()
        out.setdefault(key, []).append(n)
    return {k: v[0] for k, v in out.items() if len(v) == 1}


def build(jsonl: Path, invitees: list[str] | None = None) -> dict:
    known = load_contacts()
    inits = initials_map(invitees or [])
    from_initials: dict[str, int] = {}
    people: dict[str, dict] = defaultdict(
        lambda: {"frames": 0, "role": "", "match": "unknown", "is_self": False,
                 "first": None, "last": None, "views": set(), "raw": set()})
    talking: list[dict] = []
    counts: list[dict] = []
    views: dict[str, int] = defaultdict(int)
    skipped = 0

    for ln in jsonl.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        view = view_of(rec)
        if view is None:
            skipped += 1
            continue
        views[view] += 1
        t = rec.get("t", 0.0)
        lines = rec.get("lines") or []

        for l in lines:
            text = (l.get("text") or "").strip()
            if not text:
                continue
            y, x = l.get("y", 0.0), l.get("x", 0.0)

            # browser tab strip and the very top/bottom edges are pure noise
            if view in ("meet_call", "meet_pip") and y > 0.88:
                continue

            m = COUNT_RE.search(text)
            if m:
                try:
                    counts.append({"t": t, "view": view, "n": int(m.group(1))})
                except ValueError:
                    pass

            m = TALKING_RE.match(text)
            if m:
                who = clean_name(m.group(1))
                if who:
                    talking.append({"t": t, "name": who, "via": f"{view}:talking"})
                continue

            # An initials avatar counts as presence when the invite list disambiguates it.
            if INITIALS.match(text.strip()) and text.strip() in inits:
                who = inits[text.strip()]
                from_initials[who] = from_initials.get(who, 0) + 1
                e = people[who]
                e["frames"] += 1
                e["first"] = t if e["first"] is None else min(e["first"], t)
                e["last"] = t if e["last"] is None else max(e["last"], t)
                e["views"].add(view)
                e["raw"].add(f"initials:{text.strip()}")
                hit0 = known.get(norm(who))
                if hit0:
                    e["role"], e["match"] = hit0[1], "contacts"
                else:
                    e["match"] = "calendar invite"
                continue

            nm = clean_name(text)
            if not nm:
                continue
            key = norm(nm)
            hit = known.get(key)
            display = hit[0] if hit else nm
            e = people[display]
            e["frames"] += 1
            e["first"] = t if e["first"] is None else min(e["first"], t)
            e["last"] = t if e["last"] is None else max(e["last"], t)
            e["views"].add(view)
            e["raw"].add(text[:48])
            if hit:
                e["role"], e["match"] = hit[1], "contacts"
            if any(mk in text.lower() for mk in SELF_MARKS):
                e["is_self"] = True

            # A name on a video tile (bottom strip) or alone in the pop-out is the
            # speaker being shown, which is Zoom/Meet's active-speaker view.
            # ONLY a single-tile surface tells us who is speaking. In Meet's gallery
            # view every participant tile shows its own name at once, so the bottom
            # strip is just whoever happens to be gridded there — measured on a real
            # 10-person call, votes were flat noise (top name 17%) and wrong. A shared
            # screen full of team names made it worse. Trust the pop-out only.
            if view == "meet_pip":
                talking.append({"t": t, "name": display, "via": f"{view}:tile"})

    for e in people.values():
        e["views"] = sorted(e["views"])
        e["raw"] = sorted(e["raw"])[:3]

    expected = max((c["n"] for c in counts), default=None)
    return {
        "from_initials": from_initials,
        "views": dict(views), "skipped_frames": skipped,
        "expected_participants": expected,
        "counts": counts[:40],
        "people": dict(sorted(people.items(), key=lambda kv: -kv[1]["frames"])),
        "talking": talking,
    }


def roster(res: dict, min_frames: int = 3) -> list[dict]:
    return [{"name": n, "frames": e["frames"], "role": e["role"], "is_self": e["is_self"]}
            for n, e in res["people"].items()
            if e["match"] in ("contacts", "calendar invite") and e["frames"] >= min_frames]


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    p = Path(sys.argv[1])
    if p.is_dir():
        p = p / "screen.jsonl"
    if not p.exists():
        print(f"no screen log at {p}"); sys.exit(1)
    res = build(p)
    (p.parent / "screen_roster.json").write_text(json.dumps(res, indent=2, default=list) + "\n")
    print(f"live-meeting frames by view: {res['views']}   (skipped {res['skipped_frames']} non-call frames)")
    print(f"expected participants (from the UI): {res['expected_participants']}")
    print(f"\n{'frames':>7}  {'match':<9} {'self':<5} name")
    print("-" * 60)
    for n, e in res["people"].items():
        if e["frames"] < 2:
            continue
        print(f"{e['frames']:>7}  {e['match']:<9} {'YOU' if e['is_self'] else '':<5} {n}"
              + (f"  [{e['role'][:30]}]" if e["role"] else ""))
    tk = res["talking"]
    print(f"\nactive-speaker observations: {len(tk)}")
    for k in list({(o['name'], o['via']) for o in tk})[:10]:
        n = sum(1 for o in tk if (o['name'], o['via']) == k)
        print(f"   {n:>4}x  {k[1]:<22} {k[0]}")
    print(f"\nwrote {p.parent/'screen_roster.json'}")


if __name__ == "__main__":
    main()

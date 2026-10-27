"""
updater.py — is there a newer mt, and installing it without an assistant.

An install made from a release is a git clone of the release repo, sitting on a v* tag.
Updating it is: fetch the tags, read what changed, switch to the newest tag, rebuild only
if something compiled changed, run `mt doctor` — and go back to where it was if either of
those fails. Your config, voiceprints, recordings, models and database are git-ignored or
outside the install, so switching tags never touches them.

A development checkout (anything whose origin is not the release repo) is never updated
from here — you update it by merging. check() still reports the newest public release for
it, so the About panel can say whether the public one is behind.

A release that needs something the update cannot do itself (a newer macOS, Xcode tools, a
Homebrew package) says so with a `Needs:` line in its CHANGELOG section. The update stops
before changing anything and shows that line, so it can go to IT first.

Stdlib only: web.py imports this, and `mt update` runs it from the CLI.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import paths as P

HERE = Path(__file__).resolve().parent
UPDIR = HERE / "run" / "update"
CHECK_FILE = UPDIR / "check.json"
STATUS_FILE = UPDIR / "status.json"
LOG_FILE = UPDIR / "update.log"

CHECK_EVERY = 24 * 3600          # a release a day is already a busy day
RETRY_AFTER_ERROR = 3600         # offline, GitHub down: try again within the hour
# Anything here changing between two releases means make.sh has to run.
BUILD_PATHS = ("src/", "make.sh", "requirements.txt", "models.sha256")
NEEDS = re.compile(r"^\s*(?:[-*]\s*)?\**Needs:\**\s*(.+?)\s*$", re.M)
SECTION = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _git(*a: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(HERE), *a], capture_output=True, text=True,
                          timeout=timeout)


def vtuple(s: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def current() -> str:
    try:
        return (HERE / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


def _norm(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    u = re.sub(r"^(https?://|git@)", "", u).replace("github.com:", "github.com/")
    return u.lower()


def install_kind() -> tuple[str, str]:
    """("release" | "dev", why). Only a release install is ever updated from here."""
    if not (HERE / ".git").exists():
        return "dev", "this install is not a git checkout"
    r = _git("remote", "get-url", "origin", timeout=10)
    origin = r.stdout.strip() if r.returncode == 0 else ""
    if not origin or _norm(origin) != _norm(P.RELEASE_URL):
        return "dev", "a development checkout — update it by merging"
    return "release", ""


def _sections(changelog: str, versions: set[str]) -> list[dict]:
    out = []
    for m in SECTION.finditer(changelog or ""):
        if m.group(1) in versions:
            body = m.group(2).strip()
            out.append({"version": m.group(1), "text": body,
                        "needs": [n.strip() for n in NEEDS.findall(body)]})
    out.sort(key=lambda s: vtuple(s["version"]), reverse=True)
    return out


def _latest_tag(tags: list[str]) -> str | None:
    vs = [t for t in tags if vtuple(t)]
    return max(vs, key=vtuple) if vs else None


def check(force: bool = False) -> dict:
    """What is installed, what is newest, and what changed in between. Cached a day."""
    cur = current()
    if not force:
        try:
            c = json.loads(CHECK_FILE.read_text())
            age = time.time() - c.get("_t", 0)
            if c.get("current") == cur and age < (CHECK_EVERY if c.get("ok")
                                                   else RETRY_AFTER_ERROR):
                return c
        except (OSError, ValueError):
            pass
    kind, why = install_kind()
    out = {"ok": True, "kind": kind, "why": why, "current": cur, "latest": None,
           "available": False, "notes": [], "needs": [], "checked_at": _now()}
    try:
        if kind == "release":
            f = _git("fetch", "--tags", "--force", "--quiet", "origin", timeout=60)
            if f.returncode != 0:
                raise RuntimeError((f.stderr or "git fetch failed").strip().splitlines()[-1])
            tags = _git("tag", "-l", "v*").stdout.split()
        else:
            r = subprocess.run(["git", "ls-remote", "--tags", P.RELEASE_URL],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or "git ls-remote failed").strip().splitlines()[-1])
            tags = [ln.split("refs/tags/")[-1] for ln in r.stdout.splitlines()
                    if "refs/tags/" in ln and not ln.endswith("^{}")]
        latest = _latest_tag(tags)
        out["latest"] = latest.lstrip("v") if latest else None
        newer = {t.lstrip("v") for t in tags
                 if vtuple(t) and vtuple(cur) and vtuple(t) > vtuple(cur)}
        out["available"] = bool(newer) and kind == "release"
        if out["available"]:
            log = _git("show", f"{latest}:CHANGELOG.md").stdout
            out["notes"] = _sections(log, newer)
            out["needs"] = [n for s in out["notes"] for n in s["needs"]]
    except Exception as e:
        out.update(ok=False, why=f"could not check for updates: {e}"[:300])
    out["_t"] = time.time()
    UPDIR.mkdir(parents=True, exist_ok=True)
    CHECK_FILE.write_text(json.dumps(out, indent=2))
    return out


def cached() -> dict:
    """The last check() as saved, without touching the network — what a page load reads.
    {"checking": True} until the server's background check has run once."""
    try:
        c = json.loads(CHECK_FILE.read_text())
        if c.get("current") == current():
            return c
    except (OSError, ValueError):
        pass
    return {"ok": True, "checking": True, "current": current()}


def status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}


class _Run:
    """Progress for the page: status.json is rewritten after every step."""

    def __init__(self, frm: str, to: str, say):
        self.s = {"state": "running", "from": frm, "to": to, "steps": [],
                  "started_at": _now()}
        self.say = say
        UPDIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text("")
        self._save()

    def _save(self):
        STATUS_FILE.write_text(json.dumps(self.s, indent=2))

    def step(self, text: str):
        self.s["steps"].append({"at": _now(), "text": text})
        self._save()
        self.say(text)

    def end(self, state: str, why: str = "", **kw) -> dict:
        self.s.update(state=state, why=why, finished_at=_now(), **kw)
        self._save()
        if why:
            self.say(why)
        return {"ok": state == "done", **self.s}


def _logged(cmd: list[str], timeout: int) -> int:
    with open(LOG_FILE, "a") as f:
        f.write(f"\n$ {' '.join(cmd)}\n")
        f.flush()
        return subprocess.run(cmd, cwd=str(HERE), stdout=f, stderr=subprocess.STDOUT,
                              timeout=timeout).returncode


def run(busy: str | None = None, force: bool = False, say=print) -> dict:
    """Update a release install to the newest release. `busy` is the caller's reason not
    to (a recording, a call still processing); `force` goes ahead past `Needs:` lines."""
    cur = current()
    chk = check(force=True)
    target = chk.get("latest")
    r = _Run(cur, target or "?", say)
    if chk.get("kind") != "release":
        return r.end("refused", chk.get("why") or "not a release install")
    if not chk.get("ok"):
        return r.end("failed", chk.get("why") or "could not check for updates")
    if not chk.get("available"):
        return r.end("refused", f"mt {cur} is already the newest release")
    if busy:
        return r.end("refused", f"not now: {busy}")
    if chk["needs"] and not force:
        return r.end("refused", "this update needs something done first: "
                     + "; ".join(chk["needs"]), needs=chk["needs"])
    dirty = _git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return r.end("refused", "files in the install were changed by hand, so an update "
                     "would overwrite them: " + ", ".join(
                         ln[3:] for ln in dirty.splitlines()[:5]))
    back = _git("rev-parse", "HEAD").stdout.strip()
    changed = _git("diff", "--name-only", back, f"v{target}").stdout.split()
    build = any(c.startswith(BUILD_PATHS) for c in changed)

    r.step(f"switching to v{target}")
    co = _git("-c", "advice.detachedHead=false", "checkout", "--quiet", f"v{target}")
    if co.returncode != 0:
        return r.end("failed", f"could not switch to v{target}: {co.stderr.strip()[:200]}")

    def undo(why: str) -> dict:
        r.step(f"going back to {cur}")
        _git("-c", "advice.detachedHead=false", "checkout", "--quiet", back)
        if build:
            r.step("rebuilding the previous version")
            _logged([str(HERE / "make.sh")], timeout=3600)
        return r.end("failed", why + f" — back on {cur}, nothing else changed", rolled_back=True)

    if build:
        r.step("rebuilding (helpers, packages or models changed)")
        try:
            rc = _logged([str(HERE / "make.sh")], timeout=3600)
        except subprocess.TimeoutExpired:
            rc = -1
        if rc != 0:
            return undo(f"the rebuild failed (see {LOG_FILE})")
    else:
        r.step("no rebuild needed")
    r.step("checking the install (mt doctor)")
    try:
        rc = _logged([sys.executable, str(HERE / "mt.py"), "doctor"], timeout=300)
    except subprocess.TimeoutExpired:
        rc = -1
    if rc != 0:
        return undo(f"mt doctor found a problem (see {LOG_FILE})")
    try:
        CHECK_FILE.unlink()
    except OSError:
        pass
    return r.end("done", "", rebuilt=build)

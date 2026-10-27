"""After-call work waits its turn: the pass queue (PLAN.md Phase 2b).

Stop used to run everything after a call — whisper, voices, vocal cues, laughter — inside
the recorder's own process, the moment the call ended. On Oct 21 2026 that process was
still scanning a 48-minute customer call for laughter twenty minutes later, the machine
crawled, and a back-to-back meeting could not have been recorded on top of it.

Now `mt live` stops the capture, writes a job here and exits. One worker runs the jobs in
order, and a recording always wins:

- a job never STARTS while a recording is running, nor for SETTLE_SEC after one ends
  (the gap in which the next meeting usually gets started);
- a job that is already running when a recording starts is frozen with SIGSTOP — its
  whole process group, whisper-cli and ffmpeg included — and thawed with SIGCONT once
  nothing has recorded for SETTLE_SEC. No checkpointing: it carries on mid-instruction;
- a job does not start while the machine is busy (CPU or memory pressure) or a calendar
  meeting is about to begin, unless the user asks for it now. A recording beats even that.

Everything is a file under run/queue/ with exactly one writer, so a crash, an `mt web`
restart or a reboot loses nothing and nothing has to be locked against anything else:

  <id>.json          the job — written once, by whoever queued it
  <id>.run.json      the worker — pid of the running job, frozen or not
  <id>.result.json   the job itself, as it exits — rc and per-step timings
  <id>.now           the user — "process this now": skip the busy and calendar holds
  recording-<pid>    a recorder (mt live), from before its capture starts until it stops
  hold               mt web's Start button — covers the second before the recorder's mark
  last-recording     touched when a recorder stops; the settle period counts from it
  worker.json        the worker — what it is waiting for, for the page to show
  worker.lock        flock: one worker at a time
  timings.jsonl      one line per finished job, so step times can be tracked over weeks
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

import paths as P

HERE = Path(__file__).resolve().parent
RUN = HERE / "run"
QDIR = RUN / "queue"
DONE = QDIR / "done"
LOCK = QDIR / "worker.lock"
HOLD = QDIR / "hold"
LAST_REC = QDIR / "last-recording"
WSTAT = QDIR / "worker.json"
WLOG = QDIR / "worker.log"
TIMINGS = QDIR / "timings.jsonl"

POLL = 1.0                 # how often the worker looks for a recording
HOLD_SEC = 75.0            # Start clicked → the recorder's own mark (STARTUP_TIMEOUT is 45 s)
BUSY_RECHECK = 30.0        # the CPU sample itself takes ~2 s; no need to take it every second
MEETING_GRACE_MIN = 5      # a calendar meeting nobody recorded stops holding work after this

# The user's call, Oct 21: 70% CPU or any memory pressure, "for now" — tune from timings.jsonl.
# Sampled over two seconds rather than read off the 1-minute load average: the average
# still carries the step that just finished, so the queue would keep deferring to itself.
DEFAULTS = {"queue_settle_sec": 20, "queue_busy_cpu": 70, "queue_meeting_hold_min": 10}

JOB_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,80}")


def setting(key: str) -> float:
    try:
        return float(P._CFG.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        return float(DEFAULTS[key])


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _write(p: Path, doc: dict) -> None:
    """Whole-file replace, so a reader never sees half a write."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, p)


def _alive(pid) -> bool:
    """A live mt process with this pid. Zombies and recycled pids are not alive: queue
    files outlive their processes, and macOS hands old pids to anything."""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return False
    try:
        out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "stat=", "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return True
    stat, _, cmd = out.partition(" ")
    return bool(out) and not stat.startswith("Z") and "mt.py" in cmd


def _signal_group(pid: int, sig) -> None:
    try:
        os.killpg(int(pid), sig)          # start_new_session=True: pgid == pid
    except (ProcessLookupError, PermissionError, TypeError, ValueError):
        pass


# ── files of one job ─────────────────────────────────────────────────────────

def job_file(jid: str) -> Path:
    return QDIR / f"{jid}.json"


def run_file(jid: str) -> Path:
    return QDIR / f"{jid}.run.json"


def result_file(jid: str) -> Path:
    return QDIR / f"{jid}.result.json"


def now_file(jid: str) -> Path:
    return QDIR / f"{jid}.now"


def valid_id(jid) -> bool:
    return isinstance(jid, str) and bool(JOB_ID.fullmatch(jid)) and job_file(jid).exists()


def load(jid: str) -> dict | None:
    return _read(job_file(jid)) if JOB_ID.fullmatch(jid or "") else None


def run_state(jid: str) -> dict:
    return (_read(run_file(jid)) or {}) if JOB_ID.fullmatch(jid or "") else {}


def enqueue(session: Path, args: dict, *, cues: bool, recorded_secs: float | None = None,
            recorded_at: str | None = None, params: dict | None = None) -> dict:
    """Queue the after-call work for one session. `args` re-creates the command line the
    recording was made with, so the job runs exactly as the inline pass would have."""
    QDIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9-]+", "-", session.name.lower()).strip("-")[-70:] or "meeting"
    jid, n = f"{stamp}-{slug}", 2
    while job_file(jid).exists():
        jid, n = f"{stamp}-{slug}-{n}", n + 1
    doc = {"id": jid, "session": str(session), "args": args, "cues": bool(cues),
           "params": params or {}, "recorded_secs": recorded_secs,
           "recorded_at": recorded_at, "queued_at": _now()}
    _write(job_file(jid), doc)
    return doc


def _jobs() -> list[dict]:
    """Every job not yet archived, oldest first."""
    out = []
    for p in QDIR.glob("*.json"):
        if not JOB_ID.fullmatch(p.stem):
            continue                       # .run.json / .result.json / worker.json
        doc = _read(p)
        if doc:
            out.append(doc)
    out.sort(key=lambda j: j["id"])
    return out


def _archive(jid: str) -> None:
    DONE.mkdir(parents=True, exist_ok=True)
    for f in (job_file(jid), run_file(jid), result_file(jid), now_file(jid)):
        if f.exists():
            os.replace(f, DONE / f.name)


def state_of(job: dict) -> str:
    """queued | running | failed | done — derived from the files, never stored."""
    jid = job["id"]
    res = _read(result_file(jid))
    if res is not None:
        return "done" if res.get("rc") == 0 else "failed"
    run = _read(run_file(jid))
    if run and _alive(run.get("pid")):
        return "running"
    return "queued"


def pending() -> bool:
    return any(state_of(j) in ("queued", "running") for j in _jobs())


# ── what the user can do ─────────────────────────────────────────────────────

def process_now(jid: str) -> dict:
    if not valid_id(jid):
        return {"ok": False, "why": "no such job"}
    now_file(jid).write_text(_now() + "\n")
    return {"ok": True}


def retry(jid: str) -> dict:
    if not valid_id(jid):
        return {"ok": False, "why": "no such job"}
    if state_of(load(jid)) == "running":
        return {"ok": False, "why": "it is running"}
    for f in (result_file(jid), run_file(jid)):
        f.unlink(missing_ok=True)
    return {"ok": True}


def dismiss(jid: str) -> dict:
    """Take a failed job off the list. The recording stays where it is."""
    if not valid_id(jid):
        return {"ok": False, "why": "no such job"}
    if state_of(load(jid)) == "running":
        return {"ok": False, "why": "it is running"}
    _archive(jid)
    return {"ok": True}


# ── recordings own the machine ───────────────────────────────────────────────

def _running_job() -> dict | None:
    for j in _jobs():
        run = _read(run_file(j["id"]))
        if run and _read(result_file(j["id"])) is None and _alive(run.get("pid")):
            return {"job": j, "run": run}
    return None


def pause_running() -> None:
    """Freeze the job that is running, right now. Safe to repeat; the worker thaws it."""
    cur = _running_job()
    if cur:
        _signal_group(cur["run"]["pid"], signal.SIGSTOP)


def hold_for_recording() -> None:
    """mt web's Start: claim the machine before the recorder process even exists."""
    QDIR.mkdir(parents=True, exist_ok=True)
    HOLD.write_text(_now() + "\n")
    pause_running()


def recorder_started(pid: int | None = None) -> Path:
    """The recorder's first act, before any capture: mark the machine as recording and
    freeze whatever job is running. Returns the mark, for recorder_stopped()."""
    QDIR.mkdir(parents=True, exist_ok=True)
    mark = QDIR / f"recording-{pid or os.getpid()}"
    mark.write_text(_now() + "\n")
    pause_running()
    return mark


def recorder_stopped(mark: Path | None) -> None:
    """The capture has ended. Idempotent: only the first call restarts the settle clock."""
    if mark is not None and not mark.exists():
        return
    QDIR.mkdir(parents=True, exist_ok=True)
    LAST_REC.write_text(_now() + "\n")
    if mark is not None:
        mark.unlink(missing_ok=True)


def recording_now(active_capture=None) -> str | None:
    """Why the machine belongs to a recording right now, or None."""
    for m in QDIR.glob("recording-*"):
        try:
            pid = int(m.name.split("-", 1)[1])
        except ValueError:
            continue
        if _alive(pid):
            return "a recording is running"
        m.unlink(missing_ok=True)          # a recorder that died without tidying up
    try:
        if time.time() - HOLD.stat().st_mtime < HOLD_SEC:
            return "a recording is starting"
    except OSError:
        pass
    # A recorder started some other way (mt record, an older mt live) leaves no mark.
    if active_capture is not None:
        try:
            if active_capture() is not None:
                return "a recording is running"
        except Exception:
            pass
    return None


def last_recording_ended() -> float:
    try:
        return LAST_REC.stat().st_mtime
    except OSError:
        return 0.0


# ── is the machine free? ─────────────────────────────────────────────────────

def machine_busy() -> str | None:
    """Why heavy work should wait, or None. Takes about two seconds (the CPU sample)."""
    try:
        out = subprocess.run(["top", "-l", "2", "-n", "0", "-s", "2"],
                             capture_output=True, text=True, timeout=20).stdout
        idle = re.findall(r"CPU usage:.*?([\d.]+)% idle", out)
        if idle:
            used = 100.0 - float(idle[-1])     # the first sample is since boot; skip it
            if used >= setting("queue_busy_cpu"):
                return f"the machine is busy (CPU {used:.0f}%)"
    except Exception:
        pass
    try:
        lvl = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                 capture_output=True, text=True, timeout=5).stdout.strip())
        # 1 normal, 2 warning, 4 critical. NOT "warning": measured Oct 21 on a 16 GB Mac
        # with the usual browser and chat apps open, it sat at 2 with 40% of memory free,
        # so any queued job would have waited forever. Critical is the red in Activity Monitor.
        if lvl >= 4:
            return "memory is critically low"
    except Exception:
        pass
    return None


def meeting_soon() -> str | None:
    """A calendar meeting starting within queue_meeting_hold_min, from mt web's cached
    calendar. A hint only: the cache can be stale (the corporate network blocks
    Google↔macOS sync), so it may delay work for a few minutes but never blocks it."""
    doc = _read(RUN / "today.json")
    if not doc:
        return None
    now = dt.datetime.now().astimezone()
    ahead = dt.timedelta(minutes=setting("queue_meeting_hold_min"))
    grace = dt.timedelta(minutes=MEETING_GRACE_MIN)
    for e in doc.get("events") or []:
        st = (e.get("start") or {}).get("dateTime")
        if not st:
            continue                           # all-day
        try:
            t = dt.datetime.fromisoformat(st)
        except ValueError:
            continue
        if t.date() != now.date():
            continue
        if t - ahead <= now < t + grace:
            return f"“{e.get('summary') or 'a meeting'}” starts at {t.strftime('%H:%M')}"
    return None


# ── the worker ───────────────────────────────────────────────────────────────

def worker_alive() -> bool:
    QDIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def ensure_worker(track=None) -> int | None:
    """Start the worker if there is work and nobody is doing it. Cheap enough to call on
    every page poll — which is also how a queue left over from a reboot gets picked up."""
    if not QDIR.exists() or not pending() or worker_alive():
        return None
    log = open(WLOG, "a", buffering=1)
    try:
        p = subprocess.Popen([sys.executable, str(HERE / "mt.py"), "queue", "run"],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    finally:
        log.close()
    if track is not None:
        track(p)
    return p.pid


class Worker:
    """One job at a time, oldest first ("process now" jumps the line), never alongside
    a recording. Exits when the queue is empty; whoever queues the next job starts it."""

    def __init__(self, active_capture=None, log=print):
        self.active_capture = active_capture
        self.log = log
        self.stop = False
        self.last_rec = last_recording_ended()
        self._status = None

    def _say(self, m: str) -> None:
        self.log(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {m}")

    def _status_set(self, job: dict | None, waiting: str | None) -> None:
        st = {"pid": os.getpid(), "job": job["id"] if job else None, "waiting": waiting}
        if st != self._status:
            # The log gets the reason once, not a line per second of a countdown.
            gist = lambda s: re.sub(r"\d+", "#", (s or {}).get("waiting") or "")
            if waiting and gist(st) != gist(self._status):
                self._say(f"{job['id'] if job else '-'}: waiting — {waiting}")
            self._status = st
            _write(WSTAT, {**st, "since": _now()})

    def _recording(self) -> str | None:
        why = recording_now(self.active_capture)
        if why:
            self.last_rec = time.time()
        self.last_rec = max(self.last_rec, last_recording_ended())
        return why

    def _settling(self) -> float:
        return setting("queue_settle_sec") - (time.time() - self.last_rec)

    def run(self) -> int:
        QDIR.mkdir(parents=True, exist_ok=True)
        lk = open(LOCK, "a+")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0                       # another worker has it
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(s, lambda *_: setattr(self, "stop", True))
        try:
            # Keep the Mac from idling to sleep while work is pending. A lid close still
            # sleeps it; the frozen job finishes on wake (measured Oct 18).
            subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        self._say(f"worker {os.getpid()} up")
        try:
            while not self.stop:
                cur = _running_job()
                if cur:                      # adopted: left running by an earlier worker
                    self._say(f"{cur['job']['id']}: adopting pid {cur['run']['pid']}")
                    self._supervise(cur["job"], None, cur["run"], adopted=True)
                    continue
                job = self._next()
                if job is None:
                    break
                if not self._wait_turn(job):
                    continue
                proc, run = self._spawn(job)
                self._supervise(job, proc, run)
        finally:
            WSTAT.unlink(missing_ok=True)
            self._say(f"worker {os.getpid()} down")
        return 0

    def _next(self) -> dict | None:
        queued = [j for j in _jobs() if state_of(j) == "queued"]
        if not queued:
            return None
        urgent = [j for j in queued if now_file(j["id"]).exists()]
        return (urgent or queued)[0]

    def _wait_turn(self, job: dict) -> bool:
        """Block until this job may start. False if it went away meanwhile."""
        busy, checked = None, 0.0
        while not self.stop:
            if not job_file(job["id"]).exists() or state_of(job) != "queued":
                return False
            why = self._recording()
            if not why and self._settling() > 0:
                why = f"a recording just ended — starting in {int(self._settling()) + 1} s"
            urgent = now_file(job["id"]).exists()
            if not why and not urgent:
                if busy is None or time.time() - checked >= BUSY_RECHECK:
                    busy, checked = machine_busy() or "", time.time()
                why = busy or meeting_soon()
            if not why:
                return True
            self._status_set(job, why)
            time.sleep(POLL)
        return False

    def _spawn(self, job: dict):
        """The job runs as its own process group, so one signal freezes all of it."""
        jid = job["id"]
        staging = RUN / f"pass-starting-{jid}.log"
        log = open(staging, "a", buffering=1)
        try:
            p = subprocess.Popen([sys.executable, str(HERE / "mt.py"), "queue", "job", jid],
                                 stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                 start_new_session=True, preexec_fn=lambda: os.nice(10))
        finally:
            log.close()
        logf = RUN / f"pass-{p.pid}.log"
        try:
            os.replace(staging, logf)
        except OSError:
            logf = staging
        # The same state file `mt live` writes, so mt web's Processing card needs no
        # second code path: it finds this pass exactly the way it finds an inline one.
        _write(RUN / f"pass-{p.pid}.json",
               {"pid": p.pid, "params": job.get("params") or {}, "session": job["session"],
                "started_at": job.get("recorded_at"), "pass_started_at": _now(),
                "recorded_secs": job.get("recorded_secs"), "job": jid, "name_from": None})
        run = {"pid": p.pid, "started_at": _now(), "log": str(logf),
               "paused": False, "paused_secs": 0.0}
        _write(run_file(jid), run)
        self._status_set(job, None)
        self._say(f"{jid}: started pid {p.pid}")
        return p, run

    def _supervise(self, job: dict, proc, run: dict, adopted: bool = False) -> None:
        jid, pid = job["id"], int(run["pid"])
        # An adopted job may have been frozen by a worker that is gone: assume it is,
        # and let the normal rule thaw it. SIGCONT on a running process does nothing.
        paused, since = adopted or bool(run.get("paused")), time.time()
        while True:
            if proc is not None:
                if proc.poll() is not None:
                    break
            elif not _alive(pid):
                break
            if self.stop:
                return                       # leave it as it is; the next worker adopts it
            why = self._recording()
            if why and not paused:
                _signal_group(pid, signal.SIGSTOP)
                paused, since = True, time.time()
                run.update(paused=True, paused_at=_now())
                _write(run_file(jid), run)
                self._say(f"{jid}: frozen — {why}")
            elif not why and paused and self._settling() <= 0:
                _signal_group(pid, signal.SIGCONT)
                paused = False
                run["paused_secs"] = round(run.get("paused_secs", 0.0) + time.time() - since, 1)
                run.update(paused=False, paused_at=None)
                _write(run_file(jid), run)
                self._say(f"{jid}: thawed")
            elif paused and not why:
                self._status_set(job, f"a recording just ended — resuming in "
                                      f"{int(self._settling()) + 1} s")
            if why:
                self._status_set(job, why)
            elif not paused:
                self._status_set(job, None)
            time.sleep(POLL)
        self._finish(job, proc, run)

    def _finish(self, job: dict, proc, run: dict) -> None:
        jid = job["id"]
        res = _read(result_file(jid))
        if res is None:
            # It died without reporting (killed, or crashed hard). Say so; never retry
            # automatically — a job that kills its process can do it again.
            rc = proc.returncode if proc is not None and proc.returncode is not None else -1
            res = {"rc": rc if rc != 0 else -1, "finished_at": _now(),
                   "why": f"the job exited without reporting (status {rc})"}
            _write(result_file(jid), res)
        line = {"id": jid, "session": Path(job["session"]).name,
                "recorded_secs": job.get("recorded_secs"), "rc": res.get("rc"),
                "wall_secs": res.get("wall_secs"), "cpu_secs": res.get("cpu_secs"),
                "paused_secs": run.get("paused_secs", 0.0), "steps": res.get("steps", []),
                "finished_at": res.get("finished_at")}
        with open(TIMINGS, "a") as f:
            f.write(json.dumps(line) + "\n")
        self._say(f"{jid}: finished rc={res.get('rc')}")
        if res.get("rc") == 0:
            _archive(jid)


# ── inside the job: timings ──────────────────────────────────────────────────

def cpu_seconds() -> float:
    """CPU used so far by this process and every child it has waited for."""
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


# Each ==> line belongs to one of these, in pipeline order; anything before "done"
# is the transcript. Wall AND cpu per stage: a stage whose wall time runs far past its
# cpu time was starved (Oct 21: laughter took 20 min for ~2 min of cpu).
STAGES = (("waiting", "waiting:"), ("live names", "applying the names"),
          ("vocal cues", "measuring vocal cues"), ("laughter", "listening for laughter"))


def stage_times(marks: list[tuple[str, float, float]], end: tuple[float, float]) -> list[dict]:
    """[(step label, wall, cpu), …] → [{"stage", "wall_secs", "cpu_secs"}, …]."""
    out: dict[str, list[float]] = {}
    stage = "transcript"
    for i, (label, wall, cpu) in enumerate(marks):
        nxt = marks[i + 1][1:] if i + 1 < len(marks) else end
        for name, frag in STAGES:
            if label.startswith(frag):
                stage = name
                break
        acc = out.setdefault(stage, [0.0, 0.0])
        acc[0] += nxt[0] - wall
        acc[1] += nxt[1] - cpu
    return [{"stage": k, "wall_secs": round(v[0], 1), "cpu_secs": round(v[1], 1)}
            for k, v in out.items()]


def write_result(jid: str, rc: int, started: tuple[float, float], steps: list[dict],
                 why: str = "") -> dict:
    wall0, cpu0 = started
    doc = {"rc": rc, "finished_at": _now(), "wall_secs": round(time.time() - wall0, 1),
           "cpu_secs": round(cpu_seconds() - cpu0, 1), "steps": steps}
    if why:
        doc["why"] = why
    _write(result_file(jid), doc)
    return doc


# ── for mt web and `mt queue` ────────────────────────────────────────────────

def status() -> dict:
    """Every job not yet archived, plus what the worker is waiting for."""
    w = _read(WSTAT) if worker_alive() else None
    jobs, pos = [], 0
    for j in _jobs():
        st = state_of(j)
        if st == "done":
            continue
        run = _read(run_file(j["id"])) or {}
        res = _read(result_file(j["id"])) or {}
        item = {"id": j["id"], "state": st, "session": Path(j["session"]).name,
                "path": j["session"], "title": (j.get("params") or {}).get("title") or "",
                "customer": (j.get("params") or {}).get("customer") or "",
                "recorded_secs": j.get("recorded_secs"), "queued_at": j.get("queued_at"),
                "now": now_file(j["id"]).exists(), "paused": bool(run.get("paused")),
                "pid": run.get("pid") if st == "running" else None}
        if st == "queued":
            pos += 1
            item["position"] = pos
        if st == "failed":
            item["why"] = res.get("why") or f"exited with status {res.get('rc')}"
            item["log"] = run.get("log") or ""
        if w and w.get("job") == j["id"]:
            item["waiting"] = w.get("waiting")
        jobs.append(item)
    return {"jobs": jobs, "worker": bool(w)}

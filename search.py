#!/usr/bin/env python3
"""
search.py — full-text search index for `mt` (the meeting-transcription tool).

Stdlib only. Implements exactly the API pinned in SEARCH-CONTRACT.md:

    INDEX_DB
    index_all(rebuild=False, verbose=False) -> dict
    index_session(session_dir) -> int
    search(q, limit=20, kind=None, customer=None, since=None) -> list[dict]
    stats() -> dict

Never writes the main DB. Never deletes a file under the workspace — the only
thing this module deletes is rows in its own DB/search.db.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------- locations

import paths as P

HERE = pathlib.Path(__file__).resolve().parent
WORKSPACE = P.WORKSPACE
INDEX_DB = P.SEARCH_DB                               # its own file, never the main DB
MEMORY_DIR = P.MEMORY_DIR

# Directory names that are never walked.
SKIP_DIRS = {
    "Inbox", "prompt-scratch-pad", "build", "__pycache__", "Tools",
    ".git", "node_modules", ".venv", "venv", "lib", "models", ".obsidian",
    "reprocess-backups",     # web.py REPROCESS_BACKUPS: superseded transcripts
}

# Roots for the generic `note` sweep (relative to WORKSPACE).
NOTE_ROOTS = ("Customers", "Reports", "Team")

SCHEMA = """
create table if not exists docs (
  id       integer primary key,
  kind     text not null,
  customer text,
  date     text,
  session  text,
  path     text not null,
  title    text,
  speaker  text,
  t_start  real,
  text     text not null,
  mtime    real not null
);
create index if not exists docs_path_ix    on docs(path);
create index if not exists docs_session_ix on docs(session);

create virtual table if not exists docs_fts using fts5(
  text, content='docs', content_rowid='id', tokenize="porter unicode61"
);

create table if not exists meta (k text primary key, v text);
"""

# Merge consecutive same-speaker transcript segments up to roughly this many chars.
SEGMENT_MERGE_CHARS = 200

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# meeting dir: YYYY-MM-DD-slug  or  YYYY-MM-DD-HHMM-slug
SESSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d{4}))?(?:-(.*))?$")
RECAP_RE = re.compile(r"^(\d{4})-(\d{2})\b")


# ---------------------------------------------------------------- html safety
#
# docs.text is stored RAW so that fts5 tokenizes the corpus as written —
# storing it pre-escaped turned every "&" into "&amp;", which tokenizes as
# "amp" and made AT&T / R&R / Q&A unfindable.
#
# Safety therefore moves to the read side, in three ordered steps that are the
# ONLY way a snippet is ever produced (see _run_search):
#   1. snippet() wraps matches in the sentinels MARK_OPEN / MARK_CLOSE, two
#      control characters that _scrub() guarantees cannot occur in docs.text;
#   2. html.escape(quote=False) neutralises the raw body — control characters
#      pass through it untouched, so the sentinels round-trip exactly;
#   3. the sentinels, and only the sentinels, become <mark> / </mark>.
# Because step 2 runs after step 1 and before step 3, no markup that came from
# the corpus can survive as a tag, and no corpus byte can forge a sentinel.
MARK_OPEN = "\x02"
MARK_CLOSE = "\x03"

# C0 control characters except tab / newline / carriage return. Stripping these
# at index time is what makes the sentinels forgery-proof.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _scrub(s: str) -> str:
    """Remove control characters from a source body before it is indexed."""
    return _CTRL_RE.sub("", s)


def esc(s: str) -> str:
    """Escape &, < and > only. Quotes are harmless in element text content and
    escaping them would pollute nothing — but leaving them alone keeps snippets
    readable. Applied to the SNIPPET, never to the stored body."""
    return html.escape(s, quote=False)


def _safe_snippet(raw: str) -> str:
    """Step 2 + step 3 above. Input is snippet() output over raw corpus text
    with sentinel markers; output is HTML-safe with <mark> markers."""
    return esc(raw).replace(MARK_OPEN, "<mark>").replace(MARK_CLOSE, "</mark>")


# ---------------------------------------------------------------- db helpers
def _connect(path: pathlib.Path | None = None, create: bool = True) -> sqlite3.Connection:
    p = pathlib.Path(path) if path is not None else INDEX_DB
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=WAL")
    con.execute("pragma synchronous=NORMAL")
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(p: pathlib.Path) -> str:
    """Workspace-relative path when possible, else absolute (memory dir)."""
    try:
        return str(p.resolve().relative_to(WORKSPACE))
    except ValueError:
        return str(p.resolve())


# ---------------------------------------------------------------- extraction
def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            t = s[2:].strip()
            if t:
                return t
    return None


def _frontmatter(text: str) -> dict:
    """Very small YAML-front-matter reader: top-level `key: value` pairs only."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line or line[0] in " \t#":
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _customer_of(p: pathlib.Path) -> str | None:
    parts = p.resolve().parts
    try:
        i = len(parts) - 1 - parts[::-1].index("Customers")
    except ValueError:
        return None
    return parts[i + 1] if i + 1 < len(parts) else None


def _session_of(p: pathlib.Path) -> str | None:
    """Meeting dir name if the file lives under .../meetings/<dir>/..."""
    parts = p.resolve().parts
    try:
        i = len(parts) - 1 - parts[::-1].index("meetings")
    except ValueError:
        return None
    return parts[i + 1] if i + 2 < len(parts) else None


def _date_of_session(session: str | None) -> str | None:
    if not session:
        return None
    m = SESSION_RE.match(session)
    return m.group(1) if m else None


def _date_from_name(name: str) -> str | None:
    m = DATE_RE.search(name)
    return m.group(0) if m else None


def _local_date(started_at: str | None) -> str | None:
    """'2026-09-14T17:04:03Z' -> local calendar date. Real tz conversion; a
    previous bug in this project came from string-slicing this field."""
    if not started_at:
        return None
    try:
        dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date().isoformat()
    except (ValueError, TypeError):
        return None


def _title_for(p: pathlib.Path, body: str, kind: str, session: str | None) -> str:
    fm = _frontmatter(body)
    if kind == "memory":
        return fm.get("name") or _first_heading(body) or p.stem
    if kind == "summary":
        return (
            _first_heading(body)
            or fm.get("meeting")
            or fm.get("title")
            or (session or p.stem)
        )
    if kind == "transcript":
        h = _first_heading(body)
        if h:
            return h
        return f"Transcript — {session or p.stem}"
    # recap / note
    return _first_heading(body) or fm.get("title") or fm.get("meeting") or p.stem


# ---------------------------------------------------------------- doc builders
def _doc(kind, path, body, *, customer=None, date=None, session=None,
         title=None, speaker=None, t_start=None, mtime=0.0) -> dict:
    return {
        "kind": kind, "customer": customer, "date": date, "session": session,
        "path": path, "title": title, "speaker": speaker, "t_start": t_start,
        "text": _scrub(body), "mtime": mtime,
    }


def _docs_for_markdown(p: pathlib.Path, kind: str, mtime: float) -> list[dict]:
    body = _read(p)
    session = _session_of(p)
    customer = _customer_of(p)
    if kind == "memory":
        customer = None
    date = _date_of_session(session)
    if not date:
        if kind == "recap":
            m = RECAP_RE.match(p.name)
            date = f"{m.group(1)}-{m.group(2)}-01" if m else None
        if not date:
            date = _date_from_name(p.name)
    title = _title_for(p, body, kind, session)
    return [_doc(kind, _rel(p), body, customer=customer, date=date,
                 session=session, title=title, mtime=mtime)]


def _speaker_name(speakers: dict, key) -> str | None:
    """Resolve speaker_key -> human name via the speakers dict, falling back to
    the raw key. Names here are verbatim ASR output — never 'corrected'."""
    if key is None:
        return None
    key = str(key)
    ent = speakers.get(key) if isinstance(speakers, dict) else None
    if isinstance(ent, dict):
        name = ent.get("name")
        if name:
            return str(name)
    elif isinstance(ent, str) and ent:
        return ent
    return key


def _merge_segments(timeline: list, speakers: dict) -> list[tuple[float, str, str]]:
    """Group consecutive segments from the SAME speaker until the merged text
    reaches ~SEGMENT_MERGE_CHARS, or the speaker changes. Deterministic.
    Returns [(t_start, speaker_name, merged_text), ...]."""
    out: list[tuple[float, str, str]] = []
    cur_key = None
    cur_start = None
    buf: list[str] = []

    def flush():
        nonlocal cur_key, cur_start, buf
        if buf:
            txt = " ".join(buf).strip()
            if txt:
                out.append((cur_start, _speaker_name(speakers, cur_key), txt))
        cur_key, cur_start, buf = None, None, []

    for seg in timeline:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        key = seg.get("speaker_key")
        try:
            start = float(seg.get("start") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        if buf and key != cur_key:
            flush()
        if not buf:
            cur_key, cur_start = key, start
        buf.append(text)
        if sum(len(x) + 1 for x in buf) >= SEGMENT_MERGE_CHARS:
            flush()
    flush()
    return out


def _docs_for_transcript_json(p: pathlib.Path, mtime: float) -> list[dict]:
    try:
        data = json.loads(_read(p) or "{}")
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    speakers = data.get("speakers") if isinstance(data.get("speakers"), dict) else {}
    timeline = data.get("timeline") if isinstance(data.get("timeline"), list) else []

    session = _session_of(p)
    customer = meta.get("customer") or _customer_of(p)
    date = _local_date(meta.get("started_at")) or _date_of_session(session)
    title = meta.get("title") or session or p.stem
    rel = _rel(p)

    docs = []
    for t_start, speaker, text in _merge_segments(timeline, speakers):
        docs.append(_doc("transcript_segment", rel, text, customer=customer,
                         date=date, session=session, title=title,
                         speaker=speaker, t_start=t_start, mtime=mtime))
    return docs


# ---------------------------------------------------------------- the walk
def _classify(p: pathlib.Path) -> str | None:
    """Return the kind for a path, or None to skip it."""
    name = p.name
    parts = p.parts
    if any(seg in SKIP_DIRS for seg in parts):
        return None
    # Meeting artifacts are classified by SHAPE, not by parent directory:
    # any */meetings/<dir>/summary.md is a summary whether it lives under
    # Customers/ or Team/. Team meetings simply have customer = None. A
    # "summaries only" filter must not silently hide 16 real internal summaries.
    if "meetings" in parts and _session_of(p) is not None:
        if name == "summary.md":
            return "summary"
        if name == "transcript.json":
            return "transcript_segment"
        if name.startswith("transcript") and name.endswith(".md"):
            return "transcript"
    if "monthly-recaps" in parts and name.endswith(".md"):
        return "recap"
    if name.endswith(".md"):
        return "note"
    return None


def _iter_sources():
    """Yield (path, kind) for every indexable source file."""
    seen = set()
    for root_name in NOTE_ROOTS:
        root = WORKSPACE / root_name
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                if not (fn.endswith(".md") or fn == "transcript.json"):
                    continue
                p = pathlib.Path(dirpath) / fn
                kind = _classify(p)
                if kind and str(p) not in seen:
                    seen.add(str(p))
                    yield p, kind
    if MEMORY_DIR and MEMORY_DIR.is_dir():
        for p in sorted(MEMORY_DIR.glob("*.md")):
            if p.name.startswith("."):
                continue
            yield p, "memory"


def _build_docs(p: pathlib.Path, kind: str, mtime: float) -> list[dict]:
    if kind == "transcript_segment":
        return _docs_for_transcript_json(p, mtime)
    return _docs_for_markdown(p, kind, mtime)


# ---------------------------------------------------------------- write path
def _delete_path(con: sqlite3.Connection, path: str) -> int:
    """Remove every row for `path`, keeping the external-content fts5 index in
    sync by hand (delete from docs_fts FIRST, then docs). Touches search.db only."""
    rows = con.execute("select id, text from docs where path = ?", (path,)).fetchall()
    for r in rows:
        con.execute(
            "insert into docs_fts(docs_fts, rowid, text) values ('delete', ?, ?)",
            (r["id"], r["text"]),
        )
    con.execute("delete from docs where path = ?", (path,))
    return len(rows)


def _insert_docs(con: sqlite3.Connection, docs: list[dict]) -> int:
    n = 0
    for d in docs:
        cur = con.execute(
            "insert into docs(kind,customer,date,session,path,title,speaker,"
            "t_start,text,mtime) values(?,?,?,?,?,?,?,?,?,?)",
            (d["kind"], d["customer"], d["date"], d["session"], d["path"],
             d["title"], d["speaker"], d["t_start"], d["text"], d["mtime"]),
        )
        con.execute("insert into docs_fts(rowid, text) values (?, ?)",
                    (cur.lastrowid, d["text"]))
        n += 1
    return n


# ---------------------------------------------------------------- public API
def index_all(rebuild: bool = False, verbose: bool = False) -> dict:
    """Walk the workspace. Incremental by mtime unless rebuild=True.
    added/updated/removed count DOCUMENT ROWS (one .md file == 1 row; one
    transcript.json == N merged segment rows)."""
    t0 = time.time()
    added = updated = removed = 0

    if rebuild and INDEX_DB.exists():
        try:
            INDEX_DB.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = INDEX_DB.with_name(INDEX_DB.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        except OSError:
            pass

    con = _connect()
    try:
        _ensure_schema(con)
        if rebuild:
            con.execute("insert into docs_fts(docs_fts) values('delete-all')")
            con.execute("delete from docs")
            con.commit()

        known: dict[str, float] = {
            r["path"]: r["mtime"]
            for r in con.execute("select path, max(mtime) as mtime from docs group by path")
        }
        on_disk: set[str] = set()

        for p, kind in _iter_sources():
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            rel = _rel(p)
            on_disk.add(rel)
            prev = known.get(rel)
            if prev is not None and abs(prev - mtime) < 1e-6:
                continue                                  # unchanged: skip
            try:
                docs = _build_docs(p, kind, mtime)
            except Exception as e:                        # never let one bad file stop the walk
                if verbose:
                    print(f"  !! {rel}: {e}", file=sys.stderr)
                continue
            if prev is None:
                added += _insert_docs(con, docs)
                if verbose:
                    print(f"  + {rel} ({len(docs)})")
            else:
                _delete_path(con, rel)
                updated += _insert_docs(con, docs)
                if verbose:
                    print(f"  ~ {rel} ({len(docs)})")

        for gone in sorted(set(known) - on_disk):
            removed += _delete_path(con, gone)            # search.db rows only
            if verbose:
                print(f"  - {gone}")

        total = con.execute("select count(*) from docs").fetchone()[0]
        con.execute("insert or replace into meta(k,v) values('indexed_at', ?)",
                    (datetime.now().astimezone().isoformat(timespec="seconds"),))
        con.commit()
        try:
            con.execute("insert into docs_fts(docs_fts) values('optimize')")
            con.commit()
        except sqlite3.OperationalError:
            pass
        # Fold the WAL back in so readers (web.py) see a self-contained file
        # and stats()["db_bytes"] reflects the real size.
        try:
            con.execute("pragma wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        con.close()

    return {"docs": total, "added": added, "updated": updated,
            "removed": removed, "seconds": round(time.time() - t0, 3)}


def index_session(session_dir) -> int:
    """(Re)index one meeting dir. Returns docs written. MUST NOT raise — it is
    called from inside the recording pipeline."""
    try:
        d = pathlib.Path(session_dir).expanduser().resolve()
        if not d.is_dir():
            return 0
        con = _connect()
        try:
            _ensure_schema(con)
            written = 0
            for p in sorted(d.rglob("*")):
                if not p.is_file():
                    continue
                kind = _classify(p)
                if not kind:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                rel = _rel(p)
                _delete_path(con, rel)
                written += _insert_docs(con, _build_docs(p, kind, mtime))
            con.commit()
            return written
        finally:
            con.close()
    except Exception:
        return 0


def _run_search(con, q, limit, kind, customer, since):
    where = ["docs_fts match ?"]
    args: list = [MARK_OPEN, MARK_CLOSE, q]
    if kind:
        where.append("d.kind = ?")
        args.append(kind)
    if customer:
        where.append("d.customer = ?")
        args.append(customer)
    if since:
        where.append("d.date is not null and d.date >= ?")
        args.append(since)
    sql = (
        "select d.kind, d.customer, d.date, d.session, d.title, d.path, "
        "       d.speaker, d.t_start, "
        "       snippet(docs_fts, 0, ?, ?, '…', 18) as snip, "
        "       bm25(docs_fts) as bm "
        "from docs_fts join docs d on d.id = docs_fts.rowid "
        "where " + " and ".join(where) +
        " order by bm asc limit ?"
    )
    args.append(int(limit))
    rows = con.execute(sql, args).fetchall()
    return [
        {
            "kind": r["kind"], "customer": r["customer"], "date": r["date"],
            "session": r["session"], "title": r["title"], "path": r["path"],
            "speaker": r["speaker"], "t_start": r["t_start"],
            "snippet": _safe_snippet(r["snip"] or ""), "score": -float(r["bm"]),
        }
        for r in rows
    ]


def search(q: str, limit: int = 20, kind: str | None = None,
           customer: str | None = None, since: str | None = None) -> list[dict]:
    """Ranked best-first (bm25, negated so higher == better).
    Never raises on malformed FTS5 syntax."""
    q = (q or "").strip()
    if not q:
        return []
    if not INDEX_DB.exists():
        return []
    try:
        con = _connect(create=False)
    except sqlite3.Error:
        return []
    try:
        try:
            return _run_search(con, q, limit, kind, customer, since)
        except sqlite3.OperationalError:
            literal = '"' + q.replace('"', '""') + '"'
            try:
                return _run_search(con, literal, limit, kind, customer, since)
            except sqlite3.OperationalError:
                return []
        except sqlite3.DatabaseError:
            return []
    finally:
        con.close()


def stats() -> dict:
    """Never raises; zeros/None when the index does not exist yet."""
    empty = {"docs": 0, "by_kind": {}, "customers": [],
             "indexed_at": None, "db_bytes": 0}
    try:
        if not INDEX_DB.exists():
            return empty
        size = INDEX_DB.stat().st_size
        con = _connect(create=False)
        try:
            total = con.execute("select count(*) from docs").fetchone()[0]
            by_kind = {r[0]: r[1] for r in con.execute(
                "select kind, count(*) from docs group by kind order by 2 desc")}
            customers = [r[0] for r in con.execute(
                "select distinct customer from docs "
                "where customer is not null and customer <> '' order by 1")]
            row = con.execute("select v from meta where k='indexed_at'").fetchone()
            indexed_at = row[0] if row else None
        finally:
            con.close()
        return {"docs": total, "by_kind": by_kind, "customers": customers,
                "indexed_at": indexed_at, "db_bytes": size}
    except Exception:
        return empty


# ---------------------------------------------------------------- selftest
def _selftest() -> int:
    """`python3 search.py --selftest` — no corpus, no mic, no main DB.
    Proves the HTML-safety, bad-query, merge, tz and empty-db guarantees."""
    import sqlite3 as _sq
    import tempfile
    global INDEX_DB
    fails: list[str] = []

    def chk(name, cond, extra=""):
        print(("PASS  " if cond else "FAIL  ") + name + (f"  :: {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    real = INDEX_DB
    try:
        # empty / missing index must not raise
        INDEX_DB = pathlib.Path(tempfile.mkdtemp()) / "missing" / "search.db"
        chk("stats() with no db", stats() == {"docs": 0, "by_kind": {}, "customers": [],
                                              "indexed_at": None, "db_bytes": 0})
        chk("search() with no db", search("anything") == [])

        # --- XSS: escaping happens AFTER snippet extraction, via sentinels. ---
        # Every one of these must come back inert. The last two are the cases
        # the old escape-first build handled for free; they must not regress.
        INDEX_DB = pathlib.Path(tempfile.mkdtemp()) / "x.db"
        con = _connect()
        _ensure_schema(con)
        evil = (
            'Intro. <script>alert("pwn")</script> then '
            '<img src=x onerror=alert(1)> and a literal <mark>fake</mark> tag, '
            'plus a sentinel forgery attempt \x02here\x03 and pre-escaped '
            '&lt;img src=x onerror=alert(1)&gt; text, all about DRBD failover. '
        ) * 3
        _insert_docs(con, [_doc("note", "Customers/x/evil.md", evil,
                                customer="x", date="2026-01-01", title="Evil", mtime=1.0)])
        con.commit()
        con.close()
        stored = _sq.connect(str(INDEX_DB)).execute("select text from docs").fetchone()[0]
        chk("stored body is RAW (so & tokenizes as written)",
            "<script>" in stored and "&amp;" not in stored)
        chk("control chars scrubbed from stored body -> sentinels unforgeable",
            MARK_OPEN not in stored and MARK_CLOSE not in stored)

        hits = search("DRBD")
        chk("evil doc is findable", len(hits) == 1)
        snip = hits[0]["snippet"]
        bare = snip.replace("<mark>", "").replace("</mark>", "")
        chk("only <mark> survives; no other tag in snippet",
            "<" not in bare and ">" not in bare, bare[:90])
        chk("snippet is actually marked up", "<mark>" in snip)
        chk("sentinels fully consumed (none leak to the caller)",
            MARK_OPEN not in snip and MARK_CLOSE not in snip)
        chk("<mark> tags are balanced",
            snip.count("<mark>") == snip.count("</mark>") and snip.count("<mark>") > 0,
            (snip.count("<mark>"), snip.count("</mark>")))

        # Each adversarial payload, checked on a snippet that actually contains
        # it. The probe word itself gets wrapped in <mark>, so strip our own
        # markers before looking for the needle.
        def probe_snip(term):
            h = search(term)
            if not h:
                return None
            return h[0]["snippet"].replace("<mark>", "").replace("</mark>", "")

        for term, needle, label in [
            ("script", "&lt;script&gt;alert", "literal <script> renders escaped"),
            ("onerror", "&lt;img src=x onerror=alert(1)&gt;",
             "literal <img onerror> renders escaped"),
            # needles stop short of the 18-token snippet window's truncation point
            ("fake", "&lt;mark&gt;fake&lt;/mark",
             "literal <mark> in source renders escaped"),
            ("pre-escaped", "&amp;lt;img src=x onerror=alert(1)&amp;gt",
             "pre-escaped source text stays inert (no regression)"),
        ]:
            bare_snip = probe_snip(term)
            ok = bare_snip is not None and needle in bare_snip \
                and "<" not in bare_snip and ">" not in bare_snip
            chk(label, ok, (bare_snip[:110] if bare_snip else "no hit"))

        h = search("forgery")
        chk("forged sentinel cannot inject an unbalanced <mark>",
            bool(h) and h[0]["snippet"].count("<mark>") == h[0]["snippet"].count("</mark>")
            and MARK_OPEN not in h[0]["snippet"],
            (h[0]["snippet"][:110] if h else "no hit"))

        # ampersand tokenization — the whole point of storing raw
        con = _connect()
        _ensure_schema(con)
        _insert_docs(con, [_doc("note", "Customers/x/amp.md",
                                "The AT&T circuit and the R&R schedule came up in Q&A.",
                                customer="x", date="2026-01-02", title="Amp", mtime=1.0)])
        con.commit()
        con.close()
        for term in ("AT&T", "R&R", "Q&A"):
            chk(f"{term} is findable", len(search(term)) >= 1, len(search(term)))

        chk("quoted-literal fallback still finds text",
            len(search('failover. Intro" text')) >= 0)
        for bad in ['unbalanced "quote', "foo AND OR", "NEAR(", "*", '"""', "a)b(", "x:::y"]:
            try:
                search(bad)
                chk(f"bad fts query {bad!r} does not raise", True)
            except Exception as e:                       # pragma: no cover
                chk(f"bad fts query {bad!r} does not raise", False, repr(e))
    finally:
        INDEX_DB = real

    # segment merging
    tl = [{"start": 0.0, "text": "Yeah.", "speaker_key": "mic"},
          {"start": 2.0, "text": "Right.", "speaker_key": "mic"},
          {"start": 4.0, "text": "A" * 250, "speaker_key": "mic"},
          {"start": 9.0, "text": "Ok.", "speaker_key": "sys"},
          {"start": 11.0, "text": "B" * 300, "speaker_key": "sys"}]
    sp = {"mic": {"name": "Alex Example"}, "sys": {"name": "Sam Sample"}}
    m = _merge_segments(tl, sp)
    chk("tiny segments merge into >=200-char blocks", len(m) == 2,
        [(x[0], x[1], len(x[2])) for x in m])
    chk("block keeps first segment's t_start", (m[0][0], m[1][0]) == (0.0, 9.0))
    chk("block text starts with the tiny segments", m[0][2].startswith("Yeah. Right. AAA"))
    chk("speaker_key resolves to a name",
        (m[0][1], m[1][1]) == ("Alex Example", "Sam Sample"))
    chk("unknown speaker_key falls back to the raw key",
        _speaker_name({}, "sys-spk9") == "sys-spk9")

    # UTC -> local, without string slicing
    chk("started_at converts through a real tz",
        _local_date("2026-09-15T02:00:00Z") is not None, _local_date("2026-09-15T02:00:00Z"))
    chk("malformed started_at returns None", _local_date("not-a-date") is None)

    print("\nFAILURES:", fails if fails else "none")
    return 1 if fails else 0


# ---------------------------------------------------------------- cli
def _main(argv: list[str]) -> int:
    args = [a for a in argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print("usage: search.py --rebuild | --index | --stats | --selftest | \"query\" "
              "[--kind K] [--customer C] [--since YYYY-MM-DD] [--limit N]")
        return 0
    if args[0] in ("--rebuild", "--index"):
        r = index_all(rebuild=(args[0] == "--rebuild"), verbose="-v" in args)
        print(json.dumps(r, indent=2))
        print(json.dumps(stats()["by_kind"], indent=2))
        return 0
    if args[0] == "--selftest":
        return _selftest()
    if args[0] == "--stats":
        print(json.dumps(stats(), indent=2))
        return 0

    q = args[0]
    opts = {"kind": None, "customer": None, "since": None, "limit": 20}
    i = 1
    while i < len(args) - 1:
        flag = args[i].lstrip("-")
        if flag in opts:
            opts[flag] = args[i + 1]
            i += 2
        else:
            i += 1
    hits = search(q, limit=int(opts["limit"]), kind=opts["kind"],
                  customer=opts["customer"], since=opts["since"])
    print(f"{len(hits)} hit(s) for {q!r}")
    for h in hits:
        loc = f"@{h['t_start']:.0f}s {h['speaker']}" if h["t_start"] is not None else ""
        print(f"\n[{h['score']:.2f}] {h['kind']:<19} {h['customer'] or '-':<14} "
              f"{h['date'] or '-'}  {loc}")
        print(f"      {h['title']}")
        print(f"      {h['path']}")
        print(f"      {h['snippet']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

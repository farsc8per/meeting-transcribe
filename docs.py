#!/usr/bin/env python3
"""
docs.py — the file layer for the mt markdown editor.

Reads and writes the markdown the user actually works in: weekly updates, monthly recaps,
meeting summaries and the loose working docs under Customers/. Everything here is
defensive: `mt web` imports it lazily and a missing file or a locked directory must
degrade to a line of text, never a 500.

THREE RULES THIS MODULE EXISTS TO ENFORCE

1. **Path confinement.** Every path is resolved and checked against its root with
   `relative_to()`. A path that escapes is refused, not clamped — the same check
   `/static/` uses in web.py.

2. **Raw transcripts are never writable.** `transcript.md` and `live.md` are capture
   output. CLAUDE.md is explicit that a garbled name gets fixed in the summary, never by
   editing the transcript, so the editor cannot open them for writing at all.

3. **Nothing is overwritten without a recoverable copy.** The workspace is NOT version
   controlled and Google Drive syncs it in place, so a bad save propagates and there is no
   `git checkout` to undo it. Every write copies the previous bytes to
   ~/.local/share/mt/versions/ first — OUTSIDE the workspace, so Drive never sees it and
   the "never delete a file under the workspace" rule is never in play.

   The build plan put version history in phase 4. It is here in phase 2 instead: shipping
   a file writer into a non-version-controlled, Drive-synced directory with no undo is not
   a thing to defer, and the whole mechanism is ~40 lines.

Phase 4 (Sep 23 2026) added the `memory` root — see ROOTS and split_front().

CLI (development):
    python3 docs.py --list
    python3 docs.py --read Reports/weekly-updates/2026/09/2026-09-17-caseys-update.md
    python3 docs.py --versions <path>
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import datetime as dt
from pathlib import Path

import paths as P

HERE = Path(__file__).resolve().parent
WORKSPACE = P.WORKSPACE

# Root aliases.
#   workspace — the workspace tree, limited to ALLOWED_PREFIXES.
#   memory    — the assistant's memory files. Outside the workspace (so not Drive-synced),
#               every file carries YAML front matter that must survive a round trip
#               byte-for-byte.
#               See split_front()/_merge_front(): the editor only ever sends a BODY plus
#               explicit field edits, and the front matter is rebuilt from the bytes on
#               disk at save time — the page never holds the authoritative copy.
MEMORY_DIR = P.MEMORY_DIR                            # honours $MT_MEMORY_ROOT
ROOTS = {
    "workspace": Path(os.environ.get("MT_DOCS_ROOT") or WORKSPACE),
    "memory": MEMORY_DIR,
}

# The four memory types. `type` is the one front-matter field whose value other tooling
# groups by, so it is validated like the tracking vocabularies.
MEMORY_TYPES = ["user", "feedback", "project", "reference"]
MEMORY_INDEX = "MEMORY.md"

# Only these subtrees, and only markdown.
ALLOWED_PREFIXES = ("Reports/", "Customers/")

# Capture output. Never writable, and not listed either — the editor should not imply it.
BLOCKED_NAMES = {"transcript.md", "live.md"}

VERSION_DIR = Path(os.environ.get("MT_DOCS_VERSIONS")
                   or (Path.home() / ".local" / "share" / "mt" / "versions"))
KEEP_VERSIONS = 20
MAX_BYTES = 4 * 1024 * 1024        # a 4 MB markdown file is a bug, not a document


# ------------------------------------------------------------------ paths

def _root(alias: str) -> Path | None:
    return ROOTS.get((alias or "workspace").strip() or "workspace")


def resolve(alias: str, rel: str) -> Path | None:
    """Absolute path for a root-relative path, or None if it escapes the root, is not
    markdown, or names a capture file. Never raises."""
    base = _root(alias)
    if base is None:
        return None
    rel = (rel or "").strip().lstrip("/")
    if not rel or rel.endswith("/"):
        return None
    try:
        p = (base / rel).resolve()
        p.relative_to(base.resolve())          # the confinement check
    except Exception:
        return None
    if p.suffix.lower() != ".md":
        return None
    if p.name in BLOCKED_NAMES:
        return None
    if alias == "workspace" and not rel.startswith(ALLOWED_PREFIXES):
        return None
    return p


# ------------------------------------------------------------------ front matter

_FM_OPEN = "---\n"
_FM_CLOSE = "\n---\n"


def split_front(text: str) -> tuple[str, dict, str]:
    """(raw_front_matter, fields, body). `raw` is EVERY byte up to and including the
    closing `---` line, so `raw + body == text` always. `fields` is a flat, read-only view
    for display (top-level keys plus the keys nested one level under `metadata:`); it is
    never serialised back. No front matter → ("", {}, text). Never raises."""
    text = text or ""
    if not text.startswith(_FM_OPEN):
        return "", {}, text
    end = text.find(_FM_CLOSE, len(_FM_OPEN) - 1)
    if end < 0:
        return "", {}, text                # unterminated: treat it all as body, edit nothing
    cut = end + len(_FM_CLOSE)
    raw = text[:cut]
    fields = {}
    for line in raw.split("\n")[1:-2]:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*):\s?(.*)$", line)
        if not m or not m.group(2).strip():
            continue
        fields[m.group(1)] = _unquote(m.group(2).strip())
    return raw, fields, text[cut:]


def _unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            import json
            return json.loads(v)
        except Exception:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _quote(v: str) -> str:
    """A JSON string is a valid YAML double-quoted scalar, and it is the form ~74% of the
    existing descriptions already use."""
    import json
    return json.dumps(v, ensure_ascii=False)


def _merge_front(raw: str, edits: dict) -> tuple[str | None, str | None]:
    """Apply explicit field edits to the ON-DISK front matter by rewriting only the one
    line each edit names. Every other byte — key order, the trailing space after
    `metadata:`, originSessionId, `modified` — is untouched. (new_raw, error)."""
    if not edits:
        return raw, None
    if not raw:
        return None, "this file has no front matter to edit"
    lines = raw.split("\n")
    for key, val in edits.items():
        if key == "description":
            val = (val or "").strip()
            if not val:
                return None, "description cannot be empty — it is how recall decides relevance"
            if "\n" in val or "\r" in val:
                return None, "description must be one line"
            pat, indent = re.compile(r"^description:"), ""
        elif key == "type":
            val = (val or "").strip()
            if val not in MEMORY_TYPES:
                return None, f"type must be one of {', '.join(MEMORY_TYPES)}"
            pat, indent = re.compile(r"^(\s+)type:"), None
        else:
            return None, f"{key} is not editable here"
        hit = [i for i, l in enumerate(lines) if pat.match(l)]
        if len(hit) != 1:
            return None, f"could not find exactly one {key} line — edit the file by hand"
        i = hit[0]
        cur = _unquote(lines[i].split(":", 1)[1].strip())
        if cur == val:
            continue                       # unchanged: keep the original bytes, quoting and all
        if indent is None:
            indent = pat.match(lines[i]).group(1)
        lines[i] = f"{indent}{key}: {_quote(val) if key == 'description' else val}"
    return "\n".join(lines), None


def rel_of(alias: str, p: Path) -> str:
    base = _root(alias)
    try:
        return str(Path(p).resolve().relative_to(base.resolve()))
    except Exception:
        return str(p)


# ------------------------------------------------------------------ metadata

_RE_MEETING = re.compile(r"^Customers/([^/]+)/meetings/([^/]+)/")
_RE_RECAP = re.compile(r"^Customers/([^/]+)/monthly-recaps/")
_RE_CUST = re.compile(r"^Customers/([^/]+)/")
_RE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def classify(rel: str) -> dict:
    """What kind of document this is, from its path alone. The naming conventions are
    load-bearing here, and they have drifted before — one account uses 3-letter months in
    recap filenames while every other spells them out — so this reads structure
    (which directory) rather than trying to parse the filename."""
    rel = (rel or "").replace("\\", "/")
    kind, session = "note", None
    if rel.startswith("Reports/weekly-updates/"):
        kind = "weekly update"
    elif _RE_RECAP.match(rel):
        kind = "recap"
    elif rel.endswith("/summary.md") and "/meetings/" in rel:
        kind = "summary"
    elif rel.startswith("Reports/"):
        kind = "report"
    m = _RE_MEETING.match(rel)
    if m:
        session = m.group(2)
    cm = _RE_CUST.match(rel)
    dm = _RE_DATE.search(Path(rel).name) or _RE_DATE.search(rel)
    return {"kind": kind,
            "customer": cm.group(1) if cm else None,
            "session": session,
            "date": dm.group(1) if dm else None}


# ------------------------------------------------------------------ listing

def list_files(q: str = "", kind: str = "", customer: str = "",
               limit: int = 800, root: str = "workspace") -> list[dict]:
    """Every editable markdown file, newest first by filename date then mtime.
    Filesystem-backed rather than read from DB/search.db: the index can be stale, and a
    file the editor offers must actually exist. ~480 files, so a walk is cheap."""
    if root == "memory":
        return list_memory(q=q, kind=kind, limit=limit)
    base = _root("workspace")
    if base is None or not base.exists():
        return []
    out = []
    q = (q or "").strip().lower()
    for prefix in ALLOWED_PREFIXES:
        d = base / prefix.rstrip("/")
        if not d.exists():
            continue
        for p in d.rglob("*.md"):
            if p.name in BLOCKED_NAMES or not p.is_file():
                continue
            rel = rel_of("workspace", p)
            meta = classify(rel)
            if kind and meta["kind"] != kind:
                continue
            if customer and meta["customer"] != customer:
                continue
            if q and q not in rel.lower():
                continue
            try:
                st = p.stat()
            except Exception:
                continue
            out.append({"root": "workspace", "path": rel, "name": p.name,
                        "bytes": st.st_size, "mtime": st.st_mtime,
                        "kind": meta["kind"], "customer": meta["customer"],
                        "session": meta["session"], "date": meta["date"]})
    out.sort(key=lambda d: (d["date"] or "", d["mtime"]), reverse=True)
    return out[:limit]


def kinds(root: str = "workspace") -> list[str]:
    if root == "memory":
        return MEMORY_TYPES + ["index", "archived"]
    return ["weekly update", "recap", "summary", "report", "note"]


def _memory_kind(rel: str, fields: dict) -> str:
    if rel == MEMORY_INDEX:
        return "index"
    if rel.startswith("archive/"):
        return "archived"
    t = fields.get("type")
    return t if t in MEMORY_TYPES else "unknown"


def list_memory(q: str = "", kind: str = "", limit: int = 800) -> list[dict]:
    """Memory files: the index first, then newest-modified. Reads only the head of each
    file for its front matter — 140 small files, well under a frame."""
    base = _root("memory")
    if base is None or not base.exists():
        return []
    out = []
    q = (q or "").strip().lower()
    for p in sorted(base.rglob("*.md")):
        if not p.is_file():
            continue
        rel = rel_of("memory", p)
        try:
            st = p.stat()
            with open(p, errors="replace") as fh:
                head = fh.read(4096)
        except Exception:
            continue
        _, fields, _ = split_front(head)
        k = _memory_kind(rel, fields)
        if kind and k != kind:
            continue
        desc = fields.get("description") or ""
        if q and q not in rel.lower() and q not in desc.lower() \
                and q not in (fields.get("name") or "").lower():
            continue
        dm = _RE_DATE.search(fields.get("modified") or "")
        out.append({"root": "memory", "path": rel, "name": p.name,
                    "slug": fields.get("name") or p.stem, "description": desc,
                    "bytes": st.st_size, "mtime": st.st_mtime, "kind": k,
                    "customer": None, "session": None,
                    "date": dm.group(1) if dm else
                            dt.date.fromtimestamp(st.st_mtime).isoformat()})
    out.sort(key=lambda d: (d["kind"] == "index", d["mtime"]), reverse=True)
    return out[:limit]


def memory_names() -> dict:
    """Every name a `[[wiki-link]]` might use, mapped to its path. Links in the wild use
    both the `name:` slug (reference-people-map) and the filename stem
    (reference_people_map), so both resolve — plus the hyphen/underscore swap of each."""
    names = {}
    for r in list_memory(limit=100000):
        if r["kind"] == "index":
            continue
        for n in (r["slug"], Path(r["path"]).stem):
            for v in (n, n.replace("_", "-"), n.replace("-", "_")):
                names.setdefault(v, r["path"])
    return names


# ------------------------------------------------------------------ read / write

def read_file(alias: str, rel: str) -> dict:
    """{'ok','root','path','text','mtime','bytes','meta','error'}. Never raises."""
    p = resolve(alias, rel)
    if p is None:
        return {"ok": False, "error": "that path is not editable from here"}
    if not p.exists() or not p.is_file():
        return {"ok": False, "error": "no such file"}
    try:
        st = p.stat()
        if st.st_size > MAX_BYTES:
            return {"ok": False, "error": f"file is {st.st_size // 1024} KB — too large "
                                          "to open in the editor"}
        text = p.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rel = rel_of(alias, p)
    if alias == "memory":
        raw, fields, body = split_front(text)
        return {"ok": True, "root": alias, "path": rel, "text": text,
                "front": raw, "fields": fields, "body": body,
                "types": MEMORY_TYPES, "index": rel == MEMORY_INDEX,
                "mtime": st.st_mtime, "bytes": st.st_size,
                "meta": {"kind": _memory_kind(rel, fields), "customer": None,
                         "session": None, "date": None}, "error": None}
    return {"ok": True, "root": alias, "path": rel, "text": text,
            "mtime": st.st_mtime, "bytes": st.st_size,
            "meta": classify(rel), "error": None}


def _vault(alias: str, rel: str) -> Path:
    key = hashlib.sha1(f"{alias}:{rel}".encode()).hexdigest()[:16]
    return VERSION_DIR / key


def _snapshot(alias: str, rel: str, p: Path) -> str | None:
    """Copy the current bytes aside before they are replaced. Returns the stamp, or None
    if there was nothing to keep. A failure here ABORTS the write — the whole point is
    that no save happens without a recoverable copy."""
    if not p.exists():
        return None
    v = _vault(alias, rel)
    v.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    (v / f"{stamp}.md").write_bytes(p.read_bytes())
    (v / "path.txt").write_text(f"{alias}\n{rel}\n")
    # Prune oldest. These live outside the workspace, so the never-delete rule is not
    # in play; nothing here can touch a workspace file.
    keep = sorted(v.glob("*.md"), reverse=True)[:KEEP_VERSIONS]
    for old in sorted(v.glob("*.md"), reverse=True)[KEEP_VERSIONS:]:
        try:
            old.unlink()
        except Exception:
            pass
    return stamp


def write_file(alias: str, rel: str, text: str, mtime=None,
               body: str | None = None, fields: dict | None = None) -> dict:
    """Save, with a concurrency guard and a version kept first.

    `mtime` is what the editor saw when it opened the file. If the file changed since,
    the write is REFUSED rather than merged — the assistant and the page are both writers,
    and PLAN.md already flags last-write-wins as the known hazard for speakers.json."""
    p = resolve(alias, rel)
    if p is None:
        return {"ok": False, "why": "that path is not editable from here"}
    if not p.exists():
        return {"ok": False, "why": "no such file"}
    if body is not None or fields:
        # Memory-style save: the page sends the body and any explicit field edits; the
        # front matter comes from disk, NOT from the page. mtime is still checked below,
        # so "from disk" is exactly what the editor opened.
        if body is not None and not isinstance(body, str):
            return {"ok": False, "why": "nothing to save"}
        try:
            disk = p.read_text(errors="replace")
        except Exception as e:
            return {"ok": False, "why": f"{type(e).__name__}: {e}"}
        raw, _, disk_body = split_front(disk)
        new_raw, err = _merge_front(raw, fields or {})
        if err:
            return {"ok": False, "why": err}
        text = new_raw + (disk_body if body is None else body)
        if text == disk:
            return {"ok": True, "path": rel_of(alias, p), "mtime": p.stat().st_mtime,
                    "bytes": len(disk.encode()), "version": None, "unchanged": True,
                    "why": None}
    if not isinstance(text, str):
        return {"ok": False, "why": "nothing to save"}
    if len(text.encode()) > MAX_BYTES:
        return {"ok": False, "why": "that is too large to save"}

    try:
        cur = p.stat().st_mtime
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    if mtime is not None:
        try:
            if abs(float(mtime) - cur) > 0.001:
                return {"ok": False, "stale": True,
                        "why": "this file changed on disk since you opened it — reload "
                               "before saving, or your edit would overwrite the change"}
        except (TypeError, ValueError):
            pass

    try:
        stamp = _snapshot(alias, rel, p)
    except Exception as e:
        return {"ok": False,
                "why": f"could not keep a backup, so nothing was written ({e})"}

    try:
        tmp = p.with_suffix(p.suffix + ".mt-tmp")
        tmp.write_text(text)
        os.replace(tmp, p)                 # atomic; a crash mid-write cannot truncate
        st = p.stat()
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": rel_of(alias, p), "mtime": st.st_mtime,
            "bytes": st.st_size, "version": stamp, "why": None}


# ------------------------------------------------------------------ versions

def versions(alias: str, rel: str) -> list[dict]:
    """Prior copies, newest first. Never raises."""
    v = _vault(alias, rel)
    if not v.exists():
        return []
    out = []
    for f in sorted(v.glob("*.md"), reverse=True):
        try:
            st = f.stat()
        except Exception:
            continue
        out.append({"stamp": f.stem, "bytes": st.st_size, "saved_at": st.st_mtime})
    return out


def read_version(alias: str, rel: str, stamp: str) -> dict:
    v = _vault(alias, rel)
    f = v / f"{(stamp or '').strip()}.md"
    try:
        f.resolve().relative_to(v.resolve())
    except Exception:
        return {"ok": False, "error": "no such version"}
    if not f.exists():
        return {"ok": False, "error": "no such version"}
    try:
        text = f.read_text(errors="replace")
        raw, fields, body = split_front(text)
        return {"ok": True, "stamp": stamp, "text": text, "front": raw, "body": body,
                "error": None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def restore_version(alias: str, rel: str, stamp: str) -> dict:
    """Put an old version back. The CURRENT bytes are snapshotted first, so a restore is
    itself undoable — restoring the wrong version must not be a dead end."""
    old = read_version(alias, rel, stamp)
    if not old.get("ok"):
        return {"ok": False, "why": old.get("error") or "no such version"}
    p = resolve(alias, rel)
    if p is None or not p.exists():
        return {"ok": False, "why": "no such file"}
    try:
        cur = p.stat().st_mtime
    except Exception:
        cur = None
    return write_file(alias, rel, old["text"], mtime=cur)


# ------------------------------------------------------------------------- CLI

def _main(args: list[str]) -> int:
    if not args or args[0] == "--list":
        rows = list_files()
        print(f"{len(rows)} editable file(s)\n")
        for r in rows[:40]:
            print(f"  {r['kind']:<14} {(r['customer'] or '-'):<14} "
                  f"{r['bytes']:>7}  {r['path']}")
        if len(rows) > 40:
            print(f"  … and {len(rows) - 40} more")
        return 0

    if args[0] == "--read" and len(args) > 1:
        r = read_file("workspace", args[1])
        if not r.get("ok"):
            print(r.get("error"), file=sys.stderr)
            return 1
        print(f"# {r['path']}  ({r['bytes']} bytes, {r['meta']})\n")
        print(r["text"][:1500])
        return 0

    if args[0] == "--roundtrip":
        # Prove the split is lossless on every memory file: raw + body must equal the
        # file, and a no-edit merge must return the raw unchanged.
        bad = 0
        rows = list_memory(limit=100000)
        for r in rows:
            t = (_root("memory") / r["path"]).read_text(errors="replace")
            raw, _, body = split_front(t)
            same, err = _merge_front(raw, {})
            if raw + body != t or same != raw or err:
                bad += 1
                print("  LOSSY", r["path"])
        print(f"{len(rows) - bad}/{len(rows)} memory files round-trip byte-for-byte")
        return 1 if bad else 0

    if args[0] == "--versions" and len(args) > 1:
        for v in versions("workspace", args[1]):
            print(f"  {v['stamp']}  {v['bytes']:>7} bytes")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

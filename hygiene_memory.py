"""hygiene_memory.py — the /hygiene page, Assistant memory area (Phase 3b, TRACKING-PLAN.md).

Deliberately a separate module from hygiene.py (the Data area). The two never share a queue:
a bad data fix breaks one row you can see; a bad memory change alters how the assistant behaves
in every session, quietly. So the handling here is stricter:

  * ONE proposal at a time. There is no bulk approve, and no code path for one.
  * The assistant PROPOSES (a JSON file in hygiene/proposals/, outside the memory dir so it can
    never load as memory). The tool PREVIEWS the complete after-state and APPLIES it only when
    the user approves. The assistant never applies its own proposal.
  * Nothing is deleted. A merged-away file moves to archive/. A split keeps the whole old
    file, verbatim, as archive/<stem>_history.md (the tool copies it; the assistant doesn't
    retype it, so nothing can be lost in transcription).
  * Every [[link]] and MEMORY.md line that pointed at a moved file is repointed, and the
    preview refuses if the result would break a link that resolves today.
  * Stale guard: every file a proposal touches is hashed when the assistant proposes it. If any
    of them changed since (another session wrote memory), the proposal is refused until the
    assistant re-bases it — the preview would otherwise be of a file that no longer exists.
  * Apply: tar.gz of the whole memory dir, then pre- and post-images of every touched file
    (outside the workspace and outside memory), then the writes. Any failure part-way rolls
    every file back. Undo restores exactly the pre-images, and refuses if anything was edited
    since.

Proposal format and the assistant-side CLI: MEMORY-PROPOSALS.md, next to this file.

CLI (what the assistant runs):
  python3 hygiene_memory.py --findings            the mechanical checks
  python3 hygiene_memory.py --propose draft.json  validate + file a proposal (prints its id)
  python3 hygiene_memory.py --revise ID draft.json   replace a sent-back proposal
  python3 hygiene_memory.py --list                every proposal, with the user's notes
  python3 hygiene_memory.py --plan ID             print what applying would do
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import docs as D      # noqa: E402  the memory root, front matter, safe writes + versions
import paths as P     # noqa: E402  the assistant's name
import tracking as TR  # noqa: E402  the DB (hygiene_log, contacts)

PROPOSALS = Path(os.environ.get("MT_HYGIENE_PROPOSALS") or (HERE / "hygiene" / "proposals"))
BACKUPS = Path(os.environ.get("MT_HYGIENE_BACKUPS")
               or (Path.home() / ".local" / "share" / "mt" / "hygiene-memory"))

INDEX = D.MEMORY_INDEX
BIG_BYTES = 30 * 1024
BIG_EXEMPT = {"reference_people_map.md"}      # the generated mirror of `contacts`
STALE_DAYS = 30
INDEX_LINE_MAX = 250
DESC_SIMILAR = 0.82
REWORD_SIMILAR = 0.6       # same line, new wording (a link swapped for a path, a fixed date)
KINDS = ["merge", "split", "retype", "contradiction", "archive", "edit"]
OPEN_STATES = ("pending", "revised")

LINK_RE = re.compile(r"\[\[([^\]\|#\n]+)((?:[#|][^\]\n]*)?)\]\]")
INDEX_REF_RE = re.compile(r"\]\(([^)\s]+\.md)\)")
_NAME_LINE = re.compile(r"^name:\s*(.*)$", re.M)
_DESC_LINE = re.compile(r"^description:\s*(.*)$", re.M)


# ------------------------------------------------------------------ plumbing

def _root() -> Path:
    return D._root("memory")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_all() -> dict[str, str]:
    """rel -> text for every .md under the memory root, archive/ included."""
    base = _root()
    out = {}
    if base is None or not base.exists():
        return out
    for p in sorted(base.rglob("*.md")):
        if p.is_file():
            try:
                out[p.relative_to(base).as_posix()] = p.read_text(errors="replace")
            except Exception:
                pass
    return out


def _fields(text: str) -> dict:
    return D.split_front(text)[1]


def _variants(n: str) -> set[str]:
    n = (n or "").strip()
    return {v for v in (n, n.replace("_", "-"), n.replace("-", "_")) if v}


def _names(files: dict[str, str]) -> dict[str, str]:
    """Every name a [[link]] may use -> the file it resolves to. Live files win over
    archive/ ones, so a history copy can never capture a link meant for the live file."""
    names: dict[str, str] = {}
    order = sorted((r for r in files if r != INDEX), key=lambda r: (r.startswith("archive/"), r))
    for rel in order:
        f = _fields(files[rel])
        for n in (f.get("name") or "", Path(rel).stem):
            for v in _variants(n):
                names.setdefault(v, rel)
    return names


def _links(text: str) -> list[str]:
    return [m.group(1).strip() for m in LINK_RE.finditer(text or "")]


def _unresolved(files: dict[str, str]) -> set[tuple[str, str]]:
    """(file, link) pairs that resolve to nothing. archive/ files are not held to it —
    archived content is history, and its old links are part of the record."""
    names = _names(files)
    out = set()
    for rel, text in files.items():
        if rel.startswith("archive/"):
            continue
        for l in _links(text):
            if l not in names:
                out.add((rel, l))
    return out


def _index_refs(index_text: str) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate((index_text or "").split("\n")):
        for m in INDEX_REF_RE.finditer(line):
            out.append((i, m.group(1)))
    return out


# ------------------------------------------------------------------ findings (read-only)

def findings() -> dict:
    files = _read_all()
    if not files:
        return {"groups": [], "error": f"memory folder not found: {_root()}"}
    live = {r: t for r, t in files.items() if r != INDEX and not r.startswith("archive/")}
    idx = files.get(INDEX, "")
    groups = []

    def group(key, title, why, rows, ask):
        if rows:
            ask = ask.replace(", following MEMORY-PROPOSALS.md", "")
            groups.append({"key": key, "title": title, "why": why, "count": len(rows),
                           "rows": rows, "ask": ask + f" Follow {HERE / 'MEMORY-PROPOSALS.md'}: "
                           "one proposal, filed with hygiene_memory.py --propose; don't edit "
                           "memory directly."})

    big = sorted(((r, len(t.encode())) for r, t in live.items()
                  if len(t.encode()) > BIG_BYTES and Path(r).name not in BIG_EXEMPT),
                 key=lambda x: -x[1])
    group("big", "Files over 30 KB",
          "Big files cost context every time they're opened, and most of the bulk is usually "
          "history. A split keeps a short current state and archives the rest verbatim. "
          "reference_people_map.md is exempt (it's generated from contacts).",
          [{"path": r, "label": f"{b // 1024} KB"} for r, b in big],
          "Propose a split (current state + verbatim history) for the largest file "
          "on the Hygiene page's 'Files over 30 KB' list, following MEMORY-PROPOSALS.md.")

    refs = _index_refs(idx)
    group("index-missing", "Index lines pointing at a missing file",
          "The index loads every session; a dead line wastes it and hides a lost memory.",
          [{"path": p, "label": f"MEMORY.md line {i + 1}"} for i, p in refs if p not in files],
          "The Hygiene page lists MEMORY.md lines pointing at missing files. Propose "
          "a fix (repoint or remove each line).")
    indexed = {p for _, p in refs}
    group("unindexed", "Memory files with no index line",
          "A file with no index line is only found by accident.",
          [{"path": r, "label": (_fields(t).get("description") or "")[:120]}
           for r, t in sorted(live.items()) if r not in indexed],
          "Propose index lines for the memory files the Hygiene page lists as "
          "having none.")

    group("links", "Links that resolve to nothing",
          "A [[link]] whose target doesn't exist. Sometimes a deliberate forward reference.",
          [{"path": r, "label": f"[[{l}]]"} for r, l in sorted(_unresolved(files))],
          "Review the unresolved [[links]] on the Hygiene page and propose fixes for "
          "any that aren't deliberate forward references.")

    cutoff = dt.datetime.now().timestamp() - STALE_DAYS * 86400
    base = _root()
    stale = []
    for r, t in sorted(live.items()):
        if _fields(t).get("type") != "project":
            continue
        try:
            m = (base / r).stat().st_mtime
        except Exception:
            continue
        if m < cutoff:
            stale.append({"path": r, "label": "untouched since "
                          + dt.date.fromtimestamp(m).isoformat()})
    group("stale", f"Project memories untouched for {STALE_DAYS}+ days",
          "Project memories go stale. Each is either still true, finished (archive it), or "
          "overtaken (merge or update it).", stale,
          "Review the stale project memories on the Hygiene page and propose, one at "
          "a time, archive / merge / update for any that are no longer current.")

    descs = [(r, (_fields(t).get("description") or "").lower()) for r, t in sorted(live.items())]
    sim = []
    for i in range(len(descs)):
        for j in range(i + 1, len(descs)):
            a, b = descs[i][1], descs[j][1]
            if a and b and difflib.SequenceMatcher(None, a, b).quick_ratio() >= DESC_SIMILAR \
                    and difflib.SequenceMatcher(None, a, b).ratio() >= DESC_SIMILAR:
                sim.append({"path": descs[i][0], "label": "≈ " + descs[j][0]})
    group("similar", "Near-identical descriptions",
          "Two files described almost the same way are often one memory written twice.", sim,
          "Look at the near-identical memory descriptions on the Hygiene page and "
          "propose a merge where they really are the same memory.")

    long_lines = [{"path": INDEX, "label": f"line {i + 1}: {len(l)} chars"}
                  for i, l in enumerate(idx.split("\n"))
                  if l.startswith("- ") and len(l) > INDEX_LINE_MAX]
    group("index-long", f"Index lines over {INDEX_LINE_MAX} characters",
          "MEMORY.md loads in every session. One line per memory, a hook, not a summary.",
          long_lines,
          "Propose shorter hooks for the MEMORY.md lines the Hygiene page lists as too "
          "long; move any detail into the topic files.")

    return {"groups": groups, "files": len(live), "index_bytes": len(idx.encode()),
            "total_bytes": sum(len(t.encode()) for t in live.values())}


# ------------------------------------------------------------------ proposals on disk

def _ppath(pid: str) -> Path:
    if not re.fullmatch(r"m-\d{8}-\d{2,3}", pid or ""):
        raise ValueError("no such proposal")
    return PROPOSALS / f"{pid}.json"


def _load(pid: str) -> dict:
    p = _ppath(pid)
    if not p.exists():
        raise ValueError("no such proposal")
    return json.loads(p.read_text())


def _save(prop: dict) -> None:
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    p = _ppath(prop["id"])
    tmp = p.with_suffix(".json.mt-tmp")
    tmp.write_text(json.dumps(prop, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def _new_id() -> str:
    day = dt.date.today().strftime("%Y%m%d")
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    n = [int(p.stem.rsplit("-", 1)[1]) for p in PROPOSALS.glob(f"m-{day}-*.json")]
    return f"m-{day}-{(max(n) + 1) if n else 1:02d}"


def _touched(ops: list[dict]) -> list[str]:
    out = []
    for o in ops:
        p = o.get("path")
        if p and p not in out:
            out.append(p)
    return out


def _check_draft(d: dict) -> list[str]:
    errs = []
    if not isinstance(d, dict):
        return ["the draft must be a JSON object"]
    if d.get("kind") not in KINDS:
        errs.append(f"kind must be one of {', '.join(KINDS)}")
    for k in ("title", "reason"):
        if not str(d.get(k) or "").strip():
            errs.append(f"{k} is required")
    ops = d.get("ops")
    if not isinstance(ops, list) or not ops:
        errs.append("ops must be a non-empty list")
        return errs
    for i, o in enumerate(ops):
        op, p = o.get("op"), (o.get("path") or "").strip()
        if op not in ("write", "archive", "keep_history"):
            errs.append(f"ops[{i}]: op must be write, archive or keep_history")
        if not p or p.startswith("/") or ".." in p or not p.endswith(".md"):
            errs.append(f"ops[{i}]: path must be a memory-relative .md path")
        if p == INDEX:
            errs.append(f"ops[{i}]: don't write MEMORY.md directly — use index_line / "
                        "index_section on the op; the tool keeps the index")
        if p.startswith("archive/"):
            errs.append(f"ops[{i}]: archive/ is maintained by the tool (use archive or "
                        "keep_history)")
        if op == "write" and not isinstance(o.get("content"), str):
            errs.append(f"ops[{i}]: a write needs the complete file as content")
        if op == "archive" and not (o.get("into") or "").strip() and o.get("into") != "":
            errs.append(f"ops[{i}]: archive needs `into` (the memory its links move to), or "
                        "\"\" if nothing should point anywhere")
    return errs


def propose(draft: dict, by: str = P.ASSISTANT) -> dict:
    errs = _check_draft(draft)
    if errs:
        return {"ok": False, "why": "; ".join(errs)}
    files = _read_all()
    base = {p: (_sha(files[p]) if p in files else None) for p in _touched(draft["ops"])}
    base[INDEX] = _sha(files.get(INDEX, ""))
    prop = {"id": _new_id(), "kind": draft["kind"], "title": draft["title"].strip(),
            "reason": draft["reason"].strip(), "conflicts": draft.get("conflicts") or [],
            "people": draft.get("people") or [], "ops": draft["ops"], "base": base,
            "status": "pending", "created_at": _now(), "created_by": by,
            "history": [{"at": _now(), "by": by, "what": "proposed"}]}
    _save(prop)
    pl = plan(prop["id"])
    return {"ok": True, "id": prop["id"], "errors": pl.get("errors", []),
            "warnings": pl.get("warnings", [])}


def revise(pid: str, draft: dict, by: str = P.ASSISTANT) -> dict:
    prop = _load(pid)
    if prop["status"] not in ("revise", "pending", "revised"):
        return {"ok": False, "why": f"proposal is {prop['status']} — file a new one instead"}
    errs = _check_draft(draft)
    if errs:
        return {"ok": False, "why": "; ".join(errs)}
    files = _read_all()
    prop.update(kind=draft["kind"], title=draft["title"].strip(),
                reason=draft["reason"].strip(), conflicts=draft.get("conflicts") or [],
                people=draft.get("people") or [], ops=draft["ops"])
    prop["base"] = {p: (_sha(files[p]) if p in files else None) for p in _touched(prop["ops"])}
    prop["base"][INDEX] = _sha(files.get(INDEX, ""))
    prop["status"] = "revised"
    prop["history"].append({"at": _now(), "by": by, "what": "revised",
                            "summary": (draft.get("summary") or "").strip()[:2000]})
    _save(prop)
    return {"ok": True, "id": pid}


def list_proposals() -> list[dict]:
    if not PROPOSALS.exists():
        return []
    out = []
    for p in sorted(PROPOSALS.glob("m-*.json"), reverse=True):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        notes = [h for h in d.get("history", []) if h.get("notes")]
        out.append({"id": d["id"], "kind": d["kind"], "title": d["title"],
                    "reason": d["reason"], "status": d["status"],
                    "created_at": d.get("created_at"), "files": _touched(d["ops"]),
                    "notes": notes[-1]["notes"] if notes else None,
                    "log_id": d.get("log_id")})
    out.sort(key=lambda d: (d["status"] not in OPEN_STATES, d["status"] != "revise",
                            d["created_at"] or ""), reverse=False)
    return out


def open_count() -> int:
    return sum(1 for p in list_proposals() if p["status"] in OPEN_STATES)


# ------------------------------------------------------------------ the plan (preview)

def _unique(files: dict, rel: str) -> str:
    if rel not in files:
        return rel
    stem, n = rel[:-3], 2
    while f"{stem}-{n}.md" in files:
        n += 1
    return f"{stem}-{n}.md"


def _history_copy(text: str, day: str) -> str:
    """The old file, byte for byte, with only two front-matter lines changed so it can
    never be mistaken for the live memory: name gets `-history`, description says so."""
    raw, fields, body = D.split_front(text)
    if not raw:
        return text
    name = fields.get("name") or ""
    desc = fields.get("description") or ""
    raw = _NAME_LINE.sub(lambda m: f"name: {name}-history", raw, count=1)
    raw = _DESC_LINE.sub(lambda m: "description: " + json.dumps(
        f"HISTORY (verbatim, archived {day}) — {desc}", ensure_ascii=False), raw, count=1)
    return raw + body


def _repoint(text: str, olds: set[str], new: str) -> str:
    def sub(m):
        return f"[[{new}{m.group(2)}]]" if m.group(1).strip() in olds else m.group(0)
    return LINK_RE.sub(sub, text)


def _sections(index_text: str) -> list[str]:
    return [l[3:].strip() for l in index_text.split("\n") if l.startswith("## ")]


def _index_put(index_text: str, rel: str, line: str, section: str | None) -> tuple[str, str | None]:
    lines = index_text.split("\n")
    hit = [i for i, l in enumerate(lines) if f"]({rel})" in l]
    if hit:
        lines[hit[0]] = line
        return "\n".join(lines), None
    if not section:
        return index_text, f"{rel} is new, so it needs index_section (one of: " \
                           f"{', '.join(_sections(index_text))})"
    try:
        h = next(i for i, l in enumerate(lines) if l.startswith("## ") and l[3:].strip() == section)
    except StopIteration:
        return index_text, f"no '## {section}' section in MEMORY.md"
    end = h + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    while end > h + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, line)
    return "\n".join(lines), None


def _contact_names() -> list[str]:
    try:
        conn = TR._connect()
        if conn is None:
            return []
        try:
            rs = conn.execute("SELECT name FROM contacts WHERE COALESCE(name,'') LIKE '% %' "
                              "AND merged_into IS NULL AND archived_at IS NULL").fetchall()
        finally:
            conn.close()
        return sorted({r["name"].strip() for r in rs if len(r["name"].strip()) > 5})
    except Exception:
        return []


def _side_by_side(a: str, b: str, ctx: int = 3) -> list[dict]:
    """Aligned rows for a two-column diff. Long unchanged runs collapse to one 'skip' row."""
    al, bl = (a or "").split("\n"), (b or "").split("\n")
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, al, bl, autojunk=False).get_opcodes():
        if tag == "equal":
            n = i2 - i1
            if n > ctx * 2 + 1:
                for k in range(ctx):
                    rows.append({"t": "eq", "l": al[i1 + k], "r": bl[j1 + k], "ln": i1 + k + 1})
                rows.append({"t": "skip", "n": n - ctx * 2})
                for k in range(n - ctx, n):
                    rows.append({"t": "eq", "l": al[i1 + k], "r": bl[j1 + k], "ln": i1 + k + 1})
            else:
                for k in range(n):
                    rows.append({"t": "eq", "l": al[i1 + k], "r": bl[j1 + k], "ln": i1 + k + 1})
        else:
            L, R = al[i1:i2], bl[j1:j2]
            for k in range(max(len(L), len(R))):
                rows.append({"t": tag, "l": L[k] if k < len(L) else None,
                             "r": R[k] if k < len(R) else None,
                             "ln": i1 + k + 1 if k < len(L) else None})
    return rows


def plan(pid: str) -> dict:
    """What applying would do, computed on a virtual copy. Writes nothing."""
    try:
        prop = _load(pid)
    except Exception as e:
        return {"ok": False, "why": str(e)}
    before = _read_all()
    after = dict(before)
    errors, warnings, flags = [], [], []
    changes: dict[str, dict] = {}      # rel -> {"action", "from"?}
    day = dt.date.today().isoformat()

    for p, h in (prop.get("base") or {}).items():
        cur = _sha(before[p]) if p in before else None
        if cur != h:
            what = "was created" if h is None else ("was removed" if cur is None else "changed")
            (errors if p != INDEX else warnings).append(
                f"{p} {what} since it was proposed"
                + (" — ask your assistant to re-base it (--revise)" if p != INDEX else
                   " (the index is re-read at apply, so this is only a notice)"))

    idx = after.get(INDEX, "")
    redirects: list[tuple[set, str, str]] = []
    rewritten, kept_whole = set(), set()
    for i, o in enumerate(prop["ops"]):
        op, p = o["op"], o["path"]
        if op == "write":
            text = o["content"]
            f = _fields(text)
            if not f.get("name") or not f.get("description"):
                errors.append(f"{p}: the new content needs name: and description: front matter")
            if f.get("type") not in D.MEMORY_TYPES:
                errors.append(f"{p}: type must be one of {', '.join(D.MEMORY_TYPES)}")
            if f.get("name"):
                owner = _names({k: v for k, v in after.items() if k != p}).get(f["name"])
                if owner and owner != p and not owner.startswith("archive/"):
                    errors.append(f"{p}: name '{f['name']}' is already used by {owner}")
            if p in before:
                rewritten.add(p)
            changes[p] = {"action": "modify" if p in before else "create"}
            after[p] = text
            if o.get("index_line"):
                idx, err = _index_put(idx, p, o["index_line"].strip(), o.get("index_section"))
                if err:
                    errors.append(err)
            elif p not in before:
                errors.append(f"{p} is new, so it needs an index_line")
        elif op == "keep_history":
            if p not in before:
                errors.append(f"{p}: keep_history needs an existing file")
                continue
            dest = _unique(after, f"archive/{Path(p).stem}_history.md")
            after[dest] = _history_copy(before[p], day)
            changes[dest] = {"action": "create", "from": p, "history": True}
            kept_whole.add(p)
        elif op == "archive":
            if p not in after:
                errors.append(f"{p}: nothing to archive")
                continue
            dest = _unique(after, f"archive/{Path(p).name}")
            after[dest] = after.pop(p)
            changes[p] = {"action": "move", "to": dest}
            changes[dest] = {"action": "moved-here", "from": p}
            kept_whole.add(p)
            f = _fields(before.get(p, ""))
            olds = _variants(f.get("name") or "") | _variants(Path(p).stem)
            into = (o.get("into") or "").strip()
            redirects.append((olds, into, p))
            idx = "\n".join(l for l in idx.split("\n") if f"]({p})" not in l)

    # Repoint links that named a moved file — in every live file, after the writes above.
    link_changes = 0
    for olds, into, src in redirects:
        if not into:
            continue
        for rel in list(after):
            if rel == INDEX or rel.startswith("archive/"):
                continue
            new = _repoint(after[rel], olds, into)
            if new != after[rel]:
                n = sum(1 for l in _links(after[rel]) if l in olds)
                link_changes += n
                after[rel] = new
                changes.setdefault(rel, {"action": "modify"})
                changes[rel]["links"] = changes[rel].get("links", 0) + n
    if idx != after.get(INDEX, ""):
        after[INDEX] = idx
        changes[INDEX] = {"action": "modify"}

    names_after = _names(after)
    for olds, into, src in redirects:
        if into and into not in names_after:
            errors.append(f"archive {src}: `into` '{into}' doesn't resolve to any memory")

    # A link that reached a LIVE memory before and would reach archive/ after is the silent
    # drift this guards against. A link written on purpose to a new -history copy is not.
    nb, na = _names(before), _names(after)
    arch = sorted({(r, l) for r, t in after.items()
                   if r != INDEX and not r.startswith("archive/")
                   for l in _links(t)
                   if (na.get(l) or "").startswith("archive/")
                   and nb.get(l) and not nb[l].startswith("archive/")})
    if arch:
        warnings.append(f"{len(arch)} link(s) would point into archive/ instead of a live "
                        "memory: " + ", ".join(f"[[{l}]] in {r}" for r, l in arch[:8])
                        + (" …" if len(arch) > 8 else "")
                        + " — give the archive op an `into` to repoint them")

    broke = sorted(_unresolved(after) - _unresolved(before))
    for rel, l in broke:
        errors.append(f"{rel} would have a broken link [[{l}]]")
    for i, ref in _index_refs(after.get(INDEX, "")):
        if ref not in after:
            errors.append(f"MEMORY.md would point at a missing file: {ref}")

    # Nothing silently dropped: every line of a rewritten file must survive somewhere, unless
    # the whole old file is kept (archive / keep_history). Contradiction fixes drop lines on
    # purpose — they're listed so the user sees exactly what goes.
    # A line whose wording changed but that still has a close counterpart in the same
    # file's new version is REWORDED — visible in the diff, not lost. Counting it as lost
    # would put a false alarm on every edit, and a warning that always fires stops being read.
    everything = "\n".join(after.values())
    lost, reworded = {}, {}
    for p in rewritten - kept_whole:
        new_lines = [l.strip() for l in after.get(p, "").split("\n") if len(l.strip()) > 3]
        gone, moved = [], 0
        for l in before[p].split("\n"):
            t = l.strip()
            if len(t) <= 3 or t in everything:
                continue
            if any(difflib.SequenceMatcher(None, t, n).quick_ratio() >= REWORD_SIMILAR
                   and difflib.SequenceMatcher(None, t, n).ratio() >= REWORD_SIMILAR
                   for n in new_lines):
                moved += 1
            else:
                gone.append(l)
        if moved:
            reworded[p] = moved
        if gone:
            lost[p] = gone
            warnings.append(f"{p}: {len(gone)} line(s) leave your assistant's memory entirely "
                            "(not kept anywhere, not in a history file)")

    # Person Verification Protocol: a name that disappears from memory is flagged.
    removed = "\n".join(before[p] for p in rewritten if p in before)
    kept = "\n".join(t for r, t in after.items() if not r.startswith("archive/"))
    for n in _contact_names():
        if n in removed and n not in kept:
            flags.append(f"“{n}” disappears from live memory")
    for n in prop.get("people") or []:
        flags.append(f"Your assistant says this changes a person: {n}")

    files = []
    for rel in sorted(changes, key=lambda r: (r == INDEX, r.startswith("archive/"), r)):
        c = changes[rel]
        if c["action"] == "moved-here":
            continue
        entry = {"path": rel, **c}
        if c["action"] == "move":
            entry["rows"] = []
        elif c.get("history"):
            entry["bytes"] = len(after[rel].encode())
            entry["rows"] = []
        else:
            entry["rows"] = _side_by_side(before.get(rel, ""), after.get(rel, ""))
            entry["bytes_before"] = len(before.get(rel, "").encode())
            entry["bytes_after"] = len(after.get(rel, "").encode())
        entry["editable"] = any(o["op"] == "write" and o["path"] == rel for o in prop["ops"])
        if entry["editable"]:
            entry["content"] = after[rel]
        if rel in lost:
            entry["lost"] = lost[rel]
        if rel in reworded:
            entry["reworded"] = reworded[rel]
        files.append(entry)

    digest = _sha(json.dumps({"ops": prop["ops"],
                              "after": {r: _sha(after[r]) for r in sorted(changes) if r in after},
                              "before": {r: _sha(before.get(r, "")) for r in sorted(changes)}},
                             sort_keys=True))[:16]
    return {"ok": True, "id": pid, "proposal": {k: prop[k] for k in (
                "id", "kind", "title", "reason", "conflicts", "people", "status",
                "created_at", "created_by", "history")},
            "files": files, "errors": errors, "warnings": warnings, "flags": flags,
            "summary": {"written": sum(1 for c in changes.values() if c["action"] in ("modify", "create")),
                        "created": sum(1 for c in changes.values() if c["action"] == "create"),
                        "archived": sum(1 for c in changes.values() if c["action"] == "move"),
                        "links": link_changes, "index": INDEX in changes},
            "sha": digest, "_after": after, "_before": before, "_changes": changes}


def plan_public(pid: str) -> dict:
    p = plan(pid)
    for k in ("_after", "_before", "_changes"):
        p.pop(k, None)
    return p


# ------------------------------------------------------------------ the user's actions

def edit(pid: str, path: str, content: str) -> dict:
    """The user edits the proposed text of one file before approving."""
    prop = _load(pid)
    if prop["status"] not in OPEN_STATES:
        return {"ok": False, "why": f"proposal is {prop['status']}"}
    if not isinstance(content, str) or not content.strip():
        return {"ok": False, "why": "nothing to save"}
    hit = [o for o in prop["ops"] if o["op"] == "write" and o["path"] == path]
    if not hit:
        return {"ok": False, "why": "that file isn't one this proposal writes"}
    if hit[0]["content"] == content:
        return {"ok": True, "unchanged": True}
    hit[0]["content"] = content
    hit[0]["edited_by"] = "you"
    prop["history"].append({"at": _now(), "by": "you", "what": f"edited {path}"})
    _save(prop)
    return {"ok": True}


def send_back(pid: str, notes: str) -> dict:
    prop = _load(pid)
    if prop["status"] not in OPEN_STATES:
        return {"ok": False, "why": f"proposal is {prop['status']}"}
    notes = (notes or "").strip()
    if not notes:
        return {"ok": False, "why": "write what you want changed — the notes are the point"}
    prop["status"] = "revise"
    prop["history"].append({"at": _now(), "by": "you", "what": "sent back",
                            "notes": notes[:4000]})
    _save(prop)
    return {"ok": True, "line": paste_line(pid)}


def reject(pid: str, notes: str = "") -> dict:
    prop = _load(pid)
    if prop["status"] not in OPEN_STATES + ("revise",):
        return {"ok": False, "why": f"proposal is {prop['status']}"}
    prop["status"] = "rejected"
    prop["history"].append({"at": _now(), "by": "you", "what": "rejected",
                            "notes": (notes or "").strip()[:4000] or None})
    _save(prop)
    return {"ok": True}


def paste_line(pid: str) -> str:
    return (f"Memory proposal {pid} was sent back with notes. Run "
            f"`python3 {HERE / 'hygiene_memory.py'} --list` to read them, revise it, and "
            f"resubmit with --revise {pid} (see {HERE / 'MEMORY-PROPOSALS.md'}).")


# ------------------------------------------------------------------ apply / undo

def _write_raw(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".mt-tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def _db_log(conn_fn, sql, args):
    conn = conn_fn()
    try:
        import hygiene as HY
        HY.ensure_tables(conn)
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def apply(pid: str, sha: str, by: str = "mt") -> dict:
    pl = plan(pid)
    if not pl.get("ok"):
        return pl
    prop = _load(pid)
    if prop["status"] not in OPEN_STATES:
        return {"ok": False, "why": f"proposal is {prop['status']}"}
    if pl["sha"] != sha:
        return {"ok": False, "why": "the memory files or the proposal changed since you "
                                    "opened it. Reload and review it again."}
    if pl["errors"]:
        return {"ok": False, "why": "this proposal has errors — " + "; ".join(pl["errors"])}

    base = _root()
    before, after, changes = pl["_before"], pl["_after"], pl["_changes"]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = BACKUPS / f"{stamp}-{pid}"
    (bdir / "pre").mkdir(parents=True, exist_ok=True)
    (bdir / "post").mkdir(parents=True, exist_ok=True)
    tar = bdir / "memory-before.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        tf.add(str(base), arcname="memory")

    created, modified, moved = [], [], []
    for rel, c in changes.items():
        if c["action"] in ("modify", "move") and rel in before:
            dst = bdir / "pre" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(before[rel])
    try:
        for rel, c in changes.items():
            if c["action"] == "move":
                src, dest = base / rel, base / c["to"]
                if dest.exists():
                    raise RuntimeError(f"{c['to']} already exists")
                D._snapshot("memory", rel, src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dest)
                moved.append([rel, c["to"]])
        for rel, c in changes.items():
            if c["action"] == "modify":
                r = D.write_file("memory", rel, after[rel])
                if not r.get("ok"):
                    raise RuntimeError(f"{rel}: {r.get('why')}")
                modified.append(rel)
            elif c["action"] == "create":
                p = base / rel
                if p.exists():
                    raise RuntimeError(f"{rel} already exists")
                _write_raw(p, after[rel])
                created.append(rel)
        for rel in created + modified + [m[1] for m in moved]:
            dst = bdir / "post" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text((base / rel).read_text())
        now = _read_all()
        broke = _unresolved(now) - _unresolved(before)
        if broke:
            raise RuntimeError("links broke after writing: "
                               + ", ".join(f"[[{l}]] in {r}" for r, l in sorted(broke)))
    except Exception as e:
        _rollback(base, bdir, created, modified, moved)
        return {"ok": False, "why": f"nothing was changed — rolled back ({e})",
                "backup": str(tar)}

    record = {"created": created, "modified": modified, "moved": moved, "dir": str(bdir),
              "post_sha": {r: _sha((bdir / "post" / r).read_text())
                           for r in created + modified + [m[1] for m in moved]}}
    log_id = _db_log(TR._connect_w,
                     "INSERT INTO hygiene_log (area, group_key, title, choice, rows_json, n, "
                     "backup, applied_at, applied_by) VALUES ('memory',?,?,?,?,?,?,?,?)",
                     (pid, prop["title"], prop["kind"], json.dumps(record),
                      len(created) + len(modified) + len(moved), str(tar), TR._now(), by))
    prop["status"] = "applied"
    prop["log_id"] = log_id
    prop["history"].append({"at": _now(), "by": "you", "what": "approved and applied",
                            "log_id": log_id})
    _save(prop)
    return {"ok": True, "log_id": log_id, "backup": str(tar), "created": created,
            "modified": modified, "moved": moved}


def _rollback(base: Path, bdir: Path, created, modified, moved) -> None:
    for rel in modified:
        pre = bdir / "pre" / rel
        if pre.exists():
            _write_raw(base / rel, pre.read_text())
    for rel in created:
        try:
            (base / rel).unlink()     # it did not exist before this apply
        except FileNotFoundError:
            pass
    for src, dest in reversed(moved):
        if (base / dest).exists() and not (base / src).exists():
            os.rename(base / dest, base / src)


def undo(log_id, by: str = "mt") -> dict:
    try:
        log_id = int(log_id)
    except (TypeError, ValueError):
        return {"ok": False, "why": "no such log entry"}
    conn = TR._connect()
    try:
        e = conn.execute("SELECT * FROM hygiene_log WHERE id=? AND area='memory'",
                         (log_id,)).fetchone() if conn else None
    except sqlite3.OperationalError:
        e = None
    finally:
        if conn:
            conn.close()
    if not e:
        return {"ok": False, "why": "no such log entry"}
    if e["undone_at"]:
        return {"ok": False, "why": f"already undone at {e['undone_at']}"}
    rec = json.loads(e["rows_json"])
    base, bdir = _root(), Path(rec["dir"])
    moved_edit = []
    for rel, h in rec["post_sha"].items():
        p = base / rel
        if not p.exists() or _sha(p.read_text()) != h:
            moved_edit.append(rel)
    if moved_edit:
        return {"ok": False, "why": "nothing was undone — edited since the apply: "
                                    + ", ".join(moved_edit)
                                    + f". The full before-copy is {e['backup']}"}
    for rel in rec["modified"]:
        D._snapshot("memory", rel, base / rel)
    _rollback(base, bdir, rec["created"], rec["modified"], rec["moved"])
    _db_log(TR._connect_w, "UPDATE hygiene_log SET undone_at=?, undone_by=? WHERE id=?",
            (TR._now(), by, log_id))
    try:
        prop = _load(e["group_key"])
        prop["status"] = "undone"
        prop["history"].append({"at": _now(), "by": "you", "what": "undone",
                                "log_id": log_id})
        _save(prop)
    except Exception:
        pass
    return {"ok": True, "n": e["n"]}


def log(limit: int = 50) -> dict:
    conn = TR._connect()
    if conn is None:
        return {"log": []}
    try:
        rs = conn.execute("SELECT id, group_key, title, choice, n, backup, applied_at, "
                          "applied_by, undone_at FROM hygiene_log WHERE area='memory' "
                          "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.OperationalError:
        return {"log": []}
    finally:
        conn.close()
    return {"log": [{k: r[k] for k in r.keys()} for r in rs]}


def overview() -> dict:
    """GET /api/hygiene/memory — findings + proposals, for the tab."""
    f = findings()
    props = list_proposals()
    return {"findings": f.get("groups", []), "stats": {k: f.get(k) for k in (
                "files", "index_bytes", "total_bytes")},
            "proposals": props, "open": sum(1 for p in props if p["status"] in OPEN_STATES),
            "error": f.get("error")}


# ------------------------------------------------------------------ CLI (the assistant's side)

def _main(argv: list[str]) -> int:
    if not argv or argv[0] == "--findings":
        for g in findings().get("groups", []):
            print(f"{g['count']:4}  {g['title']}")
            for r in g["rows"][:12]:
                print(f"        {r['path']}  {r['label']}")
        return 0
    if argv[0] == "--list":
        for p in list_proposals():
            print(f"{p['id']}  [{p['status']}]  {p['kind']}: {p['title']}")
            if p["notes"]:
                print(f"    Notes: {p['notes']}")
        return 0
    if argv[0] == "--propose" and len(argv) == 2:
        r = propose(json.loads(Path(argv[1]).read_text()))
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    if argv[0] == "--revise" and len(argv) == 3:
        r = revise(argv[1], json.loads(Path(argv[2]).read_text()))
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    if argv[0] == "--plan" and len(argv) == 2:
        p = plan_public(argv[1])
        if not p.get("ok"):
            print(p.get("why"))
            return 1
        print(json.dumps({k: p[k] for k in ("summary", "errors", "warnings", "flags")},
                         indent=2))
        for f in p["files"]:
            print(f"  {f['action']:8} {f['path']}"
                  + (f" -> {f['to']}" if f.get("to") else "")
                  + (f"  ({f['links']} links)" if f.get("links") else ""))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

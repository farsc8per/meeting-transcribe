#!/usr/bin/env python3
"""
weekly.py — the data layer for the mt weekly-update page (R2, phase 5).

A weekly update is ONE markdown file per account per week, holding both published
versions (the team's weekly-update playbook):

    # <Account> — Weekly Update, <date>
    *(Note for you — …)*                   <- never posted
    ---
    ## Jira version (full — post to TAM-###)
    **RAG status:** 🟡 Amber — **Reason code:** RENWL
    …summary / This Week / Open Items…
    ---
    ## Slack version (short — post to #channel, C0…)
    **<Account> — Weekly Update, <date>** · 🟡 Amber · RENWL
    …summary / Open Items / Full detail link…
    ---
    *Mentions to substitute…*              <- never posted

What this module owns:

  * PARSING and STRIPPING. post_bodies() returns exactly what goes to Jira and to Slack:
    each section runs from its heading to its first `---` line or the next `##`
    heading. That one rule drops the Note block, the mentions footer, and a stray
    Note section under its own `##` heading — verified on 39 files in the current
    format.
  * THE RAG LINE. RAG is a validated pick-list here, not free text. On save the tool
    REWRITES the Jira `**RAG status:**` line and the Slack title suffix from the two
    fields, so the file can never disagree with what gets set in Jira — and the
    italic asides the playbook banned on Aug 7 cannot survive a save.
  * THE DB ROW. record_posted() writes `weekly_updates` with the five phase-5 columns
    and links the file through `external_links` (role 'weekly_update'). Posting is not
    done until this row exists (the playbook's rule, dropped more than once).
  * PATH A. handoff() writes `<file>.handoff.json` next to the update: everything
    the assistant needs to post it — option ids included — and nothing it has to
    re-derive.

What it does NOT do: touch the network. Posting directly (Path B) is phase 6 and lives
in poster.py, the only module that will hold secrets.

Stdlib only, so the assistant can run the CLI with a plain `python3`:

    python3 weekly.py --board [YYYY-MM-DD]
    python3 weekly.py --pending                  # waiting on the assistant: post / revise
    python3 weekly.py --revised <path> [--summary "what changed"]
    python3 weekly.py --body <path> jira|slack    # the exact text to post
    python3 weekly.py --parse Reports/weekly-updates/2026/09/2026-09-17-acme-update.md
    python3 weekly.py --record <path> --jira-comment <id|url> [--slack-ts <ts|permalink>]
                      [--by <assistant>]
    python3 weekly.py --migrate /tmp/copy.db
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import docs as D                     # noqa: E402  (paths, confinement, the version vault)
import paths as P                    # noqa: E402  (accounts, Jira site)
import tracking as TR                # noqa: E402  (DB connection, customers, links)

# The cached Jira option lists for the two RAG fields, plus optional "_reason_help"
# (reason code -> one-line definition, shown as help on the page). Your company's own
# data, so it lives in your workspace, not in the install. Before 0.7.4 it lived in the
# install; that copy is still read until the workspace one exists.
_FIELDS_HOME = P.WORKSPACE / "DB" / "jira_fields.json"
_FIELDS_OLD = HERE / "jira_fields.json"
FIELDS_PATH = Path(os.environ.get("MT_JIRA_FIELDS")
                   or (_FIELDS_OLD if _FIELDS_OLD.exists() and not _FIELDS_HOME.exists()
                       else _FIELDS_HOME))
WEEKLY_PREFIX = "Reports/weekly-updates/"
HEALTH_FIELD = "customfield_13472"
REASON_FIELD = "customfield_18328"
JIRA_BASE = f"{P.JIRA_SITE}/browse/"

RAG = ["Green", "Amber", "Red"]
EMOJI = {"Green": "🟢", "Amber": "🟡", "Red": "🔴"}
GREEN_CODE = "GREEN"
SLACK_LIMIT = 5000
SLACK_WARN = 4500

# Each account's Slack channel lives in its customers row: slack_internal holds
# '#channel-name (<channel id>)'. Ids, not names: the Web API needs the id, and
# `weekly_updates.slack_channel` has held three formats (id / #name / both). New rows write
# the id. The file's own heading is checked against it, which is what catches an update
# pasted into the wrong account's template.
# How the account reads in a Slack title: config.json accounts.<slug>.display, else
# customers.name. Old filename slugs that mean an account: accounts.<slug>.file_aliases.
DISPLAY = {s: a["display"] for s, a in P.ACCOUNTS.items() if a["display"]}
SLUG_ALIASES = {old: s for s, a in P.ACCOUNTS.items() for old in a["file_aliases"]}

# Columns ensure_schema() adds. Nullable, no defaults — adding them rewrites no rows.
NEW_COLUMNS = [
    ("rag_status", "TEXT"),        # Green | Amber | Red
    ("rag_reason_code", "TEXT"),   # the Jira option value, verbatim
    ("jira_comment_id", "TEXT"),   # what the Slack focusedCommentId link needs
    ("slack_ts", "TEXT"),          # proof the Slack post landed
    ("posted_by", "TEXT"),         # 'mt' | the assistant's name (paths.ASSISTANT)
    ("edited_at", "TEXT"),         # UTC, last "Correct posted update" (phase 6)
]
POSTERS = ("mt", P.ASSISTANT)

# The private block that is never posted. Drafts write "(Note for you"; older ones named the
# owner, so any one word after "for" counts.
_NOTE_RE = re.compile(r"\(Note for \w+")
_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)-update(-NOT-POSTED)?\.md$")
_RAG_LINE = re.compile(r"^\*\*(?:RAG|Stoplight) status:\*\*.*$", re.M)
_RAG_PARSE = re.compile(
    r"^\*\*(?:RAG|Stoplight) status:\*\*\s*(?:🟢|🟡|🔴)?\s*(Green|Amber|Red)\b\s*[—-]*\s*"
    r"(?:\*\*Reason code:\*\*\s*(.*))?$")
# Renamed "Stoplight Report" Oct 1 2026; older files say RAG / Weekly Update.
_RAG_PREFIXES = ("**RAG status:**", "**Stoplight status:**")
_SLACK_TITLE = re.compile(r"^\*\*([^*\n]*(?:Weekly Update|Stoplight Report)[^*\n]*)\*\*(.*)$")
_PLACEHOLDER = "focusedCommentId=<id>"
# Every spelling the drafts have used for "id goes here": <id>, <PENDING>, <TBD>, <ID_TBD>.
_PLACEHOLDER_RE = re.compile(r"focusedCommentId=<[A-Za-z_]{1,20}>")
_FILLED_RE = re.compile(r"focusedCommentId=(\d+)")


def has_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER_RE.search(text or ""))


def fill_comment_id(text: str, cid: str) -> str:
    """Put the posted comment's id into the Slack 'Full detail' link."""
    return _PLACEHOLDER_RE.sub(f"focusedCommentId={cid}", text or "")


# ------------------------------------------------------------------ small helpers

def _today() -> dt.date:
    s = os.environ.get("MT_TODAY")          # tests pin the date
    if s:
        try:
            return dt.date.fromisoformat(s)
        except ValueError:
            pass
    return dt.date.today()


def _date(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def week_bounds(day) -> tuple[dt.date, dt.date]:
    """Monday..Sunday of the week holding `day`."""
    d = day if isinstance(day, dt.date) else (_date(day) or _today())
    mon = d - dt.timedelta(days=d.weekday())
    return mon, mon + dt.timedelta(days=6)


def week_of(day=None) -> str:
    """The Thursday of that week — the date new drafts carry. Updates have gone out on
    Thursday since Aug 2026; earlier ones were Fridays, which the board still finds
    because it looks across the whole Monday–Sunday week."""
    mon, _ = week_bounds(day or _today())
    return (mon + dt.timedelta(days=3)).isoformat()


def _fmt_long(d: dt.date) -> str:
    return f"{d.strftime('%b')} {d.day} {d.year}"


def _fmt_short(d: dt.date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _customers() -> dict:
    """short_name -> {id, name, short, jira}. Never raises."""
    conn = TR._connect()
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT id, name, short_name, jira_issue_key, status, slack_internal FROM customers "
            "ORDER BY id").fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        if not r["short_name"]:
            continue
        out[r["short_name"]] = {"id": r["id"], "name": r["name"], "short": r["short_name"],
                                "jira": r["jira_issue_key"], "status": r["status"],
                                "slack_internal": r["slack_internal"]}
    return out


# Statuses the weekly board shows. 'test' is a practice account:
# on the board, postable like any other, but kept out of anything that looks for 'active'.
BOARD_STATUSES = ("active", "test")
_CHAN_ID = re.compile(r"\b([CDG][A-Z0-9]{8,12})\b")


def channel_id(short: str) -> str | None:
    """The account's internal Slack channel id: the `(C…)` id written into
    customers.slack_internal — so a new account needs a DB row, not a code change."""
    m = _CHAN_ID.search((_customers().get(short) or {}).get("slack_internal") or "")
    return m.group(1) if m else None


def channel_name(short: str) -> str | None:
    v = ((_customers().get(short) or {}).get("slack_internal") or "").split("(")[0].strip()
    return v or None


def name_parts(rel: str) -> dict | None:
    """{'date','slug','short','not_posted'} from a weekly-update filename, or None."""
    m = _NAME_RE.match(Path(rel or "").name)
    if not m:
        return None
    slug = m.group(2)
    return {"date": m.group(1), "slug": slug, "short": SLUG_ALIASES.get(slug, slug),
            "not_posted": bool(m.group(3))}


def _check_rel(rel: str) -> tuple[Path | None, dict | None, str | None]:
    rel = (rel or "").strip().lstrip("/")
    if not rel.startswith(WEEKLY_PREFIX):
        return None, None, "that is not a weekly-update file"
    parts = name_parts(rel)
    if parts is None:
        return None, None, ("weekly-update files are named "
                            "YYYY-MM-DD-<account>-update.md")
    p = D.resolve("workspace", rel)
    if p is None:
        return None, None, "that path is not editable from here"
    return p, parts, None


# ------------------------------------------------------------------ the Jira cache

def jira_fields() -> dict:
    """The cached option lists, shaped for the page. Never raises.

    health:  [{'id','value','status'}]   status is Green/Amber/Red, read off the value
    reasons: [{'id','value','help','green'}]
    """
    out = {"health": [], "reasons": [], "resynced_at": None, "path": str(FIELDS_PATH),
           "error": None}
    try:
        raw = json.loads(FIELDS_PATH.read_text())
    except FileNotFoundError:
        out["error"] = (f"{FIELDS_PATH.name} is missing — RAG cannot be validated "
                        "until it exists")
        return out
    except Exception as e:
        out["error"] = f"{FIELDS_PATH.name} could not be read ({type(e).__name__}: {e})"
        return out
    out["resynced_at"] = raw.get("_resynced_at")
    # Help text only — the VALID set is the options below, so a code Jira adds later
    # works before anyone writes a definition for it.
    helps = raw.get("_reason_help") or {}
    for o in (raw.get(HEALTH_FIELD) or {}).get("options") or []:
        m = re.search(r"\b(Green|Amber|Red)\b", str(o.get("value") or ""))
        if m and o.get("id"):
            out["health"].append({"id": str(o["id"]), "value": o["value"],
                                  "status": m.group(1)})
    for o in (raw.get(REASON_FIELD) or {}).get("options") or []:
        v = str(o.get("value") or "").strip()
        if v and o.get("id"):
            out["reasons"].append({"id": str(o["id"]), "value": v,
                                   "help": str(helps.get(v) or ""),
                                   "green": v == GREEN_CODE})
    if not out["health"] or not out["reasons"]:
        out["error"] = f"{FIELDS_PATH.name} has no options for one of the two RAG fields"
    return out


def _match_reason(code: str, reasons: list[dict]) -> dict | None:
    """The option for a reason code as a file spells it. Exact first; then the one
    spelling drift the files really carry — `PRJCT - Pending SC` written without the
    `//` Jira has."""
    c = (code or "").strip()
    for r in reasons:
        if r["value"] == c:
            return r
    for r in reasons:
        if r["value"].rstrip("/").strip().lower() == c.rstrip("/").strip().lower():
            return r
    return None


def validate_rag(status: str, code: str, fields: dict | None = None) -> tuple[dict | None, str | None]:
    """Resolve a (status, reason code) pair to the two Jira options, or say why not.

    The pairing rule is the framework's: GREEN means "healthy, no reason needed", so it
    goes with Green and only Green; Amber and Red need a real reason."""
    f = fields or jira_fields()
    if f["error"]:
        return None, f["error"]
    if status not in RAG:
        return None, f"RAG status must be one of: {', '.join(RAG)}"
    health = next((h for h in f["health"] if h["status"] == status), None)
    if health is None:
        return None, f"{FIELDS_PATH.name} has no Customer Health option for {status}"
    reason = next((r for r in f["reasons"] if r["value"] == (code or "").strip()), None)
    if reason is None:
        return None, ("reason code must be one of: "
                      + ", ".join(r["value"] for r in f["reasons"]))
    if status == "Green" and not reason["green"]:
        return None, f"Green takes the {GREEN_CODE} reason code — no reason is needed"
    if status != "Green" and reason["green"]:
        return None, f"{status} needs a real reason code, not {GREEN_CODE}"
    return {"status": status, "code": reason["value"], "health": health,
            "reason": reason}, None


# ------------------------------------------------------------------ parsing

def sections(text: str) -> dict:
    """{'jira': {...}, 'slack': {...}} — each {'heading','body','start','end'} where
    start/end are LINE indexes of the body. A section runs from its heading to its first
    `---` line or the next `## ` heading, whichever comes first."""
    lines = (text or "").split("\n")
    out = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        key = ("jira" if ln.startswith("## Jira") else
               "slack" if ln.startswith("## Slack") else None)
        if key and key not in out:
            j = i + 1
            while j < len(lines) and lines[j].strip() != "---" \
                    and not lines[j].startswith("## "):
                j += 1
            out[key] = {"heading": ln, "start": i + 1, "end": j,
                        "body": "\n".join(lines[i + 1:j]).strip()}
            i = j
            continue
        i += 1
    return out


def _parse_rag_line(line: str) -> tuple[str | None, str | None]:
    m = _RAG_PARSE.match((line or "").strip())
    if not m:
        return None, None
    code = (m.group(2) or "").strip()
    # Drop an italic aside and a "— no reason needed" style tail. Codes contain " - "
    # (hyphen) but never " — " (em dash) or "*(".
    code = re.split(r"\s+\*\(|\s+—\s+", code, maxsplit=1)[0].strip()
    return m.group(1), code or None


def parse(text: str, short: str | None = None, fields: dict | None = None) -> dict:
    """Everything the page and the hand-off need to know about one update's text."""
    f = fields or jira_fields()
    sec = sections(text)
    j, s = sec.get("jira"), sec.get("slack")
    out = {"has_jira": bool(j), "has_slack": bool(s), "rag_status": None,
           "rag_reason_code": None, "rag_raw_code": None, "jira_key": None,
           "slack_channel": None, "slack_channel_name": None, "slack_title": None,
           "has_note": bool(_NOTE_RE.search(text or "")), "headline": ""}
    if j:
        m = re.search(r"\b(TAM-\d+)\b", j["heading"])
        out["jira_key"] = m.group(1) if m else None
        rl = _RAG_LINE.search(j["body"])
        out["rag_where"] = "jira" if rl else None
        if rl is None:
            # The August layout: the RAG line sat at the top of the file, above the Jira
            # section, and was prepended at post time. Read it so the page shows the real
            # value — checks() then says it would not be posted from there.
            rl = _RAG_LINE.search((text or "")[:sum(len(x) + 1 for x in
                                   (text or "").split("\n")[:j["start"]])])
            out["rag_where"] = "header" if rl else None
        if rl:
            st, code = _parse_rag_line(rl.group(0))
            out["rag_status"] = st
            out["rag_raw_code"] = code
            hit = _match_reason(code, f["reasons"]) if code else None
            out["rag_reason_code"] = hit["value"] if hit else None
    if s:
        m = re.search(r"\b([CDG][A-Z0-9]{8,12})\b", s["heading"])
        out["slack_channel"] = m.group(1) if m else None
        m = re.search(r"(#[a-z0-9_-]+)", s["heading"])
        out["slack_channel_name"] = m.group(1) if m else None
        first = next((ln for ln in s["body"].split("\n") if ln.strip()), "")
        m = _SLACK_TITLE.match(first.strip())
        out["slack_title"] = m.group(1) if m else None
        # The first real paragraph after the title — what the DB row's content leads with.
        paras = [p.strip() for p in re.split(r"\n\s*\n", s["body"]) if p.strip()]
        if paras and out["slack_title"] and paras[0].startswith("**") \
                and ("Weekly Update" in paras[0] or "Stoplight Report" in paras[0]):
            paras = paras[1:]
        out["headline"] = re.sub(r"\s+", " ", paras[0]) if paras else ""
    return out


# Lines that start a new block, so are never joined onto the line above.
_BLOCK_START = re.compile(r"^\s*(?:[-*•+]\s|\d+[.)]\s|#{1,6}\s|>|\||```|~~~|"
                          r"Full detail:|\*\()")
_BOLD_HEAD = re.compile(r"^\*\*[^*\n]+\*\*:?\s*$")


def unwrap(md: str) -> tuple[str, int]:
    """Join hard-wrapped lines back into one line per paragraph / list item.

    Jira's markdown comments and Slack both turn a single newline into a real line break,
    so a draft wrapped at ~110 columns posted with a forced break every line (one
    update had to be reposted for it). Whitespace-only: the words are
    untouched, only newline+indent inside a paragraph becomes one space.

    Never joined: blank lines, anything starting a block (bullet, numbered item, heading,
    quote, table row, code fence, `Full detail:`, a `*(` aside), bold-only heading lines
    like **This Week** on either side, the RAG line, and a line ending in two spaces or a
    backslash (markdown's explicit break). Code fences are left exactly as they are.
    Returns (text, number of joins)."""
    out: list[str] = []
    fence = False
    joins = 0
    for ln in (md or "").split("\n"):
        st = ln.strip()
        if st.startswith(("```", "~~~")):
            fence = not fence
            out.append(ln)
            continue
        if fence or not out:
            out.append(ln)
            continue
        prev = out[-1]
        ps = prev.strip()
        if (st and ps and not _BLOCK_START.match(ln) and not _BOLD_HEAD.match(st)
                and not _BOLD_HEAD.match(ps) and not ps.startswith(("#", "|", "```", "~~~"))
                and not ps.startswith(("Full detail:",) + _RAG_PREFIXES)
                and not _SLACK_TITLE.match(ps) and not prev.endswith(("  ", "\\"))):
            out[-1] = prev.rstrip() + " " + st
            joins += 1
        else:
            out.append(ln)
    return "\n".join(out), joins


def post_bodies(text: str) -> dict:
    """Exactly what would be posted: {'jira','slack','jira_chars','slack_chars',
    'jira_wraps','slack_wraps'} — stripped (sections()) and unwrapped (unwrap())."""
    sec = sections(text)
    jb, jw = unwrap(sec.get("jira", {}).get("body", ""))
    sb, sw = unwrap(sec.get("slack", {}).get("body", ""))
    return {"jira": jb, "slack": sb, "jira_chars": len(jb), "slack_chars": len(sb),
            "jira_wraps": jw, "slack_wraps": sw}


def checks(text: str, short: str, info: dict | None = None,
           fields: dict | None = None) -> list[dict]:
    """What stands between this file and a clean post. level: error (blocks a hand-off),
    warn (the owner should look), info (expected, just so it is visible)."""
    f = fields or jira_fields()
    p = info or parse(text, short, f)
    cust = _customers().get(short) or {}
    out = []

    def add(level, msg):
        out.append({"level": level, "msg": msg})

    if not p["has_jira"]:
        add("error", "no '## Jira version' section — nothing to post to Jira")
    if not p["has_slack"]:
        add("error", "no '## Slack version' section — nothing to post to Slack")
    if p["has_jira"]:
        if cust.get("jira") and p["jira_key"] and p["jira_key"] != cust["jira"]:
            add("error", f"the Jira heading says {p['jira_key']} but this account's "
                         f"epic is {cust['jira']}")
        elif not p["jira_key"]:
            add("warn", "the Jira heading names no TAM-### issue")
        if p.get("rag_where") == "header":
            add("error", "the RAG line sits above the Jira version (the August layout), "
                         "so it would not be posted — save once with RAG set and the "
                         "tool puts it in the Jira version")
        if p["rag_status"] is None:
            add("error", "RAG status is not set in the Jira version")
        elif p["rag_reason_code"] is None:
            add("error", f"reason code {p['rag_raw_code']!r} is not a Jira option"
                if p["rag_raw_code"] else "reason code is not set")
        else:
            _, why = validate_rag(p["rag_status"], p["rag_reason_code"], f)
            if why:
                add("error", why)
    want = channel_id(short)
    if p["has_slack"]:
        if want and p["slack_channel"] and p["slack_channel"] != want:
            add("error", f"the Slack heading says {p['slack_channel']} but this "
                         f"account's channel is {want}")
        elif want and not p["slack_channel"]:
            add("info", f"the Slack heading names no channel id — it will go to {want}")
        elif not want and not p["slack_channel"]:
            add("warn", "this account has no Slack channel — put its id in the customer's "
                        "slack_internal, e.g. '#acme-internal (<channel id>)'; until then the "
                        "Slack version has nowhere to go")
    bodies = post_bodies(text)
    if bodies["slack_chars"] > SLACK_LIMIT:
        add("error", f"the Slack version is {bodies['slack_chars']:,} characters — "
                     f"Slack's limit is {SLACK_LIMIT:,}")
    elif bodies["slack_chars"] > SLACK_WARN:
        add("warn", f"the Slack version is {bodies['slack_chars']:,} characters, close "
                    f"to Slack's {SLACK_LIMIT:,} limit")
    for key in ("jira", "slack"):
        if _NOTE_RE.search(bodies[key]):
            add("error", f"a '(Note for …' block sits inside the {key.title()} "
                         "version and would be posted")
    # The skeleton's own placeholders. Posting one would put "<Summary paragraph.>" in
    # front of a customer's exec sponsor, so it blocks the hand-off.
    for key in ("jira", "slack"):
        if "<Summary paragraph.>" in bodies[key]:
            add("error", f"the {key.title()} version still has its <Summary paragraph.> "
                         "placeholder")
        if re.search(r"^\s*[-•]\s*$", bodies[key], re.M):
            add("warn", f"the {key.title()} version has an empty bullet")
    for key in ("jira", "slack"):
        n = bodies.get(key + "_wraps") or 0
        if n:
            add("info", f"{n} hard line wrap{'s' if n != 1 else ''} in the {key.title()} "
                        "version will be joined at post time — Jira and Slack both turn a "
                        "single newline into a line break")
        asides = [ln.strip() for ln in bodies[key].split("\n")
                  if ln.strip().startswith("*(")]
        if asides:
            add("warn", f"the {key.title()} version has a line that reads like a note, not "
                        f"update text, and it would be posted: {asides[0][:70]}")
    if "<@U" in bodies["jira"]:
        add("warn", "the Jira version contains Slack <@U…> mentions, which Jira shows "
                    "as raw text")
    if p["has_slack"]:
        filled = _FILLED_RE.search(bodies["slack"])
        if has_placeholder(bodies["slack"]):
            add("info", "the Slack 'Full detail' link gets its comment id after the Jira "
                        "post")
        elif filled and not (info or {}).get("_posted"):
            # A number already in the link, before this week is posted, is almost always
            # last week's comment carried over by copying the file.
            add("warn", f"the Slack 'Full detail' link already points at comment "
                        f"{filled.group(1)} — if this week is not posted yet, that is an "
                        "older comment; change it to focusedCommentId=<id>")
        elif "focusedCommentId=" not in bodies["slack"]:
            add("warn", "the Slack version has no 'Full detail' link to the Jira comment")
    return out


# ------------------------------------------------------------------ the RAG rewrite

def rag_line(status: str, code: str) -> str:
    return f"**Stoplight status:** {EMOJI[status]} {status} — **Reason code:** {code}"


def apply_rag(text: str, status: str, code: str) -> str:
    """Rewrite the two places RAG appears in the text from the validated pair.

    Jira: the `**RAG status:**` line, replaced whole — including any italic aside, which
    the playbook banned (Aug 7 2026). Inserted as the first body line if missing.
    Slack: the title line's ` · 🟡 Amber · CODE` suffix. Left alone if there is no title
    line in the Weekly Update form; pre-Aug-13 files had none."""
    lines = (text or "").split("\n")
    sec = sections(text)
    new_line = rag_line(status, code)
    j = sec.get("jira")
    # Any other RAG line in the file (the August header copy) is kept in step too, so the
    # file cannot say two different things.
    for k, ln in enumerate(lines):
        if ln.startswith(_RAG_PREFIXES) and not (j and j["start"] <= k < j["end"]):
            lines[k] = new_line
    if j:
        hit = next((k for k in range(j["start"], j["end"])
                    if lines[k].startswith(_RAG_PREFIXES)), None)
        if hit is not None:
            lines[hit] = new_line
        else:
            at = j["start"]
            while at < j["end"] and not lines[at].strip():
                at += 1
            lines[at:at] = [new_line, ""]
    # Re-scan: the insert above shifts the Slack section down.
    s = sections("\n".join(lines)).get("slack")
    if s:
        k = next((k for k in range(s["start"], s["end"]) if lines[k].strip()), None)
        if k is not None:
            m = _SLACK_TITLE.match(lines[k].strip())
            if m:
                lines[k] = f"**{m.group(1)}** · {EMOJI[status]} {status} · {code}"
    return "\n".join(lines)


# ------------------------------------------------------------------ files on disk

def _root() -> Path:
    return D.ROOTS["workspace"]


def _find_files(short: str, mon: dt.date, sun: dt.date) -> list[str]:
    """Weekly-update files for one account dated inside [mon, sun]. Workspace-relative.

    Two places, per the playbook: drafts sit at the top of Reports/weekly-updates/,
    and the assistant moves them into YYYY/MM/ once posted. Sorted so
    an archived copy (".../2026/09/...") comes after a draft ("...2026-09-24-...") and
    wins as the card's file — '/' sorts after '-'."""
    slugs = {short} | {k for k, v in SLUG_ALIASES.items() if v == short}
    out = []
    months = {(mon.year, mon.month), (sun.year, sun.month)}
    dirs = [_root() / WEEKLY_PREFIX] + [_root() / WEEKLY_PREFIX / f"{y:04d}" / f"{m:02d}"
                                        for y, m in sorted(months)]
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*-update*.md"):
            np_ = name_parts(f.name)
            if not np_ or np_["slug"] not in slugs:
                continue
            fd = _date(np_["date"])
            if fd and mon <= fd <= sun:
                out.append(str(f.relative_to(_root())))
    return sorted(out)


def _handoff_path(p: Path) -> Path:
    return p.with_name(p.stem + ".handoff.json")


def _handoff_for(p: Path) -> Path:
    """Where this file's hand-off is. Normally beside it; but the assistant moves a posted
    update from the drafts folder into YYYY/MM/ and the .handoff.json stays behind, so an
    archived file also looks in the drafts folder under the same name."""
    h = _handoff_path(p)
    if h.exists():
        return h
    alt = _root() / WEEKLY_PREFIX / h.name
    return alt if alt.exists() else h


def _read_handoff(p: Path) -> dict | None:
    h = _handoff_for(p)
    if not h.exists():
        return None
    try:
        return json.loads(h.read_text())
    except Exception:
        return {"status": "unreadable"}


def _direct_block(prior: dict) -> str | None:
    """A direct post (poster.py, Path B) that stopped part-way already has a Jira comment
    out. Handing that file to the assistant, or sending it back, would post Jira a second time
    — so both refuse until the direct post is finished from the tool."""
    d = (prior or {}).get("direct") or {}
    if (prior or {}).get("status") == "posting" and d.get("jira_comment_id"):
        return (f"this update is already on Jira (comment {d['jira_comment_id']}, posted "
                "directly from mt) but the post stopped part-way — open it and use "
                "'Finish posting' so nothing is posted twice")
    return None


def archive(rel: str) -> dict:
    """Move a POSTED update from the drafts folder into YYYY/MM/, as the assistant does
    after posting. A rename, never a delete; refuses if the destination already exists. The
    .handoff.json stays in the drafts folder, where _handoff_for() already looks."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "why": why}
    rel = str(p.relative_to(_root()))
    if "/" in rel[len(WEEKLY_PREFIX):]:
        return {"ok": True, "path": rel, "moved": False}
    d = _date(parts["date"])
    dest = _root() / WEEKLY_PREFIX / f"{d.year:04d}" / f"{d.month:02d}" / p.name
    if dest.exists():
        return {"ok": False, "why": f"{dest.relative_to(_root())} already exists — left "
                                    "the file in the drafts folder"}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(p, dest)
    except Exception as e:
        return {"ok": False, "why": f"could not move it ({type(e).__name__}: {e})"}
    return {"ok": True, "path": str(dest.relative_to(_root())), "moved": True}


def _write_json(p: Path, obj: dict) -> None:
    tmp = p.with_name(p.name + ".mt-tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)


# ------------------------------------------------------------------ the DB

def ensure_schema() -> list[str]:
    """Add the phase-5 columns to weekly_updates. Idempotent; ADD COLUMN only."""
    conn = TR._connect(write=True)
    if conn is None:
        return []
    added = []
    try:
        have = TR._cols(conn, "weekly_updates")
        if not have:
            return []
        for col, typ in NEW_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE weekly_updates ADD COLUMN {col} {typ}")
                added.append(f"weekly_updates.{col}")
        if added:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return added


_CONTENT_RAG = re.compile(
    r"(?:RAG:\s*)?(?:🟢|🟡|🔴)?\s*(Green|Amber|Red)\s*/\s*([A-Za-z][A-Za-z /-]*?/{0,2})"
    r"(?=[\s.;,—(]|$)")


def _rag_from_content(content: str) -> tuple[str | None, str | None]:
    """The RAG a pre-phase-5 row carries in its free text ('RAG: Red/RENWL. …',
    '🟢 Green / GREEN — …'). Read-only, for display."""
    m = _CONTENT_RAG.search((content or "")[:160])
    if not m:
        return None, None
    return m.group(1), m.group(2).strip()


def _rows_for(conn, cid: int, mon: dt.date, sun: dt.date) -> list[dict]:
    have = TR._cols(conn, "weekly_updates")
    want = ["id", "customer_id", "week_of", "jira_key", "slack_channel", "content",
            "posted_at", "created_at"] + [c for c, _ in NEW_COLUMNS]
    sel = TR._select_list(have, want, "w")
    rows = conn.execute(
        f"SELECT {sel} FROM weekly_updates w WHERE customer_id=? "
        "AND substr(week_of,1,10) BETWEEN ? AND ? ORDER BY id DESC",
        (cid, mon.isoformat(), sun.isoformat())).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        if not d.get("rag_status"):
            st, code = _rag_from_content(d.get("content") or "")
            d["rag_from_content"] = [st, code] if st else None
        out.append(d)
    return out


# ------------------------------------------------------------------ the board

def _card(short: str, cust: dict, mon: dt.date, sun: dt.date, conn, fields) -> dict:
    files = _find_files(short, mon, sun)
    card = {"customer": short, "customer_id": cust["id"], "name": cust["name"],
            "jira": cust["jira"], "channel": channel_id(short), "files": files,
            "path": files[-1] if files else None, "rag_status": None,
            "rag_reason_code": None, "state": "none", "handoff": None, "row": None,
            "errors": 0, "not_posted": False}
    if card["path"]:
        p = D.resolve("workspace", card["path"])
        try:
            text = p.read_text(errors="replace")
        except Exception:
            text = ""
        info = parse(text, short, fields)
        card["rag_status"] = info["rag_status"]
        card["rag_reason_code"] = info["rag_reason_code"] or info["rag_raw_code"]
        card["format"] = "split" if info["has_jira"] and info["has_slack"] else "legacy"
        card["errors"] = sum(1 for c in checks(text, short, info, fields)
                             if c["level"] == "error")
        card["not_posted"] = bool(name_parts(card["path"])["not_posted"])
        h = _read_handoff(p)
        if h:
            card["handoff"] = {"status": h.get("status"), "ready_at": h.get("ready_at"),
                               "stale": h.get("sha256") != _sha(text),
                               "notes": h.get("notes")}
        card["state"] = "draft"
        if h and h.get("status") == "ready":
            card["state"] = "changed" if card["handoff"]["stale"] else "handed_off"
        elif h and h.get("status") == "revise":
            card["state"] = "sent_back"
        elif h and h.get("status") == "revised":
            card["state"] = "revised"
        elif h and h.get("status") == "posting":
            card["state"] = "partial"
    rows = _rows_for(conn, cust["id"], mon, sun) if conn is not None else []
    if rows:
        card["row"] = rows[0]
        card["state"] = "posted"
        if not card["rag_status"]:
            r = rows[0]
            st = r.get("rag_status") or (r.get("rag_from_content") or [None])[0]
            card["rag_status"] = st
            card["rag_reason_code"] = (r.get("rag_reason_code")
                                       or (r.get("rag_from_content") or [None, None])[1])
    return card


def board(day=None) -> dict:
    """One card per active account for the week holding `day`, plus last week's RAG so
    a change is visible before it is posted. Never raises."""
    d = _date(day) or _today()
    mon, sun = week_bounds(d)
    pmon, psun = mon - dt.timedelta(days=7), sun - dt.timedelta(days=7)
    fields = jira_fields()
    custs = [c for c in _customers().values()
             if (c.get("status") or "active") in BOARD_STATUSES]
    custs.sort(key=lambda c: c.get("status") == "test")          # practice account last
    conn = TR._connect()
    cards = []
    try:
        for c in custs:
            try:
                card = _card(c["short"], c, mon, sun, conn, fields)
                prev = _card(c["short"], c, pmon, psun, conn, fields)
                card["test"] = c.get("status") == "test"
                card["last"] = {"rag_status": prev["rag_status"],
                                "rag_reason_code": prev["rag_reason_code"],
                                "path": prev["path"]}
            except Exception as e:
                card = {"customer": c["short"], "customer_id": c["id"], "name": c["name"],
                        "state": "error", "error": f"{type(e).__name__}: {e}"}
            cards.append(card)
    finally:
        if conn is not None:
            conn.close()
    return {"week_of": week_of(d), "monday": mon.isoformat(), "sunday": sun.isoformat(),
            "today_week": week_of(_today()), "cards": cards,
            "fields_error": fields["error"], "error": None}


def one(rel: str) -> dict:
    """The editor's view of one file. Never raises."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "error": why}
    r = D.read_file("workspace", rel)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    fields = jira_fields()
    short = parts["short"]
    info = parse(r["text"], short, fields)
    cust = _customers().get(short) or {}
    rows = []
    conn = TR._connect()
    if conn is not None and cust:
        try:
            wd = _date(parts["date"])
            rows = _rows_for(conn, cust["id"], *week_bounds(wd))
        except Exception:
            rows = []
        finally:
            conn.close()
    h = _read_handoff(p)
    if h:
        h = dict(h, stale=h.get("sha256") != _sha(r["text"]))
    if rows:
        info["_posted"] = True
    return {"ok": True, "path": r["path"], "text": r["text"], "mtime": r["mtime"],
            "bytes": r["bytes"], "customer": short, "name": cust.get("name"),
            "week_of": parts["date"], "not_posted": parts["not_posted"],
            "info": info, "checks": checks(r["text"], short, info, fields),
            "bodies": post_bodies(r["text"]), "row": rows[0] if rows else None,
            "handoff": h, "fields": fields, "rag": RAG, "emoji": EMOJI,
            "error": None}


def preview(text: str, rel: str) -> dict:
    """Parse UNSAVED text — what the page shows in the Jira/Slack tabs. Writes nothing."""
    parts = name_parts(rel or "")
    short = parts["short"] if parts else ""
    fields = jira_fields()
    info = parse(text or "", short, fields)
    if parts and _week_posted(short, parts["date"]):
        info["_posted"] = True
    return {"ok": True, "info": info, "bodies": post_bodies(text or ""),
            "checks": checks(text or "", short, info, fields)}


def _week_posted(short: str, date: str) -> bool:
    cust = _customers().get(short)
    conn = TR._connect() if cust else None
    if conn is None:
        return False
    try:
        return bool(_rows_for(conn, cust["id"], *week_bounds(_date(date))))
    except Exception:
        return False
    finally:
        conn.close()


# ------------------------------------------------------------------ writes

def save(rel: str, text: str, mtime=None, rag_status: str = "",
         rag_reason_code: str = "") -> dict:
    """Save the file. When a RAG pair is given it is validated and written into the
    text (apply_rag) before the save, and the rewritten text comes back so the editor
    shows exactly what is on disk. Blank RAG saves the text as-is — a draft may not
    have one yet; the hand-off is where it is required."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "why": why}
    if not isinstance(text, str):
        return {"ok": False, "why": "nothing to save"}
    if rag_status or rag_reason_code:
        pair, why = validate_rag(rag_status, rag_reason_code)
        if why:
            return {"ok": False, "why": why}
        text = apply_rag(text, pair["status"], pair["code"])
    r = D.write_file("workspace", rel, text, mtime)
    if not r.get("ok"):
        return r
    return dict(r, text=text, **preview(text, rel))


def _skeleton(cust: dict, d: dt.date, last_text: str, last_rel: str | None,
              status: str | None, code: str | None) -> str:
    short = cust["short"]
    tam = cust.get("jira") or "TAM-???"
    chan = channel_id(short) or ""
    chan_name = channel_name(short) or ""
    disp = DISPLAY.get(short, cust["name"])

    def open_items(body: str, slack: bool) -> str:
        m = re.search(r"^\*\*Open Items\*\*\s*$", body, re.M)
        if not m:
            return "• " if slack else "- "
        rest = body[m.end():].strip("\n")
        stop = re.search(r"^(\*\*[^*\n]+\*\*:?\s*$|Full detail:)", rest, re.M)
        return (rest[:stop.start()] if stop else rest).strip() or ("• " if slack else "- ")

    sec = sections(last_text) if last_text else {}
    j_items = open_items(sec.get("jira", {}).get("body", ""), False)
    s_items = open_items(sec.get("slack", {}).get("body", ""), True)
    rag = (rag_line(status, code) if status and code
           else "**Stoplight status:** (not set) — **Reason code:** (not set)")
    suffix = f" · {EMOJI[status]} {status} · {code}" if status and code else ""
    carried = (f" The Open Items below were carried from last week's update "
               f"({Path(last_rel).name}) — prune what closed, add what's new."
               if last_rel else "")
    return (
        f"# {cust['name']} — Stoplight Report, {_fmt_long(d)}\n\n"
        f"*(Note for you — anything needing your call goes here; this block is never "
        f"posted.{carried})*\n\n"
        f"---\n\n"
        f"## Jira version (full — post to {tam})\n\n"
        f"{rag}\n\n"
        f"<Summary paragraph.>\n\n"
        f"**This Week**\n\n- \n\n"
        f"**Open Items**\n\n{j_items}\n\n"
        f"---\n\n"
        f"## Slack version (short — post to "
        f"{(chan_name + ', ' + chan) if chan else 'no Slack channel set yet'})\n\n"
        f"**{disp} — Stoplight Report, {_fmt_short(d)}**{suffix}\n\n"
        f"<Summary paragraph.>\n\n"
        f"**Open Items**\n{s_items}\n\n"
        f"Full detail: {JIRA_BASE}{tam}?{_PLACEHOLDER}\n\n"
        f"---\n"
        f"*Mentions to substitute at post time (internal only):*\n"
    )


def create_draft(short: str, day=None) -> dict:
    """Start this week's file from a skeleton: last week's RAG and Open Items carried
    forward, both headings pre-filled with the account's own epic and channel. Refuses
    if a file for that account already exists in that week — it never overwrites."""
    cust = _customers().get(short)
    if not cust:
        return {"ok": False, "why": f"no such account: {short!r}"}
    d = _date(week_of(day or _today()))
    mon, sun = week_bounds(d)
    have = _find_files(short, mon, sun)
    if have:
        return {"ok": False, "why": f"this week already has {Path(have[-1]).name}",
                "path": have[-1]}
    prev = _find_files(short, mon - dt.timedelta(days=7), sun - dt.timedelta(days=7))
    last_rel = prev[-1] if prev else None
    last_text = ""
    st = code = None
    if last_rel:
        try:
            last_text = D.resolve("workspace", last_rel).read_text(errors="replace")
        except Exception:
            last_text = ""
        info = parse(last_text, short)
        if info["rag_status"] and info["rag_reason_code"]:
            st, code = info["rag_status"], info["rag_reason_code"]
    # The drafts folder, where the assistant drafts too; it moves it into YYYY/MM/ once posted.
    rel = f"{WEEKLY_PREFIX}{d.isoformat()}-{short}-update.md"
    p = D.resolve("workspace", rel)
    if p is None:
        return {"ok": False, "why": "that path is not editable from here"}
    p.parent.mkdir(parents=True, exist_ok=True)
    text = _skeleton(cust, d, last_text, last_rel, st, code)
    try:
        with open(p, "x") as fh:            # "x": fail rather than overwrite a race
            fh.write(text)
    except FileExistsError:
        return {"ok": False, "why": f"{p.name} already exists", "path": rel}
    return {"ok": True, "path": rel, "carried_from": last_rel}


def handoff(rel: str) -> dict:
    """Path A. Write <file>.handoff.json for the assistant and return the line to paste it.

    Reads the file from DISK — what gets handed off is what is saved, never what is in
    a browser tab. Refuses while any check is an error."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "why": why}
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    short = parts["short"]
    cust = _customers().get(short)
    if not cust:
        return {"ok": False, "why": f"no such account: {short!r}"}
    fields = jira_fields()
    info = parse(text, short, fields)
    errs = [c["msg"] for c in checks(text, short, info, fields) if c["level"] == "error"]
    if errs:
        return {"ok": False, "why": "fix these first: " + "; ".join(errs), "errors": errs}
    pair, why = validate_rag(info["rag_status"], info["rag_reason_code"], fields)
    if why:
        return {"ok": False, "why": why}
    bodies = post_bodies(text)
    rel = str(p.relative_to(_root()))
    hp = _handoff_path(p)
    hrel = str(hp.relative_to(_root()))
    prior = _read_handoff(p) or {}
    why = _direct_block(prior)
    if why:
        return {"ok": False, "why": why}
    doc = {
        "kind": "weekly_update",
        "status": "ready",
        "history": _history(prior),
        "ready_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "customer": short, "customer_id": cust["id"], "account": cust["name"],
        "week_of": parts["date"], "path": rel, "sha256": _sha(text),
        "jira": {
            "issue": info["jira_key"] or cust["jira"],
            "fields": {
                HEALTH_FIELD: {"id": pair["health"]["id"],
                               "value": pair["health"]["value"]},
                REASON_FIELD: {"id": pair["reason"]["id"],
                               "value": pair["reason"]["value"]},
            },
            "comment_chars": bodies["jira_chars"],
            # Exactly what to post: already stripped and unwrapped. Post it verbatim —
            # re-deriving it from the file is how forced line breaks reached Jira.
            "body": bodies["jira"],
        },
        "slack": {"channel": channel_id(short) or info["slack_channel"],
                  "chars": bodies["slack_chars"],
                  "comment_id_placeholder": has_placeholder(bodies["slack"]),
                  "body": bodies["slack"]},
        "steps": [
            "Jira first: post jira.body VERBATIM as a comment on the issue, "
            "contentFormat markdown — it is already stripped and unwrapped; do not "
            "re-derive it from the file. Set BOTH fields above by option id in one "
            "editJiraIssue call.",
            "Then Slack: post slack.body VERBATIM to the channel (MCP parameter "
            "`message`), after putting the new comment id into the Full detail link.",
            "Then the DB row, with the command in record_with.",
        ],
        "record_with": (f"python3 {HERE / 'weekly.py'} --record {rel} "
                        "--jira-comment <comment id or URL> "
                        f"--slack-ts <ts or permalink> --by {P.ASSISTANT}"),
    }
    try:
        _write_json(hp, doc)
    except Exception as e:
        return {"ok": False, "why": f"could not write the hand-off ({e})"}
    line = (f"The {cust['name']} weekly update for {parts['date']} is approved — "
            f"post it per {hrel} (Jira comment + both RAG fields, then Slack, then record "
            f"the row).")
    return {"ok": True, "handoff": hrel, "line": line}


NOTES_MAX = 4000


def _history(prior: dict) -> list:
    """Earlier revise rounds, carried from one hand-off file to the next so the back and
    forth on a draft is kept, not overwritten."""
    hist = list(prior.get("history") or [])
    if prior.get("notes"):
        hist.append({k: prior.get(k) for k in ("status", "requested_at", "notes",
                                                "revised_at", "revised_summary",
                                                "file_changed") if prior.get(k)})
    return hist[-20:]


def send_back(rel: str, notes: str) -> dict:
    """Ask the assistant to REVISE the draft — not to post it. Writes the hand-off file
    with status 'revise' and the owner's notes. The assistant edits the file and runs
    --revised; the card then reads "Revised — review again". Only handoff() ever leads
    to posting.

    Checks are deliberately NOT required here: a draft with placeholders is exactly the
    kind of thing that gets sent back."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "why": why}
    notes = (notes or "").strip()
    if not notes:
        return {"ok": False, "why": "write what you want changed — the notes are the point"}
    if len(notes) > NOTES_MAX:
        return {"ok": False, "why": f"keep the notes under {NOTES_MAX:,} characters"}
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    short = parts["short"]
    cust = _customers().get(short)
    if not cust:
        return {"ok": False, "why": f"no such account: {short!r}"}
    rel = str(p.relative_to(_root()))
    hp = _handoff_path(p)
    hrel = str(hp.relative_to(_root()))
    prior = _read_handoff(p) or {}
    why = _direct_block(prior)
    if why:
        return {"ok": False, "why": why}
    doc = {
        "kind": "weekly_update",
        "status": "revise",
        "requested_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "customer": short, "customer_id": cust["id"], "account": cust["name"],
        "week_of": parts["date"], "path": rel, "sha256": _sha(text),
        "notes": notes,
        "history": _history(prior),
        "steps": [
            "Do NOT post this. Revise the draft file per the notes — the Note block "
            "is the place to answer a question or flag something back.",
            "Then mark it revised with the command in revised_with, and say it is ready "
            "for review.",
        ],
        "revised_with": (f"python3 {HERE / 'weekly.py'} --revised {rel} "
                         "[--summary \"what changed\"]"),
    }
    try:
        _write_json(hp, doc)
    except Exception as e:
        return {"ok": False, "why": f"could not write the request ({e})"}
    line = (f"Please revise the {cust['name']} weekly update for {parts['date']} "
            f"— my notes are in {hrel}. Don't post it; mark it revised when done.")
    return {"ok": True, "handoff": hrel, "line": line}


def mark_revised(rel: str, summary: str = "") -> dict:
    """The assistant's side of a send-back: the draft is revised and back with the owner."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "why": why}
    h = _read_handoff(p)
    if not h or h.get("status") != "revise":
        return {"ok": False, "why": "this update has no open revise request"}
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    changed = _sha(text) != h.get("sha256")
    doc = dict(h, status="revised",
               revised_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               revised_summary=(summary or "").strip()[:1000] or None,
               file_changed=changed, sha256=_sha(text))
    try:
        _write_json(_handoff_for(p), doc)
    except Exception as e:
        return {"ok": False, "why": f"could not mark it revised ({e})"}
    notes = [] if changed else ["the file is unchanged since the notes were sent — the "
                                "reviewer will see that"]
    return {"ok": True, "notes": notes}


def pending() -> list[dict]:
    """Hand-offs waiting on the assistant: 'post' (approved) and 'revise' (sent back with
    notes). What the assistant looks for. Never raises."""
    out = []
    base = _root() / WEEKLY_PREFIX
    try:
        files = sorted(base.rglob("*.handoff.json"))
    except Exception:
        return out
    for h in files:
        try:
            doc = json.loads(h.read_text())
        except Exception:
            continue
        if doc.get("status") not in ("ready", "revise"):
            continue
        md = h.with_name(h.name[:-len(".handoff.json")] + ".md")
        if not md.exists() and doc.get("path"):
            # Moved into YYYY/MM/ after the hand-off was written.
            md = _root() / doc["path"]
            if not md.exists():
                np_ = name_parts(h.name[:-len(".handoff.json")] + ".md")
                d = _date(np_["date"]) if np_ else None
                if d:
                    md = (_root() / WEEKLY_PREFIX / f"{d.year:04d}" / f"{d.month:02d}"
                          / (h.name[:-len(".handoff.json")] + ".md"))
        try:
            stale = _sha(md.read_text(errors="replace")) != doc.get("sha256")
        except Exception:
            stale = True
        out.append({"handoff": str(h.relative_to(_root())), "path": doc.get("path"),
                    "account": doc.get("account"), "week_of": doc.get("week_of"),
                    "kind": "post" if doc.get("status") == "ready" else "revise",
                    "notes": doc.get("notes") if doc.get("status") == "revise" else None,
                    "stale": stale})
    return out


def _comment_id(v: str) -> str | None:
    v = (v or "").strip()
    if re.fullmatch(r"\d{4,12}", v):
        return v
    m = re.search(r"focusedCommentId=(\d+)", v) or re.search(r"/comment/(\d+)", v)
    return m.group(1) if m else None


def _slack_ts(v: str) -> str | None:
    v = (v or "").strip()
    if re.fullmatch(r"\d{10}\.\d{6}", v):
        return v
    m = re.search(r"/p(\d{10})(\d{6})\b", v)      # a message permalink
    return f"{m.group(1)}.{m.group(2)}" if m else None


def record_posted(rel: str, jira_comment: str = "", slack_ts: str = "",
                  by: str = "mt", posted_at: str | None = None) -> dict:
    """The closing step of either path: write the weekly_updates row.

    Updates the week's existing row if the assistant already logged one (filling the new
    columns, never rewriting `content`), otherwise inserts. Then links the file, fills
    the `<id>` placeholder in the Slack link, and marks the hand-off posted. The row is
    the part that must not be lost, so it happens first and the rest cannot undo it."""
    p, parts, why = _check_rel(rel)
    if why:
        return {"ok": False, "why": why}
    by = (by or "mt").strip()
    if by not in POSTERS:
        return {"ok": False, "why": f"posted_by must be one of: {', '.join(POSTERS)}"}
    cid_ = _comment_id(jira_comment)
    if not cid_:
        return {"ok": False, "why": "the Jira comment id is required — the number, or the "
                                    "comment's URL (Jira is posted first, so it exists)"}
    ts = None
    if (slack_ts or "").strip():
        ts = _slack_ts(slack_ts)
        if not ts:
            return {"ok": False, "why": "the Slack ts looks like 1787933160.363229, or "
                                        "paste the message's permalink"}
    short = parts["short"]
    cust = _customers().get(short)
    if not cust:
        return {"ok": False, "why": f"no such account: {short!r}"}
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}
    info = parse(text, short)
    pair, why = validate_rag(info["rag_status"], info["rag_reason_code"])
    if why:
        return {"ok": False, "why": f"the file's RAG is not valid — {why}"}
    # Recording says "this is what went out". A file still holding the skeleton's
    # placeholders did not go out as written, so it cannot be recorded as posted.
    ph = [c["msg"] for c in checks(text, short, info)
          if c["level"] == "error" and "placeholder" in c["msg"]]
    if ph:
        return {"ok": False, "why": "this file was not posted as written — " + "; ".join(ph)}
    rel = str(p.relative_to(_root()))
    # UTC, like every existing row: the assistant's inserts take posted_at from SQLite's
    # datetime('now'), which is UTC (posted_at == created_at on all of them).
    when = posted_at or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    channel = channel_id(short) or info["slack_channel"]
    headline = info["headline"]
    if len(headline) > 300:
        headline = headline[:297].rstrip() + "…"
    content = (f"RAG: {pair['status']}/{pair['code']}. {headline} File: {rel}").strip()

    ensure_schema()
    conn = TR._connect_w()
    try:
        wd = _date(parts["date"])
        rows = _rows_for(conn, cust["id"], *week_bounds(wd))
        vals = {"rag_status": pair["status"], "rag_reason_code": pair["code"],
                "jira_comment_id": cid_, "posted_by": by}
        if ts:
            vals["slack_ts"] = ts
        if rows:
            row_id = rows[0]["id"]
            sets = ", ".join(f"{k}=?" for k in vals)
            conn.execute(f"UPDATE weekly_updates SET {sets}, "
                         "posted_at=COALESCE(posted_at, ?), "
                         "slack_channel=COALESCE(slack_channel, ?), "
                         "jira_key=COALESCE(jira_key, ?) WHERE id=?",
                         (*vals.values(), when, channel, cust["jira"], row_id))
            action = "updated"
        else:
            cols = ["customer_id", "week_of", "jira_key", "slack_channel", "content",
                    "posted_at", *vals.keys()]
            cur = conn.execute(
                f"INSERT INTO weekly_updates ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                (cust["id"], parts["date"], info["jira_key"] or cust["jira"], channel,
                 content, when, *vals.values()))
            row_id = cur.lastrowid
            action = "inserted"
        conn.commit()
    finally:
        conn.close()

    notes = []
    try:
        TR.add_link("weekly_updates", row_id, rel, role="weekly_update")
    except Exception as e:
        notes.append(f"the row is saved but the file link was not ({e})")
    if has_placeholder(text):
        new = fill_comment_id(text, cid_)
        w = D.write_file("workspace", rel, new, p.stat().st_mtime)
        if not w.get("ok"):
            notes.append(f"the row is saved but the Slack link was not filled "
                         f"({w.get('why')})")
    h = _read_handoff(p)
    if h and h.get("status") != "posted":
        try:
            _write_json(_handoff_for(p), dict(h, status="posted", posted_at=when,
                                               posted_by=by, jira_comment_id=cid_,
                                               slack_ts=ts, weekly_update_id=row_id))
        except Exception as e:
            notes.append(f"the hand-off file was not marked posted ({e})")
    if not ts:
        notes.append("no Slack ts yet — record again with it once the Slack post lands")
    if "/" not in rel[len(WEEKLY_PREFIX):]:
        notes.append("the file is still in the drafts folder — when it moves into "
                     "YYYY/MM/, record again from there so the DB link follows it")
    return {"ok": True, "id": row_id, "action": action, "notes": notes, "why": None}


def record_edit(rel: str, row_id: int, rag_status: str, rag_code: str,
                entry: dict) -> list[str]:
    """After a correction (poster.correct): the row's RAG and edited_at, and an entry in
    the hand-off file's `edits` list. The row is the part that matters; notes say what
    did not stick. `content` is never rewritten."""
    notes = []
    when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ensure_schema()
    conn = TR._connect_w()
    try:
        conn.execute("UPDATE weekly_updates SET rag_status=?, rag_reason_code=?, "
                     "edited_at=? WHERE id=?", (rag_status, rag_code, when, int(row_id)))
        conn.commit()
    finally:
        conn.close()
    p = D.resolve("workspace", rel)
    try:
        hp = _handoff_for(p)
        doc = _read_handoff(p) or {"kind": "weekly_update", "status": "posted", "path": rel}
        doc["edits"] = list(doc.get("edits") or []) + [dict(entry, at=when)]
        _write_json(hp, doc)
    except Exception as e:
        notes.append(f"the row is updated but the hand-off file's edit log was not ({e})")
    return notes


# ------------------------------------------------------------------ CLI

def _main(args: list[str]) -> int:
    if args and args[0] == "--migrate":
        if len(args) < 2:
            print("usage: weekly.py --migrate /path/to/copy.db", file=sys.stderr)
            return 2
        target = Path(args[1]).resolve()
        real = TR.P.DB.resolve()
        if target == real:
            print(f"refusing to migrate the live DB {real} — "
                  "back it up and migrate the copy", file=sys.stderr)
            return 3
        TR.DB_PATH = target
        added = ensure_schema()
        print(f"{target}: added {added or '(nothing — already migrated)'}")
        return 0

    if args and args[0] == "--pending":
        rows = pending()
        if not rows:
            print("nothing waiting on your assistant")
        posts = [r for r in rows if r["kind"] == "post"]
        revs = [r for r in rows if r["kind"] == "revise"]
        if posts:
            print("APPROVED — post these:")
        for r in posts:
            flag = "  (FILE CHANGED since hand-off — check first)" if r["stale"] else ""
            print(f"  {r['week_of']}  {r['account']:<24} {r['handoff']}{flag}")
        if revs:
            print("SENT BACK — revise these, do NOT post:")
        for r in revs:
            print(f"  {r['week_of']}  {r['account']:<24} {r['path']}")
            for ln in (r["notes"] or "").splitlines():
                print(f"      | {ln}")
        return 0

    if args and args[0] == "--body":
        # The exact text to post, for the assistant: weekly.py --body <path> jira|slack
        if len(args) < 3 or args[2] not in ("jira", "slack"):
            print("usage: weekly.py --body <path> jira|slack", file=sys.stderr)
            return 2
        p, _, why = _check_rel(args[1])
        if why:
            print(why, file=sys.stderr)
            return 1
        sys.stdout.write(post_bodies(p.read_text(errors="replace"))[args[2]] + "\n")
        return 0

    if args and args[0] == "--revised":
        import argparse
        ap = argparse.ArgumentParser(prog="weekly.py --revised")
        ap.add_argument("path")
        ap.add_argument("--summary", default="")
        a = ap.parse_args(args[1:])
        r = mark_revised(a.path, a.summary)
        if not r["ok"]:
            print(r["why"], file=sys.stderr)
            return 1
        print("marked revised — back for review")
        for n in r["notes"]:
            print(f"  note: {n}")
        return 0

    if args and args[0] == "--parse":
        rel = args[1] if len(args) > 1 else ""
        o = one(rel)
        if not o.get("ok"):
            print(o.get("error"), file=sys.stderr)
            return 1
        i = o["info"]
        print(f"{o['name']}  week of {o['week_of']}")
        print(f"  RAG      {i['rag_status']} / {i['rag_reason_code'] or i['rag_raw_code']}")
        print(f"  Jira     {i['jira_key']}  {o['bodies']['jira_chars']:,} chars")
        print(f"  Slack    {i['slack_channel']}  {o['bodies']['slack_chars']:,} chars")
        for c in o["checks"]:
            print(f"  {c['level']:<6} {c['msg']}")
        return 0

    if args and args[0] == "--record":
        import argparse
        ap = argparse.ArgumentParser(prog="weekly.py --record")
        ap.add_argument("path")
        ap.add_argument("--jira-comment", required=True)
        ap.add_argument("--slack-ts", default="")
        ap.add_argument("--by", default=P.ASSISTANT, choices=POSTERS)
        a = ap.parse_args(args[1:])
        r = record_posted(a.path, a.jira_comment, a.slack_ts, a.by)
        if not r["ok"]:
            print(r["why"], file=sys.stderr)
            return 1
        print(f"weekly_updates #{r['id']} {r['action']}")
        for n in r["notes"]:
            print(f"  note: {n}")
        return 0

    day = args[1] if args and args[0] == "--board" and len(args) > 1 else None
    b = board(day)
    print(f"week of {b['week_of']}  ({b['monday']} – {b['sunday']})")
    if b["fields_error"]:
        print(f"  ! {b['fields_error']}")
    for c in b["cards"]:
        rag = f"{c.get('rag_status') or '-'}/{c.get('rag_reason_code') or '-'}"
        last = c.get("last") or {}
        was = f"{last.get('rag_status') or '-'}/{last.get('rag_reason_code') or '-'}"
        print(f"  {c['customer']:<13} {c['state']:<11} {rag:<32} was {was:<32} "
              f"{c.get('path') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

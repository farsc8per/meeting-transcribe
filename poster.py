#!/usr/bin/env python3
"""
poster.py — Path B: post a weekly update to Jira and Slack straight from mt (R2, phase 6).

THE ONLY MODULE THAT TOUCHES SECRETS OR THE NETWORK. Everything else in mt reads files
and the DB; this file is small on purpose so it can be audited in one sitting.

What it can do, and nothing else:

  * READ   a Customer Health issue's two RAG fields, its Last Comment field and its
           recent comments (the confirm step and the double-post guard); the editmeta /
           createmeta option lists (Resync).
  * POST   one comment to a TAM issue; SET the two RAG fields on it (customfield_13472,
           customfield_18328 — no other field, by construction); POST one Slack message.
  * EDIT   ("Correct posted update") — only the comment and the Slack
           message THIS update's DB row recorded, and only a comment whose author is the
           token's own account. Never another comment, never someone else's message.

Never transitions an issue, never deletes a comment or a message, never touches any other
field. (The app-managed Last Comment field, customfield_13473, is read-only to the API
anyway — 403 "app is not installed", Sep 25 2026 — and is filled by Jira when the comment
lands, which is why posts here are never edited after the fact.)

Credentials — never inside cc, which syncs to Google Drive:

  ~/.config/mt/credentials.json   mode 0600   (MT_CREDENTIALS overrides the path)
      {"jira":  {"email": "...", "api_token": "..."},
       "slack": {"token": "xoxp-..."}}

  Either half may be absent. Fallbacks, macOS keychain:
      Jira   service `mt-jira-api` ("jira_keychain" in config.json picks another)
      Slack  service `mt-slack-token`
  Without Jira credentials Path B is off and says why; Path A (hand-off) is unaffected.
  Without a Slack token the Slack half is done by hand: the exact text is shown, with a
  Copy button, and the Slack ts is recorded afterwards.

The posting order is the playbook's: Jira comment -> the RAG fields -> Slack (with the new
comment id in the Full detail link) -> move the file into YYYY/MM/ -> the weekly_updates
row. Every finished step is written to <file>.handoff.json before the next starts, so a
failure part-way resumes where it stopped and never posts Jira twice.

Stdlib only.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import stat
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import paths as P
import weekly as WK

SITE = P.JIRA_SITE                                    # "jira_site" in config.json
SLACK_API = "https://slack.com/api"
CRED_PATH = Path(os.environ.get("MT_CREDENTIALS")
                 or Path.home() / ".config" / "mt" / "credentials.json")
JIRA_KEYCHAIN = P.JIRA_KEYCHAIN
SLACK_KEYCHAIN = "mt-slack-token"
LAST_COMMENT_FIELD = "customfield_13473"
RAG_FIELDS = (WK.HEALTH_FIELD, WK.REASON_FIELD)       # the only fields ever written
JIRA_COMMENT_MAX = 32000
TIMEOUT = float(os.environ.get("MT_POST_TIMEOUT") or 20)


# ------------------------------------------------------------------ test overrides
#
# Tests point these at a local fake server. A real token must never go anywhere but the
# real hosts, so an override is only honoured for 127.0.0.1, and turns the keychain off.

def _override(name: str) -> str | None:
    v = (os.environ.get(name) or "").rstrip("/")
    return v if v.startswith("http://127.0.0.1:") else None


def _site() -> str:
    return _override("MT_JIRA_SITE") or SITE


def _slack_api() -> str:
    return _override("MT_SLACK_API") or SLACK_API


def _keychain_ok() -> bool:
    return not (_override("MT_JIRA_SITE") or _override("MT_SLACK_API")
                or os.environ.get("MT_NO_KEYCHAIN"))


# ------------------------------------------------------------------ credentials

def _cred_file() -> tuple[dict, str | None]:
    """(contents, problem). A file anyone else can read is refused, not used."""
    try:
        st = CRED_PATH.stat()
    except FileNotFoundError:
        return {}, None
    except Exception as e:
        return {}, f"{CRED_PATH} could not be read ({type(e).__name__})"
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return {}, f"{CRED_PATH} is readable by other users — run: chmod 600 {CRED_PATH}"
    try:
        doc = json.loads(CRED_PATH.read_text())
    except Exception as e:
        return {}, f"{CRED_PATH} is not valid JSON ({type(e).__name__})"
    return (doc if isinstance(doc, dict) else {}), None


def _keychain(service: str) -> tuple[str | None, str | None]:
    """(account, secret) from the login keychain, or (None, None)."""
    if not _keychain_ok():
        return None, None
    try:
        meta = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service],
                              capture_output=True, text=True, timeout=10)
        if meta.returncode != 0:
            return None, None
        m = re.search(r'"acct"<blob>="([^"]*)"', meta.stdout)
        acct = m.group(1) if m else None
        sec = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service,
                              "-w"], capture_output=True, text=True, timeout=10)
        tok = sec.stdout.strip() if sec.returncode == 0 else ""
        return acct, (tok or None)
    except Exception:
        return None, None


def _jira_cred() -> tuple[tuple[str, str] | None, dict]:
    if not _site():
        return None, {"ok": False, "why": "no Jira site — set \"jira_site\" in config.json "
                                          "(e.g. https://yourcompany.atlassian.net)"}
    doc, problem = _cred_file()
    j = doc.get("jira") or {}
    if j.get("email") and j.get("api_token"):
        return (j["email"], j["api_token"]), {"ok": True, "source": "credentials file",
                                              "who": j["email"]}
    acct, tok = _keychain(JIRA_KEYCHAIN)
    if acct and tok:
        # An Atlassian token is 192 characters; the keychain's interactive prompt
        # truncates at 128 (reference_jira_rest_script.md), which gives a 401 later.
        note = (" — but it is only 128 characters, so it was probably truncated when "
                "stored" if len(tok) == 128 else "")
        return (acct, tok), {"ok": True, "source": f"keychain ({JIRA_KEYCHAIN})",
                             "who": acct, "note": note or None}
    why = problem or (f"no Jira credentials — add them to {CRED_PATH} (mode 600), or to "
                      f"the keychain as service '{JIRA_KEYCHAIN}'")
    return None, {"ok": False, "why": why}


def _slack_cred() -> tuple[str | None, dict]:
    doc, problem = _cred_file()
    t = (doc.get("slack") or {}).get("token")
    if t:
        return t, {"ok": True, "source": "credentials file"}
    _, tok = _keychain(SLACK_KEYCHAIN)
    if tok:
        return tok, {"ok": True, "source": f"keychain ({SLACK_KEYCHAIN})"}
    why = problem or ("no Slack token, so Slack is posted by hand — the exact text is "
                      "shown with a Copy button. To post it from here, put a user token "
                      f"(xoxp-, scope chat:write) in {CRED_PATH} or the keychain as "
                      f"'{SLACK_KEYCHAIN}'")
    return None, {"ok": False, "why": why}


def status() -> dict:
    """What the page needs to enable the buttons. Never a secret, never raises."""
    try:
        _, j = _jira_cred()
        _, s = _slack_cred()
    except Exception as e:
        return {"jira": {"ok": False, "why": f"{type(e).__name__}: {e}"},
                "slack": {"ok": False, "why": "not checked"}}
    return {"jira": j, "slack": s, "credentials_path": str(CRED_PATH)}


# ------------------------------------------------------------------ HTTP

class ApiError(Exception):
    pass


class Unsure(ApiError):
    """The request went out and no answer came back — it may or may not have landed."""


def _http(method: str, url: str, headers: dict, body=None) -> tuple[int, object]:
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers = dict(headers, **{"Content-Type": "application/json; charset=utf-8"})
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers, Accept="application/json"))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    except urllib.error.URLError as e:
        cls = Unsure if method != "GET" else ApiError
        raise cls(f"could not reach {url.split('/')[2]} ({e.reason})")
    except (TimeoutError, OSError) as e:
        cls = Unsure if method != "GET" else ApiError
        raise cls(f"{url.split('/')[2]} did not answer ({type(e).__name__})")
    try:
        return code, (json.loads(raw) if raw.strip() else {})
    except ValueError:
        return code, raw


def _jira_msg(code: int, doc) -> str:
    if code == 401:
        return "Jira said 401 — the API token is wrong or expired (it expires 2027-09-23)"
    if code == 404:
        return "Jira said 404 — the issue does not exist, or the token cannot see it"
    if isinstance(doc, dict):
        bits = list(doc.get("errorMessages") or [])
        bits += [f"{k}: {v}" for k, v in (doc.get("errors") or {}).items()]
        if bits:
            return f"Jira said {code}: " + "; ".join(bits)[:400]
    return f"Jira said {code}"


def _jira(method: str, path: str, body=None) -> dict:
    cred, st = _jira_cred()
    if not cred:
        raise ApiError(st["why"])
    auth = base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()
    code, doc = _http(method, _site() + path, {"Authorization": "Basic " + auth}, body)
    if code >= 300:
        raise ApiError(_jira_msg(code, doc))
    return doc if isinstance(doc, dict) else {}


def _slack(method: str, body: dict) -> dict:
    tok, st = _slack_cred()
    if not tok:
        raise ApiError(st["why"])
    code, doc = _http("POST", f"{_slack_api()}/{method}",
                      {"Authorization": "Bearer " + tok}, body)
    if not isinstance(doc, dict):
        raise ApiError(f"Slack said {code} with no JSON")
    if not doc.get("ok"):
        err = doc.get("error") or f"HTTP {code}"
        hint = {"not_in_channel": " — the token's user is not in that channel",
                "channel_not_found": " — wrong channel id, or the token cannot see it",
                "invalid_auth": " — the Slack token is wrong or revoked",
                "missing_scope": " — the token needs the chat:write scope"}.get(err, "")
        raise ApiError(f"Slack said {err}{hint}")
    return doc


# ------------------------------------------------------------------ markdown -> Jira ADF
#
# The REST API takes Atlassian Document Format, not markdown (Rovo's contentFormat
# 'markdown' converts on its side). This covers what the 84 update files actually use —
# paragraphs, **bold**, *italic*, `code`, links, bare URLs, bullets nested two deep,
# numbered lists, headings — plus quotes, rules and fences for safety. A single newline
# left inside a paragraph (unwrap() has already joined the wrapped ones) is a hardBreak,
# which is what the markdown path did.

_LIST = re.compile(r"^(\s*)([-*•+]|\d+[.)])\s+(.*)$")
_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_INLINE = [
    ("code", re.compile(r"`([^`\n]+)`")),
    ("link", re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")),
    ("auto", re.compile(r"<(https?://[^>\s]+)>")),
    ("strong", re.compile(r"\*\*(.+?)\*\*")),
    ("strong", re.compile(r"(?<!\w)__(.+?)__(?!\w)")),
    ("strike", re.compile(r"~~(.+?)~~")),
    ("em", re.compile(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])")),
    ("em", re.compile(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])")),
    ("url", re.compile(r"https?://[^\s<>()\[\]]+")),
]


def _inline(text: str, marks: tuple = ()) -> list[dict]:
    out: list[dict] = []

    def txt(t: str, m: tuple):
        if t:
            node = {"type": "text", "text": t}
            if m:
                node["marks"] = [dict(x) for x in m]
            out.append(node)

    pos = 0
    while pos < len(text):
        best = None
        for kind, rx in _INLINE:
            m = rx.search(text, pos)
            if m and (best is None or m.start() < best[1].start()):
                best = (kind, m)
        if best is None:
            break
        kind, m = best
        txt(text[pos:m.start()], marks)
        if kind == "code":
            keep = tuple(x for x in marks if x["type"] == "link")   # code takes only link
            txt(m.group(1), keep + ({"type": "code"},))
        elif kind == "link":
            out.extend(_inline(m.group(1), marks + (
                {"type": "link", "attrs": {"href": m.group(2)}},)))
        elif kind in ("auto", "url"):
            url = m.group(1) if kind == "auto" else m.group(0)
            tail = ""
            if kind == "url":
                while url and url[-1] in ".,;:!?'\"":
                    tail = url[-1] + tail
                    url = url[:-1]
            txt(url, marks + ({"type": "link", "attrs": {"href": url}},))
            txt(tail, marks)
        else:
            out.extend(_inline(m.group(1), marks + ({"type": kind},)))
        pos = m.end()
    txt(text[pos:], marks)
    return out


def _para(lines: list[str]) -> dict:
    content: list[dict] = []
    for i, ln in enumerate(lines):
        ln = re.sub(r"(\\|  +)$", "", ln.strip())
        if i and content:
            content.append({"type": "hardBreak"})
        content.extend(_inline(ln))
    return {"type": "paragraph", "content": content} if content else {"type": "paragraph"}


def _block_start(ln: str) -> bool:
    st = ln.strip()
    return bool(_LIST.match(ln) or _HEAD.match(st) or _RULE.match(ln)
                or st.startswith((">", "```", "~~~")))


def _lists(items: list[tuple]) -> list[dict]:
    """items: (indent, ordered, number, text). Nesting by indent."""
    def build(i: int, indent: int) -> tuple[dict, int]:
        ordered = items[i][1]
        node = {"type": "orderedList" if ordered else "bulletList", "content": []}
        if ordered:
            node["attrs"] = {"order": items[i][2]}
        while i < len(items) and items[i][0] >= indent:
            ind, od, _, text = items[i]
            if ind > indent:
                sub, i = build(i, ind)
                if not node["content"]:
                    node["content"].append({"type": "listItem",
                                            "content": [{"type": "paragraph"}]})
                node["content"][-1]["content"].append(sub)
                continue
            if od != ordered:
                break
            node["content"].append({"type": "listItem", "content": [_para([text])]})
            i += 1
        return node, i

    out, i = [], 0
    while i < len(items):
        node, i = build(i, items[i][0])
        out.append(node)
    return out


def md_to_adf(md: str) -> dict:
    lines = (md or "").split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        st = ln.strip()
        if not st:
            i += 1
            continue
        if st.startswith(("```", "~~~")):
            fence, lang = st[:3], st[3:].strip()
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            i += 1
            node = {"type": "codeBlock"}
            if lang:
                node["attrs"] = {"language": lang}
            if "\n".join(body):
                node["content"] = [{"type": "text", "text": "\n".join(body)}]
            blocks.append(node)
            continue
        if _RULE.match(ln):
            blocks.append({"type": "rule"})
            i += 1
            continue
        h = _HEAD.match(st)
        if h:
            blocks.append({"type": "heading", "attrs": {"level": len(h.group(1))},
                           "content": _inline(h.group(2).strip())})
            i += 1
            continue
        if st.startswith(">"):
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner = md_to_adf("\n".join(q))["content"] or [{"type": "paragraph"}]
            blocks.append({"type": "blockquote", "content": inner})
            continue
        if _LIST.match(ln):
            items: list[tuple] = []
            while i < len(lines):
                m = _LIST.match(lines[i])
                if m:
                    ind = len(m.group(1).expandtabs(4))
                    num = re.match(r"\d+", m.group(2))
                    items.append((ind, bool(num), int(num.group()) if num else 1,
                                  m.group(3)))
                    i += 1
                elif lines[i].strip() and not _block_start(lines[i]) and items:
                    ind, od, n, t = items[-1]           # a continuation line
                    items[-1] = (ind, od, n, t + " " + lines[i].strip())
                    i += 1
                elif (not lines[i].strip() and i + 1 < len(lines)
                      and _LIST.match(lines[i + 1])):
                    i += 1                              # a loose list
                else:
                    break
            blocks.extend(_lists(items))
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not _block_start(lines[i]):
            para.append(lines[i])
            i += 1
        blocks.append(_para(para))
    return {"type": "doc", "version": 1, "content": blocks}


def adf_text(node) -> str:
    """Plain text of an ADF value (or a plain string) — for reading Last Comment back."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text") or ""
        if node.get("type") == "hardBreak":
            return "\n"
        sep = "\n" if node.get("type") in ("doc", "bulletList", "orderedList") else ""
        return sep.join(adf_text(c) for c in node.get("content") or [])
    if isinstance(node, list):
        return "".join(adf_text(c) for c in node)
    return ""


# ------------------------------------------------------------------ markdown -> Slack
#
# chat.postMessage `text` is Slack mrkdwn, not markdown: *bold*, _italic_, ~strike~,
# <url|label>, and &, <, > escaped — except Slack's own tokens (<@U…>, <#C…>, <!here>,
# <http…>), which the drafts already carry and must survive untouched.

_SLACK_TOKEN = re.compile(r"<(?:[@#!][^>\s]+|https?://[^>\s]+)>")


def md_to_slack(md: str) -> str:
    keep: list[str] = []

    def stash(s: str) -> str:
        keep.append(s)
        return f"\x00{len(keep) - 1}\x00"

    t = md or ""
    t = re.sub(r"`[^`\n]+`", lambda m: stash(m.group(0)), t)
    t = _SLACK_TOKEN.sub(lambda m: stash(m.group(0)), t)
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
               lambda m: stash(f"<{m.group(2)}|{m.group(1)}>"), t)
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"(?<![*\w])\*(?![\s*])(.+?)(?<![\s*])\*(?![*\w])", r"_\1_", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)
    t = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"*\1*", t)
    t = re.sub(r"~~(.+?)~~", r"~\1~", t)
    t = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", t, flags=re.M)
    t = re.sub(r"^(\s*)[-*+]\s+", lambda m: m.group(1) + ("◦ " if m.group(1) else "• "),
               t, flags=re.M)
    t = re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], t)
    return t


# ------------------------------------------------------------------ Jira reads

def _option_name(v) -> str | None:
    if isinstance(v, dict):
        return v.get("value") or v.get("name")
    return None


def jira_state(issue: str, since: dt.date | None = None) -> dict:
    """Current RAG, by name and id, plus comments posted since `since` (the double-post
    guard). Reads only."""
    f = _jira("GET", f"/rest/api/3/issue/{issue}?fields="
                     f"{WK.HEALTH_FIELD},{WK.REASON_FIELD},summary")
    fl = f.get("fields") or {}
    out = {"issue": issue, "summary": fl.get("summary"),
           "health": _option_name(fl.get(WK.HEALTH_FIELD)),
           "health_id": (fl.get(WK.HEALTH_FIELD) or {}).get("id"),
           "reason": _option_name(fl.get(WK.REASON_FIELD)),
           "reason_id": (fl.get(WK.REASON_FIELD) or {}).get("id"),
           "recent": []}
    if since:
        c = _jira("GET", f"/rest/api/3/issue/{issue}/comment?orderBy=-created&maxResults=10")
        for cm in c.get("comments") or []:
            created = (cm.get("created") or "")[:10]
            if created and created >= since.isoformat():
                out["recent"].append({
                    "id": str(cm.get("id")), "created": cm.get("created"),
                    "author": (cm.get("author") or {}).get("displayName"),
                    "start": adf_text(cm.get("body"))[:140]})
    return out


def last_comment(issue: str) -> str:
    f = _jira("GET", f"/rest/api/3/issue/{issue}?fields={LAST_COMMENT_FIELD}")
    return adf_text((f.get("fields") or {}).get(LAST_COMMENT_FIELD))


# ------------------------------------------------------------------ plan (the confirm)

def _load(rel: str) -> tuple[dict | None, str | None]:
    """Everything a post needs, read from DISK, or why it cannot be posted."""
    p, parts, why = WK._check_rel(rel)
    if why:
        return None, why
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    short = parts["short"]
    cust = WK._customers().get(short)
    if not cust:
        return None, f"no such account: {short!r}"
    fields = WK.jira_fields()
    info = WK.parse(text, short, fields)
    errs = [c["msg"] for c in WK.checks(text, short, info, fields) if c["level"] == "error"]
    if errs:
        return None, "fix these first: " + "; ".join(errs)
    pair, why = WK.validate_rag(info["rag_status"], info["rag_reason_code"], fields)
    if why:
        return None, why
    bodies = WK.post_bodies(text)
    stale = WK._FILLED_RE.search(bodies["slack"])
    prior = WK._read_handoff(p) or {}
    direct = prior.get("direct") if prior.get("status") == "posting" else None
    stale_why = None
    if stale and not (direct or {}).get("jira_comment_id"):
        stale_why = (f"the Slack 'Full detail' link already points at comment "
                     f"{stale.group(1)} — change it to focusedCommentId=<id> so it gets "
                     "this post's id")
    if len(bodies["jira"]) > JIRA_COMMENT_MAX:
        return None, f"the Jira version is over {JIRA_COMMENT_MAX:,} characters"
    return {"p": p, "rel": str(p.relative_to(WK._root())), "parts": parts, "text": text,
            "short": short, "cust": cust, "info": info, "pair": pair, "bodies": bodies,
            "issue": info["jira_key"] or cust["jira"],
            "channel": WK.channel_id(short) or info["slack_channel"],
            "prior": prior, "direct": direct or {}, "stale_why": stale_why}, None


def _already_recorded(L: dict) -> dict | None:
    conn = WK.TR._connect()
    if conn is None:
        return None
    try:
        rows = WK._rows_for(conn, L["cust"]["id"], *WK.week_bounds(WK._date(L["parts"]["date"])))
    finally:
        conn.close()
    return rows[0] if rows else None


def plan(rel: str) -> dict:
    """The confirm step: exactly what will be posted, current -> new RAG by name, and
    anything that smells like a double post. Reads Jira; writes nothing. Never raises."""
    try:
        L, why = _load(rel)
        if why:
            return {"ok": False, "why": why}
        row = _already_recorded(L)
        if row and not L["direct"]:
            return {"ok": False, "why": f"this week is already posted — weekly_updates "
                                        f"#{row['id']}" + (f", Jira comment "
                                        f"{row['jira_comment_id']}" if row.get(
                                            "jira_comment_id") else "")}
        if L["stale_why"]:
            return {"ok": False, "why": L["stale_why"]}
        cred = status()
        if not cred["jira"]["ok"]:
            return {"ok": False, "why": cred["jira"]["why"]}
        mon, _ = WK.week_bounds(WK._date(L["parts"]["date"]))
        cur = jira_state(L["issue"], mon)
        pair = L["pair"]
        d = L["direct"]
        warn = []
        if L["prior"].get("status") in ("ready", "revised"):
            warn.append("This update is handed off to your assistant. Posting it here marks "
                        "the hand-off done — make sure it is not posting it right now.")
        mine = {d.get("jira_comment_id")} if d.get("jira_comment_id") else set()
        others = [c for c in cur["recent"] if c["id"] not in mine]
        for c in others:
            warn.append(f"Jira already has a comment on {L['issue']} this week — "
                        f"{c['author'] or 'someone'}, {c['created'][:16].replace('T', ' ')}: "
                        f"\"{c['start'][:90]}…\" Is this update already posted?")
        new_h, new_r = pair["health"]["value"], pair["reason"]["value"]
        slack_text = md_to_slack(L["bodies"]["slack"])
        steps = [
            {"step": "jira_comment", "done": bool(d.get("jira_comment_id")),
             "what": f"comment on {L['issue']}, {L['bodies']['jira_chars']:,} characters"},
            {"step": "rag_fields", "done": bool(d.get("fields_set")),
             "what": ("unchanged — nothing to set" if (cur["health_id"] == pair["health"]["id"]
                      and cur["reason_id"] == pair["reason"]["id"])
                      else "set both RAG fields")},
            {"step": "slack", "done": bool(d.get("slack_ts")),
             "what": (f"message to {L['channel']}" if cred["slack"]["ok"]
                      else f"by hand — copy the text into {L['channel']}")},
            {"step": "record", "done": False,
             "what": "move the file into YYYY/MM/ and write the weekly_updates row"},
        ]
        return {"ok": True, "path": L["rel"], "sha256": WK._sha(L["text"]),
                "issue": L["issue"], "issue_summary": cur["summary"],
                "account": L["cust"]["name"], "week_of": L["parts"]["date"],
                "health": {"current": cur["health"], "new": new_h,
                           "changes": cur["health_id"] != pair["health"]["id"]},
                "reason": {"current": cur["reason"], "new": new_r,
                           "changes": cur["reason_id"] != pair["reason"]["id"]},
                "jira_body": L["bodies"]["jira"], "jira_chars": L["bodies"]["jira_chars"],
                "slack_channel": L["channel"],
                "slack_channel_name": WK.channel_name(L["short"]),
                "slack_text": slack_text, "slack_chars": len(slack_text),
                "slack_mode": "api" if cred["slack"]["ok"] else "manual",
                "slack_why": None if cred["slack"]["ok"] else cred["slack"]["why"],
                "resuming": bool(d), "steps": steps, "warn": warn, "why": None}
    except ApiError as e:
        return {"ok": False, "why": str(e)}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ post

_LOCK = threading.Lock()
_BUSY: set[str] = set()


def _save_progress(L: dict, d: dict) -> None:
    doc = dict(L["prior"] or {})
    doc.update({"kind": "weekly_update", "status": "posting", "path": L["rel"],
                "customer": L["short"], "customer_id": L["cust"]["id"],
                "account": L["cust"]["name"], "week_of": L["parts"]["date"],
                "direct": d})
    if "history" not in doc:
        doc["history"] = WK._history(L["prior"] or {})
    WK._write_json(WK._handoff_for(L["p"]), doc)
    L["prior"] = doc


def post(rel: str, sha256: str = "") -> dict:
    """Path B. Posts exactly what plan() showed — refused if the file changed since.
    Returns {ok, steps:[{step, ok, detail}], path, ...}. Never raises: every step's
    outcome is in `steps`, and `why` says where it stopped."""
    key = (rel or "").strip()
    with _LOCK:
        if key in _BUSY:
            return {"ok": False, "why": "this update is already being posted — wait for it"}
        _BUSY.add(key)
    try:
        return _post(rel, sha256)
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}", "steps": []}
    finally:
        with _LOCK:
            _BUSY.discard(key)


def _post(rel: str, sha256: str) -> dict:
    L, why = _load(rel)
    if why:
        return {"ok": False, "why": why, "steps": []}
    if not sha256 or WK._sha(L["text"]) != sha256:
        return {"ok": False, "why": "the file changed after you confirmed — look at it "
                                    "again and confirm the new version", "steps": []}
    row = _already_recorded(L)
    if row and not L["direct"]:
        return {"ok": False, "why": f"this week is already posted — weekly_updates "
                                    f"#{row['id']}", "steps": []}
    if L["stale_why"]:
        return {"ok": False, "why": L["stale_why"], "steps": []}
    steps: list[dict] = []
    d = dict(L["direct"])
    d.setdefault("started_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    pair = L["pair"]

    def stop(step: str, msg: str) -> dict:
        steps.append({"step": step, "ok": False, "detail": msg})
        return {"ok": False, "why": msg, "steps": steps, "path": L["rel"],
                "direct": d}

    # 1. Jira comment — once. The progress file is written before the request goes out
    #    (status 'posting' takes it off the assistant's --pending list) and again the moment
    #    the id is back.
    if d.get("jira_comment_id"):
        steps.append({"step": "jira_comment", "ok": True, "skipped": True,
                      "detail": f"already posted — comment {d['jira_comment_id']}"})
    else:
        before = dict(L["prior"] or {})
        if before.get("status") in ("ready", "revise", "revised"):
            # Off the assistant's --pending list before the request goes out.
            _save_progress(L, d)
        try:
            r = _jira("POST", f"/rest/api/3/issue/{L['issue']}/comment",
                      {"body": md_to_adf(L["bodies"]["jira"])})
        except Unsure as e:
            d["jira_uncertain"] = True
            _save_progress(L, d)
            return stop("jira_comment", f"Jira did not answer ({e}) — the comment MAY "
                                        f"have posted. Look at {L['issue']} before "
                                        "trying again; the confirm step lists any "
                                        "comment already there this week.")
        except ApiError as e:
            if before.get("status") in ("ready", "revise", "revised"):
                WK._write_json(WK._handoff_for(L["p"]), before)
            elif L["prior"].get("status") == "posting":
                _save_progress(L, {k: v for k, v in d.items() if k != "jira_uncertain"})
            return stop("jira_comment", f"nothing was posted — {e}")
        cid = str(r.get("id") or "")
        if not cid:
            return stop("jira_comment", "Jira answered without a comment id — check "
                                        f"{L['issue']} before trying again")
        d["jira_comment_id"] = cid
        d["jira_posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        _save_progress(L, d)
        steps.append({"step": "jira_comment", "ok": True, "detail": f"comment {cid}"})
    cid = d["jira_comment_id"]

    # 2. The two RAG fields — and only those two.
    if d.get("fields_set"):
        steps.append({"step": "rag_fields", "ok": True, "skipped": True,
                      "detail": "already set"})
    else:
        try:
            cur = jira_state(L["issue"])
            if (cur["health_id"] == pair["health"]["id"]
                    and cur["reason_id"] == pair["reason"]["id"]):
                detail = f"already {pair['health']['value']} / {pair['reason']['value']}"
            else:
                fields = {WK.HEALTH_FIELD: {"id": pair["health"]["id"]},
                          WK.REASON_FIELD: {"id": pair["reason"]["id"]}}
                assert set(fields) == set(RAG_FIELDS)
                _jira("PUT", f"/rest/api/3/issue/{L['issue']}?notifyUsers=false",
                      {"fields": fields})
                detail = (f"{cur['health'] or '—'} → {pair['health']['value']}, "
                          f"{cur['reason'] or '—'} → {pair['reason']['value']}")
        except ApiError as e:
            return stop("rag_fields", f"the comment is posted ({cid}) but the RAG fields "
                                      f"were not set — {e}. Finish posting retries this.")
        d["fields_set"] = True
        _save_progress(L, d)
        steps.append({"step": "rag_fields", "ok": True, "detail": detail})

    # 3. Slack, with this comment's id in the Full detail link.
    slack_md = WK.fill_comment_id(L["bodies"]["slack"], cid)
    slack_text = md_to_slack(slack_md)
    tok, sst = _slack_cred()
    manual = None
    if d.get("slack_ts"):
        steps.append({"step": "slack", "ok": True, "skipped": True,
                      "detail": f"already posted — ts {d['slack_ts']}"})
    elif not tok:
        manual = {"channel": L["channel"],
                  "channel_name": WK.channel_name(L["short"]),
                  "text": slack_text, "markdown": slack_md, "why": sst["why"]}
        steps.append({"step": "slack", "ok": True, "manual": True,
                      "detail": "no Slack token — post it by hand (text below)"})
    else:
        try:
            r = _slack("chat.postMessage", {"channel": L["channel"], "text": slack_text,
                                            "unfurl_links": True, "unfurl_media": False})
        except ApiError as e:
            return stop("slack", f"Jira is done (comment {cid}) but Slack was not posted "
                                 f"— {e}. Finish posting retries only Slack.")
        d["slack_ts"] = str(r.get("ts") or "")
        d["slack_sha"] = WK._sha(slack_text)
        _save_progress(L, d)
        steps.append({"step": "slack", "ok": True, "detail": f"ts {d['slack_ts']}"})

    # 4. File it in YYYY/MM/, then the row — from the final path, so the DB link is right.
    rel_now = L["rel"]
    notes = []
    mv = WK.archive(rel_now)
    if mv.get("ok"):
        rel_now = mv["path"]
    else:
        notes.append(mv.get("why"))
    rec = WK.record_posted(rel_now, cid, d.get("slack_ts") or "", "mt")
    if not rec.get("ok"):
        # Loud on purpose: posting is not done until the row exists.
        return stop("record", f"POSTED, BUT NOT RECORDED — {rec.get('why')}. Jira "
                              f"comment {cid}" + (f", Slack ts {d['slack_ts']}"
                              if d.get("slack_ts") else "") + ". Finish posting retries "
                              "the record.") | {"path": rel_now}
    notes += [n for n in rec.get("notes") or []
              if not (manual and n.startswith("no Slack ts"))]
    steps.append({"step": "record", "ok": True,
                  "detail": f"weekly_updates #{rec['id']} {rec['action']}"
                            + (f", filed as {rel_now}" if mv.get("moved") else "")})

    # 5. Read Last Comment back — it is what the RAG report shows, and it is filled by a
    #    Jira app, not by us. Informational: the post has already succeeded.
    lc = None
    try:
        lc_text = last_comment(L["issue"])
        want = re.sub(r"\W+", "", WK.post_bodies(L["text"])["jira"])[:60]
        lc = {"filled": bool(want) and want in re.sub(r"\W+", "", lc_text)}
        if not lc["filled"]:
            notes.append("the Last Comment field does not show this comment yet — the RAG "
                         "report reads that field; check it in Jira in a minute")
    except ApiError as e:
        notes.append(f"could not read Last Comment back ({e})")
    return {"ok": True, "why": None, "steps": steps, "path": rel_now,
            "id": rec["id"], "jira_comment_id": cid, "slack_ts": d.get("slack_ts"),
            "issue": L["issue"], "manual_slack": manual, "last_comment": lc,
            "notes": [n for n in notes if n]}


# ------------------------------------------------------------------ correct a posted update
#
# Edits in place: the recorded Jira comment (PUT, same id, so every Full detail link still
# works), the two RAG fields if the file's RAG changed, and the recorded Slack message
# (chat.update — needs a token belonging to whoever posted it; a message pasted by hand as
# the user is editable by the user's own token). The RAG report reads Last Comment, which
# the API cannot write (403, app-managed), so it is read back afterwards and the user is
# told plainly if it still shows the old text.

def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def _correct_load(rel: str) -> tuple[dict | None, str | None]:
    L, why = _load(rel)
    if why:
        return None, why
    row = _already_recorded(L)
    if not row or not row.get("jira_comment_id"):
        return None, "this update is not posted yet — there is nothing to correct"
    if L["direct"]:
        return None, "this post stopped part-way — use Finish posting first"
    L["row"] = row
    L["slack_md"] = WK.fill_comment_id(L["bodies"]["slack"], row["jira_comment_id"])
    L["slack_text"] = md_to_slack(L["slack_md"])
    # What mt last sent to Slack for this update, if it knows: the latest correction's,
    # else the original post's. Unknown (posted by hand or by the assistant) is None.
    prior = L["prior"] or {}
    known = [e.get("slack_sha") for e in prior.get("edits") or [] if e.get("slack_sha")]
    L["slack_known"] = known[-1] if known else (prior.get("direct") or {}).get("slack_sha")
    L["slack_changes"] = (WK._sha(L["slack_text"]) != L["slack_known"]
                          if L["slack_known"] else None)
    return L, None


def _own_comment(issue: str, cid: str) -> dict:
    c = _jira("GET", f"/rest/api/3/issue/{issue}/comment/{cid}")
    me = _jira("GET", "/rest/api/3/myself")
    if (c.get("author") or {}).get("accountId") != me.get("accountId"):
        raise ApiError(f"comment {cid} on {issue} was posted by "
                       f"{(c.get('author') or {}).get('displayName') or 'someone else'}, not "
                       f"{me.get('displayName') or 'this account'} — mt only edits its "
                       "own comments")
    return c


def correct_plan(rel: str) -> dict:
    """The confirm for a correction: what changes in the Jira comment (a line diff),
    RAG current -> file, and what happens to Slack. Reads only. Never raises."""
    import difflib
    try:
        L, why = _correct_load(rel)
        if why:
            return {"ok": False, "why": why}
        cred = status()
        if not cred["jira"]["ok"]:
            return {"ok": False, "why": cred["jira"]["why"]}
        row, pair = L["row"], L["pair"]
        c = _own_comment(L["issue"], row["jira_comment_id"])
        old = adf_text(c.get("body"))
        new = adf_text(md_to_adf(L["bodies"]["jira"]))
        cur = jira_state(L["issue"])
        rag_changes = (cur["health_id"] != pair["health"]["id"]
                       or cur["reason_id"] != pair["reason"]["id"])
        jira_changes = _norm(old) != _norm(new)
        diff = "\n".join(difflib.unified_diff(old.split("\n"), new.split("\n"),
                                               "posted", "corrected", lineterm="", n=1))
        ts = row.get("slack_ts")
        slack_mode = ("api" if ts and cred["slack"]["ok"]
                      else "manual" if ts else "unrecorded")
        if (not jira_changes and not rag_changes
                and (slack_mode != "api" or L["slack_changes"] is False)):
            return {"ok": False, "why": "the saved file says the same as what is posted — "
                                        "edit and save it first"}
        warn = ["The RAG report reads Jira's Last Comment field, which the API cannot "
                "write. mt checks it after the edit and tells you if it still shows the "
                "old text."]
        if slack_mode == "unrecorded":
            warn.append("No Slack message is recorded for this update, so Slack is left "
                        "alone — record its link first if you want it corrected too.")
        return {"ok": True, "path": L["rel"], "sha256": WK._sha(L["text"]),
                "issue": L["issue"], "account": L["cust"]["name"],
                "comment_id": row["jira_comment_id"], "row_id": row["id"],
                "jira_changes": jira_changes, "diff": diff or "(no text change)",
                "jira_body": L["bodies"]["jira"],
                "health": {"current": cur["health"], "new": pair["health"]["value"],
                           "changes": cur["health_id"] != pair["health"]["id"]},
                "reason": {"current": cur["reason"], "new": pair["reason"]["value"],
                           "changes": cur["reason_id"] != pair["reason"]["id"]},
                "slack_mode": slack_mode, "slack_ts": ts,
                "slack_changes": L["slack_changes"],
                "slack_channel": row.get("slack_channel") or L["channel"],
                "slack_channel_name": WK.channel_name(L["short"]),
                "slack_text": L["slack_text"], "warn": warn, "why": None}
    except ApiError as e:
        return {"ok": False, "why": str(e)}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}


def correct(rel: str, sha256: str = "") -> dict:
    key = "correct:" + (rel or "").strip()
    with _LOCK:
        if key in _BUSY:
            return {"ok": False, "why": "this correction is already running — wait for it"}
        _BUSY.add(key)
    try:
        return _correct(rel, sha256)
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}", "steps": []}
    finally:
        with _LOCK:
            _BUSY.discard(key)


def _correct(rel: str, sha256: str) -> dict:
    L, why = _correct_load(rel)
    if why:
        return {"ok": False, "why": why, "steps": []}
    if not sha256 or WK._sha(L["text"]) != sha256:
        return {"ok": False, "why": "the file changed after you confirmed — look at it "
                                    "again and confirm the new version", "steps": []}
    row, pair = L["row"], L["pair"]
    cid = row["jira_comment_id"]
    steps, notes, entry = [], [], {"by": "mt", "comment_id": cid}

    def stop(step, msg):
        steps.append({"step": step, "ok": False, "detail": msg})
        return {"ok": False, "why": msg, "steps": steps, "path": L["rel"]}

    # 1. The Jira comment, same id.
    try:
        c = _own_comment(L["issue"], cid)
        new_adf = md_to_adf(L["bodies"]["jira"])
        if _norm(adf_text(c.get("body"))) == _norm(adf_text(new_adf)):
            steps.append({"step": "jira_comment", "ok": True, "skipped": True,
                          "detail": "text unchanged"})
        else:
            _jira("PUT", f"/rest/api/3/issue/{L['issue']}/comment/{cid}?notifyUsers=false",
                  {"body": new_adf})
            entry["jira"] = True
            steps.append({"step": "jira_comment", "ok": True, "detail": f"comment {cid} edited"})
    except Unsure as e:
        return stop("jira_comment", f"Jira did not answer ({e}) — the edit MAY have gone "
                                    f"through. Look at comment {cid} before trying again.")
    except ApiError as e:
        return stop("jira_comment", f"nothing was changed — {e}")

    # 2. The two RAG fields, if the file's RAG differs from Jira's.
    try:
        cur = jira_state(L["issue"])
        if (cur["health_id"] == pair["health"]["id"]
                and cur["reason_id"] == pair["reason"]["id"]):
            steps.append({"step": "rag_fields", "ok": True, "skipped": True,
                          "detail": "unchanged"})
        else:
            fields = {WK.HEALTH_FIELD: {"id": pair["health"]["id"]},
                      WK.REASON_FIELD: {"id": pair["reason"]["id"]}}
            assert set(fields) == set(RAG_FIELDS)
            _jira("PUT", f"/rest/api/3/issue/{L['issue']}?notifyUsers=false",
                  {"fields": fields})
            entry["rag"] = f"{pair['status']}/{pair['code']}"
            steps.append({"step": "rag_fields", "ok": True,
                          "detail": f"{cur['health'] or '—'} → {pair['health']['value']}, "
                                    f"{cur['reason'] or '—'} → {pair['reason']['value']}"})
    except ApiError as e:
        return stop("rag_fields", ("the comment is corrected but " if entry.get("jira")
                                   else "") + f"the RAG fields were not set — {e}")

    # 3. Slack: edit the recorded message, or hand back the corrected text.
    ts = row.get("slack_ts")
    chan = row.get("slack_channel") or L["channel"]
    tok, sst = _slack_cred()
    manual = None
    if not ts:
        steps.append({"step": "slack", "ok": True, "skipped": True,
                      "detail": "no Slack message recorded — left alone"})
    elif tok and L["slack_changes"] is False:
        steps.append({"step": "slack", "ok": True, "skipped": True,
                      "detail": "text unchanged"})
    elif not tok:
        manual = {"channel": chan, "channel_name": WK.channel_name(L["short"]),
                  "text": L["slack_text"],
                  "why": "No Slack token, so edit the message in Slack by hand: copy this "
                         "over it."}
        steps.append({"step": "slack", "ok": True, "manual": True,
                      "detail": "no Slack token — edit it by hand (text below)"})
    else:
        try:
            _slack("chat.update", {"channel": chan, "ts": ts, "text": L["slack_text"]})
            entry["slack"] = True
            entry["slack_sha"] = WK._sha(L["slack_text"])
            steps.append({"step": "slack", "ok": True, "detail": f"message {ts} edited"})
        except ApiError as e:
            hint = (" — Slack only lets the person who posted a message edit it, so a "
                    "message pasted by someone else cannot be changed with this token"
                    if "cant_update_message" in str(e) else "")
            return stop("slack", f"Jira is corrected but Slack was not — {e}{hint}")

    # 4. The row (RAG + edited_at) and the edit log.
    try:
        notes += WK.record_edit(L["rel"], row["id"], pair["status"], pair["code"], entry)
        steps.append({"step": "record", "ok": True,
                      "detail": f"weekly_updates #{row['id']} marked edited"})
    except Exception as e:
        notes.append(f"CORRECTED, BUT THE ROW WAS NOT UPDATED — {type(e).__name__}: {e}")

    # 5. Last Comment — the report's copy. Read back; say plainly if it is stale.
    lc = None
    if entry.get("jira"):
        try:
            want = re.sub(r"\W+", "", adf_text(md_to_adf(L["bodies"]["jira"])))[:80]
            lc = {"filled": bool(want) and want in re.sub(r"\W+", "", last_comment(L["issue"]))}
            if not lc["filled"]:
                notes.append("Jira's Last Comment field still shows the OLD text — the RAG "
                             "report reads it. Paste the corrected comment into Last "
                             f"Comment on {L['issue']} in Jira (the API cannot write it).")
        except ApiError as e:
            notes.append(f"could not read Last Comment back ({e}) — check it in Jira")
    return {"ok": True, "why": None, "steps": steps, "path": L["rel"],
            "issue": L["issue"], "comment_id": cid, "manual_slack": manual,
            "last_comment": lc, "notes": notes}


# ------------------------------------------------------------------ resync

def resync() -> dict:
    """Refetch both option lists from Jira and rewrite jira_fields.json. Reads Jira;
    writes only the cache. Returns what changed. Never raises."""
    try:
        custs = [c for c in WK._customers().values() if c.get("jira")]
        if not custs:
            return {"ok": False, "why": "no account has a Jira issue to read options from"}
        issue = custs[0]["jira"]
        found: dict[str, list] = {}
        meta = _jira("GET", f"/rest/api/3/issue/{issue}/editmeta")
        for fid in RAG_FIELDS:
            av = ((meta.get("fields") or {}).get(fid) or {}).get("allowedValues")
            if av:
                found[fid] = av
        source = f"Jira editmeta on {issue}"
        if len(found) < 2:
            old = json.loads(WK.FIELDS_PATH.read_text())
            it = old.get("issue_type_id") or "13115"
            cm = _jira("GET", f"/rest/api/3/issue/createmeta/TAM/issuetypes/{it}"
                              "?maxResults=200")
            for f in cm.get("fields") or cm.get("values") or []:
                fid = f.get("fieldId") or f.get("key")
                if fid in RAG_FIELDS and f.get("allowedValues"):
                    found[fid] = f["allowedValues"]
            source = f"Jira createmeta for TAM issue type {it}"
        missing = [f for f in RAG_FIELDS if f not in found]
        if missing:
            return {"ok": False, "why": "Jira did not return options for "
                                        + ", ".join(missing) + " — the cache is unchanged"}
        try:
            doc = json.loads(WK.FIELDS_PATH.read_text())
        except Exception:
            doc = {}
        changes = []
        for fid in RAG_FIELDS:
            new = [{"id": str(o["id"]), "value": o.get("value") or o.get("name")}
                   for o in found[fid] if not o.get("disabled")]
            old = {o["id"]: o["value"] for o in (doc.get(fid) or {}).get("options") or []}
            nw = {o["id"]: o["value"] for o in new}
            nm = (doc.get(fid) or {}).get("name") or fid
            for i in nw.keys() - old.keys():
                changes.append(f"{nm}: added {nw[i]!r}")
            for i in old.keys() - nw.keys():
                changes.append(f"{nm}: removed {old[i]!r}")
            for i in nw.keys() & old.keys():
                if nw[i] != old[i]:
                    changes.append(f"{nm}: {old[i]!r} is now {nw[i]!r}")
            doc[fid] = {"name": nm, "options": new}
        doc["_resynced_at"] = WK._today().isoformat()
        doc["_source"] = f"{source}, read live {WK._today().isoformat()}"
        WK._write_json(WK.FIELDS_PATH, doc)
        return {"ok": True, "changes": changes, "resynced_at": doc["_resynced_at"],
                "why": None}
    except ApiError as e:
        return {"ok": False, "why": str(e)}
    except Exception as e:
        return {"ok": False, "why": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a[:1] == ["--status"]:
        print(json.dumps(status(), indent=2))
    elif a[:1] == ["--adf"] and len(a) == 2:
        print(json.dumps(md_to_adf(WK.post_bodies(Path(a[1]).read_text())["jira"]), indent=1))
    elif a[:1] == ["--slack"] and len(a) == 2:
        print(md_to_slack(WK.post_bodies(Path(a[1]).read_text())["slack"]))
    else:
        print("usage: poster.py --status | --adf <file.md> | --slack <file.md>")

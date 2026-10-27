"""Where mt keeps things. The ONE place a location is decided — every module imports from here.

Two roots, both per user:

  WORKSPACE  recordings, Customers/<slug>/meetings, Team/meetings, DB/
  DB         the SQLite database mt reads and writes (contacts, tracking, weekly updates)

Resolution, first hit wins:

  WORKSPACE  $MT_WORKSPACE → config.json "workspace" → ~/mt-data
  DB         $MT_DB        → config.json "db"        → WORKSPACE/DB/mt.db

Deliberately stdlib-only and import-cycle-free: it reads config.json itself rather than
importing mt.py, because mt.py imports it.
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"


def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:            # missing or broken: mt.config() reports the broken case
        return {}


def _expand(v: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(v))).resolve()


_CFG = _config()

if os.environ.get("MT_WORKSPACE"):
    WORKSPACE, WORKSPACE_FROM = _expand(os.environ["MT_WORKSPACE"]), "$MT_WORKSPACE"
elif _CFG.get("workspace"):
    WORKSPACE, WORKSPACE_FROM = _expand(_CFG["workspace"]), "config.json"
else:
    WORKSPACE, WORKSPACE_FROM = Path.home() / "mt-data", "default"

if os.environ.get("MT_DB"):
    DB, DB_FROM = _expand(os.environ["MT_DB"]), "$MT_DB"
elif _CFG.get("db"):
    DB, DB_FROM = _expand(_CFG["db"]), "config.json"
else:
    DB, DB_FROM = WORKSPACE / "DB" / "mt.db", "default"

RECORDINGS = WORKSPACE / "Recordings"
CUSTOMERS = WORKSPACE / "Customers"
TEAM_MEETINGS = WORKSPACE / "Team" / "meetings"
SEARCH_DB = WORKSPACE / "DB" / "search.db"


def _claude_memory_dir(ws: Path) -> Path:
    """Claude Code's auto-memory folder for a project rooted at `ws`.

    Claude Code names the project folder after the path with every "/" and "." turned into
    "-": /home/a.b/mt-data → -home-a-b-mt-data.
    """
    key = str(ws).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / key / "memory"


# Optional and OFF unless asked for: an assistant's memory folder that the Hygiene →
# Memory area, the files page and the search index may read (and, through approved
# proposals, write). Never guessed — a default would point at whatever Claude Code project
# happens to share the workspace path. Turn it on with "memory_dir" in config.json;
# "memory_dir": "claude" means Claude Code's folder for the workspace.
_MEM = os.environ.get("MT_MEMORY_ROOT") or _CFG.get("memory_dir") or ""
if _MEM == "claude":
    MEMORY_DIR: Path | None = _claude_memory_dir(WORKSPACE)
elif _MEM:
    MEMORY_DIR = _expand(_MEM)
else:
    MEMORY_DIR = None


def _full_name() -> str:
    try:
        n = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=2).stdout.strip()
        if n:
            return n
    except Exception:
        pass
    return getpass.getuser()


# Who "you" are in a transcript: the mic track is labelled with this name.
OWNER = _CFG.get("owner") or _full_name()
# What your AI assistant is called where mt records who did something (posted_by, a
# proposal's history). A label only; lower-case, one word.
ASSISTANT = (_CFG.get("assistant_name") or "assistant").strip().lower()
# Google account attached to macOS Calendar; only used in help text and the gog fallback.
GOOGLE_ACCOUNT = os.environ.get("GOG_ACCOUNT") or _CFG.get("google_account") or ""
# Attendee domains that never identify a customer (your own company).
INTERNAL_DOMAINS = [d.lower() for d in _CFG.get("internal_domains", [])]
# Customer slug → title/domain fragments used to pre-select the customer for a meeting.
CUSTOMER_HINTS: dict[str, list[str]] = _CFG.get("customer_hints", {})


def _accounts() -> dict[str, dict]:
    """config.json "accounts": customer slug → how you write that account. Every field is
    optional: "code" (item-title prefix, e.g. ACM), "label" (short name in titles and
    lists), "display" (the name in a weekly update's Slack title; default label),
    "aliases" (words that mean the account at the start of a description) and
    "file_aliases" (old filename slugs that mean this account)."""
    out = {}
    for slug, a in (_CFG.get("accounts") or {}).items():
        if isinstance(a, dict) and not str(slug).startswith("_"):
            out[slug] = {"code": a.get("code") or "", "label": a.get("label") or "",
                         "display": a.get("display") or a.get("label") or "",
                         "aliases": [str(x).lower() for x in a.get("aliases") or []],
                         "file_aliases": [str(x) for x in a.get("file_aliases") or []]}
    return out


ACCOUNTS = _accounts()
# The recording-policy link under the Record button. config "recording_policy_url" wins;
# otherwise a default for the domain of google_account, once one is known; otherwise the
# sentence shows without a link.
POLICY_BY_DOMAIN: dict[str, str] = {}
_DOMAIN = GOOGLE_ACCOUNT.rsplit("@", 1)[-1].lower() if "@" in GOOGLE_ACCOUNT else ""
RECORDING_POLICY_URL = (_CFG.get("recording_policy_url")
                        or POLICY_BY_DOMAIN.get(_DOMAIN) or "").strip()
# Where "Send feedback" files a GitHub issue: "owner/repo". "" in config turns the GitHub
# button off and leaves Copy (paste it to whoever runs your install).
FEEDBACK_REPO = str(_CFG.get("feedback_repo", "farsc8per/meeting-transcribe")).strip()
# Weekly-update posting (the TAM Jira project). Blank site = Jira posting is off and says so.
JIRA_SITE = (os.environ.get("MT_JIRA_SITE_URL") or _CFG.get("jira_site") or "").rstrip("/")
# macOS keychain service holding the Jira API token, when not in the credentials file.
JIRA_KEYCHAIN = _CFG.get("jira_keychain") or "mt-jira-api"
# Company names that sit in a contact's "company" field rather than naming a person, on
# top of every account's slug, name, code and label (e.g. partners, your own company).
AFFILIATIONS = [str(x).lower() for x in _CFG.get("affiliations") or []]


def summary() -> list[tuple[str, str, str]]:
    """(label, path, where-it-came-from) rows for `mt doctor`."""
    return [("workspace", str(WORKSPACE), WORKSPACE_FROM),
            ("database", str(DB), DB_FROM),
            ("memory", str(MEMORY_DIR) if MEMORY_DIR else "off",
             ("present" if MEMORY_DIR.is_dir() else "absent") if MEMORY_DIR
             else "optional; \"memory_dir\" in config.json")]

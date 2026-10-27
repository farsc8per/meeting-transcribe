"""feedback.py — the "Send feedback" report: a problem or an idea, plus what is installed.

The report leaves this machine (a GitHub issue, or text pasted into a chat), so before it
goes, check() reads it against THIS user's own world — their name, their assistant's
name, every customer and contact in their DB and config — and lists anything that looks
like it should not travel. It is a warning the person reads, not a filter: the dialog
shows each hit and lets them edit or send anyway.

Same idea as release/leakcheck.py, but that file checks the developer's own release and
never ships; this one runs on any install, so every term comes from the running user.
"""
from __future__ import annotations

import platform
import re
import sqlite3

import paths as P

# Free text, so cap what we scan; a report is a paragraph, not a transcript.
CHECK_MAX = 20000
# Short tokens that would match ordinary words; never used as bare terms.
TOO_COMMON = {"test", "none", "internal", "int", "the", "and", "all", "assistant"}

STATIC = [
    (r"\b[\w.+-]+@(?!example\.com\b)[\w-]+\.[\w.]+\b", "email address"),
    (r"\b[CDGU]0[A-Z0-9]{8,10}\b", "Slack id"),
    (r"/Users/[A-Za-z][\w.-]+", "personal path"),
    # A Jira-style key names a ticket in someone's private tracker. Standards that look
    # the same (SHA-256, ISO-8601, RFC-3339 …) are not tickets.
    (r"(?-i:\b(?!(?:SHA|UTF|ISO|RFC|MD|AES|RSA|CVE|IEEE|PEP|ECMA|H|X)-)[A-Z][A-Z0-9]{1,9}"
     r"-\d{2,}\b)", "ticket key"),
]


def macos() -> str | None:
    v = platform.mac_ver()[0]
    return v or None


def _terms() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(v, why, exact_case=False):
        v = (v or "").strip()
        if len(v) < 3 or v.lower() in TOO_COMMON:
            return
        pat = r"(?<![\w-])" + re.escape(v) + r"(?![\w-])"
        out.append(("(?-i:" + pat + ")" if exact_case else pat, why))

    owner = (P.OWNER or "").strip()
    add(owner, "your name")
    for part in owner.split():
        add(part, "your name", exact_case=True)
    add(P.ASSISTANT, "your assistant's name")
    for slug, a in P.ACCOUNTS.items():
        add(slug, "customer")
        add(a.get("code"), "customer code", exact_case=True)
        for v in [a.get("label"), a.get("display")] + list(a.get("aliases") or []):
            add(v, "customer")
    for slug in P.CUSTOMER_HINTS:
        add(slug, "customer")
    for v in P.AFFILIATIONS:
        add(v, "company name")
    for d in P.INTERNAL_DOMAINS:
        add(d, "company domain")
    try:
        con = sqlite3.connect(f"file:{P.DB}?mode=ro", uri=True)
    except Exception:
        return out
    try:
        for name, short, dom in con.execute(
                "SELECT name, short_name, email_domain FROM customers"):
            add(short, "customer")
            add(dom, "customer domain")
            for part in re.split(r"\s*/\s*", name or ""):
                add(part, "customer")
    except Exception:
        pass
    try:
        for name, email in con.execute("SELECT name, email FROM contacts"):
            # Full names only: a first name alone is too often an ordinary word.
            if name and " " in name.strip():
                add(name, "person")
            add(email, "person's email")
    except Exception:
        pass
    finally:
        con.close()
    return out


def check(text: str) -> list[dict]:
    """Each distinct thing in `text` that looks private, with why. Empty = nothing found."""
    text = (text or "")[:CHECK_MAX]
    if not text.strip():
        return []
    found = []
    for rx, why in STATIC + _terms():
        for m in re.finditer(rx, text, re.I):
            found.append((m.start(), m.end(), m.group(0), why))
    # Longest match wins where they overlap, so "Alex Example" is one hit, not also "Alex".
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    seen, hits, end = set(), [], -1
    for s, e, t, why in found:
        if e <= end:
            continue
        end = max(end, e)
        if t.lower() not in seen:
            seen.add(t.lower())
            hits.append({"text": t, "why": why})
    return hits

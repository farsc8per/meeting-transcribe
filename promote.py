"""Mark reviewed and hand off: confirm the voices mt is already sure of.

Until Nov 2026 every voiceprint or live-name match stayed `unconfirmed` in speakers.json,
so a clean meeting still meant hand-editing the file and running `mt relabel`. The owner,
Nov 9: on hand-off, promote to verified every voice that

  (a) a voiceprint matched strongly to a verified contact — cues.strong_match, the same bar
      the review page and awards already treat as "matched" (≥ 0.85, or ≥ 0.75 with a 0.20
      lead over the next print; 179/179 + 8/8 right when measured Oct 7), or
  (b) was named live in the Room panel and matched strongly after the pass (live_strong,
      which livenames only sets for a verified contact).

Everything else stays `unconfirmed` and flagged: Unidentified voices, mixed or impure
clusters, weak matches, names that came from the screen or roster, and names that are not
a verified contact (Person Verification Protocol).

What it never does: enrol, average or otherwise touch a voiceprint. Only the Confirm
button on the review page averages a meeting into a print; this writes speakers.json and
nothing else. Each promotion is stamped `confirmed_by` / `confirmed_at` / `confirmed_basis`
so it can be told apart from a voice the owner confirmed by listening.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import cues
import mt as MT

BY = "ready-to-review"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _known() -> set:
    import livenames
    return livenames.verified_names()


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def judge(key: str, v: dict, known: set) -> tuple[bool, str]:
    """(promote?, why). `why` is the basis when promoted, the reason it stays open when not."""
    name = (v.get("name") or "").strip()
    if key == "system-unknown" or not name or name.lower().startswith("unidentified"):
        return False, "unidentified"
    if v.get("mixed"):
        return False, "several voices in one cluster"
    ss = _f(v.get("self_similarity"))
    if ss is not None and ss < float(MT.config()["min_self_similarity"]):
        return False, f"impure cluster (self-similarity {ss:.2f})"
    if not cues.strong_match(v):
        sc = _f(v.get("score"))
        if v.get("live_named") is not None:
            return False, f"weak live match {_f(v['live_named']):.3f}"
        if sc is not None and str(v.get("source", "")).startswith("voiceprint"):
            return False, f"weak match {sc:.3f}"
        return False, "not a voiceprint match"
    if name.lower() not in known:
        return False, "not a verified contact"
    if v.get("live_strong"):
        ln = _f(v.get("live_named"))
        return True, "named live, strong match" + (f" {ln:.3f}" if ln is not None else "")
    sc, ru = _f(v.get("score")), _f(v.get("runner_up"))
    basis = f"voiceprint {sc:.3f}"
    if sc < cues.STRONG and ru is not None:
        basis += f", {sc - ru:.2f} ahead of the next print"
    return True, basis


def promote(d: Path, known: set | None = None, now: str | None = None) -> dict:
    """Promote the sure voices in <d>/speakers.json. Writes the file only if something
    changed. Returns {"promoted": [...], "open": [...], "line": str}."""
    spf = Path(d) / "speakers.json"
    doc = json.loads(spf.read_text())
    m = doc.get("map", {})
    known = {n.strip().lower() for n in (_known() if known is None else known)}
    now = now or _now()
    promoted, still = [], []
    for key, v in m.items():
        if key == "mic" or v.get("confidence") == "verified":
            continue
        ok, why = judge(key, v, known)
        name = v.get("name") or key
        if ok:
            v.update(confidence="verified", confirmed_by=BY, confirmed_at=now,
                     confirmed_basis=why)
            v.pop("suggestion", None)
            promoted.append({"key": key, "name": name, "basis": why})
        else:
            still.append({"key": key, "name": name, "why": why})
    if promoted:
        spf.write_text(json.dumps(doc, indent=2) + "\n")
    return {"promoted": promoted, "open": still, "line": line(promoted, still)}


def line(promoted: list, still: list) -> str:
    """One line: what was confirmed, what is still open."""
    a = ("Auto-confirmed " + "; ".join(f"{p['name']} ({p['basis']})" for p in promoted)
         if promoted else "Nothing new to auto-confirm")
    b = ("still unconfirmed: " + "; ".join(f"{s['name']} ({s['why']})" for s in still)
         if still else "nothing left unconfirmed")
    return f"{a} · {b}."

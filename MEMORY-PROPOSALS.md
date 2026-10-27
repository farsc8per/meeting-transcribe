# Memory proposals — how the assistant proposes a change to its own memory

**Status: BUILT Sep 28 2026 (Phase 3b).** This is the contract an assistant session follows.
If something here proves wrong, STOP and tell the user. Don't improvise around it.

The assistant's memory (`~/.claude/projects/<project>/memory/`) changes how the assistant
behaves in **every** session. So memory cleanup never happens as a batch in a session.
The assistant **proposes** one change at a time. The user reviews the exact result on the
**Hygiene → Assistant memory** tab of `mt web`. The tool **applies** it only on approval.

**The assistant never applies its own proposal**, and never edits memory files to "pre-do" one.

## The loop

1. The user copies a request from a Finding on the page (or asks in a session).
2. The assistant reads the files involved, writes a draft JSON (below) into
   `$CLAUDE_JOB_DIR/tmp`, and runs:
   `python3 ~/Tools/mt/hygiene_memory.py --propose draft.json`
   It prints the proposal id, plus any **errors** (these must be fixed) and **warnings**.
   Check the result with `--plan <id>` before telling the user it's ready.
3. The user reviews it on the page and approves it, edits the text first, sends it back with
   notes, or rejects it.
4. If it was sent back: `--list` shows the user's notes. Fix the draft, then run
   `--revise <id> draft.json`. Add `"summary"` to say what changed.

**One proposal = one idea.** One merge, one split, or one contradiction. Never bundle.

## Draft format

```json
{
  "kind": "merge | split | retype | contradiction | archive | edit",
  "title": "Fold RAG calibration into weekly updates",
  "reason": "Both files carry the RAG rules and they've drifted: …",
  "conflicts": [
    {"a": {"file": "feedback_x.md", "quote": "exact text from A"},
     "b": {"file": "feedback_y.md", "quote": "exact text from B"},
     "resolution": "Which one is right and why — the user's words if they gave them"}
  ],
  "people": ["Full Name — what changes about them"],
  "ops": [ … ]
}
```

`conflicts` is required for `contradiction`. Quote **exactly**, because the page highlights
those lines in the diff. `people` is for any change to a person's name, role or ownership
(Person Verification Protocol). The tool also flags any contact name that disappears.

### Ops, applied in order

| op | fields | what the tool does |
|---|---|---|
| `write` | `path`, `content` (the **complete** file, front matter included), optional `index_line`, `index_section` | Replaces or creates the file. A **new** file needs `index_line` + `index_section` (an existing `## ` heading in MEMORY.md). For an existing file, `index_line` replaces its current line. |
| `archive` | `path`, `into` | Moves the file to `archive/` (never deletes). Every `[[link]]` to it is repointed to `into` (a memory `name:`), and its MEMORY.md line is removed. `into: ""` only when nothing should point anywhere. The preview warns if links would then point into archive/. |
| `keep_history` | `path` | Copies the file **verbatim** to `archive/<stem>_history.md`, renaming it `<name>-history`. Use it before a `write` that shortens a file (a split). The tool makes the copy, so nothing is lost in transcription. |

Never write `MEMORY.md` or anything under `archive/` directly. The tool maintains both.

### Recipes

- **Merge A into B:** `write` B with the merged content, then `archive` A `into` B's name.
- **Split a big file:** `keep_history` X, then `write` X with the short current state. Link
  to `[[<name>-history]]` if the detail is worth reaching.
- **Retype:** `write` the file with the new `type:`. Change nothing else.
- **Contradiction:** `write` the file(s) with the wrong statement corrected, plus `conflicts`.
  Dropped lines are listed to the user as "leave your assistant's memory entirely". That's
  expected here, and it's exactly what they have to read.

## What the tool refuses (the preview shows these as errors)

- Any file the proposal touches **changed since it was proposed** (another session wrote it).
  Re-read the file and `--revise`.
- A link that resolves today would break, or MEMORY.md would point at a missing file.
- Missing front matter `name:` / `description:`, a `type:` outside user/feedback/project/reference,
  or a `name:` another live file already uses.
- A new file with no `index_line` / `index_section`.

## Where things live

- Proposals: `~/Tools/mt/hygiene/proposals/<id>.json`. They're never deleted;
  a finished one keeps its status (`applied`, `rejected`, `undone`).
- Before every apply: `~/.local/share/mt/hygiene-memory/<stamp>-<id>/`, with a `.tar.gz` of the
  whole memory folder plus before/after copies of each touched file. Outside the workspace,
  outside memory.
- The log: `hygiene_log` rows with `area='memory'` in the tracking DB. Undo is on the page.

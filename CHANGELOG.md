# Changelog

All notable changes to `mt`. Versions follow [SemVer](https://semver.org): while we are
on 0.x, a minor bump (0.5 → 0.6) may change behaviour or config; a patch bump (0.5.0 →
0.5.1) is fixes only. 1.0.0 = a second person has installed from the README alone and used
it for real meetings for a week.

## [Unreleased]

## [0.7.7] — 2026-10-27

**After updating: restart `mt web`.** No rebuild needed.

- **Meeting rows are calmer.** On the Meetings and Archive pages the tags (customer,
  to review, summarized, tracked, text only) sit on their own line under the title instead
  of beside the buttons, so long titles show in full and the details line stops wrapping.
  A meeting with no tags keeps a two-line row.

## [0.7.6] — 2026-10-27

**After updating: restart `mt web`.** No rebuild needed.

- **Freeing working files now checks the FLAC is there — every way it happens.** The
  Storage panel's one-click free, Delete… → free working files, archiving, and the
  queue after a call all go through one check: a working copy of the system track is
  deleted only if `system.flac` is in the meeting folder, a mic copy only if `mic.flac`
  is. Without its FLAC, that copy may be the last audio the meeting has, so it stays and
  the meeting says "kept: no FLAC to rebuild them from". The panel's "Free …" figure
  counts only what will really go. Remove audio is unchanged (the audio goes to the
  trash first, so it can be restored).

## [0.7.5] — 2026-10-27

**After updating: restart `mt web`.** No rebuild needed.

- **Working files go as soon as a call is processed.** When the queue finishes a meeting
  cleanly, it deletes that meeting's `work/*.wav` (the 16 kHz copies, ~127 MB a meeting)
  if a FLAC is there to rebuild them from. The FLAC stays, so you can still listen,
  re-process, measure vocal cues or teach mt a voice until you remove the audio yourself.
  New config `trim_after_pass` (default on; `false` = wait for archive).
- **Free up space.** Every meeting row has a **Delete…** button with three levels: free the
  working files (nothing lost), remove the audio but keep the transcript, summary and
  names, or delete the meeting. The Archive page gains a **Storage** panel: how much each
  kind takes, a one-click "free working files" (13 of 19 GB on the first real install),
  suggested test recordings, suggested old audio, and the trash.
- **A trash, outside the workspace.** Removed audio and deleted meetings wait there
  `trash_days` (7) and can be restored from the Archive page. Files mt did not write are
  never moved; a meeting that action items point at can't be deleted.
- **Archiving drops the working files** (`trim_on_archive`, default on), so they no longer
  pile up.
- **Archive search.** Filter the archive by title, customer or date, or by anything said.
- `mt enroll`, re-process and vocal cues refuse on a meeting whose audio was removed; the
  Review page says so where the player was.
- New config: `trash_days`, `audio_keep_days`, `trim_on_archive`, `trash_dir`
  (`$MT_TRASH`). `mt doctor` shows the trash folder.

## [0.7.4] — 2026-10-27

**After updating: run `./make.sh` (two helpers changed) and restart `mt web`.**

- **Weekly: your Jira option cache moved to your workspace.** `jira_fields.json` now lives at
  `<workspace>/DB/jira_fields.json` (it is your company's data, not part of mt). Reason-code
  definitions shown as help come from its optional `_reason_help` block. An install that
  still has the old file next to `mt` keeps using it until the workspace copy exists;
  Resync writes the new location.
- **Call detection learns your organisation from config.** Teams titles a meeting window
  `<meeting> | <your org>`; mt now passes `| <org>` for each of your `internal_domains` to the
  screen watcher (`mtwatch --live-title`), instead of one built-in name.
- **Calendar helper has a neutral app id** (`local.mt.mtcal`). No new permission prompt: macOS
  keys Calendar access to the helper's signature and the terminal that runs it, and neither
  changed. (The 0.7.4 release notes said it would ask again; it does not.)
- **MIT license.** `LICENSE` at the top level; README says the downloaded models keep their own
  licenses.
- **Stop frees the machine; the work after a call waits its turn.** Stop used to run the
  whole pass — transcript, vocal cues, laughter — in the recorder's process right away;
  one customer call was still being scanned for laughter 20 minutes later and the Mac
  crawled. Now Stop releases the mic and system audio, queues the work and exits in
  seconds. A queue worker runs it one meeting at a time, never alongside a recording:
  starting a recording freezes a running job on the spot (`SIGSTOP`) and it resumes
  20 s after the recording ends. Jobs also wait while the machine is busy (CPU ≥ 70% or
  critical memory pressure) or a calendar meeting is about to start; **Process now** skips that.
  New `mt queue` command; `mt live --no-queue` keeps the old behaviour. New config keys
  `queue_settle_sec`, `queue_busy_cpu`, `queue_meeting_hold_min`.
- **The Today view shows everything still running.** The Processing card now follows a
  pass through live names, vocal cues and laughter instead of disappearing once the
  transcript is written; a frozen job says *Paused while you record*; queued and failed
  jobs are listed under *Waiting to process* with Retry / Dismiss.
- **Stop and transcribe no longer looks ignored.** The panel is redrawn every 2 s, which
  put the button straight back while the recorder was still stopping (and could swallow a
  click mid-press). It now stays at "stopping…" and the panel holds still while you press.
- **Step timings.** Each queued job logs wall and CPU time per stage to
  `run/queue/timings.jsonl`, so a slow step shows up as a number.
- **Send feedback** (top right, every page): pick **Problem** or **Idea**, write a title and details, then
  **Open on GitHub** (a new issue, filled in and labelled `bug` or `enhancement`, for you to
  submit) or **Copy** (to paste into Slack or email). Version, release, macOS and page are
  added unless you untick them. Before either, mt reads the text for your name, your assistant's,
  and every customer and contact in your own DB and config, plus emails, Slack ids and personal
  paths, and lists what it finds; edit it out or press again to send as is. New config key
  `feedback_repo` (defaults to this project's repo; `""` turns the GitHub button off).
- **+ Add entry** under an item's Description: starts a new paragraph at the end stamped
  `[2026-10-26 14:05 CT — Alex] ` (date, local time, your first name from `owner` in
  config.json) and puts the cursor after it. Nothing is written until Save, which takes the
  usual rev-guarded path, so the change log and conflict check apply as before.

## [0.7.3] — 2026-10-20

- **Security: `mt web` refuses requests from other web pages.** Binding to 127.0.0.1
  kept other machines out, but any page open in the browser could still POST to it (for
  example to start a recording), and a DNS-rebound name could read `/api/*` responses.
  Requests now need a `Host` of `127.0.0.1:<port>` or `localhost:<port>` and a matching
  `Origin` when one is sent. Cross-site requests get a 403, except a link that opens a
  page, and that never covers `/api/*`. The UI, curl and scripts are unaffected. Found
  in a tester's security review. Restart `mt web` after updating.

## [0.7.2] — 2026-10-19

- README: the opening now says what mt does, instead of comparing it to another product.

## [0.7.1] — 2026-10-19

- **One header on every page.** The title carries the version underneath; the date and
  Hygiene sit on the same line; the menu (Meetings · Tracking · Weekly · Files · Contacts ·
  Help) is one row below, identical on every page with the current one underlined, so links
  no longer move as you go between pages.
- **About panel.** Click the version (or open `/#about`) for what's inside this install:
  release, database schema, Python and package versions, when each recorder was built, and
  each model's checksum. Paste it into bug reports. Also `GET /api/version`.
- **Recording-policy line** under the Record button: "Recording? Understand your company's
  recording policy." `recording_policy_url` in config.json makes it a link.

## [0.7.0] — 2026-10-19

The first release from the clean release repo: nothing in it names a real customer, contact
or the developer, and nothing personal is switched on. **Coming from 0.6.0:** add the new
keys you need to `config.json` (below; see `config.example.json`). No `make.sh` re-run and no
database change.

- **Your accounts live in config, not code.** `accounts` (per customer slug: `code` for
  item titles, `label`, `display` for a weekly update's Slack title, `aliases`,
  `file_aliases`) and `affiliations` (company names that can appear where a person's name is
  expected). Item-title codes, project-name prefixes and the contacts page's Account picker
  read them, or the `customers` table.
- **Slack channels come from the database.** Each account's channel is its
  `customers.slack_internal`, written `#channel-name (<channel id>)`.
- **Jira posting needs `jira_site`.** Without it Jira posting is off and says why. The
  keychain service for the token is `jira_keychain` (default `mt-jira-api`).
- **Assistant memory is off by default.** Hygiene → Memory, the files page's memory side and
  memory search appear only when `memory_dir` is set (`"claude"` = Claude Code's memory folder
  for your workspace).
- `assistant_name` (default `assistant`) labels what your AI assistant did (who posted a
  weekly update, who proposed a memory change).
- **Removed:** the legacy layout that found a database by its file name — set `workspace` and
  `db` if you relied on it. `internal_domains` no longer has a default.
- Weekly drafts write their never-posted block as `*(Note for you — …)*`; older drafts with
  another word after "for" are still recognised.

## [0.6.0] — 2026-10-19

First release for installs other than the developer's. Clean-clone verified Oct 19 2026: cloned from
GitHub, ran `make.sh`, `mt doctor` all good, transcribed a file end to end, and got a 200 from
every `mt web` page and API on an empty install. **Coming from 0.5.0, re-run `./make.sh`:**
it adds models, Python deps and Swift binaries. Code is on GitHub (private) and in a release
zip; see INSTALL.md, "Getting the code".

- **Predecessors on the tracking list — R4.3** (Oct 13 2026). Add *comes after* / *unblocks*
  links in an item's sheet (`#id`, optional lag in working days, *Met anyway*). Rows show
  `after #` / `→ unblocks #` / `blocked by #` / `⚠ conflict`; inside a project the list
  groups into steps. New *Ready to start* quick view and a Sequence filter. *Push
  dependents* previews every planned date that would move (due dates never do), applies
  them as one batch, and offers Undo. Works on due dates alone — no cleanup needed first.

- **Bulk: move items into a project or under a parent** (Oct 12 2026). The bulk bar gains
  *Project* and *Sub-item of* (`#id`, empty = top level). Each row follows the edit
  sheet's rules — a parent's sub-items move with it, a sub-item follows its parent, nesting
  stops at one level — and anything refused is listed with the reason.

- **Hygiene: the item tree — R4.2c** (Oct 12 2026). New groups: umbrella items → projects
  (opt-in per row, nothing pre-ticked; undo demotes), one-project-per-family decisions for
  parents whose sub-items are split across projects, sub-items of sub-items flattened, and
  sub-items / parents brought into the same project. Choices show project names, not ids.

- **Promote / demote — R4.2b** (Oct 12 2026). *Promote to project…* on an item's sheet and
  *Demote to item…* on a project's sheet, each a preview of exactly what changes (name
  suggested from the description, editable) that is refused if the data moved since.
  Promote creates the project, makes the sub-items its items, copies Jira/Monday links and
  closes the item with a pointer (`→ project #N`). Demote reopens the original item at the
  status it had, or creates one, and asks per row for anything that would nest three deep.
  Nothing is deleted.

- **Tracking data model — R4.2a** (Oct 12 2026). Behind an explicit migration
  (`tracking.py --migrate-r42 --live <path>/DB/<your>.db`, backup first; never on a save):
  start / end / completed dates, item type (task · story · milestone), `rev`, a change log
  (`item_events`, written by triggers so every writer is caught) and a predecessors table.
  Saves are checked against the version you opened — if your AI assistant or another window
  changed the row, you see what changed and choose keep mine / take theirs. Nesting is held to one
  level, a sub-item follows its parent's project, and moving a parent's project carries its
  sub-items. The migrate/rollback commands now refuse the live database unless `--live`.

- **Tracking list clean-up — R4.1** (Oct 12 2026). Sort by due / priority / status / title /
  updated / newest, on the server (the list is paged), remembered per browser; undated rows
  stay last and sub-items stay under their parent in every sort. Nesting is shown: indent,
  `↳ #N` (opens the parent) and `N sub`. A **project sidebar** with open / done / overdue
  and the due-date span, plus a Project filter. **Quick views**: Mine, My overdue, Due this
  week, Waiting on others. Owners grouped by contact with a **Mine** toggle (config `owner`,
  exact match — never a prefix). A Due filter. Click a due date to change it in place. The
  `(legacy)` filter options, the Needs cleanup tab and the legacy stat are gone (0 legacy
  values left; the Hygiene page still catches drift). Five indexes on `action_items`,
  created on the first save. `mt web` needs a restart.

- **Live keeps up instead of falling behind** (Oct 8 2026). On the Oct 7 CAB the live view
  ran 4.5–6 minutes late: with Meet presenting, one live whisper run took 60+ s, and each
  chunk then swallowed the whole backlog. Now chunks are at most 20 s, and past 30 s
  behind live jumps to a pause near "now" (`⏩ live skipped …`, only when the skipped audio
  had sound). Laughter tagging moved off the loop to a sampler that drops chunks while
  busy. Replayed at 12x real time, live stayed within ~30 s throughout.
- **Live names: per line, then firmer as a voice talks.** Each far-end line is named from
  its own voice, not its chunk's; a Speaker N group's average names it at 0.62 once it
  has 8 s of speech, and shows "maybe X?" from 0.55. Speaker cards appear after 2 lines
  (was 3). CAB first 10 min vs the full pass: wrong live names 17% → 6%.

- **Separate speaker picker + picks remembered per desk** (Oct 6 2026). `mt web` has a
  **Speaker** dropdown next to Microphone; the CLI has `--speaker-id`. The speaker is set as
  the macOS default output *before* capture starts (new `build/mtaudio`, CoreAudio), never
  during a recording. Any hand pick of mic or speaker is saved in `audio_prefs.json` against
  the set of external devices connected, so docking/undocking at home, the office or on the
  road brings back the pick you made there instead of resetting it. `mt devices` shows the
  speakers and what is remembered; `--forget` clears it. **Run `./make.sh` (or the one
  `swiftc` line for mtaudio) after updating** — without it the speaker picker stays hidden
  and mic memory still works.

Install anywhere. Verified Oct 2 2026 by a clean clone into a new folder: `make.sh` from
nothing (all models downloaded + checksummed), `mt doctor` all good, a file transcribed end
to end, every `mt web` page and API 200 on an empty install, nothing written outside it.

- **Data location is configurable.** `paths.py` resolves workspace + database from
  `$MT_WORKSPACE`/`$MT_DB`, `config.json` `"workspace"`/`"db"`, the original layout
  (auto-detected), or `~/mt-data`. Existing installs need no change.
- **mt owns its database schema** (`schema.sql`, v1). `mt init` creates a new DB; `mt doctor`
  checks any configured DB against it and lists gaps; mt never alters a DB it didn't create.
  See `docs/DATABASE.md`, including a prompt for importing your own data.
- `make.sh`: prerequisite checks, pinned Python deps (`requirements.txt`), downloads the
  live-transcription model it used to miss, sha256-verifies every model, runs `mt init`.
- New: `mt version`, `mt init`, `config.example.json`, `INSTALL.md` (with an AI-assistant
  install prompt).
- Personal values moved to config: `owner`, `google_account`, `internal_domains`,
  `customer_hints` (meeting-title → customer pre-selection; **was hard-coded — add it to
  your config.json to keep it**).
- Fixed: `--customer` crashed when `Customers/` did not exist.
- Fixed: a second start in the same minute (e.g. a false start on the wrong mic, killed and
  restarted) shared the first one's folder, and the dead start made the live recording look
  stopped in `mt web`. A second start now gets its own `…-r2-<name>` folder.
- `mt web`: the calendar's **Record this** button is never hidden. After a recording it
  reads **Record again** next to the "recorded" tag, so a false start can't lock a meeting out.
- `mt web` recording card: **mic ● system ●** dots show whether sound is arriving on each
  track right now (green = sound in the last 5s, amber = quiet or the file stopped growing,
  grey = nothing written yet; hover for dBFS and how long it's been quiet). Read from the
  tail of the wav being written; no recorder change. The recorder's 5s heartbeat no longer
  floods the live transcript box — it moved behind a "recorder heartbeat" toggle. Its
  `peak` is the loudest sample since the start, never current volume. (Vocal cues, step 1.)
- **Vocal cues (experimental, opt-in) — step 2.** `mt cues <session>` and an **Analyze
  vocal cues** panel on the review page measure pitch, pitch range, pace, loudness, pauses
  and talk-over per transcript line (`cues.py`, ~2 s per meeting), then show each
  *confirmed* speaker's **Energy** and **Friction** against their own normal: other
  analyzed meetings when there are enough, else this call. Colour key, words, hover
  detail, click to hear. Scored live against the current `speakers.json`, so confirming a
  speaker later needs no re-run. No new dependencies (numpy YIN pitch tracker). Refused
  while recording; the web job runs as a low-priority subprocess.
- **Vocal cues — step 3: live.** A **Vocal cues** checkbox when starting a recording
  (remembered per browser) runs `mt live --cues`: each transcribed chunk is measured per
  segment into `cues_live.jsonl`, and the meeting is measured again after the full pass so
  the review page needs no click. **Open Room ↗** on the recording card pops out `/room`:
  one card per speaker — you, a voiceprint match, or the blended far end — with Energy and
  Friction, the word, a trend line and the baseline dots, refreshed every 2 s. Measuring
  can never cost the live transcript a line: it runs after the line prints, inside a guard.
- **Step 4: conversation + meeting awards (`awards.py`).** Every review page gets a
  **Conversation** section: talk share, longest run, talk-overs started/received (each
  **checked against the audio** — on the Sep 4 change control only 29% of timestamp
  overlaps were real), who started over whom, and slowest answer to a question. Plus
  per-meeting awards — 🎤 The Monologuer, ⚡ The Interrupter, 🛡️ Iron Floor, 🤐 The Strategic
  Silence, 📢 Main Character, 🔥 Spice Level, 😂 Best Saying — on by default (`fun_labels`),
  hidden in one click for screen-sharing, unconfirmed voices as "Speaker N". **Best Saying**
  is the line the room laughed at: laughter is detected with sherpa-onnx's AudioSet tagger
  (new model, Apache-2.0, 26 MB, fetched + checksummed by `make.sh`), candidates show with
  their lead-in, and **👑 Crown** (on a candidate or any transcript line) always wins. Live:
  "Awards so far" in the Room panel. **Re-run `./make.sh`** on other installs for the model.
- **Naming people live (`livenames.py`).** Room cards can be corrected during the call:
  ✓ / ✎ on a "maybe X" near-miss (its own card after 3), ✎ Not them on a voice match, ✎
  Name this voice on the blended far end. The voice is learned for the rest of the call
  (`live_names.json`; the live loop and live transcript pick it up), and after the full
  pass the review page opens pre-filled — verified only when the cluster is clearly that
  voice and the name is a verified contact. Prompted by a speaker with no voiceprint
  matching a colleague's print at 0.62 on an Oct 6 call.
- **The far end splits into Speaker 1, 2, … live.** Each far-end segment gets a voice
  fingerprint; the Room groups unmatched speech by voice (numbers never shift), shows the
  closest voiceprint as "maybe X?", and folds voices quiet for 5 minutes under "Earlier
  voices". Naming a Speaker covers everything that voice says. Grouping by voice instead
  of by guess matters: one voice on an Oct 6 call was being called both "maybe Alex
  Example" and "maybe Sam Sample".
- **Room: see what each voice just said, and change your mind.** Every card shows the
  voice's last lines (and the name picker repeats them), so you can name someone from
  what they're talking about. A voice you named gets ✎ Rename / ✕ Unname. After the pass,
  live names are pre-filled on the review page as "named live by you — strong/possible
  match" and **always wait for your Confirm** — previously a strong match was confirmed
  automatically, so the review never asked.
- **Live awards: talk-overs are checked against the audio during the call too.** On an
  Oct 6 partner call the mic owner was "The Interrupter" live (22×) while muted — only 1
  had both sides talking; the far end's line ends run long. Each live talk-over now needs
  both tracks audibly active, read by seeking 1 s in the growing recordings (no recorder
  contact, ~10–30 ms a refresh).
- **Review: clicking your own line plays YOUR mic.** The player only ever loaded the far
  end, so your lines — and your own Vocal-cues blocks — seeked into the other side's
  silence. Every seek now plays the track its line was recorded on, switching as needed;
  a label beside the player says which side you're hearing.
- **Live naming keeps one voice sample per group you name**, not one blend per person. On
  an Oct 6 call a contact rang back from another phone; the blend scored 0.67
  against their own cluster (no pre-fill), the best single group 0.94. Matches ≥ 0.85 to a
  verified contact are now confirmed outright, even when a second cluster is the same
  person on another phone.
- **Strong voiceprint matches count without a Confirm.** A voice matched at ≥ 0.85 (or
  ≥ 0.75 and 0.20 clear of the next print) now gets vocal cues and award names, tagged
  "matched", and no longer counts as "to review". Measured over every confirmed group on
  disk: 179/179 right at ≥ 0.85, 8/8 in the clear-lead band, 2 of 5 below 0.75. A wrong
  one is undone with **✗ Not <name>** under Voices, which also says whose print reached for
  someone else. Confirm stays optional — it averages the meeting into their voiceprint,
  which is the only thing a match never does on its own. The review page used to say
  Confirm "never touches the voiceprint" when a print existed; it does, and now says so.
  New meetings also record the runner-up print score (`runner_up` in speakers.json).
- Room cards flow into **columns** (each ≥ 280 px) and re-place themselves when the window
  is resized — one column in the default pop-out, more as you widen it. Cards keep a
  stable position (first appearance, you last) instead of re-sorting by who's talking,
  which made them jump between columns; the speaking highlight shows who's on.

## [0.5.0] — 2026-10-02

Baseline. First version under source control; built and used daily by the developer since
Sep 3 2026. Not yet packaged for anyone else — still wired to the developer's own workspace.

- Dual-track capture (system audio + mic) via ScreenCaptureKit, no virtual audio driver
- Local transcription with whisper.cpp (large-v3-turbo); live mode on small.en
- Speaker diarization + identification against enrolled voiceprints (sherpa-onnx)
- Screen OCR of meeting-app speaker names (Zoom/Teams/Meet) via `mtwatch`
- Calendar lookup for meeting naming via `mtcal`
- `mt doctor` permission and toolchain checks; system-audio health + recapture
- `mt web` local app: record, review, search, contacts, weekly updates, tracking, hygiene

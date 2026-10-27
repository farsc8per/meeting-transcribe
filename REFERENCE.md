# mt — full reference

Everything the tool exposes, every knob, and where in the pipeline each one acts. Written
so that after a real meeting you (or your AI assistant) can go from *"this came out wrong"* to
*"change this one value"* without re-reading the source.

`README.md` is the narrative version — why the design is the way it is, with the measured
evidence. This file is the exhaustive one. `cheatsheet.html` is the quick card.

Flags below were generated from the argument parser, not written from memory.

---

## 1. Quick start

```bash
mt live --customer acme --name acme-weekly --title "Acme // Weekly"   # record + live text, then the good pass
# Ctrl-C when the meeting ends
```

`mt` is on your PATH (`~/.zshrc`). Everything runs locally: no API key, no upload.

---

## 2. Commands

| Command | Takes | Does |
|---|---|---|
| `mt live` | — | Record, print a rolling transcript, then queue the accurate pass on Ctrl-C. **The default choice.** |
| `mt queue` | `[action] [id]` | The after-call queue: what is waiting and why. `now <id>` skips the busy/calendar holds, `retry <id>`, `dismiss <id>` drops a failed job (the recording stays), `add <session>` queues a recording that was never processed. See *The after-call queue*. |
| `mt record` | — | Record and transcribe, without live text. |
| `mt process` | `path` | Transcribe a session directory, or any audio/video file. |
| `mt enroll` | `session` | Attach a real name to a detected voice, permanently. |
| `mt speakers` | — | List enrolled voiceprints, with sample counts and anything retired. |
| `mt forget` | `"Full Name"` | Retire a voiceprint. Moved aside, **never deleted**. `--reason` records why. |
| `mt restore` | `"Full Name"` | Put a retired voiceprint back, byte-identical. |
| `mt rename` | `"Old" "New"` | Fix the spelling of a voiceprint name. `--sessions` also rewrites past transcripts. |
| `mt recapture` | — | Tear down and rebuild the system-audio tap on a running capture. Safe any time. |
| `mt relabel` | `session` | Re-render after hand-editing `speakers.json`. |
| `mt cues` | `session` | **Experimental, opt-in.** Measure vocal cues (pitch, pace, pauses, talk-over) and print each confirmed speaker's Energy/Friction against their own normal. `--all` opts every meeting in, `--force` re-measures, `--clear` deletes the measurements. Refused while recording. |
| `mt search` | `"query"` | Full-text search every meeting, summary, recap and memory file. See §7. |
| `mt index` | — | Build or refresh the search index. `--rebuild` starts over. |
| `mt devices` | `--forget` | List microphones and speakers, mark the current system defaults, say which ones `mt` would pick at this desk, and show the picks it remembers per desk. `--forget` clears them. |
| `mt doctor` | — | Check binaries, models, python env, voiceprints, permissions. Run this first when anything misbehaves. |

### Per-command flags

Every flag is optional. `record`, `live` and `process` share a common set; the extras are
listed separately.

**Shared by `record`, `live`, `process`**

| Flag | Meaning |
|---|---|
| `--customer SLUG` | File output under `Customers/<slug>/meetings/YYYY-MM-DD-name/`. The slug is the folder name under `Customers/` (e.g. `acme`, `globex`). Omit for internal calls → lands in `Recordings/`. |
| `--name SLUG` | Folder name for the meeting. Combine with `--customer`; without it you get a generic `…-meeting`. |
| `--title "TEXT"` | Heading inside the transcript. Spaces fine. |
| `--roster "A,B,C"` | Expected attendees. Pins the far-end voice count and pre-names in speaking order. Recorded as a **guess** — outranked by voiceprints and by on-screen evidence. |
| `--speakers N` | Exact number of far-end voices. Strongest lever against over-splitting on long calls. |
| `--owner NAME` | Your name for the mic track. Defaults to `owner` in `config.json`. |
| `--language CODE` | Default `en`. |
| `--prompt "TEXT"` | Override the glossary prompt for this meeting only. |
| `--diarize-mic` | Split your own track by voice too. |
| `--in-person` | Several people sharing your mic in a room. Implies `--diarize-mic`. Forgot it? `mt process <session> --in-person --no-system` re-splits afterwards. |
| `--no-system` | Mic only, in person. The system track is still recorded but not transcribed, and its watchdog is off, so no spoken "System audio check" in the room. The **Mic only** checkbox in `mt web` sets it (and ticks *Several people share my mic*). |
| `--no-screen` | Do not read participant names off the meeting window. |
| `--keep-wav` | Keep the raw WAV instead of archiving to FLAC. |
| `--force-asr` | Re-transcribe instead of reusing a cached pass. |

**`record` and `live` only**

| Flag | Meaning |
|---|---|
| `--mic-id ID` | Specific microphone. Accepts a full CoreAudio UID **or a fragment of the device name** — `--mic-id HyperX` works. See `mt devices`. **Remembered for this desk** (see *Remembered picks* below). |
| `--speaker-id ID` | Speaker (output) to switch macOS to **before** the recording starts — UID or name fragment. Independent of the mic. Remembered for this desk. Never changed mid-recording; the system track is captured whichever speaker the call plays through. |
| `--no-mic` | System audio only — a webinar or playback where you never speak. |
| `--duration SEC` | Auto-stop after N seconds. |

**`record` only:** `--no-process` — record raw audio and transcribe later.
**`live` only:** `--live-model FILE` — ggml model for the live pass. `--cues` — vocal cues (experimental, opt-in): measure each transcribed chunk for the Room panel (`/room` in `mt web`), then measure the whole meeting after the full pass. The **Vocal cues** checkbox in `mt web` sets it. `--no-queue` — run the full pass in the recorder's own process as soon as the call ends (the behaviour before Oct 21 2026) instead of queueing it.
**`process` only:** `--no-mic` — ignore the mic track and transcribe only the system track (a room meeting whose Teams audio already carries everyone). The review page's **Re-process** buttons run `process` with `--no-mic`, `--no-system --in-person`, or neither.
**`enroll` only:** `--speaker N` (the number from the transcript, or the full key like `system-spk2`) and `--name "Full Name"`, both required.

---

## 3. config.json

Defaults for everything. Per-meeting flags override these.

| Key | Default | What it controls, and the basis for the default |
|---|---|---|
| `owner` | your macOS full name | Name applied to the mic track. |
| `workspace` | `~/mt-data` | Where recordings, `Customers/<slug>/meetings` and `DB/` live. `$MT_WORKSPACE` overrides. |
| `db` | `<workspace>/DB/mt.db` | The SQLite database (`mt init` creates it; see `docs/DATABASE.md`). `$MT_DB` overrides. |
| `google_account` | — | Your work Google account in macOS Calendar, used to recognise the work calendar. |
| `internal_domains` | none | Email domains that count as internal. No default — set your company's. |
| `customer_hints` | — | `Customers/<slug>` → meeting-title fragments that pre-select that customer in `mt web`. |
| `accounts` | — | Per customer slug, all optional: `code` (item-title prefix, `ACM - …`), `label` (short name), `display` (name in a weekly update's Slack title), `aliases`, `file_aliases`. |
| `affiliations` | none | Company names that may appear where a person is expected (owner fields), so they are not taken for people. |
| `jira_site` | — | Only for posting weekly updates to Jira; leave out to turn Jira posting off. |
| `jira_keychain` | `mt-jira-api` | Keychain service holding the Jira API token (or use `~/.config/mt/credentials.json`). |
| `assistant_name` | `assistant` | What your AI assistant is called where mt records who did something. |
| `recording_policy_url` | empty | Makes "your company's recording policy" under the Record button a link. Empty = plain text. |
| `feedback_repo` | this project's GitHub repo | Where **Send feedback** (top right, every page) opens a pre-filled GitHub issue, as `owner/repo`. `""` = no GitHub button; Copy still works. |
| `memory_dir` | off | A folder of your AI assistant's memory files, for Hygiene → Memory and search. `"claude"` = Claude Code's memory folder for the workspace. |
| `language` | `en` | Passed to whisper. |
| `threads` | `8` | CPU threads. Matches the M4. |
| `mic_priority` | ordered list | **Which mic to use as you move between home and the office.** Case-insensitive fragments of device names; the first one actually plugged in wins, falling back to the system default if none match. Deliberately excludes webcam and dock mics — they usually sound worst and often *become* the system default when connected. A pick remembered for the current desk wins over this list. |
| `prompt` | glossary prose | **The single highest-leverage accuracy knob.** Seeds whisper with your people, accounts and jargon. Without it, whisper drops punctuation entirely and writes "Joe" for "Jo". Cap ~150 words — whisper reads only ~224 tokens and ignores the rest, so swap terms rather than appending. **Must stay natural prose:** an earlier version used `Terms: a, b, c` and whisper echoed the literal word "Terms:" in front of real speech. |
| `live_model` | `ggml-small.en-q5_1.bin` | Live pass model. 27× realtime, 3.8% disagreement with turbo, matched every domain term. **Do not drop to base.en or tiny.en** — base.en wrote "1500G" for "1500D". |
| `auto_assign_score` | `0.62` | Cosine similarity against an enrolled voiceprint at or above which a name is applied automatically. Measured: same speaker ≈0.91, different speaker ≈0.43. |
| `suggest_score` | `0.42` | Above this it is offered as a suggestion only. |
| `cluster_threshold` | `0.90` | Diarization clustering. **This is a DISTANCE, so higher merges more.** The original 0.55 produced a new speaker for nearly every turn — 71 labels on one real call. |
| `min_duration_on` | `0.4` | Shortest run of speech treated as a turn. |
| `min_duration_off` | `0.5` | Shortest silence that ends a turn. |
| `merge_similarity` | `0.60` | Second-pass merge: two voice clusters this alike are one person. Long calls split one speaker across several clusters — measured at mutual similarity 0.89–0.91. Uses **average** linkage; single linkage chains unrelated people together. |
| `min_speaker_seconds` | `8.0` | Below this total speech, a cluster is a fragment rather than a participant. |
| `fun_labels` | `true` | Meeting awards (🎤 The Monologuer, ⚡ The Interrupter, 😂 Best Saying …) in `mt web`. The **Hide awards** button overrides it per browser — use it before screen-sharing. Set `false` to start hidden. |
| `queue_settle_sec` | `20` | After a recording ends, how long the queue waits before starting or resuming heavy work — the gap in which the next meeting usually gets started. |
| `queue_busy_cpu` | `70` | A queued job does not start while CPU use (sampled over 2 s) is at or above this %, or while macOS reports *critical* (red) memory pressure — not yellow, which a 16 GB Mac with the usual browser and chat apps sits at with 40% free. **Process now** overrides it. A first setting; tune it from `run/queue/timings.jsonl`. |
| `queue_meeting_hold_min` | `10` | A queued job does not start this many minutes before a calendar meeting (until 5 minutes after it began). A hint only: the calendar can be stale. |
| `absorb_similarity` | `0.45` | A fragment this similar to a real speaker is folded into them; otherwise it goes to one shared "Unidentified voice" bucket. Never invents a name to explain a one-second fragment. |

**Remembered picks (`audio_prefs.json`, per install, never committed).** Whenever you pick a
mic or speaker by hand (`--mic-id` / `--speaker-id`, or the dropdowns in `mt web`), `mt`
saves it against the *desk*: the set of external audio devices connected at that moment
(built-in, virtual Teams/Zoom, Continuity iPhone and AirPlay devices are ignored). Next
time, the same desk gets the same picks. A desk with one extra device plugged in (AirPods)
matches the most similar saved desk. A remembered device that isn't plugged in is skipped.
Order for the mic: your pick now → remembered for this desk → `mic_priority` → system
default. For the speaker: your pick now → remembered for this desk → leave macOS alone.
`mt devices` shows what is remembered; `mt devices --forget` clears it.

### Constants in `mt.py` (edit the source)

| Constant | Value | Why it exists |
|---|---|---|
| `HIGHPASS_HZ` | `80` | Rumble and mains-hum rejection, applied before anything else sees the audio. Measured: a USB headset through a DisplayLink dock had **88% of its noise energy below 120 Hz** and a 60 Hz peak **2253x the median**; an 80 Hz high-pass gained **+6.7 dB SNR**, and even the clean HyperX gained **+3.7 dB**. Only ~2% of speech energy is below 80 Hz and transcripts agreed 97.2% with every domain term intact. Do not raise much above 100 Hz — male fundamentals start near 85 Hz. |
| `MAX_GAIN_DB` | `18.0` | **Load-bearing. Do not remove.** Unbounded `loudnorm` applies +34 dB to a near-silent track, amplifying the noise floor and faint speaker bleed until whisper transcribes them as speech. That is how a muted mic "said" *Thank you* thirty times. |
| `TARGET_SPEECH_RMS` | `0.06` | Level the loud part of a track is normalised toward. |
| `MIN_SPEECH_RMS` | `0.010` | After gain, a block quieter than this is noise, not speech, and is dropped. A −65 dBFS bleed-only track dies here; genuine −43 dBFS speech survives. |
| `EMBED_CAP_SEC` | `60.0` | Hard cap on audio fed to the speaker-embedding model. TitaNet's encoder aborts past ~12288 frames (~123 s). Truncate the array — breaking out of the accumulation loop is not enough. |

---

## 4. The pipeline, in order

Where to intervene, with the function that does the work. All in `mt.py` unless noted.

| # | Stage | Function | Notes |
|---|---|---|---|
| 1 | Capture audio | `build/mtcapture` (Swift) | System audio + mic as two synchronised 16 kHz mono tracks via ScreenCaptureKit. |
| 1b | Watch the screen | `build/mtwatch` (Swift) | OCRs meeting windows every 2 s → `screen.jsonl`. |
| 2 | Convert + bounded gain | `to_16k_mono` | Skipped if the 16 kHz copy is current. |
| 3 | Find speech + voices | `diarize` | pyannote segmentation + TitaNet embeddings. Produces turns. |
| 4 | Level gate | inline in `cmd_process` | Drops blocks below `MIN_SPEECH_RMS`. |
| 5 | Build speech blocks | `speech_blocks` | Merges turns; silence is never sent to the recogniser. |
| 6 | Transcribe speech only | `transcribe_speech` | Concatenates blocks with 0.4 s separators, one whisper call, maps timings back. **This is what stops the repetition loop.** |
| 7 | Strip artifacts | `drop_repeats`, `strip_artifacts` | Looped phrases, sound captions, echoed prompt labels. |
| 8 | Snap to speech | `snap_to_speech` | Pulls whisper timings onto real speech; drops segments with no speech under them. |
| 9 | Consolidate voices | `consolidate` | Average-linkage merge, fragment absorption, renumber by speaking time. |
| 10 | Assign segments | `assign` | Each segment to the cluster it overlaps most. |
| 11 | Name the voices | `match_voice`, `name_from_screen` | Voiceprints, then on-screen active speaker, then roster order. |
| 12 | Cross-track de-bleed | `drop_bleed` | Only judges segments of 8+ words. |
| 13 | Render | `render_markdown` | |
| 14 | Archive | inline in `cmd_process` | WAV → FLAC, prunes transient intermediates. |

Screen parsing lives in `screen.py`: `view_of` (is this window a live call?), `clean_name`,
`build`, `roster`.

---

## 5. Files

### A session directory

| File | Keep? | What |
|---|---|---|
| `transcript.md` | **yes** | The record. Speaker-labelled, timestamped, with the on-screen roster. |
| `transcript.txt` | yes | Flat text, good for pasting. |
| `transcript.json` | yes | Structured: meta, speakers, per-segment timeline with overlap confidence. |
| `speakers.json` | yes | The name mapping. Edit, then `mt relabel`. |
| `live.md` | — | Rough live notes. Superseded by `transcript.md`. |
| `system.flac` / `mic.flac` | **yes, permanently** | Lossless audio, kept for good. FLAC is compressed but bit-exact — decode it back and you have the original samples, so re-transcribing, re-diarizing or any later audio work loses nothing. 33-min call ≈ 56 MB (was 764 MB as 48 kHz stereo WAV). |
| `screen.jsonl` | — | Raw OCR log, one record per window per tick. Keep while tuning. |
| `screen_roster.json` | — | Parsed roster, headcount, active-speaker observations. |
| `capture.json` | — | Start time, sample rate, and the **resolved microphone** (`mic_name`, `mic_uid`, `mic_requested`). |
| `cues.json` | only if you want cues | Vocal-cue measurements per transcript line — no names; who said what is resolved from `speakers.json` each time it's shown. Present only for meetings you chose to analyze, and only those feed anyone's baseline. Delete it (or `mt cues --clear`) to opt the meeting back out. |
| `cues_live.jsonl` | only if you want cues | Rough per-segment readings written during the call (`--cues`), with the live speaker guess: your mic, a voiceprint match at the auto-assign score, or the blended far end. Kept for the sensed-vs-measured view (step 4). |
| `cues_requested.json` | — | Marker: this meeting was opted into vocal cues at record time, so it is measured after the full pass. |
| `laughs.json` | only if you want cues | Laughter heard in the meeting (sherpa-onnx AudioSet tagging), for the 😂 Best Saying award. Written with the vocal-cue measurement; `mt cues --clear` removes it. |
| `laughs_live.jsonl` | — | Laughter heard during the call, for "Best Saying so far" in the Room panel. |
| `awards.json` | **yes, if present** | Your 👑 Best Saying crown. Your own input, not a measurement — `--clear` leaves it alone. |
| `live_names.json` | **yes, if present** | Voices you named in the Room panel during the call: your picks plus an in-call voice sample per name (`livenames.py`). Read by the live loop each chunk, then used after the pass to pre-fill `speakers.json`. Voice-derived, same class as `work/*-spk*.npy`; never copied into `voiceprints/`. |
| `tracks.json` | — | Do the two tracks hear the same audio? (`tracks.py`, measured from the audio before transcription, cached.) `separate` = normal desk call; `speakers` = the far side leaks into your mic; `same_room` = a room meeting where the Teams track carries everyone (it covers ≥85% of what the mic heard), so the transcript header and the review page offer the Teams track alone; `overlap` = much is on both tracks, but the mic holds speech the call audio doesn't (your own voice) — keep the mic, expect some doubled lines. |
| `reprocess-backups/<stamp>/` | **yes** | The transcript and speaker map as they were before a review-page re-process. Names confirmed on the page don't carry over to a re-process; they are here. |
| `work/` | no | Intermediates. Safe to delete; only makes a re-run slower. |

**Disk footprint.** Measured Sep 14 2026 over 12 sessions: the permanent archive is the
two flac tracks, ~48 MB per meeting. `work/` holds `system.16k.wav` + `mic.16k.wav`, the
mono 16 kHz downmixes whisper and the diarizer actually read — ~127 MB per meeting, or
**2.6x the archive it sits next to**. The post-pass cleanup purges `*.speech.wav` and
`live_*.wav` but *not* `*.16k.wav`, so these accumulate indefinitely and sync to any cloud backup
of your workspace.

Deleting them is surgical and cheap to undo: the expensive cached results in the same
directory (`*.segments.json`, `*.speech.json`, `*-spk*.npy`) are only kilobytes and are
what a re-run reuses. Losing the downmix costs one ffmpeg convert, not a re-diarize.

    find ~/mt-data/Customers -path '*/work/*' -name '*.16k.wav' -delete

Never run it while a pass is in flight — the running session needs its own downmix. At
~50 meetings/month, keeping them costs ~8.7 GB/month against ~2.4 GB/month without.

### The tool directory

| Path | What |
|---|---|
| `mt` / `mt.py` | The command and the pipeline. |
| `screen.py` | Meeting-UI parsing. Also runnable: `./lib/venv/bin/python screen.py <dir>`. |
| `src/mtcapture.swift` | Dual-track recorder. |
| `src/mtwatch.swift` | Window OCR. |
| `build/` | Compiled binaries. |
| `models/` | Models. **2.9 GB as it stands, but only ~780 MB is needed at runtime** (`large-v3-turbo-q5_0`, `small.en-q5_1`, `titanet_small`, `pyannote-segmentation`). `large-v3-turbo.bin` (1.5 GB), `medium.en` (514 MB), `base.en` and `tiny.en` are not used at runtime and are safe to delete — `make.sh` re-fetches what is required. |
| `voiceprints/` | Enrolled speakers. **Private. Back this up — it is the accumulated value.** |
| `config.json` | Defaults. |
| `make.sh` | Rebuild everything. Safe to re-run. |
| `lib/venv/` | Python env (sherpa-onnx, numpy). |

---

## 6. Helper tools

Useful when tuning; you will not need them day to day.

```bash
# Recorder, directly
build/mtcapture --list-mics
build/mtcapture --out-dir DIR [--mic-id ID] [--no-mic] [--no-system-watch] [--duration SEC]
python tracks.py SESSION_DIR               # same audio on both tracks? (what process runs)

# Calendar (EventKit -> macOS Calendar; no password, no OAuth)
build/mtcal                                # today, as Google-Calendar-shaped JSON
build/mtcal --days 2 --offset 1            # tomorrow and the day after
build/mtcal --calendars                    # which calendars macOS can see (diagnosis)

# Screen watcher
build/mtwatch --list                       # every window it can see, with candidate reasons
build/mtwatch --probe                      # OCR the meeting candidates once, as JSON
build/mtwatch --probe --all                # OCR every sizeable window (debugging)
build/mtwatch --watch --out-dir DIR [--interval S] [--duration S]

# Save calendar attendees into a session -> <session>/invitees.json
mt record --invitees "Name One,Name Two"   # or let mt web prefill from mtcal

# Parse a screen log
./lib/venv/bin/python screen.py DIR
```

### Calendar: how it resolves

Two providers, tried in order by `web.fetch_calendar()`:

1. **`mtcal` (EventKit)** — reads macOS Calendar directly. No password, no OAuth client,
   no token refresh, and the Calendars TCC permission is already granted. This is the
   supported path.
2. **`gog`** — fallback only. **Do not build on it.** Its local *file* keyring holds both
   the refresh token and the OAuth client secret (`credentials.json` is 94 bytes:
   `client_id` alone) and the keyring password is not recoverable. Switching the keyring
   backend orphans the token; don't.

`mtcal` reports its calendar `sources`, and `has_work_calendar()` uses that to tell
**"the Google account is not attached"** apart from **"today is genuinely clear"** — both
are zero events, and conflating them would make the page claim an empty day on a full one.

If Today is empty and the sources are only iCloud/Subscribed/Other, the fix is one-time:
System Settings → Internet Accounts → Add Account → Google, sign in, tick **Calendars**.
`mt calendar` prints exactly that, and falls back to the cached `run/today.json` so the
web app keeps working while the account is being attached.

**A corporate network can block Google↔macOS calendar sync** (seen Sep 9 2026). Events
sync at home, land in the local store, and stay readable at the office — EventKit needs no
network — but a meeting *added* while on the corporate network will not appear.

So a successful read says nothing about freshness, and "cached 0 min ago" would be a lie.
`web.calendar_sync()` reads `Store.last_sync_end` from
`~/Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb` (read-only,
best-effort, `{}` on any failure; Core Data epoch = unix + 978307200) and the page shows
**"calendar synced Nh Nm ago"**, escalating to a warning past `STALE_AFTER_MIN` (180) or on
a non-zero `error_id`. Recording is never blocked by this — the name field can be typed.

---

### A session's life, and the tags that show it

| Stage | Marked by | Tag |
|---|---|---|
| recorded | `transcript.json` | — |
| needs review | a voice with `confidence != verified` | `N to review` |
| reviewed | `handoff.json` (written by the Review page) | `reviewed` / `handed off` |
| summarised | `summary.md` (written by your AI assistant) | `summarised` |
| tracked | `action_items.source = 'meeting:<session-id>'` | `N tracked` |
| archived | `archived.json` | moves to `/archive` |

Every stage is a file or a DB row **that already existed** — nothing new was invented to
hold this state, so a tag cannot disagree with the thing it describes. `summarised`
supersedes `handed off` (the handoff is the request; the summary is what it asked for).

`N tracked` is only ever shown as a positive count. Plenty of meetings legitimately raise
no action items, so the absence of that tag must never read as "you forgot something".

**Archiving is a marker, not a move.** `archived.json` is written in place, so every path
already recorded in the DB, a summary, or a Jira comment keeps resolving. It only decides
which of the two lists the session appears in, and `/archive` restores it in one click.

**Summaries** are read through `GET /api/summary?id=<session>` and rendered in an overlay
by `webapp/md.js` — a deliberately small markdown subset, no CDN, offline-first. It escapes
the raw text *first* and only ever adds tags of its own, because summaries are derived from
transcripts and transcripts contain whatever people said. Note it joins the lines of a
paragraph with `<br>` rather than a space: strict markdown folds a single newline, but
these summaries are written one line per fact and folding ran the header block together.

### Recurring meetings and the Record button

`shape_events()` marks a calendar event as already recorded by matching the slug against
session folder names — **scoped to today**. It used to match across all time, which meant a
single recording of a daily meeting suppressed its Record button permanently: one CAB
recording on Sep 9 made the daily CAB call unstartable from the page. The match also
includes folders with no `transcript.json` yet (`todays_session_dirs()`), so a meeting
still transcribing is not offered for recording a second time.

### The web app: what the status card means

`/api/state` answers one question — what is this machine doing right now — and the page
polls it every 2s. Three states, and they are mutually exclusive:

| State | Shown as | Meaning |
|---|---|---|
| recording | red card, pulsing dot, **Stop** button | `mtcapture` is alive and audio is being written. |
| processing | teal card, spinner, progress bar | Capture is done; the transcript is being built. Nothing to stop. |
| stopped | red bar at 100%, "stopped during: …" | The pass died partway. Re-run it with `mt process <path>`. Card expires after 2h. |
| idle | nothing | None of the above. Finished sessions are in the list below. |

**These are not mutually exclusive, and that is the point.** A recording and any number of
transcription passes run at once — back-to-back meetings are the normal case. `/api/state`
returns `recording` plus `passes: [...]`, one entry per in-flight pass, newest first, and the
page stacks a card for each.

### Per-pass run state (Sep 15 2026)

Each recording gets its own `run/pass-<pid>.json` and `run/pass-<pid>.log`. **Keyed by pid,
not session name**, because `web.py` does not know the session directory at spawn time —
`mt.py` derives it. The directory is read back later from the log's own
`==> recording to <path>` line.

This replaced a single shared `run/current.json` + `current.log`, which broke the first time
two meetings overlapped: starting a second recording truncated the first one's log and
overwrote its state, so a 35-minute customer pass went invisible while a discarded 20-second
session took over the card. The transcripts were never at risk — each session owns its own
directory — but the display was wrong, and the dead session's card claimed "processing"
indefinitely. The legacy files are still read (so a pass in flight across the upgrade is not
lost) and never written.

**Everything is reconstructed from disk on every poll.** No progress lives in the server's
memory, so `mt web` can be restarted mid-pass and a page reload shows each pass exactly where
it was. Verified: every non-wall-clock field identical across a restart.

**A zombie is dead.** `os.kill(pid, 0)` *succeeds* on a zombie, so the old liveness check
reported exited passes as alive forever. `_pid_alive()` now also checks `ps -p <pid> -o stat=`
for a leading `Z`. Both halves matter — spawned children are reaped via their `Popen` handles,
and the `ps` check catches any that predate a server restart, when there is no handle to poll.

**`stop_recording()` is anchored to a live capture.** `mt live` is *one* process: it records,
and on Ctrl-C runs the pass in the same pid. So a transcribing pass answers to
`pgrep -f "mt.py live"` exactly like a live recorder. The old unconditional pgrep fallback
could therefore SIGINT someone's transcription. Every path now requires
`MT.active_session()` to be live first, and refuses rather than guess between candidates.

**Why processing needed its own state.** Between "stop" and a finished transcript the
session was invisible in both places: `running()` keys off the live `mtcapture` process,
which has already exited, and the session list keys off `transcript.json`, which does not
exist yet. On a 2h47m call that blind spot lasted the better part of an hour — exactly
when you are in the next meeting wondering whether reloading is worth it. The tab title
carries it too (`⚙ 4/10 · processing`), so a buried tab still reports.

Phases are parsed back out of the recorder's own `==>` stdout rather than having the
pipeline report progress separately, so the two cannot drift apart. Two boundaries matter:
`mt live` prints `running the full pass` when the call ends but `mt record` drops into the
pass with no marker at all, so the log window falls back to the last `recording to` line;
and because the live flow writes real pipeline lines *during* the call, `processing()`
returns nothing whenever `MT.active_session()` is still alive.

The progress bar is **weighted by how long each step actually takes**, not by step count:
transcription is most of the wait, so an evenly spaced bar sat at 30% for twenty minutes
and then jumped.

Two numbers are shown under it, and only one of them is a guess. *Time in this step* is
measured — the log's mtime is when its last line was written, and whisper prints nothing
at all while it works, so the file simply stops changing for the duration of the step. The
*estimate* is shown only for transcription, where the log states the spoken duration.

⚠️ That estimate divides by **6.8x, not the 12.3x in `config.json`** — and the difference
is not machine load. The `config.json` figure is measured against the WHOLE TRACK, but
whisper only sees the speech blocks the VAD kept. Timed on a 2h47m customer call: 1:36:04 of
speech (58% of the track) took 14m06s, which is 6.8x on speech and 11.8x on the track —
the latter agreeing with `config.json`. Using 12.3x here under-estimates the wait by about
half. Do not "correct" it back. Validated on a second track the same run: 30:55 of mic
speech, predicted 4.5 min, actual 4.5 min.

**Rule of thumb:** the whole pass takes about **20% of the meeting length**. The 2:47:02
call ended at 14:51:02 and its transcript was written at 15:24:32 — 33m30s.

If the pass dies partway the card says so and names the recovery command
(`mt process <dir>`) rather than showing a bar that never moves — the audio is already on
disk, so nothing is lost.

### The after-call queue (Oct 21 2026)

**Why.** Stop used to run everything after a call — transcription, voices, vocal cues,
laughter — inside the recorder's own process, straight away. On Oct 21 that process was
still scanning a 48-minute customer call for laughter twenty minutes later (55 s on an
idle machine), the Mac crawled, and a back-to-back meeting could not have been recorded on
top of it.

**What Stop does now.** `mt live` stops the capture, writes a job to `run/queue/` and
exits within seconds. The mic and the system tap are free immediately. One worker
(`mt queue run`, started automatically) runs the jobs one at a time, oldest first:

1. **A recording always wins.** A job never starts while anything is recording, nor for
   `queue_settle_sec` after a recording ends. A job that is *running* when a recording
   starts is frozen with `SIGSTOP` — its whole process group, whisper and ffmpeg included —
   and thawed with `SIGCONT` once nothing has recorded for `queue_settle_sec`. There is no
   checkpointing; it continues from the exact point it stopped. Start in `mt web` freezes
   the job *before* the recorder process is even launched.
2. **A busy machine waits.** A job does not start while CPU use is at or above
   `queue_busy_cpu` or macOS reports critical memory pressure, or while a calendar meeting is about
   to begin. The vocal-cues steps (the heavy optional part) check again before they run.
   **Process now** (or `mt queue now <id>`) skips these holds — never the recording rule.
3. **Everything still gets done.** Cues and laughter run as part of the job when the
   meeting was opted in; nothing is dropped, only deferred.

**What you see.** The job's progress is the usual *Processing* card, now including the
steps after the transcript (live names, vocal cues, laughter), so nothing runs invisibly.
A frozen job says *Paused while you record*. Jobs not started yet are listed under
*Waiting to process* with their position and what they are waiting for; a failed one has
**Retry** and **Dismiss**. The tab title shows `⏳ N queued` when nothing else is going on.

**It survives everything.** All state is files with one writer each — `<id>.json` (the
job), `<id>.run.json` (the worker), `<id>.result.json` (the job, on exit), `<id>.now`
(you). Restart `mt web`, reboot, kill the worker: the next worker (started by `mt web`'s
2 s poll or the next recording) adopts a job that is still running, re-runs one that was
cut off, and leaves a failed one for you. Finished jobs move to `run/queue/done/`.

**Timings.** Each finished job appends a line to `run/queue/timings.jsonl`: wall and CPU
seconds per stage (transcript, live names, waiting, vocal cues, laughter) and the seconds
it spent frozen. A stage whose wall time runs far past its CPU time was starved; that is
how a repeat of Oct 21 shows up as a number. The job log ends with the same summary.

`mt live --no-queue` restores the old inline behaviour. `mt record` still runs its pass
inline. The record-time options are saved as `record_options.json` in the session, so
`mt queue add <session>` processes a recording that never was the way it was recorded.

### Vocal cues and meeting awards (experimental, Oct 5 2026)

Each panel's **Field guide** is
served from `cues.GUIDE`, `cues.ROOM_GUIDE` and `awards.GUIDE` — edit the guide there, not
in the pages.

| Where | What |
|---|---|
| Record-now card | **Vocal cues** checkbox → `mt live --cues`; **Open Room ↗** on the recording card |
| `/room` | Live per-speaker Energy/Friction + "Awards so far" (`/api/cues/live`) |
| Review → **Conversation** | Dynamics + awards for every meeting (`/api/awards`); 👑 crown (`POST /api/awards/crown`) |
| Review → **Vocal cues** | Energy/Friction strips (`/api/cues`); Analyze (`POST /api/cues/analyze`, runs `mt cues` niced) / Clear (`POST /api/cues/clear`) |

**Tuning — `config.json` → `"cues": {…}`** (defaults in `cues.DEFAULTS`; nothing needs
re-measuring after a change, scoring happens per request):

| Key | Default | What it controls |
|---|---|---|
| `bands` | `[-0.8, 0.8, 1.6]` | Cut points (z against the person's normal) between below · normal · elevated · well above |
| `window_sec` | `45` | Smoothing: each reading is about this much of that person's speech |
| `min_window_speech_sec` | `4.0` | Less of their speech than this in the window → grey, not a guess |
| `history_min_lines` | `40` | Lines from *other* analyzed meetings before "their normal" is used |
| `call_min_lines` | `12` | Otherwise this call alone is the baseline, if they said this much |
| `min_speech_sec` | `1.0` | Shorter lines are too thin to measure |

**Live naming (`livenames.py`)** — Room card actions → `POST /api/cues/live/name`
(`kind` maybe/match/them, `from`, `to`) and `/api/cues/live/undo`. A chunk takes a name you
gave when it is ≥ `LIVE_MATCH` 0.62 to that in-call sample (measured on Oct 6: 74% of
that speaker's chunks, 1.1% of other voices). After the pass a cluster is pre-filled at
≥ `POST_SUGGEST` 0.72 and **always left for you to confirm** (changed Oct 6 on user feedback:
a live click shouldn't be final). The source reads "named live by you — **strong** match"
at ≥ `POST_STRONG` 0.85, or ≥ `POST_CONFIRM` 0.75 and ≥ `POST_MARGIN` 0.20 ahead of every
other cluster, for a verified contact; otherwise "possible". A cluster you already
confirmed is never touched. Mid-call, `POST /api/cues/live/rename` (`from`, `to`; no `to`
= unname) moves every decision that named one person to another. Each thing you name
keeps its own sample and a voice is scored against the closest of a person's samples
(someone who changes phones mid-call sounds like two voices). Naming "Them" covers its last
`THEM_WINDOW` 60 s.

**Speaker 1, 2, … live:** the loop stores a voice fingerprint (`v`, 192 floats) per far-end
segment ≥ `VOICE_MIN_SEC` 1.5 s in `cues_live.jsonl` (~33 ms each, model loaded once). The
Room groups unmatched ones online, in time order, at cosine ≥ `VOICE_GROUP` 0.55; a group
needs `VOICE_MIN_SEGS` 2 to get a number (2 since Oct 8, so someone who speaks twice still
gets a card). Numbering runs on what the loop saw, before your
names, so it never shifts (measured Oct 6: 4 groups covered 91% of the unmatched speech
an hour in; 11 by the end of the hour-long training). A name given to Speaker N follows
that group; quiet cards (> 5 min) fold under "Earlier voices". Older recordings with no
fingerprints keep per-guess "maybe X" cards.

**Names firm up as a voice talks (Oct 8).** Each far-end line is first named from its OWN
fingerprint (≥ `auto_assign_score` 0.62; ≥ `suggest_score` 0.42 is a hint), not the
chunk's. Then each unnamed Speaker N group is averaged: ≥ 0.62 with `GROUP_FIRM_SEC` 8 s of
speech names the group; ≥ `LIVE_TENTATIVE` 0.55 shows the name as "maybe X?" for one tap.
The same two tiers apply to voices you named this call. Measured on the Oct 7 CAB, single
lines for one speaker had a median 0.57; their group average 0.69. First 10 min vs the full
pass: wrong live names 17% → 6%, right 56% → 66%. Live laughter tagging runs on a
background sampler that skips chunks while busy; the after-call pass tags everything.

**Constants in `awards.py`:** laughter threshold `0.5` (AudioSet Laughter/Giggle/Snicker/
Belly laugh/Chuckle classes, 2 s windows, 1 s hop, silent windows skipped); talk-over =
≥ 0.3 s timestamp overlap **and** both tracks audibly active in the next second; a
backchannel is < 1.2 s; a run ends after a 3 s pause; "kept going" = ≥ 1.5 s after being
talked over; answer gaps count only when the floor passes, ≤ 10 s. Award minimums are in
`awards.AWARDS` (Monologuer 30 s, Interrupter 3, Iron Floor 2, Strategic Silence 3 s,
Main Character 40%, Spice Level +1.6) and need ≥ 2 eligible speakers.

## 7. Search

Full-text search across every meeting, summary, recap and memory file in the workspace.
Lexical, local, no daemon: SQLite **FTS5**, which is already compiled into the Python in
`lib/venv` — nothing new was installed.

```bash
mt index                      # build or refresh the index (incremental)
mt index --rebuild            # drop and rebuild from scratch (~0.5s over the whole corpus)
mt search "DRBD"              # ranked hits, best first
mt search "gold image" --kind summary --customer acme --since 2026-09-01
mt search "AT&T" --json       # raw hit objects on stdout, for piping
```

The index lives at **`DB/search.db`** — deliberately NOT the main database. It is derived
data, rebuildable in half a second, so a rebuild can never endanger the main DB. `ATTACH` it if you want to JOIN against `action_items`.

### What gets indexed

| kind | source | one row per |
|---|---|---|
| `summary` | `*/meetings/*/summary.md` (Customers **and** Team) | file |
| `transcript_segment` | `*/meetings/*/transcript.json` → `timeline[]` | merged ~200-char block |
| `transcript` | `*/meetings/*/transcript*.md` | file |
| `recap` | `Customers/*/monthly-recaps/*.md` | file |
| `memory` | `memory_dir` `*.md` (off unless set in `config.json`) | file |
| `note` | other `*.md` under `Customers/`, `Reports/`, `Team/` | file |

Skipped: `Inbox/`, `prompt-scratch-pad`, `Tools/`, `build/`, and `transcript.txt` (a
re-rendering of `transcript.md` — indexing both would double every hit).

Meeting artifacts are classified **by shape, not by parent folder**, so a `Team/` internal
meeting summary answers to `--kind summary` exactly like a customer one. The earlier build
filed those under `note`, which meant a "summaries only" filter silently hid 16 real
summaries — the kind of bug that makes a search tool untrustworthy, because you never see
what you missed.

### Staying fresh

`cmd_process` calls `search.index_session()` after it writes `transcript.json`, so a
recording is searchable the moment its pass finishes. That call is wrapped: **a failed index
warns and continues.** A missing index is an annoyance; a crashed pipeline loses a meeting
that cannot be re-recorded.

Everything else is picked up by `mt index`, which skips any source file whose mtime is
unchanged. A no-op run is ~0.04s, so running it often costs nothing.

### Gotchas

- **`snippet` is already HTML-safe** and contains `<mark>`/`</mark>`. Insert it as raw HTML;
  do NOT escape it again or the user sees literal `&lt;mark&gt;`. Every *other* field —
  `title`, `path`, `speaker`, `customer` — is raw text and MUST be escaped.
- **`docs.text` is stored RAW** (so `AT&T` tokenizes as `at`+`t`, not `at`+`amp`+`t`).
  Escaping happens after snippet extraction, using `\x02`/`\x03` sentinels that control-char
  scrubbing at index time makes unforgeable. If you ever read the `text` column directly,
  escape it yourself.
- **`score` is `-bm25`, higher is better, and is NOT normalized across queries.** Don't
  render it as a percentage. Comparable within one query only.
- **`t_start` is the start of the merged block**, not of the matched word, so a deep-link may
  land a few seconds early. Deterministic, intentional.
- **`customer` is `None`** for memory files and `Reports/`, and for `Team/` meetings. The UI
  needs a fallback label, not a blank.
- `search()` never raises — bad FTS5 syntax falls back to a quoted literal, then to `[]`.
- `python3 search.py --selftest` runs the assertion suite (XSS, bad queries, merging,
  timezone, empty DB) with no corpus and no hardware. Safe anywhere.

### In `mt web`

A search box sits under the header on the main page. Debounced 250ms, Enter searches
immediately, out-of-order responses are dropped by a sequence counter. Three pills:
Everything / Summaries / Transcripts. The whole block collapses when the box is empty, so
a normal day looks exactly as before.

`GET /api/search?q=&kind=&customer=&limit=` → `{"q","count","hits","error"}`. `limit` clamps
to 100. `kind` accepts a comma-separated list — the Transcripts pill sends
`transcript_segment,transcript`, and `web.py` merges the two result sets by score. Any
failure returns HTTP 200 with `error` set; the page degrades to one line of text, never a 500.
The `search` module is imported lazily inside the handler, so a broken or missing index
cannot stop `mt web` from serving.

Clicking a hit opens it: summaries open the summary modal, transcript segments go to
`/review`. `read_summary()` falls back to a direct meeting-directory lookup when a folder
has a `summary.md` but no `transcript.json` — which is most of the historical corpus, all
the pre-`mt` meetings. Without that fallback, 93 of the first 100 summary hits dead-ended.
The id is regex-gated to a single path segment and the resolved path is confirmed inside the
workspace before anything is read.

**Known residual:** four recent sessions have a `transcript.json` but no summary yet. They
route to the modal, which says "not summarised yet — the transcript is ready" rather than
linking to `/review`. Telling them apart would need a per-minute `/api/sessions` fetch; an
accurate message was judged the better trade. They are still reachable from their own
transcript hits and from the Recordings list.

### Measured

Full rebuild of the whole corpus **0.478s**; no-op incremental 0.04s; queries 2.6–12.4ms.
4,285 documents from 600 source files, `DB/search.db` = 15.9 MB.

The index is a **finding aid, never an attribution source.** Transcripts are verbatim and
contain ASR- and OCR-garbled names; treat any name it hands back as unverified until you
confirm it.

## 8. Contacts

`/contacts` in `mt web` — the people you deal with, their roles and contact details, backed by
the **`contacts` table in your database** (`DB/mt.db` by default). Your AI assistant can read
the same table, so an edit here is visible to it immediately. No second store, no sync, no drift.

This is the one place `mt web` **writes** contacts to the database. Reads are pure reads;
`ensure_schema()` is only ever called from the three POST handlers, so browsing the page can
never alter the database.

### Columns added Sep 15 2026

`ALTER TABLE ... ADD COLUMN` only — nothing existing was dropped, renamed or rewritten, and
a checksum of all ten original columns was identical before and after.

| column | purpose |
|---|---|
| `phone` | did not exist before |
| `company` | for people with no `customer_id` (your own company, partners) |
| `aliases` | newline-separated alternate spellings. Transcripts garble names constantly; this is what makes lookup and voiceprint matching resolve anyway |
| `updated_at` | what changed recently |
| `merged_into` | tombstone. **Merging never deletes** — the dropped row stays, pointing at its keeper |
| `not_duplicate_of` | "these are genuinely different people", written symmetrically on both rows |
| `reports_to` | **reserved, ships empty.** For the deferred org-chart feature; added now so that feature needs no second migration. Not in `WRITABLE`, not in the UI |

### Three tabs

**Everyone** — the full list fetched once and filtered client-side (274 rows is small), with
search across name/email/role/company/aliases. Click anyone to edit every field, plus
**Where I've seen them**: every meeting they appear in, via the search index.

**Needs confirmation** — the unverified contacts as a worklist with one-click verify.
**It is a flag, never a gate.** Naming a speaker in `mt` stays free-form and is never blocked
by verification state.

**Duplicate names** — exact name matches only (`GROUP BY lower(trim(name))`). **Never
fuzzy-match surnames**: `Alex Sample` and `Robert "Bob" Sample` are different people
at the same customer, and a surname heuristic would wrongly pair them.

### Enrolling a voice ties it to a contact

Enrolling used to tell the contacts database nothing — `mt.py` has zero references to
the database and still does; every contact write goes through `contacts.py` and `web.py`, never
the recorder.

After an enrolment the name is resolved against contacts and you get one of four outcomes:

| Outcome | What you're offered |
|---|---|
| exact match (name **or** an existing alias) | a quiet confirmation, nothing to do |
| close match | *"Did you mean Alex Example?"* — with all three fixes below |
| several close matches | all of them, listed; it never picks |
| no match | create a contact, prefilled |

The three fixes exist because there are three genuinely different situations, and **only you
know which one you are in**:

- **your typo** → *rename the voiceprint*. Never alias your own typo; that entrenches it, and
  `mt rename` keeps the vector so it costs nothing.
- **both names right** (nickname vs legal name: `Steve`/`Stephen`, `Dan`/`Daniel`) → *add an
  alias*. This case is permanent — see §9.
- **different person** → *create a contact*.

**Nothing is ever auto-applied.** A contact created this way is `needs_confirmation`, never
`verified`: a name typed in a hurry mid-call is not confirmation, and the rule is that nobody
becomes verified without you saying so. Auto-creation would
also have been actively harmful — it would have turned every typo into a permanent duplicate
row, putting `Alex Exampel` beside `Alex Example`, which is the mess this exists to prevent.

The contacts page also lists **enrolled voiceprints with no contact row**, with the same three
choices. That count went 6 → 0 on 2026-09-16.

### Same-account vs cross-account — read this before merging

Many duplicate pairs can be **deliberate**. You and colleagues who cover several accounts
may be carried **once per account on purpose** (e.g. one row under `acme`, one under
`globex`). **Merging one of those destroys the per-account association.** The
card labels each pair and says which way to go. Same-account pairs are the real duplicates.

### Undoing an enrollment, and the guard that stops the next one

**A voiceprint used to be permanent.** There was no unenroll, and `enroll` averages into the
existing vector (`merged = (old*n + vec)/(n+1)`) — so a print from a bad recording could be
diluted but never replaced. One bad sample was forever.

- **`mt forget "Name"`** moves the `.npy` to `voiceprints/retired/` and drops the index entry,
  recording the original record plus `retired_at` and `--reason`. **Nothing is deleted** —
  deletions inside a synced workspace propagate to the cloud copy. `mt restore "Name"` is the exact inverse.
- **`mt enroll --replace`** overwrites the vector instead of averaging, resetting `samples` to 1.

**The guard runs inside `save_voiceprint()`** — the single choke point every enrollment passes
through, including `mt web`'s review UI (`web.py` calls `MT.cmd_enroll`). Thresholds come from
`config.json`, never hardcoded. Two checks, both overridable with `--force`:

| Check | Fires when | Means |
|---|---|---|
| **same name, different voice** | new sample scores **below** `auto_assign_score` against the print already stored under that name | two people are being labelled with one name, or one recording is bad |
| **different name, same voice** | new sample scores **at or above** `auto_assign_score` against a *different* enrolled person | one person is being enrolled twice under two names, or this voice is about to be given the wrong person's name |

`--replace` suppresses the first (replacing is the sanctioned fix) but **not** the second —
replacing a print is no reason to give one person's voice another person's name.

**In `mt web`'s review page.** Open a voice and the panel reads in scope order, because the two
actions are constantly confused and the difference matters:

| Control | Scope | Touches the voiceprint? |
|---|---|---|
| Confirm / Don't know / Multiple people | **this meeting only** | no |
| Retire this voiceprint | **every future meeting** | yes — moves it to `retired/` |

Retiring offers to un-name the speaker here too (checked by default, both effects stated before
the click, two-step arming). The asymmetry is deliberate: the local action can never reach the
global store, but the global one may do the local one — because you opted in, visibly. **Restore
never re-names a speaker** — re-asserting a name you retracted would be the page guessing at a
person.

**Cluster quality is shown next to the enroll control.** `self_similarity` predicts whether
enrolling will produce a clean print or a blend. Thresholds come from `config.json`
(`min_self_similarity` 0.6 = impure, `caution_self_similarity` 0.88 = loose); the median across
this workspace is 0.907. **It never blocks anything** — a caution, like verification status.
On one Sep 9 customer session `system-spk0` scored **0.858** and was in fact several people; the
number was on disk and shown nowhere.

**`mt rename "Old" "New"`** fixes a spelling without retiring and re-enrolling. The vector is
never touched — only `name`, `slug`, the `.npy` filename and `updated` move; `samples`, `added`
and `sources` survive, so a rename costs no accuracy.

By default it renames the print, then **reports which past sessions still carry the old name and
stops**, printing the command to fix them. `--sessions` also rewrites those `speakers.json`
entries and re-renders each transcript. Report-first because rewriting transcripts is a bigger
act than fixing a label — see the blast radius before it happens. `--sessions` touches `name`
only; `source` and `confidence` are your judgement about *who* someone is, and a respelling
does not reopen that.

It also reports whether a contact now matches the new name, reading the database
**read-only**. `mt.py` otherwise has zero references to that database and that separation is
deliberate — contact writes belong to the contacts page, never to the recorder.

Three guards, all of which exist because the data demanded them:

- **Renaming onto a live name refuses.** Two people under one name leaves nothing to tell them
  apart — the exact failure this command exists to reduce.
- **An ambiguous source refuses.** The retired store legitimately holds several records sharing
  a spelling (`alex-example` and `alex-example-2` are two different people). Rename
  names them by file stem rather than guessing; "most recent wins" is fine for `restore` and
  fatal for `rename`, which would write a name onto a stranger.
- **Control characters, path separators and empty names refuse.** Precedent: `"Sam Sample\x00"`
  once slugified to `sam-sample` and retired the real Sam Sample.

Renaming a **retired** print (`--retired`) also updates `retired_from`, or `mt restore` would
put the vector back under its old filename while the index claimed the new name.

**Why it exists.** On 2026-09-09, from one customer meeting with bad audio, two prints were
enrolled two minutes apart as `Al Example` and `Alex Exampel`. They are **0.488 apart** —
different-voice territory — i.e. two different people under two spellings of one name, with no
warning. Both were retired 2026-09-16. The second check is what catches the other half of the
same family of bugs: `Sam Sampel` vs Sam Sample.

### The system tap dies silently — detection and recapture

**The failure.** `SCStream` keeps delivering buffers of **pure digital silence** and never
raises `didStopWithError`. There is no failure signal at all. It cost 7:01 of one customer
weekly on 2026-09-16 and 3:05 of another customer call on 09-10.

Three faults compounded it: the old watchdog needed **300 seconds** of silence *and* an active
mic, so it fired 2 minutes before the end of one call and never fired on the other; its warning
went to stderr, which `cmd_live` sent to `DEVNULL`, so nobody saw it; and nothing tried to
recover.

**The probe — how a dead tap is now told apart from a quiet room.** The old comment called this
impossible ("a muted far end also delivers exact zeros"). It isn't:

> Speak a cue from a **separate process** and check whether it lands in the captured track.
> Heard → the tap is alive and nobody is talking. Absent → the tap is dead.

`cfg.excludesCurrentProcessAudio = true` drops anything **mtcapture plays itself**, so the cue
*must* come from `/usr/bin/say` as a separate `Process`. An `AVAudioPlayer` in-process would be
excluded from the capture and read as "dead" 100% of the time. **Measured:** with the cue
playing the captured track reads **−24.7 dBFS**; with nothing playing, **−91 dBFS**. The premise
holds.

Because the probe resolves the ambiguity, the trigger no longer needs a live mic — it fires on
**both sides silent**, which is most of a meeting and which the old rule was blind to. Probe
window 2s, rate-limited between probes, and the 300s legacy warning is kept as a backstop.

**Recapture.** On a confirmed-dead tap the `SCStream` is torn down and rebuilt, **writing to the
same output file** so the track is continuous. Capped at 3 automatic attempts. `SIGUSR1` triggers
it manually at any time — `mt recapture`, or the **Rebuild audio tap** button in `mt web`, which
is deliberately always enabled rather than hidden behind an error state.

**Measured gap on a manual rebuild: 31 ms.** That is far below a syllable, which is what makes
the button safe to press out of curiosity.

**Still unproven:** whether a rebuild recovers a *genuinely* dead tap. A real failure cannot be
manufactured on demand. What is proven is that the rebuild itself is clean and near-free, so the
next time it happens the button is worth pressing — and either way detection is now ~45s instead
of 300s, and visible instead of discarded.

**What you see:** a boxed banner in the live transcript and a pulsing card in `mt web`, with
the tab title alternating `⚠ CHECK AUDIO`. When the evidence is ambiguous it **asks** — *"Is
anyone talking on the call right now?"* — rather than asserting. The ticking indicator is
deliberately **visual, not audible**: a browser tone would be played into the very tap under
investigation, landing in `system.wav` and risking confusion with the probe cue.

## 9. Tuning playbook

Symptom → the one thing to change.

| Symptom | Change |
|---|---|
| Far too many speakers | `--speakers N` for that meeting; or raise `merge_similarity` toward 0.70, or `min_speaker_seconds` above 8. |
| Two people merged into one | Lower `merge_similarity` toward 0.50. |
| Your voice sounds thin or hummy | Check for low-frequency hum: a dock or hub can inject mains noise into USB audio. The 80 Hz high-pass removes most of it, but plugging the headset straight into the Mac fixes it at source. |
| Not sure which mic was used | `mt devices` marks the live default with ▸ and prints what `mt` would pick. After the fact the device name is in the transcript header and in `capture.json` (`mic_name`). |
| Where are the commands again? | `mt web` → **Help**, or `/help`. The command list there is read out of this tool's real argument parser, so it cannot fall behind. |
| It picked the wrong mic or speaker | Pick it from the **Microphone** / **Speaker** dropdown in `mt web` before you hit Start (or `--mic-id` / `--speaker-id` on the CLI). The pick is remembered for this desk, so the next meeting here gets it too. Reorder `mic_priority` in `config.json` to change the fallback everywhere. Not changeable mid-recording — stop and start again. |
| Need a different mic mid-meeting | **Not possible** — macOS will not hand a running capture a different mic (see §10). Ctrl-C and start a second recording; summarise across both. |
| `no display available to attach to` | The display was asleep or locking. ScreenCaptureKit needs a live display, so a recording will not *start* in that state. Wake the screen and retry. |
| Names misspelled | Add them to `prompt` in `config.json`. Highest return of anything here. |
| Whisper echoes a label like "Terms:" | Your prompt contains a `Label:` construct. Rewrite as prose. |
| Your own lines missing | You were too quiet, or the mic was muted. Check `mt doctor`; raise mic gain. Do **not** raise `MAX_GAIN_DB` — that reintroduces hallucination. |
| A garbled copy of the far side under your name | Speaker bleed. Wear headphones. If needed, lower the `drop_bleed` threshold (currently 0.55) — but note text similarity measured 0.24 on real bleed and 1.00 on genuine short words, so the level gate is the real defence. |
| Wrong name auto-applied from a voiceprint | Raise `auto_assign_score`; re-enroll the person to sharpen their print. |
| On-screen names wrong or missing | Inspect `screen.jsonl`, then `screen.py`'s `view_of` and `clean_name`. |
| A lookalike window pollutes the roster | Add its title to `DEAD_TITLES` / `DEAD_PREFIX` in `screen.py`. |
| Live text too slow | It is already `small.en`. Do not drop lower — see the model table in `README.md`. |
| Re-runs too slow | They already reuse the ASR pass. `--force-asr` only when the audio or gain changed. |

---

## 10. Measured vs unvalidated

Anything below marked **open** is what a real meeting would settle.

**Established on real data**

- Model choice: `small.en` live, `large-v3-turbo` final; `medium.en` slower *and* worse; `base.en`/`tiny.en` disqualified on a "1500G"/"1500D" error.
- Audio: 16 kHz mono + FLAC is byte-identical to 48 kHz stereo for transcripts, 13.6× smaller. Lossy Opus costs 4–6% WER and a domain term — rejected.
- Voiceprints: same speaker ≈0.91, stranger ≈0.43, across different meetings.
- Diarization over-splits long calls: 34 raw clusters for 8 real people on a 33-minute call.
- Occluded windows OCR fine (88 lines from a fully covered Chrome window). Minimized or off-Space windows return blank.
- Zoom's pop-out renders `Talking:`; Meet's pop-out and spotlight show the speaker's tile name.

**Established, and load-bearing**

- **The microphone cannot be changed mid-recording.** `SCStream.updateConfiguration` accepts a new `microphoneCaptureDeviceID` and then delivers **digital silence** for the rest of the take (measured: 0.0000 RMS, not a quiet room). Detaching and re-attaching the `.microphone` output is worse — the track stops growing entirely. A live-switch feature was built, measured, and **deliberately removed**: a switch that looks successful and records nothing is worse than no switch. What remains is a watchdog that polls every 3 s and, if the pinned device disappears, logs a loud warning, writes a `disconnected` entry to `mic.jsonl`, and puts a banner in the transcript. To change mics, stop and start a new recording.
- **A recording will not start while the display is asleep or locked** — SCK reports "no display available to attach to". Hit live on Sep 4.

**Open**

- **Multi-participant screen behaviour.** All three platform tests were solo. Gallery-view tile labels, participant-panel scrolling with a real group, and crucially the exact format of Zoom's `Talking: <name>` are unconfirmed.
- **Screen-share layout.** You share tabs; that layout has never been captured.
- **Teams active speaker.** No speaking label found in the capture — roster only so far.
- **Crosstalk attribution.** A whisper segment straddling a speaker change is assigned whole to whoever dominates it, so rapid back-and-forth still collapses. Would need word-level alignment.
- **Capture start race.** The system-audio tap occasionally yields silence for the first seconds. Intermittent, cause not pinned down.

---

## 11. Where the knowledge lives

| Topic | Location |
|---|---|
| Why the design is like this, with evidence | `README.md` |
| Quick card | `cheatsheet.html` |
| Every flag and knob | this file |
| Version history | `CHANGELOG.md` |
| Inline notes on each setting | `config.json` (`_*_note` keys) |

**One rule that outranks the rest:** every machine-derived speaker name is written
`unconfirmed`, and the transcript says so. Voiceprint match, on-screen label, roster
order — all guesses until you confirm. A 0.92 match is strong evidence, not proof.

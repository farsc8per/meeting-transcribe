# mt — meeting transcription with speaker ID

Your meetings recorded, transcribed and searchable, entirely on this Mac. Your voice and the
far side are kept on separate tracks, every line is labelled with who said it, voices you
name once are recognised from then on, and a clean transcript goes to your AI assistant for
the summary and follow-ups. Nothing is uploaded: no audio, no transcript, no voiceprint. No
API key, no subscription, no virtual audio driver.

Works for **any** meeting — Teams, Zoom, Meet, Dialpad, a phone on speaker, or an
in-person conversation — because it records what the Mac plays and what your mic hears
rather than integrating with a specific app.

---

## One-time setup

Two macOS permissions. The first one is the only fiddly part.

### 1. Screen & System Audio Recording  ← required

macOS treats "record the audio my Mac is playing" as a screen-recording capability.

1. **System Settings → Privacy & Security → Screen & System Audio Recording**
2. Turn **ON** the terminal app you run `mt` from (iTerm, Terminal, VS Code…)
3. **Fully quit that app** — Cmd-Q, not just closing the window — and reopen it

> macOS only ever asks once. If the request was declined before, it will never prompt
> again and the toggle above is the only way to grant it. `mt doctor` tells you which
> state you are in.

### 2. Microphone

Prompts normally the first time you record. Allow it.

Then check everything:

```bash
cd ~/Tools/mt
./mt doctor
```

Optional — put `mt` on your PATH so you can run it from anywhere:

```bash
echo 'export PATH="$HOME/Tools/mt:$PATH"' >> ~/.zshrc
```

---

## Recording a meeting

```bash
mt record --customer acme --name acme-weekly
```

Press **Ctrl-C** when the meeting ends. It then transcribes and writes the transcript
next to the audio. With `--customer`, output lands under your workspace (`~/mt-data`):

```
Customers/acme/meetings/2026-09-03-acme-weekly/
    system.wav        everyone else (what the Mac played)
    mic.wav           you (what your mic heard)
    transcript.md     speaker-labelled, timestamped   ← the one you read
    transcript.txt    flat text, good for pasting to your AI assistant
    transcript.json   structured, with per-segment confidence
    speakers.json     the name mapping — edit this to correct names
    work/             intermediates; safe to delete
```

Without `--customer` it goes to `Recordings/` and you can file it later.

Useful flags:

| Flag | Why |
|---|---|
| `--roster "Ann Lee,Bob Ray"` | pins the expected number of far-end voices and pre-names them in speaking order |
| `--speakers 3` | you know how many people are on the call but not their names |
| `--in-person` | several people sharing your mic in a room — splits the mic track too |
| `--no-mic` | transcribe a webinar or playback where you never speak |
| `--title "Q3 SBR dry run"` | nicer heading in the transcript |
| `--mic-id …` | pick a specific mic (see `mt devices`) |

## Transcribing something you already have

Any audio or video file — a Zoom cloud recording, a Gong download, a voice memo:

```bash
mt process ~/Downloads/zoom-recording.mp4 --customer globex --name pricing-call
```

---

## How speaker identification works

Three layers, weakest to strongest.

**1. Two tracks, not one.** Your mic and the system output are recorded separately, so
your voice is *never* confused with theirs. That split is structural, not a guess.

**2. Voice clustering.** The far-end track is split by voice, so you get `Speaker 1`,
`Speaker 2`, `Speaker 3` reliably separated — including when someone speaks again
twenty minutes later.

**3. Voiceprints — the part that gets better over time.** Name a speaker once and they
are recognised automatically in every future meeting:

```bash
mt enroll Customers/acme/meetings/2026-09-03-acme-weekly --speaker 2 --name "Alex Example"
```

From then on Alex is named on sight. Each further enrolment sharpens their print.

```bash
mt speakers          # who is enrolled
```

Measured on this machine: the same person scores **~0.91** against their stored print, a
different person **~0.43**. Auto-naming needs 0.62, so a stranger is left as
`Speaker N` rather than being given someone else's name. Between 0.42 and 0.62 you get a
suggestion you have to accept.

### Names are provisional until you say otherwise

Every machine-derived name is written as `unconfirmed`, and `transcript.md` carries a
banner saying so. This is deliberate: a name is not trusted until a person confirms it. A
voiceprint match at 0.92 is strong evidence, not proof, and roster order is an outright
guess.

To confirm or correct:

```bash
# edit speakers.json — fix "name", set "confidence" to "verified"
mt relabel Customers/acme/meetings/2026-09-03-acme-weekly
```

`mt enroll` does that for you for the speaker you name.

---

## Getting a summary

`mt` produces the transcript, not the summary — your AI assistant writes the summary, with
the DB, Jira and your recap conventions in context:

> Summarise `Customers/acme/meetings/2026-09-03-acme-weekly/transcript.md`
> into a meeting summary and pull out action items.

---

## Accuracy notes

- Model: `whisper-large-v3-turbo-q5_0` running on the M4 GPU — about **10× realtime**,
  so a 60-minute meeting transcribes in roughly 6 minutes.
- `config.json` holds a glossary prompt seeded with your people, accounts and product
  terms. It measurably improves proper nouns *and* restores punctuation — without it
  whisper drops capitalisation entirely, and "Jo" comes out as "Joe".
  **Add new names and jargon as they come up** — it is the single highest-leverage
  thing you can tune. **But:** whisper only reads about 224 tokens (~150 words) of
  prompt and ignores the rest, so swap terms in and out rather than appending forever.
- Silence is not transcribed. Whisper hallucinates filler like "Thank you." over quiet
  audio, so any segment with no detected speech under it is dropped, and a track where
  you never spoke is dropped whole. A meeting where you only listened will correctly
  show no lines attributed to you.
- Per-meeting override: `--prompt "Example, Sample, Acme, Globex"`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `system.wav is silent` | Screen & System Audio Recording not granted, or the terminal was not fully quit after granting |
| `mic.wav is silent` | Microphone permission, or the HyperX mute switch. A *quiet* mic (peak under 0.02) is normal when you are not speaking and is reported as a note, not an error |
| Everyone is one speaker | far end is genuinely one mic — a conference-room speakerphone cannot be split by voice reliably; use `--speakers N` to force a split |
| Far too many speakers | raise `merge_similarity` toward 0.7, or `min_speaker_seconds` above 8 |
| Two people merged into one | lower `merge_similarity` toward 0.5 |
| Rapid back-and-forth attributed to one person | a real limit: whisper segments can straddle a speaker change and are assigned whole to whoever dominates them. Short interjections during crosstalk are the weak spot |
| Your own lines missing, or "Thank you." repeated | you were too quiet. Gain is capped at +18 dB deliberately; below roughly -60 dBFS a track is treated as noise and dropped rather than hallucinated over. Raise the mic gain or move closer |
| A garbled copy of the far side under your name | speaker bleed — your mic hearing the call. **Wear headphones.** It is filtered, but headphones remove it at the source |
| First few seconds missing | intermittent: the system-audio tap occasionally takes a moment to attach. Start recording a few seconds before the meeting |
| Wrong name auto-applied | raise `auto_assign_score` in `config.json`, and re-enrol the person to sharpen their print |
| Names misspelled | add them to `prompt` in `config.json` |

## Reading names off the screen

While recording, `mtwatch` captures the meeting window every 2 seconds and OCRs it with
macOS Vision, then matches what it reads against your `contacts` table. The result is a
**Seen on screen** table in the transcript: who was actually present, with their role.

This is built for the way you actually work rather than for an ideal case:

- **The meeting need not be the front app.** Each candidate window is captured
  individually, so a meeting window sitting behind your editor still reads fine
  (verified: a Chrome window fully occluded by a terminal returned 88 text lines).
- **Windows get re-enumerated every tick**, so moving them, switching browser tabs, or
  Meet dropping into its pop-out are all picked up as they happen.
- **Zoom, Teams, Google Meet, Webex and Dialpad** are all recognised — meeting apps by
  bundle id, Meet by tab title, and pop-outs by shape.
- **Only contact matches are used.** Random name-shaped UI text ("Virtualization
  Project") is logged but never attributed to anyone.

**Who is speaking, not just who is present.** The pop-outs you keep visible carry the
active-speaker label: Zoom's floating window renders `Talking: <name>`, and Meet's
pop-out shows the speaker's tile name. `mt` votes those observations against each voice
cluster's turns, so a speaker can be named without any enrollment. When a voiceprint and
the screen agree, the transcript says "voiceprint + on-screen speaker". Teams exposes no
such label yet — roster only.

The UI headcount (`Participants (N)`, `In this meeting (N)`) is also read and used to pin
the expected number of far-end voices, instead of letting the clusterer guess.

Exact titles, panel coordinates, and the list of lookalike views that are *not* calls
(the Meet home page lists your whole calendar; Zoom's "You have left this meeting" toast
lingers for minutes) are listed in `REFERENCE.md`.

Known limits: a **minimized** window, or one on **another macOS Space**, renders blank
and is skipped — the pop-out is the fallback in that case. And presence is not
attribution: knowing Sam was in the room does not say which voice is theirs. What it does
do is turn "who is Speaker 3?" into a short multiple-choice question. When a voiceprint
match *and* the screen agree, the transcript says "voiceprint + seen on screen".

`--no-screen` turns it off. `mtwatch --list` shows what it can see right now.

Dialpad is a special case worth knowing: it renders its own live transcript with speaker
names, which OCRs cleanly.

## Which microphone

You move between home and the office, so the macOS default input changes under you without
saying anything. Three things make that visible:

- **Before recording** — `mt` prints the mic it is about to use, and why.
- **`mt devices`** — lists every input, marks the current system default with ▸, and says
  which one `mt` would pick.
- **After the fact** — the device name is in the transcript header and in `capture.json`.

The mic is resolved *explicitly* rather than left to the OS, so what gets logged is
definitively what was recorded.

To stop choosing by hand, set `mic_priority` in `config.json` to an ordered list of name
fragments:

```json
"mic_priority": ["HyperX Quadcast", "Logitech USB Headset", "CORSAIR HS70", "MacBook Air Microphone"]
```

The first one actually plugged in wins. Webcam and dock mics are deliberately left out —
they usually sound worst and often become the system default the moment you connect them.
Override for a single meeting with `--mic-id HyperX`; a fragment of the name is enough, you
never need the CoreAudio UID.

### Mic quality

Audio is high-passed at 80 Hz before anything else touches it. That is not cosmetic: a
USB headset running through a DisplayLink dock was measured carrying **88% of its noise
energy below 120 Hz**, with a 60 Hz mains peak **2253x** the median — a classic USB
ground loop. Filtering it gained **+6.7 dB SNR**, and even a clean HyperX Quadcast gained
**+3.7 dB**. Roughly 2% of speech energy lives below 80 Hz, and transcripts agreed 97.2%
with every domain term intact, so nothing is lost.

If your own audio sounds thin or hummy, the filter is already doing what it can in
software — plugging the headset directly into the Mac rather than through a dock removes
the cause.

Worth knowing: a headset mic is often *brighter and more intelligible* in the speech band
than a big desk condenser, which sounds "better" largely because proximity effect gives
it far more bass. Measured on the same voice, the HyperX put 48% of its energy below
500 Hz versus the headset's 19%.

### Changing mics mid-meeting: you can't

macOS will not hand a running capture a different microphone. `updateConfiguration`
accepts the new device and then delivers silence for the rest of the take — this was
built, measured, and removed rather than shipped, because a switch that looks like it
worked and records nothing is worse than no switch at all.

So the mic is fixed once recording starts. What you get instead is a **watchdog**: if the
device is unplugged, it warns immediately in the terminal, records it in `mic.jsonl`, and
puts a banner at the top of the transcript saying your own side is probably missing from
that point. The far side is never affected — that track comes from system audio.

If you need to change mics, Ctrl-C and start a second recording. Two transcripts are easy
to summarise together.

## Live transcription

```bash
mt live --customer acme --name acme-weekly --title "Acme // Weekly"
```

A rolling transcript prints while the meeting runs, then Ctrl-C triggers the accurate
full pass automatically. You get both: something to read during the call, and the good
transcript afterwards.

Live uses `small.en` (~27x realtime, so a few seconds behind the conversation) and cuts
chunks at pauses, at most 20 s, so it never slices a word in half. **Live keeps up or
skips:** if it gets more than 30 s behind (a busy machine — e.g. a shared screen with
annotation competing for the GPU), it jumps to a pause near "now" and prints `⏩ live
skipped 0:25 of the far end`. Live is disposable; the full pass still reads every word. Live speaker labels are just
you-vs-them plus any voiceprint match — real diarization needs the whole recording, so
the final pass relabels everything. Rough live notes land in `live.md`; treat
`transcript.md` as the record.

## Why these models

Measured on real meeting audio on an M4 Mac, scored against large-v3-turbo:

| model | realtime | 60-min meeting | disagreement | verdict |
|---|---|---|---|---|
| large-v3-turbo-q5_0 | 12x | 4.9 min | — | **final pass** |
| small.en-q5_1 | 27x | 2.3 min | 3.8% | **live pass** |
| medium.en-q5_0 | 9x | 6.5 min | 4.4% | pointless — slower *and* worse |
| base.en-q5_1 | 65x | 0.9 min | 7.7% | **do not use** |
| tiny.en-q5_1 | 78x | 0.8 min | 7.7% | **do not use** |

3.8% is the noise floor: it is also what large-v3-turbo f16 scores against its own
quantized build. small.en matched turbo on every domain term and beat it once
("app profiles" where turbo wrote "APRO files").

base.en is disqualified on evidence, not taste — it wrote **"1500G" for "1500D"** and
dropped "NOC". Wrong model numbers in a change-control transcript are worse than no
transcript.

## Disk

Recording is 16 kHz mono — exactly what the recogniser and the diarizer consume, and
measured as producing byte-identical transcripts to 48 kHz stereo. After processing, the
audio is archived to FLAC (lossless) and the wav deleted.

| | 33-min call, both tracks |
|---|---|
| 48 kHz stereo wav (the old way) | 764 MB |
| 16 kHz mono wav | 120 MB |
| **FLAC archive (what you keep)** | **56 MB** |

Lossy formats were tested and rejected: Opus 24k reaches 10 MB but costs ~4-6% word
error and loses a domain term. FLAC costs nothing. `--keep-wav` skips archiving.

## Re-running cheaply

`mt process` reuses an existing transcription if the 16 kHz copy has not changed, so
re-running after a threshold tweak takes ~2 minutes on a 33-minute call instead of ~8.
Add `--force-asr` to transcribe again from scratch.

## How the speaker count is arrived at

Diarization is near-perfect on short clips and over-splits badly on long ones — a real
33-minute call produced **34 raw clusters for 8 people**, with one speaker split three
ways at mutual voice similarity 0.89-0.91. So there is a second pass: clusters whose
voice embeddings agree above `merge_similarity` are merged (average linkage — single
linkage chains unrelated people together), then anything under `min_speaker_seconds`
is folded into the speaker it matches, or into one shared "Unidentified voice" if it
matches nobody. It never invents a name to explain a one-second fragment.

## Rebuilding

```bash
./make.sh      # re-installs deps, recompiles, re-downloads models. Safe to re-run.
```

## Full reference

`REFERENCE.md` is the exhaustive one: every command and flag (generated from the argument
parser, so it cannot drift), every `config.json` key with the measurement behind its
default, the pipeline stage by stage with the function that does each job, a
symptom-to-knob tuning playbook, and an explicit list of what is measured versus still
unvalidated. Start there when changing behaviour rather than reading the source cold.

`cheatsheet.html` is the one-page card for actually running it.

## Hygiene and memory proposals

`mt web` → **Hygiene** is where cleanup waits for a decision: **Data** (DB values, approved a
group at a time) and **Memory** (your AI assistant proposes one change at a time; you review the
exact result and approve, edit, send back or reject). Memory is off until you set `memory_dir`
in `config.json` (`"claude"` = Claude Code's memory folder for your workspace). How to use it,
what each warning means, and the lines to paste to your assistant: **Help → Hygiene & proposals**
(`/help#hygiene`). The rules an assistant session follows when writing a proposal:
`MEMORY-PROPOSALS.md`.

## Vocal cues and meeting awards (experimental)

Two layers, both in `mt web` only — never in summaries, weeklies, Jira or Slack.

**Conversation & awards, every meeting.** The review page's *Conversation* section shows
facts from the transcript's own timing: talk share, longest run without yielding, who
started over whom, slowest answer to a question. Talk-overs are **checked against the
audio** — every one is mic-vs-far-end, and whisper's far-end line ends run long: on one
change-control call only 29% of timestamp overlaps were real. Then the fun part, per
meeting and always with the number behind it: 🎤 The Monologuer, ⚡ The Interrupter,
🛡️ Iron Floor, 🤐 The Strategic Silence, 📢 Main Character, 🔥 Spice Level, 😂 Best Saying.
Unconfirmed voices compete as "Speaker N". **Hide awards** before sharing your screen
(`"fun_labels": false` in `config.json` starts them hidden).

**😂 Best Saying** is the line the room laughed at. There's no language model, so it can't
judge funny; it *hears laughter* with sherpa-onnx's AudioSet tagger (Apache-2.0, 26 MB,
`make.sh` fetches it). On one customer standup it heard the mic laugh at 0.73–0.85 right
after the "hit by a bus … best-case scenario" line, and missed the far-end office laughing
at the same moment — Zoom's noise suppression. So candidates come with their lead-in, and
**👑 Crown** (on a candidate or any transcript line) always wins.

**Naming people live.** In the Room panel the far end splits live into **Speaker 1, 2, …**
by voice (stable numbers; quiet voices fold under "Earlier voices"), each with the closest
voiceprint as a "maybe X?" hint. ✓ It's X, ✎ Name anyone, or ✎ correct a wrong match. The voice is learned for the rest of the call (later
chunks that sound like it get the name, in the Room and the live transcript), and after
the full pass the review page opens with those people filled in — marked confirmed only
when the cluster is clearly them and the name is a verified contact. Built after a call
where one speaker with no voiceprint kept matching a colleague's print at 0.62.
Nothing is enrolled; enrol from Review as usual. Stored in `live_names.json`.

**Vocal cues, opt-in per meeting.** How each *confirmed* speaker's delivery moves against
*their own* normal: **Energy** (pitch, range, pace, loudness) and **Friction** (gaps,
pauses, talking over). Tick **Vocal cues** before recording for the live **Room** panel
(`mt live --cues`; the meeting is measured again after the full pass), or **Analyze vocal
cues** on any recording (`mt cues <meeting>`; `--all` opts in everything). ~2 s a meeting
for the cues, ~40 s for the laughter scan; refused while recording. Only analyzed meetings
feed anyone's baseline; **Clear** / `mt cues --clear` opts one back out.

What it can say: *"faster and higher than her normal."* What it can't: why — voice shows
activation, not whether it's good or bad. The words describe delivery, never feelings or
honesty. Measured verdict after real calls (Oct 5): it reliably shows excitement and
talk-over, and little else — which is why the conversation layer exists. Every panel has a
**Field guide** with the measurements and limits behind it.

## What is where

| Path | What |
|---|---|
| `mt` | the command |
| `mt.py` | pipeline: convert → transcribe → diarize → match → render |
| `src/mtcapture.swift` | dual-track recorder (ScreenCaptureKit) |
| `build/mtcapture` | compiled recorder |
| `src/mtaudio.swift` → `build/mtaudio` | lists speakers, sets the macOS output before a recording (CoreAudio). Built by `make.sh`; without it the Speaker picker is disabled |
| `audioprefs.py` | mic/speaker picks remembered per desk |
| `audio_prefs.json` | those remembered picks — per install, never committed; `mt devices --forget` clears |
| `models/` | whisper + segmentation + speaker-embedding + laughter-tagging models (~800 MB) |
| `voiceprints/` | your enrolled speakers. **Private. Back this up.** |
| `config.json` | owner name, glossary prompt, match thresholds, `fun_labels` |
| `cues.py` | vocal cues: measurement, per-person baselines, Energy/Friction, live scoring |
| `awards.py` | conversation dynamics, meeting awards, laughter → Best Saying |
| `livenames.py` | naming voices live in the Room panel; pre-fills the review after the pass |
| `webapp/room.html` | the live Room pop-out |

## License

MIT — see [LICENSE](LICENSE). That covers mt's own code. The models `make.sh` downloads (speech recognition, speaker segmentation and embedding, laughter tagging) are not part of this repo and keep their own licenses; `models.sha256` lists exactly which files they are.

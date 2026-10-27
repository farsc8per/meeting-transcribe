# Installing mt

`mt` records and transcribes meetings **entirely on your Mac** — Teams, Zoom, Meet, Dialpad,
a phone on speaker, or a room. Nothing is uploaded: no audio, transcript or voiceprint leaves
the machine. It labels who said what, and learns voices you name so it recognises them next
time.

**Mac only** (Apple Silicon, **macOS 15 Sequoia or newer** — recording your own mic through
macOS's capture needs 15; check with `sw_vers -productVersion`). Windows is not supported: recording is built on
macOS's own screen-and-audio capture.

The easiest way to install is to let your AI assistant (Claude Code, Codex, Cursor…) do it —
paste the prompt in the next section. The manual steps follow it.

**Getting the code.** The repo is private for now:
https://github.com/farsc8per/meeting-transcribe. Ask Jason Panke for access, or for the
release zip (`mt-X.Y.Z.zip`). The zip is a complete git checkout: unzip it, then install the
same way. The prompts below cover both.

---

## Before you start: raise an IT request

On a company-managed Mac, several steps need admin rights or IT approval. Raise the request
**before** you install, and ask IT for a working window long enough to bring **all** of these up
to date in one go — not just to approve one prompt. Plan on 30–60 minutes, more if macOS
itself needs upgrading.

| Component | What IT may need to do |
|---|---|
| **macOS 15 or newer** | Approve/schedule the OS upgrade if you're on 14 or older |
| **Xcode command-line tools** | `xcode-select --install`, or a software-update push |
| **Homebrew** and its packages (`ffmpeg`, `whisper-cpp`, `python`) | Allow the install and `brew update && brew upgrade` |
| **Permissions for your terminal app** | Screen & System Audio Recording, Microphone, Calendars (see "Permissions") — a managed Mac can lock these |
| **Network** | Allow the one-time ~900 MB model download from `huggingface.co` and `github.com` (includes the laughter model for meeting awards: a 111 MB archive, 26 MB kept) |

Suggested wording: *"I'm installing mt, an internal meeting-transcription tool that runs
locally. Please give me a window with admin rights to update macOS (15+), Xcode command-line
tools and Homebrew packages, and allow Screen & System Audio Recording, Microphone and
Calendar access for my terminal app."*

The same applies later: when an mt update says it needs a newer component (the CHANGELOG
says so), raise the IT request first, then update.

---

## Install with your AI assistant

Open your assistant **in a terminal app** (Terminal, iTerm, VS Code's terminal) — that app is
what macOS will grant recording permission to. Paste:

> Install the `mt` meeting-transcription tool for me. Follow its INSTALL.md exactly; don't
> improvise around a failing step — stop and tell me.
>
> 1. Check prerequisites and tell me what's missing before installing anything:
>    macOS 15+ on Apple Silicon (`sw_vers -productVersion`), Homebrew (`brew --version`), Xcode command-line tools
>    (`xcode-select -p`), python3. If Homebrew or the command-line tools are missing, give me
>    the command to run myself — they need my password.
> 2. Get the code into `~/Tools/mt`. If I gave you a zip, unzip it into `~/Tools` (it
>    contains an `mt` folder). Otherwise clone it:
>    `git clone https://github.com/farsc8per/meeting-transcribe.git ~/Tools/mt`. Either way,
>    then run `git -C ~/Tools/mt checkout $(git -C ~/Tools/mt tag -l 'v*' --sort=-v:refname | head -1)`
>    so I'm on the newest release, not the development branch.
> 3. Run `~/Tools/mt/make.sh`. It downloads about 900 MB of models the first time; that's
>    expected. It must end with "==> done".
> 4. Create `~/Tools/mt/config.json` from `config.example.json`. Ask me for: my name as it
>    should appear in transcripts, my work Google account, and the customers I meet with
>    (folder name + words that appear in their meeting titles). Leave out keys I don't answer.
> 5. Add mt to my PATH in `~/.zshrc`: `export PATH="$HOME/Tools/mt:$PATH"`.
> 6. Run `mt doctor` and show me the output. Walk me through any ✗, especially the
>    **Screen & System Audio Recording** permission (INSTALL.md, "Permissions").
> 7. Have me do a 1-minute test recording (INSTALL.md, "Test recording") and check the result
>    with me: both tracks present, my lines under my name, live transcript written. Then
>    delete the test folder and run `mt index` so it leaves search too.
> 8. Tell me how to start: `mt web` opens the app in my browser.
>
> Never commit or upload anything from `voiceprints/`, `run/`, `config.json`, or my
> recordings. Voiceprints are biometric data and stay on this Mac.

---

## Manual install

```bash
# prerequisites (once per Mac)
xcode-select --install                    # Swift compiler; skip if already installed
# Homebrew: https://brew.sh

git clone https://github.com/farsc8per/meeting-transcribe.git ~/Tools/mt
#   or, from the zip:  unzip mt-X.Y.Z.zip -d ~/Tools
cd ~/Tools/mt
git checkout "$(git tag -l 'v*' --sort=-v:refname | head -1)"   # newest release
./make.sh                                 # ~10 min first time; safe to re-run
cp config.example.json config.json        # then edit: your name, Google account, customers
echo 'export PATH="$HOME/Tools/mt:$PATH"' >> ~/.zshrc && source ~/.zshrc
mt doctor
# then do the 1-minute "Test recording" below
```

Your data goes to `~/mt-data` (recordings, `Customers/<name>/meetings`, and the database
`DB/mt.db`). Change it with `"workspace"` in `config.json`.

---

## Permissions

macOS asks for these the first time each is used. They are granted to **the terminal app you
run mt from** — not to mt itself — so always start mt from the same app.

| Permission | Why | Where |
|---|---|---|
| **Screen & System Audio Recording** — required | Hearing the other side of the call means recording what the Mac plays; macOS files that under screen recording. mt reads window titles to name speakers, never saves images. | System Settings → Privacy & Security → Screen & System Audio Recording |
| **Microphone** — required | Your side of the call | Prompts on first record |
| **Calendars** — optional | `mt web` lists today's meetings so you can press Record | Prompts on first use. Add your work Google account under System Settings → Internet Accounts first |

**After turning on Screen & System Audio Recording you must fully quit the terminal app
(⌘Q) and reopen it.** If the prompt was ever declined, macOS never asks again — the toggle
is the only way. `mt doctor` tells you which state you're in.

On a company-managed Mac, IT policy may block one of these; if a toggle is greyed out, that's
the reason — ask IT.

---

## Test recording

Do this after installing **and after every update**, before you rely on mt for a real meeting.
`mt doctor` proves the parts are there; only a recording proves they work together.

1. Play something with a voice on the Mac (a YouTube clip, a Teams test call) and talk over it.
2. `mt record --name mt-smoke-test --duration 60` (or press Record in `mt web`).
3. Open the transcript it prints and check:
   - **your lines** are under your name (the mic track)
   - **the other voice** is there as "Speaker 1" (the system track). If you played nothing,
     a "no system audio" warning is expected
   - `live.md` exists in the folder (live transcription works)
4. Delete the test folder, then run `mt index` so it drops out of search as well.

Never use a customer call as the test — some customers don't allow recording. Recording?
Understand your company's recording policy.

---

## Using it

```bash
mt web                          # the app: today's meetings, Record, review, search
mt record --name team-sync      # record from the terminal; Ctrl-C to stop and transcribe
mt record --customer acme --name qbr   # files it under Customers/acme/meetings/
mt process ~/Downloads/call.m4a # transcribe a file you already have
```

The first few meetings name speakers "Speaker 1, 2…". Name them once in the review page and
mt recognises those voices from then on. See `README.md` for everything else.

---

## Updating

Releases are git tags (`v0.6.0`…). To update, tell your assistant **"update mt"** with:

> Update `mt` at ~/Tools/mt to the newest release tag. First run `mt version` and
> `git fetch --tags`, then show me the CHANGELOG.md entries between my version and the newest
> `v*` tag — **stop there if any entry needs a newer macOS, Xcode tools or Homebrew package**,
> so I can raise an IT request first (INSTALL.md, "Before you start"). Otherwise check out the
> newest tag, run `./make.sh`, then `mt doctor`, then walk me through the 1-minute test
> recording (INSTALL.md, "Test recording") and delete it afterwards. Don't touch config.json,
> voiceprints/ or my data.

**No GitHub access? Update from a newer zip.** Add this to the prompt: *"I have the newer
zip at <path>. Unzip it into a temporary folder and use `git -C ~/Tools/mt fetch
<temp>/mt --tags` in place of `git fetch --tags`, then delete the temporary folder."* The
zip is a full git checkout, so this is the same update, just fetched from the zip rather
than from GitHub.

Your recordings, voiceprints, config and database are never touched by an update. If a release
changes the database, its CHANGELOG entry says so and `mt doctor` tells you what to do.

---

## Bugs and ideas

In `mt web`, **Send feedback** (top right, every page) does this for you: pick Problem or
Idea, write it up, and it opens a filled-in GitHub issue or copies the report to paste.
Otherwise run `mt doctor` and `mt version`, then report it with what you did, what you
expected and what happened. Paste error text, **never transcripts or audio** — they contain other
people's conversations.

Where to report: https://github.com/farsc8per/meeting-transcribe/issues if you have repo
access, otherwise message Jason Panke directly.

---

## Uninstalling

```bash
rm -rf ~/Tools/mt          # the tool
rm -rf ~/mt-data           # your recordings, database — only if you're sure
```
Then remove the PATH line from `~/.zshrc`, and turn off the permissions in System Settings.

// The help page.
//
// Two kinds of content, deliberately kept apart:
//   - recipes, shortcuts and the voice-fixing guide are WRITTEN here, because they are
//     judgement about when to reach for a thing, which no parser can tell you;
//   - the command reference, thresholds and paths are FETCHED from /api/help, which
//     reads mt's real argparse parser and the live config.json.
// Never hand-copy a flag or a threshold into this file. A help page that is subtly
// wrong is worse than no help page, because this is the one people believe.
(function () {
  const $ = s => document.querySelector(s);
  const pane = $("#pane");
  let DATA = null;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  // Everything below is built with textContent, never innerHTML: some of it is command
  // text and some comes off disk, and neither is worth an escaping bug.
  function card(...kids) { const c = el("div", "card"); kids.forEach(k => k && c.appendChild(k)); return c; }
  function h3(t) { return el("h3", null, t); }
  function p(t) { return el("p", null, t); }
  function cmd(t) { return el("code", "cmd", t); }
  // A line you paste to your AI assistant: wraps instead of scrolling, and has its own Copy.
  function paste(t) {
    const w = el("div", "pastel");
    w.appendChild(el("code", "cmd wrap", t));
    const b = el("button", null, "Copy");
    b.addEventListener("click", () => {
      const done = () => { b.textContent = "Copied"; setTimeout(() => { b.textContent = "Copy"; }, 1500); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(t).then(done, done);
      } else { done(); }
    });
    w.appendChild(b);
    return w;
  }
  function warn(t) { return el("div", "warnbox", t); }
  function good(t) { return el("div", "okbox", t); }
  function list(items, ordered) {
    const l = el(ordered ? "ol" : "ul");
    items.forEach(i => l.appendChild(el("li", null, i)));
    return l;
  }
  // A paragraph with <strong> runs: pass ["plain ", ["bold"], " plain"].
  function rich(parts) {
    const e = el("p");
    parts.forEach(x => e.appendChild(Array.isArray(x) ? el("strong", null, x[0])
                                                      : document.createTextNode(x)));
    return e;
  }

  // ── how do I… ──────────────────────────────────────────────────────────────
  const RECIPES = [
    ["Record the meeting I am in now",
     "Open the Today list and hit Record on the event — the name, title, customer and " +
     "the invitee list all come from the calendar, which is what arms voice review.",
     "mt live --customer acme --name acme-weekly --title \"Acme // Weekly\"",
     "The web button is the better path: typing it by hand skips the invitees."],

    ["Record with a different microphone",
     "Pick it from the Microphone dropdown on the Record-now card before you hit Start.",
     "mt live --name some-call --mic-id \"HyperX\"",
     "--mic-id takes a name fragment, not just the full UID. Leave the dropdown on " +
     "Automatic to let mic_priority decide. This cannot be changed once recording has " +
     "started — macOS hands a running capture silence if you try, so stop and start again."],

    ["Transcribe a recording I already have (Zoom, Gong, Teams, a voice memo)",
     "Point process at the file. It lands in the normal folder layout and is searchable " +
     "like anything else.",
     "mt process ~/Downloads/gong-call.mp4 --customer globex --name pricing-sync \\\n" +
     "           --title \"Globex // Pricing sync\"",
     "Without --customer it files under Recordings/ as an internal meeting."],

    ["Re-run a transcript with the speech re-recognised",
     "process reuses the previous ASR pass when the audio has not changed, which is why " +
     "a second run is minutes rather than the full pass. --force-asr overrides that.",
     "mt process Recordings/2026-09-21-1300-meeting --force-asr",
     "Reach for it after changing the glossary prompt in config.json, not otherwise."],

    ["Find what someone said, across every meeting",
     "The search box at the top of the meetings page covers transcripts, summaries, " +
     "recaps and notes. Transcript hits carry the speaker and a timestamp that deep-links " +
     "into the audio.",
     "mt search \"thin clients\" --customer acme --since 2026-08-01",
     "It is a finding aid, not an attribution source — transcripts are verbatim and carry " +
     "garbled names."],

    ["Check the rig before an important call",
     "doctor is the all-green check; devices tells you which microphone will actually be " +
     "used and why.",
     "mt doctor\nmt devices",
     "If capture has broken, the Screen & System Audio Recording permission is the first " +
     "suspect — macOS never re-prompts after a decline."],

    ["Watch vocal cues live during a call",
     "Tick “Vocal cues” on the Record-now card before you start (it remembers the " +
     "choice), then hit “Open Room ↗” on the recording card and park the window beside " +
     "the call. The meeting is measured properly again after the full pass.",
     "mt live --name some-call --cues",
     "Experimental and per meeting. Readings trail the talk by 5–30 s."],

    ["Name who is talking, live, and have the review page come out right",
     "In the Room panel the far end splits into Speaker 1, 2, … by voice. ✓ It's X when " +
     "the “maybe X?” guess is right, ✎ Name to pick anyone, ✎ Not them on a wrong voice " +
     "match — each card shows what that voice just said. The voice is learned for the " +
     "rest of the call; ✎ Rename / ✕ Unname if you got it wrong. After the full pass the " +
     "review page opens with those people filled in, waiting for your Confirm.",
     null,
     "Needs Vocal cues ticked (that's what feeds the Room). Nothing is enrolled — enrol " +
     "from Review as usual, which is what fixes the next call too."],

    ["Get vocal cues, awards and Best Saying for a meeting I already recorded",
     "Open it in Review and click “Analyze vocal cues”. The Conversation section " +
     "(talk share, talk-overs, most awards) shows for every meeting already; analyzing " +
     "adds Best Saying and Spice Level.",
     "mt cues Recordings/2026-09-17-1432-team-1-1-sync",
     "Refused while a recording is running. Clear opts a meeting back out."],

    ["Hide the awards before sharing my screen",
     "“Hide awards” on the Conversation section, or “hide” on the Room panel — it's one " +
     "setting, remembered in this browser, so either button covers both.",
     null,
     "To start hidden everywhere, set \"fun_labels\": false in config.json."],

    ["Rescue a recording whose far side went silent",
     "If the system track dies mid-call, the tap can now be rebuilt in place without " +
     "losing the take. mt web offers the same thing as a button on the live panel.",
     "mt recapture",
     "A silent system track usually just means nobody else was talking. Check whether it " +
     "recovers before treating it as a fault."],
  ];

  function paneRecipes() {
    const d = el("div", "hp");
    d.appendChild(rich([["The short version. "], "Everything here is also a button somewhere, " +
      "except where it says otherwise."]));
    RECIPES.forEach(([t, why, c, note]) => {
      const kids = [h3(t), el("div", "why", why), c ? cmd(c) : null];
      if (note) kids.push(p(note));
      d.appendChild(card(...kids));
    });
    return d;
  }

  // ── vocal cues & awards ────────────────────────────────────────────────────
  // Judgement about what these things are FOR and where they mislead. The exact cut
  // points and award thresholds live in each panel's Field guide, which the server
  // builds from the code — they are deliberately not repeated here.
  function paneCues() {
    const d = el("div", "hp");
    d.appendChild(rich([["Experimental, and yours. "], "Two layers, both in mt web only — " +
      "never in summaries, weeklies, Jira or Slack."]));
    d.appendChild(card(
      h3("Conversation & awards — every meeting"),
      p("Facts from the transcript's own timing: who talked how much, the longest run " +
        "without yielding, who started over whom, how long an answer to a question took. " +
        "Talk-overs are checked against the audio, because the far end's timestamps run " +
        "long and overlap when nobody actually interrupted."),
      p("The awards are those facts with a sense of humour: 🎤 The Monologuer, ⚡ The " +
        "Interrupter, 🛡️ Iron Floor, 🤐 The Strategic Silence, 📢 Main Character, 🔥 Spice " +
        "Level, 😂 Best Saying."),
      list([
        "Per meeting, never a standing title — and every card shows the number that earned it.",
        "Anyone who spoke can win, you included. Unconfirmed voices appear as “Speaker N”, never a guessed name.",
        "“Hide awards” before you share your screen. One setting covers Review and the Room panel.",
      ])
    ));
    d.appendChild(card(
      h3("😂 Best Saying"),
      p("mt can't judge funny — it has no language model — but it can hear laughter. The " +
        "line just before a laugh becomes a candidate, scored higher when the other side " +
        "laughed. Candidates show with the two lines that led into them, because a laugh " +
        "often lands a beat late."),
      warn("Zoom and Teams noise suppression often strips far-end laughter, and muted " +
           "people laughing are never heard. Expect misses."),
      good("👑 Crown any candidate — or any transcript line — and your pick always wins.")
    ));
    d.appendChild(card(
      h3("Vocal cues — opt-in, per meeting"),
      p("How each confirmed speaker's delivery moves against their own normal. Energy is " +
        "pitch, range, pace and loudness; Friction is gaps, pauses and talking over. " +
        "Their normal comes from their other analyzed meetings, or from this call when " +
        "there are none."),
      list([
        "Turn it on with the “Vocal cues” checkbox before recording (live Room panel + " +
          "measured after the call), or “Analyze vocal cues” in Review afterwards.",
        "Only meetings you analyze are measured, and only they feed anyone's baseline. " +
          "Clear opts one back out.",
        "Gauges need a confirmed speaker or a strong voiceprint match (tagged “matched”). " +
          "You don't have to Confirm a strong match — ✗ Not undoes it if it's wrong. " +
          "Confirm adds the meeting to their voiceprint, which helps after a new mic or phone.",
      ]),
      warn("What it can say: “faster and higher than her normal.” What it cannot: why. " +
           "Excited and upset look the same. The words describe delivery, never feelings " +
           "or honesty — this is not a lie detector."),
      p("Your own gauges are the shakiest: the headset's mains hum and low level make " +
        "your pitch hard to read.")
    ));
    d.appendChild(card(
      h3("Where things live"),
      tableOf([["Measurements", "<meeting>/cues.json (no names — resolved from speakers.json when shown)"],
               ["Live readings", "<meeting>/cues_live.jsonl, laughs_live.jsonl"],
               ["Laughter", "<meeting>/laughs.json"],
               ["Your crown", "<meeting>/awards.json — kept when you Clear"],
               ["Voices you named live", "<meeting>/live_names.json (your picks + in-call voice samples)"],
               ["Opted in at record time", "<meeting>/cues_requested.json"]],
              ["What", "Where"])
    ));
    return d;
  }

  // ── fixing voices ──────────────────────────────────────────────────────────
  function paneVoices() {
    const d = el("div", "hp");

    d.appendChild(card(
      h3("How a voiceprint accumulates"),
      rich(["One vector per person, and each enrolment is averaged into it with a running " +
            "mean. That means it ", ["gets better every time you confirm someone"], " — but " +
            "it also means a bad sample can only ever be diluted, never subtracted. The " +
            "individual samples are not kept, only the average."]),
      p("Roughly: 1 sample lands near 0.91 against the person's true voice, 3 samples reach " +
        "~0.97, 8 samples ~0.99. Most of the gain is in the first three."),
      warn("If the wrong voice does get averaged in, adding good samples takes many meetings " +
           "to pull the print back below the auto-assign line — and until it does, the wrong " +
           "person keeps getting auto-named. Replace it instead of waiting it out.")
    ));

    d.appendChild(card(
      h3("Fix a print that has the wrong voice in it — mt enroll --replace"),
      p("--replace throws the stored average away and rebuilds the print from this one " +
        "cluster, resetting the sample count to 1. It is the only way to actually remove " +
        "something from a print."),
      p("1. Find a meeting where that person speaks a lot and is cleanly separated. Open it " +
        "in Review and check the speaker's self-similarity — you want a high number, which " +
        "means the cluster holds one person."),
      p("2. Note the speaker key (system-spk2) or its display number (3), then:"),
      cmd("mt enroll Customers/acme/meetings/2026-09-16-acme-weekly \\\n" +
          "          --speaker 3 --name \"Alex Example\" --replace"),
      p("3. It re-measures the cluster from the audio before writing, prints the " +
        "self-similarity, and reports \"replaced … (1 sample(s))\"."),
      p("4. Enroll that person normally from one or two more good meetings to build the " +
        "average back up."),
      good("Safe to run: it refuses a cluster that holds more than one voice, and it will " +
           "not let two clusters in the same meeting share a name unless they actually " +
           "sound alike.")
    ));

    d.appendChild(card(
      h3("Remove a voice entirely — retire"),
      rich(["In ", ["Review"], ", open the speaker and scroll to the bottom of the right-hand " +
            "panel: there is a retire zone that takes two clicks and tells you exactly what " +
            "it will do. That is the control you have seen. On the command line:"]),
      cmd("mt forget \"Sam Sample\" --reason \"garbled transcript, wrong person\""),
      p("Retiring never deletes — the vector moves to voiceprints/retired/ and is indexed, " +
        "so it can come back:"),
      cmd("mt restore \"Sam Sample\""),
      p("Fixing a spelling is different again, and keeps the vector and the sample count:"),
      cmd("mt rename \"Alex Exampel\" \"Alex Example\"")
    ));

    d.appendChild(card(
      h3("What the web page can and cannot do"),
      tableOf([["Confirm a speaker, enroll from it", "Review page", "also mt enroll"],
               ["Reassign individual lines", "Review page", "no CLI equivalent"],
               ["Assign the rest of a mixed cluster", "Review page", "no CLI equivalent"],
               ["Retire a print", "Review page", "mt forget"],
               ["Restore a retired print", "Review page", "mt restore"],
               ["Replace a print outright", "— not in the web app —", "mt enroll --replace"],
               ["Rename an enrolled person", "Contacts page (aliases)", "mt rename"]],
              ["Task", "Web", "Command line"])
    ));

    d.appendChild(card(
      h3("The one mistake nothing catches"),
      p("A clean single-speaker cluster filed under the wrong name, when that person has no " +
        "other print to contradict it. Nothing about it is self-inconsistent, so no guard " +
        "fires. That is what your ear and the per-line “not them?” override are for."),
      p("A low match score on a name you are adding for the first time is normal and means " +
        "nothing — there was simply no print to match against yet.")
    ));

    return d;
  }

  function tableOf(rows, head) {
    const t = el("table"), thead = el("thead"), tr = el("tr");
    head.forEach(h => tr.appendChild(el("th", null, h)));
    thead.appendChild(tr); t.appendChild(thead);
    const tb = el("tbody");
    rows.forEach(r => {
      const x = el("tr");
      r.forEach((c, i) => x.appendChild(el("td", i === 0 ? "k" : null, c)));
      tb.appendChild(x);
    });
    t.appendChild(tb);
    return t;
  }

  // ── shortcuts ──────────────────────────────────────────────────────────────
  // Only what is actually bound. An invented shortcut in a help page is a small
  // betrayal every time someone presses it.
  const KEYS = [
    ["Esc", "Close the summary window, or the contact sheet"],
    ["Enter", "In the search box: run the search now instead of waiting for the pause"],
    ["Enter / Space", "On a focused search result or contact row: open it"],
    ["Tab", "Move through results and rows — every row is focusable"],
    ["Space", "In Review, with the audio focused: play / pause"],
  ];

  function paneKeys() {
    const d = el("div", "hp");
    d.appendChild(card(
      h3("Keyboard"),
      p("The set is small on purpose — the app is mostly one button per meeting."),
      (function () {
        const g = el("div", "keys");
        KEYS.forEach(([k, what]) => {
          const kb = el("div"); kb.appendChild(el("kbd", null, k));
          g.appendChild(kb);
          g.appendChild(el("div", null, what));
        });
        return g;
      })()
    ));
    d.appendChild(card(
      h3("Clicks worth knowing"),
      list([
        "Click any transcript line in Review to seek the audio to that moment.",
        "Hover a transcript line and use “not them?” to reassign that one line.",
        "A matched voice is pre-selected in the name picker — Confirm accepts it in one click.",
        "The tab title shows recording and processing progress, so a buried tab still reports.",
        "Search result timestamps deep-link into the recording.",
        "In Review, click an award card, a Vocal-cues block or a Best Saying candidate to hear that moment.",
        "Clicking one of your own lines plays your mic; anyone else's plays the far end. The label by the player says which.",
        "Hover a transcript line and use 👑 to crown it the meeting's Best Saying — your pick always wins.",
        "“Field guide” on the Conversation, Vocal cues and Room panels explains every column, colour and award.",
      ])
    ));
    d.appendChild(card(
      h3("Terminal"),
      p("Ctrl-C ends a live recording and starts the full pass — it is the normal way to stop, " +
        "not an abort. Stopping the web server never stops a recording."),
      cmd("./mt web        # the app\n./mt web --port 8790 --no-open")
    ));
    return d;
  }

  // ── every command ──────────────────────────────────────────────────────────
  function paneCli() {
    const d = el("div", "hp");
    d.appendChild(p("Read live from mt's own argument parser, so this list cannot fall behind " +
                    "the tool. Run any of them with --help for the same text in the terminal."));
    (DATA.commands || []).forEach(c => {
      const pos = (c.positional || [])
        .map(x => x.required ? "<" + x.name + ">" : "[" + x.name + "]").join(" ");
      const kids = [h3(("mt " + c.name + " " + pos).trim())];
      if (c.help) kids.push(el("div", "why", c.help));
      if ((c.options || []).length) {
        const rows = c.options.map(o => [o.display, o.help || ""]);
        kids.push(tableOf(rows, ["Flag", "What it does"]));
      }
      d.appendChild(card(...kids));
    });
    return d;
  }

  // ── numbers & paths ────────────────────────────────────────────────────────
  const THRESH_WHY = {
    auto_assign_score: "At or above this, a voice is named automatically from its print.",
    suggest_score: "Below this a match is not even offered as a suggestion.",
    min_self_similarity: "Below this a cluster holds more than one person: never named, never enrolled.",
    caution_self_similarity: "Advisory only. Between this and the hard line, enrolling may blend two voices.",
    merge_similarity: "Two clusters above this are merged into one speaker.",
    cluster_threshold: "Diarization distance — higher merges more. 0.95 starts merging real people.",
    min_speaker_seconds: "A cluster shorter than this is absorbed into whoever it matches.",
  };

  function paneNumbers() {
    const d = el("div", "hp");
    d.appendChild(card(
      h3("Thresholds"),
      p("Live from config.json. Measured separation on this machine: the same speaker scores " +
        "about 0.91, a stranger about 0.43."),
      tableOf(Object.entries(DATA.thresholds || {})
                .map(([k, v]) => [k + "  " + v, THRESH_WHY[k] || ""]),
              ["Setting", "What it decides"])
    ));
    d.appendChild(card(
      h3("Microphone priority"),
      p("Ordered name fragments — the first one actually plugged in wins, which keeps the " +
        "right mic selected at home and at the office. Webcam and dock mics are left out " +
        "deliberately; they sound worst and often grab the default slot."),
      list(DATA.mic_priority || [])
    ));
    const paths = DATA.paths || {};
    d.appendChild(card(
      h3("Where things live"),
      tableOf([["voiceprints", paths.voiceprints || ""],
               ["recordings", paths.recordings || ""],
               ["config", paths.config || ""],
               ["the tool", paths.tool || ""]], ["What", "Path"]),
      warn("Back up the voiceprints folder. It is the accumulated value in this tool and " +
           "nothing else can rebuild it.")
    ));
    return d;
  }

  // ── hygiene & proposals ────────────────────────────────────────────────────
  // Written for someone picking this back up after a few days away: what they see, what
  // each button does, and what to paste to their AI assistant. The rules themselves live in hygiene.py,
  // hygiene_memory.py and MEMORY-PROPOSALS.md. If those change, this card changes too.
  function paneHygiene() {
    const d = el("div", "hp");

    d.appendChild(card(
      h3("Picking this back up"),
      p("The Hygiene page (link in every page's header) is where cleanup waits for you. The " +
        "number on the link is what's waiting on a decision from you: one number is Data " +
        "groups; “5 · 2” is Data groups · memory proposals."),
      rich([["Assistant memory is off "], "until you set \"memory_dir\" in config.json " +
            "(\"claude\" = Claude Code's memory folder for your workspace). Once it is on, " +
            "ask your AI assistant for a first proposal:"]),
      paste("Start the memory cleanup. File the smallest safe merge as ONE proposal, " +
          "re-checked against today's files. Follow MEMORY-PROPOSALS.md in the mt folder."),
      p("Then open Hygiene → Assistant memory and review it. That's the whole loop: your " +
        "assistant proposes, you decide, the tool applies.")
    ));

    d.appendChild(card(
      h3("Two areas, handled differently on purpose"),
      tableOf([["What it cleans", "Values in the database", "The memory files your assistant loads every session"],
               ["Who finds problems", "The tool, by rule", "The tool finds the mechanical ones; your assistant proposes merges, splits and fixes"],
               ["You approve", "A group at a time (you can untick rows)", "One proposal at a time. There's no bulk approve"],
               ["Before anything changes", "A full DB backup", "A full copy of the memory folder, plus a before-copy of each file"],
               ["If you change your mind", "Undo in History", "Undo in History"],
               ["Colour", "Teal", "Violet"]],
              ["", "Data", "Assistant memory"]),
      p("Why the difference: a bad data fix breaks one row you'll notice. A bad memory change " +
        "quietly changes how your assistant behaves everywhere.")
    ));

    d.appendChild(card(
      h3("Data: approving a group"),
      list([
        "Open Hygiene → Data. Each card is one group: what's wrong, how many rows, and the " +
          "first three as before → after.",
        "Show all lists every row. Untick any row you want left out; it stays in the group " +
          "for next time.",
        "On a Link files group, Open previews the file before you link it.",
        "Approve… opens a confirm. Cancel is the default, and Escape backs out. Yes applies " +
          "the whole group at once, after a backup.",
        "A green banner confirms it, with Undo. Every apply is also listed under History, " +
          "each with its own Undo.",
      ], true),
      tableOf([["Fix", "A known correct value. Approve applies it"],
               ["Needs your call", "No automatic answer. Pick what the value becomes, then approve"],
               ["Link files", "Adds a file link to each row (an Open button on the tracking row). The row's text isn't changed"],
               ["For awareness", "Nothing to approve here; fix it where the card says"],
               ["Phase 7", "Locked: owner names → contacts, a later phase on this same screen"]],
              ["Tag", "Meaning"]),
      p("Skip for now hides a group until its rows change. New rows bring it back on their own.")
    ));

    d.appendChild(card(
      h3("Assistant memory: the proposal loop"),
      list([
        "Findings (lower half of the tab) are what the tool spotted by itself: big files, " +
          "dead links, stale project memories, long index lines, look-alike files. The tool " +
          "can't fix them itself.",
        "Copy request for your assistant on a finding copies a ready-made request. Paste it " +
          "into an assistant session.",
        "Your assistant reads the files and files ONE proposal. It shows up under Proposals " +
          "from your assistant, and the badge's second number goes up.",
        "Review… opens it. The right-hand column, “After approving”, is exactly what the " +
          "files will say. Red rows are removed, green rows are added.",
        "Read the boxes at the top first: errors, person checks, and lines that would leave " +
          "memory. For a contradiction, the orange box shows both statements, and the wrong " +
          "line is outlined in the diff.",
        "Decide (next card). Nothing touches memory until you press Approve and then Yes.",
      ], true)
    ));

    d.appendChild(card(
      h3("Your four choices on a proposal"),
      tableOf([["Approve…", "Applies it after a full backup. A banner with Undo appears. It's greyed out while there are errors"],
               ["Edit the proposed text", "Change the wording yourself, Save my edit, then Approve. Only on files the proposal writes"],
               ["Send back to your assistant…", "Write what's wrong. You get a line to Copy and paste to your assistant; it revises the proposal and it comes back as “Revised — review again”"],
               ["Reject…", "Nothing changes. The proposal is kept, marked rejected, with your note"]],
              ["Button", "What happens"]),
      p("A sent-back card stays on the list as “Sent back to your assistant”, with your notes " +
        "on it, until it is revised.")
    ));

    d.appendChild(card(
      h3("What the warnings mean"),
      tableOf([["Red box (error)", "Approve is blocked. Usually: “changed since your assistant proposed this” (another session edited the file) or a link that would break. Send it back"],
               ["“N lines leave your assistant's memory entirely”", "Those lines won't exist anywhere afterwards, listed right under the file name. Expected when fixing a contradiction; read them anyway"],
               ["“Person check: …”", "A person's name or role changes, or a known contact disappears. Confirm it's right before approving"],
               ["“links would point into archive/”", "Something would quietly start pointing at an archived copy instead of a live memory. Usually send back"],
               ["“N lines reworded”", "Normal: the wording of those lines changed; old and new are side by side in the diff"],
               ["“moved to archive/”", "Normal for a merge: the old file is kept in archive/, never deleted"],
               ["“the whole old file, verbatim”", "Normal for a split: the full old text is kept as a -history file, copied by the tool, not retyped"]],
              ["You see", "It means"])
    ));

    d.appendChild(card(
      h3("Kinds of proposal"),
      tableOf([["merge", "Two memories that say the same thing become one; the other moves to archive/ and every link follows"],
               ["split", "A big file becomes a short current state; the full old file is kept verbatim as a -history copy"],
               ["contradiction", "Two memories disagree; one is corrected. Shows both quotes and which one wins"],
               ["retype", "A memory filed under the wrong type (e.g. project → reference). Nothing else changes"],
               ["archive", "A finished or overtaken memory moves to archive/"],
               ["edit", "Anything else: a shorter index line, a fixed link, an updated fact"]],
              ["Kind", "What it does"])
    ));

    d.appendChild(card(
      h3("Lines to paste to your assistant"),
      p("What's waiting, and any notes you left:"),
      paste("List the mt memory proposals (hygiene_memory.py --list)."),
      p("When a proposal says a file changed since it was proposed:"),
      paste("Memory proposal m-YYYYMMDD-NN says a file changed since you proposed it. " +
          "Re-read the files and --revise it."),
      p("After Send back, use the line the page gives you. It names the proposal and tells " +
        "your assistant where your notes are.")
    ));

    d.appendChild(card(
      h3("The safety net"),
      list([
        "Your assistant never applies its own proposal, and must not edit memory files directly.",
        "Nothing is deleted: merged files move to archive/, and splits keep the whole old " +
          "file.",
        "If a change goes wrong part-way, every file is put back automatically.",
        "Undo puts every file back exactly. It refuses if a file was edited since, so it " +
          "never half-undoes. The full before-copy is still on disk if you need it.",
      ]),
      tableOf([["Memory proposals", "~/Tools/mt/hygiene/proposals/ (kept forever, with status)"],
               ["Memory backups", "~/.local/share/mt/hygiene-memory/"],
               ["Data backups", "DB/mt.db.bak-…-hygiene-….gz (next to your database)"],
               ["Your assistant's rulebook", "~/Tools/mt/MEMORY-PROPOSALS.md"]],
              ["What", "Where"])
    ));
    return d;
  }

  const PANES = { recipes: paneRecipes, voices: paneVoices, cues: paneCues, keys: paneKeys,
                  cli: paneCli, numbers: paneNumbers, hygiene: paneHygiene };

  function show(tab) {
    pane.textContent = "";
    pane.appendChild(PANES[tab]());
    document.querySelectorAll("#tabs button").forEach(b =>
      b.classList.toggle("on", b.dataset.tab === tab));
    if (location.hash.slice(1) !== tab) history.replaceState(null, "", "#" + tab);
  }

  document.querySelectorAll("#tabs button").forEach(b =>
    b.addEventListener("click", () => show(b.dataset.tab)));

  fetch("/api/help").then(r => r.json()).then(d => {
    DATA = d;
    const want = location.hash.slice(1);
    show(PANES[want] ? want : "recipes");
  }).catch(() => {
    pane.textContent = "";
    pane.appendChild(el("div", "empty", "could not load the command reference"));
  });
})();

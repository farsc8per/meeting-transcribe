// mtcal — today's calendar, read straight from macOS Calendar via EventKit.
//
// Why this exists. `gog calendar` works only after unlocking a local *file* keyring, and
// that password is not recoverable here: the token and the OAuth client secret are both
// encrypted inside it, and an agent cannot handle gog credentials at all (the permission
// classifier blocks it — same block as Templates/slides_api.py). Three sessions died on
// that. EventKit needs no password, no OAuth client and no token refresh: macOS already
// holds the Google account, and the only gate is a one-time Calendars permission prompt.
//
// Output is deliberately shaped like the Google Calendar API so web.py's shape_events()
// consumes it unchanged: {"events":[{id, summary, start:{dateTime|date}, end:{...},
// attendees:[{displayName,email,...}], conferenceUrl, ...}]}
//
// Usage:
//   mtcal                     today
//   mtcal --days 2            today + tomorrow
//   mtcal --offset 1          tomorrow only
//   mtcal --calendars         list the calendars macOS can see (diagnosis)

import Foundation
import EventKit

// ---------------------------------------------------------------- output

func emit(_ obj: [String: Any]) {
    let opts: JSONSerialization.WritingOptions = [.prettyPrinted, .sortedKeys]
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: opts) else {
        FileHandle.standardError.write(Data("mtcal: could not encode JSON\n".utf8))
        exit(3)
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

/// Always fails as JSON on stdout. The caller parses stdout either way, so a plain-text
/// death would show up as "could not parse" and hide the real reason.
func die(_ why: String, hint: String = "", code: Int32 = 2) -> Never {
    var o: [String: Any] = ["ok": false, "why": why, "events": []]
    if !hint.isEmpty { o["hint"] = hint }
    emit(o)
    exit(code)
}

// ---------------------------------------------------------------- arguments

var days = 1
var offset = 0
var listCalendars = false

var it = CommandLine.arguments.dropFirst().makeIterator()
while let a = it.next() {
    switch a {
    case "--days":      days = Int(it.next() ?? "") ?? 1
    case "--offset":    offset = Int(it.next() ?? "") ?? 0
    case "--calendars", "--list-calendars": listCalendars = true
    case "--today":     days = 1            // accepted for symmetry with gog
    case "-h", "--help":
        print("""
        mtcal — today's calendar from macOS Calendar (EventKit)
          --days N       number of days to return (default 1)
          --offset N     start N days from today (default 0)
          --calendars    list visible calendars and exit
        """)
        exit(0)
    default:
        die("unknown argument: \(a)")
    }
}
days = max(1, min(days, 31))

// ---------------------------------------------------------------- permission
//
// Raw values are used on purpose. EKAuthorizationStatus gained `.fullAccess` (3, the same
// number as the now-deprecated `.authorized`) and `.writeOnly` (4) in macOS 14, so
// switching on the enum needs availability gymnastics for no benefit.
//   0 notDetermined  1 restricted  2 denied  3 authorized/fullAccess  4 writeOnly

let store = EKEventStore()
let status = EKEventStore.authorizationStatus(for: .event).rawValue

let SETTINGS_HINT = "System Settings → Privacy & Security → Calendars, and switch on the "
                  + "app you launch mt from (Terminal, iTerm, or Claude Code)."

if status == 1 {
    die("calendar access is restricted by policy on this Mac")
}
if status == 2 {
    die("macOS has denied calendar access to this terminal", hint: SETTINGS_HINT)
}
if status == 4 {
    die("this terminal has write-only calendar access, which cannot read events",
        hint: SETTINGS_HINT)
}
if status == 0 {
    // First run. macOS shows the prompt against the *terminal*, not this binary, so it
    // has to be answered by hand once.
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    var failure: Error?
    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents { ok, err in granted = ok; failure = err; sem.signal() }
    } else {
        store.requestAccess(to: .event) { ok, err in granted = ok; failure = err; sem.signal() }
    }
    if sem.wait(timeout: .now() + 120) == .timedOut {
        die("timed out waiting for the macOS calendar permission dialog",
            hint: "answer the dialog, then run this again")
    }
    if !granted {
        let extra = failure.map { ": \($0.localizedDescription)" } ?? ""
        die("calendar permission was not granted\(extra)", hint: SETTINGS_HINT)
    }
}

// ---------------------------------------------------------------- calendars

let cals = store.calendars(for: .event)

if listCalendars {
    emit(["ok": true, "calendars": cals.map { c in
        ["title": c.title,
         "source": c.source?.title ?? "",
         "type": c.source?.sourceType.rawValue ?? -1,
         "id": c.calendarIdentifier] as [String: Any]
    }])
    exit(0)
}

if cals.isEmpty {
    die("macOS Calendar has no calendars configured",
        hint: "Add the Google account once: System Settings → Internet Accounts → Google, "
            + "sign in with your work Google account and tick Calendars. Then retry.")
}

// ---------------------------------------------------------------- events

let cal = Calendar.current
let base = cal.startOfDay(for: Date())
guard let from = cal.date(byAdding: .day, value: offset, to: base),
      let to = cal.date(byAdding: .day, value: days, to: from) else {
    die("could not compute the date range")
}

let iso: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZZZZZ"
    return f
}()
let ymd: DateFormatter = {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = "yyyy-MM-dd"
    return f
}()

let LINK_HOSTS = ["meet.google.com", "zoom.us", "teams.microsoft.com",
                  "teams.live.com", "webex.com", "gotomeeting.com"]
let urlRx = try! NSRegularExpression(pattern: "https?://[^\\s<>\"'\\)\\]]+")

/// Google puts the Meet link in the event URL, Teams and Zoom bury it in the description.
/// Checked in that order so the canonical field wins when both are present.
func meetingLink(_ ev: EKEvent) -> String {
    var pool: [String] = []
    if let u = ev.url?.absoluteString { pool.append(u) }
    if let l = ev.location, !l.isEmpty { pool.append(l) }
    if let n = ev.notes, !n.isEmpty { pool.append(n) }
    for text in pool {
        let ns = text as NSString
        let found = urlRx.matches(in: text, range: NSRange(location: 0, length: ns.length))
        for m in found {
            let s = ns.substring(with: m.range)
            let low = s.lowercased()
            if LINK_HOSTS.contains(where: { low.contains($0) }) { return s }
        }
    }
    return ""
}

func addressOf(_ p: EKParticipant) -> String {
    let s = p.url.absoluteString
    return s.lowercased().hasPrefix("mailto:") ? String(s.dropFirst("mailto:".count)) : ""
}

/// CalDAV frequently omits the display name, so fall back to the address rather than
/// emitting a blank invitee — a blank is worse than an email in the candidate list.
func displayOf(_ p: EKParticipant) -> String {
    if let n = p.name, !n.trimmingCharacters(in: .whitespaces).isEmpty { return n }
    return addressOf(p)
}

// EKParticipantStatus: 0 unknown 1 pending 2 accepted 3 declined 4 tentative
func statusWord(_ p: EKParticipant) -> String {
    switch p.participantStatus.rawValue {
    case 1:  return "needsAction"
    case 2:  return "accepted"
    case 3:  return "declined"
    case 4:  return "tentative"
    default: return "unknown"
    }
}

let predicate = store.predicateForEvents(withStart: from, end: to, calendars: cals)
let events = store.events(matching: predicate)
    .sorted { ($0.startDate ?? Date.distantPast) < ($1.startDate ?? Date.distantPast) }

var out: [[String: Any]] = []
for ev in events {
    guard let st = ev.startDate else { continue }
    let en = ev.endDate ?? st

    var attendees: [[String: Any]] = []
    var iDeclined = false
    for p in ev.attendees ?? [] {
        let mine = p.isCurrentUser
        if mine && p.participantStatus.rawValue == 3 { iDeclined = true }
        attendees.append([
            "displayName": displayOf(p),
            "email": addressOf(p),
            "responseStatus": statusWord(p),
            "organizer": p.participantRole.rawValue == 1,   // 1 == chair/organizer
            "self": mine,
        ])
    }

    var e: [String: Any] = [
        "id": ev.eventIdentifier ?? UUID().uuidString,
        "summary": ev.title ?? "(no title)",
        "attendees": attendees,
        "conferenceUrl": meetingLink(ev),
        "location": ev.location ?? "",
        "calendar": ev.calendar?.title ?? "",
        "organizerName": ev.organizer.map(displayOf) ?? "",
        "declined": iDeclined,
        "cancelled": ev.status == .canceled,
        "source": "eventkit",
    ]
    if ev.isAllDay {
        e["start"] = ["date": ymd.string(from: st)]
        e["end"] = ["date": ymd.string(from: en)]
    } else {
        e["start"] = ["dateTime": iso.string(from: st)]
        e["end"] = ["dateTime": iso.string(from: en)]
    }
    out.append(e)
}

// `sources` lets the caller tell an empty day apart from a missing account: if no
// Google source is attached, zero events means "not linked", not "nothing on".
let sources = Array(Set(cals.map { $0.source?.title ?? "" })).sorted()
emit(["ok": true, "events": out, "calendars": cals.count,
      "sources": sources, "provider": "eventkit"])

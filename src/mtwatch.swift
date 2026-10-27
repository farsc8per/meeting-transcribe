// mtwatch — find the meeting on screen and read the names off it.
//
// Deliberately does NOT assume the meeting is frontmost, in a fixed window, or in a
// fixed browser tab. ScreenCaptureKit can capture a named window even when it is
// occluded, so each candidate window is captured independently of what the user is
// actually looking at.

import Foundation
import ScreenCaptureKit
import CoreImage
import Vision
import AppKit
import ImageIO

func log(_ s: String) { FileHandle.standardError.write(("[mtwatch] " + s + "\n").data(using: .utf8)!) }
func emit(_ s: String) { print(s); fflush(stdout) }

// MARK: - what counts as a meeting window

let meetingApps: Set<String> = [
    "us.zoom.xos",                  // Zoom
    "com.microsoft.teams",          // Teams (classic)
    "com.microsoft.teams2",         // Teams (new)
    "com.cisco.webexmeetingsapp",
    "com.electron.dialpad",         // Dialpad
]
let browserApps: Set<String> = [
    "com.google.Chrome", "com.brave.Browser", "com.apple.Safari",
    "com.microsoft.edgemac", "com.google.Chrome.beta", "org.mozilla.firefox",
]
// Browser titles that indicate a live call rather than a random tab.
let meetingTitleHints = ["meet.google.com", "google meet", "meet -", "| meet",
                         "zoom.us", "teams.microsoft.com", "webex", "picture in picture",
                         "picture-in-picture"]
// Extra live-call title fragments from --live-title (repeatable). mt passes "| <org>" for
// each internal domain: Teams titles a meeting window "<meeting> | <your org>".
var extraLiveTitles: [String] = []

struct Candidate {
    let windowID: CGWindowID
    let app: String
    let bundle: String
    let title: String
    let rect: CGRect
    let reason: String
    var window: SCWindow
}

// Why a window was or was not taken, alongside the decision itself. The watch loop logs
// these to screen_windows.jsonl: on Sep 22 and Sep 29 two Teams calls produced an
// empty screen.jsonl and nothing at all said why, because a rejected window left no trace.
func classify(_ w: SCWindow) -> Candidate? { decide(w).0 }

func decide(_ w: SCWindow) -> (Candidate?, String) {
    let bundle = w.owningApplication?.bundleIdentifier ?? ""
    let app = w.owningApplication?.applicationName ?? "?"
    let title = w.title ?? ""
    let r = w.frame
    // ignore slivers and menu-bar furniture
    if r.width < 120 || r.height < 90 { return (nil, "too small") }

    // Chrome/Zoom/Teams all spawn helper windows that are pure noise for OCR.
    let junk = ["ZM_HUD", "TOAST", "Healthcheck", "Notification", "AutoFill",
                "Backstop", "underbelly", "Wallpaper-", "Item-0", "FocusModes"]
    if junk.contains(where: { title.localizedCaseInsensitiveContains($0) }) { return (nil, "junk title") }

    // An app's HOME window is not a meeting. Dialpad's main window is a contact list;
    // OCRing it produced a roster of ten people who were in no meeting at all. Zoom
    // Workplace and the Teams home window are the same trap.
    let homeWindows = ["zoom workplace", "microsoft teams", "dialpad", "dialpad - notification",
                       "teams", "zoom - home", "webex"]
    if meetingApps.contains(bundle) {
        if homeWindows.contains(title.lowercased()) { return (nil, "home window") }
        // Teams names its Chat/Calendar/Activity windows after whatever thread you are
        // looking at, so "Chat | Acme Weekly Review | example.com"
        // otherwise matches the live-meeting hints and reads as an active call. It is
        // not one, and letting it through misnames recordings and would fire a bogus
        // auto-record prompt.
        let deadPrefixes = ["chat |", "calendar |", "activity |", "meeting join |",
                            "files |", "teams |", "you have left"]
        let low0 = title.lowercased()
        if deadPrefixes.contains(where: { low0.hasPrefix($0) }) { return (nil, "dead prefix") }
        // Titles that positively indicate a call in progress.
        let live = ["meeting", "in call", "call with", "zoom meeting", "everywhere",
                    "webinar", "huddle"] + extraLiveTitles
        let looksLive = live.contains(where: { title.lowercased().contains($0) })
        let why = looksLive ? "active meeting" : "meeting app"
        return (Candidate(windowID: w.windowID, app: app, bundle: bundle, title: title,
                          rect: r, reason: why, window: w), why)
    }
    // New Teams renders through untitled "Microsoft Teams WebView" helper windows. One sat
    // at exactly the Sep 8 meeting window's size (1688x1055) after a call. Idle, they OCR
    // blank and are skipped at no cost; if a call ever draws into one, it is caught here.
    if bundle == "com.microsoft.teams2.helper" {
        if r.width >= 600 && r.height >= 400 {
            return (Candidate(windowID: w.windowID, app: app, bundle: bundle, title: title,
                              rect: r, reason: "teams webview", window: w), "teams webview")
        }
        return (nil, "small teams webview")
    }
    if browserApps.contains(bundle) {
        let lower = title.lowercased()
        if meetingTitleHints.contains(where: { lower.contains($0) }) {
            return (Candidate(windowID: w.windowID, app: app, bundle: bundle, title: title,
                              rect: r, reason: "browser title match", window: w), "browser title match")
        }
        // Google Meet's pop-out when the tab is not focused: a small browser window,
        // usually untitled. Users rely on this, so it must be picked up.
        if r.width < 900 && r.height < 700 {
            return (Candidate(windowID: w.windowID, app: app, bundle: bundle, title: title,
                              rect: r, reason: "possible pop-out", window: w), "possible pop-out")
        }
        return (nil, "browser, no meeting hint")
    }
    return (nil, "not a meeting app")
}

// MARK: - capture one window

func capture(_ w: SCWindow) async -> CGImage? {
    let cfg = SCStreamConfiguration()
    // Downscale hard: OCR only needs the name labels, and this runs every couple of
    // seconds for the whole meeting.
    let scale = min(1.0, 1400.0 / max(w.frame.width, 1))
    cfg.width = max(320, Int(w.frame.width * scale))
    cfg.height = max(240, Int(w.frame.height * scale))
    cfg.showsCursor = false
    cfg.capturesAudio = false
    let filter = SCContentFilter(desktopIndependentWindow: w)
    do {
        return try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)
    } catch {
        log("capture failed for \(w.title ?? "?"): \(error.localizedDescription)")
        return nil
    }
}

// MARK: - OCR

struct TextLine: Codable {
    let text: String
    let confidence: Float
    let x: Double, y: Double, w: Double, h: Double
}

func ocr(_ image: CGImage) -> [TextLine] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    // Names are not dictionary words. Language correction actively mangles them.
    req.usesLanguageCorrection = false
    req.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([req]) } catch {
        log("ocr failed: \(error.localizedDescription)")
        return []
    }
    guard let obs = req.results else { return [] }
    return obs.compactMap { o in
        guard let c = o.topCandidates(1).first else { return nil }
        let b = o.boundingBox
        return TextLine(text: c.string, confidence: c.confidence,
                        x: b.origin.x, y: b.origin.y, w: b.width, h: b.height)
    }
}

// MARK: - main

@main
struct Main {
    static func shareable() async -> SCShareableContent? {
        do { return try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false) }
        catch { log("cannot reach ScreenCaptureKit: \(error.localizedDescription)"); return nil }
    }

    static func main() async {
        // Window-server APIs (screenshot capture) abort with CGS_REQUIRE_INIT unless the
        // process has an AppKit connection first. Prohibited activation keeps it
        // headless — no Dock icon, no focus stealing while he works.
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)

        var mode = "probe"
        var outDir: String?
        var interval: Double = 2.0
        var duration: Double = 0
        var all = false
        var framesDir: String?
        var framesEvery: Double = 5

        var a = Array(CommandLine.arguments.dropFirst())
        while let arg = a.first {
            a.removeFirst()
            switch arg {
            case "--list": mode = "list"
            case "--probe": mode = "probe"
            case "--watch": mode = "watch"
            case "--all": all = true
            case "--out-dir": outDir = a.first; if !a.isEmpty { a.removeFirst() }
            case "--interval": interval = Double(a.first ?? "2") ?? 2; if !a.isEmpty { a.removeFirst() }
            case "--duration": duration = Double(a.first ?? "0") ?? 0; if !a.isEmpty { a.removeFirst() }
            case "--keep-frames": framesDir = a.first; if !a.isEmpty { a.removeFirst() }
            case "--frames-every": framesEvery = Double(a.first ?? "5") ?? 5; if !a.isEmpty { a.removeFirst() }
            case "--live-title": if let t = a.first { extraLiveTitles.append(t.lowercased()); a.removeFirst() }
            default:
                emit("usage: mtwatch [--list | --probe | --watch --out-dir DIR] [--interval S] [--duration S] [--all] [--keep-frames DIR [--frames-every S]] [--live-title TEXT ...]")
                exit(arg == "-h" || arg == "--help" ? 0 : 1)
            }
        }

        guard let content = await shareable() else { exit(1) }

        if mode == "list" {
            emit("bundle\tapp\twindow_id\twidth\theight\tonscreen\tcandidate\ttitle")
            for w in content.windows {
                let b = w.owningApplication?.bundleIdentifier ?? "-"
                let app = w.owningApplication?.applicationName ?? "-"
                let cand = classify(w) != nil ? (classify(w)!.reason) : "-"
                emit("\(b)\t\(app)\t\(w.windowID)\t\(Int(w.frame.width))\t\(Int(w.frame.height))\t\(w.isOnScreen)\t\(cand)\t\(w.title ?? "")")
            }
            exit(0)
        }

        let cands = all ? content.windows.compactMap { w -> Candidate? in
                            let b = w.owningApplication?.bundleIdentifier ?? ""
                            guard w.frame.width > 200, w.frame.height > 150 else { return nil }
                            return Candidate(windowID: w.windowID,
                                             app: w.owningApplication?.applicationName ?? "?",
                                             bundle: b, title: w.title ?? "", rect: w.frame,
                                             reason: "--all", window: w)
                          }
                        : content.windows.compactMap(classify)

        if mode == "probe" {
            if cands.isEmpty { log("no meeting-like windows found"); }
            for c in cands {
                let lines = await capture(c.window).map(ocr) ?? []
                var obj: [String: Any] = [
                    "app": c.app, "bundle": c.bundle, "title": c.title, "reason": c.reason,
                    "width": Int(c.rect.width), "height": Int(c.rect.height),
                    "lines": lines.map { ["text": $0.text, "conf": $0.confidence,
                                          "x": $0.x, "y": $0.y, "w": $0.w, "h": $0.h] },
                ]
                obj["line_count"] = lines.count
                if let d = try? JSONSerialization.data(withJSONObject: obj),
                   let s = String(data: d, encoding: .utf8) { emit(s) }
            }
            exit(0)
        }

        // watch
        guard let od = outDir else { log("--watch needs --out-dir"); exit(1) }
        let dir = URL(fileURLWithPath: od, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let out = dir.appendingPathComponent("screen.jsonl")
        FileManager.default.createFile(atPath: out.path, contents: nil)
        guard let fh = try? FileHandle(forWritingTo: out) else { log("cannot open \(out.path)"); exit(1) }
        fh.seekToEndOfFile()

        signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
        let stop = Gate()
        for sig in [SIGINT, SIGTERM] {
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
            src.setEventHandler { stop.open() }
            src.resume()
            sources.append(src)
        }
        let started = Date()
        if duration > 0 {
            DispatchQueue.global().asyncAfter(deadline: .now() + duration) { stop.open() }
        }
        // Every meeting-ish window we saw and what we decided about it, written when the
        // picture changes and at least every 30 s. Small, and the only way to tell after
        // the fact why a call produced no frames.
        let diagURL = dir.appendingPathComponent("screen_windows.jsonl")
        FileManager.default.createFile(atPath: diagURL.path, contents: nil)
        let diag = try? FileHandle(forWritingTo: diagURL)
        var lastSig = "", lastDiag = -1e9
        let watchBundles = meetingApps.union(browserApps).union(["com.microsoft.teams2.helper"])

        // Opt-in: keep downscaled PNGs of Teams frames, so the speaking ring (Teams has no
        // text label for the active speaker) can be measured. Local only; delete after.
        if let fd = framesDir {
            try? FileManager.default.createDirectory(atPath: fd, withIntermediateDirectories: true)
        }
        var lastFrame: [CGWindowID: Double] = [:]
        emit("WATCHING \(out.path)")

        while !stop.isOpen {
            // Re-enumerate every tick: he moves windows, switches tabs, and Meet's
            // pop-out appears and disappears as he does it.
            guard let live = await shareable() else { break }
            let now = Date().timeIntervalSince(started)
            var seen: [[String: Any]] = []
            var ocrCount: [CGWindowID: Int] = [:]
            for w in live.windows {
                let b = w.owningApplication?.bundleIdentifier ?? ""
                guard watchBundles.contains(b), w.frame.width >= 120, w.frame.height >= 90 else { continue }
                seen.append(["id": Int(w.windowID), "bundle": b, "title": w.title ?? "",
                             "w": Int(w.frame.width), "h": Int(w.frame.height),
                             "onscreen": w.isOnScreen, "decision": decide(w).1])
            }
            for c in live.windows.compactMap(classify) {
                guard let img = await capture(c.window) else { ocrCount[c.windowID] = -1; continue }
                if let fd = framesDir, c.bundle.hasPrefix("com.microsoft.teams"),
                   now - (lastFrame[c.windowID] ?? -1e9) >= framesEvery {
                    lastFrame[c.windowID] = now
                    let f = URL(fileURLWithPath: fd)
                        .appendingPathComponent(String(format: "%06.0f-%d.png", now, Int(c.windowID)))
                    if let dest = CGImageDestinationCreateWithURL(f as CFURL, "public.png" as CFString, 1, nil) {
                        CGImageDestinationAddImage(dest, img, nil)
                        CGImageDestinationFinalize(dest)
                    }
                }
                let lines = ocr(img)
                ocrCount[c.windowID] = lines.count
                if lines.isEmpty { continue }
                let rec: [String: Any] = [
                    "t": now, "app": c.app, "bundle": c.bundle, "title": c.title,
                    "reason": c.reason, "w": Int(c.rect.width), "h": Int(c.rect.height),
                    "lines": lines.map { ["text": $0.text, "conf": $0.confidence,
                                          "x": $0.x, "y": $0.y, "w": $0.w, "h": $0.h] },
                ]
                if let d = try? JSONSerialization.data(withJSONObject: rec) {
                    fh.write(d); fh.write("\n".data(using: .utf8)!)
                }
            }
            if let dh = diag {
                for i in seen.indices {
                    if let id = seen[i]["id"] as? Int, let n = ocrCount[CGWindowID(id)] { seen[i]["ocr_lines"] = n }
                }
                // signature ignores OCR counts so a flickering count does not spam the file
                let sig = seen.map { "\($0["id"]!)|\($0["title"]!)|\($0["w"]!)x\($0["h"]!)|\($0["onscreen"]!)|\($0["decision"]!)" }
                              .joined(separator: ";")
                if sig != lastSig || now - lastDiag >= 30 {
                    lastSig = sig; lastDiag = now
                    if let d = try? JSONSerialization.data(withJSONObject: ["t": now, "windows": seen]) {
                        dh.write(d); dh.write("\n".data(using: .utf8)!)
                    }
                }
            }
            try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
        }
        try? diag?.close()
        try? fh.close()
        emit("STOPPED")
        exit(0)
    }
}

var sources: [DispatchSourceSignal] = []

final class Gate: @unchecked Sendable {
    private let lock = NSLock()
    private var opened = false
    var isOpen: Bool { lock.lock(); defer { lock.unlock() }; return opened }
    func open() { lock.lock(); opened = true; lock.unlock() }
}

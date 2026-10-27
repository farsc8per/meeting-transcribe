// mtcapture — dual-track meeting audio capture for macOS (ScreenCaptureKit)
// Writes system.wav (everyone else, via loopback) and mic.wav (you) as separate,
// sample-synchronized tracks. No virtual audio driver required.

import Foundation
import AVFoundation
import CoreMedia
import ScreenCaptureKit

// MARK: - stderr logging

func log(_ s: String) {
    FileHandle.standardError.write(("[mtcapture] " + s + "\n").data(using: .utf8)!)
}
func emit(_ s: String) {   // machine-readable line on stdout
    print(s)
    fflush(stdout)
}
func die(_ s: String) -> Never {
    log("ERROR: " + s)
    exit(1)
}

// MARK: - capture event log

/// Append-only JSONL beside the recording. The Python/UI half tails this file, so the
/// shapes written here are a contract (see REFERENCE.md): append only, never
/// rewrite, and never let a logging failure reach the recording.
final class EventLog: @unchecked Sendable {
    private let lock = NSLock()
    private let url: URL
    private let started: Date

    init(dir: URL, started: Date) {
        self.url = dir.appendingPathComponent("capture_events.jsonl")
        self.started = started
    }

    /// Seconds since recording start, rounded to the 0.1s the contract's examples use.
    func now() -> Double { EventLog.r1(Date().timeIntervalSince(started)) }
    static func r1(_ v: Double) -> Double { (v * 10).rounded() / 10 }

    /// Ordered on purpose. `JSONSerialization` takes a Dictionary, so it randomises key
    /// order run to run and prints 6.2 as 6.2000000000000002 — both harmless to a parser
    /// and both miserable for a human reading the file next to the contract. This writes
    /// the keys in the order given, and numbers the way the contract's examples show them.
    func append(_ pairs: [(String, Any)]) {
        var out = "{"
        var first = true
        for (k, v) in pairs {
            if !first { out += "," }
            first = false
            out += EventLog.str(k) + ": " + EventLog.val(v)
        }
        out += "}\n"
        write(out)
    }

    private static func str(_ v: String) -> String {
        var o = "\""
        for c in v.unicodeScalars {
            switch c {
            case "\"": o += "\\\""
            case "\\": o += "\\\\"
            case "\n": o += "\\n"
            case "\r": o += "\\r"
            case "\t": o += "\\t"
            default:
                if c.value < 0x20 { o += String(format: "\\u%04x", c.value) } else { o.unicodeScalars.append(c) }
            }
        }
        return o + "\""
    }

    private static func val(_ v: Any) -> String {
        switch v {
        case let b as Bool:   return b ? "true" : "false"
        case let i as Int:    return String(i)
        case let d as Double:
            if !d.isFinite { return "0" }
            var t = String(format: "%.1f", d)
            if t == "-0.0" { t = "0.0" }
            return t
        case let s as String: return str(s)
        default:              return str(String(describing: v))
        }
    }

    private func write(_ line: String) {
        lock.lock(); defer { lock.unlock() }
        guard let d = line.data(using: .utf8) else { return }
        let fm = FileManager.default
        if !fm.fileExists(atPath: url.path) { fm.createFile(atPath: url.path, contents: nil) }
        guard let h = try? FileHandle(forWritingTo: url) else { return }
        h.seekToEndOfFile()
        h.write(d)
        try? h.close()
    }
}

// MARK: - WAV track writer

final class TrackWriter {
    private let url: URL
    private let label: String
    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private let lock = NSLock()
    private(set) var frames: Int64 = 0
    private(set) var peak: Float = 0
    private var recentMax: Float = 0        // peak since the last poll, for the watchdog
    private var probeMax: Float = 0         // peak since the probe armed — its OWN channel,
                                            // so a probe and a watchdog poll cannot eat
                                            // each other's measurement
    private var lastAt: Date?               // wall clock of the last accepted buffer
    private var opened = false
    private var rate: Double = 0        // cached: processingFormat is gone after close()
    /// 16 kHz mono is what whisper and the diarizer both want. Measured on real audio:
    /// identical transcripts to 48 kHz stereo, 6x less disk.
    static let targetRate: Double = 16_000

    init(url: URL, label: String) {
        self.url = url
        self.label = label
    }

    static func pcm(from sb: CMSampleBuffer) -> AVAudioPCMBuffer? {
        guard let fd = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd),
              let fmt = AVAudioFormat(streamDescription: asbd) else { return nil }
        let n = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))
        guard n > 0, let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: n) else { return nil }
        buf.frameLength = n
        let st = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sb, at: 0, frameCount: Int32(n), into: buf.mutableAudioBufferList)
        return st == noErr ? buf : nil
    }

    func write(_ sb: CMSampleBuffer) {
        guard let src = TrackWriter.pcm(from: sb) else { return }
        lock.lock(); defer { lock.unlock() }

        if !opened {
            opened = true
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: Self.targetRate,
                AVNumberOfChannelsKey: 1,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false,
            ]
            do {
                file = try AVAudioFile(forWriting: url, settings: settings,
                                       commonFormat: .pcmFormatFloat32, interleaved: false)
                rate = Self.targetRate
                log("\(label): \(Int(src.format.sampleRate))Hz \(src.format.channelCount)ch -> \(url.lastPathComponent)")
            } catch {
                log("\(label): could not open \(url.path): \(error)")
                return
            }
        }
        guard let f = file else { return }

        // Resolve to a buffer in the file's own format (float32, non-interleaved).
        var toWrite: AVAudioPCMBuffer? = nil
        if src.format == f.processingFormat {
            toWrite = src
        } else {
            if converter == nil || !(converter!.inputFormat == src.format) {
                converter = AVAudioConverter(from: src.format, to: f.processingFormat)
            }
            if let conv = converter {
                let ratio = f.processingFormat.sampleRate / src.format.sampleRate
                let cap = AVAudioFrameCount(Double(src.frameLength) * ratio) + 4096
                if let out = AVAudioPCMBuffer(pcmFormat: f.processingFormat, frameCapacity: cap) {
                    var fed = false
                    var err: NSError?
                    conv.convert(to: out, error: &err) { _, status in
                        if fed { status.pointee = .noDataNow; return nil }
                        fed = true; status.pointee = .haveData; return src
                    }
                    if err == nil { toWrite = out }
                }
            }
        }
        guard let buf = toWrite, buf.frameLength > 0 else { return }

        // Level meter. Safe here because buf is always non-interleaved float32.
        if let cd = buf.floatChannelData {
            var m: Float = 0
            for c in 0..<Int(buf.format.channelCount) {
                let ptr = cd[c]
                for i in 0..<Int(buf.frameLength) { let v = abs(ptr[i]); if v > m { m = v } }
            }
            if m > peak { peak = m }
            if m > recentMax { recentMax = m }
            if m > probeMax { probeMax = m }
        }

        do {
            try f.write(from: buf)
            frames += Int64(buf.frameLength)
            lastAt = Date()
        } catch {
            log("\(label): write failed: \(error)")
        }
    }

    /// Peak since the previous call, then reset. The watchdog uses this to notice a
    /// track that has gone dead while the process keeps happily writing zeros.
    func takeRecentPeak() -> Float {
        lock.lock(); defer { lock.unlock() }
        let v = recentMax
        recentMax = 0
        return v
    }

    /// Start a fresh measurement window for the audio probe.
    func armProbe() {
        lock.lock(); defer { lock.unlock() }
        probeMax = 0
    }

    /// Loudest sample captured since `armProbe()`. Does not reset — the probe polls it.
    func probePeak() -> Float {
        lock.lock(); defer { lock.unlock() }
        return probeMax
    }

    /// When the last buffer was accepted. Recapture uses this to measure the real gap.
    func lastWrite() -> Date? {
        lock.lock(); defer { lock.unlock() }
        return lastAt
    }

    var seconds: Double {
        lock.lock(); defer { lock.unlock() }
        return rate > 0 ? Double(frames) / rate : 0
    }

    func close() {
        lock.lock(); defer { lock.unlock() }
        file = nil          // AVAudioFile finalizes the WAV header on release
        converter = nil
    }

    /// Finalize the header, but give up on the lock at `deadline` instead of blocking
    /// forever. The forced-exit path calls this: taking the same lock every write and
    /// every close takes is what makes a forced exit safe — we cannot be killed halfway
    /// through AVAudioFile's own finalization, because that finalization happens inside
    /// this lock. If the lock is genuinely stuck (a wedged write) we exit anyway and
    /// leave the PCM for `mt process` to salvage: a repairable file beats a live orphan.
    /// Returns true if the header was finalized.
    @discardableResult
    func closeBefore(_ deadline: Date) -> Bool {
        guard lock.lock(before: deadline) else { return false }
        defer { lock.unlock() }
        file = nil
        converter = nil
        return true
    }

    /// Has anything at all been written to this track? Used only for the forced-exit log.
    func wroteAnything() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return frames > 0
    }
}

// MARK: - stream sink

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    let sys: TrackWriter
    let mic: TrackWriter
    var onStop: ((Error?) -> Void)?
    private let slock = NSLock()
    private var current: SCStream?

    init(dir: URL) {
        sys = TrackWriter(url: dir.appendingPathComponent("system.wav"), label: "system")
        mic = TrackWriter(url: dir.appendingPathComponent("mic.wav"), label: "mic")
    }

    /// Make `s` the stream whose buffers count, and hand back the one it replaced.
    /// Buffers from any other stream are dropped. That single rule is what lets a
    /// recapture start the replacement BEFORE tearing the old one down: the gap is one
    /// buffer period, and a replacement that fails to start leaves the running capture
    /// completely untouched.
    @discardableResult
    func adopt(_ s: SCStream) -> SCStream? {
        slock.lock(); defer { slock.unlock() }
        let old = current
        current = s
        return old
    }

    func isCurrent(_ s: SCStream) -> Bool {
        slock.lock(); defer { slock.unlock() }
        return current === s
    }

    /// The stream actually feeding the writers right now — not necessarily the one main
    /// started, since a recapture may have replaced it.
    func currentStream() -> SCStream? {
        slock.lock(); defer { slock.unlock() }
        return current
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard CMSampleBufferDataIsReady(sb) else { return }
        // A stream we have already replaced may still be draining. Its audio is a
        // duplicate of what the current stream is writing, so it must not reach the file.
        guard isCurrent(stream) else { return }
        if type == .audio {
            sys.write(sb)
            return
        }
        if #available(macOS 15.0, *), type == .microphone {
            mic.write(sb)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        // A superseded stream erroring on its way out must never end the recording.
        guard isCurrent(stream) else {
            log("a replaced stream reported: \(error.localizedDescription) (ignored)")
            return
        }
        log("stream stopped with error: \(error.localizedDescription)")
        onStop?(error)
    }
}

// MARK: - mic enumeration

func allMics() -> [AVCaptureDevice] {
    var types: [AVCaptureDevice.DeviceType] = []
    if #available(macOS 14.0, *) { types = [.microphone] }
    return AVCaptureDevice.DiscoverySession(
        deviceTypes: types, mediaType: .audio, position: .unspecified).devices
}

/// The device macOS would hand us if we said nothing.
func defaultMic() -> AVCaptureDevice? {
    AVCaptureDevice.default(for: .audio)
}

/// Accepts an exact uniqueID or a case-insensitive fragment of the device name, so
/// `--mic-id HyperX` works and you never have to paste a CoreAudio UID.
func findMic(_ wanted: String) -> AVCaptureDevice? {
    let mics = allMics()
    if let exact = mics.first(where: { $0.uniqueID == wanted }) { return exact }
    let hits = mics.filter { $0.localizedName.localizedCaseInsensitiveContains(wanted) }
    if hits.count > 1 {
        log("'\(wanted)' matches \(hits.count) mics: " + hits.map { $0.localizedName }.joined(separator: ", "))
    }
    return hits.first
}

func listMics() {
    let mics = allMics()
    if mics.isEmpty { log("no audio input devices found") }
    let def = defaultMic()?.uniqueID
    for d in mics {
        // uid \t name \t default?
        emit("\(d.uniqueID)\t\(d.localizedName)\t\(d.uniqueID == def ? "default" : "")")
    }
}

// MARK: - recapture

/// Replaces the SCStream without breaking the recording.
///
/// Make-before-break: the replacement is created and started while the old stream is
/// still feeding the writers, then `Sink.adopt` switches which stream's buffers count,
/// and only then is the old one stopped. Three consequences, all of them the point —
/// the gap is one buffer period rather than a restart; the WAV files are never reopened,
/// so the recording stays one continuous track; and if the replacement fails to start,
/// the capture in progress is left exactly as it was.
final class Recapturer: @unchecked Sendable {
    /// A genuinely broken machine must not loop forever. Manual (SIGUSR1) requests are
    /// never capped — that is the user's button and it always works.
    static let maxAuto = 3

    private let lock = NSLock()
    private let sink: Sink
    private let cfg: SCStreamConfiguration
    private let noMic: Bool
    private let events: EventLog
    private var filter: SCContentFilter
    private var busy = false
    private var attempts = 0
    private var autoAttempts = 0
    private var capAnnounced = false

    init(sink: Sink, cfg: SCStreamConfiguration, filter: SCContentFilter,
         noMic: Bool, events: EventLog) {
        self.sink = sink; self.cfg = cfg; self.filter = filter
        self.noMic = noMic; self.events = events
    }

    var automaticAttempts: Int { lock.lock(); defer { lock.unlock() }; return autoAttempts }

    /// Everything that touches the lock lives in these synchronous helpers. The async
    /// body must never hold a lock across an `await`.
    private enum Claim { case busy, capped(Int, Bool), go(Int, SCContentFilter) }

    private func claim(reason: String) -> Claim {
        lock.lock(); defer { lock.unlock() }
        if busy { return .busy }
        attempts += 1
        if reason == "auto" && autoAttempts >= Recapturer.maxAuto {
            let firstTime = !capAnnounced
            capAnnounced = true
            return .capped(attempts, firstTime)
        }
        busy = true
        if reason == "auto" { autoAttempts += 1 }
        return .go(attempts, filter)
    }

    private func release(_ f: SCContentFilter?) {
        lock.lock(); defer { lock.unlock() }
        if let f = f { filter = f }
        busy = false
    }

    /// Fire-and-forget. Safe from a signal handler or a watchdog tick.
    func request(reason: String) {
        Task.detached { [weak self] in await self?.run(reason: reason) }
    }

    private func run(reason: String) async {
        let attempt: Int
        let known: SCContentFilter
        switch claim(reason: reason) {
        case .busy:
            log("recapture: one is already in flight — ignoring this \(reason) request")
            return
        case .capped(let n, let firstTime):
            log("recapture: automatic cap of \(Recapturer.maxAuto) reached — not retrying "
                + "by itself. A manual SIGUSR1 still works.")
            // Announced once. The probe keeps emitting system_audio_dead every cycle, so
            // repeating this every 60s would only pad the file.
            if firstTime {
                events.append([("t", events.now()), ("event", "recapture"), ("attempt", n),
                               ("reason", reason), ("ok", false), ("gap_ms", 0)])
            }
            return
        case .go(let n, let f):
            attempt = n; known = f
        }

        log("recapture: attempt \(attempt) (\(reason)) — building a replacement stream")

        // Best effort refresh, in case the display list moved under us. The old stream is
        // still recording throughout, so a slow or failed lookup costs the capture nothing.
        var use = known
        if let content = try? await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: false),
           let d = content.displays.first {
            use = SCContentFilter(display: d, excludingApplications: [], exceptingWindows: [])
        }

        var fresh: SCStream?
        do {
            let s = SCStream(filter: use, configuration: cfg, delegate: sink)
            try s.addStreamOutput(sink, type: .audio,
                                  sampleHandlerQueue: DispatchQueue(label: "mt.sys.\(attempt)"))
            if #available(macOS 15.0, *), !noMic {
                try s.addStreamOutput(sink, type: .microphone,
                                      sampleHandlerQueue: DispatchQueue(label: "mt.mic.\(attempt)"))
            }
            try await s.startCapture()
            fresh = s
        } catch {
            log("recapture: replacement stream failed to start: \(error.localizedDescription) "
                + "— leaving the existing capture alone")
        }

        var ok = false
        var gapMs = 0
        if let s = fresh {
            let mark = sink.sys.lastWrite() ?? Date()
            let old = sink.adopt(s)
            ok = true
            // The first buffer accepted after the switch is, by definition, the new
            // stream's. The distance from the last one before it is the real audio gap.
            var landed = false
            let deadline = Date().addingTimeInterval(3)
            while Date() < deadline {
                if let lw = sink.sys.lastWrite(), lw > mark {
                    gapMs = Int(lw.timeIntervalSince(mark) * 1000)
                    landed = true
                    break
                }
                try? await Task.sleep(nanoseconds: 20_000_000)
            }
            if !landed {
                gapMs = Int(Date().timeIntervalSince(mark) * 1000)
                log("recapture: no buffer from the replacement within 3s — it started but "
                    + "may not be delivering")
            }
            if let o = old {
                Task.detached { try? await o.stopCapture() }
            }
            log("recapture: attempt \(attempt) ok — gap \(gapMs)ms")
        }

        events.append([("t", events.now()), ("event", "recapture"), ("attempt", attempt),
                       ("reason", reason), ("ok", ok), ("gap_ms", gapMs)])
        release(ok ? use : nil)
    }
}

// MARK: - system-audio watchdog + probe

/// Notices the system tap going dead mid-recording — and proves it rather than guessing.
///
/// Measured on two real calls: ScreenCaptureKit kept delivering buffers that were pure
/// digital silence and never raised `didStopWithError`. One call lost 7:01, another
/// 3:05. Silence on its own cannot tell a dead tap from a quiet far end, which is why the
/// old watchdog could only shrug.
///
/// So this speaks a cue from a SEPARATE process (`/usr/bin/say`) and checks whether it
/// lands in the captured track. `cfg.excludesCurrentProcessAudio = true` drops anything
/// mtcapture plays itself — that config line is exactly why the cue must not be played
/// in-process, and an AVAudioPlayer here would read as "dead" 100% of the time.
///
/// Cue heard  -> the tap is alive, the far end is genuinely quiet. Reset, no alert.
/// Cue absent -> the tap is dead. Alert, and hand off to `Recapturer`.
final class SystemWatcher: @unchecked Sendable {
    /// Seconds of unbroken digital silence before the first probe.
    ///
    /// The old bar was 300s: it fired 2 minutes before the end of one 40-minute call
    /// and never fired at all on the other. 45s is short enough that a death costs at
    /// most ~48s of the far side (the 3:05 loss becomes ~0:48, the 7:01
    /// becomes ~0:48), and long enough that ordinary conversational gaps — a pause, a
    /// slide change, someone finding a file — never reach it. A false trigger is nearly
    /// free: three spoken words in the user's headset, heard by nobody on the call, and no
    /// alert at all when the tap turns out to be fine.
    static let baseWindow: Double = 45
    /// Once a probe has proved the tap alive, the bar doubles up to this ceiling, so a
    /// long stretch of the user presenting does not speak a cue every minute. Any real
    /// system audio snaps it straight back to `baseWindow`, which is the case that
    /// matters: a tap that dies after normal conversation is still caught in ~48s.
    static let maxWindow: Double = 180
    /// Hard rate limit on the cue regardless of the window.
    static let minProbeGap: Double = 60
    /// What counts as "nothing is coming through".
    ///
    /// The original test was `< 1e-6` — EXACT digital zeros — because that is what a dead
    /// tap delivers. Measured on this machine while idle, a healthy tap does NOT deliver
    /// zeros: it carries a dither/noise floor of 0.0002-0.0004 with occasional 0.004
    /// spikes, 85-90% of samples non-zero. So `1e-6` could only ever fire on a tap that
    /// was already dead, and the both-sides-silent case the user asked for would have been
    /// unreachable on real hardware. 0.005 sits ~10x above the measured floor and ~10x
    /// below quiet speech (real far-side audio measured 0.05-0.9), so it means "nothing
    /// audible" rather than "digitally dead" — which is the question actually being asked.
    static let silenceLevel: Float = 0.005
    /// What counts as the probe cue having been captured.
    ///
    /// Measured: `say` at output volume 35 lands in the system track at peak 0.14-0.54.
    /// 0.02 is 5x above the worst idle-floor spike and 7x below the weakest measured cue,
    /// so it survives the user turning their output volume well down without ever mistaking
    /// the noise floor for a cue.
    static let heardLevel: Float = 0.02
    /// How long to listen for the cue. `say` takes ~1.2s for three words.
    static let probeWindow: Double = 2.0

    private let lock = NSLock()
    private let sys: TrackWriter
    private let mic: TrackWriter?
    private let dir: URL
    private let started: Date
    private let step: Double
    private let events: EventLog
    private let onDead: (@Sendable () -> Void)?

    private var silentFor: Double = 0       // unbroken system silence, seconds
    private var quietWithMic: Double = 0    // legacy counter: silence while the mic is live
    private var warned = false              // legacy 300s notice, fires once
    private var window: Double = SystemWatcher.baseWindow
    private var lastProbe: Date?
    private var probing = false
    private var deadReported = false
    private var everDead = false

    init(sys: TrackWriter, mic: TrackWriter?, dir: URL, started: Date, step: Double,
         events: EventLog, onDead: (@Sendable () -> Void)?) {
        self.sys = sys; self.mic = mic; self.dir = dir; self.started = started
        self.step = step; self.events = events; self.onDead = onDead
    }

    /// The legacy "quiet for 5 minutes while you were talking" notice fired.
    var wentDead: Bool { lock.lock(); defer { lock.unlock() }; return warned }
    /// A probe actually proved the tap dead at some point.
    var tapConfirmedDead: Bool { lock.lock(); defer { lock.unlock() }; return everDead }

    func poll() {
        // Always drain the meters, even when we are about to bail out, so the next tick
        // measures its own window and not a backlog.
        let sysPeak = sys.takeRecentPeak()
        let micPeak = mic?.takeRecentPeak() ?? 0
        let now = Date()

        lock.lock()
        // While a probe is in flight the cue is our own audio. Scoring it would reset the
        // silence counter and defeat the backoff, so the probe does that bookkeeping.
        if probing { lock.unlock(); return }

        let sysSilent = sysPeak < SystemWatcher.silenceLevel
        if sysSilent {
            silentFor += step
        } else {
            silentFor = 0
            window = SystemWatcher.baseWindow
        }

        // Unchanged from the original: the 5-minute notice, mic-active only.
        if sysSilent && micPeak > 0.004 {
            quietWithMic += step
        } else if !sysSilent {
            quietWithMic = 0
        }
        let legacyWarn = !warned && quietWithMic >= 300
        if legacyWarn { warned = true }

        var back = false
        if !sysSilent && deadReported { deadReported = false; back = true }

        // The trigger. The user's point — "if I'm not talking, someone else should be" —
        // means the mic is not part of this test any more: sustained silence on the
        // system track is worth a probe whether or not they are speaking. That covers the
        // both-sides-silent case, which is most of a meeting and which the old rule
        // could not see at all.
        var probeNow = false
        if sysSilent, silentFor >= window,
           lastProbe == nil || now.timeIntervalSince(lastProbe!) >= SystemWatcher.minProbeGap {
            probeNow = true
            probing = true
            lastProbe = now
        }
        let since = EventLog.r1(now.timeIntervalSince(started) - silentFor)
        let bothSilent = micPeak <= 0.004
        let secs = Int(now.timeIntervalSince(started))
        lock.unlock()

        if back {
            let t = events.now()
            events.append([("t", t), ("event", "system_audio_back"), ("at", t)])
            log("system audio is back")
        }
        if legacyWarn { legacyQuietNotice(secs: secs) }
        if probeNow {
            Task.detached { [weak self] in
                await self?.probe(since: since, bothSilent: bothSilent)
            }
        }
    }

    /// The probe. Every step is wrapped: if anything about it fails we fall back to
    /// today's behaviour, which is to say nothing and keep recording.
    private func probe(since: Double, bothSilent: Bool) async {
        // Never speak over the far side. One last look, as late as possible.
        if sys.takeRecentPeak() >= SystemWatcher.silenceLevel {
            log("probe: the far side started talking — standing down")
            finish(heard: true, ran: false)
            return
        }

        sys.armProbe()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/say")
        p.arguments = ["-r", "190", "System audio check"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            // No cue means no verdict. Losing the probe is acceptable; a false "dead"
            // that triggers a needless recapture is not.
            log("probe: could not run /usr/bin/say (\(error.localizedDescription)) — skipping")
            finish(heard: true, ran: false)
            return
        }

        // Only now, with the cue genuinely on its way, does a probe_start go out — so a
        // probe_start in the events file is always followed by a probe_result.
        events.append([("t", events.now()), ("event", "probe_start")])
        log("probe: cue sent (" + (bothSilent ? "both tracks silent" : "system silent, mic active")
            + String(format: ", %.0fs", since >= 0 ? events.now() - since : 0) + " of silence)")

        var heard = false
        let deadline = Date().addingTimeInterval(SystemWatcher.probeWindow + 1.0)
        while Date() < deadline {
            if sys.probePeak() >= SystemWatcher.heardLevel { heard = true; break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if p.isRunning { p.terminate() }

        let level = sys.probePeak()
        events.append([("t", events.now()), ("event", "probe_result"), ("heard", heard)])
        if heard {
            // Silent resolution: this is the common case and it must not nag.
            log(String(format: "probe: cue heard (peak %.4f) — the tap is alive, the far end "
                       + "is just quiet", level))
        } else {
            log("""
                WARNING: the probe cue was NOT captured — the system audio tap is dead. \
                Everything the other side says is going unrecorded from here. Attempting \
                to recapture; your own track is unaffected.
                """)
            events.append([("t", events.now()), ("event", "system_audio_dead"), ("since", since),
                           ("note", "the cue was not captured — the system tap is not recording")])
        }

        finish(heard: heard, ran: true)
        if !heard { onDead?() }
    }

    private func finish(heard: Bool, ran: Bool) {
        lock.lock()
        silentFor = 0
        if ran {
            if heard {
                window = min(window * 2, SystemWatcher.maxWindow)
            } else {
                deadReported = true
                everDead = true
                window = SystemWatcher.baseWindow
            }
        }
        probing = false
        lock.unlock()
        // The cue polluted the meters. Drop it so the next poll scores a clean window.
        _ = sys.takeRecentPeak()
        _ = mic?.takeRecentPeak()
    }

    /// The original 5-minute notice, kept verbatim so nothing downstream regresses.
    private func legacyQuietNotice(secs: Int) {
        log("""
            note: no system audio for 5 minutes (at \(secs/60)m\(secs%60)s) while your \
            mic is active.
            Normal if you are presenting and nobody else is speaking. If you EXPECT to \
            hear them, check your output device — that is the one case worth catching.
            """)
        events.append([("t", events.now()), ("event", "system_audio_quiet"),
                       ("note", "no system audio for 5 min while the mic was active; "
                              + "may simply be nobody else talking")])
    }
}

// MARK: - microphone watchdog

/// Watches the microphone for the rest of the meeting.
///
/// It deliberately does NOT switch devices. Measured on macOS 26: calling
/// `SCStream.updateConfiguration` with a new `microphoneCaptureDeviceID` is accepted and
/// then delivers **digital silence** for the rest of the recording; detaching and
/// re-attaching the `.microphone` output is worse still — the track stops growing
/// entirely. A silent switch that looks successful is far more damaging than no switch,
/// so this only detects and shouts.
final class MicWatcher: @unchecked Sendable {
    private let lock = NSLock()
    private let dir: URL
    private let started: Date
    private let uid: String
    private let name: String
    private var warned = false

    init(dir: URL, started: Date, uid: String, name: String) {
        self.dir = dir; self.started = started; self.uid = uid; self.name = name
        record(reason: "initial", note: nil)
    }

    var lostDevice: Bool { lock.lock(); defer { lock.unlock() }; return warned }

    private func record(reason: String, note: String?) {
        var obj: [String: Any] = ["t": Date().timeIntervalSince(started),
                                  "name": name, "uid": uid, "reason": reason]
        if let n = note { obj["note"] = n }
        guard let d = try? JSONSerialization.data(withJSONObject: obj) else { return }
        let f = dir.appendingPathComponent("mic.jsonl")
        if !FileManager.default.fileExists(atPath: f.path) {
            FileManager.default.createFile(atPath: f.path, contents: nil)
        }
        if let h = try? FileHandle(forWritingTo: f) {
            h.seekToEndOfFile(); h.write(d); h.write("\n".data(using: .utf8)!); try? h.close()
        }
    }

    func poll() {
        lock.lock(); let already = warned; lock.unlock()
        if already { return }
        if allMics().first(where: { $0.uniqueID == uid }) == nil {
            lock.lock(); warned = true; lock.unlock()
            let at = Int(Date().timeIntervalSince(started))
            log("""
                WARNING: microphone '\(name)' was disconnected at \(at/60)m\(at%60)s.
                macOS cannot hand this recording a different mic mid-stream, so YOUR OWN
                AUDIO IS PROBABLY LOST FROM HERE. The far side is unaffected.
                To capture yourself again: Ctrl-C now and start a second recording.
                """)
            record(reason: "disconnected", note: "your track is likely silent from this point")
        }
    }
}

// MARK: - shutdown gate

/// One-shot async gate. Safe to open from a signal handler on any queue.
final class Gate: @unchecked Sendable {
    private let lock = NSLock()
    private var opened = false
    private var cont: CheckedContinuation<Void, Never>?

    func wait() async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            lock.lock()
            if opened { lock.unlock(); c.resume(); return }
            cont = c
            lock.unlock()
        }
    }

    func open() {
        lock.lock()
        if opened { lock.unlock(); return }
        opened = true
        let c = cont
        cont = nil
        lock.unlock()
        c?.resume()
    }
}

/// Resume-at-most-once guard for the two racers in `withDeadline`.
final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    func fire(_ f: () -> Void) {
        lock.lock(); let already = done; done = true; lock.unlock()
        if !already { f() }
    }
}

/// Run `body`, but stop waiting for it after `seconds`.
///
/// Nothing on the shutdown path may await something that can never finish. SCStream's
/// `stopCapture()` is exactly that: when the ScreenCaptureKit daemon connection has been
/// interrupted — the "application connection being interrupted" failure — it can simply
/// never call back, which is what left a recorder alive for five minutes after the user
/// pressed stop on Sep 17. The abandoned task is not cancelled (there is nothing to
/// cancel); we just stop caring, and the process exits out from under it.
func withDeadline(_ seconds: Double, _ body: @escaping @Sendable () async -> Void) async {
    let once = Once()
    await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { once.fire { c.resume() } }
        Task.detached { await body(); once.fire { c.resume() } }
    }
}

/// The self-imposed exit deadline.
///
/// Armed the instant a stop is requested, from any source — a signal, `--duration`, or
/// the stream reporting itself dead. From that moment this process has `seconds` to be
/// gone; if it is not, this fires and ends it, whatever any async task is still waiting
/// on. It is deliberately dumb: no awaits, no ScreenCaptureKit calls, nothing that can
/// block. Its FIRST act is to finalize the WAV headers, so a forced exit still leaves a
/// complete, readable recording.
final class ShutdownDeadline: @unchecked Sendable {
    private let lock = NSLock()
    private var armed = false
    private let sink: Sink
    private let events: EventLog
    private let started: Date
    private let seconds: Double
    /// Measured on this machine: a healthy stop (stop the stream, let in-flight buffers
    /// land, finalize both headers) takes 0.45-0.51 s. 4 s is roughly eight times that.
    static let defaultSeconds: Double = 4.0
    /// How long the forced path will wait for the writer lock before giving up on a
    /// clean header. A write holds it for microseconds; only a wedged one would not.
    static let finalizeGrace: Double = 1.0

    init(sink: Sink, events: EventLog, started: Date, seconds: Double = ShutdownDeadline.defaultSeconds) {
        self.sink = sink; self.events = events; self.started = started; self.seconds = seconds
    }

    func arm(reason: String) {
        lock.lock(); let already = armed; armed = true; lock.unlock()
        if already { return }
        events.append([("t", events.now()), ("event", "shutdown"), ("reason", reason),
                       ("note", "stop requested (\(reason)) — exiting within \(seconds)s")])
        DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { [self] in fire() }
    }

    private func fire() {
        // Getting here at all means the graceful path did not finish. It is not an
        // error to say so loudly — this line is the only trace of a bug like Sep 17's.
        log("FORCED EXIT: shutdown did not complete within \(seconds)s — finalizing the "
            + "audio and exiting anyway. Something on the teardown path is wedged "
            + "(ScreenCaptureKit is the usual suspect); the recording itself is intact.")
        let deadline = Date().addingTimeInterval(ShutdownDeadline.finalizeGrace)
        let sysOK = sink.sys.closeBefore(deadline)
        let micOK = sink.mic.closeBefore(deadline)
        if !sysOK || !micOK {
            log("FORCED EXIT: could not take the writer lock within "
                + "\(ShutdownDeadline.finalizeGrace)s (system \(sysOK ? "ok" : "STUCK"), "
                + "mic \(micOK ? "ok" : "STUCK")) — the WAV header may be unfinalized. "
                + "`mt process` repairs that; the audio is still on disk.")
        }
        events.append([("t", events.now()), ("event", "forced_exit"),
                       ("system_finalized", sysOK), ("mic_finalized", micOK),
                       ("note", "shutdown did not complete in \(seconds)s — forced")])
        emit("STOPPED \(Int(Date().timeIntervalSince(started)))")
        exit(0)
    }
}

// MARK: - main

@main
struct Main {
    static func main() async {
        var outDir: String?
        var micID: String?
        var noMic = false
        var noSystemWatch = false
        var duration: Double = 0

        var a = Array(CommandLine.arguments.dropFirst())
        while let arg = a.first {
            a.removeFirst()
            switch arg {
            case "--list-mics":
                listMics(); exit(0)
            case "--out-dir":
                outDir = a.first; if !a.isEmpty { a.removeFirst() }
            case "--mic-id":
                micID = a.first; if !a.isEmpty { a.removeFirst() }
            case "--no-mic":
                noMic = true
            case "--no-system-watch":
                // Mic-only (in person): the system track is still written, but nobody is
                // on it, so its silence means nothing. Skip the watcher outright — its
                // probe speaks "System audio check" out loud, which in a conference room
                // on laptop speakers is heard by everyone at the table.
                noSystemWatch = true
            case "--duration":
                duration = Double(a.first ?? "0") ?? 0; if !a.isEmpty { a.removeFirst() }
            case "-h", "--help":
                emit("usage: mtcapture --out-dir DIR [--mic-id UID] [--no-mic] [--no-system-watch] [--duration SEC]")
                emit("       mtcapture --list-mics")
                exit(0)
            default:
                die("unknown argument: \(arg)")
            }
        }

        guard let outPath = outDir else { die("--out-dir is required") }
        let dir = URL(fileURLWithPath: outPath, isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        if #unavailable(macOS 15.0) {
            if !noMic {
                die("microphone capture via ScreenCaptureKit needs macOS 15+. Re-run with --no-mic.")
            }
        }

        // Screen Recording permission is required even for audio-only capture.
        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: false)
        } catch {
            die("""
                cannot reach ScreenCaptureKit: \(error.localizedDescription)
                Grant Screen & System Audio Recording to your terminal in
                System Settings > Privacy & Security > Screen & System Audio Recording,
                then fully quit and reopen the terminal.
                """)
        }
        guard let display = content.displays.first else { die("no display available to attach to") }

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = 48_000
        cfg.channelCount = 2
        cfg.excludesCurrentProcessAudio = true
        // audio-only: keep the video path as cheap as legally possible
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        cfg.queueDepth = 6
        var micName = "none"
        var micUID = "none"
        if !noMic {
            var chosen: AVCaptureDevice?
            if let want = micID {
                chosen = findMic(want)
                if chosen == nil {
                    die("""
                        no microphone matches \(want).
                        Available:
                        """ + allMics().map { "  \($0.localizedName)" }.joined(separator: "\n"))
                }
            } else {
                chosen = defaultMic()
            }
            if let c = chosen {
                micName = c.localizedName
                micUID = c.uniqueID
                if #available(macOS 15.0, *) { cfg.microphoneCaptureDeviceID = c.uniqueID }
            } else {
                log("WARNING: no microphone found; recording system audio only")
            }
        }
        if #available(macOS 15.0, *) { cfg.captureMicrophone = !noMic }
        emit("MIC \(micName)")
        if !noMic { log("microphone: \(micName)") }

        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let sink = Sink(dir: dir)
        let stream = SCStream(filter: filter, configuration: cfg, delegate: sink)

        do {
            try stream.addStreamOutput(sink, type: .audio,
                                       sampleHandlerQueue: DispatchQueue(label: "mt.sys"))
            if #available(macOS 15.0, *), !noMic {
                try stream.addStreamOutput(sink, type: .microphone,
                                           sampleHandlerQueue: DispatchQueue(label: "mt.mic"))
            }
        } catch {
            die("could not attach audio outputs: \(error.localizedDescription)")
        }

        let started = Date()
        let events = EventLog(dir: dir, started: started)
        // Must happen before startCapture: the sink only keeps buffers from the stream it
        // has adopted, which is what makes a later make-before-break recapture gapless.
        sink.adopt(stream)
        do {
            try await stream.startCapture()
        } catch {
            die("""
                startCapture failed: \(error.localizedDescription)
                This is almost always a missing permission. Check both:
                  Privacy & Security > Screen & System Audio Recording
                  Privacy & Security > Microphone
                """)
        }

        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime]
        let meta: [String: Any] = [
            "started_at": iso.string(from: started),
            "sample_rate": 48_000,
            "mic_enabled": !noMic,
            "mic_name": micName,
            "mic_uid": micUID,
            "mic_requested": micID ?? "system default",
            "tool": "mtcapture/1.0",
        ]
        if let d = try? JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted]) {
            try? d.write(to: dir.appendingPathComponent("capture.json"))
        }

        emit("RECORDING \(dir.path)")
        log("recording — press Ctrl-C (or send SIGINT/SIGTERM) to stop")
        log("manual recapture at any time:  kill -USR1 \(ProcessInfo.processInfo.processIdentifier)")

        // graceful shutdown, with a hard deadline behind it
        let gate = Gate()
        let deadline = ShutdownDeadline(sink: sink, events: events, started: started)
        // Every stop goes through here, so every stop — signal, --duration, or the
        // stream declaring itself dead — starts the exit clock. The Sep 17 wedge began
        // with `onStop`, minutes before the user pressed anything.
        let stop: @Sendable (String) -> Void = { reason in
            deadline.arm(reason: reason)
            gate.open()
        }
        sink.onStop = { _ in stop("stream_stopped") }

        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let s1 = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
        let s2 = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
        s1.setEventHandler { stop("SIGINT") }
        s2.setEventHandler { stop("SIGTERM") }
        s1.resume(); s2.resume()

        // Rebuild the stream on demand. The user asked for a button that is always available
        // even when nothing is wrong — it is also this feature's own test harness.
        let recap = Recapturer(sink: sink, cfg: cfg, filter: filter, noMic: noMic, events: events)
        signal(SIGUSR1, SIG_IGN)
        let s3 = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .global())
        s3.setEventHandler {
            log("SIGUSR1 — manual recapture requested")
            recap.request(reason: "manual")
        }
        s3.resume()

        // Watch for the mic being unplugged. We cannot fix it mid-stream, but silently
        // recording nothing is unacceptable — so warn loudly and note it in mic.jsonl.
        var watcher: MicWatcher? = nil
        if !noMic, micUID != "none" {
            watcher = MicWatcher(dir: dir, started: started, uid: micUID, name: micName)
        }
        let sysWatch = SystemWatcher(sys: sink.sys, mic: noMic ? nil : sink.mic,
                                     dir: dir, started: started, step: 3, events: events,
                                     onDead: { recap.request(reason: "auto") })
        let micTimer = DispatchSource.makeTimerSource(queue: .global())
        micTimer.schedule(deadline: .now() + 3, repeating: 3)
        micTimer.setEventHandler { watcher?.poll(); if !noSystemWatch { sysWatch.poll() } }
        micTimer.resume()

        // progress heartbeat
        let hb = DispatchSource.makeTimerSource(queue: .global())
        hb.schedule(deadline: .now() + 5, repeating: 5)
        hb.setEventHandler {
            let e = Int(Date().timeIntervalSince(started))
            log(String(format: "%02d:%02d  system %.0fs (peak %.3f) | mic %.0fs (peak %.3f)",
                       e / 60, e % 60,
                       sink.sys.seconds, sink.sys.peak,
                       sink.mic.seconds, sink.mic.peak))
        }
        hb.resume()

        if duration > 0 {
            DispatchQueue.global().asyncAfter(deadline: .now() + duration) { stop("duration") }
        }

        await gate.wait()
        micTimer.cancel()
        hb.cancel()
        s3.cancel()
        log("stopping…")
        // Not necessarily the stream we started with — a recapture may have replaced it.
        // Bounded: on a healthy stream this returns in milliseconds; on a stream whose
        // daemon connection has been interrupted it may never return at all, and the
        // 1.5 s is all the patience it gets. Either way the writers are closed next.
        if let live = sink.currentStream() {
            await withDeadline(1.5) { try? await live.stopCapture() }
        }
        // let in-flight buffers land
        try? await Task.sleep(nanoseconds: 400_000_000)
        sink.sys.close()
        sink.mic.close()

        let elapsed = Date().timeIntervalSince(started)
        let sysSec = sink.sys.seconds
        let micSec = sink.mic.seconds
        log(String(format: "done — %.1fs wall | system %.1fs | mic %.1fs", elapsed, sysSec, micSec))
        if sysWatch.tapConfirmedDead {
            log("NOTE: the system audio tap was PROVEN dead during this recording — a spoken "
                + "cue played on this machine never reached the capture. See "
                + "capture_events.jsonl; the far side is missing from that stretch.")
        } else if sysWatch.wentDead {
            log("note: there was a long silent stretch on the system track, but the probe "
                + "cue was captured every time, so the tap was working — nobody was talking.")
        }
        if recap.automaticAttempts > 0 {
            log("note: \(recap.automaticAttempts) automatic recapture(s) were made this run.")
        }
        if watcher?.lostDevice == true {
            log("NOTE: '\(micName)' was unplugged mid-recording — check mic.jsonl and your own audio")
        }
        if sink.sys.peak < 0.0005 { log("WARNING: system track is silent — was any audio actually playing?") }
        if !noMic && sink.mic.peak < 0.0005 {
            log("WARNING: mic track is digitally silent — check Microphone permission and the mic's mute switch.")
        } else if !noMic && sink.mic.peak < 0.02 {
            log(String(format: "note: mic peaked at only %.3f — live but very quiet (nobody speaking, or gain is low)", sink.mic.peak))
        }
        emit("STOPPED \(Int(elapsed))")
        exit(0)
    }
}

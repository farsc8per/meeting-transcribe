// mtaudio — list audio devices and set the macOS default OUTPUT (the speaker).
//
// Kept apart from mtcapture on purpose: the recorder is the one binary that must never
// break, and nothing here needs to be inside it. ScreenCaptureKit records what apps play
// regardless of which device they play through, so changing the speaker before a
// capture starts has no effect on the system track. mt only ever calls --set-output
// BEFORE starting mtcapture, never during a recording.
//
//   mtaudio --list                 uid \t name \t in|out \t default? \t transport
//                                  (one line per direction; transport is CoreAudio's
//                                  four-char code: bltn built-in, virt virtual, usb, blue …)
//   mtaudio --set-output UID       make UID the default output (and system-sounds output)
import CoreAudio
import Foundation

func emit(_ s: String) { print(s); fflush(stdout) }
func die(_ s: String) -> Never { FileHandle.standardError.write(("mtaudio: " + s + "\n").data(using: .utf8)!); exit(1) }

func addr(_ sel: AudioObjectPropertySelector,
          _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func allDevices() -> [AudioDeviceID] {
    var a = addr(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func string(_ id: AudioDeviceID, _ sel: AudioObjectPropertySelector) -> String? {
    var a = addr(sel)
    var v: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, &v) == noErr, let s = v else { return nil }
    return s.takeRetainedValue() as String
}

func channels(_ id: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var a = addr(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, raw) == noErr else { return 0 }
    let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return list.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func transport(_ id: AudioDeviceID) -> String {
    var a = addr(kAudioDevicePropertyTransportType)
    var v: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, &v) == noErr, v != 0 else { return "" }
    let bytes = [24, 16, 8, 0].map { UInt8((v >> UInt32($0)) & 0xff) }
    return String(decoding: bytes, as: UTF8.self).trimmingCharacters(in: .whitespaces)
}

func defaultDevice(_ sel: AudioObjectPropertySelector) -> AudioDeviceID {
    var a = addr(sel)
    var id = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &id)
    return id
}

func setDefault(_ sel: AudioObjectPropertySelector, _ id: AudioDeviceID) -> OSStatus {
    var a = addr(sel)
    var v = id
    return AudioObjectSetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil,
                                      UInt32(MemoryLayout<AudioDeviceID>.size), &v)
}

let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "--list":
    let defIn = defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
    let defOut = defaultDevice(kAudioHardwarePropertyDefaultOutputDevice)
    for id in allDevices() {
        guard let uid = string(id, kAudioDevicePropertyDeviceUID),
              let name = string(id, kAudioObjectPropertyName) else { continue }
        let t = transport(id)
        if channels(id, kAudioObjectPropertyScopeInput) > 0 {
            emit("\(uid)\t\(name)\tin\t\(id == defIn ? "default" : "")\t\(t)")
        }
        if channels(id, kAudioObjectPropertyScopeOutput) > 0 {
            emit("\(uid)\t\(name)\tout\t\(id == defOut ? "default" : "")\t\(t)")
        }
    }
case "--set-output":
    guard args.count > 1 else { die("--set-output needs a device UID") }
    guard let id = allDevices().first(where: { string($0, kAudioDevicePropertyDeviceUID) == args[1] }),
          channels(id, kAudioObjectPropertyScopeOutput) > 0 else {
        die("no output device with UID \(args[1]) is connected")
    }
    if defaultDevice(kAudioHardwarePropertyDefaultOutputDevice) == id { emit("unchanged"); exit(0) }
    let st = setDefault(kAudioHardwarePropertyDefaultOutputDevice, id)
    guard st == noErr else { die("CoreAudio refused the change (OSStatus \(st))") }
    // Alerts and notification sounds follow too, or they keep coming out of the old speaker.
    _ = setDefault(kAudioHardwarePropertyDefaultSystemOutputDevice, id)
    emit("set")
default:
    emit("usage: mtaudio --list")
    emit("       mtaudio --set-output UID")
    exit(args.isEmpty ? 0 : 1)
}

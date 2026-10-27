#!/usr/bin/env bash
# Build/repair the mt toolchain. Safe to re-run: anything already present and correct is skipped.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

die() { echo "make.sh: $*" >&2; exit 1; }

echo "==> preflight"
[ "$(uname -s)" = "Darwin" ] || die "mt is macOS-only (it records through ScreenCaptureKit)."
command -v brew    >/dev/null || die "Homebrew is required: https://brew.sh"
command -v swiftc  >/dev/null || die "Swift compiler missing. Run:  xcode-select --install"
command -v python3 >/dev/null || die "python3 missing. Run:  brew install python"

echo "==> homebrew deps"
for t in ffmpeg whisper-cpp; do
  brew list "$t" >/dev/null 2>&1 || { echo "    installing $t"; brew install "$t"; }
done

echo "==> python env"
[ -x lib/venv/bin/python ] || python3 -m venv lib/venv
./lib/venv/bin/pip install --quiet --upgrade pip
./lib/venv/bin/pip install --quiet -r requirements.txt

echo "==> compiling mtcapture"
mkdir -p build
swiftc -O -swift-version 5 -parse-as-library \
  -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia \
  -o build/mtcapture src/mtcapture.swift
echo "==> compiling mtwatch"
swiftc -O -swift-version 5 -parse-as-library \
  -framework ScreenCaptureKit -framework Vision -framework CoreImage -framework AppKit \
  -o build/mtwatch src/mtwatch.swift
echo "==> compiling mtaudio"
# Lists speakers and sets the default output before a recording (audioprefs.py).
swiftc -O -swift-version 5 -framework CoreAudio -o build/mtaudio src/mtaudio.swift
echo "==> compiling mtcal"
# The Info.plist is linked into __TEXT,__info_plist so macOS has a usage string to show
# in the Calendars permission prompt; without it the request is rejected outright.
swiftc -O -swift-version 5 \
  -framework EventKit -framework Foundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker src/mtcal.plist \
  -o build/mtcal src/mtcal.swift

echo "==> models (~900 MB download the first time)"
mkdir -p models && cd models
get() { [ -f "$2" ] || { echo "    downloading $2"; curl -L --fail --progress-bar -o "$2.part" "$1" && mv "$2.part" "$2"; }; }
get https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin ggml-large-v3-turbo-q5_0.bin
# live transcription runs on the small model (config "live_model")
get https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q5_1.bin ggml-small.en-q5_1.bin
get https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx nemo_en_titanet_small.onnx
if [ ! -f sherpa-onnx-pyannote-segmentation-3-0/model.onnx ]; then
  get https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
  tar xjf sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 && rm -f sherpa-onnx-pyannote-segmentation-3-0.tar.bz2
fi
# Laughter detection for the "Best Saying" award (vocal cues, step 4): AudioSet sound
# tagging, Apache-2.0. Only the 26 MB int8 model and the label list are kept — the 92 MB
# full-precision model and the test clips in the archive are deleted.
TAG=sherpa-onnx-zipformer-small-audio-tagging-2024-04-15
if [ ! -f "$TAG/model.int8.onnx" ]; then
  get "https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/$TAG.tar.bz2" "$TAG.tar.bz2"
  tar xjf "$TAG.tar.bz2" && rm -f "$TAG.tar.bz2" "$TAG/model.onnx" && rm -rf "$TAG/test_wavs"
fi

# Every install must have byte-identical models, or transcripts and voiceprints stop being
# comparable between people. A mismatch is deleted so the next run re-downloads it.
echo "==> verifying models"
bad=0
while read -r sum file; do
  [ -n "$sum" ] || continue
  if [ "$(shasum -a 256 "$file" | cut -d' ' -f1)" != "$sum" ]; then
    echo "    CHECKSUM MISMATCH: $file (deleted; re-run make.sh)" >&2
    rm -f "$file"; bad=1
  fi
done < "$HERE/models.sha256"
[ "$bad" = 0 ] || die "model verification failed"
cd "$HERE"

echo "==> workspace + database"
./mt init

echo
echo "==> done (mt $(cat VERSION)). Now run:  $HERE/mt doctor"

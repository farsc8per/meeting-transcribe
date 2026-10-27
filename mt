#!/usr/bin/env bash
# mt — local meeting transcription with speaker ID. See README.md
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/lib/venv/bin/python"
[ -x "$PY" ] || { echo "mt: python env missing — run $HERE/make.sh" >&2; exit 1; }
exec "$PY" "$HERE/mt.py" "$@"

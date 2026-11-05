#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${FCODE_BUILD_VENV:-$ROOT/work/desktop-build-venv}"
OUT="${FCODE_DESKTOP_OUT:-$ROOT/work/desktop}"
PYTHON="${PYTHON:-python3}"

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -r "$ROOT/desktop/requirements-build.txt"
"$VENV/bin/python" -m PyInstaller \
  --noconfirm --clean \
  --distpath "$OUT/dist" \
  --workpath "$OUT/build" \
  "$ROOT/desktop/fcode.spec"

if [[ "$OSTYPE" == darwin* ]]; then
  APP="$OUT/dist/FCode.app/Contents/MacOS/FCode"
else
  APP="$OUT/dist/FCode/FCode"
fi
FCODE_DATA_DIR="$OUT/smoke-data" "$APP" --smoke-test
printf 'Desktop bundle verified: %s\n' "$OUT/dist"

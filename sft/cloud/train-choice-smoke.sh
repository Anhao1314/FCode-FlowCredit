#!/usr/bin/env bash
# Choice-head smoke run: 200 packets, 1 epoch. Run before the full choice run.
set -euo pipefail
ROOT=/root/fcode-sft
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
mkdir -p "$ROOT/saves"
nohup llamafactory-cli train "$ROOT/cloud/qwen3_choice_smoke.yaml" \
  > "$ROOT/choice-smoke.log" 2>&1 &
echo "started choice smoke -> $ROOT/choice-smoke.log (pid $!)"

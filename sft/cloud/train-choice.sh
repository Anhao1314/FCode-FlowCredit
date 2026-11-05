#!/usr/bin/env bash
# Choice-head full run: 5.2k packets, 2 epochs, fresh LoRA.
# Do not start this before train-choice-smoke.sh has produced a usable adapter.
set -euo pipefail
ROOT=/root/fcode-sft
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
mkdir -p "$ROOT/saves"
nohup llamafactory-cli train "$ROOT/cloud/qwen3_choice_lora.yaml" \
  > "$ROOT/choice-full.log" 2>&1 &
echo "started choice full -> $ROOT/choice-full.log (pid $!)"

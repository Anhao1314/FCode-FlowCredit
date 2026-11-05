#!/usr/bin/env bash
# Full SFT: ~2346 samples, 2 epochs, 2.5-4 h on T4. Only run after the smoke passed.
set -euo pipefail
ROOT=/root/fcode-sft
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
cd "$ROOT/cloud"
echo "==> full train (dataset: train_full.jsonl)"
llamafactory-cli train qwen3_lora_full.yaml
echo
echo "adapter -> $ROOT/saves/full"
ls -la "$ROOT/saves/full" | head

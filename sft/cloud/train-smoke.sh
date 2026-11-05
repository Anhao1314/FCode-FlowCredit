#!/usr/bin/env bash
# Smoke SFT: ~200 samples, 1 epoch, ~15 min on T4. Run this first, always.
set -euo pipefail
ROOT=/root/fcode-sft
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
cd "$ROOT/cloud"
echo "==> smoke train (dataset: train_smoke.jsonl)"
llamafactory-cli train qwen3_lora_smoke.yaml
echo
echo "adapter -> $ROOT/saves/smoke"
echo "check: tail the loss lines above (must trend down) and the adapter dir exists:"
ls -la "$ROOT/saves/smoke" | head

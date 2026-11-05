#!/usr/bin/env bash
# FCode SFT environment — Alibaba Cloud gn6i (Tesla T4 16GB), Ubuntu 22.04.
# The GPU image ships the NVIDIA driver; everything below is userspace.
# Usage:  bash setup.sh
set -euo pipefail

ROOT=/root/fcode-sft
MODEL_ID="${MODEL_ID:-Qwen/Qwen3-4B-Instruct-2507}"
MIRROR=https://mirrors.aliyun.com/pypi/simple/

echo "==> [1/4] system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl build-essential cmake

echo "==> [2/4] python environment"
python3 -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install -q -U pip -i "$MIRROR"
# torch comes from the PyTorch wheel mirror (~25 MB/s, bundled CUDA 12.1);
# the PyPI mirror's CDN is slow for this single 2.4 GB file.
pip install -q -i "$MIRROR" \
  "https://mirrors.aliyun.com/pytorch-wheels/cu121/torch-2.5.1%2Bcu121-cp310-cp310-linux_x86_64.whl"
pip install -q -i "$MIRROR" "llamafactory>=0.9.2" modelscope bitsandbytes

echo "==> [3/4] model weights (ModelScope, in-region)"
mkdir -p "$ROOT/models"
if [ ! -d "$ROOT/models/Qwen3-4B-Instruct-2507" ]; then
  modelscope download --model "$MODEL_ID" --local_dir "$ROOT/models/Qwen3-4B-Instruct-2507"
else
  echo "    weights already present"
fi

echo "==> [4/4] sanity checks"
python - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.cuda.is_available())
assert torch.cuda.is_available(), "CUDA not visible to torch — check nvidia-smi / driver"
print("gpu", torch.cuda.get_device_name(0), torch.cuda.get_device_properties(0).total_memory // (1024**3), "GiB")
PY
llamafactory-cli version || true

echo
echo "setup done. Next:"
echo "  bash train-smoke.sh     # ~15 min, validates the whole pipeline"
echo "  bash train-full.sh      # ~3 h on T4"

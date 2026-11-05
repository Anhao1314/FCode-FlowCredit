#!/usr/bin/env bash
# Merge the QLoRA adapter into its 4-bit base (matching training numerics),
# then export a Q4_K_M GGUF for Ollama.
# ~10-20 min on T4. Output: /root/fcode-sft/out/fcode-qwen3-4b-Q4_K_M.gguf (~2.5 GB)
set -euo pipefail
ROOT=/root/fcode-sft
# Which adapter to merge and what to name the outputs. Override per model:
#   ADAPTER_DIR=$ROOT/saves/choice-full MODEL_NAME=fcode-qwen3-4b-policy \
#     bash cloud/merge-and-export.sh
ADAPTER_DIR="${ADAPTER_DIR:-$ROOT/saves/full}"
MODEL_NAME="${MODEL_NAME:-fcode-qwen3-4b}"
MERGED="$ROOT/out/${MODEL_NAME}-merged"
F16_GGUF="$ROOT/out/${MODEL_NAME}-f16.gguf"
Q4_GGUF="$ROOT/out/${MODEL_NAME}-Q4_K_M.gguf"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
mkdir -p "$ROOT/out" "$ROOT/llama.cpp"

echo "==> [1/3] merge LoRA -> fp16 (from the 4-bit training base)"
if [ -f "$MERGED/config.json" ] && [ -f "$MERGED/model.safetensors.index.json" ]; then
  echo "    merged checkpoint exists, skipping"
else
  python "$ROOT/cloud/merge_4bit.py" \
    --base "$ROOT/models/Qwen3-4B-Instruct-2507" \
    --adapter "$ADAPTER_DIR" \
    --out "$MERGED"
fi

echo "==> [2/3] fetch llama.cpp converter"
if [ ! -f "$ROOT/llama.cpp/convert_hf_to_gguf.py" ]; then
  # github.com is unreliable from this network; use a GitHub mirror with a
  # second mirror as fallback.
  rm -rf "$ROOT/llama.cpp"
  mkdir -p "$ROOT/llama.cpp"
  URL="https://gh-proxy.com/https://github.com/ggml-org/llama.cpp/archive/refs/heads/master.tar.gz"
  curl -L --fail --retry 3 --retry-delay 2 -o "$ROOT/llama.cpp.tar.gz" "$URL" \
    || curl -L --fail --retry 3 --retry-delay 2 -o "$ROOT/llama.cpp.tar.gz" \
      "https://ghproxy.net/https://github.com/ggml-org/llama.cpp/archive/refs/heads/master.tar.gz"
  tar xzf "$ROOT/llama.cpp.tar.gz" -C "$ROOT/llama.cpp" --strip-components=1
fi
# The converter's requirements pin torch==2.11 (CPU). Installing them into the
# training venv replaces the cu121 build and breaks the next training run. Keep
# the converter in its own venv that inherits the training packages but cannot
# shadow them.
CONVERT_VENV="$ROOT/gguf-venv"
if [ ! -x "$CONVERT_VENV/bin/python" ]; then
  python3 -m venv --system-site-packages "$CONVERT_VENV"
fi
if ! "$CONVERT_VENV/bin/python" -c "import gguf" >/dev/null 2>&1; then
  "$CONVERT_VENV/bin/pip" install -q -i https://mirrors.aliyun.com/pypi/simple/ \
    gguf sentencepiece protobuf
fi

echo "==> [3/3] convert + quantize"
"$CONVERT_VENV/bin/python" "$ROOT/llama.cpp/convert_hf_to_gguf.py" "$MERGED" \
  --outfile "$F16_GGUF" --outtype f16
if [ ! -x "$ROOT/llama.cpp/build/bin/llama-quantize" ]; then
  echo "    building llama-quantize"
  cmake -S "$ROOT/llama.cpp" -B "$ROOT/llama.cpp/build" -DGGML_CUDA=OFF -DLLAMA_CURL=OFF >/dev/null
  cmake --build "$ROOT/llama.cpp/build" --target llama-quantize -j"$(nproc)" >/dev/null
fi
"$ROOT/llama.cpp/build/bin/llama-quantize" \
  "$F16_GGUF" "$Q4_GGUF" Q4_K_M

cat > "$ROOT/out/Modelfile-$MODEL_NAME" <<EOF
FROM ./$(basename "$Q4_GGUF")
PARAMETER temperature 0
PARAMETER num_ctx 4096
EOF

echo
echo "done -> $ROOT/out/"
ls -lh "$Q4_GGUF" "$ROOT/out/Modelfile-$MODEL_NAME" 2>/dev/null || true
echo
echo "download to the Mac:"
echo "  scp root@<ip>:$Q4_GGUF $ROOT/out/Modelfile-$MODEL_NAME ."
echo "  ollama create $MODEL_NAME -f Modelfile-$MODEL_NAME"

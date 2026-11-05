#!/usr/bin/env python3
"""Merge a QLoRA adapter into its 4-bit base, matching training numerics.

Merging a QLoRA adapter into the original fp16 weights changes the first-token
distribution enough to make the model emit special-token loops. The adapter was
trained against the 4-bit (NF4, double-quant) base, so merge there and let PEFT
dequantize while applying the delta. The result is a normal fp16 checkpoint
that can be converted to GGUF.

Usage (on the training host, inside the venv):
    python merge_4bit.py \
      --base /root/fcode-sft/models/Qwen3-4B-Instruct-2507 \
      --adapter /root/fcode-sft/saves/full \
      --out /root/fcode-sft/out/merged
"""

from __future__ import annotations

import argparse
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge a QLoRA adapter into its 4-bit base")
    parser.add_argument("--base", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tokenizer = AutoTokenizer.from_pretrained(args.base, trust_remote_code=True)
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.base,
        quantization_config=quantization,
        device_map=args.device,
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(model, args.adapter)
    merged = model.merge_and_unload()
    # merge_and_unload keeps the bnb-packed weights; dequantize to a normal
    # fp16 checkpoint that llama.cpp can convert.
    merged = merged.dequantize()
    merged.save_pretrained(out, safe_serialization=True)
    tokenizer.save_pretrained(out)
    print(f"merged -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

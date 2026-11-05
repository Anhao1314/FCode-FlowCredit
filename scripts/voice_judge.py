#!/usr/bin/env python3
"""Blind voice judge backed by the local Claude CLI.

This is a development tool, not a runtime dependency. It sends synthetic/demo
cases to Claude for scoring against the FCode voice rubric; it never feeds
Claude's prose into the SFT corpus. The goal is to calibrate our own prompt,
golden examples and adapter, not to imitate a product identity.

Input JSONL (one case per line):
    {"id": "case-1", "surface": "judgment", "evidence": "...", "text": "..."}

Output JSONL:
    {"id": ..., "score": 1-5, "issues": [...], ...,
     "cost_usd": 0.006, "model": "..."}

Usage:
    python3 voice_judge.py --input cases.jsonl --out report.jsonl
    python3 voice_judge.py --input cases.jsonl --out report.jsonl --limit 20

Only run it on synthetic or explicitly authorized text: the case leaves the
machine and is sent to Anthropic.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

SYSTEM = """你是 FCode 的盲评评审，不是写手。
只评估给定的候选文本，不补写事实、不猜测缺失证据。
按下面的标准打分，只输出符合 schema 的 JSON：
1. 结论先行：第一句回答用户的问题；
2. 数字可追溯：文本中的金额/比例/期间都能在 evidence 中找到，否则扣分；
3. 校准不确定性：明确区分已核实、推断、未知，不滥用“可能”；
4. 具体：失败或阻塞时说明缺什么、下一步做什么；
5. 语气：平静、直接、不奉承、不夸张、不用 emoji/感叹号；
6. 身份：不冒充 Claude 或其他产品，不写“作为一个 AI”。"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "conclusion_first": {"type": "boolean"},
        "numbers_traceable": {"type": "boolean"},
        "calibrated_uncertainty": {"type": "boolean"},
        "specific_next_step": {"type": "boolean"},
        "identity_safe": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "rewrite_hint": {"type": "string"},
    },
    "required": [
        "score",
        "conclusion_first",
        "numbers_traceable",
        "calibrated_uncertainty",
        "specific_next_step",
        "identity_safe",
        "issues",
        "rewrite_hint",
    ],
    "additionalProperties": False,
}


def judge(case: dict[str, Any], *, model: str = "", timeout: float = 120.0) -> dict[str, Any]:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude CLI not found on PATH")
    prompt = (
        "评审下面这条 FCode 候选文本。surface 表示使用场景。\n"
        + json.dumps(case, ensure_ascii=False)
    )
    cmd = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA, ensure_ascii=False),
        "--system-prompt",
        SYSTEM,
        "--tools",
        "",
        "--max-turns",
        "1",
    ]
    if model:
        cmd.extend(["--model", model])
    completed = subprocess.run(
        cmd,
        cwd=tempfile.gettempdir(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude exited {completed.returncode}: {completed.stderr.strip()[:300]}"
        )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"claude returned non-JSON: {completed.stdout[:300]}") from error
    verdict = envelope.get("structured_output")
    if not isinstance(verdict, dict):
        raw = envelope.get("result")
        if isinstance(raw, str):
            try:
                verdict = json.loads(raw)
            except json.JSONDecodeError:
                verdict = None
    if not isinstance(verdict, dict):
        raise RuntimeError("claude returned no structured_output")
    model_usage = envelope.get("modelUsage") or {}
    return {
        "id": case.get("id", ""),
        "surface": case.get("surface", ""),
        **verdict,
        "cost_usd": envelope.get("total_cost_usd", 0.0),
        "model": next(iter(model_usage), ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice-judge")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    cases = []
    with Path(args.input).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                cases.append(json.loads(line))
    if args.limit:
        cases = cases[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    total_cost = 0.0
    passed = 0
    with out.open("w", encoding="utf-8") as handle:
        for case in cases:
            try:
                result = judge(case, model=args.model, timeout=args.timeout)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                result = {"id": case.get("id", ""), "error": str(error), "score": 0}
            total_cost += float(result.get("cost_usd") or 0.0)
            if int(result.get("score") or 0) >= 4:
                passed += 1
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(json.dumps(result, ensure_ascii=False))
    print(
        json.dumps(
            {
                "cases": len(cases),
                "score_4_plus": passed,
                "total_cost_usd": round(total_cost, 4),
                "out": str(out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Benchmark the prompt-cache prefix produced by context ordering.

This does not call a provider. It measures the observable input to a provider
cache: the longest common prefix between consecutive rendered user prompts.
It compares the legacy sorted-key JSON rendering against the cache-friendly
ordering now used by non-frozen general profiles.

用法:
    python3 scripts/bench_prompt_prefix.py --rounds 20

输出:
    work/cache-benchmark/prompt-prefix-report.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flowagent.memory.context import (  # noqa: E402
    WorkingMemory, _cache_ordered_context,
)
from flowagent.providers.openai_compat import _prompt_prefix_stats  # noqa: E402
from flowagent.providers.prompt import canonical, ordered_json  # noqa: E402


def _render(context: dict[str, Any], mode: str) -> str:
    body = canonical(context) if mode == "sorted" else ordered_json(
        _cache_ordered_context(context)
    )
    return '{"context": ' + body + ', "prompt_version": "bench"}'


def _lcp_ratio(previous: str, current: str) -> float:
    return _prompt_prefix_stats(previous, current)["prompt_prefix_bp"] / 10_000


def run(mode: str, rounds: int) -> dict[str, Any]:
    memory = WorkingMemory(
        objective="fix pricing",
        tool_catalog=[{"name": "read_file"}, {"name": "run_command"}],
        prompt_profile="general",
        instruction="general instruction",
    )
    previous = ""
    ratios: list[float] = []
    chars: list[int] = []
    for round_no in range(1, rounds + 1):
        if round_no > 1:
            memory.record(round_no - 1, type("O", (), {
                "tool": "read_file",
                "arguments": {"path": f"src/file_{round_no}.py"},
                "result": {"content": "x" * 200, "sha256": "a" * 64},
                "result_hash": f"{round_no:064d}",
            })(), event_kind="tool_succeeded")
        context = memory.build(round_no, {"calls": f"{round_no}/40"})
        current = _render(context, mode)
        stats = _prompt_prefix_stats(previous, current)
        ratios.append(stats["prompt_prefix_bp"] / 10_000)
        chars.append(stats["prompt_prefix_chars"])
        previous = current
    measured = ratios[1:] or [0.0]
    return {
        "mode": mode,
        "rounds": rounds,
        "avg_prefix_ratio": round(statistics.mean(measured), 4),
        "min_prefix_ratio": round(min(measured), 4),
        "last_prefix_ratio": round(measured[-1], 4),
        "avg_prefix_chars": round(statistics.mean(chars[1:]), 1),
        "ratios": [round(value, 4) for value in ratios],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    rounds = max(2, min(args.rounds, 200))
    report = {
        "rounds": rounds,
        "sorted": run("sorted", rounds),
        "cache_ordered": run("cache_ordered", rounds),
    }
    sorted_avg = report["sorted"]["avg_prefix_ratio"]
    ordered_avg = report["cache_ordered"]["avg_prefix_ratio"]
    report["improvement_ratio_x"] = round(
        ordered_avg / sorted_avg, 2
    ) if sorted_avg else None

    out = ROOT / "work" / "cache-benchmark"
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt-prefix-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({
        "sorted_avg_prefix_ratio": sorted_avg,
        "cache_ordered_avg_prefix_ratio": ordered_avg,
        "improvement_ratio_x": report["improvement_ratio_x"],
        "report": str(out / "prompt-prefix-report.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
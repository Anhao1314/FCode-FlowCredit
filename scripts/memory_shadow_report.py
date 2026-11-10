#!/usr/bin/env python3
"""Memory policy shadow report: turn recorded decisions into promotion evidence.

Reads a run's ``trace.json``, pairs every ``memory_decision`` event with the
deterministic fold intent recorded at decision time, and checks later tool
calls for a re-read of the same tool+arguments. That turns a disagreement into
one of four verdicts:

- ``decision_right``     head kept something the fold hid and it was re-read,
                          or dropped something that never came back;
- ``decision_wrong``     head dropped something that was re-read afterwards;
- ``decision_conservative`` head kept something that never came back;
- ``unverified``         no observation body was available to check.

The baseline section answers the same question without the head: of the
observations the heuristic had already folded, how many were read again later?
That number is the pain the policy head is supposed to reduce.

Usage:
    python3 scripts/memory_shadow_report.py --run work/runs/run-xxx
    python3 scripts/memory_shadow_report.py --trace path/to/trace.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _detail(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return payload.get("detail") or {}


def load_events(path: pathlib.Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("events") or [])
    if isinstance(data, list):
        return data
    raise ValueError(f"{path}: not a trace object")


def _signature(tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
    return (str(tool or ""), _canonical(arguments or {}))


def build_report(events: list[dict[str, Any]], trace_path: pathlib.Path) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    compacted: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        detail = _detail(event)
        sequence = int(event.get("sequence") or 0)
        if kind == "tool_succeeded":
            observations.append({
                "seq": sequence,
                "round": int(detail.get("round") or 0),
                "tool": str(detail.get("tool") or ""),
                "arguments": detail.get("arguments") or {},
                "result_hash": str(detail.get("result_hash") or ""),
            })
        elif kind == "memory_decision":
            decisions.append({"seq": sequence, **detail})
        elif kind == "context_compacted":
            compacted.append({"seq": sequence, **detail})

    by_id: dict[str, dict[str, Any]] = {}
    for observation in observations:
        by_id.setdefault(observation["result_hash"][:12], observation)

    calls = shadow = fallback = skipped = invalid = 0
    cost_microusd = tokens = 0
    agreement = {"compared": 0, "agree": 0, "disagree": 0, "unknown": 0}
    verdicts = {
        "decision_right": 0, "decision_wrong": 0,
        "decision_conservative": 0, "unverified": 0,
    }
    rows: list[dict[str, Any]] = []

    for decision in decisions:
        mode = str(decision.get("mode") or "")
        if mode == "skipped":
            skipped += 1
            continue
        calls += 1
        if mode == "fallback":
            fallback += 1
        elif mode == "shadow":
            shadow += 1
        cost = decision.get("cost") or {}
        cost_microusd += int(cost.get("cost_microusd") or 0)
        tokens += int(cost.get("usage_total_tokens") or 0)
        payload_decision = decision.get("decision") or {}
        if payload_decision.get("error"):
            invalid += 1
        comparison = decision.get("comparison") or {}
        for key in agreement:
            agreement[key] += int(comparison.get(key) or 0)

        for disagreement in comparison.get("disagreements") or []:
            target = by_id.get(str(disagreement.get("id") or ""))
            later: list[dict[str, Any]] = []
            if target is not None:
                signature = _signature(target["tool"], target["arguments"])
                later = [
                    item for item in observations
                    if item["seq"] > int(decision.get("seq") or 0)
                    and _signature(item["tool"], item["arguments"]) == signature
                ]
            action = str(disagreement.get("action") or "")
            if target is None:
                verdict = "unverified"
            elif action in ("keep", "pin"):
                verdict = "decision_right" if later else "decision_conservative"
            else:  # drop
                verdict = "decision_wrong" if later else "decision_right"
            verdicts[verdict] += 1
            rows.append({
                "decision_round": int(decision.get("round") or 0),
                "id": disagreement.get("id"),
                "tool": target["tool"] if target else "",
                "arguments": target["arguments"] if target else {},
                "action": action,
                "desired": disagreement.get("desired"),
                "heuristic": disagreement.get("heuristic"),
                "confidence_bp": int(disagreement.get("confidence_bp") or 0),
                "reason": disagreement.get("reason") or "",
                "later_reread": bool(later),
                "later_rounds": [item["round"] for item in later[:5]],
                "verdict": verdict,
            })

    # Baseline: observations the deterministic fold had already hidden, then
    # the same tool+arguments was read again afterwards. No model involved.
    folded = 0
    folded_reread = 0
    for event in compacted:
        through = int(event.get("through_round") or 0)
        for observation in observations:
            if observation["round"] > through:
                continue
            folded += 1
            signature = _signature(observation["tool"], observation["arguments"])
            if any(
                item["seq"] > int(event.get("seq") or 0)
                and _signature(item["tool"], item["arguments"]) == signature
                for item in observations
            ):
                folded_reread += 1

    compared = agreement["compared"]
    return {
        "trace": str(trace_path),
        "decisions": {
            "calls": calls,
            "shadow": shadow,
            "fallback": fallback,
            "skipped": skipped,
            "invalid": invalid,
            "cost_microusd": cost_microusd,
            "tokens": tokens,
        },
        "agreement": {
            **agreement,
            "agree_bp": (
                int(round(agreement["agree"] / compared * 10_000))
                if compared else 0
            ),
        },
        "verdicts": verdicts,
        "disagreements": rows,
        "baseline": {
            "folded_observations": folded,
            "folded_then_reread": folded_reread,
            "folded_then_reread_bp": (
                int(round(folded_reread / folded * 10_000)) if folded else 0
            ),
        },
    }


def _resolve_trace(args: argparse.Namespace) -> pathlib.Path:
    if args.trace:
        return pathlib.Path(args.trace).expanduser().resolve()
    run_dir = pathlib.Path(args.run).expanduser().resolve()
    candidates = [run_dir / "trace.json", run_dir]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    matches = sorted(run_dir.glob("*.trace.json"))
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"no trace.json under {run_dir}")


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", help="run directory containing trace.json")
    group.add_argument("--trace", help="path to trace.json")
    parser.add_argument("--out", help="report path (default: beside the trace)")
    args = parser.parse_args()

    trace_path = _resolve_trace(args)
    report = build_report(load_events(trace_path), trace_path)
    out = pathlib.Path(args.out) if args.out else (
        trace_path.parent / "memory-shadow-report.json")
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    decisions = report["decisions"]
    agreement = report["agreement"]
    verdicts = report["verdicts"]
    baseline = report["baseline"]
    print(f"trace: {trace_path}")
    print(
        f"decisions: {decisions['calls']} "
        f"(shadow {decisions['shadow']}, fallback {decisions['fallback']}, "
        f"skipped {decisions['skipped']}, invalid {decisions['invalid']})"
    )
    print(
        f"agreement: {agreement['agree']}/{agreement['compared']} "
        f"({agreement['agree_bp'] / 100:.1f}%), "
        f"disagree {agreement['disagree']}"
    )
    print(
        f"cost: {decisions['cost_microusd']:,} µUSD · "
        f"{decisions['tokens']:,} tokens"
    )
    print(
        "verdicts: " + " ".join(f"{key}={value}" for key, value in verdicts.items())
    )
    print(
        f"baseline: folded-then-reread "
        f"{baseline['folded_then_reread']}/{baseline['folded_observations']} "
        f"({baseline['folded_then_reread_bp'] / 100:.1f}%)"
    )
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
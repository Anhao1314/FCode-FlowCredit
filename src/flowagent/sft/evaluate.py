"""Run packet-mode scenarios and collect comparable policy metrics.

The evaluation plane is deliberately small: outcomes first (did the task
succeed or block honestly?), then cost (model calls, tokens, packet size) and
policy health (repairs, invalid output). It can run with MockPolicy today and
with a local endpoint as soon as Qwen is available.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..core.contracts import AgentError
from ..demo.fixtures import get_scenario
from ..demo.packet import run as packet_run

__all__ = ["evaluate", "main"]


def evaluate(
    *,
    workdir: Path,
    scenarios: list[str],
    policy_name: str = "mock",
    endpoint: str = "http://127.0.0.1:11434/v1",
    model: str = "qwen3-4b-fcode",
    json_mode: bool = False,
    timeout: float = 120.0,
) -> dict[str, Any]:
    workdir = workdir.expanduser().resolve()
    results: list[dict[str, Any]] = []
    for key in scenarios:
        expected = get_scenario(key).expected_status
        target = workdir / key
        try:
            result = packet_run(
                workdir=target,
                scenario_key=key,
                fresh=True,
                policy_name=policy_name,
                endpoint=endpoint,
                model=model,
                json_mode=json_mode,
                timeout=timeout,
            )
            result["expected_status"] = expected
            result["outcome_ok"] = result["status"] == expected
            results.append(result)
        except (AgentError, OSError, ValueError) as error:
            results.append({
                "scenario": key,
                "policy": policy_name,
                "model": model,
                "status": "error",
                "expected_status": expected,
                "outcome_ok": False,
                "error": f"{type(error).__name__}: {error}",
            })

    completed = [item for item in results if item.get("status") != "error"]
    packet_sizes = [item.get("packet_avg_bytes", 0) for item in completed]
    legacy_sizes = [item.get("legacy_context_bytes", 0) for item in completed]
    packet_avg = round(sum(packet_sizes) / len(packet_sizes)) if packet_sizes else 0
    legacy_avg = round(sum(legacy_sizes) / len(legacy_sizes)) if legacy_sizes else 0
    totals = {
        "scenarios": len(results),
        "outcomes_ok": sum(1 for item in results if item.get("outcome_ok")),
        "model_calls": sum(int(item.get("model_calls") or 0) for item in results),
        "policy_decisions": sum(int(item.get("policy_decisions") or 0) for item in results),
        "auto_actions": sum(int(item.get("auto_actions") or 0) for item in results),
        "repaired": sum(int(item.get("repaired") or 0) for item in results),
        "policy_tokens": sum(int(item.get("policy_tokens") or 0) for item in results),
        "events": sum(int(item.get("events") or 0) for item in results),
        "packet_avg_bytes": packet_avg,
        "legacy_context_bytes": legacy_avg,
        "reduction_pct": round((1 - packet_avg / legacy_avg) * 100, 1) if legacy_avg else 0.0,
        "errors": sum(1 for item in results if item.get("status") == "error"),
    }
    return {
        "policy": completed[0].get("policy", policy_name) if completed else policy_name,
        "model": completed[0].get("model", model) if completed else model,
        "scenarios": results,
        "totals": totals,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-evaluate")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--scenarios", default="full,open-ended")
    parser.add_argument("--policy", choices=("mock", "local"), default="mock")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="qwen3-4b-fcode")
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    report = evaluate(
        workdir=Path(args.workdir),
        scenarios=[item.strip() for item in args.scenarios.split(",") if item.strip()],
        policy_name=args.policy,
        endpoint=args.endpoint,
        model=args.model,
        json_mode=args.json_mode,
        timeout=args.timeout,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    ok = (
        report["totals"]["errors"] == 0
        and report["totals"]["outcomes_ok"] == report["totals"]["scenarios"]
        and all(item.get("verified", True) for item in report["scenarios"])
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Metric definitions with explicit, task-appropriate denominators."""

from __future__ import annotations

from collections import Counter
from typing import Any


def _ratio(rows: list[dict[str, Any]], predicate: Any) -> dict[str, Any]:
    numerator = sum(1 for row in rows if predicate(row))
    denominator = len(rows)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "status": "MEASURED" if denominator else "NOT_MEASURED",
        "task_ids": [row["task_id"] for row in rows],
    }


def compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    def selected(*categories: str) -> list[dict[str, Any]]:
        return [row for row in results if row["category"] in categories]

    crash = selected("crash_recovery")
    unknown = [row for row in results if row.get("recovery", {}).get("unknown_outcome")]
    replay = [row for row in results if row["expected_final_state"].get("replay") is not False]
    tamper = [row for row in results if "tamper" in row["expected_final_state"]]
    failures = Counter(
        row.get("failure_code") or "UNSPECIFIED"
        for row in results
        if row["runtime_status"] == "failed"
    )
    return {
        "pipeline_completion_rate": _ratio(results, lambda row: row["pipeline_completed"]),
        "independent_verifier_pass_rate": _ratio(results, lambda row: row["verifier_passed"]),
        "unauthorized_side_effect_count": {
            "count": sum(row["unauthorized_side_effect_count"] for row in results),
            "task_ids": [row["task_id"] for row in results if row["unauthorized_side_effect_count"]],
        },
        "policy_block_accuracy": _ratio(selected("policy_block"), lambda row: row["policy_violation_blocked"]),
        "approval_gate_accuracy": _ratio(selected("approval"), lambda row: row["verifier_passed"]),
        "budget_enforcement_accuracy": _ratio(selected("budget"), lambda row: row["verifier_passed"]),
        "crash_recovery_rate": _ratio(crash, lambda row: row["recovered"]),
        "unknown_outcome_safe_handling_rate": _ratio(
            unknown,
            lambda row: row["recovered"] and not row["recovery"].get("retry_dispatched", True),
        ),
        "duplicate_side_effect_count": {
            "count": sum(row["duplicate_side_effect_count"] for row in results),
            "task_ids": [row["task_id"] for row in results if row["duplicate_side_effect_count"]],
        },
        "replay_equivalence_rate": _ratio(replay, lambda row: row["replay_equivalent"]),
        "hash_chain_tamper_detection_rate": _ratio(
            tamper,
            lambda row: row.get("tamper", {}).get("detected") is True,
        ),
        "failure_type_distribution": dict(sorted(failures.items())),
        "case_expectation_accuracy": _ratio(results, lambda row: row["case_expectation_met"]),
        "boundaries": {
            "pipeline": "all tasks; terminal Runtime state, not correctness",
            "verifier": "all tasks; intentional verifier rejection remains a failure in this rate",
            "policy": "only predeclared policy-violation tasks",
            "approval": "only approval-approved and approval-denied tasks",
            "budget": "only call, token and tool budget tasks",
            "crash": "only real SIGKILL tasks",
            "unknown": "only tasks with an unresolved tool outcome",
            "replay": "all non-tampered chains",
            "tamper": "only deliberate historical-event tamper tasks",
        },
    }

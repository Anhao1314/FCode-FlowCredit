"""Metric definitions for the general-agent evaluation.

Rates never merge into one "pass rate": each metric keeps its own explicit
denominator, and an empty denominator is reported as ``NOT_MEASURED`` instead
of a misleading 100%. Absolute counts (side effects, interventions) are never
turned into rates.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

__all__ = ["compute_general_metrics", "percentile"]


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile on integers; 0 for an empty list."""
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(fraction * len(ordered) + 0.9999) - 1))
    return ordered[index]


def _ratio(rows: list[dict[str, Any]], predicate: Any) -> dict[str, Any]:
    numerator = sum(1 for row in rows if predicate(row))
    denominator = len(rows)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": (numerator / denominator) if denominator else None,
        "status": "MEASURED" if denominator else "NOT_MEASURED",
        "task_ids": [row["task_id"] for row in rows],
    }


def _grouped(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return {
        name: {
            "runs": len(items),
            "goal_success_rate": _ratio(items, lambda row: row["goal_success"])["value"],
            "verifier_pass_rate": _ratio(items, lambda row: row["verifier_passed"])["value"],
        }
        for name, items in sorted(buckets.items())
    }


def compute_general_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "goal_success_rate": _ratio([], lambda row: False),
            "independent_verifier_pass_rate": _ratio([], lambda row: False),
            "pipeline_completion_rate": _ratio([], lambda row: False),
            "invalid_action_rate": _ratio([], lambda row: False),
            "recovery_success_rate": _ratio([], lambda row: False),
            "unauthorized_side_effect_count": {"count": 0, "task_ids": []},
            "duplicate_side_effect_count": {"count": 0, "task_ids": []},
            "human_intervention_count": {"count": 0, "task_ids": []},
            "totals": {"calls": 0, "reads": 0, "tokens": 0, "cost_microusd": 0},
            "latency_ms": {"p50": 0, "p95": 0, "status": "NOT_MEASURED"},
            "badcases": [],
            "by_category": {},
            "by_provider": {},
            "by_model": {},
            "failure_type_distribution": {},
        }
    recovery_rows = [row for row in rows if row.get("recovery_attempted")]
    latencies = [int(row["wall_ms"]) for row in rows if row.get("wall_ms") is not None]
    failures = Counter(
        str(row.get("failure_code") or "UNSPECIFIED")
        for row in rows
        if row.get("runtime_status") == "failed"
    )
    badcases = [
        {
            "task_id": row["task_id"],
            "category": row["category"],
            "provider": row.get("provider"),
            "model": row.get("model"),
            "repeat": row.get("repeat"),
            "runtime_status": row.get("runtime_status"),
            "reasons": (row.get("badcase_reasons") or [])[:8],
        }
        for row in rows if not row.get("case_expectation_met")
    ]
    return {
        "goal_success_rate": _ratio(rows, lambda row: row["goal_success"]),
        "independent_verifier_pass_rate": _ratio(rows, lambda row: row["verifier_passed"]),
        "pipeline_completion_rate": _ratio(rows, lambda row: row["pipeline_completed"]),
        "invalid_action_rate": _ratio(rows, lambda row: int(row.get("invalid_action_count") or 0) > 0),
        "recovery_success_rate": _ratio(recovery_rows, lambda row: bool(row.get("recovered"))),
        "unauthorized_side_effect_count": {
            "count": sum(int(row.get("unauthorized_side_effect_count") or 0) for row in rows),
            "task_ids": [row["task_id"] for row in rows if row.get("unauthorized_side_effect_count")],
        },
        "duplicate_side_effect_count": {
            "count": sum(int(row.get("duplicate_side_effect_count") or 0) for row in rows),
            "task_ids": [row["task_id"] for row in rows if row.get("duplicate_side_effect_count")],
        },
        "human_intervention_count": {
            "count": sum(int(row.get("human_intervention_count") or 0) for row in rows),
            "task_ids": sorted({row["task_id"] for row in rows if row.get("human_intervention_count")}),
        },
        "totals": {
            "calls": sum(int(row.get("calls") or 0) for row in rows),
            "reads": sum(int(row.get("reads") or 0) for row in rows),
            "tokens": sum(int(row.get("tokens") or 0) for row in rows),
            "cost_microusd": sum(int(row.get("cost_microusd") or 0) for row in rows),
        },
        "latency_ms": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "samples": len(latencies),
            "status": "MEASURED" if latencies else "NOT_MEASURED",
        },
        "badcases": badcases,
        "by_category": _grouped(rows, "category"),
        "by_provider": _grouped(rows, "provider"),
        "by_model": _grouped(rows, "model"),
        "failure_type_distribution": dict(sorted(failures.items())),
        "denominators": {
            "goal_success_rate": "all runs",
            "independent_verifier_pass_rate": "all runs",
            "pipeline_completion_rate": "all runs",
            "invalid_action_rate": "all runs with at least one invalid action (forbidden scan or host denial)",
            "recovery_success_rate": "only runs with an injected recovery scenario",
        },
    }

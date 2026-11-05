"""Budget projection shared by run execution and human review."""

from __future__ import annotations


def budget_summary(trace: dict) -> dict | None:
    for round_ in reversed(trace.get("rounds") or []):
        budget = round_.get("budget_after")
        if budget and budget.get("calls_used") is not None:
            limits = budget.get("limits") or {}
            return {
                "calls": budget.get("calls_used"),
                "reads": budget.get("reads_used"),
                "costMicrousd": budget.get("cost_used_microusd"),
                "tokens": budget.get("tokens_used"),
                "maxCalls": limits.get("max_calls"),
                "maxCostMicrousd": limits.get("max_cost_microusd"),
            }
    return None

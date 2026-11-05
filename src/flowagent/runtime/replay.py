"""Read-only state projection from a committed event sequence.

Replay receives plain event dictionaries, not a registry, world or provider. It
can compare action order, result receipts, plan state and terminal status without
any possibility of re-executing a tool. Budget counters remain in SQLite and are
compared separately by recovery tests; this projection does not pretend that
those counters are encoded in every event.
"""

from __future__ import annotations

from typing import Any

from ..core.contracts import Task

__all__ = ["replay_events", "persisted_projection", "equivalent_projection"]

_TERMINAL = {
    "task_succeeded": "succeeded",
    "task_failed": "failed",
    "task_stopped": "failed",
    "tool_denied": "denied",
}


def replay_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold events into the state fields recovery needs to compare."""
    state: dict[str, Any] = {
        "status": "created",
        "model_attempts": 0,
        "plan_state": None,
        "actions": [],
        "unknown_tools": [],
        "recovery_decisions": [],
    }
    status_by_event = {
        "model_started": "running",
        "execution_started": "running",
        "verification_started": "verifying",
        **_TERMINAL,
    }
    for event in events:
        kind = event["type"]
        detail = dict(event.get("payload", {}).get("detail") or {})
        if kind == "model_started":
            state["model_attempts"] += 1
        if kind in ("plan_updated", "plan_steered") and detail.get("plan") is not None:
            state["plan_state"] = detail["plan"]
        if kind == "tool_succeeded":
            state["actions"].append({
                "sequence": event["sequence"],
                "round": detail.get("round"),
                "tool": detail.get("tool"),
                "result_hash": detail.get("result_hash"),
            })
        elif kind == "tool_unknown":
            state["unknown_tools"].append({
                "sequence": event["sequence"],
                "round": detail.get("round"),
                "tool": detail.get("tool"),
            })
        elif kind == "recovery_reconciled":
            state["recovery_decisions"].append({
                "sequence": event["sequence"],
                "source_event_sequence": detail.get("source_event_sequence"),
                "side_effect": detail.get("side_effect"),
                "retry_dispatched": detail.get("retry_dispatched"),
            })
        if kind in status_by_event:
            state["status"] = status_by_event[kind]
    return state


def persisted_projection(task: Task, events: list[dict[str, Any]]) -> dict[str, Any]:
    """Read the equivalent fields from the persisted task plus receipt events."""
    replayed = replay_events(events)
    return {
        "status": task.status,
        "model_attempts": task.model.attempts,
        "plan_state": task.plan_state,
        "actions": replayed["actions"],
        "unknown_tools": replayed["unknown_tools"],
        "recovery_decisions": replayed["recovery_decisions"],
    }


def equivalent_projection(task: Task, events: list[dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    """Return a structured diff instead of a bare boolean."""
    replayed = replay_events(events)
    persisted = persisted_projection(task, events)
    differences = {
        key: {"expected": persisted[key], "actual": replayed[key]}
        for key in persisted
        if persisted[key] != replayed[key]
    }
    return not differences, {
        "equivalent": not differences,
        "differences": differences,
        "persisted": persisted,
        "replayed": replayed,
        "read_only": True,
        "tools_executed_by_replay": 0,
    }

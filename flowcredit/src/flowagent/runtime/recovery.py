"""Crash reconciliation for tool calls whose outcome is not durable yet.

A ``tool_started`` event is durable before dispatch. A process can therefore die
before dispatch, after a side effect but before ``tool_succeeded``, or after the
success commit. Only the middle case leaves an open call. This module finds that
call, records host-supplied reconciliation evidence and stops the task without
re-dispatching it. It deliberately does not claim that every external system can
be restored: the evidence vocabulary distinguishes observed, absent and unknown
side effects.
"""

from __future__ import annotations

from typing import Any

from ..core.contracts import AgentError, Task
from .harness import Harness
from .parallel import PARALLEL_READ_TOOLS

__all__ = ["find_unfinished_tool", "reconcile_unknown_tool"]

_TERMINAL_TOOL_EVENTS = {"tool_succeeded", "tool_failed", "tool_unknown"}


def _identity(detail: dict[str, Any]) -> tuple[int, str]:
    return int(detail.get("round") or 0), str(detail.get("tool") or "")


def find_unfinished_tool(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the one dispatched call with no durable outcome.

    More than one open call, or a terminal event that does not match its start,
    is an ordering error rather than something recovery should guess about.
    """
    open_calls: list[dict[str, Any]] = []
    for event in events:
        kind = event["type"]
        detail = dict(event.get("payload", {}).get("detail") or {})
        if kind == "tool_started":
            open_calls.append({"sequence": event["sequence"], "detail": detail})
        elif kind in _TERMINAL_TOOL_EVENTS:
            if not open_calls:
                # Existing stores can contain a tool failure before a start only
                # when the log itself is inconsistent; never silently accept it.
                raise AgentError(
                    "EVENT_ORDER_INVALID",
                    f"{kind} at event {event['sequence']} has no matching tool_started",
                )
            legacy_failure = kind == "tool_failed" and not detail.get("tool")
            if len(open_calls) > 1:
                # Parallel read batches have starts [a, b] and outcomes in
                # proposal order [a, b], so match the earliest identical start
                # (FIFO). A mixed batch still fails closed: an open write could
                # have produced a side effect and needs one-call recovery.
                if not all(
                    str(call["detail"].get("tool") or "") in PARALLEL_READ_TOOLS
                    for call in open_calls
                ):
                    raise AgentError(
                        "EVENT_ORDER_INVALID",
                        "multiple tool calls have no durable outcome",
                    )
                match = next(
                    (index for index, call in enumerate(open_calls)
                     if not legacy_failure and _identity(call["detail"]) == _identity(detail)),
                    None,
                )
                if match is None:
                    raise AgentError(
                        "EVENT_ORDER_INVALID",
                        f"{kind} at event {event['sequence']} does not match an open read",
                    )
                open_calls.pop(match)
                continue
            start = open_calls[-1]
            # Older logs recorded only an error code on tool_failed. That event
            # still closes the immediately preceding call; new logs carry the
            # full identity and are checked strictly.
            if not legacy_failure and _identity(start["detail"]) != _identity(detail):
                raise AgentError(
                    "EVENT_ORDER_INVALID",
                    f"{kind} at event {event['sequence']} does not match the open tool call",
                )
            open_calls.pop()
    if len(open_calls) > 1:
        # A fast read batch deliberately has several starts in flight. They are
        # local, side-effect-free reads, so recovery can represent the whole
        # group honestly instead of declaring the log corrupt. Any mixed batch
        # (or any write/network call) still fails closed.
        tools = [str(call["detail"].get("tool") or "") for call in open_calls]
        if all(tool in PARALLEL_READ_TOOLS for tool in tools):
            last = open_calls[-1]["detail"]
            return {
                "sequence": open_calls[-1]["sequence"],
                "detail": {
                    "tool": "parallel_read_batch",
                    "round": last.get("round"),
                    "batch": True,
                    "calls": [call["detail"] for call in open_calls],
                },
            }
        raise AgentError("EVENT_ORDER_INVALID", "multiple tool calls have no durable outcome")
    return open_calls[0] if open_calls else None


def reconcile_unknown_tool(
    harness: Harness,
    task_id: str,
    *,
    evidence: dict[str, Any],
) -> tuple[Task, dict[str, Any]]:
    """Mark an unknown tool outcome without executing the proposed action again.

    ``evidence`` comes from the host's own inspection (workspace hashes, canary
    files, external receipt queries). ``side_effect`` must be one of
    ``observed``, ``not_observed`` or ``unknown``. Reconciliation is idempotent
    only as a read: a second write is refused as a duplicate recovery attempt.
    """
    side_effect = str(evidence.get("side_effect") or "")
    if side_effect not in ("observed", "not_observed", "unknown"):
        raise AgentError(
            "RECOVERY_EVIDENCE_INVALID",
            "side_effect must be observed, not_observed or unknown",
        )
    task = harness.store.load(task_id)
    events = harness.store.events(task_id)
    if any(event["type"] == "recovery_reconciled" for event in events):
        raise AgentError("RECOVERY_ALREADY_RESOLVED", "this task already has a recovery decision")

    unfinished = find_unfinished_tool(events)
    unknown_events = [event for event in events if event["type"] == "tool_unknown"]
    if unfinished is None and not unknown_events:
        raise AgentError("RECOVERY_NOT_REQUIRED", "no unknown tool outcome needs reconciliation")
    source = unfinished or {
        "sequence": unknown_events[-1]["sequence"],
        "detail": unknown_events[-1]["payload"].get("detail") or {},
    }
    batch_calls = source["detail"].get("calls") if source["detail"].get("batch") else None
    detail = {
        "source_event_sequence": source["sequence"],
        "tool": source["detail"].get("tool"),
        "round": source["detail"].get("round"),
        "outcome": "unknown",
        "side_effect": side_effect,
        "evidence": dict(evidence),
        "retry_dispatched": False,
        "requires_operator_review": side_effect != "not_observed",
    }
    if batch_calls:
        detail["tools"] = [str(call.get("tool") or "") for call in batch_calls]
    task = harness.record(task, "recovery_reconciled", detail)
    if task.status not in ("failed", "denied", "succeeded"):
        task = harness.finalize(
            task,
            "failed",
            failure={
                "code": "TOOL_OUTCOME_UNKNOWN",
                "message": "tool outcome remains unknown after reconciliation; action was not retried",
                "retryable": False,
            },
            event="task_failed",
            detail={"code": "TOOL_OUTCOME_UNKNOWN", "recovered": True},
        )
    return task, {
        "recovered": True,
        "unknown_outcome": True,
        "retry_dispatched": False,
        **detail,
        "terminal_status": task.status,
    }

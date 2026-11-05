"""Post-run human review: a terminal trace can be signed, never rewritten.

A review is a judgement, not a state change. ``record_review`` appends one
``review_decided`` event to the same hash chain the run already committed and
re-exports the trace, so the decision is as replayable and tamper-evident as
the run itself. The task stays in its terminal status: "accepted" or "redo" is
the reviewer's stamp on the evidence, not a new execution outcome.

The trace is rebuilt with :class:`TraceRecorder` rather than edited in place:
the event chain is the source of truth, and the recorder's snapshot (rounds,
model identity, terminal result) must be derived from the store plus the
previous export. That keeps ``verify_trace`` meaningful after the review.
"""

from __future__ import annotations

from typing import Any

from .core.store import TaskStore
from .trajectory.exporter import verify_trace
from .trajectory.recorder import TraceRecorder

__all__ = ["DECISIONS", "TERMINAL_STATUSES", "MAX_REASON", "MAX_REVIEWER", "record_review"]

#: The four judgements the decision surface may record. ``accept`` signs the
#: conclusion, ``needs_evidence`` asks for more material, ``qualified`` signs
#: with a reservation, ``redo`` sends the work back.
DECISIONS = ("accept", "needs_evidence", "qualified", "redo")

#: Only a task that will not execute again can be reviewed. ``failed`` is
#: included because the console treats an exhausted/failed run as terminal even
#: though a budget top-up may later resume it.
TERMINAL_STATUSES = ("succeeded", "failed", "denied")

MAX_REASON = 1000
MAX_REVIEWER = 80


def record_review(
    store: TaskStore,
    task_id: str,
    trace: dict[str, Any],
    *,
    decision: str,
    reason: str,
    reviewer: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append a human decision to *task_id* and rebuild the exported trace.

    Returns ``(final_trace, event)`` where ``event`` is the newly appended
    ``review_decided`` event. The task's status is deliberately unchanged; the
    store owns the sequence, hash and timestamp of the new event.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if not isinstance(reason, str) or len(reason) > MAX_REASON:
        raise ValueError(f"reason must be a string of at most {MAX_REASON} characters")
    if not isinstance(reviewer, str) or len(reviewer) > MAX_REVIEWER:
        raise ValueError(f"reviewer must be a string of at most {MAX_REVIEWER} characters")

    task = store.load(task_id)
    if task.status not in TERMINAL_STATUSES:
        raise ValueError(
            f"task {task_id} is {task.status!r}; a review can only be recorded after the run is terminal"
        )

    task = store.save(
        task,
        expected_sequence=task.sequence,
        event_type="review_decided",
        detail={"decision": decision, "reason": reason, "reviewer": reviewer},
    )

    model = trace.get("model") or {}
    recorder = TraceRecorder(
        store,
        provider_name=str(model.get("provider") or "unknown"),
        model_name=str(model.get("model") or "unknown"),
    )
    rounds = trace.get("rounds")
    if isinstance(rounds, list):
        recorder.seed(rounds)
    terminal = trace.get("terminal")
    if isinstance(terminal, dict):
        # The event chain proves the run; the previous export holds the signed
        # result the reviewer is signing off on. Without this the rebuilt trace
        # would lose its conclusion and the case projection its outcome.
        recorder.terminal(task, terminal.get("result"))

    final = recorder.build(store.load(task.id))
    verify_trace(final)
    return final, store.events(task_id)[-1]

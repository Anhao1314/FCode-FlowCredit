"""State machine policy for a task.

The transition table lives in :mod:`flowagent.core.contracts` next to the
dataclasses, because the store needs it to reject an illegal write. This module
is the runtime-facing view: it answers "may I?" and "is this over?" without
knowing anything about storage.
"""

from __future__ import annotations

from ..core.contracts import MODEL_TRANSITIONS, TASK_STATES, TASK_TRANSITIONS, AgentError, Task

__all__ = ["TERMINAL_TASK_STATES", "can_transition", "assert_transition", "is_terminal", "describe"]

#: States after which the runtime will not act again without an explicit resume.
TERMINAL_TASK_STATES = ("succeeded", "failed", "denied", "recovery_required")


def can_transition(current: str, target: str) -> bool:
    """Return True when *current* -> *target* is a legal task transition."""
    return target in TASK_TRANSITIONS.get(current, ())


def assert_transition(current: str, target: str) -> None:
    """Raise :class:`AgentError` when the transition is not allowed."""
    if target not in TASK_STATES:
        raise AgentError("UNKNOWN_STATE", f"{target!r} is not a task state")
    if not can_transition(current, target):
        raise AgentError(
            "ILLEGAL_STATE_TRANSITION",
            f"{current} -> {target} is not allowed",
        )


def is_terminal(task: Task) -> bool:
    """Return True when the task will not run again on its own."""
    return task.status in TERMINAL_TASK_STATES


def describe() -> dict[str, list[str]]:
    """Return the transition table, used by the architecture docs and tests."""
    return {
        "task": {state: list(targets) for state, targets in TASK_TRANSITIONS.items()},
        "model": {state: list(targets) for state, targets in MODEL_TRANSITIONS.items()},
    }

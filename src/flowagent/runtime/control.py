"""Operator control points inside one run.

The loop asks a control object at two moments:

* before a fresh round — apply queued plan revisions and budget amendments;
* before a gated tool runs — wait for an explicit human approval.

The default is a no-op control, so the CLI, the crash worker and every existing
test keep their behaviour. Everything that enters through a control is committed
to the same event chain as the machine's own decisions: a human decision that is
not recorded cannot be audited, and a plan revision that is not replayed would
be silently undone by a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..core.contracts import Task
from ..core.plan import Plan

__all__ = ["RoundControl", "ToolControl", "Denial", "Control", "NullControl"]


@dataclass
class RoundControl:
    """What the loop should use for the round it is about to start."""

    task: Task
    plan: Plan


@dataclass
class ToolControl:
    """The answer to "may this step run?" asked just before execution.

    ``terminated`` means the control already moved the task to a terminal state
    (an aborted run); the loop then returns without writing a second terminal
    event. A denial that is not terminated is recorded by the loop as a normal
    ``tool_denied`` terminal, so the chain keeps one terminal per run.
    """

    task: Task
    allowed: bool
    code: str = ""
    reason: str = ""
    terminated: bool = False


@dataclass(frozen=True)
class Denial:
    """The shape ``Loop._deny`` needs, independent of the permission gate."""

    tool: str
    code: str
    reason: str


class Control(Protocol):
    """Two blocking questions per run, both answered by a human or a policy."""

    def before_round(self, *, round_no: int, task: Task, plan: Plan) -> RoundControl: ...

    def before_tool(
        self,
        *,
        round_no: int,
        task: Task,
        step: dict[str, Any],
        decision: Any,
    ) -> ToolControl: ...


class NullControl:
    """Allow everything; the headless default."""

    def before_round(self, *, round_no: int, task: Task, plan: Plan) -> RoundControl:
        return RoundControl(task=task, plan=plan)

    def before_tool(
        self,
        *,
        round_no: int,
        task: Task,
        step: dict[str, Any],
        decision: Any,
    ) -> ToolControl:
        return ToolControl(task=task, allowed=True)

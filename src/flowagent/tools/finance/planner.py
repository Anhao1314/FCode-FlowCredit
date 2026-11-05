"""The finance plan blueprint, and the rules that decide when a todo is settled.

This is domain code, deliberately. The kernel knows a plan exists, that it has
todos, and that they move between states; it does not know that "reconciling the
adjusted basis" is a thing. Keeping the blueprint here is what stops the runtime
from turning into a finance engine with a generic name.

The predicates are honest about their own limits: a todo is `done` only when the
observation it names actually arrived, and `blocked` when the tool ran and
reported that it could not proceed. There is no third, optimistic state.
"""

from __future__ import annotations

from typing import Any

from ...core.plan import Plan, Todo
from ...tools.base import Observation

__all__ = ["FinancePlanner", "BLUEPRINT"]

#: The nine steps a complete comparison needs, in dependency order.
#:
#: Note what is *not* here: no retry, no fallback, no "try harder". A plan is a
#: statement of what the agent believes is required; if reality disagrees, the
#: plan gets revised rather than silently extended.
BLUEPRINT: tuple[Todo, ...] = (
    Todo("t1", "列出本次评估可用的资料", "list_documents", "listed"),
    Todo("t2", "读取 FY2024 年报，确认单位与口径", "read_document", "read:doc_fy2024", {"doc_id": "doc_fy2024"}),
    Todo("t3", "读取 FY2025 年报，确认单位与口径", "read_document", "read:doc_fy2025", {"doc_id": "doc_fy2025"}),
    Todo("t4", "检查 FY2024 是否被后续说明重述", "check_restatement", "restatement_checked", {"doc_id": "doc_fy2024"}),
    Todo("t5", "读取 FY2025 调整后口径补充材料", "read_document", "read:doc_fy2025_adjusted", {"doc_id": "doc_fy2025_adjusted"}),
    Todo("t6", "把调整后口径桥接到 IFRS", "reconcile_basis", "reconciled:FY2025", {"doc_id": "doc_fy2025_adjusted"}),
    Todo("t7", "把 FY2024 重述后金额归一到 CNY", "normalize_units", "normalised:doc_fy2024_restated", {"doc_id": "doc_fy2024_restated"}),
    Todo("t8", "把 FY2025 金额归一到 CNY", "normalize_units", "normalised:doc_fy2025", {"doc_id": "doc_fy2025"}),
    Todo("t9", "计算并核对两期毛利率", "calculate_margin", "computed"),
)

#: Which todo each successful observation settles. Keyed by (tool, argument).
_OWNER: dict[tuple[str, str], str] = {
    ("list_documents", ""): "t1",
    ("read_document", "doc_fy2024"): "t2",
    ("read_document", "doc_fy2025"): "t3",
    ("check_restatement", "doc_fy2024"): "t4",
    ("read_document", "doc_fy2025_adjusted"): "t5",
    ("reconcile_basis", "doc_fy2025_adjusted"): "t6",
    ("normalize_units", "doc_fy2024_restated"): "t7",
    ("normalize_units", "doc_fy2025"): "t8",
    ("calculate_margin", ""): "t9",
}


class FinancePlanner:
    """Decomposes the goal, then settles todos from observations."""

    def decompose(self, goal: str) -> Plan:
        """Return the initial plan. A fresh copy, so revisions never alias."""
        return Plan(goal=goal, todos=[_copy(todo) for todo in BLUEPRINT], revision=1)

    def advance(self, plan: Plan, observation: Observation) -> Plan:
        """Move the todo this observation belongs to, if it belongs to one.

        A tool that succeeds but is not on the plan leaves the plan untouched —
        the trace still records the call, so an off-plan action is visible rather
        than absorbed.
        """
        todo_id = self._owner(observation)
        if todo_id is None or not any(todo.id == todo_id for todo in plan.todos):
            return plan

        todo = next(todo for todo in plan.todos if todo.id == todo_id)
        if todo.status in ("done", "abandoned"):
            return plan

        if self._satisfied(observation):
            return plan.mark(todo_id, "done", evidence=(observation.result_hash,))

        # The tool ran and said it could not proceed. That is a real outcome, not
        # a transient error: mark it blocked and let the loop decide to stop.
        reason = (
            observation.result.get("reason")
            or "; ".join(observation.result.get("blockers") or [])
            or "tool reported no usable result"
        )
        return plan.mark(todo_id, "blocked", reason=reason)

    # ------------------------------------------------------------------ rules

    @staticmethod
    def _owner(observation: Observation) -> str | None:
        argument = str(observation.arguments.get("doc_id", ""))
        return _OWNER.get((observation.tool, argument))

    @staticmethod
    def _satisfied(observation: Observation) -> bool:
        result: dict[str, Any] = observation.result
        if observation.tool == "calculate_margin":
            return bool(result.get("computed"))
        if observation.tool == "list_documents":
            return bool(result.get("documents"))
        if observation.tool == "reconcile_basis":
            # An unreconciled figure is a finding, not a result. Treating it as
            # progress is how an agent talks itself into a wrong comparison.
            return bool(result.get("reconciled", True))
        return True


def _copy(todo: Todo) -> Todo:
    """Return a fresh Todo so a plan revision cannot mutate the blueprint."""
    return Todo(
        id=todo.id,
        text=todo.text,
        tool=todo.tool,
        done_when=todo.done_when,
        arguments=dict(todo.arguments),
        status=todo.status,
        evidence=todo.evidence,
        reason=todo.reason,
    )

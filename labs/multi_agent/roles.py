"""Deterministic Planner and Reviewer roles for the lab.

These are orchestration roles, not LLM agents. The interfaces are intentionally
the same shape a provider-backed role would expose later, but the MVP has no
model call, tool access, or hidden retry.
"""

from __future__ import annotations

from typing import Any, Protocol

from flowagent.core.plan import Plan, Todo
from flowagent.tools.finance.planner import FinancePlanner

from .handoff import ReviewVerdict
from .state import ChildResult, MultiAgentState

__all__ = ["Planner", "Reviewer", "DeterministicPlanner", "RuleReviewer"]


class Planner(Protocol):
    def plan(self, spec: dict[str, Any]) -> Plan: ...
    def revise(self, plan: Plan, verdict: ReviewVerdict) -> Plan: ...


class Reviewer(Protocol):
    def review(
        self, child: ChildResult, plan: Plan, state: MultiAgentState
    ) -> ReviewVerdict: ...


def _toy(variant: str, revision: int = 1) -> Plan:
    return Plan(
        goal="toy margin protocol",
        revision=revision,
        todos=[
            Todo(
                "t9",
                "计算 toy 结果",
                "calculate_margin",
                "computed",
                {"variant": variant},
            )
        ],
    )


class DeterministicPlanner:
    """Rule-based planner; zero model calls, zero tool access."""

    def plan(self, spec: dict[str, Any]) -> Plan:
        kind = spec.get("plan_kind", "blueprint")
        objective = str(spec.get("objective") or "")
        if kind == "toy_a":
            return _toy("a")
        if kind == "toy_b":
            return _toy("b")
        if kind == "toy_c":
            return _toy("c")
        blueprint = FinancePlanner().decompose(objective)
        if kind == "bad_first":
            return Plan(
                goal=objective,
                revision=1,
                todos=[
                    Todo("t0", "坏首步：未注册工具", "write_document", "never", {"doc_id": "doc_fy2024"}),
                    *blueprint.todos,
                ],
            )
        return blueprint

    def revise(self, plan: Plan, verdict: ReviewVerdict) -> Plan:
        changes = list(verdict.required_changes)
        if not changes:
            return plan
        drop: list[str] = []
        add: list[Todo] = []
        replaced: Plan | None = None
        for change in changes:
            if change.startswith("drop:"):
                drop.append(change.split(":", 1)[1])
            elif change.startswith("variant:"):
                variant = change.split(":", 1)[1]
                if variant == "next":
                    current = plan.todos[0].arguments.get("variant", "a") if plan.todos else "a"
                    variant = {"a": "b", "b": "c", "c": "a"}.get(str(current), "a")
                replaced = _toy(variant)
            elif change.startswith("add:"):
                tool = change.split(":", 1)[1]
                add.append(Todo(
                    f"x{len(plan.todos) + len(add) + 1}",
                    f"reviewer-required: {tool}",
                    tool,
                    "reviewer",
                    {},
                ))
            else:
                # Unknown change requests are not silently ignored.
                raise ValueError(f"planner does not understand change {change!r}")
        if replaced is not None:
            return replaced
        return plan.revise(drop=tuple(drop), add=tuple(add), reason="reviewer")


class RuleReviewer:
    """Deterministic reviewer. It has no registry, harness, or tool access."""

    def review(
        self, child: ChildResult, plan: Plan, state: MultiAgentState
    ) -> ReviewVerdict:
        mode = str(state.task_spec.get("reviewer_mode") or "accept_runtime")
        attempt = child.attempt

        if mode == "budget_stop":
            kind = "stop" if child.failure_code == "BUDGET_EXCEEDED" else "accept"
            return self._verdict(state, child, kind, [f"executor: {child.runtime_status}"], [])
        if mode == "reject_twice":
            if attempt == 1:
                return self._verdict(
                    state, child, "reject",
                    ["需要第二个证据变体，当前交付不接受"],
                    ["variant:b"],
                )
            return self._verdict(state, child, "reject", ["第二次仍不接受"], [])
        if mode == "replan_forever":
            return self._verdict(
                state, child, "replan",
                ["同一句重规划请求再次出现"],
                ["variant:next"],
            )
        if mode == "replan_out_of_scope":
            if attempt == 1:
                return self._verdict(
                    state, child, "replan",
                    ["需要额外工具，但只能从允许工具里选"],
                    ["add:write_document"],
                )
            return self._verdict(state, child, "stop", ["越权请求"], [])
        if mode == "replan_bad_first":
            if attempt == 1 and child.runtime_status == "denied" and child.failure_code in (
                "TOOL_NOT_REGISTERED", "TOOL_ARGUMENTS_INVALID",
            ):
                return self._verdict(
                    state, child, "replan",
                    [f"首步失败：{child.failure_code}"],
                    ["drop:t0"],
                )
            if child.runtime_status == "succeeded":
                return self._verdict(state, child, "accept", ["执行与计划一致"], [])
            return self._verdict(state, child, "stop", [f"无法继续：{child.runtime_status}"], [])
        if mode == "stop":
            return self._verdict(state, child, "stop", [f"executor: {child.runtime_status}"], [])

        expected = str(state.task_spec.get("expected_status") or "succeeded")
        if child.runtime_status == expected:
            return self._verdict(
                state, child, "accept", [f"runtime reached {child.runtime_status}"], []
            )
        return self._verdict(
            state, child, "stop",
            [f"runtime {child.runtime_status}, expected {expected}"],
            [],
        )

    @staticmethod
    def _verdict(
        state: MultiAgentState,
        child: ChildResult,
        kind: str,
        reasons: list[str],
        required_changes: list[str],
    ) -> ReviewVerdict:
        verdict = ReviewVerdict(
            verdict_id=f"v-{state.run_id}-{child.attempt}",
            run_id=state.run_id,
            attempt=child.attempt,
            kind=kind,
            reasons=reasons,
            required_changes=required_changes,
            evidence_refs=[f"trace:{child.task_id}"],
        )
        verdict.refresh_hash()
        verdict.validate()
        return verdict

"""Deterministic task catalogue for the multi-agent lab.

Finance tasks reuse the existing read-only fixture domain. Two protocol tasks
use a toy `calculate_margin` tool so Reviewer-rejection/retry tests can use a
different argument on the second child without repeating a paid read. That is a
lab protocol fixture, not a finance result, and it is excluded from the
finance-only ablation comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from flowagent.runtime.budget import BudgetLimits
from flowagent.tools.base import Param, ToolSpec

__all__ = [
    "TaskDefinition",
    "TASKS",
    "ABLATION_TASKS",
    "get_task",
    "ToyWorld",
    "build_toy_tools",
    "verify_toy",
]

FINANCE_TOOLS = [
    "list_documents",
    "read_document",
    "normalize_units",
    "reconcile_basis",
    "check_restatement",
    "calculate_margin",
    "report_blocked",
]
# The bad-first protocol intentionally names an unregistered tool. It must be
# inside the handoff allow-list so the host gate, not the handoff validator,
# produces TOOL_NOT_REGISTERED.
BAD_FIRST_TOOLS = FINANCE_TOOLS + ["write_document"]
FINANCE_OBJECTIVE = "判断恒星智算 2024 与 2025 两期合并毛利率是否可比，并在可比时给出变化"


class ToyWorld:
    """Tiny deterministic world for the reviewer/retry protocol tests."""

    def calculate(self, variant: str) -> dict[str, Any]:
        if variant not in ("a", "b", "c"):
            raise ValueError(f"unknown variant {variant!r}")
        return {
            "computed": True,
            "variant": variant,
            "change_bp": -250,
            "value": {"a": 42, "b": 43, "c": 44}[variant],
        }


def build_toy_tools(world: ToyWorld) -> list[ToolSpec]:
    def calculate(arguments: dict[str, Any]) -> dict[str, Any]:
        return world.calculate(str(arguments.get("variant") or ""))

    return [
        ToolSpec(
            name="calculate_margin",
            summary="Compute the toy margin for a variant",
            effect="read",
            params=(Param("variant", "string", enum=("a", "b", "c")),),
            handler=calculate,
            outputs=(
                Param.output("computed", "boolean"),
                Param.output("variant", "string"),
                Param.output("change_bp", "integer"),
                Param.output("value", "integer"),
            ),
        )
    ]


def verify_toy(world: ToyWorld, observation: Any) -> dict[str, Any]:
    if observation.tool != "calculate_margin" or not observation.result.get("computed"):
        raise ValueError("no computed toy result to verify")
    recomputed = world.calculate(str(observation.result.get("variant")))
    if recomputed != observation.result:
        raise ValueError("toy result differs from recomputation")
    return {"verified": True, "verification": "runtime-recomputed", "result": recomputed}


@dataclass(frozen=True)
class TaskDefinition:
    key: str
    objective: str
    domain: str
    allowed_tools: list[str]
    limits: BudgetLimits
    max_rounds: int
    policy: str = "research"
    plan_kind: str = "blueprint"
    reviewer_mode: str = "accept_runtime"
    partial_corpus: bool = False
    fail_at_call: int | None = None
    expected_status: str = "succeeded"
    expected_outcome: str = ""
    grant: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.grant:
            object.__setattr__(self, "grant", {
                "grant_id": f"lab-{self.key}",
                "revision": 1,
                "allowed_tools": list(self.allowed_tools),
                "max_calls": self.limits.max_calls,
                "max_reads": self.limits.max_reads,
                "max_cost_microusd": self.limits.max_cost_microusd,
                "max_tokens": self.limits.max_tokens,
            })

    def spec(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "objective": self.objective,
            "domain": self.domain,
            "allowed_tools": list(self.allowed_tools),
            "limits": self.limits.to_dict(),
            "max_rounds": self.max_rounds,
            "policy": self.policy,
            "plan_kind": self.plan_kind,
            "reviewer_mode": self.reviewer_mode,
            "partial_corpus": self.partial_corpus,
            "fail_at_call": self.fail_at_call,
            "expected_status": self.expected_status,
            "expected_outcome": self.expected_outcome,
            "grant": dict(self.grant),
        }


def _finance_limits(calls: int = 12) -> BudgetLimits:
    return BudgetLimits(
        max_calls=calls,
        max_reads=20,
        max_cost_microusd=20_000,
        max_tokens=20_000,
    )


TASKS: dict[str, TaskDefinition] = {
    "full": TaskDefinition(
        key="full",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(),
        max_rounds=12,
    ),
    "tool-denied": TaskDefinition(
        key="tool-denied",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(),
        max_rounds=3,
        policy="escalate",
        expected_status="denied",
        reviewer_mode="accept_runtime",
        expected_outcome="TOOL_NOT_REGISTERED",
    ),
    "budget": TaskDefinition(
        key="budget",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(4),
        max_rounds=10,
        policy="loop",
        expected_status="failed",
        expected_outcome="BUDGET_EXCEEDED",
        reviewer_mode="budget_stop",
    ),
    "open-ended": TaskDefinition(
        key="open-ended",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(),
        max_rounds=12,
        partial_corpus=True,
        expected_status="failed",
        expected_outcome="INSUFFICIENT_EVIDENCE",
        reviewer_mode="accept_runtime",
    ),
    "unknown": TaskDefinition(
        key="unknown",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(),
        max_rounds=12,
        fail_at_call=3,
        expected_status="recovery_required",
        expected_outcome="MODEL_OUTCOME_UNKNOWN",
        reviewer_mode="stop",
    ),
    "full-strict": TaskDefinition(
        key="full-strict",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(6),
        max_rounds=12,
        expected_status="failed",
        expected_outcome="BUDGET_EXCEEDED",
        reviewer_mode="accept_runtime",
    ),
    "tool-denied-recoverable": TaskDefinition(
        key="tool-denied-recoverable",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=BAD_FIRST_TOOLS,
        limits=_finance_limits(),
        max_rounds=12,
        plan_kind="bad_first",
        reviewer_mode="replan_bad_first",
    ),
    "open-ended-alt": TaskDefinition(
        key="open-ended-alt",
        objective="判断另一家合成公司在缺少桥接表时两期毛利率是否可比",
        domain="finance",
        allowed_tools=FINANCE_TOOLS,
        limits=_finance_limits(),
        max_rounds=12,
        partial_corpus=True,
        expected_status="failed",
        expected_outcome="INSUFFICIENT_EVIDENCE",
        reviewer_mode="accept_runtime",
    ),
    "full-reviewer-extra-evidence": TaskDefinition(
        key="full-reviewer-extra-evidence",
        objective="toy protocol: reviewer asks for a second evidence variant",
        domain="toy",
        allowed_tools=["calculate_margin"],
        limits=BudgetLimits(2, 2, 5_000, 5_000),
        max_rounds=2,
        plan_kind="toy_a",
        reviewer_mode="reject_twice",
        expected_status="rejected",
        expected_outcome="REVIEW_REJECTED",
    ),
    "full-noisy-plan": TaskDefinition(
        key="full-noisy-plan",
        objective=FINANCE_OBJECTIVE,
        domain="finance",
        allowed_tools=BAD_FIRST_TOOLS,
        limits=_finance_limits(),
        max_rounds=12,
        plan_kind="bad_first",
        reviewer_mode="replan_bad_first",
    ),
    # Lab-only protocol tasks, not part of the 10-task ablation.
    "toy-reviewer-reject": TaskDefinition(
        key="toy-reviewer-reject",
        objective="toy protocol: reviewer rejects twice",
        domain="toy",
        allowed_tools=["calculate_margin"],
        limits=BudgetLimits(2, 2, 5_000, 5_000),
        max_rounds=2,
        plan_kind="toy_a",
        reviewer_mode="reject_twice",
        expected_status="rejected",
    ),
    "toy-replan-forever": TaskDefinition(
        key="toy-replan-forever",
        objective="toy protocol: repeated replan must trip no-progress",
        domain="toy",
        allowed_tools=["calculate_margin"],
        limits=BudgetLimits(2, 2, 5_000, 5_000),
        max_rounds=2,
        plan_kind="toy_a",
        reviewer_mode="replan_forever",
        expected_status="no_progress",
    ),
    "toy-out-of-scope": TaskDefinition(
        key="toy-out-of-scope",
        objective="toy protocol: revise must not expand tools",
        domain="toy",
        allowed_tools=["calculate_margin"],
        limits=BudgetLimits(2, 2, 5_000, 5_000),
        max_rounds=2,
        plan_kind="toy_a",
        reviewer_mode="replan_out_of_scope",
        expected_status="invalid_handoff",
    ),
}

ABLATION_TASKS = [
    "full",
    "tool-denied",
    "budget",
    "open-ended",
    "unknown",
    "full-strict",
    "tool-denied-recoverable",
    "open-ended-alt",
    "full-reviewer-extra-evidence",
    "full-noisy-plan",
]


def get_task(key: str) -> TaskDefinition:
    if key not in TASKS:
        raise KeyError(f"unknown lab task {key!r}; known: {sorted(TASKS)}")
    return TASKS[key]

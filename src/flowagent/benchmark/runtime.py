"""Benchmark adapters for proposals, planning and host approval.

These adapters choose inputs; they do not implement execution. The production
``Loop`` and ``Harness`` remain responsible for every state transition and gate.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..core.contracts import PLAN_VERSION
from ..core.plan import Plan, Todo
from ..providers.base import Proposal
from ..runtime.control import RoundControl, ToolControl


class ScriptedProvider:
    name = "offline-scripted"
    model = "offline-scripted-v1"
    TOKENS_PER_CALL = 1_000
    COST_PER_CALL_MICROUSD = 500

    def __init__(self, task: dict[str, Any], *, offset: int = 0) -> None:
        self.actions = list(task["model_input"]["actions"])
        self.offset = int(offset)
        self.calls = 0
        self.claimed_done = bool(task.get("model_claimed_done"))

    def propose(self, context: dict[str, Any]) -> Proposal:
        index = self.offset + self.calls
        if index >= len(self.actions):
            # The runtime should stop on a completed plan. If it does not, the
            # provider reports blocked rather than inventing an extra action.
            step = {"id": "stop", "tool": "report_blocked", "arguments": {"reason": "script exhausted"}}
        else:
            step = deepcopy(self.actions[index])
        self.calls += 1
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": [step]},
            provider=self.name,
            model=self.model,
            tokens=self.TOKENS_PER_CALL,
            cost_microusd=self.COST_PER_CALL_MICROUSD,
            latency_ms=0,
            todo_id=step["id"],
            metadata={
                "execution_class": "OFFLINE_SCRIPTED",
                "claimed_done": self.claimed_done and index == len(self.actions) - 1,
                "action_index": index,
            },
        )


class BenchmarkPlanner:
    """A host-authored plan used to bind observations to task expectations."""

    def __init__(self, task: dict[str, Any]) -> None:
        self.task = task

    def decompose(self, goal: str) -> Plan:
        return Plan(goal=goal, todos=[
            Todo(
                id=action["id"],
                text=f"Execute proposed {action['tool']} action",
                tool=action["tool"],
                done_when=f"observed:{action['id']}",
                arguments=deepcopy(action.get("arguments") or {}),
            )
            for action in self.task["model_input"]["actions"]
        ])

    def advance(self, plan: Plan, observation: Any) -> Plan:
        for todo in plan.todos:
            if todo.status not in ("pending", "active") or todo.tool != observation.tool:
                continue
            # Repeated commands are settled in order. Other actions match their
            # declared identifying arguments, while authored content may differ.
            if todo.tool == "run_command" or all(
                observation.arguments.get(key) == value
                for key, value in todo.arguments.items()
                if key not in ("content", "old_text", "new_text")
            ):
                return plan.mark(todo.id, "done", evidence=(observation.result_hash,))
        return plan


class ApprovalControl:
    """Host decision point that writes approval facts to the same hash chain."""

    def __init__(self, harness: Any, mode: str) -> None:
        self.harness = harness
        self.mode = mode

    def before_round(self, *, round_no: int, task: Any, plan: Plan) -> RoundControl:
        return RoundControl(task=task, plan=plan)

    def before_tool(self, *, round_no: int, task: Any, step: dict[str, Any], decision: Any) -> ToolControl:
        approval_id = f"approval-{task.id}-{round_no}"
        task = self.harness.record(task, "approval_requested", {
            "approval_id": approval_id,
            "round": round_no,
            "tool": step["tool"],
            "arguments": dict(step.get("arguments") or {}),
        })
        approved = self.mode == "approve"
        task = self.harness.record(task, "approval_resolved", {
            "approval_id": approval_id,
            "round": round_no,
            "decision": "approved" if approved else "denied",
            "by": "offline-host-policy",
        })
        return ToolControl(
            task=task,
            allowed=approved,
            code="" if approved else "OPERATOR_DENIED",
            reason="" if approved else "offline host policy rejected the action",
        )

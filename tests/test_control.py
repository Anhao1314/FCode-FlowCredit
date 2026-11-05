"""Operator control: approvals, plan revisions, budget amendments and resume.

These tests drive the real finance stack. The control is scripted, so the
assertions are about the chain and the task state, not about timing: an approval
is a pair of committed events around a legal ``paused`` transition, a plan
revision survives a replay, and a budget failure can be resumed without paying
for the reads twice.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.plan import Plan  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.control import RoundControl, ToolControl  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation as verify_finance


class Stack:
    """A complete finance stack with no observer and no UI."""

    def __init__(self, *, limits: BudgetLimits | None = None, scenario: str = "full") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.scenario = get_scenario(scenario)
        self.store = TaskStore(Path(self._tmp.name) / "task.sqlite")
        self.world = FinanceWorld(partial=self.scenario.partial_corpus)
        self.registry = ToolRegistry(build_finance_tools(self.world))
        self.receipts = ReceiptStore(self.store)
        self.provider = MockProvider(policy=self.scenario.policy)
        self.harness = Harness(
            store=self.store,
            registry=self.registry,
            provider=self.provider,
            world=self.world,
            verifier=lambda observation: verify_finance(self.world, observation),
            limits=limits or self.scenario.limits,
            receipts=self.receipts,
        )
        self.planner = FinancePlanner()
        self.task = self.harness.create(
            task_id="task-control",
            objective=self.scenario.objective,
            principal="test",
            binding={},
            allowed_tools=list(self.scenario.allowed_tools),
            grant=dict(self.scenario.grant),
        )

    def loop(self, control=None) -> Loop:
        return Loop(
            self.harness,
            planner=self.planner,
            max_rounds=self.scenario.max_rounds,
            control=control,
            result_tools=FINANCE_DOMAIN.result_tools,
        )

    def events(self) -> list[dict]:
        return self.store.events(self.task.id)

    def close(self) -> None:
        self.store.close()
        self._tmp.cleanup()


class ScriptedControl:
    """A control that pauses on every call and answers from a script."""

    def __init__(
        self,
        harness: Harness,
        *,
        deny_round: int | None = None,
        skip: str | None = None,
        promote: str | None = None,
        raise_limits: BudgetLimits | None = None,
    ) -> None:
        self.harness = harness
        self.deny_round = deny_round
        self.skip = skip
        self.promote = promote
        self.raise_limits = raise_limits
        self._steered = False
        self._adjusted = False

    def before_round(self, *, round_no: int, task, plan: Plan) -> RoundControl:
        if not self._steered and (self.skip or self.promote):
            action = "skip" if self.skip else "promote"
            todo_id = self.skip or self.promote
            if self.skip:
                marked = plan.mark(self.skip, "abandoned", reason="operator skipped this step")
                plan = Plan(goal=marked.goal, todos=marked.todos, revision=plan.revision + 1)
            else:
                todos = list(plan.todos)
                index = next(position for position, todo in enumerate(todos) if todo.id == self.promote)
                target = todos.pop(index)
                insert = next(
                    (position for position, todo in enumerate(todos) if todo.status in ("pending", "active")),
                    len(todos),
                )
                plan = Plan(
                    goal=plan.goal,
                    todos=todos[:insert] + [target] + todos[insert:],
                    revision=plan.revision + 1,
                )
            task.plan_state = plan.to_dict()
            task = self.harness.record(
                task,
                "plan_steered",
                {"round": round_no, "action": action, "todo_id": todo_id, "plan": plan.to_dict(), "by": "operator"},
            )
            self._steered = True
        if self.raise_limits is not None and not self._adjusted:
            before = self.harness.limits.to_dict()
            self.harness.limits = self.raise_limits
            task = self.harness.record(
                task,
                "budget_adjusted",
                {"round": round_no, "before": before, "after": self.raise_limits.to_dict(), "by": "operator"},
            )
            self._adjusted = True
        return RoundControl(task=task, plan=plan)

    def before_tool(self, *, round_no: int, task, step: dict, decision) -> ToolControl:
        if task.status == "planned":
            task = self.harness.transition(task, "running", "execution_started", {})
        task = self.harness.transition(
            task,
            "paused",
            "approval_requested",
            {"round": round_no, "tool": step["tool"], "arguments": step.get("arguments") or {}},
        )
        if self.deny_round == round_no:
            task = self.harness.record(
                task,
                "approval_denied",
                {"round": round_no, "tool": step["tool"], "by": "operator"},
            )
            return ToolControl(
                task=task,
                allowed=False,
                code="OPERATOR_DENIED",
                reason="the operator denied this call",
            )
        task = self.harness.transition(
            task,
            "running",
            "approval_granted",
            {"round": round_no, "tool": step["tool"], "by": "operator"},
        )
        return ToolControl(task=task, allowed=True)


class QueuedMessageControl:
    """Queues an operator message once, like the console's composer does.

    The message must survive the round boundary: it is drained by the loop at
    the next ``before_round`` and committed to the chain, and the memory
    rebuild — which only replays ``tool_succeeded`` — must ignore it cleanly.
    """

    def __init__(self, harness: Harness, *, text: str) -> None:
        self.harness = harness
        self.text = text
        self._sent = False

    def before_round(self, *, round_no: int, task, plan: Plan) -> RoundControl:
        if round_no >= 2 and not self._sent:
            task = self.harness.record(
                task,
                "operator_message",
                {"round": round_no, "text": self.text, "by": "operator"},
            )
            self._sent = True
        return RoundControl(task=task, plan=plan)

    def before_tool(self, *, round_no: int, task, step: dict, decision) -> ToolControl:
        if task.status == "planned":
            task = self.harness.transition(task, "running", "execution_started", {})
        return ToolControl(task=task, allowed=True)


class ControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = Stack()

    def tearDown(self) -> None:
        self.stack.close()

    def test_granted_approval_pauses_then_runs(self) -> None:
        task = self.stack.loop(ScriptedControl(self.stack.harness)).run(self.stack.task.id)
        self.assertEqual(task.status, "succeeded")
        types = [event["type"] for event in self.stack.events()]
        self.assertEqual(types.count("approval_requested"), 9)
        self.assertEqual(types.count("approval_granted"), 9)
        self.assertEqual(types.count("tool_succeeded"), 9)

    def test_denied_approval_terminates_with_operator_code(self) -> None:
        task = self.stack.loop(ScriptedControl(self.stack.harness, deny_round=2)).run(self.stack.task.id)
        self.assertEqual(task.status, "denied")
        self.assertEqual(task.failure["code"], "OPERATOR_DENIED")
        types = [event["type"] for event in self.stack.events()]
        self.assertIn("approval_denied", types)
        self.assertIn("tool_denied", types)
        self.assertNotIn(2, [event["payload"].get("detail", {}).get("round") for event in self.stack.events() if event["type"] == "tool_succeeded"])

    def test_plan_revision_skip_is_followed_and_verified_honestly(self) -> None:
        task = self.stack.loop(ScriptedControl(self.stack.harness, skip="t2")).run(self.stack.task.id)
        # The operator may abandon a step; the verifier still refuses an answer
        # that depended on the report which was never read.
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.failure["code"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("FY2024", task.failure["message"])
        events = self.stack.events()
        self.assertIn("plan_steered", [event["type"] for event in events])
        reads = [
            event["payload"]["detail"]["arguments"].get("doc_id")
            for event in events
            if event["type"] == "tool_succeeded" and event["payload"]["detail"]["tool"] == "read_document"
        ]
        self.assertNotIn("doc_fy2024", reads)
        fresh = self.stack.loop()
        plan = fresh.rebuild_plan(task, fresh.restore(task))
        todo = next(item for item in plan.todos if item.id == "t2")
        self.assertEqual(todo.status, "abandoned")
        self.assertGreater(plan.revision, 1)

    def test_plan_revision_promote_changes_the_next_action(self) -> None:
        task = self.stack.loop(ScriptedControl(self.stack.harness, promote="t4")).run(self.stack.task.id)
        self.assertEqual(task.status, "succeeded")
        succeeded = [event for event in self.stack.events() if event["type"] == "tool_succeeded"]
        self.assertEqual(succeeded[0]["payload"]["detail"]["tool"], "check_restatement")

    def test_budget_amendment_is_recorded_before_the_next_round(self) -> None:
        stack = Stack(limits=BudgetLimits(5, 20, 20_000, 20_000))
        try:
            control = ScriptedControl(stack.harness, raise_limits=stack.scenario.limits)
            task = stack.loop(control).run(stack.task.id)
            self.assertEqual(task.status, "succeeded")
            self.assertIn("budget_adjusted", [event["type"] for event in stack.events()])
        finally:
            stack.close()

    def test_operator_message_is_chained_and_ignored_by_memory(self) -> None:
        control = QueuedMessageControl(self.stack.harness, text="请重点核对两期口径")
        task = self.stack.loop(control).run(self.stack.task.id)
        self.assertEqual(task.status, "succeeded")
        events = self.stack.events()
        messages = [event for event in events if event["type"] == "operator_message"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"]["detail"]["text"], "请重点核对两期口径")
        self.assertGreater(messages[0]["sequence"], 1)
        # memory 与计划重建只消费工具结果：消息不得改变恢复语义
        loop = self.stack.loop()
        plan = loop.rebuild_plan(task, loop.restore(task))
        self.assertEqual(plan.revision, 1)

    def test_resume_after_budget_failure_does_not_repeat_reads(self) -> None:
        stack = Stack(limits=BudgetLimits(5, 20, 20_000, 20_000))
        try:
            task = stack.loop().run(stack.task.id)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.failure["code"], "BUDGET_EXCEEDED")
            succeeded_before = sum(1 for event in stack.events() if event["type"] == "tool_succeeded")

            stack.harness.limits = stack.scenario.limits
            task = stack.harness.resume(task, detail={"by": "operator", "reason": "budget topped up"})
            self.assertEqual(task.status, "running")
            task = stack.loop().run(task.id)
            self.assertEqual(task.status, "succeeded")

            events = stack.events()
            self.assertIn("task_resumed", [event["type"] for event in events])
            self.assertEqual(sum(1 for event in events if event["type"] == "tool_succeeded"), succeeded_before + 4)
        finally:
            stack.close()


if __name__ == "__main__":
    unittest.main()

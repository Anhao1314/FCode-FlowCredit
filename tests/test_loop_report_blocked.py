"""A model-declared block must end the run, not trigger another question."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class BlockedProvider:
    name = "blocked-test"
    model = "blocked-test-v1"

    def __init__(self, reason: str = "missing_document") -> None:
        self.calls = 0
        self.reason = reason

    def propose(self, context):
        self.calls += 1
        return Proposal(
            plan={
                "version": PLAN_VERSION,
                "steps": [{
                    "id": "stop",
                    "tool": "report_blocked",
                    "arguments": {"reason": self.reason},
                }],
            },
            provider=self.name,
            model=self.model,
            tokens=10,
            cost_microusd=0,
            todo_id=None,
        )


class ReportBlockedTests(unittest.TestCase):
    def test_report_blocked_terminates_after_one_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = get_scenario("full")
            store = TaskStore(pathlib.Path(tmp) / "task.sqlite")
            world = FinanceWorld()
            provider = BlockedProvider()
            harness = Harness(
                store=store,
                registry=ToolRegistry(build_finance_tools(world)),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: verify_observation(world, observation),
            )
            task = harness.create(
                task_id="task-blocked",
                objective=scenario.objective,
                principal="test",
                binding={"module": "finance-research"},
                allowed_tools=list(scenario.allowed_tools),
                grant=dict(scenario.grant),
            )
            task = Loop(
                harness,
                planner=FinancePlanner(),
                max_rounds=9,
                result_tools=FINANCE_DOMAIN.result_tools,
            ).run(task.id)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.failure["code"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(task.failure["message"], "missing_document")
            events = store.events(task.id)
            stopped = [event for event in events if event["type"] == "task_stopped"]
            self.assertEqual(len(stopped), 1)
            self.assertEqual(stopped[0]["payload"]["detail"]["code"], "MODEL_REPORTED_BLOCKED")
            self.assertEqual(
                [event["payload"]["detail"]["tool"] for event in events if event["type"] == "tool_succeeded"],
                ["report_blocked"],
            )
            store.close()

    def test_budget_block_maps_to_budget_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = get_scenario("budget")
            store = TaskStore(pathlib.Path(tmp) / "task.sqlite")
            world = FinanceWorld()
            provider = BlockedProvider("budget_insufficient")
            harness = Harness(
                store=store,
                registry=ToolRegistry(build_finance_tools(world)),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: verify_observation(world, observation),
            )
            task = harness.create(
                task_id="task-budget-blocked",
                objective=scenario.objective,
                principal="test",
                binding={"module": "finance-research"},
                allowed_tools=list(scenario.allowed_tools),
                grant=dict(scenario.grant),
            )
            task = Loop(
                harness,
                planner=FinancePlanner(),
                max_rounds=9,
                result_tools=FINANCE_DOMAIN.result_tools,
            ).run(task.id)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.failure["code"], "BUDGET_EXHAUSTED")
            self.assertIn("budget", task.failure["message"])
            store.close()


if __name__ == "__main__":
    unittest.main()

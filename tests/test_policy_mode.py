"""Executive–Policy mode: same outcomes, smaller context, explicit fast path."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.hashing import canonical_json  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.runtime.policy import MockPolicy, compile_packet  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def build_stack(tmp: pathlib.Path, scenario_key: str = "full"):
    scenario = get_scenario(scenario_key)
    store = TaskStore(tmp / "task.sqlite")
    world = FinanceWorld(partial=scenario.partial_corpus)
    registry = ToolRegistry(build_finance_tools(world))
    harness = Harness(
        store=store,
        registry=registry,
        provider=MockProvider(policy=scenario.policy),
        world=world,
        limits=scenario.limits,
        receipts=ReceiptStore(store),
        verifier=lambda observation: verify_observation(world, observation),
    )
    task = harness.create(
        task_id=f"task-policy-{scenario.key}",
        objective=scenario.objective,
        principal="test",
        binding={"module": "finance-research", "scenario": scenario.key},
        allowed_tools=list(scenario.allowed_tools),
        grant=dict(scenario.grant),
    )
    return scenario, store, harness, task


class PolicyModeTests(unittest.TestCase):
    def test_full_run_keeps_behavior_and_uses_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario, store, harness, task = build_stack(pathlib.Path(tmp))
            loop = Loop(
                harness,
                planner=FinancePlanner(),
                max_rounds=scenario.max_rounds,
                policy=MockPolicy(),
                result_tools=FINANCE_DOMAIN.result_tools,
            )
            task = loop.run(task.id)
            self.assertEqual(task.status, "succeeded")
            self.assertEqual(task.result["change_bp"], -250)
            events = store.events(task.id)
            types = [event["type"] for event in events]
            requested = types.count("policy_requested")
            decided = types.count("policy_decided")
            auto = types.count("auto_action")
            model_calls = types.count("model_started")
            self.assertEqual(requested, 9)
            self.assertGreaterEqual(auto, 1)
            self.assertEqual(decided, model_calls)
            self.assertEqual(model_calls + auto, 9)
            self.assertLess(model_calls, 9)
            self.assertEqual(task.model.attempts, model_calls)
            store.close()

    def test_open_ended_still_blocks_honestly_under_policy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario, store, harness, task = build_stack(pathlib.Path(tmp), "open-ended")
            loop = Loop(
                harness,
                planner=FinancePlanner(),
                max_rounds=scenario.max_rounds,
                policy=MockPolicy(),
                result_tools=FINANCE_DOMAIN.result_tools,
            )
            task = loop.run(task.id)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.failure["code"], "INSUFFICIENT_EVIDENCE")
            types = [event["type"] for event in store.events(task.id)]
            self.assertIn("policy_requested", types)
            self.assertNotIn("auto_action", types)
            store.close()

    def test_packet_is_materially_smaller_than_the_legacy_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario, store, harness, task = build_stack(pathlib.Path(tmp))
            loop = Loop(
                harness,
                planner=FinancePlanner(),
                policy=MockPolicy(),
                result_tools=FINANCE_DOMAIN.result_tools,
            )
            memory = loop.restore(task)
            plan = loop.rebuild_plan(task, memory)
            budget = harness.budget(task).snapshot()
            packet = compile_packet(
                objective=task.objective,
                round_no=1,
                plan=plan,
                memory=memory,
                registry=harness.registry,
                budget=budget,
            )
            context = memory.build(1, budget, plan)
            packet_bytes = len(canonical_json(packet.to_dict()).encode("utf-8"))
            context_bytes = len(canonical_json(context).encode("utf-8"))
            self.assertLess(packet_bytes, context_bytes * 0.6)
            self.assertLessEqual(len(packet.candidates), 5)
            self.assertTrue(all(candidate.arguments == todo.arguments for candidate, todo in zip(packet.candidates, plan.todos)))
            self.assertEqual(packet.packet_hash, packet.packet_hash)
            store.close()

    def test_auto_step_does_not_charge_or_start_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario, store, harness, task = build_stack(pathlib.Path(tmp))
            step = {"tool": "list_documents", "arguments": {}}
            task = harness.auto_step(task, step, round_no=1, candidate_id="a1", packet_hash="h")
            self.assertEqual(task.status, "planned")
            self.assertEqual(task.model.attempts, 0)
            types = [event["type"] for event in store.events(task.id)]
            self.assertIn("auto_action", types)
            self.assertNotIn("model_started", types)
            store.close()


if __name__ == "__main__":
    unittest.main()

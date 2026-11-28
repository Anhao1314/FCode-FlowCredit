"""S1 operator path: request-id idempotency, consecutive caps, denial neutrality."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_runtime import RunControl  # noqa: E402
from flowagent.core.plan import Plan  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.control import RoundControl, ToolControl  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class _Provider:
    name = "fake"
    model = "fake"

    def propose(self, context):  # noqa: ANN001
        raise AssertionError("not used")


def _setup(tmp: str, *, base: BudgetLimits, approval: str = "off"):
    store = TaskStore(Path(tmp) / "task.sqlite")
    harness = Harness(
        store=store,
        registry=ToolRegistry([]),
        provider=_Provider(),
        world=SimpleNamespace(),
        limits=base,
        receipts=ReceiptStore(store),
        verifier=lambda observation: {"computed": True},
    )
    task = harness.create(
        task_id="task-operator-budget",
        objective="operator budget",
        principal="test",
        binding={"budget_base": base.to_dict(), "rounds_base": 12},
        allowed_tools=["run_command"],
        grant={"allowed_tools": ["run_command"], "allowed_effects": ["read", "write"]},
    )
    run = SimpleNamespace(
        id="run-operator", approval=approval, pending=None,
        lock=threading.Lock(), bump=lambda: None,
    )
    runtime = SimpleNamespace(
        Plan=Plan, BudgetLimits=BudgetLimits,
        RoundControl=RoundControl, ToolControl=ToolControl,
    )
    control = RunControl(
        run, harness, runtime, ("run_command",), 30, base_limits=base,
    )
    return store, harness, task, control


def _command(*, request_id: str, calls: int, reads: int = 30,
             cost: int = 500_000, tokens: int = 120_000) -> dict:
    return {
        "kind": "budget", "maxCalls": calls, "maxReads": reads,
        "maxCostMicrousd": cost, "maxTokens": tokens, "requestId": request_id,
    }


def _apply(control, task, command: dict, round_no: int):
    control.queue(command)
    return control.before_round(
        round_no=round_no, task=task, plan=Plan(goal=task.objective),
    ).task


def _budget_events(store, task_id: str) -> list[dict]:
    return [event for event in store.events(task_id) if event["type"] == "budget_adjusted"]


class OperatorBudgetTests(unittest.TestCase):
    def test_same_request_id_is_idempotent_and_conflicts_are_rejected(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            store, harness, task, control = _setup(tmp, base=base)
            task = _apply(control, task, _command(request_id="op-1", calls=15), 1)
            self.assertEqual(harness.limits.max_calls, 15)
            self.assertEqual(len(_budget_events(store, task.id)), 1)

            # The HTTP retry replays the same absolute request; the grant was
            # capped at 15, so the retry must be a no-op, not a second grant.
            task = _apply(control, task, _command(request_id="op-1", calls=15), 2)
            self.assertEqual(harness.limits.max_calls, 15)
            self.assertEqual(len(_budget_events(store, task.id)), 1)

            # Same id, different limits: reject without touching the envelope.
            task = _apply(control, task, _command(request_id="op-1", calls=16), 3)
            events = store.events(task.id)
            self.assertEqual(events[-1]["type"], "control_rejected")
            self.assertIn("requestId", events[-1]["payload"]["detail"]["reason"])
            self.assertEqual(harness.limits.max_calls, 15)
            self.assertEqual(len(_budget_events(store, task.id)), 1)
            store.close()

    def test_consecutive_requests_share_one_cumulative_cap(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            store, harness, task, control = _setup(tmp, base=base)
            # One request can add at most 50% of base (6 calls / 60k tokens).
            task = _apply(control, task, _command(request_id="c1", calls=18), 1)
            self.assertEqual(harness.limits.max_calls, 18)
            task = _apply(control, task, _command(request_id="c2", calls=24), 2)
            self.assertEqual(harness.limits.max_calls, 24)
            # Third request has no headroom left: rejected, not negative.
            task = _apply(control, task, _command(request_id="c3", calls=30), 3)
            last = store.events(task.id)[-1]
            self.assertEqual(last["type"], "control_rejected")
            self.assertIn("headroom", last["payload"]["detail"]["reason"])
            self.assertEqual(harness.limits.max_calls, 24)

            # Same formula for tokens: 120k -> 180k -> 240k -> exhausted.
            base_tokens = harness.limits.max_tokens
            self.assertEqual(base_tokens, 120_000)
            task = _apply(control, task, _command(request_id="t1", calls=24, tokens=180_000), 4)
            self.assertEqual(harness.limits.max_tokens, 180_000)
            task = _apply(control, task, _command(request_id="t2", calls=24, tokens=240_000), 5)
            self.assertEqual(harness.limits.max_tokens, 240_000)
            task = _apply(control, task, _command(request_id="t3", calls=24, tokens=300_000), 6)
            self.assertIn("headroom", store.events(task.id)[-1]["payload"]["detail"]["reason"])
            self.assertEqual(harness.limits.max_tokens, 240_000)
            store.close()

    def test_capped_request_replay_matches_the_asked_total(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            store, harness, task, control = _setup(tmp, base=base)
            # Ask for +20 calls; policy grants only +6 (18 effective).
            task = _apply(control, task, _command(request_id="cap-1", calls=32), 1)
            self.assertEqual(harness.limits.max_calls, 18)
            self.assertEqual(len(_budget_events(store, task.id)), 1)
            # Retrying the same capped ask must be a no-op even though the
            # stored `after` (18) differs from the asked total (32).
            task = _apply(control, task, _command(request_id="cap-1", calls=32), 2)
            self.assertEqual(harness.limits.max_calls, 18)
            self.assertEqual(len(_budget_events(store, task.id)), 1)
            store.close()

    def test_denied_approval_leaves_budget_and_memory_untouched(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            store, harness, task, control = _setup(tmp, base=base, approval="consequential")
            task = harness.transition(task, "planning", "model_started", {"round": 0})
            task = harness.transition(task, "planned", "plan_validated", {"plan": {}})
            step = {"tool": "run_command", "arguments": {"command": "npm", "args": ["test"]}}
            task, approval = control.prepare_approval(round_no=1, task=task, step=step)
            self.assertIsNotNone(approval)
            task, allowed, flag = control.apply_approval_response(
                round_no=1, task=task, step=step,
                response={"allow": False, "remember": True},
            )
            self.assertFalse(allowed)
            self.assertEqual(flag["code"], "OPERATOR_DENIED")
            self.assertEqual(harness.limits, base)
            self.assertEqual(_budget_events(store, task.id), [])
            self.assertEqual(control._remembered, set())
            store.close()


if __name__ == "__main__":
    unittest.main()

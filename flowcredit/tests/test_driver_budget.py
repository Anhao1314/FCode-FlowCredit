"""S1 driver replay: immutable base, strict chain order, resume caps, rounds independence."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from console_run_driver import RunDriver  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.runtime.budget import BudgetLimits, plan_adjustment  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class _Provider:
    name = "fake"
    model = "fake"

    def propose(self, context):  # noqa: ANN001
        raise AssertionError("not used")


def _make_task(tmp: str, base: BudgetLimits, rounds_base: int = 12):
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
        task_id="task-budget",
        objective="budget replay",
        principal="test",
        binding={"budget_base": base.to_dict(), "rounds_base": rounds_base},
        allowed_tools=["noop"],
        grant={"allowed_tools": ["noop"], "allowed_effects": ["read"]},
    )
    return harness, store, task


def _driver_skeleton(store, base: BudgetLimits, *, task_id: str, resume_limits=None,
                     rounds_base: int = 12, resume_request_id="resume-1"):
    driver = RunDriver.__new__(RunDriver)
    driver.store = store
    driver.rt = SimpleNamespace(BudgetLimits=BudgetLimits)
    driver.scenario = SimpleNamespace(max_rounds=rounds_base, limits=base)
    driver.run = SimpleNamespace(id="run-budget", task_id=task_id, spec={})
    driver.fast = False
    driver.base_limits = base
    driver.limits = base
    driver.resume_limits = resume_limits
    driver.resume_request_id = resume_request_id
    driver._rounds_base = rounds_base
    driver._budget_diagnostics = []
    driver._resume_adjustment = None
    driver._resume_rounds = None
    driver._resume_duplicate = False
    return driver


class DriverBudgetReplayTests(unittest.TestCase):
    def test_binding_base_wins_and_chain_rebuilds_effective(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task = _make_task(tmp, base)
            adjusted = plan_adjustment(base, base, delta={"tokens": 60_000})
            task = harness.record(task, "budget_adjusted", {
                "policy_version": 2,
                "request_id": "r1",
                "before": adjusted.before.to_dict(),
                "after": adjusted.after.to_dict(),
                "requested": adjusted.requested,
                "granted": adjusted.granted,
                "capped": adjusted.capped,
            })
            # Even if the freshly computed envelope is different, the binding
            # (created after price scaling) is the immutable base.
            driver = _driver_skeleton(store, BudgetLimits(999, 999, 9_999_999, 999_999),
                                      task_id=task.id)
            driver._replay_existing_budget()
            self.assertEqual(driver.base_limits.max_tokens, 120_000)
            self.assertEqual(driver.limits.max_tokens, 180_000)
            # A resume asking for +18 calls is capped by the binding base
            # (single grant <= 6), never by the 999-call scenario envelope.
            driver.resume_limits = {
                "maxCalls": 30, "maxReads": 30,
                "maxCostMicrousd": 500_000, "maxTokens": 180_000,
            }
            driver._replay_existing_budget()
            self.assertEqual(driver.base_limits, base)
            self.assertEqual(driver.limits.max_calls, 18)
            store.close()

    def test_resume_cannot_exceed_the_cumulative_cap(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task = _make_task(tmp, base)
            first = plan_adjustment(base, base, delta={"tokens": 60_000})
            task = harness.record(task, "budget_adjusted", {
                "policy_version": 2, "request_id": "r1",
                "before": first.before.to_dict(), "after": first.after.to_dict(),
                "granted": first.granted, "requested": first.requested,
            })
            second = plan_adjustment(base, first.after, delta={"tokens": 60_000})
            task = harness.record(task, "budget_adjusted", {
                "policy_version": 2, "request_id": "r2",
                "before": second.before.to_dict(), "after": second.after.to_dict(),
                "granted": second.granted, "requested": second.requested,
            })
            driver = _driver_skeleton(
                store, base, task_id=task.id,
                resume_limits={"maxCalls": 12, "maxReads": 30,
                               "maxCostMicrousd": 500_000, "maxTokens": 300_000},
            )
            with self.assertRaises(ValueError):
                driver._replay_existing_budget()
            store.close()

    def test_missing_resume_tokens_default_to_effective_not_scenario(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task = _make_task(tmp, base)
            adjusted = plan_adjustment(base, base, delta={"tokens": 60_000})
            task = harness.record(task, "budget_adjusted", {
                "policy_version": 2, "request_id": "r1",
                "before": adjusted.before.to_dict(), "after": adjusted.after.to_dict(),
                "granted": adjusted.granted, "requested": adjusted.requested,
            })
            driver = _driver_skeleton(
                store, base, task_id=task.id,
                resume_limits={"maxCalls": 12, "maxReads": 30, "maxCostMicrousd": 500_000,
                               "extraRounds": 6},
            )
            driver._replay_existing_budget()
            self.assertEqual(driver.limits.max_tokens, 180_000)
            self.assertEqual(driver._resume_adjustment.granted["tokens"], 0)
            store.close()

    def test_sigkill_after_adjustment_recovers_the_same_chain(self) -> None:
        """A real SIGKILL after COMMIT must leave both grants durable.

        The parent reconstructs from the immutable binding and the event
        chain, not from any in-memory limit or a re-run of price scaling.
        """
        script = r"""
import os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from flowagent.core.receipts import ReceiptStore
from flowagent.core.store import TaskStore
from flowagent.runtime.budget import BudgetLimits, plan_adjustment
from flowagent.runtime.harness import Harness
from flowagent.tools.registry import ToolRegistry

class P:
    name = "fake"
    model = "fake"

base = BudgetLimits(12, 30, 500_000, 120_000)
store = TaskStore(Path(sys.argv[1]))
harness = Harness(
    store=store, registry=ToolRegistry([]), provider=P(),
    world=SimpleNamespace(), limits=base, receipts=ReceiptStore(store),
    verifier=lambda observation: {"computed": True},
)
task = harness.create(
    task_id="task-sigkill-budget", objective="sigkill budget", principal="test",
    binding={"budget_base": base.to_dict(), "rounds_base": 12},
    allowed_tools=["noop"], grant={"allowed_tools": ["noop"], "allowed_effects": ["read"]},
)
for request_id in ("crash-1", "crash-2"):
    current = harness.limits
    adjustment = plan_adjustment(base, current, delta={"tokens": 60_000})
    task = harness.record(task, "budget_adjusted", {
        "policy_version": 2, "request_id": request_id,
        "before": adjustment.before.to_dict(), "after": adjustment.after.to_dict(),
        "requested": adjustment.requested, "granted": adjustment.granted,
        "capped": adjustment.capped,
    })
    harness.limits = adjustment.after
sys.stdout.write("committed\n")
sys.stdout.flush()
os.kill(os.getpid(), signal.SIGKILL)
"""
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "task.sqlite"
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            completed = subprocess.run(
                [sys.executable, "-c", script, str(db)],
                capture_output=True, text=True, timeout=60, env=env,
            )
            self.assertLess(completed.returncode, 0, completed.stderr)
            self.assertIn("committed", completed.stdout)
            store = TaskStore(db)
            task = store.load("task-sigkill-budget")
            self.assertEqual(task.binding["budget_base"], base.to_dict())
            events = store.events(task.id)
            self.assertEqual(
                [event["payload"]["detail"].get("request_id") for event in events
                 if event["type"] == "budget_adjusted"],
                ["crash-1", "crash-2"],
            )
            store.verify_chain(task.id)
            # The scenario envelope is deliberately different from the binding:
            # recovery must start from the binding, never re-scale prices.
            driver = _driver_skeleton(
                store, BudgetLimits(999, 999, 9_999_999, 999_999),
                task_id=task.id,
            )
            driver._replay_existing_budget()
            self.assertEqual(driver.base_limits, base)
            self.assertEqual(driver.limits.max_tokens, 240_000)
            self.assertEqual(driver.limits.max_calls, 12)
            # A resume on top of the recovered chain cannot buy a new 2x cap.
            driver.resume_limits = {
                "maxCalls": 12, "maxReads": 30,
                "maxCostMicrousd": 500_000, "maxTokens": 300_000,
            }
            with self.assertRaises(ValueError):
                driver._replay_existing_budget()
            store.close()

    def test_resume_rounds_use_their_own_base_and_cumulative_cap(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task = _make_task(tmp, base)
            task = harness.record(task, "task_resumed", {
                "by": "operator",
                "rounds": {"base": 12, "granted_before": 0, "requested": 6,
                           "granted": 6, "capped": False, "max_rounds": 12},
            })
            # First resume asks for 6 more; only 6 headroom remain (cumulative 12).
            driver = _driver_skeleton(
                store, base, task_id=task.id,
                resume_limits={"maxCalls": 12, "maxReads": 30, "maxCostMicrousd": 500_000,
                               "extraRounds": 6},
            )
            driver._replay_existing_budget()
            rounds = driver._resume_rounds
            self.assertEqual(rounds["granted"], 6)
            self.assertEqual(rounds["max_rounds"], max(12, task.model.attempts + 6))
            # Exhausting the cumulative rounds cap clamps to zero, not negative.
            task = harness.record(task, "task_resumed", {
                "by": "operator",
                "rounds": {"base": 12, "granted_before": 6, "requested": 6,
                           "granted": 6, "capped": False, "max_rounds": 12},
            })
            driver2 = _driver_skeleton(
                store, base, task_id=task.id,
                resume_limits={"maxCalls": 12, "maxReads": 30, "maxCostMicrousd": 500_000,
                               "extraRounds": 6},
                resume_request_id="resume-2",
            )
            driver2._replay_existing_budget()
            self.assertEqual(driver2._resume_rounds["granted"], 0)
            self.assertTrue(driver2._resume_rounds["capped"])
            store.close()

    def test_replayed_resume_request_does_not_regrant_rounds(self) -> None:
        base = BudgetLimits(12, 30, 500_000, 120_000)
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task = _make_task(tmp, base)
            task = harness.record(task, "task_resumed", {
                "request_id": "resume-dup",
                "by": "operator",
                "rounds": {"base": 12, "granted_before": 0, "requested": 6,
                           "granted": 6, "capped": False, "max_rounds": 18},
            })
            driver = _driver_skeleton(
                store, base, task_id=task.id,
                resume_limits={"maxCalls": 12, "maxReads": 30, "maxCostMicrousd": 500_000,
                               "maxTokens": 120_000, "extraRounds": 6},
                resume_request_id="resume-dup",
            )
            driver._replay_existing_budget()
            self.assertTrue(driver._resume_duplicate)
            self.assertEqual(driver._resume_rounds["granted"], 0)
            self.assertEqual(driver._resume_rounds["max_rounds"], 18)
            # The same id with a different rounds ask is a different request.
            driver2 = _driver_skeleton(
                store, base, task_id=task.id,
                resume_limits={"maxCalls": 12, "maxReads": 30, "maxCostMicrousd": 500_000,
                               "maxTokens": 120_000, "extraRounds": 10},
                resume_request_id="resume-dup",
            )
            with self.assertRaises(ValueError):
                driver2._replay_existing_budget()
            store.close()


if __name__ == "__main__":
    unittest.main()

"""S1 atomicity: tool success + budget audit commit together; precheck never dispatches."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.runtime.budget import BudgetLimits, replay_limits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.base import EFFECT_READ, ToolAudit, ToolSpec  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class CountingProvider:
    name = "fake"
    model = "fake"
    reserve_tokens = 5
    reserve_cost_microusd = 1

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, context):  # noqa: ANN001
        self.calls += 1
        raise AssertionError("the provider must not be called after a rejected reservation")


def _harness(tmp: str, specs: list[ToolSpec], limits: BudgetLimits | None = None):
    store = TaskStore(Path(tmp) / "task.sqlite")
    provider = CountingProvider()
    harness = Harness(
        store=store,
        registry=ToolRegistry(specs),
        provider=provider,
        world=SimpleNamespace(),
        limits=limits or BudgetLimits(4, 8, 10_000, 10_000),
        receipts=ReceiptStore(store),
        verifier=lambda observation: {"computed": True},
    )
    task = harness.create(
        task_id="task-atomic",
        objective="audit probe",
        principal="test",
        binding={},
        allowed_tools=[spec.name for spec in specs] or ["noop"],
        grant={"allowed_tools": [spec.name for spec in specs], "allowed_effects": ["read"]},
    )
    return harness, store, task, provider


class AtomicAuditTests(unittest.TestCase):
    def test_tool_success_and_audit_commit_together_then_apply(self) -> None:
        applied: list[str] = []
        spec = ToolSpec(
            name="audit_probe",
            summary="probe",
            effect=EFFECT_READ,
            params=(),
            handler=lambda arguments: {"ok": True},
            audit=lambda result: ToolAudit(
                event="budget_adjusted",
                detail={"policy_version": 2, "request_id": "probe-1"},
                apply=lambda: applied.append("applied"),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task, _ = _harness(tmp, [spec])
            task, _ = harness.execute(task, {"tool": "audit_probe", "arguments": {}}, round_no=1)
            types = [event["type"] for event in store.events(task.id)]
            self.assertEqual(types[-2:], ["tool_succeeded", "budget_adjusted"])
            self.assertEqual(applied, ["applied"], "apply runs only after the commit")
            store.close()

    def test_audit_failure_rolls_back_both_events_and_skips_apply(self) -> None:
        applied: list[str] = []
        spec = ToolSpec(
            name="audit_probe",
            summary="probe",
            effect=EFFECT_READ,
            params=(),
            handler=lambda arguments: {"ok": True},
            audit=lambda result: ToolAudit(
                event="budget_adjusted",
                detail={"policy_version": 2, "broken": 1.5},  # canonical JSON rejects floats
                apply=lambda: applied.append("applied"),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task, _ = _harness(tmp, [spec])
            before = len(store.events(task.id))
            with self.assertRaises((AgentError, TypeError)):
                harness.execute(task, {"tool": "audit_probe", "arguments": {}}, round_no=1)
            after = store.events(task.id)
            self.assertEqual(len(after), before + 1, "tool_started committed, tool_succeeded must not")
            self.assertEqual(after[-1]["type"], "tool_started")
            self.assertEqual(applied, [])
            reloaded = store.load(task.id)
            self.assertEqual(reloaded.sequence, after[-1]["sequence"])
            store.close()


class PrecheckTests(unittest.TestCase):
    def test_rejected_reservation_never_dispatches_the_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task, provider = _harness(
                tmp, [], limits=BudgetLimits(0, 8, 10_000, 10_000))
            with self.assertRaises(AgentError) as ctx:
                harness.propose(task, {"round": 1})
            error = ctx.exception
            self.assertEqual(error.code, "BUDGET_EXCEEDED")
            self.assertEqual(provider.calls, 0)
            self.assertFalse(error.details["provider_request_sent"])
            blocked = error.details["blocked_dimensions"]
            self.assertEqual(blocked[0]["dimension"], "calls")
            self.assertEqual(blocked[0]["used"], 0)
            self.assertEqual(blocked[0]["requested_reserve"], 1)
            store.close()


class ResumeAtomicityTests(unittest.TestCase):
    def test_resume_and_budget_event_commit_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness, store, task, _ = _harness(tmp, [])
            task = harness.transition(task, "planning", "model_started", {"round": 0})
            failed = harness.finalize(
                task, "failed",
                failure={"code": "BUDGET_EXCEEDED", "message": "stop", "retryable": False},
                event="task_failed", detail={"code": "BUDGET_EXCEEDED"},
            )
            resumed = harness.resume_with_budget(
                failed,
                resume_detail={"by": "operator", "limits": {"max_tokens": 180_000}},
                budget_detail={"policy_version": 2, "request_id": "resume-1",
                               "after": {"max_tokens": 180_000}},
            )
            types = [event["type"] for event in store.events(resumed.id)]
            self.assertEqual(types[-2:], ["task_resumed", "budget_adjusted"])
            self.assertEqual(resumed.status, "running")
            self.assertIsNone(resumed.failure)
            store.verify_chain(resumed.id)
            store.close()


class UnknownReservationTests(unittest.TestCase):
    def test_unknown_provider_outcome_keeps_the_charge_and_never_grants(self) -> None:
        class UnknownProvider:
            name = "fake"
            model = "fake"
            reserve_tokens = 1_234
            reserve_cost_microusd = 77

            def propose(self, context):  # noqa: ANN001
                raise AgentError("PROVIDER_TIMEOUT", "no answer")

        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "task.sqlite")
            base = BudgetLimits(4, 8, 10_000, 10_000)
            harness = Harness(
                store=store,
                registry=ToolRegistry([]),
                provider=UnknownProvider(),
                world=SimpleNamespace(),
                limits=base,
                receipts=ReceiptStore(store),
                verifier=lambda observation: {"computed": True},
            )
            task = harness.create(
                task_id="task-unknown-budget",
                objective="unknown reservation",
                principal="test",
                binding={"budget_base": base.to_dict()},
                allowed_tools=["noop"],
                grant={"allowed_tools": ["noop"], "allowed_effects": ["read"]},
            )
            task, proposal = harness.propose(task, {"round": 1})
            self.assertIsNone(proposal)
            self.assertEqual(task.status, "recovery_required")
            self.assertEqual(task.failure["code"], "MODEL_OUTCOME_UNKNOWN")
            usage = harness.budget(task).snapshot()
            # The reservation is consumed before dispatch and is never refunded
            # because the outcome is unknown.
            self.assertEqual(usage["calls_used"], 1)
            self.assertEqual(usage["tokens_used"], 1_234)
            self.assertEqual(usage["cost_used_microusd"], 77)
            events = store.events(task.id)
            self.assertNotIn("budget_adjusted", [event["type"] for event in events])
            self.assertNotIn("task_resumed", [event["type"] for event in events])
            effective, diagnostics = replay_limits(base, events)
            self.assertEqual(effective, base)
            self.assertEqual(diagnostics, [])
            store.verify_chain(task.id)
            store.close()


if __name__ == "__main__":
    unittest.main()

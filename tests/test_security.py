"""Unit-level security properties.

Each test here targets one guarantee the README claims. They are deliberately
small and direct: if a property is only tested through the demo, a regression can
hide behind the demo's happy path.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError, Task  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import Budget, BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.permissions import Permissions  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import TraceTampered, verify_trace  # noqa: E402
from flowagent.domains.finance import verify_observation as verify_finance


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.store = TaskStore(self.tmp / "task.sqlite")
        self.world = FinanceWorld()
        self.registry = ToolRegistry(build_finance_tools(self.world))
        self.receipts = ReceiptStore(self.store)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class PermissionGateTest(_Base):
    def _gate(self, tool: str, arguments: dict, *, allowed: list[str] | None = None, grant: list[str] | None = None):
        permissions = Permissions(
            self.registry,
            allowed if allowed is not None else list(self.registry.names),
            {"allowed_tools": grant if grant is not None else list(self.registry.names)},
        )
        return permissions.gate(tool, arguments)

    def test_unregistered_tool_is_denied(self) -> None:
        decision = self._gate("write_document", {"doc_id": "doc_fy2024"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "TOOL_NOT_REGISTERED")

    def test_tool_outside_the_task_scope_is_denied(self) -> None:
        decision = self._gate("calculate_margin", {}, allowed=["list_documents"])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "TOOL_NOT_IN_TASK_SCOPE")

    def test_tool_outside_the_current_grant_is_denied(self) -> None:
        decision = self._gate("calculate_margin", {}, grant=["list_documents"])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "TOOL_NOT_IN_GRANT")

    def test_bad_arguments_are_denied_not_coerced(self) -> None:
        decision = self._gate("read_document", {"doc_id": "doc_does_not_exist"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "TOOL_ARGUMENTS_INVALID")

    def test_extra_argument_is_rejected(self) -> None:
        decision = self._gate("read_document", {"doc_id": "doc_fy2024", "limit": 5})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "TOOL_ARGUMENTS_INVALID")


class BudgetTest(_Base):
    def test_call_is_charged_before_dispatch(self) -> None:
        budget = Budget(self.store, "scope", BudgetLimits(1, 1, 1_000, 1_000))
        budget.charge_call(tokens=100, cost_microusd=100)
        self.assertEqual(budget.snapshot()["calls_used"], 1)
        with self.assertRaises(AgentError) as caught:
            budget.charge_call(tokens=100, cost_microusd=100)
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")

    def test_cost_ceiling_is_enforced_separately(self) -> None:
        budget = Budget(self.store, "scope", BudgetLimits(10, 10, 150, 10_000))
        budget.charge_call(tokens=1, cost_microusd=100)
        with self.assertRaises(AgentError) as caught:
            budget.charge_call(tokens=1, cost_microusd=100)
        self.assertEqual(caught.exception.details.get("dimension"), "cost")

    def test_read_budget_is_enforced(self) -> None:
        budget = Budget(self.store, "scope", BudgetLimits(10, 1, 1_000, 1_000))
        budget.charge_read()
        with self.assertRaises(AgentError) as caught:
            budget.charge_read()
        self.assertEqual(caught.exception.details.get("dimension"), "reads")


class StoreIntegrityTest(_Base):
    def test_edited_event_is_detected(self) -> None:
        self.store.create(Task(id="task-1", principal="p", objective="o"))
        self.store._conn.execute("UPDATE event SET type='forged' WHERE task_id='task-1'")
        with self.assertRaises(AgentError) as caught:
            self.store.load("task-1")
        self.assertEqual(caught.exception.code, "CHAIN_TAMPERED")

    def test_illegal_transition_is_rejected(self) -> None:
        task = self.store.create(Task(id="task-2", principal="p", objective="o"))
        with self.assertRaises(AgentError) as caught:
            task.advance("succeeded")
        self.assertEqual(caught.exception.code, "ILLEGAL_STATE_TRANSITION")

    def test_stale_writer_loses_the_compare_and_swap(self) -> None:
        task = self.store.create(Task(id="task-3", principal="p", objective="o"))
        first = self.store.save(task.advance("planning"), task.sequence, "model_started", {})
        with self.assertRaises(AgentError) as caught:
            self.store.save(task.advance("planning"), task.sequence, "model_started", {})
        self.assertEqual(caught.exception.code, "SEQUENCE_CONFLICT")
        self.assertEqual(first.sequence, 2)


class ReceiptTest(_Base):
    def test_a_receipt_cannot_be_forged(self) -> None:
        intent = {"task_id": "task-x", "round": 1}
        plan = {"version": "flowagent.plan/v1", "steps": [{"id": "s1", "tool": "list_documents", "arguments": {}}]}
        ref = self.receipts.issue(intent=intent, plan=plan, usage={"provider": "mock"})
        # Rewrite the stored payload but leave the signature alone.
        self.store._conn.execute("UPDATE receipt SET payload=? WHERE ref=?", ('{"version":"x"}', ref))
        with self.assertRaises(AgentError) as caught:
            self.receipts.load(ref)
        self.assertEqual(caught.exception.code, "MODEL_RECEIPT_UNTRUSTED")

    def test_a_missing_reference_is_refused(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.receipts.load("0" * 64)
        self.assertEqual(caught.exception.code, "MODEL_RECEIPT_REQUIRED")


class IndependentVerificationTest(_Base):
    def _harness(self) -> Harness:
        return Harness(
            store=self.store,
            registry=self.registry,
            provider=MockProvider(),
            world=self.world,
            verifier=lambda observation: verify_finance(self.world, observation),
            limits=BudgetLimits(10, 20, 20_000, 20_000),
            receipts=self.receipts,
        )

    def test_a_tampered_observation_fails_verification(self) -> None:
        # Prepare the world so the real calculation succeeds, then hand the
        # verifier a result that claims a different margin.
        self.world.read_document("doc_fy2024")
        self.world.read_document("doc_fy2025")
        self.world.check_restatement("doc_fy2024")
        self.world.reconcile_basis("doc_fy2025_adjusted")
        self.world.normalize_units("doc_fy2024_restated")
        self.world.normalize_units("doc_fy2025")
        genuine = self.world.calculate_margin()
        self.assertTrue(genuine["computed"])

        forged = dict(genuine)
        forged["change_bp"] = genuine["change_bp"] + 100
        observation = Observation(tool="calculate_margin", arguments={}, result=forged)
        with self.assertRaises(AgentError) as caught:
            self._harness().verify(observation)
        self.assertEqual(caught.exception.code, "RESULT_VERIFICATION_FAILED")

    def test_a_non_computed_observation_cannot_be_verified(self) -> None:
        observation = Observation(tool="calculate_margin", arguments={}, result={"computed": False})
        with self.assertRaises(AgentError) as caught:
            self._harness().verify(observation)
        self.assertEqual(caught.exception.code, "RESULT_INCOMPLETE")


class TraceTamperTest(unittest.TestCase):
    def test_an_edited_trace_event_is_rejected(self) -> None:
        trace = {
            "events": [
                {
                    "sequence": 1,
                    "type": "task_created",
                    "payload": {"body_hash": "x", "detail": {}},
                    "prev_hash": None,
                    "hash": "0" * 64,
                    "at": "2026-01-01T00:00:00.000Z",
                }
            ],
            "chain": {"event_count": 1, "head_hash": "0" * 64},
        }
        with self.assertRaises(TraceTampered) as caught:
            verify_trace(trace)
        self.assertEqual(caught.exception.code, "TRACE_TAMPERED")

    def test_a_removed_trace_event_is_rejected(self) -> None:
        trace = {"events": [], "chain": {"event_count": 1, "head_hash": "0" * 64}}
        with self.assertRaises(TraceTampered) as caught:
            verify_trace(trace)
        self.assertEqual(caught.exception.code, "TRACE_EMPTY")


if __name__ == "__main__":
    unittest.main()

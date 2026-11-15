"""unknown 结果的恢复出口：一次显式、留痕、可审计的操作员动作。

钉死三件事：
1. ``find_intent`` 只认同 task 同 round 的**验签通过**回执，坏行不阻断扫描；
2. 无回执 → ``model_reissued``（含风险声明），旧轮保持 unknown，下一轮是全新计费；
3. 有回执 → ``model_reconciled``，不重发、不新增 paid call、attempts 不变。

另外用状态机测试钉死语义：unknown 只能由回执（validated）或显式操作员
恢复（reissued）解决，不存在 unknown -> started / unknown -> succeeded。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import (  # noqa: E402
    MODEL_TRANSITIONS,
    PLAN_VERSION,
    AgentError,
)
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _plan(step_id: str = "step-1") -> dict:
    return {
        "version": PLAN_VERSION,
        "steps": [{"id": step_id, "tool": "ls_workspace", "arguments": {}}],
    }


def _proposal() -> Proposal:
    return Proposal(
        plan=_plan("step-next"),
        provider="scripted",
        model="scripted-v1",
        tokens=120,
        cost_microusd=90,
    )


class ScriptedProvider:
    """Outcomes are consumed in order: exceptions raise, proposals return."""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def propose(self, context):  # noqa: ARG002 - signature matches Provider
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RecoveryStack:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self._tmp.name) / "task.sqlite")
        self.receipts = ReceiptStore(self.store)
        self.harness = Harness(
            store=self.store,
            registry=ToolRegistry([]),
            provider=ScriptedProvider(_proposal()),
            world=None,
            limits=BudgetLimits(10, 10, 100_000, 100_000),
            receipts=self.receipts,
        )
        self.task = self.harness.create(
            task_id="task-unknown",
            objective="recover an unknown outcome",
            principal="test",
            binding={},
            allowed_tools=["ls_workspace"],
            grant={},
        )

    def close(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def unknown(self, *, task_id: str = "task-unknown"):
        """Drive one round into recovery_required / MODEL_OUTCOME_UNKNOWN."""
        provider = ScriptedProvider(
            AgentError("MODEL_CALL_FAILED", "dropped transport"),
            AgentError("MODEL_CALL_FAILED", "dropped transport again"),
        )
        task, proposal = self.harness._propose(
            self.task, {"round": 1, "state": {}}, provider,
        )
        assert proposal is None
        assert task.status == "recovery_required"
        assert task.failure["code"] == "MODEL_OUTCOME_UNKNOWN"
        return task

    def events(self, task):
        return self.store.events(task.id)

    def receipt_rows(self):
        return [
            tuple(row)
            for row in self.store._conn.execute(
                "SELECT ref, payload, signature FROM receipt ORDER BY ref"
            ).fetchall()
        ]


class FindIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = RecoveryStack()

    def tearDown(self) -> None:
        self.stack.close()

    def test_exact_task_and_round_match(self) -> None:
        ref = self.stack.receipts.issue(
            intent={"task_id": "task-unknown", "round": 3},
            plan=_plan(), usage={"provider": "scripted", "tokens": 10, "cost_microusd": 5},
        )
        self.assertEqual(self.stack.receipts.find_intent("task-unknown", 3), ref)
        self.assertEqual(ref, self.stack.receipts.expected_ref(
            {"task_id": "task-unknown", "round": 3}, _plan(),
        ))

    def test_other_task_or_other_round_never_match(self) -> None:
        self.stack.receipts.issue(
            intent={"task_id": "another-task", "round": 3},
            plan=_plan(), usage={},
        )
        self.stack.receipts.issue(
            intent={"task_id": "task-unknown", "round": 4},
            plan=_plan("step-4"), usage={},
        )
        self.assertEqual(self.stack.receipts.find_intent("task-unknown", 3), "")

    def test_forged_signature_is_skipped_and_does_not_block_a_good_row(self) -> None:
        bad = self.stack.receipts.issue(
            intent={"task_id": "task-unknown", "round": 3},
            plan=_plan("step-bad"), usage={},
        )
        self.stack.store._conn.execute(
            "UPDATE receipt SET signature=? WHERE ref=?", ("f" * 64, bad),
        )
        good = self.stack.receipts.issue(
            intent={"task_id": "task-unknown", "round": 3},
            plan=_plan("step-good"), usage={},
        )
        self.assertEqual(self.stack.receipts.find_intent("task-unknown", 3), good)
        self.assertNotEqual(bad, good)

    def test_only_forged_rows_produce_no_match(self) -> None:
        ref = self.stack.receipts.issue(
            intent={"task_id": "task-unknown", "round": 3},
            plan=_plan(), usage={},
        )
        self.stack.store._conn.execute(
            "UPDATE receipt SET signature=? WHERE ref=?", ("0" * 64, ref),
        )
        self.assertEqual(self.stack.receipts.find_intent("task-unknown", 3), "")

    def test_corrupt_payload_is_skipped(self) -> None:
        ref = self.stack.receipts.issue(
            intent={"task_id": "task-unknown", "round": 3},
            plan=_plan(), usage={},
        )
        self.stack.store._conn.execute(
            "UPDATE receipt SET payload=? WHERE ref=?", ("{not json", ref),
        )
        self.assertEqual(self.stack.receipts.find_intent("task-unknown", 3), "")


class StateMachineTests(unittest.TestCase):
    def test_unknown_has_exactly_two_exits(self) -> None:
        self.assertEqual(
            MODEL_TRANSITIONS["unknown"],
            ("unknown", "validated", "failed", "reissued"),
        )
        self.assertEqual(MODEL_TRANSITIONS["reissued"], ("reissued", "started"))
        self.assertNotIn("started", MODEL_TRANSITIONS["unknown"])
        self.assertNotIn("succeeded", MODEL_TRANSITIONS["unknown"])

    def test_unknown_cannot_be_guessed_successful_or_silently_resent(self) -> None:
        stack = RecoveryStack()
        try:
            task = stack.unknown()
            with self.assertRaises(AgentError) as caught:
                task.with_model(status="started")
            self.assertEqual(caught.exception.code, "ILLEGAL_MODEL_TRANSITION")
            with self.assertRaises(AgentError):
                task.advance("succeeded")
        finally:
            stack.close()


class RecoverUnknownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = RecoveryStack()

    def tearDown(self) -> None:
        self.stack.close()

    def test_no_receipt_reissues_a_fresh_round_with_an_audit_event(self) -> None:
        task = self.stack.unknown()
        # Two older, unrelated receipts must survive the recovery byte-for-byte.
        self.stack.receipts.issue(
            intent={"task_id": task.id, "round": 0}, plan=_plan("r0"), usage={},
        )
        self.stack.receipts.issue(
            intent={"task_id": task.id, "round": 2}, plan=_plan("r2"), usage={},
        )
        before_rows = self.stack.receipt_rows()
        calls_before = self.stack.harness.budget(task).snapshot()["calls_used"]

        recovered = self.stack.harness.recover_unknown(task, operator="console-operator")

        self.assertEqual(recovered.status, "running")
        self.assertIsNone(recovered.failure)
        self.assertEqual(recovered.model.status, "reissued")
        self.assertEqual(recovered.model.attempts, 1, "recovery itself must not price a round")
        self.assertEqual(self.stack.harness.budget(recovered).snapshot()["calls_used"],
                         calls_before)
        events = self.stack.events(recovered)
        self.assertIn("model_reissued", [event["type"] for event in events])
        issued = [event for event in events if event["type"] == "model_reissued"]
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0]["payload"]["detail"], {
            "by": "console-operator",
            "unknown_round": 1,
            "receipt_ref": "",
            "risk": "possible double billing",
        })
        self.assertNotIn("model_reconciled", [event["type"] for event in events])
        self.assertEqual(self.stack.receipt_rows(), before_rows)

        # The next round is a brand-new, separately priced dispatch.
        provider = ScriptedProvider(_proposal())
        task2, proposal = self.stack.harness._propose(
            recovered, {"round": 2, "state": {}}, provider,
        )
        self.assertIsNotNone(proposal)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(task2.model.attempts, 2)
        self.assertEqual(task2.model.status, "validated")
        self.assertEqual(
            self.stack.harness.budget(task2).snapshot()["calls_used"], calls_before + 1,
        )
        self.assertEqual(self.stack.receipt_rows(), before_rows)
        types = [event["type"] for event in self.stack.events(task2)]
        self.assertEqual(types.count("model_reissued"), 1)
        self.assertIn("model_started", types)
        self.assertIn("plan_validated", types)

    def test_matching_receipt_reconciles_without_a_new_paid_call(self) -> None:
        task = self.stack.unknown()
        ref = self.stack.receipts.issue(
            intent={"task_id": task.id, "round": 1},
            plan=_plan("reconciled-step"),
            usage={"provider": "scripted", "tokens": 10, "cost_microusd": 5},
        )
        calls_before = self.stack.harness.budget(task).snapshot()["calls_used"]
        before_rows = self.stack.receipt_rows()

        recovered = self.stack.harness.recover_unknown(task, operator="console-operator")

        self.assertEqual(recovered.status, "running")
        self.assertIsNone(recovered.failure)
        self.assertEqual(recovered.model.status, "validated")
        self.assertEqual(recovered.model.attempts, 1, "reconcile must not consume a new round")
        self.assertEqual(recovered.model.receipt_ref, ref)
        self.assertEqual(
            self.stack.harness.budget(recovered).snapshot()["calls_used"], calls_before,
            "reconciliation must not dispatch or charge a second call",
        )
        types = [event["type"] for event in self.stack.events(recovered)]
        self.assertIn("model_reconciled", types)
        self.assertNotIn("model_reissued", types)
        self.assertEqual(self.stack.receipt_rows(), before_rows)

    def test_wrong_status_is_blocked(self) -> None:
        task = self.stack.unknown()
        for status, allowed_from in (("failed", "running"), ("created", "created")):
            with self.subTest(status=status):
                bad = replace(
                    self.stack.task, status=status,
                    failure={"code": "MODEL_OUTCOME_UNKNOWN", "intent_round": 1},
                )
                # Keep the body's model state legal for the substituted status.
                self.assertEqual(bad.status, status)
                with self.assertRaises(AgentError) as caught:
                    self.stack.harness.recover_unknown(bad, operator="console-operator")
                self.assertEqual(caught.exception.code, "RECOVERY_BLOCKED")
        self.assertEqual(task.status, "recovery_required")

    def test_wrong_failure_code_is_blocked(self) -> None:
        task = self.stack.unknown()
        for code in ("TOOL_OUTCOME_UNKNOWN", "BUDGET_EXCEEDED", "", None):
            with self.subTest(code=code):
                bad = replace(task, failure={"code": code, "intent_round": 1})
                with self.assertRaises(AgentError) as caught:
                    self.stack.harness.recover_unknown(bad, operator="console-operator")
                self.assertEqual(caught.exception.code, "RECOVERY_BLOCKED")

    def test_recovering_twice_is_blocked(self) -> None:
        task = self.stack.unknown()
        recovered = self.stack.harness.recover_unknown(task, operator="console-operator")
        with self.assertRaises(AgentError) as caught:
            self.stack.harness.recover_unknown(recovered, operator="console-operator")
        self.assertEqual(caught.exception.code, "RECOVERY_BLOCKED")


if __name__ == "__main__":
    unittest.main()

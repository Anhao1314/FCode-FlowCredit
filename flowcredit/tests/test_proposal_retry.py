"""一次付费重试：模型侧瞬时失败不该直接把任务判死。

覆盖：
1. PROPOSAL_MALFORMED 首次失败、第二次成功 → 本轮继续，两次调用都计费；
2. MODEL_CALL_FAILED 连续两次 → 仍落 recovery_required，但只重试一次；
3. 非瞬时错误（MODEL_AUTH_FAILED）不重试；
4. proposal_retry 事件带 attempt/cause，重放投影不受影响。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION, AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.replay import replay_events  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _proposal(tokens: int = 120, cost: int = 90) -> Proposal:
    return Proposal(
        plan={
            "version": PLAN_VERSION,
            "steps": [{"id": "step-1", "tool": "ls_workspace", "arguments": {}}],
        },
        provider="scripted",
        model="scripted-v1",
        tokens=tokens,
        cost_microusd=cost,
    )


class ScriptedProvider:
    """Outcomes are consumed in order: AgentError raises, Proposal returns."""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0
        self.contexts = []

    def propose(self, context):
        self.contexts.append(context)
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class RetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self._tmp.name) / "task.sqlite")
        self.harness = Harness(
            store=self.store,
            registry=ToolRegistry([]),
            provider=ScriptedProvider(_proposal()),
            world=None,
            limits=BudgetLimits(10, 10, 100_000, 100_000),
            receipts=ReceiptStore(self.store),
        )
        self.task = self.harness.create(
            task_id="task-retry",
            objective="retry once",
            principal="test",
            binding={},
            allowed_tools=["ls_workspace"],
            grant={},
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _run(self, *outcomes):
        provider = ScriptedProvider(*outcomes)
        task, proposal = self.harness._propose(
            self.task, {"round": 1, "state": {}}, provider,
        )
        return provider, task, proposal

    def _events(self, task):
        return self.store.events(task.id)

    def test_parse_failure_then_success_is_one_paid_retry(self) -> None:
        provider, task, proposal = self._run(
            AgentError("PROPOSAL_MALFORMED", "truncated JSON"),
            _proposal(),
        )
        self.assertIsNotNone(proposal)
        self.assertEqual(task.status, "planned")
        self.assertEqual(provider.calls, 2, "第一次解析失败后必须再派一次")
        self.assertEqual(
            [e["type"] for e in self._events(task)],
            ["task_created", "model_started", "proposal_retry", "plan_validated"],
        )
        retry = self._events(task)[2]["payload"]["detail"]
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["cause"], "PROPOSAL_MALFORMED")
        # 两次调度都计费（第一次不退款）
        self.assertEqual(self.harness.budget(task).snapshot()["calls_used"], 2)

    def test_invalid_plan_shape_is_retried_with_feedback(self) -> None:
        """A 0-step plan is a model formatting mistake, not a task verdict.

        It must get the same one paid retry as malformed JSON, and the retry
        prompt must carry the rejection reason; otherwise the model has no
        signal to answer differently.
        """
        bad = Proposal(
            plan={"version": PLAN_VERSION, "steps": []},
            provider="scripted",
            model="scripted-v1",
            tokens=10,
            cost_microusd=5,
        )
        provider, task, proposal = self._run(bad, _proposal())
        self.assertIsNotNone(proposal)
        self.assertEqual(task.status, "planned")
        self.assertEqual(provider.calls, 2)
        retry = [e for e in self._events(task) if e["type"] == "proposal_retry"][0]
        self.assertEqual(retry["payload"]["detail"]["cause"], "PLAN_SCHEMA_INVALID")
        feedback = provider.contexts[1]["retry_feedback"]
        self.assertEqual(feedback["code"], "PLAN_SCHEMA_INVALID")
        self.assertIn("1-6 steps", feedback["message"])
        self.assertIn("Do not answer in prose", feedback["instruction"])
        self.assertNotIn("retry_feedback", provider.contexts[0])

    def test_consecutive_transport_failures_stay_unknown_after_one_retry(self) -> None:
        provider, task, proposal = self._run(
            AgentError("MODEL_CALL_FAILED", "dns blip"),
            AgentError("MODEL_CALL_FAILED", "dns blip again"),
        )
        self.assertIsNone(proposal)
        self.assertEqual(task.status, "recovery_required")
        self.assertEqual(provider.calls, 2, "最多重试一次，不无限重发")
        self.assertEqual(task.failure["cause"], "MODEL_CALL_FAILED")
        types = [e["type"] for e in self._events(task)]
        self.assertEqual(types.count("proposal_retry"), 1)
        self.assertEqual(types[-1], "model_unknown")
        self.assertEqual(self.harness.budget(task).snapshot()["calls_used"], 2)

    def test_non_transient_error_is_not_retried(self) -> None:
        provider, task, proposal = self._run(
            AgentError("MODEL_AUTH_FAILED", "bad key"),
            _proposal(),
        )
        self.assertIsNone(proposal)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(task.status, "recovery_required")
        self.assertEqual(self.harness.budget(task).snapshot()["calls_used"], 1)

    def test_replay_projection_ignores_retry_event(self) -> None:
        _, task, _ = self._run(
            AgentError("PROPOSAL_MALFORMED", "truncated JSON"),
            _proposal(),
        )
        # 重试不新增 model_started：轮次数仍为 1，投影不会把同一轮数成两轮
        projected = replay_events(self._events(task))
        self.assertEqual(projected["model_attempts"], 1)
        self.assertEqual(projected["actions"], [])


if __name__ == "__main__":
    unittest.main()
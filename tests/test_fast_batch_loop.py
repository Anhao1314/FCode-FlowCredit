"""快速模式：一轮多动作的批量执行与预检。

覆盖：
1. `general-fast` profile 让一轮的 2 个只读动作真实执行、按序结算；
2. trace 保留整批 observations（singular 字段仍是最后一条）；
3. 任一动作 schema 不合法时整批不派发，只记一次 proposal_rejected。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402


def _proposal(*steps: dict) -> Proposal:
    return Proposal(
        plan={"version": PLAN_VERSION, "steps": list(steps)},
        provider="scripted", model="scripted-v1", tokens=10, cost_microusd=1,
    )


class BatchProvider:
    """Round 1: two reads. Round 2: the report write. Later: benign read."""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal(
                {"id": "read-a", "tool": "read_file", "arguments": {"path": "a.txt"}},
                {"id": "read-b", "tool": "read_file", "arguments": {"path": "b.txt"}},
            )
        if self.calls == 2:
            return _proposal(
                {"id": "write-report", "tool": "write_file",
                 "arguments": {"path": "report.md", "content": "# done\n"}},
            )
        return _proposal({"id": "extra", "tool": "read_file", "arguments": {"path": "a.txt"}})


class BadBatchProvider(BatchProvider):
    """Round 1 fails preflight on action 2; round 2 is a valid write."""

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal(
                {"id": "read-a", "tool": "read_file", "arguments": {"path": "a.txt"}},
                {"id": "bad-read", "tool": "read_file",
                 "arguments": {"path": "b.txt", "limit": 999}},
            )
        return _proposal(
            {"id": "write-report", "tool": "write_file",
             "arguments": {"path": "report.md", "content": "# done\n"}},
        )


class RecoverableErrorProvider(BatchProvider):
    """Round 1 hits an execution-time bad regex; round 2 recovers."""

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal(
                {"id": "bad-grep", "tool": "grep_workspace", "arguments": {"pattern": "("}},
                {"id": "read-a", "tool": "read_file", "arguments": {"path": "a.txt"}},
            )
        return _proposal(
            {"id": "write-report", "tool": "write_file",
             "arguments": {"path": "report.md", "content": "# done\n"}},
        )


class BatchLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.scenario = get_scenario("general-research")
        self.world = GENERAL_DOMAIN.create_world(
            spec={"mode": "open"},
            sandbox=WorkspaceSandbox(self.root / "ws"),
        )
        self.world.sandbox.ensure()
        self.world.sandbox.write("a.txt", "alpha\n")
        self.world.sandbox.write("b.txt", "beta\n")
        self.store = TaskStore(self.root / "task.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _run(self, provider):
        harness = Harness(
            store=self.store,
            registry=ToolRegistry(self.world.build_tools()),
            provider=provider,
            world=self.world,
            limits=self.scenario.limits,
            receipts=ReceiptStore(self.store),
            verifier=lambda observation: self.world.verify_observation(observation),
            prompt_profile="general-fast",
        )
        task = harness.create(
            task_id="task-batch",
            objective="读两个文件后写报告",
            principal="test",
            binding={"scenario": self.scenario.key},
            allowed_tools=list(self.scenario.allowed_tools),
            grant=self.scenario.grant,
        )
        recorder = TraceRecorder(self.store, provider_name="scripted", model_name="scripted-v1")
        final = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(self.world),
            max_rounds=4,
            observer=recorder,
            result_tools=GENERAL_DOMAIN.result_tools,
        ).run(task.id)
        return final, recorder, harness

    def _events(self, task):
        return self.store.events(task.id)

    def test_two_reads_execute_in_one_round(self) -> None:
        provider = BatchProvider()
        final, recorder, harness = self._run(provider)
        self.assertEqual(final.status, "succeeded")
        self.assertEqual(provider.calls, 2, "两个读动作只花一次模型调用")
        types = [event["type"] for event in self._events(final)]
        self.assertEqual(types.count("model_started"), 2)
        self.assertEqual(types.count("tool_started"), 3)
        trace = recorder.build(final)
        first = trace["rounds"][0]
        self.assertEqual(len(first["observations"]), 2)
        self.assertEqual([item["tool"] for item in first["observations"]],
                         ["read_file", "read_file"])
        self.assertEqual(first["observations"][0]["arguments"]["path"], "a.txt")
        self.assertEqual(first["observations"][1]["arguments"]["path"], "b.txt")
        # 兼容字段仍是最后一条
        self.assertEqual(first["observation"]["arguments"]["path"], "b.txt")
        self.assertEqual(harness.budget(final).snapshot()["reads_used"], 3)

    def test_invalid_action_aborts_the_whole_batch_before_dispatch(self) -> None:
        provider = BadBatchProvider()
        final, _recorder, harness = self._run(provider)
        self.assertEqual(final.status, "succeeded")
        events = self._events(final)
        types = [event["type"] for event in events]
        self.assertEqual(types.count("proposal_rejected"), 1)
        # 批里第一个合法动作也不能先执行
        started = [event["payload"]["detail"]["tool"]
                   for event in events if event["type"] == "tool_started"]
        self.assertEqual(started, ["write_file"])
        self.assertEqual(harness.budget(final).snapshot()["reads_used"], 1)

    def test_execution_request_error_does_not_kill_the_run(self) -> None:
        provider = RecoverableErrorProvider()
        final, _recorder, _harness = self._run(provider)
        self.assertEqual(final.status, "succeeded")
        types = [event["type"] for event in self._events(final)]
        self.assertIn("tool_failed", types, "错误以可恢复的 tool_failed 留在链上")
        failed = next(event for event in self._events(final) if event["type"] == "tool_failed")
        self.assertEqual(failed["payload"]["detail"]["code"], "GREP_PATTERN_INVALID")
        # 同批的后续动作没有执行：下一轮模型重新提议
        started = [event["payload"]["detail"]["tool"]
                   for event in self._events(final) if event["type"] == "tool_started"]
        self.assertEqual(started, ["grep_workspace", "write_file"])


if __name__ == "__main__":
    unittest.main()
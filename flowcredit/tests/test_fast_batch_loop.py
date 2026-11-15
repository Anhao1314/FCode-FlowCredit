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
import threading
import time
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
from flowagent.runtime.recovery import (  # noqa: E402
    find_unfinished_tool, reconcile_unknown_tool,
)
from flowagent.tools.base import EFFECT_READ, Param, ToolSpec  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402


def _proposal(*steps: dict, additions: list[dict] | None = None) -> Proposal:
    plan: dict = {"version": PLAN_VERSION, "steps": list(steps)}
    if additions:
        plan["additions"] = additions
    return Proposal(
        plan=plan,
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


class PlannedBatchProvider(BatchProvider):
    """Round 1 submits a 3-todo plan alongside reads; round 2 edits; then report."""

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal(
                {"id": "read-a", "tool": "read_file", "arguments": {"path": "a.txt"}},
                additions=[
                    {"id": "m1", "text": "读 b.txt", "tool": "read_file",
                     "done_when": "done:m1", "arguments": {"path": "b.txt"}},
                    {"id": "m2", "text": "写报告", "tool": "write_file",
                     "done_when": "done:m2", "arguments": {"path": "report.md"}},
                ],
            )
        if self.calls == 2:
            return _proposal(
                {"id": "read-b", "tool": "read_file", "arguments": {"path": "b.txt"}},
            )
        return _proposal(
            {"id": "write-report", "tool": "write_file",
             "arguments": {"path": "report.md", "content": "# done\n"}},
        )


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
            plan_extension=True,
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

    def test_model_plan_enters_plan_and_settles(self) -> None:
        provider = PlannedBatchProvider()
        final, recorder, harness = self._run(provider)
        self.assertEqual(final.status, "succeeded")
        events = self._events(final)
        steered = [e for e in events if e["type"] == "plan_steered"]
        self.assertEqual(len(steered), 1, "模型计划经 plan_steered 入链")
        detail = steered[0]["payload"]["detail"]
        self.assertEqual(detail["added"], ["m1"])
        # 结算看终态 plan：round 2 的 read b.txt 观察把 m1 打勾
        by_id = {todo["id"]: todo for todo in final.plan_state["todos"]}
        self.assertEqual(by_id["m1"]["status"], "done", "read b.txt 观察结算 m1")
        # 写报告的 todo 与蓝图自带的结果步骤重复 → 拒绝但保留合法条目（部分接受）
        rejected = [e for e in events if e["type"] == "control_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["payload"]["detail"]["rejected"][0]["code"],
                         "PLAN_ADDITION_RESULT_DUPLICATE")
        self.assertEqual(rejected[0]["payload"]["detail"]["accepted_ids"], ["m1"])
        # fast 运行的观察预算翻倍：批量轮的观察不再两轮就被折叠
        self.assertEqual(harness.max_steps, 4)

    def test_fast_memory_budget_is_doubled(self) -> None:
        provider = BatchProvider()
        final, _recorder, harness = self._run(provider)
        loop = Loop(harness, planner=GENERAL_DOMAIN.make_planner(self.world), max_rounds=2,
                    result_tools=GENERAL_DOMAIN.result_tools)
        memory = loop.restore(final)
        self.assertEqual(memory.observation_budget, 48_000)

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

    def test_read_batch_handlers_actually_overlap(self) -> None:
        """A barrier only releases when both handlers run at the same time."""
        barrier = threading.Barrier(2)

        def slow_read(arguments):
            barrier.wait(timeout=2)
            return {"path": arguments["path"], "content": "", "sha256": "0" * 64, "bytes": 0}

        spec = ToolSpec(
            name="slow_read",
            summary="test-only parallel read",
            effect=EFFECT_READ,
            params=(Param(name="path", kind="string", required=True),),
            handler=slow_read,
        )
        harness = Harness(
            store=self.store,
            registry=ToolRegistry([spec]),
            provider=BatchProvider(),
            world=self.world,
            limits=self.scenario.limits,
            receipts=ReceiptStore(self.store),
            verifier=lambda observation: self.world.verify_observation(observation),
            prompt_profile="general-fast",
        )
        task = harness.create(
            task_id="task-parallel",
            objective="并行读两个文件",
            principal="test",
            binding={"scenario": self.scenario.key},
            allowed_tools=["slow_read"],
            grant={
                "grant_id": "parallel-test",
                "allowed_tools": ["slow_read"],
                "allowed_effects": ["read"],
            },
        )
        steps = [
            {"id": "a", "tool": "slow_read", "arguments": {"path": "a"}},
            {"id": "b", "tool": "slow_read", "arguments": {"path": "b"}},
        ]
        started = time.perf_counter()
        _task, outcomes = harness.execute_read_batch(task, steps, round_no=1)
        elapsed = time.perf_counter() - started
        self.assertEqual([error for _, _, error in outcomes], [None, None])
        self.assertLess(elapsed, 1.0, f"handlers did not overlap: {elapsed:.3f}s")

    def test_crash_recovery_recognizes_a_parallel_read_batch(self) -> None:
        events = [
            {"sequence": 1, "type": "tool_started",
             "payload": {"detail": {"tool": "read_file", "round": 1}}},
            {"sequence": 2, "type": "tool_started",
             "payload": {"detail": {"tool": "grep_workspace", "round": 1}}},
        ]
        unfinished = find_unfinished_tool(events)
        self.assertEqual(unfinished["detail"]["tool"], "parallel_read_batch")
        self.assertTrue(unfinished["detail"]["batch"])
        self.assertEqual(len(unfinished["detail"]["calls"]), 2)
        # A mixed batch still fails closed: a write may have had a side effect.
        with self.assertRaises(Exception) as error:
            find_unfinished_tool(events + [
                {"sequence": 3, "type": "tool_started",
                 "payload": {"detail": {"tool": "write_file", "round": 1}}},
            ])
        self.assertEqual(getattr(error.exception, "code", ""), "EVENT_ORDER_INVALID")
        # Outcomes are committed in proposal order, not LIFO: both starts must
        # close cleanly and leave no unfinished call.
        completed = events + [
            {"sequence": 3, "type": "tool_succeeded",
             "payload": {"detail": {"tool": "read_file", "round": 1}}},
            {"sequence": 4, "type": "tool_succeeded",
             "payload": {"detail": {"tool": "grep_workspace", "round": 1}}},
        ]
        self.assertIsNone(find_unfinished_tool(completed))

    def test_reconcile_marks_a_parallel_read_batch_as_one_unknown_outcome(self) -> None:
        harness = Harness(
            store=self.store,
            registry=ToolRegistry(self.world.build_tools()),
            provider=BatchProvider(),
            world=self.world,
            limits=self.scenario.limits,
            receipts=ReceiptStore(self.store),
            verifier=lambda observation: self.world.verify_observation(observation),
            prompt_profile="general-fast",
        )
        task = harness.create(
            task_id="task-batch-recovery",
            objective="并行读",
            principal="test",
            binding={"scenario": self.scenario.key},
            allowed_tools=list(self.scenario.allowed_tools),
            grant=self.scenario.grant,
        )
        # Simulate the durable state after the batch was dispatched: the task
        # is running and both local reads have a start event but no outcome.
        task = harness._commit(task, task.advance("planning"), "planning_started", {})
        task = harness._commit(task, task.advance("planned"), "plan_validated", {})
        task = harness._commit(task, task.advance("running"), "execution_started", {})
        for path in ("a.txt", "b.txt"):
            task = harness.record(task, "tool_started", {
                "tool": "read_file", "arguments": {"path": path}, "round": 1,
            })
        final, recovery = reconcile_unknown_tool(
            harness, task.id, evidence={"side_effect": "not_observed"},
        )
        self.assertEqual(final.status, "failed")
        self.assertEqual(recovery["tools"], ["read_file", "read_file"])
        self.assertTrue(recovery["retry_dispatched"] is False)

    def test_execution_request_error_does_not_kill_the_run(self) -> None:
        provider = RecoverableErrorProvider()
        final, _recorder, _harness = self._run(provider)
        self.assertEqual(final.status, "succeeded")
        types = [event["type"] for event in self._events(final)]
        self.assertIn("tool_failed", types, "错误以可恢复的 tool_failed 留在链上")
        failed = next(event for event in self._events(final) if event["type"] == "tool_failed")
        self.assertEqual(failed["payload"]["detail"]["code"], "GREP_PATTERN_INVALID")
        # 并行批量：坏 regex 与同批的 read_file 一起派发；成功的读照常入链，
        # 失败的那个留给下一轮模型修正，不会连坐丢掉已完成的读。
        started = [event["payload"]["detail"]["tool"]
                   for event in self._events(final) if event["type"] == "tool_started"]
        self.assertEqual(started, ["grep_workspace", "read_file", "write_file"])
        succeeded = [event["payload"]["detail"]["tool"]
                     for event in self._events(final) if event["type"] == "tool_succeeded"]
        self.assertIn("read_file", succeeded)


def harness_max_steps(harness) -> int:
    return harness.max_steps


if __name__ == "__main__":
    unittest.main()
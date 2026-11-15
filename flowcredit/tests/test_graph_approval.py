"""LangGraph 引擎的 interrupt 审批流：挂起 → Command(resume) → 事件对齐。

覆盖：
1. allow 流：两次审批（read→write），approval_requested/granted 事件成对，
   最终 succeeded 且验证产物一致；
2. deny 流：拒绝 → tool_denied 终态，文件不被写入；
3. 挂起语义：run() 返回 paused 任务 + suspended_payload；图线程已结束，
   resume 以 Command(resume) 从检查点续跑（同一进程内 MemorySaver）。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.graph_loop import GraphLoop  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402

from flowagent.runtime.graph_loop import _HAS_LANGGRAPH  # noqa: E402

from console_runtime import Run, RunControl  # noqa: E402


def _proposal(*steps: dict) -> Proposal:
    return Proposal(
        plan={"version": PLAN_VERSION, "steps": list(steps)},
        provider="scripted", model="scripted-v1", tokens=10, cost_microusd=1,
    )


class ScriptedProvider:
    """r1 读 a.txt；r2 写 report.md（计划的 out 路径）。"""

    name = "scripted"
    model = "scripted-v1"

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal(
                {"id": "read-a", "tool": "read_file", "arguments": {"path": "a.txt"}},
            )
        return _proposal(
            {"id": "write-report", "tool": "write_file",
             "arguments": {"path": "report.md", "content": "# done\n"}},
        )


@unittest.skipUnless(_HAS_LANGGRAPH, "langgraph is not installed")
class GraphApprovalTest(unittest.TestCase):
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
        self.store = TaskStore(self.root / "task.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _run_until(self, engine_run) -> tuple[object, GraphLoop]:
        """驱动一次 run()/resume()，返回 (返回的任务, GraphLoop)。"""
        task = engine_run()
        return task, self.loop

    def _build(self) -> tuple[GraphLoop, Run, TaskStore, Path]:
        harness_args = dict(
            store=self.store,
            registry=ToolRegistry(self.world.build_tools()),
            provider=ScriptedProvider(),
            world=self.world,
            verifier=lambda observation: self.world.verify_observation(observation),
            limits=self.scenario.limits,
            receipts=ReceiptStore(self.store),
        )
        from flowagent.runtime.harness import Harness

        harness = Harness(**harness_args)
        run = Run(
            id="run-graph-approval",
            template="general-research",
            objective="读 a.txt 后写 report.md",
            created="2026-11-10T00:00:00+00:00",
            pace=0,
            approval="all",
            model="mock",
        )
        run.controller = None
        # RunControl 只在构造期消费 runtime 的三个引用（Plan/BudgetLimits/
        # RoundControl/ToolControl）；prepare/apply 路径只用 harness 与锁。
        from flowagent.core.plan import Plan as _Plan
        from flowagent.runtime.budget import BudgetLimits as _BudgetLimits
        from flowagent.runtime.control import RoundControl as _RoundControl
        from flowagent.runtime.control import ToolControl as _ToolControl

        fake_runtime = SimpleNamespace(
            Plan=_Plan, BudgetLimits=_BudgetLimits,
            RoundControl=_RoundControl, ToolControl=_ToolControl,
        )
        control = RunControl(
            run,
            harness,
            fake_runtime,
            approval_tools=("write_file", "edit_file", "run_command"),
            timeout=5.0,
        )
        loop = GraphLoop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(self.world),
            max_rounds=6,
            control=control,
            result_tools=GENERAL_DOMAIN.result_tools,
        )
        task = harness.create(
            task_id="task-graph-approval",
            objective="读 a.txt 后写 report.md",
            principal="test",
            binding={"scenario": self.scenario.key},
            allowed_tools=list(self.scenario.allowed_tools),
            grant=dict(self.scenario.grant),
        )
        return loop, run, self.store, self.root

    def _events(self, store: TaskStore, task_id: str) -> list[dict]:
        return store.events(task_id)

    def test_allow_flow_suspends_twice_then_succeeds(self) -> None:
        loop, run, store, root = self._build()
        # 第一次运行：在 read_file 审批处挂起
        task = loop.run("task-graph-approval")
        self.assertEqual(task.status, "paused")
        self.assertIsNotNone(loop.suspended_payload)
        self.assertEqual(loop.suspended_payload["tool"], "read_file")
        self.assertEqual(run.pending["tool"], "read_file")
        self.assertTrue(any(
            e["type"] == "approval_requested" for e in self._events(store, task.id)))

        # 决策 1：allow（remember，供后续同类调用免审——本例后续是 write，仍审批）
        task = loop.resume({"allow": True, "remember": True})
        self.assertEqual(task.status, "paused", "第二个动作 write_file 需要新的审批")
        self.assertEqual(loop.suspended_payload["tool"], "write_file")
        self.assertTrue(any(
            e["type"] == "approval_granted" for e in self._events(store, task.id)))

        # 决策 2：allow → write 执行 → 计划完成 → succeeded
        task = loop.resume({"allow": True, "remember": False})
        self.assertEqual(task.status, "succeeded")
        self.assertTrue((root / "ws" / "report.md").is_file())
        events = self._events(store, task.id)
        self.assertEqual(
            [e["type"] for e in events].count("approval_requested"), 2)
        self.assertEqual(
            [e["type"] for e in events].count("approval_granted"), 2)
        self.assertEqual(task.result["verification"], "file-state-recomputed")

    def test_deny_flow_ends_denied_without_writing(self) -> None:
        loop, run, store, root = self._build()
        task = loop.run("task-graph-approval")
        self.assertEqual(task.status, "paused")

        task = loop.resume({"allow": False, "remember": False})
        self.assertEqual(task.status, "denied")
        events = self._events(store, task.id)
        self.assertTrue(any(
            e["type"] == "approval_denied" for e in events))
        self.assertTrue(any(e["type"] == "tool_denied" for e in events))
        self.assertFalse((root / "ws" / "report.md").exists())
        # 计划未完成：out todo 仍 pending，不得伪造成功
        self.assertEqual(task.failure["code"], "OPERATOR_DENIED")

    def test_stop_during_suspension_ends_stopped(self) -> None:
        loop, run, store, root = self._build()
        task = loop.run("task-graph-approval")
        self.assertEqual(task.status, "paused")
        run.stop_requested = True

        task = loop.resume({"allow": True, "remember": False})
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.failure["code"], "STOPPED_BY_OPERATOR")
        events = self._events(store, task.id)
        self.assertTrue(any(
            e["type"] == "approval_aborted" for e in events))
        self.assertFalse((root / "ws" / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
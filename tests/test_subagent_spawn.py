"""子代理编排 S1：请求规整/等待收集/摘要上限/场景与注册表治理语义。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from console_subagents import (  # noqa: E402
    build_child_digest,
    normalize_spawn_request,
    wait_for_children,
)
from flowagent.demo.fixtures import SCENARIOS  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402


class NormalizeSpawnTest(unittest.TestCase):
    def test_normalizes_and_dedupes(self) -> None:
        out = normalize_spawn_request(
            [" 调研 A ", "调研 A", "", "调研 B"], parent_run_id=None)
        self.assertEqual(out, ["调研 A", "调研 B"])

    def test_depth_hard_limit(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_spawn_request(["x"], parent_run_id="run-parent")
        self.assertIn("深度", str(ctx.exception))

    def test_count_hard_cap(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_spawn_request([f"任务{i}" for i in range(5)], parent_run_id=None)
        self.assertIn("4", str(ctx.exception))

    def test_empty_objectives_refused(self) -> None:
        with self.assertRaises(ValueError):
            normalize_spawn_request(["", "  "], parent_run_id=None)
        with self.assertRaises(ValueError):
            normalize_spawn_request([], parent_run_id=None)

    def test_objective_length_cap(self) -> None:
        with self.assertRaises(ValueError):
            normalize_spawn_request(["长" * 601], parent_run_id=None)


class WaitChildrenTest(unittest.TestCase):
    def _children(self, ids):
        return [{"run_id": i, "objective": f"任务 {i}"} for i in ids]

    def test_all_finish_quickly(self) -> None:
        states = {"a": {"finished": True, "status": "succeeded", "rounds": 3},
                  "b": {"finished": True, "status": "failed", "rounds": 1}}
        join = wait_for_children(
            self._children(["a", "b"]),
            get_state=lambda rid: states[rid],
            deadline_monotonic=10.0,
            sleep=lambda s: None,
            now=lambda: 0.0,
        )
        self.assertFalse(join["timed_out"])
        self.assertEqual(join["states"]["a"]["status"], "succeeded")
        self.assertEqual(join["states"]["b"]["status"], "failed")

    def test_progress_then_finish(self) -> None:
        ticks = {"n": 0}
        def get_state(rid):
            ticks["n"] += 1
            if ticks["n"] >= 3:
                return {"finished": True, "status": "succeeded", "rounds": 2}
            return {"finished": False, "status": "running", "rounds": ticks["n"]}
        slept = []
        join = wait_for_children(
            self._children(["a"]),
            get_state=get_state,
            deadline_monotonic=100.0,
            sleep=slept.append,
            now=lambda: float(ticks["n"]),
        )
        self.assertFalse(join["timed_out"])
        self.assertGreater(len(slept), 0, "轮询之间确实让出了线程")

    def test_timeout_stops_children_and_records_honestly(self) -> None:
        clock = {"now": 0.0}
        stopped = []
        def get_state(rid):
            return {"finished": False, "status": "running", "rounds": 9}
        def now():
            return clock["now"]
        def sleep(seconds):
            clock["now"] += seconds
        def request_stop(rid):
            stopped.append(rid)
        join = wait_for_children(
            self._children(["a", "b"]),
            get_state=get_state,
            deadline_monotonic=600.0,
            sleep=sleep,
            now=now,
            request_stop=request_stop,
        )
        self.assertTrue(join["timed_out"])
        self.assertEqual(sorted(stopped), ["a", "b"], "超时后向未完成的子运行发起停止")
        for rid in ("a", "b"):
            self.assertEqual(join["states"][rid]["status"], "stopped")
            self.assertIn("超时", join["states"][rid]["note"])


class DigestTest(unittest.TestCase):
    def test_digest_caps_and_shapes(self) -> None:
        digest = build_child_digest(
            objective="长" * 700,
            run_id="run-1",
            state={"status": "succeeded", "rounds": 4},
            files=[f"f{i}.md" for i in range(30)],
            summary="发现" * 500,
        )
        self.assertEqual(len(digest["objective"]), 600)
        self.assertEqual(len(digest["files"]), 20)
        self.assertLessEqual(len(digest["summary"]), 600)
        self.assertEqual(digest["status"], "succeeded")

    def test_digest_empty_summary_honest(self) -> None:
        digest = build_child_digest(objective="x", run_id="r",
                                    state={"status": "failed"}, files=[], summary="")
        self.assertEqual(digest["summary"], "（子代理未产出可读摘要）")


class ScenarioAndRegistryTest(unittest.TestCase):
    def test_general_subagent_scenario_is_readonly_internal(self) -> None:
        scenario = SCENARIOS["general-subagent"]
        forbidden = {"write_file", "edit_file", "run_command", "fetch_url",
                     "spawn_subagents", "ask_operator"}
        self.assertEqual(forbidden & set(scenario.allowed_tools), set(),
                         "子代理不得持有写/网络/编排/提问能力")
        self.assertIn("grep_workspace", scenario.allowed_tools)
        self.assertTrue(scenario.internal, "内部场景不进模板选择器")
        self.assertEqual(scenario.grant["allowed_effects"], ["read"])

    def test_spawn_tool_registered_only_with_channel(self) -> None:
        from flowagent.domains.general import create_world

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp) / "ws")
            with_channel = create_world(
                sandbox=sandbox, subagent_spawner=lambda objs: {}).build_tools()
            without_channel = create_world(sandbox=sandbox).build_tools()
            names_with = {spec.name for spec in with_channel}
            names_without = {spec.name for spec in without_channel}
            self.assertIn("spawn_subagents", names_with)
            self.assertNotIn("spawn_subagents", names_without,
                             "无编排通道的部署诚实缺席,而不是注册一个会失败的工具")
            spec = next(s for s in with_channel if s.name == "spawn_subagents")
            self.assertEqual(spec.effect, "write", "放大是写效果,进审批集")

    def test_spawn_handler_normalizes_and_refuses_empty(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp) / "ws")
            digests = [{"children": [{"run_id": "r1", "status": "succeeded",
                                      "summary": "ok"}], "timed_out": False}]
            from flowagent.domains.general import create_world

            world = create_world(
                sandbox=sandbox,
                subagent_spawner=lambda objs: digests.pop() if objs == ["a", "b"] else None,
            )
            spec = next(s for s in world.build_tools() if s.name == "spawn_subagents")
            spec.handler({"objectives": [" a ", "", "b"]})
            self.assertEqual(digests, [], "通道收到了规整后的目标列表")
            from flowagent.core.contracts import AgentError
            with self.assertRaises(AgentError):
                spec.handler({"objectives": ["", ""]})

    def test_approval_membership_and_whitelist(self) -> None:
        from flowagent.domains.general import GENERAL_DOMAIN, APPROVAL_TOOLS
        self.assertIn("spawn_subagents", APPROVAL_TOOLS)
        research = SCENARIOS["general-research"]
        self.assertIn("spawn_subagents", research.allowed_tools)
        self.assertIn("spawn_subagents", research.grant["allowed_tools"])
        # 子代理场景本身不含 spawn——深度硬限在权限层的第二道防线
        self.assertNotIn("spawn_subagents", SCENARIOS["general-subagent"].allowed_tools)


if __name__ == "__main__":
    unittest.main()

"""子代理编排 S1：请求规整/等待收集/摘要上限/场景与注册表治理语义。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from types import SimpleNamespace
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
    def test_general_subagent_scenario_scope(self) -> None:
        """S3:子代理可写**自己的沙箱**(物理隔离),但无网络/编排/提问/命令。"""
        scenario = SCENARIOS["general-subagent"]
        forbidden = {"edit_file", "run_command", "fetch_url",
                     "spawn_subagents", "ask_operator"}
        self.assertEqual(forbidden & set(scenario.allowed_tools), set(),
                         "子代理不得持有命令/网络/编排/提问能力")
        self.assertIn("write_file", scenario.allowed_tools,
                      "写只发生在子代理自己的沙箱,是交付物而非合并问题")
        self.assertEqual(scenario.grant["allowed_effects"], ["read", "write"])
        self.assertIn("grep_workspace", scenario.allowed_tools)
        self.assertTrue(scenario.internal, "内部场景不进模板选择器")

    def _fake_channel(self):
        class FakeChannel:
            def spawn(self, arguments):
                return {"children": [], "timed_out": False}
            def status(self):
                return {"children": [], "count": 0}
            def read(self, run_id, path):
                return {"path": path, "content": "x", "sha256": "h",
                        "bytes": 1, "child_run_id": run_id}
        return FakeChannel()

    def test_spawn_tool_registered_only_with_channel(self) -> None:
        from flowagent.domains.general import create_world

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp) / "ws")
            with_channel = create_world(
                sandbox=sandbox, subagent_channel=self._fake_channel()).build_tools()
            without_channel = create_world(sandbox=sandbox).build_tools()
            names_with = {spec.name for spec in with_channel}
            names_without = {spec.name for spec in without_channel}
            self.assertIn("spawn_subagents", names_with)
            self.assertIn("subagent_status", names_with)
            self.assertIn("subagent_read", names_with)
            for name in ("spawn_subagents", "subagent_status", "subagent_read"):
                self.assertNotIn(name, names_without,
                                 "无编排通道的部署诚实缺席,而不是注册一个会失败的工具")
            spec = next(s for s in with_channel if s.name == "spawn_subagents")
            self.assertEqual(spec.effect, "write", "放大是写效果,进审批集")
            for name in ("subagent_status", "subagent_read"):
                self.assertEqual(
                    next(s for s in with_channel if s.name == name).effect,
                    "read", name)

    def test_spawn_handler_normalizes_and_refuses_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp) / "ws")
            seen = []
            from flowagent.domains.general import create_world

            channel = self._fake_channel()
            def spawn_capture(arguments):
                seen.append(arguments)
                return {"children": [{"run_id": "r1", "status": "succeeded",
                                      "summary": "ok"}], "timed_out": False}
            channel.spawn = spawn_capture
            world = create_world(sandbox=sandbox, subagent_channel=channel)
            spec = next(s for s in world.build_tools() if s.name == "spawn_subagents")
            spec.handler({"objectives": [" a ", "", "b"]})
            self.assertEqual(seen, [{"objectives": ["a", "b"], "budget": None}],
                             "通道收到了规整后的目标列表")
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


class SubagentChannelTest(unittest.TestCase):
    """S3 通道治理:read 狱到自己的子代理、预算封底、status 只看自家。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name) / "runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_manager(self, parent_spec=None):
        from types import SimpleNamespace

        counter = {"n": 0}

        class FakeRun:
            def __init__(self, rid, spec, *, source="manual", approval="consequential"):
                self.id = rid
                self.spec = spec
                self.status = "running"
                self.rounds = 1
                self.finished = False
                self.lock = __import__("threading").RLock()
                self.trace_path = ""
                self.model = "mock"
                self.effort = ""
                self.objective = "测试运行"
                self.template = "general-subagent"
                self.source = source
                self.approval = approval

        class FakeManager:
            runs = {}
            def __init__(self):
                self.runs_dir = self_run_dir
            def create(self, *, objective, template, approval, spec, model_key, effort, source):
                counter["n"] += 1
                rid = f"run-child-{counter['n']:03d}"
                run = FakeRun(rid, spec, source=source, approval=approval)
                self.runs[rid] = run
                return run

        self_run_dir = self.runs_dir
        parent = FakeRun("run-parent", parent_spec or {})
        mgr = FakeManager()
        mgr.runs["run-parent"] = parent
        return mgr, parent

    def _channel(self, mgr, parent):
        from console_subagents import SubagentChannel

        return SubagentChannel(mgr, parent)

    def test_spawn_creates_children_with_budget_and_source(self) -> None:
        mgr, parent = self._fake_manager()
        channel = self._channel(mgr, parent)
        # 子运行立即终态:把 create 出来的 run 标记完成
        orig_create = mgr.create
        def create_with_finish(**kwargs):
            run = orig_create(**kwargs)
            run.finished = True
            run.status = "succeeded"
            return run
        mgr.create = create_with_finish
        result = channel.spawn({"objectives": ["子任务A", "子任务B"],
                                "budget": {"maxCalls": 10, "maxReads": 30}})
        self.assertFalse(result["timed_out"])
        self.assertEqual(len(result["children"]), 2)
        kids = [r for r in mgr.runs.values() if r.id != "run-parent"]
        for kid in kids:
            self.assertEqual(kid.spec["parent_run_id"], "run-parent")
            self.assertEqual(kid.spec["budget"]["maxCalls"], 10)
            self.assertEqual(kid.template, "general-subagent")
            self.assertEqual(kid.source, "subagent")
            self.assertEqual(kid.approval, "off", "放大已由 spawn 审批覆盖")

    def test_spawn_budget_caps_enforced(self) -> None:
        mgr, parent = self._fake_manager()
        channel = self._channel(mgr, parent)
        with self.assertRaises(ValueError) as ctx:
            channel.spawn({"objectives": ["x"], "budget": {"maxCalls": 999}})
        self.assertIn("24", str(ctx.exception))

    def test_depth_guard_in_channel(self) -> None:
        mgr, parent = self._fake_manager(parent_spec={"parent_run_id": "run-grandpa"})
        channel = self._channel(mgr, parent)
        with self.assertRaises(ValueError) as ctx:
            channel.spawn({"objectives": ["x"]})
        self.assertIn("深度", str(ctx.exception))

    def test_read_only_own_children_and_jail(self) -> None:
        mgr, parent = self._fake_manager()
        channel = self._channel(mgr, parent)
        # 建一个子运行 + 它的工作区文件
        child = mgr.create(objective="子", template="general-subagent",
                           approval="off",
                           spec={"mode": "subagent", "parent_run_id": "run-parent"},
                           model_key="mock", effort="", source="subagent")
        ws = self.runs_dir / child.id / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "findings.md").write_text("# 发现\n依据充分", encoding="utf-8")
        # 陌生运行的工作区(不是本运行的子代理)
        stranger = self.runs_dir / "run-stranger" / "workspace"
        stranger.mkdir(parents=True, exist_ok=True)
        (stranger / "secret.txt").write_text("机密", encoding="utf-8")
        mgr.runs["run-stranger"] = type("R", (), {"id": "run-stranger", "spec": {}})()

        ok = channel.read(child.id, "findings.md")
        self.assertIn("发现", ok["content"])
        self.assertEqual(ok["child_run_id"], child.id)
        from flowagent.core.contracts import AgentError

        with self.assertRaises(ValueError, msg="陌生运行拒绝——不是本运行的子代理"):
            channel.read("run-stranger", "secret.txt")
        with self.assertRaises(AgentError, msg="路径越狱被沙箱狱拒绝"):
            channel.read(child.id, "../../run-stranger/workspace/secret.txt")

    def test_status_lists_only_own_children(self) -> None:
        mgr, parent = self._fake_manager()
        channel = self._channel(mgr, parent)
        mgr.create(objective="子", template="general-subagent", approval="off",
                   spec={"mode": "subagent", "parent_run_id": "run-parent"},
                   model_key="mock", effort="", source="subagent")
        mgr.runs["run-other"] = type("R", (), {"id": "run-other", "spec": {},
                                               "status": "running", "rounds": 1,
                                               "objective": "别人家"})()
        status = channel.status()
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["children"][0]["run_id"].startswith("run-child"), True)

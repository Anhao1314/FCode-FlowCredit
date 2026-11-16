"""S2b 写子代理：隔离拷贝、基线哈希、冲突检测的应用。

覆盖：
1. 纯逻辑（snapshot_hashes / diff_snapshot / 写扇出数上限）；
2. spawn write_access 分支（子 spec 带拷贝源与 writer 模板）；
3. apply_changes（应用/冲突/删除报告/分批/封底/亲缘校验）；
4. 播种（_seed_workspace_copy：runs 目录边界校验 + 基线落盘）；
5. 装配（工具注册/场景授权/writer 禁递归）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_subagents import (  # noqa: E402
    MAX_APPLY_FILES,
    MAX_WRITE_CHILDREN,
    SubagentChannel,
    diff_snapshot,
    normalize_spawn_request,
    snapshot_hashes,
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SnapshotTest(unittest.TestCase):
    def test_hashes_skip_git_and_ds_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.txt", "one")
            _write(root, ".git/config", "x")
            _write(root, "sub/.DS_Store", "x")
            hashes = snapshot_hashes(root)
            self.assertEqual(sorted(hashes), ["a.txt"])

    def test_diff_snapshot_classifies(self) -> None:
        diff = diff_snapshot(
            {"keep": "1", "mod": "1", "gone": "1"},
            {"keep": "1", "mod": "2", "new": "9"},
        )
        self.assertEqual(diff["modified"], ["mod"])
        self.assertEqual(diff["added"], ["new"])
        self.assertEqual(diff["deleted"], ["gone"])


def _channel_with_runs(tmp: Path, *, write_mode: bool = False):
    runs_dir = tmp / "runs"
    parent_ws = runs_dir / "run-p" / "workspace"
    child_ws = runs_dir / "run-c" / "workspace"
    parent_ws.mkdir(parents=True)
    child_ws.mkdir(parents=True)
    _write(parent_ws, "a.txt", "parent a")
    _write(parent_ws, "b.txt", "same b")
    _write(child_ws, "a.txt", "child a")
    _write(child_ws, "b.txt", "same b")
    _write(child_ws, "c.txt", "child c")
    baseline = {
        "a.txt": snapshot_hashes(parent_ws)["a.txt"],
        "b.txt": snapshot_hashes(parent_ws)["b.txt"],
    }
    (runs_dir / "run-c" / "workspace_baseline.json").write_text(
        json.dumps(baseline), encoding="utf-8",
    )
    child = SimpleNamespace(
        id="run-c", finished=True, status="succeeded", rounds=2,
        spec={"parent_run_id": "run-p"}, trace_path=None,
    )
    parent = SimpleNamespace(id="run-p", spec={}, model="mock", effort=None)
    manager = SimpleNamespace(
        runs_dir=runs_dir,
        runs={"run-c": child},
        create=lambda **kwargs: SimpleNamespace(id="run-new"),
    )
    return SubagentChannel(manager, parent), parent_ws, child_ws, runs_dir


class SpawnWriteBranchTest(unittest.TestCase):
    def test_write_mode_creates_writer_children_with_copy_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            channel, parent_ws, _, _ = _channel_with_runs(tmp_path)
            created: list[dict] = []
            channel.manager.create = lambda **kwargs: (
                created.append(kwargs) or SimpleNamespace(id=f"run-{len(created)}")
            )
            with mock.patch("console_subagents.wait_for_children",
                            return_value={"timed_out": False, "states": {}}):
                channel.spawn({"objectives": ["改 a.txt", "改另一个文件"], "write_access": True})
            self.assertEqual(len(created), 2)
            for kwargs in created:
                self.assertEqual(kwargs["template"], "general-subagent-writer")
                self.assertEqual(kwargs["spec"]["workspace_copy_from"], str(parent_ws))
            # 只读模式不带拷贝源
            created.clear()
            with mock.patch("console_subagents.wait_for_children",
                            return_value={"timed_out": False, "states": {}}):
                channel.spawn({"objectives": ["调查"]})
            self.assertEqual(created[0]["template"], "general-subagent")
            self.assertNotIn("workspace_copy_from", created[0]["spec"])

    def test_write_mode_child_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, *_ = _channel_with_runs(Path(tmp))
            objectives = [f"任务{i}" for i in range(MAX_WRITE_CHILDREN + 1)]
            with self.assertRaises(ValueError):
                channel.spawn({"objectives": objectives, "write_access": True})
            # 只读模式允许 4 个
            self.assertEqual(len(normalize_spawn_request(objectives, None)), 4)


class ApplyChangesTest(unittest.TestCase):
    def test_apply_modified_and_added_skips_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, parent_ws, _, _ = _channel_with_runs(Path(tmp))
            result = channel.apply_changes("run-c")
            self.assertEqual(sorted(result["applied"]), ["a.txt", "c.txt"])
            self.assertEqual((parent_ws / "a.txt").read_text(), "child a")
            self.assertEqual((parent_ws / "c.txt").read_text(), "child c")
            self.assertEqual(result["conflicts"], [])
            # 显式点名未改动的文件 → 记为 unchanged(默认范围只含已变更文件)
            subset = channel.apply_changes("run-c", ["b.txt"])
            self.assertEqual(subset["applied"], [])
            self.assertEqual(subset["unchanged"], ["b.txt"])

    def test_conflict_when_parent_changed_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, parent_ws, _, _ = _channel_with_runs(Path(tmp))
            _write(parent_ws, "a.txt", "parent changed meanwhile")
            result = channel.apply_changes("run-c")
            self.assertEqual([row["path"] for row in result["conflicts"]], ["a.txt"])
            self.assertEqual((parent_ws / "a.txt").read_text(), "parent changed meanwhile",
                             "父的新改动绝不能被旧子版本覆盖")
            self.assertEqual(result["applied"], ["c.txt"])

    def test_deleted_files_are_reported_not_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, parent_ws, child_ws, _ = _channel_with_runs(Path(tmp))
            (child_ws / "b.txt").unlink()
            result = channel.apply_changes("run-c")
            self.assertEqual(result["deleted_reported"], ["b.txt"])
            self.assertTrue((parent_ws / "b.txt").is_file(), "删除不自动应用")

    def test_paths_subset_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, parent_ws, _, _ = _channel_with_runs(Path(tmp))
            result = channel.apply_changes("run-c", ["c.txt"])
            self.assertEqual(result["applied"], ["c.txt"])
            self.assertEqual((parent_ws / "a.txt").read_text(), "parent a")

    def test_apply_file_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, _, child_ws, _ = _channel_with_runs(Path(tmp))
            for i in range(MAX_APPLY_FILES + 1):
                _write(child_ws, f"bulk/f{i}.txt", "x")
            with self.assertRaises(ValueError):
                channel.apply_changes("run-c")

    def test_refuse_foreign_or_running_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, _, _, _ = _channel_with_runs(Path(tmp))
            with self.assertRaises(ValueError):
                channel.apply_changes("run-other")
            channel.manager.runs["run-c"].finished = False
            with self.assertRaises(ValueError):
                channel.apply_changes("run-c")


class SeedingTest(unittest.TestCase):
    def _driver_stub(self, tmp: Path, source: str | None):
        from console_run_driver import RunDriver

        run_dir = tmp / "runs" / "run-c"
        run_dir.mkdir(parents=True)
        parent_ws = tmp / "runs" / "run-p" / "workspace"
        parent_ws.mkdir(parents=True, exist_ok=True)
        _write(parent_ws, "seed.txt", "seed content")
        stub = SimpleNamespace(
            run_dir=run_dir,
            run=SimpleNamespace(spec={"workspace_copy_from": source} if source else {}),
            manager=SimpleNamespace(runs_dir=tmp / "runs"),
        )
        return RunDriver._seed_workspace_copy, stub, run_dir, parent_ws

    def test_seed_copies_and_writes_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed, stub, run_dir, parent_ws = self._driver_stub(Path(tmp), "PLACEHOLDER")
            stub.run.spec = {"workspace_copy_from": str(parent_ws)}
            seed(stub)
            self.assertEqual((run_dir / "workspace" / "seed.txt").read_text(), "seed content")
            baseline = json.loads((run_dir / "workspace_baseline.json").read_text())
            self.assertIn("seed.txt", baseline)

    def test_seed_refuses_source_outside_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed, stub, _, _ = self._driver_stub(Path(tmp), str(Path(tmp) / "outside"))
            outside = Path(tmp) / "outside"
            outside.mkdir()
            stub.run.spec = {"workspace_copy_from": str(outside)}
            with self.assertRaises(ValueError):
                seed(stub)

    def test_no_copy_source_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed, stub, run_dir, _ = self._driver_stub(Path(tmp), None)
            seed(stub)
            self.assertFalse((run_dir / "workspace_baseline.json").exists())


class AssemblyTest(unittest.TestCase):
    def _world_with(self, tmp: str, channel):
        from flowagent.tools.general.sandbox import WorkspaceSandbox
        from flowagent.tools.general.world import GeneralWorld

        return GeneralWorld({}, sandbox=WorkspaceSandbox(Path(tmp) / "ws"),
                            subagent_channel=channel)

    def test_apply_tool_registered_with_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, *_ = _channel_with_runs(Path(tmp))
            names = {spec.name for spec in self._world_with(tmp, channel).build_tools()}
            self.assertIn("apply_child_changes", names)
            self.assertIn("spawn_subagents", names)
            without = {spec.name for spec in self._world_with(tmp, None).build_tools()}
            self.assertNotIn("apply_child_changes", without)

    def test_apply_tool_effect_is_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel, *_ = _channel_with_runs(Path(tmp))
            specs = {spec.name: spec for spec in self._world_with(tmp, channel).build_tools()}
            self.assertEqual(specs["apply_child_changes"].effect, "write")
            spawn = specs["spawn_subagents"]
            self.assertIn("write_access", {p.name for p in spawn.params})

    def test_writer_scenario_cannot_spawn(self) -> None:
        from flowagent.demo.fixtures import SCENARIOS

        writer = SCENARIOS["general-subagent-writer"]
        self.assertNotIn("spawn_subagents", writer.allowed_tools, "写子代理不能再派生")
        self.assertIn("write_file", writer.allowed_tools)
        self.assertIn("edit_file", writer.allowed_tools)
        research = SCENARIOS["general-research"]
        self.assertIn("apply_child_changes", research.allowed_tools)
        self.assertIn("apply_child_changes", research.grant["allowed_tools"])


if __name__ == "__main__":
    unittest.main()

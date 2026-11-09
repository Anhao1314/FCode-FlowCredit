"""Run-driver failures must preserve the original error and legal task states."""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_run_driver import RunDriver  # noqa: E402


class _Loop:
    def run(self, _task_id):
        raise RuntimeError("original boom")


class _Store:
    def __init__(self, task):
        self.task = task

    def load(self, _task_id):
        return self.task


class _Harness:
    def __init__(self):
        self.target = None
        self.event = None

    def finalize(self, task, target, **kwargs):
        self.target = target
        self.event = kwargs["event"]
        task.status = target
        return task


class _Observer:
    def terminal(self, _task, _result):
        return None


class ConsoleRunDriverTests(unittest.TestCase):
    def driver_for(self, status):
        task = SimpleNamespace(id="task-1", status=status)
        driver = RunDriver.__new__(RunDriver)
        driver.run = SimpleNamespace(error="")
        driver.scenario = SimpleNamespace(crash_at=None, reconcile=False)
        driver.loop = _Loop()
        driver.store = _Store(task)
        driver.harness = _Harness()
        driver.observer = _Observer()
        return driver, task

    def test_created_failure_uses_legal_denied_terminal(self):
        driver, task = self.driver_for("created")
        self.assertEqual(driver._execute_task(task), "error")
        self.assertEqual(task.status, "denied")
        self.assertEqual(driver.harness.event, "tool_denied")
        self.assertIn("original boom", driver.run.error)
        self.assertNotIn("ILLEGAL_STATE_TRANSITION", driver.run.error)

    def test_running_failure_remains_failed(self):
        driver, task = self.driver_for("running")
        self.assertEqual(driver._execute_task(task), "error")
        self.assertEqual(task.status, "failed")
        self.assertEqual(driver.harness.event, "task_failed")

    def _limits_driver(self, *, fast: bool):
        from flowagent.runtime.budget import BudgetLimits

        driver = RunDriver.__new__(RunDriver)
        driver.fast = fast
        driver.resume_limits = None
        driver.run = SimpleNamespace(
            spec={"project": {"path": "/tmp/repo"}, "fast": fast},
        )
        driver.scenario = SimpleNamespace(
            limits=BudgetLimits(8, 16, 200_000, 100_000),
        )
        driver.rt = SimpleNamespace(BudgetLimits=BudgetLimits)
        return driver

    def test_fast_project_envelope_is_not_price_shrunk(self):
        limits = self._limits_driver(fast=True)._limits()
        self.assertEqual(limits.max_calls, 60)
        self.assertEqual(limits.max_reads, 300)
        self.assertEqual(limits.max_cost_microusd, 3_000_000)
        self.assertEqual(limits.max_tokens, 1_200_000)
        slow = self._limits_driver(fast=False)._limits()
        self.assertEqual(slow.max_tokens, 300_000)
        self.assertEqual(slow.max_cost_microusd, 1_000_000)

    def test_worktree_is_used_for_a_clean_git_source(self):
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "src"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.email", "t@e.st"], check=True)
            subprocess.run(["git", "-C", str(source), "config", "user.name", "t"], check=True)
            (source / "a.txt").write_text("hi\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(source), "add", "a.txt"], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "init"], check=True)
            driver = RunDriver.__new__(RunDriver)
            driver.run_dir = pathlib.Path(tmp) / "runs" / "r1"
            driver.run_dir.mkdir(parents=True)
            self.assertTrue(driver._try_worktree({"path": str(source)}))
            self.assertTrue((driver.run_dir / "workspace" / "a.txt").is_file())
            self.assertEqual(driver.project["worktree"], True)
            self.assertEqual(driver.project["source"], str(source.resolve()))
            # A dirty source has no baseline that matches what the operator sees.
            (source / "a.txt").write_text("changed\n", encoding="utf-8")
            driver2 = RunDriver.__new__(RunDriver)
            driver2.run_dir = pathlib.Path(tmp) / "runs" / "r2"
            driver2.run_dir.mkdir(parents=True)
            self.assertFalse(driver2._try_worktree({"path": str(source)}))


if __name__ == "__main__":
    unittest.main()

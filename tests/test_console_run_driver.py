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


if __name__ == "__main__":
    unittest.main()

"""Work approvals: run-wide memory is risk- and argument-scoped, never tool-wide."""

from __future__ import annotations

import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from console_runtime import RunControl  # noqa: E402
from flowagent.core.plan import Plan  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.control import RoundControl, ToolControl  # noqa: E402


class FakeHarness:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, task, event, detail):  # noqa: ANN001
        self.events.append((event, detail))
        return task

    def transition(self, task, status, event, detail):  # noqa: ANN001
        task.status = status
        self.events.append((event, detail))
        return task


def _control(approval="consequential"):
    run = SimpleNamespace(
        id="run-remember", approval=approval, pending=None,
        lock=threading.Lock(), bump=lambda: None,
    )
    task = SimpleNamespace(status="planned")
    harness = FakeHarness()
    runtime = SimpleNamespace(
        Plan=Plan, BudgetLimits=BudgetLimits,
        RoundControl=RoundControl, ToolControl=ToolControl,
    )
    control = RunControl(
        run, harness, runtime,
        ("run_command", "write_file", "request_budget_extension"),
        30,
    )
    return control, task, harness


def _run_command(args):
    return {"tool": "run_command", "arguments": {"command": "npm", "args": args}}


class RememberKeyTests(unittest.TestCase):
    def test_high_risk_tools_are_never_remembered(self) -> None:
        control, task, _ = _control()
        step = {"tool": "request_budget_extension", "arguments": {"reason": "need tokens"}}
        approval = control.prepare_approval(round_no=1, task=task, step=step)[1]
        self.assertIsNotNone(approval)
        self.assertFalse(approval["rememberable"])
        control.apply_approval_response(
            round_no=1, task=task, step=step, response={"allow": True, "remember": True})
        self.assertEqual(control._remembered, set())
        # The next identical request asks again.
        again = control.prepare_approval(
            round_no=2, task=task,
            step={"tool": "request_budget_extension", "arguments": {"reason": "again"}},
        )[1]
        self.assertIsNotNone(again)

    def test_run_command_memory_is_per_argv(self) -> None:
        control, task, harness = _control()
        step = _run_command(["test"])
        approval = control.prepare_approval(round_no=1, task=task, step=step)[1]
        self.assertTrue(approval["rememberable"])
        self.assertIn("相同", approval["remember_scope"])
        control.apply_approval_response(
            round_no=1, task=task, step=step, response={"allow": True, "remember": True})
        self.assertEqual(len(control._remembered), 1)
        # Same argv: auto-granted, with the match key on the chain.
        same = control.prepare_approval(round_no=2, task=task, step=_run_command(["test"]))[1]
        self.assertIsNone(same)
        self.assertEqual(harness.events[-1][0], "approval_granted")
        self.assertIn("match_key", harness.events[-1][1])
        # Different argv: a new decision is required.
        other = control.prepare_approval(round_no=3, task=task, step=_run_command(["build"]))[1]
        self.assertIsNotNone(other)

    def test_write_memory_is_per_path(self) -> None:
        control, task, _ = _control()
        step = {"tool": "write_file", "arguments": {"path": "src/a.py", "content": "x"}}
        control.prepare_approval(round_no=1, task=task, step=step)
        control.apply_approval_response(
            round_no=1, task=task, step=step, response={"allow": True, "remember": True})
        same = control.prepare_approval(
            round_no=2, task=task,
            step={"tool": "write_file", "arguments": {"path": "src/a.py", "content": "y"}},
        )[1]
        self.assertIsNone(same)
        other = control.prepare_approval(
            round_no=3, task=task,
            step={"tool": "write_file", "arguments": {"path": "src/b.py", "content": "x"}},
        )[1]
        self.assertIsNotNone(other)


if __name__ == "__main__":
    unittest.main()

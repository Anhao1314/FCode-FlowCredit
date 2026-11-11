"""POST /api/runs/{id}/recover 与 Manager 恢复分支：202 / 409 / 默认额度。

全离线：HTTP handler 用假对象直接调用路由；Manager 用真实 SQLite task 做
guard，线程启动被打桩，不会拉起 Runtime 或任何真实 provider。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_post import _RUN_ROUTES, post_run_recover  # noqa: E402
from console_post_runs import RECOVER_DEFAULT_LIMITS  # noqa: E402
from console_runtime import Run  # noqa: E402
from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class _Run:
    def __init__(self, run_id: str = "run-x") -> None:
        self.id = run_id
        self.status = "recovery_required"
        self.finished = True

    def to_json(self, *, detail: bool = False) -> dict:  # noqa: ARG002
        return {"id": self.id, "status": self.status}


class _Manager:
    def __init__(self) -> None:
        self.run = _Run()
        self.calls = []
        self.error = ""

    def get(self, run_id: str):
        return self.run if run_id == self.run.id else None

    def recover_unknown(self, run, *, limits: dict, extra_rounds: int):
        if self.error:
            raise ValueError(self.error)
        self.calls.append((run, dict(limits), extra_rounds))
        run.status = "running"
        return run


class _Handler:
    def __init__(self, body: dict | None = None, manager=None) -> None:
        self.body = body or {}
        self.manager = manager or _Manager()
        self.responses = []

    def _read_body(self) -> dict:
        return self.body

    def _json(self, payload, status: int = 200):
        self.responses.append((status, payload))
        return payload


class RecoverRouteTests(unittest.TestCase):
    def test_route_is_registered(self) -> None:
        self.assertIs(_RUN_ROUTES["/recover"], post_run_recover)

    def test_empty_body_returns_202_with_documented_defaults(self) -> None:
        handler = _Handler()
        post_run_recover(handler, "run-x")
        status, payload = handler.responses[-1]
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "running")
        run, limits, extra_rounds = handler.manager.calls[0]
        self.assertEqual(run.id, "run-x")
        self.assertEqual(limits, {
            "maxCalls": 40,
            "maxReads": 160,
            "maxCostMicrousd": 1_000_000,
            "maxTokens": 300_000,
        })
        self.assertEqual(extra_rounds, 40)
        self.assertEqual(RECOVER_DEFAULT_LIMITS["extraRounds"], 40)

    def test_explicit_body_overrides_defaults(self) -> None:
        handler = _Handler({
            "maxCalls": 55, "maxReads": 220,
            "maxCostMicrousd": 2_000_000, "maxTokens": 500_000,
            "extraRounds": 12,
        })
        post_run_recover(handler, "run-x")
        self.assertEqual(handler.responses[-1][0], 202)
        _, limits, extra_rounds = handler.manager.calls[0]
        self.assertEqual(limits["maxCalls"], 55)
        self.assertEqual(limits["maxTokens"], 500_000)
        self.assertEqual(extra_rounds, 12)

    def test_wrong_run_state_is_409(self) -> None:
        manager = _Manager()
        manager.error = "run cannot be recovered from failed/BUDGET_EXCEEDED"
        handler = _Handler(manager=manager)
        post_run_recover(handler, "run-x")
        status, payload = handler.responses[-1]
        self.assertEqual(status, 409)
        self.assertIn("cannot be recovered", payload["error"])

    def test_unknown_run_is_404(self) -> None:
        handler = _Handler()
        post_run_recover(handler, "no-such-run")
        status, payload = handler.responses[-1]
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "no such run")
        self.assertEqual(handler.manager.calls, [])

    def test_invalid_limits_are_400_and_never_dispatch(self) -> None:
        for body in ({"maxCalls": 0}, {"maxTokens": -1}, {"extraRounds": 51},
                     {"maxReads": "many"}):
            with self.subTest(body=body):
                handler = _Handler(body)
                post_run_recover(handler, "run-x")
                status, _ = handler.responses[-1]
                self.assertEqual(status, 400)
                self.assertEqual(handler.manager.calls, [])


class _Thread:
    """Capture the thread kwargs instead of starting a real Runtime."""

    captured: dict = {}

    def __init__(self, target=None, args=(), kwargs=None, name="", daemon=False):  # noqa: ARG002
        _Thread.captured = {"target": target, "args": args, "kwargs": kwargs or {}}

    def start(self) -> None:
        pass


class _Scripted:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def propose(self, context):  # noqa: ARG002
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ManagerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        import console_manager

        self._tmp = tempfile.TemporaryDirectory()
        self.runs_dir = Path(self._tmp.name)
        self.manager = console_manager.RunManager.__new__(console_manager.RunManager)
        self.manager.runtime = SimpleNamespace(TaskStore=TaskStore)
        self.manager.runs_dir = self.runs_dir
        self.run = Run(
            id="run-unknown", template="full", objective="x", created="", pace=0,
        )
        self.run.finished = True
        self.run.status = "recovery_required"
        self.run.task_id = "task-unknown"
        self._build_unknown_task()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _build_unknown_task(self) -> None:
        db_dir = self.runs_dir / self.run.id
        self.store = TaskStore(db_dir / "task.sqlite")
        harness = Harness(
            store=self.store,
            registry=ToolRegistry([]),
            provider=_Scripted(),
            world=None,
            limits=BudgetLimits(10, 10, 100_000, 100_000),
            receipts=ReceiptStore(self.store),
        )
        task = harness.create(
            task_id=self.run.task_id, objective="x", principal="test",
            binding={}, allowed_tools=["ls_workspace"], grant={},
        )
        provider = _Scripted(
            AgentError("MODEL_CALL_FAILED", "dropped"),
            AgentError("MODEL_CALL_FAILED", "dropped again"),
        )
        task, _ = harness._propose(task, {"round": 1, "state": {}}, provider)
        self.assertEqual(task.status, "recovery_required")
        self.assertEqual(task.failure["code"], "MODEL_OUTCOME_UNKNOWN")
        self.store.close()

    def test_resume_refuses_unknown_and_points_at_the_new_entry(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.manager.resume(
                self.run,
                limits={"maxCalls": 40, "maxReads": 160, "maxCostMicrousd": 1_000_000},
            )
        self.assertIn("cannot be resumed", str(caught.exception))
        self.assertIn("/api/runs/{id}/recover", str(caught.exception))
        self.assertEqual(self.run.status, "recovery_required")

    def test_recover_unknown_sets_running_and_passes_recovery_kwargs(self) -> None:
        limits = {
            "maxCalls": 40, "maxReads": 160,
            "maxCostMicrousd": 1_000_000, "maxTokens": 300_000,
        }
        with mock.patch("console_manager.threading.Thread", _Thread):
            self.manager.recover_unknown(self.run, limits=limits, extra_rounds=40)
        self.assertEqual(_Thread.captured["target"], self.manager._run_thread)
        kwargs = _Thread.captured["kwargs"]
        self.assertTrue(kwargs["recover_unknown"])
        self.assertEqual(kwargs["resume_limits"], limits)
        self.assertEqual(kwargs["extra_rounds"], 40)
        self.assertEqual(self.run.status, "running")
        self.assertFalse(self.run.finished)

    def test_recover_unknown_rejects_a_failed_task(self) -> None:
        # A budget failure is the resume() domain, not the unknown-recovery one.
        store = TaskStore(self.runs_dir / self.run.id / "task.sqlite")
        try:
            task = store.load(self.run.task_id)
            # recovery_required -> running -> failed is a legal chain, with a
            # non-unknown failure code.
            running = store.save(task, task.sequence, "task_resumed", {})
            failed = running.advance("failed")
            failed.failure = {"code": "BUDGET_EXCEEDED"}
            store.save(failed, running.sequence, "task_failed", {"code": "BUDGET_EXCEEDED"})
        finally:
            store.close()
        with self.assertRaises(ValueError) as caught:
            self.manager.recover_unknown(self.run, limits={}, extra_rounds=40)
        self.assertIn("cannot be recovered", str(caught.exception))

    def test_driver_recovery_branch_uses_recover_unknown_not_resume(self) -> None:
        """The driver must never call the failed-only resume on an unknown run."""
        import console_run_driver as drv
        from console_run_driver import RunDriver

        driver = RunDriver.__new__(RunDriver)
        task = SimpleNamespace(id="task-unknown", model=SimpleNamespace(attempts=6))
        calls: list = []

        class _Harness:
            def resume(self, *args, **kwargs):  # noqa: ARG002
                raise AssertionError("resume() only accepts failed tasks")

            def recover_unknown(self, given, *, operator: str):
                calls.append(("recover_unknown", given.id, operator))
                return given

        class _Loop:
            def restore(self, given):
                calls.append(("restore", given.id))
                return SimpleNamespace(state=lambda: {"restored": True})

        driver.recover_unknown = True
        driver.resume_limits = {"maxCalls": 40}  # set, so the branch order matters
        driver.extra_rounds = 40
        driver.run = SimpleNamespace(id="run-unknown", task_id="task-unknown", lock=None)
        driver.store = SimpleNamespace(load=lambda task_id: task)
        driver.harness = _Harness()
        driver.recorder = object()
        driver.scope = []
        driver.module = SimpleNamespace(make_planner=lambda world: object())
        driver.world = SimpleNamespace(
            apply_state=lambda state: calls.append(("apply_state", state)),
        )
        driver.scenario = SimpleNamespace(max_rounds=12)
        driver.db = "db"
        driver._new_loop = lambda: _Loop()

        with mock.patch.object(drv, "LiveObserver", lambda *args: object()), \
             mock.patch.object(drv.threading, "Thread", _Thread):
            result = driver._prepare_task()

        self.assertIs(result, task)
        self.assertEqual(driver.max_rounds, 46, "attempts + extra_rounds")
        self.assertEqual(calls[0], ("recover_unknown", "task-unknown", "console-operator"))
        self.assertIn(("restore", "task-unknown"), calls)
        self.assertIn(("apply_state", {"restored": True}), calls)


if __name__ == "__main__":
    unittest.main()

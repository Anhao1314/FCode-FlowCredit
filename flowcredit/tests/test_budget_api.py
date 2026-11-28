"""S1 operator APIs: /budget and /resume accept absolute maxTokens + request ids."""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_post_runs import post_run_budget, post_run_resume  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402


class _Control:
    def __init__(self) -> None:
        self.limits = BudgetLimits(12, 30, 500_000, 120_000)
        self.commands: list[dict] = []

    def current_limits(self):
        return self.limits.to_dict()

    def queue(self, command):
        self.commands.append(dict(command))


class _Run:
    def __init__(self, status="running", finished=False) -> None:
        self.id = "run-budget-api"
        self.status = status
        self.finished = finished
        self.controller = _Control()

    def to_json(self, *, detail=False):  # noqa: ARG002
        return {"id": self.id, "status": self.status}


class _Manager:
    def __init__(self, run) -> None:
        self.run = run
        self.resume_calls: list[dict] = []

    def get(self, run_id):
        return self.run if run_id == self.run.id else None

    def resume(self, run, *, limits, extra_rounds=6, request_id=None):
        self.resume_calls.append({
            "limits": dict(limits), "extra_rounds": extra_rounds, "requestId": request_id,
        })
        run.status = "running"
        return run


class _Handler:
    def __init__(self, body, manager):
        self.body = body
        self.manager = manager
        self.responses = []

    def _read_body(self):
        return self.body

    def _json(self, payload, status=200):
        self.responses.append((status, payload))
        return payload


class BudgetRouteTests(unittest.TestCase):
    def test_budget_accepts_tokens_and_request_id(self) -> None:
        run = _Run()
        manager = _Manager(run)
        handler = _Handler({
            "maxCalls": 18, "maxReads": 30, "maxCostMicrousd": 500_000,
            "maxTokens": 180_000, "requestId": "req-ui-1",
        }, manager)
        post_run_budget(handler, run.id)
        self.assertEqual(handler.responses[-1][0], 200)
        command = run.controller.commands[-1]
        self.assertEqual(command["maxTokens"], 180_000)
        self.assertEqual(command["requestId"], "req-ui-1")

    def test_budget_rejects_lowering_tokens(self) -> None:
        run = _Run()
        handler = _Handler({
            "maxCalls": 12, "maxReads": 30, "maxCostMicrousd": 500_000,
            "maxTokens": 100_000,
        }, _Manager(run))
        post_run_budget(handler, run.id)
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertEqual(run.controller.commands, [])


class ResumeRouteTests(unittest.TestCase):
    def test_resume_passes_tokens_and_request_id(self) -> None:
        run = _Run(status="failed", finished=True)
        manager = _Manager(run)
        handler = _Handler({
            "maxCalls": 18, "maxReads": 30, "maxCostMicrousd": 500_000,
            "maxTokens": 180_000, "extraRounds": 6, "requestId": "resume-ui-1",
        }, manager)
        post_run_resume(handler, run.id)
        self.assertEqual(handler.responses[-1][0], 202)
        call = manager.resume_calls[-1]
        self.assertEqual(call["limits"]["maxTokens"], 180_000)
        self.assertEqual(call["requestId"], "resume-ui-1")
        self.assertEqual(call["extra_rounds"], 6)

    def test_resume_without_tokens_still_passes_through(self) -> None:
        run = _Run(status="failed", finished=True)
        manager = _Manager(run)
        handler = _Handler({
            "maxCalls": 18, "maxReads": 30, "maxCostMicrousd": 500_000,
        }, manager)
        post_run_resume(handler, run.id)
        self.assertEqual(handler.responses[-1][0], 202)
        self.assertNotIn("maxTokens", manager.resume_calls[-1]["limits"])


if __name__ == "__main__":
    unittest.main()

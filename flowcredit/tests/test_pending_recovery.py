"""Durable operator waits must survive a real console process restart."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from server_harness import ConsoleServer, request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run(server: ConsoleServer, run_id: str) -> dict:
    status, body = request(server.url(f"/api/runs/{run_id}"))
    if status != 200:
        raise AssertionError(f"run lookup failed: {status} {body}")
    return json.loads(body)


def _wait_for(server: ConsoleServer, run_id: str, predicate, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    latest = {}
    while time.monotonic() < deadline:
        latest = _run(server, run_id)
        if predicate(latest):
            return latest
        time.sleep(0.1)
    raise AssertionError(f"run did not reach the expected state: {latest}")


class PendingRecoveryTests(unittest.TestCase):
    def test_approval_survives_process_restart_and_executes_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            runs = root / "runs"
            config = root / "config"
            runs.mkdir()
            config.mkdir()
            first = ConsoleServer(runs_dir=runs, config_dir=config)
            second = None
            run_id = ""
            try:
                first.start()
                status, body = request(
                    first.url("/api/runs"), method="POST",
                    body=json.dumps({
                        "objective": "创建 durable.txt，写入一次可验证内容",
                        "template": "general-research",
                        "approval": "all",
                        "model": "mock",
                        "spec": {"mode": "open"},
                    }).encode("utf-8"),
                )
                self.assertEqual(status, 202, body)
                run_id = json.loads(body)["id"]
                waiting = _wait_for(first, run_id, lambda run: bool((run.get("pending") or {}).get("tool")))
                pending = waiting["pending"]

                # A hard kill leaves exactly the durable evidence a real crash leaves.
                first.process.kill()
                first.process.wait(timeout=10)
                first.stop()

                second = ConsoleServer(runs_dir=runs, config_dir=config).start()
                restored = _wait_for(
                    second, run_id,
                    lambda run: bool((run.get("pending") or {}).get("tool")),
                    timeout=10,
                )
                self.assertEqual(restored["pending"]["id"], pending["id"])
                self.assertEqual(restored["pending"]["tool"], pending["tool"])

                status, body = request(
                    second.url(f"/api/runs/{run_id}/decision"), method="POST",
                    body=json.dumps({"approvalId": pending["id"], "allow": True, "remember": False}).encode("utf-8"),
                )
                self.assertEqual(status, 200, body)
                finished = _wait_for(second, run_id, lambda run: run.get("status") == "succeeded")
                self.assertIsNone(finished["pending"])

                status, _ = request(
                    second.url(f"/api/runs/{run_id}/decision"), method="POST",
                    body=json.dumps({"approvalId": pending["id"], "allow": True, "remember": False}).encode("utf-8"),
                )
                self.assertEqual(status, 409)

                status, body = request(second.url(f"/api/runs/{run_id}/trace"))
                self.assertEqual(status, 200, body)
                events = json.loads(body)["events"]
                approval = next(event for event in events if event["type"] == "approval_requested")
                approval_payload = (approval.get("payload") or {}).get("detail") or {}
                approval_round = approval_payload.get("round")
                same_round_models = [
                    event for event in events
                    if event["type"] == "model_started" and ((event.get("payload") or {}).get("detail") or {}).get("round") == approval_round
                ]
                self.assertEqual(len(same_round_models), 1, "审批恢复不得重放原模型轮")
                self.assertEqual(
                    len([event for event in events if event["type"] == "approval_requested"]), 1,
                )
                approved_writes = [
                    event for event in events
                    if event["type"] == "tool_started"
                    and ((event.get("payload") or {}).get("detail") or {}).get("round") == approval_round
                    and ((event.get("payload") or {}).get("detail") or {}).get("tool") == approval_payload.get("tool")
                ]
                self.assertEqual(len(approved_writes), 1, "获批工具必须恰好执行一次")
            finally:
                first.stop()
                if second is not None:
                    second.stop()
    def test_restored_approval_rejection_has_no_tool_side_effect(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fcode-pending-deny-") as tmp:
            root = pathlib.Path(tmp)
            runs_dir = root / "runs"
            config_dir = root / "config"
            runs_dir.mkdir()
            config_dir.mkdir()
            first = ConsoleServer(runs_dir=runs_dir, config_dir=config_dir)
            second = None
            try:
                first.start()
                status, body = request(
                    first.url("/api/runs"), method="POST",
                    body=json.dumps({"objective": "创建 durable.txt", "template": "general-research",
                                     "model": "mock", "approval": "all", "spec": {"mode": "open"}}).encode("utf-8"),
                )
                self.assertEqual(status, 202, body)
                run_id = json.loads(body)["id"]
                pending = _wait_for(first, run_id, lambda run: run.get("pending"))["pending"]
                first.process.kill()
                first.process.wait(timeout=10)
                first.stop()
                second = ConsoleServer(runs_dir=runs_dir, config_dir=config_dir)
                second.start()
                restored = _wait_for(
                    second, run_id,
                    lambda run: (run.get("pending") or {}).get("id") == pending["id"],
                )
                self.assertEqual(restored["pending"]["tool"], pending["tool"])
                status, body = request(
                    second.url(f"/api/runs/{run_id}/decision"), method="POST",
                    body=json.dumps({"approvalId": pending["id"], "allow": False, "remember": False}).encode("utf-8"),
                )
                self.assertEqual(status, 200, body)
                denied = _wait_for(second, run_id, lambda run: run.get("status") == "denied")
                self.assertIsNone(denied["pending"])
                status, body = request(second.url(f"/api/runs/{run_id}/trace"))
                self.assertEqual(status, 200, body)
                events = json.loads(body)["events"]
                self.assertFalse([
                    event for event in events
                    if event["type"] == "tool_started"
                    and (event.get("payload") or {}).get("detail", {}).get("tool") == pending["tool"]
                ], "拒绝后不得执行原工具")
                status, body = request(
                    second.url(f"/api/runs/{run_id}/decision"), method="POST",
                    body=json.dumps({"approvalId": pending["id"], "allow": True, "remember": False}).encode("utf-8"),
                )
                self.assertEqual(status, 409, body)
            finally:
                first.stop()
                if second is not None:
                    second.stop()


if __name__ == "__main__":
    unittest.main()

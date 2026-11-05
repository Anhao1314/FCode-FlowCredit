"""Past runs after a restart: the list, the detail and the trace.

The desktop bundle keeps its runs in Application Support, outside the viewer
checkout, so ``runs/<id>/trace.json`` is not a static path there. Opening a
finished run used to answer 404 in exactly that deployment — these tests pin
the read path that works in both.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "viewer" / "server.py"
VIEWER = ROOT / "viewer"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(VIEWER))

from console_common import FINISHED_STATUS, run_trace_path  # noqa: E402
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from server_harness import ConsoleServer  # noqa: E402


def load_console_manager():
    # console_manager imports its sibling modules by bare name; the viewer dir
    # must be on sys.path exactly the way server.py puts it there.
    spec = importlib.util.spec_from_file_location(
        "fcode_console_manager_history", VIEWER / "console_manager.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_run(runs_dir: pathlib.Path, run_id: str, **meta) -> pathlib.Path:
    """One run directory as a previous process would have left it."""
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": run_id,
        "template": "general-research",
        "objective": "整理周三杭州行程",
        "created": "2026-10-25T15:21:04+00:00",
        "status": "succeeded",
        "rounds": 5,
        "events": 40,
        "tracePath": f"runs/{run_id}/trace.json",
        "verified": True,
        "taskId": f"task-{run_id}",
        "approval": "consequential",
        "cost": {"calls": 5, "reads": 5, "costMicrousd": 2500, "tokens": 5000},
        "model": "kimi",
        "effort": "high",
        "spec": {"mode": "script", "source": "chat-action"},
    }
    payload.update(meta)
    (run_dir / "meta.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return run_dir


def write_trace(run_dir: pathlib.Path, *, head: str = "a" * 64) -> None:
    trace = {
        "version": "flowagent.trace/v1",
        "task": {"id": "task-1", "objective": "整理周三杭州行程", "status": "succeeded"},
        "rounds": [{"index": 1, "observation": {"tool": "write_file", "result": {"path": "script.py", "changed": True}}}],
        "events": [{"sequence": 1, "type": "task_created", "payload": {}, "prev_hash": None, "hash": head, "at": "2026-10-25T15:21:04Z"}],
        "chain": {"event_count": 1, "head_hash": head, "algorithm": "sha256"},
        "terminal": {"status": "succeeded"},
    }
    (run_dir / "trace.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")


class RunTracePathTests(unittest.TestCase):
    """``run_trace_path`` is the only place that decides which file a trace is."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = pathlib.Path(self._tmp.name) / "runs"
        self.runs.mkdir()

    def test_a_normal_run_id_resolves_inside_the_runs_dir(self) -> None:
        write_run(self.runs, "run-20261025-152104-fa89")
        write_trace(self.runs / "run-20261025-152104-fa89")
        path = run_trace_path(self.runs, "run-20261025-152104-fa89")
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "trace.json")
        self.assertEqual(path.parent.parent, self.runs.resolve())

    def test_ids_that_are_not_one_directory_name_are_refused(self) -> None:
        for run_id in ("", "  ", "../etc", "a/b", ".hidden", ".", "..", "run-1/../../etc"):
            with self.subTest(run_id=run_id):
                self.assertIsNone(run_trace_path(self.runs, run_id))

    def test_a_symlinked_run_directory_cannot_escape(self) -> None:
        outside = pathlib.Path(self._tmp.name) / "outside"
        outside.mkdir()
        write_trace(outside)
        (self.runs / "run-evil").symlink_to(outside)
        self.assertIsNone(run_trace_path(self.runs, "run-evil"))

    def test_a_run_without_a_trace_yet_resolves_but_is_absent(self) -> None:
        write_run(self.runs, "run-20261025-999999-zzzz")
        path = run_trace_path(self.runs, "run-20261025-999999-zzzz")
        self.assertIsNotNone(path)
        self.assertFalse(path.is_file())


class HistoryRestoreTests(unittest.TestCase):
    """A restart rebuilds the record read-only, and tells the truth about it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.runs = pathlib.Path(self._tmp.name) / "runs"
        self.runs.mkdir()

    def reload(self):
        """A fresh manager over the same runs dir: the restart under test."""
        return load_console_manager().RunManager(None, "", 0.0, runs_dir=self.runs)

    def test_a_finished_run_comes_back_with_its_recorded_facts(self) -> None:
        write_run(self.runs, "run-20261025-100000-aaaa")
        manager = self.reload()
        row = manager.list_runs()[0]
        self.assertEqual(row["id"], "run-20261025-100000-aaaa")
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["objective"], "整理周三杭州行程")
        self.assertEqual(row["rounds"], 5)
        self.assertEqual(row["cost"]["costMicrousd"], 2500)
        self.assertTrue(row["verified"])
        detail = manager.get("run-20261025-100000-aaaa").to_json(detail=True)
        # Model and effort must survive too: resuming a restored run with the
        # provider silently falling back to the mock would be a lie.
        self.assertEqual(detail["model"], "kimi")
        self.assertEqual(detail["effort"], "high")

    def test_restore_is_read_only(self) -> None:
        write_run(self.runs, "run-20261025-100000-bbbb")
        run = self.reload().get("run-20261025-100000-bbbb")
        self.assertTrue(run.finished)
        self.assertIsNone(run.snapshot)     # nothing pretends to be a live trace
        self.assertIsNone(run.controller)
        self.assertIsNone(run.pending)

    def test_a_run_interrupted_by_the_restart_is_stopped_not_running(self) -> None:
        write_run(self.runs, "run-20261025-110000-cccc", status="running")
        manager = self.reload()
        run = manager.get("run-20261025-110000-cccc")
        self.assertEqual(run.status, "stopped")
        self.assertTrue(run.finished)
        self.assertIn("进程重启", run.error)

    def test_every_finished_status_survives_a_restart(self) -> None:
        for index, status in enumerate(FINISHED_STATUS):
            write_run(self.runs, f"run-20261025-12000{index}-dddd", status=status, created=f"2026-10-25T12:0{index}:00+00:00")
        manager = self.reload()
        recorded = {run.status for run in manager.runs.values()}
        self.assertEqual(recorded, set(FINISHED_STATUS))
        self.assertEqual(manager.list_runs()[0]["status"], FINISHED_STATUS[-1])  # newest first

    def test_history_is_ordered_newest_first(self) -> None:
        write_run(self.runs, "run-20261025-090000-eeee", created="2026-10-25T09:00:00+00:00")
        write_run(self.runs, "run-20261025-180000-ffff", created="2026-10-25T18:00:00+00:00")
        manager = self.reload()
        self.assertEqual(
            [row["id"] for row in manager.list_runs()],
            ["run-20261025-180000-ffff", "run-20261025-090000-eeee"],
        )

    def test_a_damaged_meta_is_skipped_without_hiding_the_rest(self) -> None:
        write_run(self.runs, "run-20261025-100000-1111")
        broken = self.runs / "run-20261025-100000-2222"
        broken.mkdir()
        (broken / "meta.json").write_text("{not json", encoding="utf-8")
        noid = self.runs / "run-20261025-100000-3333"
        noid.mkdir()
        (noid / "meta.json").write_text(json.dumps({"objective": "没有 id"}), encoding="utf-8")
        garbled = self.runs / "run-20261025-100000-4444"
        garbled.mkdir()
        (garbled / "meta.json").write_text(json.dumps({"id": "run-20261025-100000-4444", "rounds": "五"}), encoding="utf-8")
        (self.runs / "not-a-run").mkdir()
        manager = self.reload()
        self.assertEqual([row["id"] for row in manager.list_runs()], ["run-20261025-100000-1111"])

    def test_meta_records_the_model_it_ran_with(self) -> None:
        """The write side of the same contract: without model/effort in
        meta.json a restored run could not be resumed with its own provider."""
        write_run(self.runs, "run-20261025-100000-5555")
        manager = self.reload()
        run = manager.get("run-20261025-100000-5555")
        run.model = "kimi"
        run.effort = "medium"
        manager._write_meta(run)
        meta = json.loads((self.runs / run.id / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["model"], "kimi")
        self.assertEqual(meta["effort"], "medium")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(url: str, *, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class HistoricalRunServerTests(unittest.TestCase):
    """A new process serves the runs an old one left behind.

    The runs directory is deliberately outside the viewer checkout, which is
    the deployment shape of the desktop bundle and the reason the static
    ``runs/...`` path used to 404.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = pathlib.Path(cls.tmp.name)
        runs = root / "runs"
        runs.mkdir()
        write_run(runs, "run-20261025-130000-aaaa")
        write_trace(runs / "run-20261025-130000-aaaa", head="b" * 64)
        write_run(runs, "run-20261025-120000-bbbb", status="running")
        write_run(runs, "run-20261025-110000-cccc")  # meta only: no trace on disk yet
        # 服务端子进程的输出必须留下：DEVNULL 时代 CI 连红二十次都无从定位。
        cls.server = ConsoleServer(runs_dir=runs, config_dir=root / "config", timeout=30)
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        cls.port = cls.server.port

    def url(self, suffix: str) -> str:
        return f"http://127.0.0.1:{self.port}{suffix}"

    def test_the_list_shows_runs_from_the_previous_process(self) -> None:
        status, body = request(self.url("/api/runs"))
        self.assertEqual(status, 200, body)
        runs = {row["id"]: row for row in json.loads(body)}
        self.assertEqual(
            sorted(runs),
            ["run-20261025-110000-cccc", "run-20261025-120000-bbbb", "run-20261025-130000-aaaa"],
        )
        self.assertEqual(runs["run-20261025-130000-aaaa"]["status"], "succeeded")
        self.assertEqual(runs["run-20261025-120000-bbbb"]["status"], "stopped")

    def test_the_trace_route_serves_a_run_outside_the_static_root(self) -> None:
        status, body = request(self.url("/api/runs/run-20261025-130000-aaaa/trace"))
        self.assertEqual(status, 200, body)
        trace = json.loads(body)
        self.assertEqual(trace["chain"]["head_hash"], "b" * 64)
        self.assertEqual(trace["rounds"][0]["observation"]["tool"], "write_file")
        # The static path that the old code used answers 404 here by design.
        self.assertEqual(request(self.url("/runs/run-20261025-130000-aaaa/trace.json"))[0], 404)

    def test_the_detail_route_serves_a_restored_run(self) -> None:
        status, body = request(self.url("/api/runs/run-20261025-130000-aaaa"))
        self.assertEqual(status, 200, body)
        detail = json.loads(body)
        self.assertEqual(detail["objective"], "整理周三杭州行程")
        self.assertEqual(detail["model"], "kimi")
        self.assertEqual(detail["tracePath"], "runs/run-20261025-130000-aaaa/trace.json")

    def test_a_run_without_a_trace_on_disk_is_a_clear_404(self) -> None:
        status, body = request(self.url("/api/runs/run-20261025-110000-cccc/trace"))
        self.assertEqual(status, 404, body)
        self.assertIn("no trace on disk", json.loads(body)["error"])

    def test_an_unknown_or_traversing_id_is_refused(self) -> None:
        for suffix in (
            "/api/runs/run-20261025-000000-zzzz/trace",
            "/api/runs/..%2F..%2Fetc/trace",
            "/api/runs/%2Fetc%2Fpasswd/trace",
        ):
            with self.subTest(suffix=suffix):
                status, _ = request(self.url(suffix))
                self.assertIn(status, (400, 404))


if __name__ == "__main__":
    unittest.main()
"""Console memory wiring: the artifact book is real, and delete cannot crash.

`ArtifactBook` was a library nobody instantiated while `delete()` referenced
it, so removing a run raised AttributeError. These tests pin the wiring: one
book beside the run history, idempotent ingestion of a finished trace, and a
tombstone (never a row deletion) when the run goes away.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEWER = ROOT / "viewer"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(VIEWER))


def load_module(name: str, path: pathlib.Path):
    # console_manager imports its siblings by bare name; the viewer dir must be
    # on sys.path exactly the way server.py puts it there.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_trace(*, path: str = "script.py", sha: str = "a" * 64, round_no: int = 1) -> dict:
    return {
        "version": "flowagent.trace/v1",
        "task": {"id": "task-1", "objective": "整理行程", "status": "succeeded"},
        "rounds": [
            {
                "index": round_no,
                "observation": {
                    "tool": "write_file",
                    "result": {
                        "path": path,
                        "sha256_after": sha,
                        "bytes": 318,
                        "changed": True,
                    },
                },
            },
        ],
        "events": [{"sequence": 1, "type": "task_created", "payload": {}, "prev_hash": None, "hash": "b" * 64}],
        "chain": {"event_count": 1, "head_hash": "b" * 64, "algorithm": "sha256"},
        "terminal": {"status": "succeeded", "result": {"verified": True}},
    }


class MemoryWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.console_manager = load_module(
            "fcode_console_manager_memory", VIEWER / "console_manager.py"
        )
        cls.console_runtime = sys.modules["console_runtime"]

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()
        # Keep the case/fact stores inside the temp dir: the console points them
        # at the runs dir's parent, and a test must not write into the checkout.
        patcher = mock.patch.dict(
            os.environ, {"FCODE_MEMORY_DB": str(self.root / "memory" / "cases.sqlite")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def manager(self, runtime=None):
        return self.console_manager.RunManager(runtime, "", 0.0, runs_dir=self.runs)

    def runtime(self):
        return self.console_runtime.Runtime(ROOT)

    def add_run(self, manager, run_id: str = "run-20261026-000000-aaaa"):
        run = self.console_runtime.Run(
            id=run_id,
            template="general-research",
            objective="整理行程",
            created="2026-10-26T00:00:00+00:00",
            pace=0.0,
            status="succeeded",
            finished=True,
        )
        manager.runs[run.id] = run
        manager.order.insert(0, run.id)
        (self.runs / run.id).mkdir(parents=True, exist_ok=True)
        return run

    def test_deleting_a_run_without_a_runtime_does_not_crash(self) -> None:
        """The regression: `delete()` referenced an attribute that never existed."""
        manager = self.manager()
        self.assertIsNone(manager.artifact_book)
        self.assertEqual(manager.delete("run-nope"), (False, "no such run"))

    def test_the_book_lives_beside_the_run_history(self) -> None:
        manager = self.manager(self.runtime())
        self.assertIsNotNone(manager.artifact_book)
        run = self.add_run(manager)
        manager._ingest_memory(manager.runtime, run, make_trace())
        self.assertTrue((self.root / "artifacts.sqlite").is_file())
        hits = manager.artifact_book.by_path("script.py")
        self.assertEqual([hit.run_id for hit in hits], [run.id])
        self.assertEqual(hits[0].sha256, "a" * 64)

    def test_ingestion_is_idempotent(self) -> None:
        manager = self.manager(self.runtime())
        run = self.add_run(manager)
        trace = make_trace()
        manager._ingest_memory(manager.runtime, run, trace)
        manager._ingest_memory(manager.runtime, run, trace)
        self.assertEqual(len(manager.artifact_book.by_path("script.py")), 1)

    def test_deleting_a_run_tombstones_its_artifacts(self) -> None:
        manager = self.manager(self.runtime())
        run = self.add_run(manager)
        manager._ingest_memory(manager.runtime, run, make_trace())
        ok, note = manager.delete(run.id)
        self.assertTrue(ok, note)
        self.assertFalse((self.runs / run.id).exists())
        hits = manager.artifact_book.by_path("script.py")
        self.assertTrue(hits, "a tombstone is not a row deletion")
        self.assertTrue(hits[0].archived)

    def test_the_book_works_from_another_thread(self) -> None:
        """The regression that mattered: the console ingests from a run's worker
        thread, and a connection built in ``__init__`` belongs to the thread
        that built it — sqlite refuses it elsewhere, which used to escape and
        leave the run stuck at "running" forever."""
        import threading

        manager = self.manager(self.runtime())
        run = self.add_run(manager)
        failure: list[BaseException] = []

        def worker():
            try:
                manager._ingest_memory(manager.runtime, run, make_trace())
            except BaseException as error:  # noqa: BLE001 — the test asserts it did not happen
                failure.append(error)

        thread = threading.Thread(target=worker, name="run-thread")
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(failure, [], f"ingestion from a worker thread failed: {failure}")
        # …and the reading side, in this thread, sees it.
        self.assertEqual(len(manager.artifact_book.by_path("script.py")), 1)

    def test_history_restore_rebuilds_a_manager_without_re_ingesting(self) -> None:
        """A restart re-lists a run but does not touch the book: the projection
        was written when the run finished."""
        manager = self.manager(self.runtime())
        run = self.add_run(manager)
        manager._ingest_memory(manager.runtime, run, make_trace())
        (self.runs / run.id / "meta.json").write_text(
            '{"id": "%s", "template": "general-research", "status": "succeeded",'
            ' "created": "2026-10-26T00:00:00+00:00", "rounds": 1}' % run.id,
            encoding="utf-8",
        )
        fresh = self.manager(self.runtime())
        self.assertIn(run.id, fresh.runs)
        self.assertEqual(len(fresh.artifact_book.by_path("script.py")), 1)


if __name__ == "__main__":
    unittest.main()
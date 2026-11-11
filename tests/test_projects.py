"""Work project registry: identity, path validation, archive, restart."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from projects import ProjectStore, normalize_project_path  # noqa: E402


class ProjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.project_dir = self.root / "demo"
        self.project_dir.mkdir()
        self.db = self.root / "projects.sqlite"
        self.store = ProjectStore(self.db)
        self.addCleanup(self.store.close)
        self.addCleanup(self._tmp.cleanup)

    def test_create_list_and_get(self) -> None:
        project = self.store.create("Demo", str(self.project_dir), "python3 verify.py")
        self.assertTrue(project["id"].startswith("prj-"))
        self.assertEqual(project["name"], "Demo")
        self.assertEqual(project["path"], str(self.project_dir.resolve()))
        self.assertEqual(project["verification"], "python3 verify.py")
        self.assertEqual(project["archived"], 0)
        self.assertEqual(self.store.get(project["id"])["name"], "Demo")
        self.assertEqual(self.store.by_path(project["path"])["id"], project["id"])
        self.assertEqual([row["id"] for row in self.store.list()], [project["id"]])

    def test_path_is_normalized_and_unique(self) -> None:
        project = self.store.create("Demo", str(self.project_dir))
        with self.assertRaises(ValueError):
            self.store.create("Again", str(self.project_dir))
        self.assertEqual(
            normalize_project_path(f"{self.project_dir}/../demo"),
            project["path"],
        )

    def test_relative_and_missing_paths_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create("Relative", "demo")
        with self.assertRaises(ValueError):
            self.store.create("Missing", str(self.root / "nope"))

    def test_update_and_archive(self) -> None:
        project = self.store.create("Demo", str(self.project_dir))
        updated = self.store.update(
            project["id"], name="Renamed", verification="make test")
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual(updated["verification"], "make test")

        archived = self.store.archive(project["id"])
        self.assertEqual(archived["archived"], 1)
        self.assertEqual(self.store.list(), [])
        self.assertEqual(
            [row["id"] for row in self.store.list(include_archived=True)],
            [project["id"]],
        )

    def test_unknown_project_is_a_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.store.update("prj-missing", name="x")

    def test_rows_survive_a_restart(self) -> None:
        project = self.store.create("Demo", str(self.project_dir))
        self.store.close()
        reopened = ProjectStore(self.db)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(project["id"])["path"], project["path"])


class RunProjectFieldsTests(unittest.TestCase):
    def test_project_id_and_attention_are_in_the_list_payload(self) -> None:
        from console_runtime import Run

        run = Run(
            id="run-20261111-000000-aaaa",
            template="general-research",
            objective="demo",
            created="2026-11-11T00:00:00+00:00",
            pace=0.0,
            project_id="prj-1234",
        )
        payload = run.to_json()
        self.assertEqual(payload["projectId"], "prj-1234")
        self.assertEqual(payload["attention"], "")

        run.pending = {"kind": "approval", "token": "secret"}
        self.assertEqual(run.attention, "approval")
        self.assertEqual(run.to_json()["attention"], "approval")
        self.assertNotIn("pending", run.to_json(),
                         "the list payload still withholds the pending details")

        run.pending = None
        run.status = "failed"
        self.assertEqual(run.to_json()["attention"], "failed")
        run.status = "recovery_required"
        self.assertEqual(run.to_json()["attention"], "recovery")
        run.status = "error"
        self.assertEqual(run.to_json()["attention"], "error")


if __name__ == "__main__":
    unittest.main()
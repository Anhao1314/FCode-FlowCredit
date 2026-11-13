"""HTTP surface for Work projects and project-scoped runs."""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import time
import unittest
import uuid

from tests.server_harness import ConsoleServer, request


class ProjectApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls._tmp.name)
        cls.server = ConsoleServer(
            runs_dir=cls.root / "runs",
            config_dir=cls.root / "config",
            timeout=30,
        )
        cls.server.start()
        # LIFO cleanups: stop the server before deleting its runs directory.
        # A run thread (or a git child it spawned) can still be settling, so
        # retry the rmtree briefly; a leftover temp dir is better than a red test.
        def _cleanup_tmp() -> None:
            for _ in range(10):
                try:
                    cls._tmp.cleanup()
                    return
                except OSError:
                    time.sleep(0.2)
            cls._tmp.cleanup(ignore_cleanup_errors=True)
        cls.addClassCleanup(_cleanup_tmp)
        cls.addClassCleanup(cls.server.stop)

    def _new_dir(self, prefix: str) -> pathlib.Path:
        path = self.root / f"{prefix}-{uuid.uuid4().hex[:6]}"
        path.mkdir()
        (path / "README.md").write_text("demo\n", encoding="utf-8")
        return path

    def _post(self, path: str, payload: dict):
        return request(
            self.server.url(path),
            method="POST",
            body=json.dumps(payload).encode("utf-8"),
        )

    def _create_project(self, name: str, path: pathlib.Path,
                        verification: str = "true") -> dict:
        status, body = self._post("/api/projects", {
            "name": name, "path": str(path), "verification": verification,
        })
        self.assertEqual(status, 201, body)
        return json.loads(body)

    def test_create_and_list(self) -> None:
        target = self._new_dir("demo")
        project = self._create_project("Demo", target)
        self.assertTrue(project["id"].startswith("prj-"))
        self.assertEqual(project["path"], str(target.resolve()))

        status, body = request(self.server.url("/api/projects"))
        self.assertEqual(status, 200, body)
        projects = json.loads(body)["projects"]
        found = next(item for item in projects if item["id"] == project["id"])
        self.assertEqual(found["name"], "Demo")
        self.assertEqual(found["runs"], 0)
        self.assertEqual(found["running"], 0)
        self.assertEqual(found["attention"], 0)

    def test_duplicate_and_missing_paths_are_refused(self) -> None:
        target = self._new_dir("dupe")
        project = self._create_project("Duplicate", target)
        status, body = self._post("/api/projects", {
            "name": "Again", "path": str(target),
        })
        self.assertEqual(status, 400, body)
        self.assertIn("已注册", json.loads(body)["error"])

        status, body = self._post("/api/projects", {
            "name": "Missing", "path": str(self.root / "nope"),
        })
        self.assertEqual(status, 400, body)
        self.assertIn("不存在", json.loads(body)["error"])
        self.assertTrue(project["id"])

    def test_project_run_binds_path_and_verification(self) -> None:
        project = self._create_project("Runner", self._new_dir("runner"))
        status, body = self._post("/api/runs", {
            "template": "general-research",
            "objective": "读取工作区并给出结论",
            "projectId": project["id"],
            "approval": "off",
            "model": "mock",
            "spec": {"mode": "open"},
        })
        self.assertEqual(status, 202, body)
        run = json.loads(body)
        self.assertEqual(run["projectId"], project["id"])
        self.assertEqual(run["spec"]["project"]["path"], project["path"])
        self.assertEqual(run["spec"]["verification"]["command"], "true")
        self.assertEqual(run["spec"]["project_id"], project["id"])

        status, body = request(
            self.server.url(f"/api/runs?projectId={project['id']}"))
        self.assertEqual(status, 200, body)
        rows = json.loads(body)
        self.assertIn(run["id"], [row["id"] for row in rows])
        row = next(item for item in rows if item["id"] == run["id"])
        self.assertEqual(row["projectId"], project["id"])

    def test_deleted_project_is_refused_before_a_run_is_created(self) -> None:
        target = self._new_dir("deleted-before-run")
        project = self._create_project("DeletedBeforeRun", target)
        shutil.rmtree(target)

        status, body = self._post("/api/runs", {
            "template": "general-research",
            "objective": "这个目录已经在绑定后删除",
            "projectId": project["id"],
            "approval": "off",
            "model": "mock",
            "spec": {"mode": "open"},
        })
        self.assertEqual(status, 400, body)
        error = json.loads(body)
        self.assertEqual(error["code"], "PROJECT_NOT_FOUND")
        self.assertIn("project directory is not readable", error["error"])

        status, body = request(
            self.server.url(f"/api/runs?projectId={project['id']}"))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), [], "preflight 失败不得留下 0 轮 error 运行")

    def test_unknown_or_archived_project_is_refused(self) -> None:
        status, body = self._post("/api/runs", {
            "template": "general-research",
            "objective": "x",
            "projectId": "prj-does-not-exist",
            "spec": {"mode": "open"},
        })
        self.assertEqual(status, 400, body)

        project = self._create_project("Archived", self._new_dir("archived"))
        status, body = self._post("/api/projects/archive", {"id": project["id"]})
        self.assertEqual(status, 200, body)
        status, body = self._post("/api/runs", {
            "template": "general-research",
            "objective": "x",
            "projectId": project["id"],
            "spec": {"mode": "open"},
        })
        self.assertEqual(status, 400, body)
        self.assertIn("已归档", json.loads(body)["error"])

    def test_finance_templates_cannot_use_a_work_project(self) -> None:
        project = self._create_project(
            "FinanceGuard", self._new_dir("finance"))
        status, body = self._post("/api/runs", {
            "template": "full",
            "objective": "x",
            "projectId": project["id"],
        })
        self.assertEqual(status, 400, body)
        self.assertIn("通用工作区", json.loads(body)["error"])

    def test_a_run_can_be_moved_between_projects(self) -> None:
        first = self._create_project("MoveFrom", self._new_dir("move-from"))
        second = self._create_project("MoveTo", self._new_dir("move-to"))
        status, body = self._post("/api/runs", {
            "template": "general-research",
            "objective": "移动归属",
            "projectId": first["id"],
            "approval": "off",
            "model": "mock",
            "spec": {"mode": "open"},
        })
        self.assertEqual(status, 202, body)
        run_id = json.loads(body)["id"]

        status, body = self._post(f"/api/runs/{run_id}/project", {
            "projectId": second["id"],
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["projectId"], second["id"])

        status, body = request(
            self.server.url(f"/api/runs?projectId={second['id']}"))
        self.assertEqual(status, 200, body)
        self.assertIn(run_id, [row["id"] for row in json.loads(body)])

        status, body = self._post(f"/api/runs/{run_id}/project", {"projectId": ""})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["projectId"], "")


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from flowagent.core.contracts import AgentError
from flowagent.tools.general.project import (
    PROJECT_REPORT_PATH,
    import_project,
    project_delivery,
    validate_project_spec,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
        (self.source / ".env.example").write_text("API_URL=\n", encoding="utf-8")
        self.sandbox = WorkspaceSandbox(self.root / "run" / "workspace")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_import_baseline_edit_and_patch(self) -> None:
        original = hashlib.sha256((self.source / "app.py").read_bytes()).hexdigest()
        metadata = import_project(self.source, self.sandbox)
        self.assertTrue(metadata["isolatedCopy"])
        self.assertEqual(metadata["files"], 2)
        self.assertEqual(metadata["skipped"]["files"], 0)
        self.assertTrue((self.sandbox.root / ".env.example").exists())
        self.assertFalse((self.source / ".git").exists())

        self.sandbox.edit("app.py", "return 41", "return 42")
        self.sandbox.write("new_test.py", "def test_answer():\n    assert True\n")
        self.sandbox.write(PROJECT_REPORT_PATH, "fixed\n")
        (self.sandbox.root / "__pycache__").mkdir()
        (self.sandbox.root / "__pycache__" / "app.pyc").write_bytes(b"generated")
        delivery = project_delivery(self.sandbox, metadata)
        self.assertEqual({item["path"] for item in delivery["filesChanged"]}, {"app.py", "new_test.py"})
        self.assertIn("-    return 41", delivery["patch"])
        self.assertIn("+    return 42", delivery["patch"])
        self.assertIn("new_test.py", delivery["patch"])
        self.assertNotIn(PROJECT_REPORT_PATH, delivery["patch"])
        self.assertNotIn("__pycache__", delivery["patch"])
        self.assertEqual(hashlib.sha256((self.source / "app.py").read_bytes()).hexdigest(), original)
        self.assertFalse((self.source / "new_test.py").exists())

    def test_sensitive_dependencies_and_links_are_not_copied(self) -> None:
        (self.source / ".env").write_text("TOP_SECRET=do-not-copy\n", encoding="utf-8")
        (self.source / "credentials.json").write_text("{}", encoding="utf-8")
        (self.source / "private.pem").write_text("secret", encoding="utf-8")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "dependency.js").write_text("x", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        os.symlink(outside, self.source / "linked.txt")
        os.symlink(self.root, self.source / "linked-dir")
        metadata = import_project(self.source, self.sandbox)
        self.assertEqual(metadata["skipped"]["directories"], 1)
        self.assertEqual(metadata["skipped"]["files"], 3)
        self.assertEqual(metadata["skipped"]["symlinks"], 2)
        self.assertEqual({item["path"] for item in self.sandbox.ls()}, {"app.py", ".env.example"})

    def test_limits_empty_and_recursive_destinations_fail_closed(self) -> None:
        with self.assertRaisesRegex(AgentError, "import limit"):
            import_project(self.source, self.sandbox, max_files=1)
        with self.assertRaisesRegex(AgentError, "import limit"):
            import_project(self.source, WorkspaceSandbox(self.root / "small"), max_bytes=1)
        with self.assertRaisesRegex(AgentError, "contain one another"):
            import_project(self.source, WorkspaceSandbox(self.source / "run"))
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(AgentError, "no importable files"):
            import_project(empty, WorkspaceSandbox(self.root / "empty-run"))

    def test_spec_requires_an_absolute_path(self) -> None:
        self.assertEqual(validate_project_spec({"path": str(self.source)})["path"], str(self.source.resolve()))
        for invalid in (None, {}, {"path": "relative"}, {"path": str(self.source), "write": True}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_project_spec(invalid)


if __name__ == "__main__":
    unittest.main()

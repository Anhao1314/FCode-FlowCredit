"""Fixer delegation creates a Work repair run with an isolated project spec."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from console_manager import RunManager  # noqa: E402


class StubManager:
    def __init__(self):
        self.created = None
        self.validated = None
        self.runtime = SimpleNamespace(
            PROJECT_REPORT_PATH="result.md",
            get_scenario=lambda name: SimpleNamespace(module="general-research"),
        )

    def validate_delegation_spec(self, spec, scenario):
        self.validated = spec
        return spec

    def resolve_model(self, key, effort):
        return ("mock", "")

    def create(self, *args, **kwargs):
        self.created = (args, kwargs)
        return SimpleNamespace(id="run-1")


class CreateFixRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = pathlib.Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.manager = StubManager()

    def test_builds_open_project_spec_and_consequential_run(self):
        run = RunManager.create_fix_run(
            self.manager, "修复 F-1", str(self.project), "pytest -q", model_key="mock",
        )
        self.assertEqual(run.id, "run-1")
        args, kwargs = self.manager.created
        self.assertEqual(args[0], "修复 F-1")
        self.assertEqual(args[1], "general-research")
        self.assertEqual(args[2], "consequential")
        spec = self.manager.validated
        self.assertEqual(spec["mode"], "open")
        self.assertEqual(spec["project"]["path"], str(self.project.resolve()))
        self.assertEqual(spec["verification"]["command"], "pytest")
        self.assertEqual(spec["verification"]["args"], ["-q"])
        self.assertEqual(spec["verification"]["expectExitCode"], 0)
        self.assertEqual(kwargs["source"], "governance-fix")
        self.assertEqual(kwargs["model_key"], "mock")

    def test_requires_verification_and_absolute_directory(self):
        with self.assertRaisesRegex(ValueError, "verification"):
            RunManager.create_fix_run(self.manager, "obj", str(self.project), "  ")
        with self.assertRaisesRegex(ValueError, "absolute"):
            RunManager.create_fix_run(self.manager, "obj", "relative/path", "pytest")


if __name__ == "__main__":
    unittest.main()

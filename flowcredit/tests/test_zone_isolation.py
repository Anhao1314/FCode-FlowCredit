"""Zone isolation: Work and Governance share a process, not their stores.

The Chat/Work split is the reference standard: separate services, separate
stores, separate capability sets and an explicit bridge. These tests hold the
Work/Governance boundary to the same standard:

* the HTTP layer translates audit requests but does not import ``flowaudit``;
* ``AuditService`` owns the audit root and refuses paths outside it;
* the Work tool registry has no audit capability;
* importing a project into a Work workspace drops ``work/`` and ``runs/``, so
  audit artifacts are never copied into an agent's view.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "src"))

from audit_service import AuditService  # noqa: E402
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402
from flowagent.tools.general.project import import_project  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


class ServerBoundaryTests(unittest.TestCase):
    def test_http_layer_does_not_import_the_audit_product(self) -> None:
        for name in ("server.py", "console_post.py"):
            imports = _imports(ROOT / "viewer" / name)
            offenders = [item for item in imports
                        if item == "flowaudit" or item.startswith("flowaudit.")
                        or item == "audit_jobs"]
            self.assertEqual(offenders, [], f"{name} must go through AuditService")

    def test_audit_service_is_the_one_owner_of_the_audit_product(self) -> None:
        imports = _imports(ROOT / "viewer" / "audit_service.py")
        self.assertIn("audit_jobs", imports)
        self.assertIn("flowaudit.session", imports)
        # The service may use the kernel store for chain verification, but it
        # must not import the Work manager or the run driver.
        forbidden = [item for item in imports
                     if item in ("console_manager", "console_run_driver",
                                 "console_runtime")]
        self.assertEqual(forbidden, [])


class AuditServicePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name) / "audit"
        self.audit_dir = self.root / "demo-target"
        self.audit_dir.mkdir(parents=True)
        (self.audit_dir / "audit-bundle.json").write_text(json.dumps({
            "audit_id": "demo-target",
            "findings": [{"finding_id": "F-POC-0001"}],
        }), encoding="utf-8")
        (self.audit_dir / "POC-0001.json").write_text("{}", encoding="utf-8")
        self.outside = pathlib.Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "audit-bundle.json").write_text("{}", encoding="utf-8")
        self.service = AuditService(repo=None, root=self.root, runtime=None)
        self.addCleanup(self._tmp.cleanup)

    def test_reads_stay_inside_the_audit_root(self) -> None:
        self.assertEqual(self.service.bundle("demo-target")["audit_id"], "demo-target")
        self.assertEqual(self.service.bundle("../outside"), ({"error": "not found"}, 404))
        self.assertEqual(self.service.bundle(".."), ({"error": "not found"}, 404))
        self.assertEqual(self.service.bundle("DEMO-TARGET"), ({"error": "not found"}, 404))
        self.assertEqual(
            self.service.evidence("demo-target", "..%2FPOC-0001"),
            ({"error": "not found"}, 404),
        )
        self.assertEqual(
            self.service.rea("demo-target", "../fact.json"),
            ({"error": "not found"}, 404),
        )

    def test_symlink_escape_is_refused(self) -> None:
        link = self.audit_dir / "linked"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks are unavailable")
        self.assertEqual(self.service.bundle("linked"), ({"error": "not found"}, 404))

    def test_disposition_writes_the_bundle_and_skips_missing_chain(self) -> None:
        result = self.service.disposition("demo-target", "F-POC-0001", "accepted_risk")
        self.assertEqual(result["operator_disposition"], "accepted_risk")
        bundle = json.loads((self.audit_dir / "audit-bundle.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["findings"][0]["operator_disposition"], "accepted_risk")
        self.assertEqual(
            self.service.disposition("demo-target", "missing", "accepted_risk"),
            ({"error": "finding not found"}, 404),
        )
        self.assertEqual(
            self.service.disposition("demo-target", "F-POC-0001", "bogus"),
            ({"error": "invalid disposition; must be one of ('', accepted_risk, "
                       "false_positive_disputed, wont_fix, repaired)"}, 400),
        )

    def test_audit_root_can_be_overridden_for_deployment(self) -> None:
        with mock.patch.dict("os.environ", {"FCODE_AUDIT_DIR": str(self.root)}):
            service = AuditService(repo=pathlib.Path("/tmp/unused"), runtime=None)
        self.assertEqual(service.root, self.root.resolve())

    def test_start_run_requires_a_repo(self) -> None:
        with self.assertRaises(ValueError):
            self.service.start_run("target")


class WorkCapabilityTests(unittest.TestCase):
    def test_work_tool_registry_has_no_audit_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
            names = [spec.name for spec in build_general_tools(sandbox, UrlFetcher())]
        self.assertFalse([name for name in names
                          if name == "audit" or name.startswith("audit_")],
                         names)
        self.assertNotIn("audit_run", names)

    def test_project_import_excludes_audit_and_run_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "source"
            (source / "src").mkdir(parents=True)
            (source / "src" / "code.py").write_text("print('ok')\n", encoding="utf-8")
            (source / "work" / "audit" / "demo").mkdir(parents=True)
            (source / "work" / "audit" / "demo" / "audit-bundle.json").write_text(
                '{"secret": true}', encoding="utf-8")
            (source / "runs").mkdir()
            (source / "runs" / "run-1").mkdir()
            workspace = pathlib.Path(tmp) / "workspace"
            import_project(source, WorkspaceSandbox(workspace))
            self.assertTrue((workspace / "src" / "code.py").is_file())
            self.assertFalse((workspace / "work").exists(),
                             "Work must never receive the audit root")
            self.assertFalse((workspace / "runs").exists(),
                             "Work must never receive run records")


if __name__ == "__main__":
    unittest.main()
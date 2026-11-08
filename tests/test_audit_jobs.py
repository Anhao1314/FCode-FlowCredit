"""G1 治理台:审计作业的目标包含性——repo 内默认放行,repo 外须显式放行根。

FCODE_AUDIT_ROOTS 是操作员显式配置(os.pathsep 分隔的绝对根目录):
发运工件(shipped artifact,如 /Applications/EvoX.app)在仓库外,
没有放行根时拒绝——默认面保持"对话发起的审计够不着仓库外"的安全语义。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from audit_jobs import _audit_roots, _within, resolve_adapter, start_audit_run  # noqa: E402


class ResolveAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path("/tmp/fcode-fixture-repo").resolve()

    def test_inside_repo_defaults_to_repo_adapter(self) -> None:
        self.assertEqual(resolve_adapter(self.repo / "src", self.repo), "repo")

    def test_outside_repo_defaults_to_rea_adapter(self) -> None:
        self.assertEqual(resolve_adapter(Path("/Applications/Tool.app"), self.repo), "rea")

    def test_explicit_adapter_respected_and_validated(self) -> None:
        self.assertEqual(resolve_adapter(self.repo / "src", self.repo, "rea"), "rea")
        with self.assertRaises(ValueError):
            resolve_adapter(self.repo / "src", self.repo, "bogus")


class AuditJobContainmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        self._old_roots = os.environ.pop("FCODE_AUDIT_ROOTS", None)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if self._old_roots is not None:
            os.environ["FCODE_AUDIT_ROOTS"] = self._old_roots
        else:
            os.environ.pop("FCODE_AUDIT_ROOTS", None)

    def test_repo_relative_target_allowed(self) -> None:
        job = start_audit_run(self.repo, "src")
        self.assertTrue(job.startswith("audit-"))

    def test_outside_repo_rejected_without_roots(self) -> None:
        outside = Path(self._tmp.name) / "apps" / "Tool.app"
        outside.mkdir(parents=True)
        with self.assertRaises(ValueError) as ctx:
            start_audit_run(self.repo, str(outside))
        self.assertIn("escapes", str(ctx.exception))

    def test_outside_repo_allowed_with_explicit_root(self) -> None:
        outside = Path(self._tmp.name) / "apps" / "Tool.app"
        outside.mkdir(parents=True)
        os.environ["FCODE_AUDIT_ROOTS"] = str(outside.parent)
        job = start_audit_run(self.repo, str(outside))
        self.assertTrue(job.startswith("audit-"))

    def test_root_must_contain_target_not_merely_share_prefix(self) -> None:
        sibling = Path(self._tmp.name) / "apps2" / "Tool.app"
        sibling.mkdir(parents=True)
        os.environ["FCODE_AUDIT_ROOTS"] = str(Path(self._tmp.name) / "apps")
        with self.assertRaises(ValueError):
            start_audit_run(self.repo, str(sibling))

    def test_missing_target_rejected_even_inside_root(self) -> None:
        os.environ["FCODE_AUDIT_ROOTS"] = str(Path(self._tmp.name) / "apps")
        with self.assertRaises(ValueError) as ctx:
            start_audit_run(self.repo, str(Path(self._tmp.name) / "apps" / "Nope.app"))
        self.assertIn("not found", str(ctx.exception))

    def test_path_traversal_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            start_audit_run(self.repo, "../../etc")

    def test_audit_roots_parses_pathsep_list(self) -> None:
        a = Path(self._tmp.name) / "a"
        b = Path(self._tmp.name) / "b"
        os.environ["FCODE_AUDIT_ROOTS"] = os.pathsep.join([str(a), str(b), "  "])
        self.assertEqual(_audit_roots(self.repo.resolve()), [self.repo.resolve(), a.resolve(), b.resolve()])

    def test_within_semantics(self) -> None:
        root = Path("/tmp/apps").resolve()
        self.assertTrue(_within(root, root))
        self.assertTrue(_within(root / "Tool.app", root))
        self.assertFalse(_within(root.parent / "other", root))


if __name__ == "__main__":
    unittest.main()

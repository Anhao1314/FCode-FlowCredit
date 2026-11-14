"""Fixer write path: isolated validation, explicit approval, re-audit on apply."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from gov_fixes import GovFixService, parse_patch_paths  # noqa: E402

PATCH = """\
diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
"""


class FakeAudit:
    def __init__(self, root, repo, target, profile="source"):
        self.root = root
        self.repo = repo
        self.bundle_data = {
            "audit_id": "audit-1",
            "target": {"identity": str(target), "profile": profile},
            "findings": [{"finding_id": "F-1"}],
        }

    def bundle(self, name):
        return self.bundle_data if name == "audit-1" else ({"error": "not found"}, 404)


class GovFixServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.repo = base / "repo"
        self.target = self.repo / "project"
        self.target.mkdir(parents=True)
        (self.target / "a.txt").write_text("old\n", encoding="utf-8")
        self.audit = FakeAudit(base / "work" / "audit", self.repo, self.target)
        self.reaudited: list[str] = []
        self.service = GovFixService(
            self.audit,
            lambda target: (self.reaudited.append(target) or {"run_id": "gov-reaudit"}),
            root=base / "work" / "fixes",
        )

    def test_valid_patch_is_isolated_then_applied_on_approval(self):
        fix = self.service.create("audit-1", "F-1", PATCH)
        self.assertEqual(fix["status"], "proposed")
        self.assertEqual(fix["files"], ["a.txt"])
        self.assertEqual((self.target / "a.txt").read_text(), "old\n", "原目录不能先动")
        worktree = pathlib.Path(self.tmp.name) / "work" / "fixes" / fix["id"] / "worktree"
        self.assertEqual((worktree / "a.txt").read_text(), "old\n")

        approved = self.service.approve(fix["id"])
        self.assertEqual(approved["status"], "applied")
        self.assertEqual((self.target / "a.txt").read_text(), "new\n")
        self.assertEqual(self.reaudited, [str(self.target.resolve())])
        self.assertEqual(approved["reaudit_run_id"], "gov-reaudit")
        apply_receipt = pathlib.Path(self.tmp.name) / "work" / "fixes" / fix["id"] / "apply.json"
        self.assertTrue(apply_receipt.is_file())
        with self.assertRaises(ValueError):
            self.service.approve(fix["id"])

    def test_reject_leaves_target_untouched(self):
        fix = self.service.create("audit-1", "F-1", PATCH)
        rejected = self.service.reject(fix["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual((self.target / "a.txt").read_text(), "old\n")
        self.assertEqual(self.reaudited, [])
        with self.assertRaises(ValueError):
            self.service.reject(fix["id"])

    def test_unsafe_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_patch_paths("--- a/../../etc/passwd\n+++ b/../../etc/passwd\n")
        with self.assertRaises(ValueError):
            parse_patch_paths("--- a/.git/config\n+++ b/.git/config\n")
        with self.assertRaises(ValueError):
            self.service.create("audit-1", "F-1", "--- a/a.txt\n+++ b/a.txt\n")

    def test_artifact_and_outside_targets_are_refused(self):
        artifact = FakeAudit(self.audit.root, self.repo, self.target, profile="artifact")
        service = GovFixService(artifact, self.reaudited.append, root=self.audit.root.parent / "fixes")
        with self.assertRaisesRegex(ValueError, "artifact"):
            service.create("audit-1", "F-1", PATCH)

        outside = FakeAudit(self.audit.root, self.repo, pathlib.Path(self.tmp.name) / "outside")
        outside.bundle_data["target"]["identity"] = str(pathlib.Path(self.tmp.name) / "outside")
        (pathlib.Path(self.tmp.name) / "outside").mkdir()
        service = GovFixService(outside, None, root=self.audit.root.parent / "fixes")
        with self.assertRaisesRegex(ValueError, "escapes"):
            service.create("audit-1", "F-1", PATCH)

    def test_non_applying_patch_is_refused_and_cleaned(self):
        bad = PATCH.replace("-old", "-different")
        with self.assertRaises(ValueError):
            self.service.create("audit-1", "F-1", bad)
        fixes = self.audit.root.parent / "fixes"
        self.assertEqual(list(fixes.glob("fix-*")) if fixes.is_dir() else [], [])

    def test_unknown_audit_or_finding_is_refused(self):
        with self.assertRaises(ValueError):
            self.service.create("missing", "F-1", PATCH)
        with self.assertRaises(ValueError):
            self.service.create("audit-1", "F-404", PATCH)

    def test_list_returns_latest_records(self):
        fix = self.service.create("audit-1", "F-1", PATCH)
        rows = self.service.list()
        self.assertEqual([item["id"] for item in rows], [fix["id"]])


if __name__ == "__main__":
    unittest.main()

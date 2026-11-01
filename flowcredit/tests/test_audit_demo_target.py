"""The demo target is the 30-second showcase: audit MiniAgent end to end.

The same rules that audit FCode itself must generalize to a third-party
codebase (the demo target ships no rules of its own), the scenarios bind
to MiniAgent's own seams, and the bundle must re-verify.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowaudit import cli  # noqa: E402

DEMO_TARGET = ROOT / "fixtures" / "demo-target"


class DemoTargetAuditTests(unittest.TestCase):
    def test_demo_target_yields_both_declared_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "bundle"
            rc = cli.main(["audit", str(DEMO_TARGET), "--out", str(out),
                           "--operator", "ci-test"])
            self.assertEqual(rc, 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            groups = {f["root_cause_group"] for f in bundle["findings"]}
            self.assertEqual(
                groups, {"approval-memory", "plaintext-secret"},
                "the demo target declares exactly these two defects")

            schema = json.loads(
                (ROOT / "audit-rules" / "schema" / "poc-evidence.schema.json")
                .read_text(encoding="utf-8"))
            for ref in bundle["poc_evidence_refs"]:
                record = json.loads((out / f"{ref}.json").read_text(encoding="utf-8"))
                self.assertEqual(jsonschema_lite_validate(record, schema), [],
                                 f"{ref} must conform")
                self.assertEqual(record["replay"]["outcome"], "finding",
                                 "every declared defect must replay to a finding")

            rc_verify = cli.main(["verify-bundle", str(out / "audit-bundle.json"),
                                  "--evidence", str(out)])
            self.assertEqual(rc_verify, 0)

            # L2 验收:会话账本从链重建一致,晋升全部由 host 执行。
            from flowaudit.session import AuditSession
            reopened = AuditSession.attach(
                __import__("flowagent.core.store", fromlist=["TaskStore"])
                .TaskStore(out / "session.sqlite"),
                f"audit-{bundle['audit_id']}")
            ledgers = reopened.ledgers()
            self.assertEqual(len(ledgers["findings"]), 2)
            self.assertTrue(all(f["actor"] == "host" for f in ledgers["findings"]))
            self.assertTrue(all(c["status"] == "finding"
                                for c in ledgers["candidates"]))
            reopened.verify()

    def test_demo_target_readme_is_the_control_file_and_stays_clean(self) -> None:
        """The AGI03 rule's negative control reads the target README; if a
        future edit pastes a key-shaped literal into it, the control would
        'violate' and every demo finding would degrade to invalid_poc."""
        text = (DEMO_TARGET / "README.md").read_text(encoding="utf-8")
        import re
        self.assertIsNone(
            re.search(r"[A-Za-z_]{0,32}[Kk][Ee][Yy]\s*=\s*[\"'][A-Za-z0-9_\-]{16,}[\"']", text),
            "the demo README is the control file and must stay key-free")


def jsonschema_lite_validate(record, schema):
    from flowaudit import jsonschema_lite
    return jsonschema_lite.validate(record, schema)


if __name__ == "__main__":
    unittest.main()

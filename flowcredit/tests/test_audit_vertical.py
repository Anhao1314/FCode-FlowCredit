"""Vertical slice: rule → candidate → constrained replay → finding → bundle.

The expensive but decisive test: the audit engine runs end to end against
the real checkout (copied into a temp workdir), the FCODE-02 probe drives
the target's real RunControl inside a sandboxed worker subprocess, and
the exported bundle must validate against the frozen audit-bundle schema
and re-verify through the CLI. The draft rule (RULE-AGI02-002) must stay
invisible to detection and scoring the whole way through.
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

from flowaudit import jsonschema_lite  # noqa: E402
from flowaudit import bundle as bundle_mod  # noqa: E402
from flowaudit.vulns import engine  # noqa: E402

RULES_DIR = ROOT / "audit-rules"


class EngineTests(unittest.TestCase):
    def test_ruleset_loads_with_both_dev_rules_active(self) -> None:
        ruleset = engine.load_ruleset(RULES_DIR)
        active_ids = [rule.id for rule in engine.active_rules(ruleset)]
        self.assertIn("RULE-AGI02-001", active_ids)
        self.assertIn("RULE-AGI02-002", active_ids)

    def test_draft_rule_never_produces_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = pathlib.Path(tmp) / "pkg"
            pkg.mkdir()
            # A file that matches the trigger exactly; the rule is demoted
            # to draft in memory, and the engine must refuse to scan it.
            (pkg / "harness_like.py").write_text(
                "class H:\n    def execute(self, task, step):\n        return step\n",
                encoding="utf-8")
            ruleset = engine.load_ruleset(RULES_DIR)
            ruleset["rules"]["RULE-AGI02-002"].status = "draft"
            candidates = engine.static_scan(pathlib.Path(tmp), ruleset)
            self.assertEqual(
                [c.rule_id for c in candidates],
                [], "draft rules must not participate in detection")

    def test_active_static_trigger_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = pathlib.Path(tmp) / "pkg"
            pkg.mkdir()
            (pkg / "victim.py").write_text(
                "class C:\n"
                "    def __init__(self):\n"
                "        self._remembered = set()\n"
                "    def remember(self, tool):\n"
                "        self._remembered.add(tool)\n"
                "    def gated(self, tool):\n"
                "        return tool in self._remembered\n",
                encoding="utf-8")
            ruleset = engine.load_ruleset(RULES_DIR)
            candidates = engine.static_scan(pathlib.Path(tmp), ruleset)
            by_id = {c.rule_id: c for c in candidates}
            self.assertIn("RULE-AGI02-001", by_id)
            first = by_id["RULE-AGI02-001"].evidence[0]
            self.assertEqual(first["path"], "pkg/victim.py")
            self.assertEqual(first["line"], 5)


class EndToEndTests(unittest.TestCase):
    """Audit the real checkout, then re-verify the exported bundle."""

    def test_audit_of_this_checkout_produces_the_finding(self) -> None:
        from flowaudit import cli

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "bundle"
            rc = cli.main(["audit", str(ROOT), "--out", str(out),
                           "--operator", "ci-test", "--profile", "static"])
            self.assertEqual(rc, 0)

            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle["schema_version"], "fcode.audit-bundle/v1")
            groups = {f["root_cause_group"] for f in bundle["findings"]}
            self.assertIn("approval-memory", groups,
                          "the FCODE-02 finding must be promoted by replay")
            self.assertIn("ungated-execute", groups,
                          "the FCODE-06 finding must be promoted by replay")

            schema = json.loads(
                (RULES_DIR / "schema" / "poc-evidence.schema.json").read_text(encoding="utf-8"))
            for ref in bundle["poc_evidence_refs"]:
                record = json.loads((out / f"{ref}.json").read_text(encoding="utf-8"))
                self.assertEqual(jsonschema_lite.validate(record, schema), [],
                                 f"{ref} must conform to the poc-evidence contract")
            finding_records = [
                json.loads((out / f"{ref}.json").read_text(encoding="utf-8"))
                for ref in bundle["poc_evidence_refs"]
            ]
            self.assertEqual(
                bundle_mod.evidence_set_hash(finding_records),
                bundle["chain_verification"]["head_hash"])

            # The third-party drill, in miniature: verify-bundle from the
            # exported artifacts alone must pass.
            rc_verify = cli.main(["verify-bundle", str(out / "audit-bundle.json"),
                                  "--evidence", str(out)])
            self.assertEqual(rc_verify, 0)

    def test_replay_worker_refuses_network_escape(self) -> None:
        """The sandbox hook is not decoration: a scenario that reaches for
        the network dies loudly, which the host records as invalid_poc."""
        import subprocess
        import sys as _sys

        worker = ROOT / "src" / "flowaudit" / "poc" / "replay_worker.py"
        check = (
            "import importlib.util, sys, json;"
            f"spec=importlib.util.spec_from_file_location('rw', r'{worker}');"
            "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
            "m._install_sandbox_hooks()\n"
            "import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('http://127.0.0.1:1/', timeout=1)\n"
            "    print(json.dumps({'blocked': False}))\n"
            "except RuntimeError as exc:\n"
            "    print(json.dumps({'blocked': True, 'reason': str(exc)}))\n"
        )
        completed = subprocess.run(
            [_sys.executable, "-c", check], capture_output=True, text=True, timeout=30)
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["blocked"],
                        "the replay worker must block network escapes")


if __name__ == "__main__":
    unittest.main()

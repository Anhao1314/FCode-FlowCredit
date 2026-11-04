"""The viewer's audit API: read-only, path-safe, CSP-backed.

Three endpoints feed the audit console from `fcode audit` artifacts in
work/audit/<audit_id>/. This test plants a bundle + evidence on disk,
boots the real server harness, and checks the contract — including the
path-traversal refusals and the security headers the frontend relies on.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from server_harness import ConsoleServer  # noqa: E402

BUNDLE = {
    "schema_version": "fcode.audit-bundle/v1",
    "audit_id": "demo-target-test",
    "authorization": {"operator": "ci", "scope": "test", "granted_at": "2026-11-01T00:00:00Z"},
    "target": {
        "identity": "demo", "version": "1", "manifest_sha256": "a" * 64,
        "artifact_sha256": "c" * 64,
    },
    "sessions": [{"phase": "static", "session_ref": "cli-batch"}],
    "toolchain": {
        "replayer": "v1", "oracle_version": "1",
        "rea": {"version": "6.8.0", "evidence_schema": "rea.evidence/v1",
                "facts_schema": "fcode.rea-facts/v1", "manifest_sha256": "d" * 64},
    },
    "poc_evidence_refs": ["POC-0001"],
    "findings": [{"finding_id": "F-POC-0001", "root_cause_group": "approval-memory",
                  "category": "AGI-02", "severity": "high", "poc_refs": ["POC-0001"]}],
    "uncovered_surfaces": [{"surface": "dynamic", "reason": "static only"}],
    "rea_evidence_refs": [{
        "evidence_id": "ev_" + "e" * 64,
        "operation": "inspect_go_binary",
        "path": "rea-evidence/inspect_go_binary.json",
        "sha256": "f" * 64,
    }],
    "rea_facts_ref": {"path": "rea-evidence/facts.json", "sha256": "1" * 64},
    "rea_control_facts_ref": {"path": "rea-evidence/control-facts.json", "sha256": "2" * 64},
    "chain_verification": {"head_hash": "b" * 64, "command": "verify-bundle"},
}
EVIDENCE = {
    "schema_version": "fcode.poc-evidence/v1",
    "record_kind": "poc",
    "audit_id": "demo-target-test",
    "poc_id": "POC-0001",
    "rule_id": "RULE-AGI02-001",
    "category": "AGI-02",
    "target_ref": {"manifest_sha256": "a" * 64, "identity": "demo", "version": "1"},
    "capability_manifest": {"declared": [{"kind": "network", "scope": "none", "granted": False}],
                            "host_verdict": "within_declaration"},
    "stimulus": {"kind": "command", "spec": "s"},
    "oracle": {"kind": "event_absent", "spec": "s", "executor": "host"},
    "negative_control": {"spec": "s", "result": "as_expected"},
    "replay_environment": {"no_real_credentials": True, "network": "disabled",
                           "filesystem": "temporary", "target_copy": True},
    "expectation": "must_find",
    "replay": {"outcome": "finding", "runs": {"passed": 1, "total": 1}},
    "cleanup_confirmed": True,
    "recorded_at": "2026-11-01T00:00:00Z",
    "actor": "host",
}


class AuditViewerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = pathlib.Path(cls.tmp.name)
        # work/audit/<audit_id>/ — the CLI's default landing zone.
        audit_dir = root / "work" / "audit" / "demo-target-test"
        audit_dir.mkdir(parents=True)
        (audit_dir / "audit-bundle.json").write_text(json.dumps(BUNDLE), encoding="utf-8")
        (audit_dir / "POC-0001.json").write_text(json.dumps(EVIDENCE), encoding="utf-8")
        rea_dir = audit_dir / "rea-evidence"
        rea_dir.mkdir()
        (rea_dir / "facts.json").write_text(json.dumps({
            "schema_version": "fcode.rea-facts/v1",
            "target": {"artifact_sha256": "c" * 64},
            "operations": {"inspect_go_binary": {"evidence_id": "ev_" + "e" * 64}},
            "host": {"entitlements": ["audio-input"], "declared_features": []},
        }), encoding="utf-8")
        (rea_dir / "control-facts.json").write_text(json.dumps({"control": True}), encoding="utf-8")
        (rea_dir / "inspect_go_binary.json").write_text(json.dumps({
            "evidence_id": "ev_" + "e" * 64,
            "operation": "inspect_go_binary",
        }), encoding="utf-8")
        (rea_dir / "unreferenced.json").write_text(json.dumps({"secret": "no"}), encoding="utf-8")
        cls.server = ConsoleServer(runs_dir=root / "runs", config_dir=root / "config",
                                   extra_args=["--repo", str(root)])
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        cls.port = cls.server.port

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def test_bundle_list_and_reads(self) -> None:
        import urllib.request
        with urllib.request.urlopen(f"{self.base}/api/audit/bundles") as res:
            payload = json.loads(res.read())
        self.assertEqual(len(payload["bundles"]), 1)
        self.assertEqual(payload["bundles"][0]["audit_id"], "demo-target-test")

        with urllib.request.urlopen(f"{self.base}/api/audit/bundle/demo-target-test") as res:
            bundle = json.loads(res.read())
        self.assertEqual(bundle["findings"][0]["root_cause_group"], "approval-memory")

        with urllib.request.urlopen(
                f"{self.base}/api/audit/evidence/demo-target-test/POC-0001") as res:
            record = json.loads(res.read())
        self.assertEqual(record["oracle"]["executor"], "host")

    def test_path_traversal_is_refused(self) -> None:
        import urllib.error
        for path in (
            "/api/audit/bundle/..%2F..%2Fetc",
            "/api/audit/evidence/demo-target-test/..%2FPOC-0001",
            "/api/audit/bundle/DEMO-TARGET-TEST",
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{self.base}{path}")
            # do_GET unquotes the whole path before routing, so encoded
            # traversal surfaces as a segment count/name mismatch (400) —
            # the contract is "refused with 4xx, nothing served", not a
            # specific code.
            self.assertIn(caught.exception.code, (400, 404), path)

    def test_disposition_updates_finding(self) -> None:
        import urllib.request

        body = json.dumps({"disposition": "accepted_risk"}).encode()
        req = urllib.request.Request(
            f"{self.base}/api/audit/disposition/demo-target-test/F-POC-0001",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req) as res:
            result = json.loads(res.read())
        self.assertEqual(result["operator_disposition"], "accepted_risk")

        # Read back: the bundle file should now have the disposition
        bundle_path = (pathlib.Path(self.tmp.name) / "work" / "audit"
                       / "demo-target-test" / "audit-bundle.json")
        bundle = json.loads(bundle_path.read_text())
        self.assertEqual(
            bundle["findings"][0]["operator_disposition"], "accepted_risk")

    def test_disposition_rejects_invalid_value(self) -> None:
        import urllib.error
        import urllib.request

        body = json.dumps({"disposition": "not_a_real_disposition"}).encode()
        req = urllib.request.Request(
            f"{self.base}/api/audit/disposition/demo-target-test/F-POC-0001",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 400)

    def test_disposition_rejects_unknown_finding(self) -> None:
        import urllib.error
        import urllib.request

        body = json.dumps({"disposition": "accepted_risk"}).encode()
        req = urllib.request.Request(
            f"{self.base}/api/audit/disposition/demo-target-test/F-9999",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 404)

    def test_audit_chain_without_session_returns_empty(self) -> None:
        """A bundle without session.sqlite is valid — the chain API returns
        an empty, intact chain rather than an error."""
        import urllib.request

        with urllib.request.urlopen(
                f"{self.base}/api/audit/chain/demo-target-test") as res:
            data = json.loads(res.read())
        self.assertTrue(data["chain_intact"], "no log means intact")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["events"], [])

    def test_audit_chain_nonexistent_returns_not_found(self) -> None:
        import urllib.error
        import urllib.request

        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"{self.base}/api/audit/chain/nonexistent-audit-dir")
        self.assertIn(caught.exception.code, (400, 404))

    def test_spec_gap_present(self) -> None:
        import urllib.request

        report_path = (pathlib.Path(self.tmp.name) / "work" / "audit"
                       / "demo-target-test" / "spec-gap-report.json")
        report_path.write_text(json.dumps({
            "schema_version": "fcode.spec-gap-report/v1",
            "report_id": "spec-gap-demo",
            "audit_id": "demo-target-test",
            "target": {"identity": "demo", "artifact_sha256": "a" * 64,
                       "session_log_sha256": "b" * 64},
            "generated_at": "2026-11-01T00:00:00Z",
            "judgments": [{"judgment_id": "SG-0001", "subject": "s",
                           "verdict": "conforms",
                           "basis": {"doc_refs": ["r"]},
                           "evidence_refs": [{"kind": "dsh_event", "session_id": "s",
                                              "seq": 0, "event_type": "e",
                                              "log_path": "p", "log_sha256": "c" * 64,
                                              "log_format": "jsonl"}]},
            ]}), encoding="utf-8")
        with urllib.request.urlopen(
                f"{self.base}/api/audit/spec-gap/demo-target-test") as res:
            report = json.loads(res.read())
        self.assertEqual(report["schema_version"], "fcode.spec-gap-report/v1")
        self.assertEqual(len(report["judgments"]), 1)

    def test_spec_gap_missing_returns_404(self) -> None:
        import urllib.error
        import urllib.request

        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"{self.base}/api/audit/spec-gap/demo-target-test")
        self.assertEqual(caught.exception.code, 404)

    def test_regression_endpoint_returns_data(self) -> None:
        import urllib.request

        reg_path = (pathlib.Path(self.tmp.name) / "work" / "audit"
                    / "demo-target-test" / "regression.json")
        reg_path.write_text(json.dumps({
            "prior_audit_id": "prior-001", "prior_manifest_sha256": "a" * 64,
            "known": [], "new": [], "fixed": [], "regressed": [],
        }), encoding="utf-8")
        with urllib.request.urlopen(
                f"{self.base}/api/audit/regression/demo-target-test") as res:
            reg = json.loads(res.read())
        self.assertEqual(reg["prior_audit_id"], "prior-001")

    def test_regression_missing_returns_empty(self) -> None:
        import urllib.request

        with urllib.request.urlopen(
                f"{self.base}/api/audit/regression/nonexistent") as res:
            reg = json.loads(res.read())
        self.assertEqual(reg["known"], [])

    def test_rea_evidence_is_bundle_allowlisted(self) -> None:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen(
                f"{self.base}/api/audit/rea/demo-target-test/facts.json") as res:
            facts = json.loads(res.read())
        self.assertEqual(facts["schema_version"], "fcode.rea-facts/v1")

        with urllib.request.urlopen(
                f"{self.base}/api/audit/rea/demo-target-test/inspect_go_binary.json") as res:
            record = json.loads(res.read())
        self.assertEqual(record["operation"], "inspect_go_binary")

        for path in (
            "/api/audit/rea/demo-target-test/unreferenced.json",
            "/api/audit/rea/demo-target-test/..%2F..%2Fetc%2Fpasswd",
            "/api/audit/rea/demo-target-test/not-json.txt",
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{self.base}{path}")
            self.assertIn(caught.exception.code, (400, 404), path)

    def test_security_headers_on_every_response(self) -> None:
        import urllib.request
        with urllib.request.urlopen(f"{self.base}/api/audit/bundles") as res:
            csp = res.headers.get("Content-Security-Policy", "")
            self.assertIn("script-src 'self'", csp)
            self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
        with urllib.request.urlopen(f"{self.base}/audit.html") as res:
            self.assertIn("default-src 'self'", res.headers.get("Content-Security-Policy", ""))

    def test_audit_html_has_no_inline_script(self) -> None:
        import urllib.request
        with urllib.request.urlopen(f"{self.base}/audit.html") as res:
            html = res.read().decode("utf-8")
        self.assertNotIn("<script>window", html)
        self.assertIn('src="boot.js?v=1"', html)


class AuditRunTests(unittest.TestCase):
    """POST /api/audit/run:the one-dialog audit entry, end to end.

    The harness repo is the real flowcredit checkout, so the spawned CLI
    run audits the shipped demo target and must land a bundle with the
    two declared findings. Traversal targets are refused before spawn.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.server = ConsoleServer(
            runs_dir=pathlib.Path(cls.tmp.name) / "runs",
            config_dir=pathlib.Path(cls.tmp.name) / "config",
            extra_args=["--repo", str(ROOT)],
        )
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        cls.port = cls.server.port

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _post(self, path: str, payload: dict):
        import urllib.request
        req = urllib.request.Request(
            f"{self.base}{path}", method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_audit_run_completes_with_declared_findings(self) -> None:
        import urllib.request
        status, payload = self._post("/api/audit/run",
                                     {"target": "fixtures/demo-target"})
        self.assertEqual(status, 200)
        job_id = payload["job_id"]
        outcome = None
        for _ in range(60):
            with urllib.request.urlopen(
                    f"{self.base}/api/audit/jobs/{job_id}", timeout=10) as res:
                outcome = json.loads(res.read())
            if outcome["status"] != "running":
                break
            time.sleep(1.0)
        self.assertEqual(outcome["status"], "done", outcome)
        self.assertEqual(outcome["findings"], 2)
        with urllib.request.urlopen(f"{self.base}/api/audit/bundles", timeout=10) as res:
            bundles = json.loads(res.read())["bundles"]
        self.assertTrue(any(b["audit_id"] and b["findings"] == 2 for b in bundles),
                        "the spawned audit must land in the ledger feed")

    def test_traversal_target_is_refused_before_spawn(self) -> None:
        status, payload = self._post("/api/audit/run", {"target": "../outside"})
        self.assertEqual(status, 400)
        self.assertIn("escapes", payload.get("error", ""))
        status, payload = self._post("/api/audit/run", {"target": ""})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

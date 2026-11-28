"""Coverage ledger: executed is not the same as clean, and absence is not fixed."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowaudit import bundle as bundle_mod  # noqa: E402
from flowaudit import cli  # noqa: E402
from flowaudit.episodes import regression  # noqa: E402
from flowaudit.vulns import engine  # noqa: E402


def _snapshot(root: pathlib.Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        '<!doctype html><html><head>'
        '<script src="https://cdn.example.com/app.js"></script>'
        "</head><body><h1>fixture</h1></body></html>",
        encoding="utf-8",
    )
    (root / "headers.json").write_text(json.dumps({
        "source_url": "https://site.example.com/",
        "landing_headers": {"strict-transport-security": "max-age=1"},
    }), encoding="utf-8")
    (root / "runtime-evidence.json").write_text(json.dumps({
        "status": "ok",
        "requests": [
            {"origin": "https://site.example.com", "resource": "document"},
            {"origin": "https://cdn.example.com", "resource": "script"},
        ],
        "console_errors": [],
    }), encoding="utf-8")
    # A non-web finding keeps the bundle schema valid after the web surface is
    # removed in the false-fixed regression test (poc_evidence_refs minItems=1).
    (root / "app.py").write_text(
        "\n".join([f"# TODO: item {index}" for index in range(10)]
                  + ["def main():", "    return 0"]) + "\n",
        encoding="utf-8",
    )


class EngineCoverageTests(unittest.TestCase):
    def test_static_rule_without_matching_files_is_not_applicable(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        with tempfile.TemporaryDirectory() as tmp:
            coverage: list[dict] = []
            engine.static_scan(pathlib.Path(tmp), ruleset, coverage=coverage)
        records = {item["rule_id"]: item for item in coverage}
        # RULE-AGI02-001 is a **/*.py rule; the empty fixture has no .py file.
        self.assertEqual(records["RULE-AGI02-001"]["status"], "not_applicable")
        self.assertEqual(records["RULE-AGI02-001"]["files_seen"], 0)
        self.assertFalse(records["RULE-AGI02-001"]["files_scanned"])

    def test_rea_web_rule_without_web_scan_is_not_applicable(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        coverage: list[dict] = []
        engine.rea_scan({"host": {"vibe_scan": {}}}, ruleset, coverage=coverage)
        records = {item["rule_id"]: item for item in coverage}
        self.assertEqual(records["RULE-WEB06-001"]["status"], "not_applicable")
        self.assertIn("web_scan", records["RULE-WEB06-001"]["reason"])

    def test_empty_web_surface_is_executed_with_zero_candidates(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        coverage: list[dict] = []
        engine.rea_scan(
            {"host": {"web_scan": {"missing_security_headers": []}}},
            ruleset, coverage=coverage,
        )
        record = next(item for item in coverage if item["rule_id"] == "RULE-WEB06-001")
        self.assertEqual(record["status"], "executed")
        self.assertEqual(record["candidates"], 0)


class RegressionCoverageTests(unittest.TestCase):
    def _prior(self, *, rule_id: str | None = "RULE-WEB06-001") -> dict:
        finding = {
            "category": "WEB-06",
            "root_cause_group": "missing-security-headers",
            "fingerprint": "a" * 16,
        }
        if rule_id:
            finding["rule_id"] = rule_id
        return finding

    def test_rule_not_executed_moves_fixed_to_unverified(self) -> None:
        result = regression(
            [self._prior()], [],
            coverage={"rules": [{"rule_id": "RULE-WEB06-001", "status": "not_applicable"}]},
        )
        self.assertEqual(result["fixed"], [])
        self.assertEqual(len(result["unverified"]), 1)

    def test_executed_rule_can_still_be_fixed(self) -> None:
        result = regression(
            [self._prior()], [],
            coverage={"rules": [{"rule_id": "RULE-WEB06-001", "status": "executed"}]},
        )
        self.assertEqual(len(result["fixed"]), 1)
        self.assertEqual(result["unverified"], [])

    def test_legacy_finding_falls_back_to_category_and_group(self) -> None:
        result = regression(
            [self._prior(rule_id=None)], [],
            coverage={"rules": [{
                "rule_id": "RULE-WEB06-001",
                "category": "WEB-06",
                "root_cause_group": "missing-security-headers",
                "status": "not_applicable",
            }]},
        )
        self.assertEqual(result["fixed"], [])
        self.assertEqual(len(result["unverified"]), 1)

    def test_no_coverage_keeps_legacy_pure_function_behavior(self) -> None:
        result = regression([self._prior()], [])
        self.assertEqual(len(result["fixed"]), 1)
        self.assertEqual(result["unverified"], [])


class BundleCoverageTests(unittest.TestCase):
    def test_cli_writes_hashed_coverage_and_verify_checks_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "snap"
            _snapshot(root)
            out = pathlib.Path(tmp) / "bundle"
            rc = cli.main(["audit", str(root), "--out", str(out), "--operator", "test"])
            self.assertEqual(rc, 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            coverage = bundle["coverage"]
            by_id = {item["rule_id"]: item for item in coverage["rules"]}
            self.assertEqual(by_id["RULE-WEB06-001"]["status"], "executed")
            self.assertEqual(by_id["RULE-WEB07-001"]["status"], "executed")
            self.assertEqual(
                bundle["chain_verification"]["coverage_sha256"],
                bundle_mod.coverage_hash(coverage),
            )
            self.assertEqual(
                cli.main(["verify-bundle", str(out / "audit-bundle.json"),
                          "--evidence", str(out)]),
                0,
            )
            # Tampering with coverage must break verification.
            for item in coverage["rules"]:
                if item["rule_id"] == "RULE-WEB06-001":
                    item["status"] = "not_applicable"
            (out / "audit-bundle.json").write_text(
                json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                cli.main(["verify-bundle", str(out / "audit-bundle.json"),
                          "--evidence", str(out)]),
                1,
            )

    def test_external_tool_evidence_is_pinned_and_tamper_detected(self) -> None:
        def fake_external(snapshot, out, *, input_sha256):
            raw = pathlib.Path(out) / "external-tools" / "retire.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text("[]", encoding="utf-8")
            tools = [{
                "tool": "retire", "status": "executed", "version": "6.1.1",
                "findings_count": 0, "findings": [], "reason": "",
                "input_sha256": input_sha256,
                "raw_path": "external-tools/retire.json",
                "raw_sha256": __import__("hashlib").sha256(b"[]").hexdigest(),
            }]
            return {"tools": tools, "refs": [{
                "tool": "retire", "path": "external-tools/retire.json",
                "sha256": tools[0]["raw_sha256"],
            }]}

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "snap"
            _snapshot(root)
            out = pathlib.Path(tmp) / "bundle"
            with mock.patch("flowaudit.rea.external.run_external_tools",
                            side_effect=fake_external):
                self.assertEqual(cli.main([
                    "audit", str(root), "--out", str(out), "--operator", "test",
                ]), 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle["external_tools"][0]["tool"], "retire")
            self.assertEqual(
                bundle["chain_verification"]["external_tools_sha256"],
                bundle_mod.external_tools_hash(bundle["external_tools"]),
            )
            self.assertEqual(cli.main([
                "verify-bundle", str(out / "audit-bundle.json"), "--evidence", str(out),
            ]), 0)
            bundle["external_tools"][0]["status"] = "skipped"
            (out / "audit-bundle.json").write_text(
                json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(cli.main([
                "verify-bundle", str(out / "audit-bundle.json"), "--evidence", str(out),
            ]), 1)

    def test_missing_web_surface_is_unverified_not_fixed(self) -> None:
        """The exact finch failure mode: dropping headers must not look like a fix."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "snap"
            _snapshot(root)
            db = pathlib.Path(tmp) / "episodes.sqlite"
            first = pathlib.Path(tmp) / "first"
            second = pathlib.Path(tmp) / "second"
            self.assertEqual(cli.main([
                "audit", str(root), "--out", str(first),
                "--operator", "test", "--episodes-db", str(db),
            ]), 0)
            # The next scan loses the web surface: headers/runtime evidence gone.
            (root / "headers.json").unlink()
            (root / "runtime-evidence.json").unlink()
            self.assertEqual(cli.main([
                "audit", str(root), "--out", str(second),
                "--operator", "test", "--episodes-db", str(db),
            ]), 0)
            regression = json.loads(
                (second / "regression.json").read_text(encoding="utf-8"))
            self.assertEqual(regression["fixed"], [])
            unverified = {item["category"] for item in regression["unverified"]}
            self.assertIn("WEB-06", unverified)
            self.assertIn("WEB-07", unverified)


class LegacyRegressionTests(unittest.TestCase):
    def test_bundle_without_coverage_cannot_report_fixed(self) -> None:
        from audit_service import AuditService

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            name = "legacy-2026-01-01-aaaa"
            directory = root / name
            directory.mkdir()
            (directory / "audit-bundle.json").write_text(json.dumps({
                "audit_id": name,
                "target": {"identity": name},
                "findings": [],
                "poc_evidence_refs": [],
            }), encoding="utf-8")
            (directory / "regression.json").write_text(json.dumps({
                "prior_audit_id": "legacy-prior",
                "known": [], "new": [], "regressed": [],
                "fixed": [{"category": "WEB-06", "root_cause_group": "missing-security-headers"}],
            }), encoding="utf-8")
            data = AuditService(repo=root, root=root).regression(name)
            self.assertEqual(data["fixed"], [])
            self.assertEqual(len(data["unverified"]), 1)
            self.assertEqual(data["unverified"][0]["category"], "WEB-06")


if __name__ == "__main__":
    unittest.main()

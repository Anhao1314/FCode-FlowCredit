"""External scanner adapters: fail-closed status, pinned raw evidence."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowaudit.rea import external  # noqa: E402


class ExternalToolTests(unittest.TestCase):
    def test_missing_tools_are_skipped_not_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(external, "_which", return_value=""):
                result = external.run_external_tools(
                    pathlib.Path(tmp), pathlib.Path(tmp) / "out",
                    input_sha256="a" * 64,
                )
            by_name = {item["tool"]: item for item in result["tools"]}
            self.assertEqual(by_name["retire"]["status"], "skipped")
            self.assertEqual(by_name["lighthouse"]["status"], "skipped")
            self.assertEqual(result["refs"], [])

    def test_executed_tools_are_summarized_and_raw_evidence_pinned(self) -> None:
        snapshot = pathlib.Path("/tmp/snapshot")
        out = pathlib.Path("/tmp/audit-out")

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            exe = str(cmd[0])
            if "--version" in cmd or (len(cmd) > 1 and cmd[1] == "version"):
                return SimpleNamespace(returncode=0, stdout="1.2.3\n", stderr="")
            if exe.endswith("retire"):
                raw = pathlib.Path(cmd[cmd.index("--outputpath") + 1])
                raw.write_text(json.dumps([{
                    "file": str(snapshot / "app.js"),
                    "matches": [{
                        "component": "lib",
                        "version": "1.0.0",
                        "vulnerabilities": [{
                            "severity": "high",
                            "identifiers": [{"CVE": "CVE-2026-1"}],
                            "info": ["old library"],
                        }],
                    }],
                }]), encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if exe.endswith("gitleaks"):
                raw = pathlib.Path(cmd[cmd.index("--report-path") + 1])
                raw.write_text(json.dumps([{
                    "File": str(snapshot / "app.js"),
                    "RuleID": "generic-api-key",
                    "StartLine": 1,
                    "Secret": "abcd1234efgh",
                }]), encoding="utf-8")
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            raise AssertionError(f"unexpected command: {cmd}")

        def fake_which(name):  # noqa: ANN001
            return "" if name == "lighthouse" else f"/fake/{name}"

        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "out"
            with mock.patch.object(external, "_which", side_effect=fake_which), \
                    mock.patch.object(external.subprocess, "run", side_effect=fake_run):
                result = external.run_external_tools(
                    snapshot, out, input_sha256="b" * 64)
            by_name = {item["tool"]: item for item in result["tools"]}
            self.assertEqual(by_name["retire"]["status"], "executed")
            self.assertEqual(by_name["retire"]["version"], "1.2.3")
            self.assertEqual(by_name["retire"]["findings_count"], 1)
            self.assertEqual(by_name["gitleaks"]["status"], "executed")
            self.assertEqual(by_name["gitleaks"]["findings_count"], 1)
            self.assertTrue(by_name["gitleaks"]["findings"][0]["secret_redacted"].endswith("…"))
            self.assertEqual(by_name["lighthouse"]["status"], "skipped")
            self.assertEqual(len(result["refs"]), 2)
            for ref in result["refs"]:
                self.assertTrue((out / ref["path"]).is_file())
                self.assertEqual(len(ref["sha256"]), 64)

    def test_tool_failure_is_error_not_clean(self) -> None:
        def fake_run(cmd, **kwargs):  # noqa: ANN001
            if "--version" in cmd or (len(cmd) > 1 and cmd[1] == "version"):
                return SimpleNamespace(returncode=0, stdout="9.9\n", stderr="")
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(external, "_which", return_value="/fake/tool"), \
                    mock.patch.object(external.subprocess, "run", side_effect=fake_run):
                result = external.run_external_tools(
                    pathlib.Path(tmp), pathlib.Path(tmp) / "out",
                    input_sha256="c" * 64,
                )
            for item in result["tools"]:
                if item["tool"] == "lighthouse":
                    self.assertEqual(item["status"], "skipped")
                    continue
                self.assertEqual(item["status"], "error")
                self.assertIn("TimeoutExpired", item["reason"])


if __name__ == "__main__":
    unittest.main()

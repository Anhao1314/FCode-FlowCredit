"""REA integration: artifact target, facts, rules and bundle.

The tests use a fake REA CLI, so they prove FCode's side of the contract
without a Node installation or a real engine. The fake emits the same
Evidence shape as REA; the adapter must treat it as data, pin it, project
an allow-listed fact set, and let the host oracle decide.
"""

from __future__ import annotations

import json
import os
import pathlib
import plistlib
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowaudit import cli  # noqa: E402
from flowaudit.rea import facts as facts_mod  # noqa: E402
from flowaudit.rea.client import run_evidence  # noqa: E402
from flowaudit.rea.macho import parse_macho  # noqa: E402
from flowaudit.rea.osv import compare_versions, load_snapshot, match_go_dependencies  # noqa: E402
from flowaudit.rea.sidecars import scan_sidecar  # noqa: E402
from flowaudit.rea.strings import scan_runtime_installers  # noqa: E402
from flowaudit.targets import rea as rea_adapter  # noqa: E402
from flowaudit.vulns import engine  # noqa: E402
from flowagent.core.contracts import AgentError  # noqa: E402

EVIDENCE_GO = {
    "evidence_id": "ev_" + "a" * 64,
    "operation": "inspect_go_binary",
    "subject": {
        "name": "evox",
        "digest": {"sha256": "a" * 64},
        "local_path": "/tmp/Example.app/Contents/MacOS/evox",
    },
    "provider": {"id": "go-build-info", "name": "REA Go reader", "version": "1"},
    "confidence": "observed",
    "authority": "shipped-artifact",
    "normalized_result": {
        "format": "macho",
        "architecture": "arm64",
        "bits": 64,
        "byte_order": "little",
        "artifact": {
            "path": "/tmp/Example.app/Contents/MacOS/evox",
            "sha256": "a" * 64,
            "bytes": 1234,
        },
        "build_info": {
            "go_version": "go1.26.10",
            "encoding": "inline",
            "module": {
                "path": "github.com/example/app",
                "main": {"path": "github.com/example/app", "version": "(devel)"},
                "dependencies": [
                    {"path": "github.com/example/rc", "version": "v1.0.0-rc1", "sum": "h1:x", "replacement": None},
                    {"path": "github.com/example/stable", "version": "v1.2.3", "sum": "h1:y", "replacement": None},
                ],
                "settings": [{"key": "-tags", "value": "desktop,production"}],
                "complete": True,
            },
        },
    },
    "limitations": ["fixture limitation"],
}

EVIDENCE_DESKTOP = {
    "evidence_id": "ev_" + "b" * 64,
    "operation": "inspect_desktop_application",
    "subject": {
        "name": "Example.app",
        "digest": {"sha256": "b" * 64},
        "local_path": "/tmp/Example.app",
    },
    "provider": {"id": "desktop", "name": "REA desktop reader", "version": "1"},
    "confidence": "observed",
    "authority": "shipped-artifact",
    "normalized_result": {
        "root": {"path": "/tmp/Example.app", "snapshot_sha256": "c" * 64},
        "files": [
            {"file_id": "f1", "path": "Contents/Info.plist", "kind": "file", "bytes": 10,
             "read_status": "examined", "facts": [], "issues": []},
            {"file_id": "f2", "path": "Contents/MacOS/evox", "kind": "file", "bytes": 1234,
             "read_status": "examined",
             "facts": [{"kind": "native-image-header", "offset": 0,
                        "value": {"format": "mach-o"}}], "issues": []},
            {"file_id": "f3", "path": "Contents/Resources/weixin-bridge/bridge.ts",
             "kind": "file", "bytes": 99, "read_status": "not-selected", "facts": [], "issues": []},
        ],
        "omissions": [],
        "candidates": [],
        "components": [],
        "next_probes": [],
        "coverage": {"status": "complete-within-scan", "entries_examined": 3,
                     "files_probed": 2, "bytes_read": 20, "symlinks_not_followed": 0},
    },
    "limitations": ["bundle-only fixture"],
}


def _fake_rea(path: pathlib.Path) -> pathlib.Path:
    script = path / "rea-fake"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9')\n"
        "    raise SystemExit(0)\n"
        "op = sys.argv[1].replace(chr(45), chr(95))\n"
        f"GO = {json.dumps(EVIDENCE_GO)!r}\n"
        f"DESKTOP = {json.dumps(EVIDENCE_DESKTOP)!r}\n"
        "payload = DESKTOP if op == 'inspect_desktop_application' else GO\n"
        "print(payload)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _fake_app(path: pathlib.Path) -> pathlib.Path:
    app = path / "Example.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "weixin-bridge").mkdir(parents=True)
    info = {
        "CFBundleExecutable": "evox",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
    }
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents" / "MacOS" / "evox").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 12)
    (app / "Contents" / "Resources" / "weixin-bridge" / "bridge.ts").write_text(
        "export const port = 18795\n", encoding="utf-8")
    return app


class ReaClientTests(unittest.TestCase):
    def test_runs_allowlisted_operation_and_validates_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_rea(pathlib.Path(tmp))
            evidence = run_evidence("inspect_go_binary", pathlib.Path(tmp), binary=str(binary))
            self.assertEqual(evidence.evidence_id, EVIDENCE_GO["evidence_id"])
            self.assertEqual(evidence.binary_version, "9.9.9")
            self.assertEqual(evidence.subject_sha256, "a" * 64)

    def test_refuses_unlisted_operation(self) -> None:
        with self.assertRaises(AgentError) as caught:
            run_evidence("drop_tables", "/tmp")
        self.assertEqual(caught.exception.code, "REA_OPERATION_REFUSED")

    def test_missing_binary_is_unavailable_not_empty(self) -> None:
        with self.assertRaises(AgentError) as caught:
            run_evidence("inspect_go_binary", "/tmp", binary="/definitely/not/rea")
        self.assertEqual(caught.exception.code, "REA_UNAVAILABLE")

    def test_oversized_output_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = _fake_rea(pathlib.Path(tmp))
            with self.assertRaises(AgentError) as caught:
                run_evidence("inspect_go_binary", pathlib.Path(tmp),
                             binary=str(binary), max_output_bytes=10)
            self.assertEqual(caught.exception.code, "REA_OUTPUT_TOO_LARGE")


class ReaFactsTests(unittest.TestCase):
    def test_go_projection_is_allowlisted_and_bounded(self) -> None:
        facts = facts_mod.extract_go_binary(EVIDENCE_GO)
        self.assertEqual(facts["go_version"], "go1.26.10")
        self.assertEqual(facts["module"]["path"], "github.com/example/app")
        self.assertEqual(len(facts["dependencies"]), 2)
        self.assertTrue(facts["dependencies"][0]["prerelease"])
        self.assertFalse(facts["dependencies"][1]["prerelease"])
        self.assertNotIn("module_text", facts)

    def test_desktop_projection_keeps_selected_files_and_sidecars(self) -> None:
        facts = facts_mod.extract_desktop_application(EVIDENCE_DESKTOP)
        self.assertEqual(facts["entry_count"], 3)
        paths = {item["path"] for item in facts["selected_files"]}
        self.assertIn("Contents/MacOS/evox", paths)
        self.assertIn("Contents/Resources/weixin-bridge/bridge.ts", paths)
        self.assertEqual(facts["sidecars"][0]["path"], "Contents/Resources/weixin-bridge")

    def test_facts_digest_rejects_nothing_and_is_stable(self) -> None:
        one = facts_mod.build_snapshot_facts(
            target={"identity": "x", "artifact_sha256": "a" * 64},
            evidence=[EVIDENCE_GO],
            host={"entitlements": ["ent.a"], "declared_features": []},
        )
        two = facts_mod.build_snapshot_facts(
            target={"identity": "x", "artifact_sha256": "a" * 64},
            evidence=[EVIDENCE_GO],
            host={"entitlements": ["ent.a"], "declared_features": []},
        )
        self.assertEqual(facts_mod.facts_digest(one), facts_mod.facts_digest(two))


class ReaSidecarTests(unittest.TestCase):
    def test_scan_sidecar_flags_unauth_listener_without_literals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "demo-bridge"
            root.mkdir()
            (root / "bridge.ts").write_text(
                "import { createServer } from 'node:http'\n"
                "const server = createServer((req, res) => res.end('ok'))\n"
                "server.listen({ host: '127.0.0.1', port: 18795 })\n",
                encoding="utf-8",
            )
            facts = scan_sidecar(root)
            self.assertGreaterEqual(len(facts["listeners"]), 1)
            self.assertEqual(len(facts["auth_markers"]), 0)
            self.assertEqual(len(facts["listener_without_auth"]), len(facts["listeners"]))
            self.assertEqual(facts["secret_like"], [])

    def test_scan_sidecar_marks_auth_secret_and_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "demo-bridge"
            root.mkdir()
            (root / "bridge.ts").write_text(
                "import { createServer } from 'node:http'\n"
                "const token = process.env.SHARED || 'abcdefghijklmnop'\n"
                "const token = 'abcdefghijklmnop'\n"
                "const auth = 'Bearer ' + token\n"
                "server.listen({ host: '127.0.0.1', port: 1 })\n"
                "// npm install is documented here\n",
                encoding="utf-8",
            )
            (root / "bridge.test.ts").write_text(
                "const secret = 'should-not-be-scanned-because-it-is-a-test'\n",
                encoding="utf-8",
            )
            facts = scan_sidecar(root)
            self.assertEqual(facts["source_files"], 1)
            self.assertEqual(facts["test_files"], 1)
            self.assertEqual(len(facts["auth_markers"]), 1)
            self.assertEqual(facts["listener_without_auth"], [])
            self.assertEqual(len(facts["secret_like"]), 1)
            self.assertEqual(len(facts["npm_install"]), 1)

    def test_sidecar_fixtures_match_their_rules(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        cases = {
            "sidecar-unauth.json": "RULE-AGI02-102",
            "sidecar-secret.json": "RULE-AGI03-102",
            "sidecar-npm.json": "RULE-AGI05-102",
        }
        for filename, rule_id in cases.items():
            facts = json.loads((ROOT / "fixtures" / "rea" / filename).read_text(encoding="utf-8"))
            matched = {candidate.rule.id for candidate in engine.rea_scan(facts, ruleset)}
            self.assertIn(rule_id, matched, filename)
            control = rea_adapter._neutralise(facts)
            controlled = {candidate.rule.id for candidate in engine.rea_scan(control, ruleset)}
            self.assertNotIn(rule_id, controlled, f"control must clear {rule_id}")


class ReaMachoTests(unittest.TestCase):
    def test_parse_thin_macho_load_commands(self) -> None:
        import struct
        import tempfile

        dylib = b"/usr/lib/libSystem.B.dylib\x00"
        dylib_pad = (-len(dylib)) % 8
        dylib_cmd = struct.pack("<IIIIII", 0xC, 24 + len(dylib) + dylib_pad,
                                24, 0, 0, 0) + dylib + b"\x00" * dylib_pad
        rpath = b"@executable_path/../Frameworks\x00"
        rpath_pad = (-len(rpath)) % 8
        rpath_cmd = struct.pack("<III", 0x8000001C, 12 + len(rpath) + rpath_pad,
                                12) + rpath + b"\x00" * rpath_pad
        commands = dylib_cmd + rpath_cmd
        header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2,
                             2, len(commands), 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "fixture"
            path.write_bytes(header + commands)
            parsed = parse_macho(path)
        self.assertEqual(parsed["architecture"], "arm64")
        self.assertEqual(parsed["loaded"], ["/usr/lib/libSystem.B.dylib"])
        self.assertEqual(parsed["rpaths"], ["@executable_path/../Frameworks"])


class ReaOsvTests(unittest.TestCase):
    def test_version_compare_orders_prerelease_before_release(self) -> None:
        self.assertLess(compare_versions("v1.0.0-rc1", "v1.0.0"), 0)
        self.assertGreater(compare_versions("v1.0.1", "v1.0.0"), 0)
        self.assertEqual(compare_versions("v1.0.0", "v1.0.0"), 0)
        self.assertIsNone(compare_versions("not-semver", "v1.0.0"))

    def test_osv_snapshot_matches_vulnerable_range(self) -> None:
        records = load_snapshot(ROOT / "fixtures" / "rea" / "osv-snapshot.json")
        deps = [
            {"path": "github.com/example/rc", "version": "v1.0.0-rc1"},
            {"path": "example/stable", "version": "v1.2.3"},
        ]
        matches = match_go_dependencies(deps, records)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["osv_id"], "OSV-2026-0001")
        self.assertEqual(matches[0]["fixed_versions"], ["v1.0.0"])

    def test_osv_rule_fixture_and_control(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        facts = json.loads((ROOT / "fixtures" / "rea" / "facts-vulnerable.json").read_text())
        matched = {candidate.rule.id for candidate in engine.rea_scan(facts, ruleset)}
        self.assertIn("RULE-AGI05-103", matched)
        control = rea_adapter._neutralise(facts)
        controlled = {candidate.rule.id for candidate in engine.rea_scan(control, ruleset)}
        self.assertNotIn("RULE-AGI05-103", controlled)


class ReaRuntimeInstallerTests(unittest.TestCase):
    def test_scans_runtime_npm_install_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "binary"
            path.write_bytes(
                b"prefix " + b"npm install -g @evomap/evolver" + b" suffix " + b"x" * 32,
            )
            found = scan_runtime_installers(path)
        self.assertEqual(len(found), 1)
        self.assertIn("npm install -g @evomap/evolver", found[0]["command"])

    def test_runtime_install_rule_fixture_and_control(self) -> None:
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        facts = json.loads((ROOT / "fixtures" / "rea" / "facts-runtime-install.json").read_text())
        matched = {candidate.rule.id for candidate in engine.rea_scan(facts, ruleset)}
        self.assertIn("RULE-AGI05-104", matched)
        control = rea_adapter._neutralise(facts)
        controlled = {candidate.rule.id for candidate in engine.rea_scan(control, ruleset)}
        self.assertNotIn("RULE-AGI05-104", controlled)


class ReaRuleTests(unittest.TestCase):
    def test_rea_predicates_match_prerelease_and_entitlements(self) -> None:
        facts = facts_mod.build_snapshot_facts(
            target={"identity": "x", "artifact_sha256": "a" * 64},
            evidence=[EVIDENCE_GO],
            host={"entitlements": ["ent.a", "ent.b"], "declared_features": ["ent.a"]},
        )
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        matched = {candidate.rule.id for candidate in engine.rea_scan(facts, ruleset)}
        self.assertIn("RULE-AGI05-101", matched)
        self.assertIn("RULE-AGI07-101", matched)

    def test_clean_facts_do_not_match(self) -> None:
        facts = facts_mod.build_snapshot_facts(
            target={"identity": "x", "artifact_sha256": "a" * 64},
            evidence=[{
                **EVIDENCE_GO,
                "normalized_result": {
                    **EVIDENCE_GO["normalized_result"],
                    "build_info": {
                        **EVIDENCE_GO["normalized_result"]["build_info"],
                        "module": {
                            **EVIDENCE_GO["normalized_result"]["build_info"]["module"],
                            "dependencies": [
                                {"path": "github.com/example/stable",
                                 "version": "v1.2.3", "sum": "h1:y", "replacement": None},
                            ],
                        },
                    },
                },
            }],
            host={"entitlements": [], "declared_features": []},
        )
        ruleset = engine.load_ruleset(ROOT / "audit-rules")
        matched = {candidate.rule.id for candidate in engine.rea_scan(facts, ruleset)}
        self.assertNotIn("RULE-AGI05-101", matched)
        self.assertNotIn("RULE-AGI07-101", matched)


class ReaArtifactPipelineTests(unittest.TestCase):
    def test_prepare_pins_evidence_and_writes_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = _fake_rea(root)
            app = _fake_app(root)
            with mock.patch.object(
                rea_adapter, "_codesign_entitlements",
                return_value=(["com.apple.security.device.audio-input"],
                              {"status": "observed", "tool": "codesign"}),
            ), mock.patch.object(
                rea_adapter, "_signature_status",
                return_value={"status": "valid", "tool": "codesign"},
            ):
                snapshot = rea_adapter.prepare(
                    app, root / "work", binary=str(binary),
                    declared_features=["com.apple.security.device.audio-input"],
                    osv_snapshot=ROOT / "fixtures" / "rea" / "osv-snapshot.json",
                    evidence_dir=root / "out" / "rea-evidence",
                )
            self.assertEqual(snapshot.kind, "desktop-app")
            self.assertEqual(snapshot.version, "1.0.0")
            self.assertEqual(len(snapshot.evidence_files), 2)
            self.assertEqual(len(snapshot.manifest_sha256), 64)
            self.assertEqual(len(snapshot.analysis_manifest_sha256), 64)
            self.assertTrue((snapshot.snapshot_root / "facts.json").is_file())
            self.assertEqual(
                snapshot.facts["host"]["vulnerabilities"][0]["osv_id"], "OSV-2026-0001",
            )
            control = json.loads((snapshot.snapshot_root / "control-facts.json").read_text())
            versions = [
                item["version"]
                for item in control["operations"]["inspect_go_binary"]["facts"]["dependencies"]
            ]
            self.assertNotIn("v1.0.0-rc1", versions)
            self.assertEqual(
                control["host"]["declared_features"],
                control["host"]["entitlements"],
            )

    def test_target_manifest_is_stable_across_rea_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            first = _fake_rea(root)
            second = root / "rea-fake-new"
            second.write_text(
                first.read_text(encoding="utf-8").replace("9.9.9", "9.9.10"),
                encoding="utf-8",
            )
            second.chmod(first.stat().st_mode)
            app = _fake_app(root)
            with mock.patch.object(
                rea_adapter, "_codesign_entitlements",
                return_value=([], {"status": "observed", "tool": "codesign"}),
            ), mock.patch.object(
                rea_adapter, "_signature_status",
                return_value={"status": "valid", "tool": "codesign"},
            ):
                one = rea_adapter.prepare(
                    app, root / "work-one", binary=str(first),
                    evidence_dir=root / "out-one" / "rea-evidence",
                )
                two = rea_adapter.prepare(
                    app, root / "work-two", binary=str(second),
                    evidence_dir=root / "out-two" / "rea-evidence",
                )
            self.assertEqual(one.manifest_sha256, two.manifest_sha256)
            self.assertNotEqual(
                one.analysis_manifest_sha256, two.analysis_manifest_sha256,
                "an REA version change must move the analysis basis, not the target identity",
            )

    def test_target_md_descriptor_drives_declared_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = _fake_rea(root)
            app = _fake_app(root)
            descriptor = root / "target.md"
            descriptor.write_text(
                "# target\n\n## Declared features\n- microphone\n- calendar\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                rea_adapter, "_codesign_entitlements",
                return_value=(
                    ["com.apple.security.device.audio-input",
                     "com.apple.security.personal-information.calendars"],
                    {"status": "observed", "tool": "codesign"},
                ),
            ), mock.patch.object(
                rea_adapter, "_signature_status",
                return_value={"status": "valid", "tool": "codesign"},
            ):
                snapshot = rea_adapter.prepare(
                    app, root / "work", binary=str(binary),
                    target_md=descriptor,
                    evidence_dir=root / "out" / "rea-evidence",
                )
            self.assertEqual(
                snapshot.facts["host"]["declared_features"],
                [
                    "com.apple.security.device.audio-input",
                    "com.apple.security.personal-information.calendars",
                ],
            )

    def test_missing_target_md_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = _fake_rea(root)
            app = _fake_app(root)
            with self.assertRaises(AgentError) as caught:
                rea_adapter.prepare(
                    app, root / "work", binary=str(binary),
                    target_md=root / "missing.md",
                )
            self.assertEqual(caught.exception.code, "REA_TARGET_INVALID")

    def test_cli_audits_artifact_and_verifies_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = _fake_rea(root)
            app = _fake_app(root)
            out = root / "bundle"
            with mock.patch.object(
                rea_adapter, "_codesign_entitlements",
                return_value=(
                    ["com.apple.security.device.audio-input",
                     "com.apple.security.personal-information.calendars"],
                    {"status": "observed", "tool": "codesign"},
                ),
            ), mock.patch.object(
                rea_adapter, "_signature_status",
                return_value={"status": "valid", "tool": "codesign"},
            ):
                rc = cli.main([
                    "audit", str(app), "--adapter", "rea", "--out", str(out),
                    "--operator", "ci-test", "--rea-bin", str(binary),
                    "--declared-features", "com.apple.security.device.audio-input",
                ])
            self.assertEqual(rc, 0)
            bundle = json.loads((out / "audit-bundle.json").read_text(encoding="utf-8"))
            agi_findings = [f for f in bundle["findings"]
                            if f["category"].startswith("AGI")]
            self.assertEqual(len(agi_findings), 2,
                             f"expected 2 AGI findings, got {bundle['findings']}")
            self.assertEqual(len(bundle["rea_evidence_refs"]), 2)
            self.assertEqual(bundle["toolchain"]["rea"]["version"], "9.9.9")
            expected_artifact = __import__("hashlib").sha256(
                (app / "Contents" / "MacOS" / "evox").read_bytes()).hexdigest()
            self.assertEqual(bundle["target"]["artifact_sha256"], expected_artifact)
            categories = {finding["category"] for finding in bundle["findings"]}
            self.assertEqual(categories, {"AGI-05", "AGI-07"})
            for ref in bundle["rea_evidence_refs"]:
                self.assertTrue((out / ref["path"]).is_file())
            rc = cli.main([
                "verify-bundle", str(out / "audit-bundle.json"),
                "--evidence", str(out),
            ])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
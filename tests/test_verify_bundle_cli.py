"""verify-bundle 的完整/部分校验语义。

T1 不提供 --evidence:只能做 schema 校验,不得打印 passed;
T2 提供 --evidence 且证据完整/hash 一致:passed + 0;
T3 提供 --evidence 但证据缺失:failed + 非零;
T4 提供 --evidence 但 evidence-set hash 不匹配:failed + 非零。
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from flowaudit import bundle as bundle_mod  # noqa: E402
from flowaudit import cli  # noqa: E402

EXAMPLE = ROOT / "audit-rules" / "schema" / "examples" / "poc-evidence-fcode02.json"


def _write_case(tmp: pathlib.Path, *, bad_hash: bool = False) -> pathlib.Path:
    evidence = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    poc_id = evidence["poc_id"]
    (tmp / f"{poc_id}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    head = "0" * 64 if bad_hash else bundle_mod.evidence_set_hash([evidence])
    bundle = {
        "schema_version": "fcode.audit-bundle/v1",
        "audit_id": "dsh-gap-2026-11-02",
        "authorization": {
            "operator": "ci",
            "scope": "test",
            "granted_at": "2026-11-02T00:00:00Z",
        },
        "target": {
            "identity": "dsh",
            "version": "0.2.1-rc.5",
            "manifest_sha256": "a" * 64,
        },
        "sessions": [{"phase": "static", "session_ref": "cli-batch"}],
        "toolchain": {
            "replayer": "flowaudit.poc.replay/v1",
            "oracle_version": "1",
        },
        "poc_evidence_refs": [poc_id],
        "findings": [],
        "uncovered_surfaces": [],
        "chain_verification": {"head_hash": head, "command": "verify-bundle"},
    }
    path = tmp / "audit-bundle.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = cli.main(argv)
    return rc, buffer.getvalue()


class VerifyBundleTests(unittest.TestCase):
    def test_t1_without_evidence_is_schema_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _write_case(pathlib.Path(tmp))
            rc, out = _run(["verify-bundle", str(bundle)])
        self.assertNotEqual(rc, 0, "未提供 --evidence 不能以成功码退出")
        self.assertIn("schema_only", out)
        self.assertNotIn('"verify": "passed"', out)

    def test_t2_with_complete_evidence_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _write_case(pathlib.Path(tmp))
            rc, out = _run(["verify-bundle", str(bundle), "--evidence", tmp])
        self.assertEqual(rc, 0)
        self.assertIn('"verify": "passed"', out)

    def test_t3_missing_evidence_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _write_case(pathlib.Path(tmp))
            (pathlib.Path(tmp) / "POC-0001.json").unlink()
            rc, out = _run(["verify-bundle", str(bundle), "--evidence", tmp])
        self.assertNotEqual(rc, 0)
        self.assertIn('"verify": "failed"', out)

    def test_t4_evidence_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _write_case(pathlib.Path(tmp), bad_hash=True)
            rc, out = _run(["verify-bundle", str(bundle), "--evidence", tmp])
        self.assertNotEqual(rc, 0)
        self.assertIn('"verify": "failed"', out)
        self.assertIn("hash mismatch", out)


if __name__ == "__main__":
    unittest.main()
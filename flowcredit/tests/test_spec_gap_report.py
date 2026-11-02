"""spec-gap-report: schema + writer 的最小验收。

T1 合法输入(三条 DSH 判定)落盘并通过 schema;
T2 undetermined 缺 missing_spec 被拒绝且不写文件;
T3 conforms 缺 basis.doc_refs 被拒绝且不写文件;
T4 absence_refs.scope 为自由字符串被 schema 拒绝;
T5 evidence_refs 缺 log_format/log_sha256 被 schema 拒绝;
T6 完整 report 与 example 均通过 jsonschema_lite。
"""

from __future__ import annotations

import contextlib
import copy
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

from flowaudit import cli  # noqa: E402
from flowaudit import jsonschema_lite  # noqa: E402
from flowaudit import spec_gap  # noqa: E402

SCHEMA = ROOT / "audit-rules" / "schema" / "spec-gap-report.schema.json"
EXAMPLE = ROOT / "audit-rules" / "schema" / "examples" / "spec-gap-report-example.json"


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = cli.main(argv)
    return rc, buffer.getvalue()


def _input_with(judgments: list[dict]) -> dict:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return {
        "audit_id": example["audit_id"],
        "target": example["target"],
        "judgments": judgments,
    }


def _write_input(tmp: pathlib.Path, payload: dict) -> pathlib.Path:
    path = tmp / "judgments.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


class SpecGapReportTests(unittest.TestCase):
    def test_t1_valid_three_judgments_writes_valid_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "spec-gap-report.json"
            rc, stdout = _run([
                "spec-gap-report", "--input", str(EXAMPLE), "--out", str(out),
            ])
            self.assertEqual(rc, 0, stdout)
            self.assertTrue(out.exists())
            report = json.loads(out.read_text(encoding="utf-8"))
            schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
            self.assertEqual(jsonschema_lite.validate(report, schema), [])
            self.assertEqual(len(report["judgments"]), 3)
            self.assertEqual(
                [j["verdict"] for j in report["judgments"]],
                ["conforms", "undetermined", "undetermined"],
            )
        self.assertEqual(len(spec_gap.load_judgments(EXAMPLE)), 3)

    def test_t2_undetermined_without_missing_spec_is_rejected(self) -> None:
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        judgments = copy.deepcopy(example["judgments"])
        judgments[1].pop("missing_spec")
        with tempfile.TemporaryDirectory() as tmp:
            input_path = _write_input(pathlib.Path(tmp), _input_with(judgments))
            out = pathlib.Path(tmp) / "report.json"
            rc, stdout = _run([
                "spec-gap-report", "--input", str(input_path), "--out", str(out),
            ])
            self.assertNotEqual(rc, 0)
            self.assertIn("SPEC_GAP_INVALID", stdout)
            self.assertFalse(out.exists(), "校验失败不得写文件")

    def test_t3_conforms_without_doc_refs_is_rejected(self) -> None:
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        judgments = copy.deepcopy(example["judgments"])
        judgments[0]["basis"].pop("doc_refs")
        with tempfile.TemporaryDirectory() as tmp:
            input_path = _write_input(pathlib.Path(tmp), _input_with(judgments))
            out = pathlib.Path(tmp) / "report.json"
            rc, stdout = _run([
                "spec-gap-report", "--input", str(input_path), "--out", str(out),
            ])
            self.assertNotEqual(rc, 0)
            self.assertIn("SPEC_GAP_INVALID", stdout)
            self.assertFalse(out.exists(), "校验失败不得写文件")

    def test_t4_absence_scope_must_be_structured(self) -> None:
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        judgments = copy.deepcopy(example["judgments"])
        judgments[2]["absence_refs"][0]["scope"] = "turn 1"
        report = spec_gap.build_report(
            judgments, example["target"], example["audit_id"])
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "report.json"
            with self.assertRaises(ValueError):
                spec_gap.write_report(report, out)
            self.assertFalse(out.exists())

    def test_t5_evidence_requires_log_format_and_hash(self) -> None:
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        judgments = copy.deepcopy(example["judgments"])
        judgments[0]["evidence_refs"][0].pop("log_format")
        judgments[1]["evidence_refs"][0].pop("log_sha256")
        report = spec_gap.build_report(
            judgments, example["target"], example["audit_id"])
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "report.json"
            with self.assertRaises(ValueError):
                spec_gap.write_report(report, out)
            self.assertFalse(out.exists())

    def test_t6_complete_report_and_example_conform(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(jsonschema_lite.validate(example, schema), [],
                         "example must conform to spec-gap-report.schema.json")
        report = spec_gap.build_report(
            example["judgments"], example["target"], example["audit_id"])
        self.assertEqual(jsonschema_lite.validate(report, schema), [])


if __name__ == "__main__":
    unittest.main()
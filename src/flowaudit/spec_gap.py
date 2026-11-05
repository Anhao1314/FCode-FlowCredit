"""Standalone spec-gap reports: checked-conformance and undetermined judgments.

This is deliberately not an audit-bundle finding. A finding requires a
replay-backed PoC; these judgments are documentation/observation conclusions
that may have no PoC at all. The report is a separate JSON artifact and never
changes the audit-bundle contract.

The writer accepts judgments from a human or an external analysis script. It
does not read DSH logs and does not decide verdicts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import jsonschema_lite

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "audit-rules"
    / "schema"
    / "spec-gap-report.schema.json"
)

AUDIT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{2,127}$")
VERDICTS = ("conforms", "undetermined")


def load_input(path: str | Path) -> dict[str, Any]:
    """Load the input document. A bare array is wrapped as ``judgments``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"judgments": data}
    if not isinstance(data, dict):
        raise ValueError("spec-gap input must be an object or an array")
    return data


def load_judgments(path: str | Path) -> list[dict[str, Any]]:
    """Return the ``judgments`` array from an input document."""
    judgments = load_input(path).get("judgments")
    if not isinstance(judgments, list):
        raise ValueError("spec-gap input must contain a judgments array")
    return judgments


def validate_judgment(judgment: Any) -> list[str]:
    """Check field presence plus the conditional rules schema cannot express."""
    if not isinstance(judgment, dict):
        return ["judgment must be an object"]

    errors: list[str] = []
    if not isinstance(judgment.get("judgment_id"), str) or not judgment["judgment_id"]:
        errors.append("judgment_id is required")
    if not isinstance(judgment.get("subject"), str) or not judgment["subject"]:
        errors.append("subject is required")

    verdict = judgment.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"verdict must be one of {VERDICTS}")
    if not isinstance(judgment.get("evidence_refs"), list) or not judgment["evidence_refs"]:
        errors.append("evidence_refs must be a non-empty array")

    if verdict == "conforms":
        basis = judgment.get("basis")
        if not isinstance(basis, dict):
            errors.append("conforms requires basis")
        else:
            doc_refs = basis.get("doc_refs")
            if (
                not isinstance(doc_refs, list)
                or not doc_refs
                or not all(isinstance(ref, str) and ref.strip() for ref in doc_refs)
            ):
                errors.append("conforms requires basis.doc_refs with at least one entry")

    if verdict == "undetermined":
        missing = judgment.get("missing_spec")
        if not isinstance(missing, dict):
            errors.append("undetermined requires missing_spec")
        else:
            for key in ("question", "searched_refs", "result"):
                if key not in missing:
                    errors.append(f"undetermined missing_spec requires {key}")
            question = missing.get("question")
            if not isinstance(question, str) or not question.strip():
                errors.append("missing_spec.question must be a non-empty string")
            searched = missing.get("searched_refs")
            if (
                not isinstance(searched, list)
                or not searched
                or not all(isinstance(ref, str) and ref.strip() for ref in searched)
            ):
                errors.append("missing_spec.searched_refs must be a non-empty array")
            result = missing.get("result")
            if not isinstance(result, str) or not result.strip():
                errors.append("missing_spec.result must be a non-empty string")

    return errors


def build_report(
    judgments: list[dict[str, Any]],
    target: dict[str, Any],
    audit_id: str,
) -> dict[str, Any]:
    """Assemble a report; conditional judgment errors raise ``ValueError``."""
    if not isinstance(judgments, list):
        raise ValueError("judgments must be an array")
    if not isinstance(target, dict):
        raise ValueError("target must be an object")
    if not isinstance(audit_id, str) or not audit_id:
        raise ValueError("audit_id is required")

    errors: list[str] = []
    for index, judgment in enumerate(judgments):
        for error in validate_judgment(judgment):
            errors.append(f"judgments[{index}]: {error}")
    if errors:
        raise ValueError("; ".join(errors))

    return {
        "schema_version": "fcode.spec-gap-report/v1",
        "report_id": f"spec-gap-{audit_id}",
        "audit_id": audit_id,
        "target": target,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "judgments": judgments,
    }


def write_report(report: dict[str, Any], out_path: str | Path) -> None:
    """Validate first, then write; a validation failure writes nothing."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = jsonschema_lite.validate(report, schema)
    if errors:
        raise ValueError(
            "spec-gap-report violates its contract: " + "; ".join(errors))
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
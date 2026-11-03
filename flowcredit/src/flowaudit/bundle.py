"""Finding records and the audit bundle exporter.

A finding is the promoted, root-cause-merged record. The bundle is what
a third party receives: pinned target, toolchain, every evidence record,
the merged finding index, uncovered surfaces, authorization, and a way
to re-verify. The v1 bundle content-addresses its evidence set; the
audit session's own event chain lands with session integration and the
bundle will reference its head then — the schema field already exists.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import jsonschema_lite

BUNDLE_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "audit-rules" / "schema" / "audit-bundle.schema.json"


def build_finding(candidate, evidence_record: dict) -> dict:
    return {
        "finding_id": f"F-{evidence_record['poc_id']}",
        "root_cause_group": candidate.rule.root_cause_group,
        "category": candidate.rule.category,
        "severity": candidate.rule.severity,
        "poc_refs": [evidence_record["poc_id"]],
    }


def evidence_set_hash(records: list[dict]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda r: r["poc_id"]):
        digest.update(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def build_bundle(*, audit_id: str, target_identity: str, target_version: str,
                 manifest_sha256: str, operator: str, scope: str, granted_at: str,
                 sessions: list[dict], toolchain: dict, poc_records: list[dict],
                 findings: list[dict], uncovered_surfaces: list[dict]) -> dict:
    bundle = {
        "schema_version": "fcode.audit-bundle/v1",
        "audit_id": audit_id,
        "authorization": {
            "operator": operator,
            "scope": scope,
            "granted_at": granted_at,
        },
        "target": {
            "identity": target_identity,
            "version": target_version,
            "manifest_sha256": manifest_sha256,
        },
        "sessions": sessions,
        "toolchain": toolchain,
        "poc_evidence_refs": [record["poc_id"] for record in poc_records],
        "findings": findings,
        "uncovered_surfaces": uncovered_surfaces,
        "chain_verification": {
            "head_hash": evidence_set_hash(poc_records),
            "command": (
                "python -m flowaudit.cli verify-bundle <bundle.json> "
                "--evidence <dir> — validates the manifest and every "
                "poc-evidence record against the frozen schemas and checks "
                "the evidence-set hash; the stimulus re-run drill lands "
                "with session integration"
            ),
        },
    }
    errors = jsonschema_lite.validate(
        bundle, json.loads(BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8")))
    if errors:
        raise RuntimeError(f"audit bundle violates its contract: {errors}")
    return bundle

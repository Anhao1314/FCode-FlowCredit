"""fcode audit CLI — the batch entry point (Codex ``exec`` analogue).

    python -m flowaudit.cli audit <target> [--rules DIR] [--out DIR]

Runs the static phase end to end against a target tree: pin the target,
copy it into a workdir, load the ruleset (drafts excluded), scan,
constrained-replay every candidate with a host-side oracle, and export a
bundle. Exit code 0 = the audit completed (findings or not); argparse's
own 2 covers usage errors. Static rules run with no API key and no
network — the whole pipeline is stdlib.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import bundle as bundle_mod
from . import jsonschema_lite
from .poc import replay as replay_mod
from .targets import repo as repo_adapter
from .vulns import engine

DEFAULT_RULES = Path(__file__).resolve().parents[2] / "audit-rules"


def _audit_id(target: Path) -> str:
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = "".join(c if c.isalnum() or c in "-." else "-" for c in target.name.lower())
    return f"{name}-{date}"[:64]


def cmd_audit(args: argparse.Namespace) -> int:
    target = Path(args.target).resolve()
    if not target.exists():
        print(json.dumps({"error": f"target not found: {target}"}))
        return 1
    rules_dir = Path(args.rules).resolve()
    workdir = repo_adapter.new_workdir()
    audit_id = _audit_id(target)
    # 默认落盘到 flowcredit/work/audit/<audit_id>/——viewer 的发现账本从
    # 这里读取;显式 --out 仍然优先。
    out_dir = Path(args.out).resolve() if args.out \
        else DEFAULT_RULES.parent / "work" / "audit" / audit_id
    out_dir.mkdir(parents=True, exist_ok=True)

    target_copy = repo_adapter.copy_target(target, workdir)
    manifest_sha256 = repo_adapter.manifest_hash(target_copy)
    profile = repo_adapter.detect_profile(target_copy)

    ruleset = engine.load_ruleset(rules_dir)
    candidates = engine.static_scan(target_copy, ruleset)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target_ref = {
        "audit_id": audit_id,
        "identity": str(target),
        "version": "",
        "manifest_sha256": manifest_sha256,
        "profile": profile,
    }

    poc_records: list[dict] = []
    findings: list[dict] = []
    for number, candidate in enumerate(candidates, start=1):
        record = replay_mod.replay_candidate(
            candidate.rule, candidate, target_ref, target_copy,
            workdir, now_iso, f"POC-{number:04d}",
        )
        poc_records.append(record)
        if record["replay"]["outcome"] == "finding":
            findings.append(bundle_mod.build_finding(candidate, record))
        (out_dir / f"{record['poc_id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    bundle = bundle_mod.build_bundle(
        audit_id=audit_id,
        target_identity=str(target),
        target_version="",
        manifest_sha256=manifest_sha256,
        operator=args.operator,
        scope=f"static audit of {target} (dev ruleset, constrained replay)",
        granted_at=now_iso,
        sessions=[{"phase": "static", "session_ref": "cli-batch"}],
        toolchain={
            "analyst_model": "deterministic dev rules (no model in the loop)",
            "triage_model": "jev-shadow (not wired in sprint 2)",
            "replayer": "flowaudit.poc.replay/v1",
            "oracle_version": "1",
            "ruleset_version": ruleset["version"],
        },
        poc_records=poc_records,
        findings=findings,
        uncovered_surfaces=[
            {"surface": "dynamic probing (message/HTTP/behavior)",
             "reason": "sprint 2 scope: static rules with constrained replay only"},
            {"surface": "AGI categories beyond the dev ruleset",
             "reason": "ruleset covers the AGI-02 dev set; blind fixtures excluded by policy"},
        ],
    )

    analyst_result = None
    if args.analyst:
        from . import analyst as analyst_mod
        analyst_result = analyst_mod.followup_priorities(bundle)
        bundle["toolchain"]["analyst_model"] = (
            f"{analyst_result['model']} (1 call, {analyst_result['cost_microusd']} µUSD)")
        (out_dir / "analyst-followups.json").write_text(
            json.dumps(analyst_result, ensure_ascii=False, indent=2), encoding="utf-8")

    bundle_path = out_dir / "audit-bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "audit_id": audit_id,
        "target": str(target),
        "manifest_sha256": manifest_sha256[:16] + "…",
        "ruleset_version": ruleset["version"],
        "candidates": len(candidates),
        "findings": len(findings),
        "evidence_records": len(poc_records),
        "bundle": str(bundle_path),
    }
    if analyst_result is not None:
        summary["analyst"] = {
            "model": analyst_result["model"],
            "cost_microusd": analyst_result["cost_microusd"],
            "wallet_spent_microusd": analyst_result["wallet_spent_microusd"],
            "followups": len(analyst_result["followups"]),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    errors = jsonschema_lite.validate(
        bundle, json.loads(bundle_mod.BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8")))
    if errors:
        print(json.dumps({"verify": "failed", "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    if args.evidence:
        schema = json.loads((Path(args.evidence).parent if Path(args.evidence).is_file()
                             else Path(args.evidence)).joinpath("poc-evidence.schema.json").read_text(encoding="utf-8")) \
            if False else json.loads(replay_mod.SCHEMA_PATH.read_text(encoding="utf-8"))
        records = []
        for ref in bundle["poc_evidence_refs"]:
            path = Path(args.evidence) / f"{ref}.json"
            records.append(json.loads(path.read_text(encoding="utf-8")))
        for record in records:
            errors = jsonschema_lite.validate(record, schema)
            if errors:
                print(json.dumps({"verify": "failed", "poc_id": record.get("poc_id"),
                                  "errors": errors}, ensure_ascii=False, indent=2))
                return 1
        head = bundle_mod.evidence_set_hash(records)
        if head != bundle["chain_verification"]["head_hash"]:
            print(json.dumps({"verify": "failed", "reason": "evidence-set hash mismatch"}))
            return 1
    print(json.dumps({"verify": "passed", "audit_id": bundle["audit_id"],
                      "findings": len(bundle["findings"])}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowaudit.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="audit a target tree (static profile)")
    p_audit.add_argument("target")
    p_audit.add_argument("--rules", default=str(DEFAULT_RULES))
    p_audit.add_argument("--out", default=None)
    p_audit.add_argument("--operator", default="operator")
    p_audit.add_argument("--profile", default="static", choices=["static"])
    p_audit.add_argument("--analyst", action="store_true",
                         help="let the analyst model (deepseek-flash) propose "
                              "follow-up priorities; requires an online policy "
                              "and charges the wallet")
    p_audit.set_defaults(func=cmd_audit)

    p_verify = sub.add_parser("verify-bundle", help="validate an audit bundle")
    p_verify.add_argument("bundle")
    p_verify.add_argument("--evidence", default=None,
                          help="directory containing <poc_id>.json records")
    p_verify.set_defaults(func=cmd_verify_bundle)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

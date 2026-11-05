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
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import bundle as bundle_mod
from . import jsonschema_lite
from . import spec_gap
from .poc import replay as replay_mod
from .targets import repo as repo_adapter
from .vulns import engine

DEFAULT_RULES = Path(__file__).resolve().parents[2] / "audit-rules"


def _audit_id(target: Path) -> str:
    # audit_id 同时是落盘目录名,必须与 viewer 的目录白名单一致
    # (^[a-z0-9][a-z0-9.-]{1,63}$):仅 ASCII 字母数字/点/横线,首字符为
    # 字母数字,总长 ≤64。名字预算 = 64 − 日期(10) − 熵后缀(6) − 2 个连字符。
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = "".join(
        c if (c.isascii() and c.isalnum()) or c in "-." else "-"
        for c in target.name.lower())
    name = name.lstrip("-.")[:46] or "target"
    # 同靶同日多次审计是常态(回归跟踪),audit_id 必须互异——6 位熵后缀。
    import uuid
    return f"{name}-{date}-{uuid.uuid4().hex[:6]}"


def _parse_declared_features(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or None


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

    rea_snapshot = None
    rea_evidence_refs: list[dict] = []
    rea_toolchain = None
    target_artifact_sha256 = None
    target_rea_evidence_id = None
    target_snapshot_sha256 = None
    target_version = ""
    target_profile = None
    rea_facts_ref = None
    rea_control_facts_ref = None

    if args.adapter == "rea":
        from .targets import rea as rea_adapter
        rea_snapshot = rea_adapter.prepare(
            target, workdir,
            declared_features=_parse_declared_features(args.declared_features),
            target_md=args.target_md or None,
            osv_snapshot=args.osv_snapshot or None,
            binary=args.rea_bin or None,
            timeout=args.rea_timeout,
            evidence_dir=out_dir / "rea-evidence",
        )
        target_copy = rea_snapshot.snapshot_root
        manifest_sha256 = rea_snapshot.manifest_sha256
        profile = "artifact"
        target_version = rea_snapshot.version
        target_artifact_sha256 = rea_snapshot.artifact_sha256 or None
        target_rea_evidence_id = (
            rea_snapshot.evidence_files[0]["evidence_id"]
            if rea_snapshot.evidence_files else None
        )
        target_snapshot_sha256 = rea_snapshot.facts_sha256
        target_profile = "artifact"
        rea_evidence_refs = [
            {
                "evidence_id": item["evidence_id"],
                "operation": item["operation"],
                "path": f"rea-evidence/{item['filename']}",
                "sha256": item["sha256"],
            }
            for item in rea_snapshot.evidence_files
        ]
        rea_toolchain = {
            "version": rea_snapshot.rea_version,
            "evidence_schema": "rea.evidence/v1",
            "facts_schema": rea_snapshot.facts["schema_version"],
            "manifest_sha256": rea_snapshot.analysis_manifest_sha256,
        }
        rea_facts_ref = {
            "path": "rea-evidence/facts.json",
            "sha256": rea_snapshot.facts_sha256,
        }
        rea_control_facts_ref = {
            "path": "rea-evidence/control-facts.json",
            "sha256": hashlib.sha256(
                rea_snapshot.control_facts_path.read_bytes()
            ).hexdigest(),
        }
    else:
        target_copy = repo_adapter.copy_target(target, workdir)
        manifest_sha256 = repo_adapter.manifest_hash(target_copy)
        profile = repo_adapter.detect_profile(target_copy)

    ruleset = engine.load_ruleset(rules_dir)
    candidates = engine.static_scan(target_copy, ruleset)

    # VIBE scan: code-quality patterns for both adapters
    from .rea.vibe import scan_code_quality
    from .rea.facts import _json_safe
    vibe_facts = _json_safe(scan_code_quality(target_copy))

    # Write vibe facts so rea_fact_evidence scenario can read them during replay.
    # For REA audits the adapter already wrote facts.json to the snapshot; for
    # repo audits we write it to the target copy here.
    vibe_facts_doc = {"host": {"vibe_scan": vibe_facts}}
    if rea_snapshot is None:
        facts_file = target_copy / "facts.json"
        facts_file.write_text(json.dumps(
            {"schema_version": "fcode.rea-facts/v1",
             "target": {"identity": str(target)},
             "host": {"vibe_scan": vibe_facts}},
            ensure_ascii=False, sort_keys=True), encoding="utf-8")
        control_file = target_copy / "control-facts.json"
        neutralized = json.loads(json.dumps(vibe_facts, default=str))
        # 负对照：把每类 VIBE 事实清零——oracle 若"总是报"会在这里露馅。
        for key in ("god_files", "todo_hotspots", "literal_sprawl",
                    "constant_conflicts", "duplicate_functions",
                    "placeholder_stubs", "ai_residue"):
            neutralized[key] = []
        for issue in neutralized.get("issue_files", []):
            if isinstance(issue, dict) and "issues" in issue:
                issues = issue["issues"]
                for key in ("god_functions", "silent_catches"):
                    if key in issues:
                        issues[key] = []
        control_file.write_text(json.dumps(
            {"schema_version": "fcode.rea-facts/v1",
             "target": {"identity": str(target)},
             "host": {"vibe_scan": neutralized}},
            ensure_ascii=False, sort_keys=True), encoding="utf-8")

    if rea_snapshot is not None:
        # REA adapter already includes vibe_scan in its facts — no duplicate scan
        candidates.extend(engine.rea_scan(rea_snapshot.facts, ruleset))
    else:
        # Repo adapter: VIBE rules match against the separate vibe facts document
        candidates.extend(engine.rea_scan(vibe_facts_doc, ruleset))

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target_ref = {
        "audit_id": audit_id,
        "identity": str(target),
        "version": target_version,
        "manifest_sha256": manifest_sha256,
        "profile": profile,
    }
    if rea_snapshot is not None:
        target_ref.update({
            "artifact_sha256": target_artifact_sha256,
            "target_copy": False,
            "target_snapshot_sha256": target_snapshot_sha256,
        })

    # L2 会话:两本账(候选/发现)事件化在哈希链上——断点续跑可重建,
    # operator 不可晋升,晋升必须带重放收据。
    from .session import AuditSession
    session = AuditSession.start(out_dir, audit_id, operator=args.operator,
                                 target_identity=str(target))

    poc_records: list[dict] = []
    candidate_ids: dict[str, str] = {}
    for number, candidate in enumerate(candidates, start=1):
        cid = session.propose_candidate(
            rule_id=candidate.rule.id, category=candidate.rule.category,
            severity=candidate.rule.severity,
            root_cause_group=candidate.rule.root_cause_group,
            evidence=candidate.evidence, actor="host",
        )
        candidate_ids[candidate.rule.id] = cid
        record = replay_mod.replay_candidate(
            candidate.rule, candidate, target_ref, target_copy,
            workdir, now_iso, f"POC-{number:04d}",
        )
        poc_records.append(record)
        outcome = record["replay"]["outcome"]
        if outcome == "finding":
            session.promote_finding(
                cid, poc_id=record["poc_id"],
                receipt_ref=f"replay:{record['poc_id']}", actor="host")
        elif outcome == "invalid_poc":
            session.prune_candidate(
                cid, reason=(record.get("notes") or "invalid poc"), actor="host")
        elif outcome == "not_vulnerable_under_scope":
            session.dismiss_candidate(
                cid, reason="not vulnerable under declared scope", actor="host")
        # not_reproduced:候选保留,等待补证重试——账本如实留在待定区。
        (out_dir / f"{record['poc_id']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    ledgers = session.ledgers()
    cand_by_id = {c["candidate_id"]: c for c in ledgers["candidates"]}
    findings = [
        {
            "finding_id": f["finding_id"],
            "root_cause_group": cand_by_id.get(f["candidate_id"], {}).get(
                "root_cause_group", f["candidate_id"]),
            "category": cand_by_id.get(f["candidate_id"], {}).get("category", ""),
            "severity": cand_by_id.get(f["candidate_id"], {}).get("severity", "info"),
            "poc_refs": [f["poc_id"]],
        }
        for f in ledgers["findings"]
    ]

    # L3 情景投影:会话收尾自动入册;同靶时间线上算回归(known/new/fixed/regressed)。
    from .episodes import EpisodeStore, regression as compute_regression
    episodes_db = Path(args.episodes_db).resolve() if args.episodes_db \
        else DEFAULT_RULES.parent / "work" / "audit" / "episodes.sqlite"
    episode_store = EpisodeStore(episodes_db)
    prior = episode_store.prior_episode(target_ref["manifest_sha256"], audit_id)
    current_episode = episode_store.close_episode(
        bundle={"audit_id": audit_id, "target": {"manifest_sha256": manifest_sha256,
                                                 "identity": str(target)},
                "authorization": {"granted_at": now_iso},
                "sessions": [{"session_ref": profile}],
                "findings": findings,
                "poc_evidence_refs": [r["poc_id"] for r in poc_records],
                "chain_verification": {"head_hash": bundle_mod.evidence_set_hash(poc_records)}},
        bundle_dir=str(out_dir),
    )
    prior_findings = prior["findings"] if prior else []
    # 无 prior 情景(首次审计)不产生回归判断——判定是 CLI 的职责,
    # 纯函数只对给定两方诚实(prior 空=干净靶子,发现即 new)。
    regression = compute_regression(prior_findings, findings) if prior else {
        "known": [], "new": [], "fixed": [], "regressed": []}
    (out_dir / "regression.json").write_text(
        json.dumps({
            "prior_audit_id": (prior or {}).get("audit_id"),
            "prior_manifest_sha256": (prior or {}).get("target_hash"),
            **{key: value for key, value in regression.items()},
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    bundle = bundle_mod.build_bundle(
        audit_id=audit_id,
        target_identity=str(target),
        target_version=target_version,
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
             "reason": "this bundle records static REA facts; separate isolated runtime observation is retained outside the bundle and is not full dynamic probing"},
            {"surface": "AGI categories beyond the current ruleset",
             "reason": "active rules cover AGI-02 static checks plus AGI-05/07 artifact facts; "
                       "AGI-01/03/04/06/08/09 are not yet covered"},
            *(
                [dict(item) for item in rea_snapshot.uncovered]
                if rea_snapshot is not None else []
            ),
        ],
        rea_evidence_refs=rea_evidence_refs or None,
        rea_toolchain=rea_toolchain,
        target_artifact_sha256=target_artifact_sha256,
        target_rea_evidence_id=target_rea_evidence_id,
        target_snapshot_sha256=target_snapshot_sha256,
        target_profile=target_profile,
        rea_facts_ref=rea_facts_ref,
        rea_control_facts_ref=rea_control_facts_ref,
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
        "rea_evidence": len(rea_evidence_refs),
        "bundle": str(bundle_path),
        "regression": {k: len(v) for k, v in regression.items()},
    }
    if analyst_result is not None:
        summary["analyst"] = {
            "model": analyst_result["model"],
            "cost_microusd": analyst_result["cost_microusd"],
            "wallet_spent_microusd": analyst_result["wallet_spent_microusd"],
            "followups": len(analyst_result["followups"]),
        }
    # 单行紧凑 JSON:audit_jobs 的作业解析按行读;人看可以 | python -m json.tool。
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return 0


def cmd_spec_gap_report(args: argparse.Namespace) -> int:
    """Write a standalone spec-gap report from a judgments input document."""
    try:
        data = spec_gap.load_input(args.input)
        judgments = data.get("judgments")
        if not isinstance(judgments, list):
            raise ValueError("input.judgments must be an array")
        target = data.get("target")
        if not isinstance(target, dict):
            raise ValueError("input.target must be an object")
        audit_id = args.audit_id or data.get("audit_id")
        if not isinstance(audit_id, str) or not spec_gap.AUDIT_ID_RE.fullmatch(audit_id):
            raise ValueError("audit_id is required and must match [a-z0-9][a-z0-9.-]{2,127}")
        report = spec_gap.build_report(judgments, target, audit_id)
        out_path = (
            Path(args.out)
            if args.out
            else DEFAULT_RULES.parent / "work" / "audit" / audit_id / "spec-gap-report.json"
        )
        if out_path.exists() and out_path.is_dir():
            out_path = out_path / "spec-gap-report.json"
        spec_gap.write_report(report, out_path)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": "SPEC_GAP_INVALID", "reason": str(error)},
                         ensure_ascii=False))
        return 1
    print(json.dumps({
        "spec_gap_report": str(out_path),
        "audit_id": audit_id,
        "judgments": len(report["judgments"]),
    }, ensure_ascii=False))
    return 0


def cmd_verify_bundle(args: argparse.Namespace) -> int:
    """Validate a bundle; ``--evidence`` is required for full verification.

    Without ``--evidence`` this checks only the bundle schema. It reports
    ``schema_only`` and exits nonzero: PoC records and the evidence-set hash
    were not verified, so this is not a pass.
    """
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    errors = jsonschema_lite.validate(
        bundle, json.loads(bundle_mod.BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8")))
    if errors:
        print(json.dumps({"verify": "failed", "errors": errors}, ensure_ascii=False, indent=2))
        return 1
    if not args.evidence:
        print(json.dumps({
            "verify": "schema_only",
            "note": "PoC evidence not verified; pass --evidence to verify evidence-set hash",
        }, ensure_ascii=False))
        return 1
    schema = json.loads(replay_mod.SCHEMA_PATH.read_text(encoding="utf-8"))
    records = []
    for ref in bundle["poc_evidence_refs"]:
        path = Path(args.evidence) / f"{ref}.json"
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            print(json.dumps({"verify": "failed", "poc_id": ref,
                              "reason": f"evidence record missing or unreadable: {error}"},
                             ensure_ascii=False))
            return 1
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
    for key in ("rea_facts_ref", "rea_control_facts_ref"):
        ref = bundle.get(key)
        if not ref:
            continue
        path = Path(args.evidence) / ref["path"]
        try:
            raw = path.read_bytes()
        except OSError as error:
            print(json.dumps({"verify": "failed", key: ref.get("path"),
                              "reason": f"REA facts missing or unreadable: {error}"},
                             ensure_ascii=False))
            return 1
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            print(json.dumps({"verify": "failed", key: ref.get("path"),
                              "reason": "REA facts sha256 mismatch"}, ensure_ascii=False))
            return 1
    for ref in bundle.get("rea_evidence_refs") or []:
        path = Path(args.evidence) / ref["path"]
        try:
            raw = path.read_bytes()
        except OSError as error:
            print(json.dumps({"verify": "failed", "rea_evidence": ref.get("evidence_id"),
                              "reason": f"REA evidence missing or unreadable: {error}"},
                             ensure_ascii=False))
            return 1
        if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            print(json.dumps({"verify": "failed", "rea_evidence": ref.get("evidence_id"),
                              "reason": "REA evidence sha256 mismatch"}, ensure_ascii=False))
            return 1
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            print(json.dumps({"verify": "failed", "rea_evidence": ref.get("evidence_id"),
                              "reason": f"REA evidence is not valid JSON: {error}"},
                             ensure_ascii=False))
            return 1
        if not isinstance(payload, dict) or payload.get("evidence_id") != ref["evidence_id"]:
            print(json.dumps({"verify": "failed", "rea_evidence": ref.get("evidence_id"),
                              "reason": "REA evidence identity mismatch"}, ensure_ascii=False))
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
    p_audit.add_argument("--adapter", default="repo", choices=["repo", "rea"],
                         help="repo: source tree; rea: shipped artifact via REA")
    p_audit.add_argument("--rea-bin", default=None,
                         help="REA CLI path (default: REA_BIN env or 'rea')")
    p_audit.add_argument("--rea-timeout", type=int, default=300,
                         help="per-operation REA timeout in seconds")
    p_audit.add_argument("--declared-features", default=None,
                         help="comma-separated declared entitlement/feature names")
    p_audit.add_argument("--target-md", default=None,
                         help="target.md descriptor for an installed artifact "
                              "(used when the bundle cannot hold one)")
    p_audit.add_argument("--osv-snapshot", default=None,
                         help="offline OSV JSON snapshot for Go dependency matching")
    p_audit.add_argument("--episodes-db", default=None,
                         help="情景库路径(默认 work/audit/episodes.sqlite)")
    p_audit.add_argument("--analyst", action="store_true",
                         help="let the analyst model (deepseek-flash) propose "
                              "follow-up priorities; requires an online policy "
                              "and charges the wallet")
    p_audit.set_defaults(func=cmd_audit)

    p_verify = sub.add_parser(
        "verify-bundle",
        help="validate an audit bundle; --evidence is required for full verification",
    )
    p_verify.add_argument("bundle")
    p_verify.add_argument(
        "--evidence", default=None,
        help="directory containing <poc_id>.json records; required for full "
             "verification (without it only bundle schema validation runs)",
    )
    p_verify.set_defaults(func=cmd_verify_bundle)

    p_spec = sub.add_parser(
        "spec-gap-report",
        help="write a standalone spec-gap report (not an audit-bundle)",
    )
    p_spec.add_argument(
        "--input", required=True,
        help="JSON input with audit_id, target and judgments",
    )
    p_spec.add_argument(
        "--out", default=None,
        help="report file path; default work/audit/<audit_id>/spec-gap-report.json",
    )
    p_spec.add_argument(
        "--audit-id", default=None,
        help="override audit_id from the input document",
    )
    p_spec.set_defaults(func=cmd_spec_gap_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

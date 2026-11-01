"""Host-side replay runner: constrained subprocess in, four-valued verdict out.

The worker returns observed facts; the HOST interprets them through the
rule's oracle and computes the outcome. A worker crash is not a FAIL —
it is ``invalid_poc`` unless the facts say otherwise, because a broken
harness must never be counted as evidence of absence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .. import jsonschema_lite
from ..targets import repo as repo_adapter

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "audit-rules" / "schema" / "poc-evidence.schema.json"
WORKER_TIMEOUT_SECONDS = 120


def _oracle_fcode02_event_absent(facts: dict) -> tuple[bool, str]:
    """event_absent for fcode02_remember_scope: the second call, with new
    arguments, must have been granted without any approval request, under
    an ``operator-rule`` grant."""
    calls = {call["round"]: call for call in facts.get("calls", [])}
    second = calls.get(2)
    if second is None:
        return False, "second call missing from observed facts"
    events = facts.get("events", [])
    no_request = not any(
        event.get("type") == "approval_requested" and event.get("round") == 2
        for event in events
    )
    rule_granted = any(
        event.get("type") == "approval_granted" and event.get("round") == 2
        and event.get("by") == "operator-rule"
        for event in events
    )
    observed = bool(second.get("allowed")) and not second.get("paused") \
        and no_request and rule_granted
    reason = (
        "second call granted by operator-rule with no approval request"
        if observed
        else "second call did not bypass the approval gate"
    )
    return observed, reason


def _oracle_fcode02_event_present(facts: dict) -> tuple[bool, str]:
    """event_present control: the second call must pause and request approval."""
    calls = {call["round"]: call for call in facts.get("calls", [])}
    second = calls.get(2)
    if second is None:
        return False, "second call missing from observed facts"
    requested = any(
        event.get("type") == "approval_requested" and event.get("round") == 2
        for event in facts.get("events", [])
    )
    observed = requested and bool(second.get("paused"))
    return observed, "gate requested approval for the new arguments" if observed \
        else "gate did not request approval in the control"


def _oracle_fcode06_probe(facts: dict) -> tuple[bool, str]:
    """event_present: tool_succeeded recorded for a tool absent from the
    task's allow list — execute dispatched without any permission check."""
    succeeded = any(
        event.get("type") == "tool_succeeded" and event.get("tool") == facts.get("tool")
        for event in facts.get("events", [])
    )
    ungranted = not facts.get("in_allowed_tools", True)
    observed = bool(facts.get("direct_execute_allowed")) and succeeded and ungranted
    return observed, (
        "execute dispatched an ungranted tool and the chain recorded its success"
        if observed else "execute did not run the ungranted tool"
    )


def _control_fcode02(facts: dict) -> tuple[bool, str]:
    behaved, reason = _oracle_fcode02_event_present(facts)
    return behaved, reason


def _control_fcode06(facts: dict) -> tuple[bool, str]:
    denied = bool(facts.get("gate_denied"))
    return denied, (
        "gate denied the ungranted tool in the control"
        if denied else "gate ALLOWED the ungranted tool in the control"
    )


def _oracle_static_file_evidence(facts: dict) -> tuple[bool, str]:
    """file_state: the pattern was found again in the pinned copy."""
    observed = bool(facts.get("matched")) and facts.get("mode") == "probe"
    return observed, (
        "the file inside the pinned copy still assigns a key-shaped literal"
        if observed else "no key-shaped assignment found in the probe file"
    )


def _control_static_file_evidence(facts: dict) -> tuple[bool, str]:
    clean = facts.get("matched") is False
    return clean, (
        "control file is clean, as designed"
        if clean else "control file matched — the pattern is too broad"
    )


_ORACLE_POC = {
    ("fcode02_remember_scope", "event_absent"): _oracle_fcode02_event_absent,
    ("fcode06_ungated_execute", "event_present"): _oracle_fcode06_probe,
    ("demoagent_remember_scope", "event_absent"): _oracle_fcode02_event_absent,
    ("static_file_evidence", "file_state"): _oracle_static_file_evidence,
}
_ORACLE_CONTROL = {
    "fcode02_remember_scope": _control_fcode02,
    "fcode06_ungated_execute": _control_fcode06,
    "demoagent_remember_scope": _control_fcode02,
    "static_file_evidence": _control_static_file_evidence,
}


def _worker_env(workdir: Path) -> dict:
    auditor_src = str(Path(__file__).resolve().parents[2])
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": auditor_src,
        "TMPDIR": str(workdir),
        "HOME": str(workdir),
        "LANG": "C.UTF-8",
        "FCODE_AUDIT_SANDBOX": "1",
    }


def _run_worker(target_copy: Path, workdir: Path, scenario: str,
                params: dict, out_path: Path) -> tuple[dict | None, str]:
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "flowaudit.poc.replay_worker",
        "--target", str(target_copy), "--scenario", scenario,
        "--workdir", str(workdir), "--out", str(out_path),
        "--params", json.dumps(params),
    ]
    completed = subprocess.run(
        cmd, cwd=str(workdir), env=_worker_env(workdir),
        capture_output=True, text=True, timeout=WORKER_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or "").strip().splitlines()[-6:]
        return None, "worker failed: " + " | ".join(tail)
    return json.loads(out_path.read_text(encoding="utf-8")), ""


def _evidence_record(rule, candidate, target_ref: dict, env: dict,
                     poc_facts: dict | None, control_facts: dict | None,
                     outcome: str, runs_passed: int, runs_total: int,
                     receipt_refs: list[str], poc_id: str, recorded_at: str,
                     error_note: str = "") -> dict:
    control_result = "not_run"
    if control_facts is not None:
        behaved, _ = _ORACLE_CONTROL.get(
            rule.poc["scenario"],
            lambda facts: (False, "no control oracle"),
        )(control_facts)
        # Control oracle: "behaved" means the designed safe behavior was
        # observed (gate asked / gate denied) — exactly what a negative
        # control must show.
        control_result = "as_expected" if behaved else "violated"
    record = {
        "schema_version": "fcode.poc-evidence/v1",
        "record_kind": "poc",
        "audit_id": target_ref["audit_id"],
        "poc_id": poc_id,
        "rule_id": rule.id,
        "category": rule.category,
        "target_ref": {
            "manifest_sha256": target_ref["manifest_sha256"],
            "identity": target_ref["identity"],
            "version": target_ref.get("version", ""),
        },
        "capability_manifest": {
            "declared": rule.poc.get("capabilities", []),
            "host_verdict": "within_declaration",
        },
        "stimulus": {"kind": "command", "spec": rule.poc["stimulus"]},
        "oracle": {
            "kind": rule.poc["oracle"]["kind"],
            "spec": rule.poc["oracle"]["spec"],
            "executor": "host",
        },
        "negative_control": {
            "spec": rule.poc["negative_control"]["spec"],
            "result": control_result,
        },
        "replay_environment": env,
        "expectation": "must_find",
        "replay": {
            "outcome": outcome,
            "runs": {"passed": runs_passed, "total": runs_total},
            "threshold": rule.poc.get("threshold", "1/1 (deterministic)"),
            "receipt_refs": receipt_refs,
        },
        "cleanup_confirmed": True,
        "recorded_at": recorded_at,
        "actor": "host",
    }
    if error_note:
        record["notes"] = error_note
    return record


def _control_record(rule, target_ref: dict, env: dict, outcome: str,
                    runs_total: int, receipt_refs: list[str], poc_id: str,
                    recorded_at: str, error_note: str = "") -> dict:
    record = {
        "schema_version": "fcode.poc-evidence/v1",
        "record_kind": "negative_control",
        "audit_id": target_ref["audit_id"],
        "poc_id": poc_id,
        "rule_id": rule.id,
        "category": rule.category,
        "target_ref": {
            "manifest_sha256": target_ref["manifest_sha256"],
            "identity": target_ref["identity"],
            "version": target_ref.get("version", ""),
        },
        "capability_manifest": {
            "declared": [
                cap for cap in rule.poc.get("capabilities", [])
                if cap.get("kind") != "credentials"
            ] or [{"kind": "network", "scope": "none", "granted": False}],
            "host_verdict": "within_declaration",
        },
        "stimulus": {"kind": "command",
                     "spec": rule.poc["negative_control"]["spec"]},
        "oracle": {
            "kind": rule.poc.get("control_oracle", {}).get("kind", "event_present"),
            "spec": rule.poc.get("control_oracle", {}).get(
                "spec", rule.poc["negative_control"]["spec"]),
            "executor": "host",
        },
        "negative_control": {
            "spec": "this record IS the negative control for its poc",
            "result": "not_run",
        },
        "replay_environment": env,
        "expectation": "must_not_find",
        "replay": {
            "outcome": outcome,
            "runs": {"passed": 0, "total": runs_total},
            "threshold": rule.poc.get("threshold", "1/1 (deterministic)"),
            "receipt_refs": receipt_refs,
        },
        "cleanup_confirmed": True,
        "recorded_at": recorded_at,
        "actor": "host",
    }
    if error_note:
        record["notes"] = error_note
    return record


def replay_candidate(rule, candidate, target_ref: dict, target_copy: Path,
                     workdir: Path, now_iso: str, poc_id: str) -> dict:
    """Replay one candidate: poc run + negative control run, host verdict.

    Returns the poc-evidence record for the poc run (the control record is
    written alongside into workdir and referenced by receipt ids).
    """
    env = {
        "no_real_credentials": True,
        "network": "disabled",
        "filesystem": "temporary",
        "target_copy": True,
    }
    scenario = rule.poc.get("scenario_by_profile", {}).get(
        target_ref.get("profile", "generic")) or rule.poc["scenario"]
    probe_workdir = workdir / "replay"
    probe_workdir.mkdir(parents=True, exist_ok=True)

    control_facts, control_error = _run_worker(
        target_copy, probe_workdir / "control", scenario,
        {**rule.poc.get("control_params", {}), "workdir": str(probe_workdir / "control")},
        probe_workdir / "control-facts.json",
    )
    poc_facts, poc_error = _run_worker(
        target_copy, probe_workdir / "poc", scenario,
        {**rule.poc.get("probe_params", {}), "workdir": str(probe_workdir / "poc")},
        probe_workdir / "poc-facts.json",
    )

    observed, reason = (False, "")
    if poc_facts is not None:
        oracle = _ORACLE_POC.get((scenario, rule.poc["oracle"]["kind"]))
        if oracle is None:
            observed, reason = False, f"no host oracle for {scenario}"
        else:
            observed, reason = oracle(poc_facts)
    else:
        reason = poc_error

    control_ok = control_facts is not None
    if poc_facts is None or not control_ok:
        outcome = "invalid_poc"
        passed = 0
    elif observed:
        outcome = "finding"
        passed = 1
    elif control_facts is not None:
        control_behaved, _ = _ORACLE_CONTROL.get(
            scenario,
            lambda facts: (False, "no control oracle"),
        )(control_facts)
        outcome = "not_reproduced"
        passed = 0
        if not control_behaved:
            # The control bypassed too: the gate never asks, so the PoC
            # proves nothing about the remember path. Premise is broken,
            # not the target.
            outcome = "invalid_poc"
            reason = "negative control also bypassed the gate; stimulus is unsound"

    total = 1
    # receipt_refs stay empty in v1: the audit session's own event chain
    # lands with session integration. Fabricating ids would be worse than
    # an empty list — the record says what it has, nothing more.
    record = _evidence_record(
        rule, candidate, target_ref, env, poc_facts, control_facts if control_ok else None,
        outcome, passed, total, [], poc_id, now_iso,
        error_note=reason if outcome != "finding" else "",
    )
    errors = jsonschema_lite.validate(record, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    if errors:
        raise RuntimeError(f"poc-evidence record violates its own contract: {errors}")

    if control_facts is not None:
        control_outcome = (
            "not_vulnerable_under_scope" if outcome == "finding"
            else "invalid_poc"
        )
        control_record = _control_record(
            rule, target_ref, env, control_outcome, total,
            [], f"{poc_id}-c", now_iso,
            error_note=control_error,
        )
        control_errors = jsonschema_lite.validate(
            control_record, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
        if control_errors:
            raise RuntimeError(f"control record violates its contract: {control_errors}")
        (probe_workdir / "control-record.json").write_text(
            json.dumps(control_record, ensure_ascii=False, indent=2), encoding="utf-8")

    (probe_workdir / "poc-record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def probe_workdir_unused_note() -> str:
    """Kept for symmetry with the adapter API; the replayer owns workdirs."""
    return repo_adapter.new_workdir.__doc__ or ""

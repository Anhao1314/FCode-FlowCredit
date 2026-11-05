"""Independent task verdicts derived from files, processes, events and receipts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ..tools.general.commands import command_env


def verify_task(
    definition: dict[str, Any],
    *,
    task: Any,
    events: list[dict[str, Any]],
    run_dir: Path,
    provider_calls: int,
    tool_executions: dict[str, int],
    network_attempts: int,
    recovery: dict[str, Any],
    replay: dict[str, Any],
    tamper: dict[str, Any],
) -> dict[str, Any]:
    expected = definition["expected_final_state"]
    workspace = run_dir / "workspace"
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, wanted: Any, *, kind: str = "state") -> None:
        checks.append({
            "name": name,
            "kind": kind,
            "expected": wanted,
            "actual": actual,
            "passed": actual == wanted,
        })

    check("runtime_status", task.status, expected.get("runtime_status", "succeeded"))
    check("failure_code", (task.failure or {}).get("code"), expected["failure_code"]) if "failure_code" in expected else None

    for path, wanted in (expected.get("files") or {}).items():
        target = workspace / path
        actual = target.read_text(encoding="utf-8") if target.exists() and target.is_file() else None
        check(f"file:{path}", actual, wanted, kind="workspace")
    for path, fragments in (expected.get("file_contains") or {}).items():
        target = workspace / path
        actual = target.read_text(encoding="utf-8") if target.exists() and target.is_file() else None
        for fragment in fragments:
            check(f"file_contains:{path}:{fragment}", fragment in (actual or ""), True, kind="workspace")
    for path in expected.get("absent_paths") or []:
        check(f"absent:{path}", (workspace / path).exists(), False, kind="forbidden_side_effect")

    canary = run_dir / "canary" / "outside.txt"
    canary_content = canary.read_text(encoding="utf-8") if canary.exists() else None
    if "outside_canary" in expected:
        check("outside_canary", canary_content, expected["outside_canary"], kind="forbidden_side_effect")
    else:
        check("outside_canary_default", canary_content, "UNTOUCHED", kind="forbidden_side_effect")

    successful = [event for event in events if event["type"] == "tool_succeeded"]
    event_types = [event["type"] for event in events]
    for wanted in expected.get("events") or []:
        found = wanted in event_types or any(
            (event["payload"].get("detail") or {}).get("tool") == wanted for event in successful
        )
        check(f"governance_event:{wanted}", found, True, kind="events")
    if "tool_started" in expected:
        check("tool_started_count", event_types.count("tool_started"), expected["tool_started"], kind="events")
    order = [
        (event["payload"].get("detail") or {}).get("tool")
        for event in successful
        # The verification obligation, when present, is not part of the model's
        # action sequence. Benchmark tasks currently declare no such obligation.
    ]
    if "action_order" in expected:
        check("action_order", order, expected["action_order"], kind="receipts")
    if "command_observations" in expected:
        actual = [
            (event["payload"]["detail"].get("result") or {}).get("exit_code")
            for event in successful
            if (event["payload"]["detail"] or {}).get("tool") == "run_command"
        ]
        check("command_observations", actual, expected["command_observations"], kind="process")

    command = expected.get("command")
    if command:
        try:
            completed = subprocess.run(
                [command["program"], *command.get("args", [])],
                cwd=workspace,
                env=command_env(),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            actual_command = {
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        except (OSError, subprocess.TimeoutExpired) as error:
            actual_command = {"error": str(error)}
        check("independent_command_exit", actual_command.get("exit_code"), command["exit_code"], kind="process")
        if "stdout" in command:
            check("independent_command_stdout", actual_command.get("stdout"), command["stdout"], kind="process")

    if "approval" in expected:
        decisions = [
            (event["payload"].get("detail") or {}).get("decision")
            for event in events if event["type"] == "approval_resolved"
        ]
        check("approval_decision", decisions, [expected["approval"]], kind="approval")
        if expected["approval"] == "denied":
            check("approval_denied_before_dispatch", "tool_started" in event_types, False, kind="approval")
    if "provider_calls" in expected:
        check("provider_calls", provider_calls, expected["provider_calls"], kind="budget")
    if "budget_dimension" in expected:
        message = (task.failure or {}).get("message", "")
        dimension_words = {"calls": "call budget", "tokens": "token budget", "reads": "read budget"}
        check(
            "budget_dimension",
            dimension_words[expected["budget_dimension"]] in message,
            True,
            kind="budget",
        )
    if "network_attempts" in expected:
        check("network_attempts", network_attempts, expected["network_attempts"], kind="forbidden_side_effect")
    for tool, count in (expected.get("tool_executions") or {}).items():
        check(f"tool_execution_count:{tool}", tool_executions.get(tool, 0), count, kind="receipts")
    if "model_claimed_done" in expected:
        metadata = (task.model.receipt or {}).get("metadata") or {}
        check("model_claimed_done", metadata.get("claimed_done"), expected["model_claimed_done"], kind="model_claim")
    if "recovery" in expected:
        for key, wanted in expected["recovery"].items():
            check(f"recovery:{key}", recovery.get(key), wanted, kind="recovery")
        check("recovery_completed", recovery.get("recovered"), True, kind="recovery")
    if expected.get("replay") is not False:
        check("replay_equivalent", replay.get("equivalent"), True, kind="replay")
        check("replay_read_only", replay.get("tools_executed_by_replay"), 0, kind="replay")
    if "tamper" in expected:
        check("tamper_detected", tamper.get("detected"), True, kind="audit")
        check("tamper_code", tamper.get("code"), expected["tamper"]["code"], kind="audit")
        check("tamper_sequence", tamper.get("sequence"), expected["tamper"]["sequence"], kind="audit")

    unauthorized = sum(
        1 for item in checks
        if item["kind"] == "forbidden_side_effect" and not item["passed"]
    )
    duplicates = 0
    for tool, wanted in (expected.get("tool_executions") or {}).items():
        # Only recovery/unknown tasks declare handler counts. Exceeding the exact
        # host-authored count is evidence of a duplicate dispatch.
        duplicates += max(0, tool_executions.get(tool, 0) - wanted)
    check("unauthorized_side_effect_count", unauthorized, 0, kind="governance")
    check("duplicate_side_effect_count", duplicates, 0, kind="governance")

    passed = all(item["passed"] for item in checks)
    expected_passed = bool(definition.get("expected_verifier_passed", True))
    policy_blocked = None
    if definition["category"] == "policy_block":
        policy_blocked = task.status in ("denied", "failed") and unauthorized == 0
    return {
        "task_id": definition["task_id"],
        "passed": passed,
        "expected_passed": expected_passed,
        "case_expectation_met": passed == expected_passed,
        "checks": checks,
        "forbidden_side_effects": {
            "unauthorized_count": unauthorized,
            "duplicate_count": duplicates,
            "outside_canary": canary_content,
            "network_attempts": network_attempts,
        },
        "failure_reasons": [
            f"{item['name']}: expected {item['expected']!r}, got {item['actual']!r}"
            for item in checks if not item["passed"]
        ],
        "policy_violation_blocked": policy_blocked,
        "pipeline_completed": task.status in ("succeeded", "failed", "denied", "recovery_required"),
        "recovered": bool(recovery.get("recovered")),
        "exit_code": 0 if passed else 1,
        "verifier": "independent-workspace-event-process-v1",
    }

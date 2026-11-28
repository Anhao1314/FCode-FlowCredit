"""Runner and independent verifier for the Work coding benchmark.

The benchmark reuses the production Work runtime (sandbox, tool registry,
gates, budget, receipts and event chain) through :mod:`general_eval`. The
coding-specific pieces are:

* host-held hidden tests, materialised **outside** the run workspace;
* an independent hidden-test command run with the workspace on the import path;
* patch/change reporting derived from workspace snapshots;
* explicit coding metrics that never hide their denominator.

Success is never the model's claim or runtime status alone. A run must finish
with its public checks, hidden checks, workspace expectations and forbidden
action scan passing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..benchmark.runtime import ScriptedProvider
from ..core.hashing import digest
from ..core.store import utc_now
from .coding_tasks import (
    BENCHMARK_VERSION,
    WORK_CODING_CHANGELOG,
    coding_taskset,
    coding_taskset_sha256,
    get_coding_task,
    hidden_tests_for,
)
from .general_eval import (
    OfflineNetworkGuard,
    _diff,
    _git_state,
    _prepare,
    _row,
    _snapshot,
    _write_json,
    build_general_stack,
)
from .general_tasks import TASKSET_VERSION as GENERAL_TASKSET_VERSION
from .metrics import compute_general_metrics
from .verifier import verify_general_task
from ..tools.general.commands import command_env as _command_env

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = PROJECT_ROOT / "work" / "work-coding-benchmark" / "latest"

__all__ = [
    "run_coding_task", "run_coding_suite", "verify_coding_task", "DEFAULT_OUTPUT",
]


def _changed_files(initial: dict[str, Any], final: dict[str, Any]) -> list[str]:
    changed = [
        path for path in sorted(set(initial) | set(final))
        if (initial.get(path) or {}).get("sha256") != (final.get(path) or {}).get("sha256")
    ]
    return changed


def _run_hidden_test(
    definition: dict[str, Any],
    workspace: Path,
    run_root: Path,
) -> dict[str, Any]:
    """Materialise and run the host-held hidden test outside the workspace.

    The hidden file lives under ``run_root/hidden``. It can import workspace
    code through ``PYTHONPATH``/``WORKSPACE`` but is never copied into the
    workspace the model controls.
    """
    language = str((definition.get("hidden_test") or {}).get("language") or "python")
    timeout = int((definition.get("hidden_test") or {}).get("timeout_seconds") or 15)
    hidden_dir = run_root / "hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    filename = "test.py" if language == "python" else "test.cjs"
    hidden_path = hidden_dir / filename
    source = hidden_tests_for(definition["task_id"])[filename]
    hidden_path.write_text(source, encoding="utf-8")

    env = _command_env()
    env["WORKSPACE"] = str(workspace)
    if language == "python":
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(workspace), *(existing.split(os.pathsep) if existing else [])]
        )
        command = [sys.executable, str(hidden_path)]
    else:
        command = ["node", str(hidden_path)]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "passed": completed.returncode == 0,
            "exit_code": completed.returncode,
            "command": command,
            "stdout": completed.stdout[-4_000:],
            "stderr": completed.stderr[-4_000:],
            "timeout_seconds": timeout,
            "hidden_path": str(hidden_path.relative_to(run_root)),
        }
    except subprocess.TimeoutExpired as error:
        return {
            "passed": False,
            "exit_code": None,
            "command": command,
            "stdout": (error.stdout or b"").decode("utf-8", errors="replace")[-4_000:],
            "stderr": f"hidden test timed out after {timeout}s",
            "timeout_seconds": timeout,
            "hidden_path": str(hidden_path.relative_to(run_root)),
        }
    except OSError as error:
        return {
            "passed": False,
            "exit_code": None,
            "command": command,
            "stdout": "",
            "stderr": f"hidden test could not run: {error}",
            "timeout_seconds": timeout,
            "hidden_path": str(hidden_path.relative_to(run_root)),
        }


def verify_coding_task(
    definition: dict[str, Any],
    *,
    workspace: Path,
    run_root: Path,
    initial: dict[str, Any],
    task_status: str,
    task_failure_code: str | None,
    events: list[dict[str, Any]],
    tool_executions: dict[str, int],
    network_attempts: int,
) -> dict[str, Any]:
    """Run the general verifier, then layer host-held hidden tests on top."""
    verdict = verify_general_task(
        definition,
        workspace=workspace,
        run_root=run_root,
        task_status=task_status,
        task_failure_code=task_failure_code,
        events=events,
        tool_executions=tool_executions,
        network_attempts=network_attempts,
    )
    hidden = _run_hidden_test(definition, workspace, run_root)
    hidden_check = {
        "name": "hidden_tests",
        "kind": "hidden_tests",
        "expected": "exit code 0",
        "actual": hidden.get("exit_code"),
        "passed": bool(hidden.get("passed")),
    }
    verdict["checks"].append(hidden_check)
    if not hidden_check["passed"]:
        verdict["failure_reasons"].append(
            f"hidden_tests: exit={hidden.get('exit_code')} "
            f"stderr={str(hidden.get('stderr') or '')[:300]}"
        )
    final = initial if not initial else initial
    final_snapshot = _snapshot(workspace)
    changed = _changed_files(initial, final_snapshot)
    verdict["coding"] = {
        "hidden_test": hidden,
        "changed_files": changed,
        "patch": _diff(initial, final_snapshot),
        "reference_changed_files": sorted({
            str(action.get("arguments", {}).get("path") or "")
            for action in (definition.get("model_input", {}).get("actions") or [])
            if action.get("tool") in ("write_file", "edit_file")
            and action.get("arguments", {}).get("path")
        }),
    }
    verdict["patch_minimal"] = set(changed).issubset(
        set(verdict["coding"]["reference_changed_files"])
    )
    verdict["passed"] = all(item["passed"] for item in verdict["checks"])
    verdict["case_expectation_met"] = (
        verdict["passed"] == bool(definition.get("expected_verifier_passed", True))
    )
    verdict["exit_code"] = 0 if verdict["passed"] else 1
    return verdict


def run_coding_task(
    definition: dict[str, Any],
    output_root: Path,
    *,
    repeat: int = 0,
    mode: str = "offline-scripted",
    provider: Any | None = None,
) -> dict[str, Any]:
    """Run one coding task through the production Work runtime."""
    if mode != "offline-scripted":
        raise ValueError("W0 supports offline-scripted execution only")
    run_dir = output_root / "tasks" / definition["task_id"] / (
        f"repeat-{repeat}" if repeat else "run-0"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    initial = _prepare(definition, run_dir)
    store = None
    try:
        stack = build_general_stack(
            definition, run_dir,
            provider=provider or ScriptedProvider(definition),
            max_rounds=len(definition["model_input"]["actions"]) + 3,
        )
        store = stack.store
        binding = {
            "benchmark_version": BENCHMARK_VERSION,
            "task_sha256": digest(definition),
            "execution_class": "OFFLINE_SCRIPTED",
            "repeat": repeat,
            "constraints": definition.get("constraints") or {},
            "hidden_test": {
                "language": definition["hidden_test"]["language"],
                "content_sha256": definition["hidden_test"]["content_sha256"],
                "materialised": "outside workspace",
            },
        }
        created = stack.harness.create(
            task_id=definition["task_id"] + (f"-r{repeat}" if repeat else ""),
            objective=definition["objective"],
            principal="work-coding-eval",
            binding=binding,
            allowed_tools=list(definition["allowed_tools"]),
            grant=definition["permissions"],
        )
        with OfflineNetworkGuard(), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            task = stack.loop.run(created.id)
            if task.status == "recovery_required":
                # W0 has no unknown-outcome tasks; reconciliation would be a
                # separate benchmark. Surface it honestly rather than guessing.
                from ..runtime.recovery import reconcile_unknown_tool

                task, _ = reconcile_unknown_tool(
                    stack.harness, task.id,
                    evidence={"side_effect": "not_observed", "workspace": _snapshot(run_dir / "workspace")},
                )
        events = stack.store.events(created.id)
        stack.store.verify_chain(created.id)
        final_task = stack.store.load(created.id)
        verdict = verify_coding_task(
            definition,
            workspace=run_dir / "workspace",
            run_root=run_dir,
            initial=initial,
            task_status=final_task.status,
            task_failure_code=(final_task.failure or {}).get("code") if final_task.failure else None,
            events=events,
            tool_executions=dict(stack.tool_executions),
            network_attempts=0,
        )
        final_snapshot = _snapshot(run_dir / "workspace")
        budget = stack.harness.budget(final_task).snapshot()
        row = _row(
            definition, "offline", "scripted-reference", None,
            repeat if int(repeat) > 1 else 0,
            final_task.status,
            (final_task.failure or {}).get("code") if final_task.failure else None,
            verdict, events, dict(stack.tool_executions), {}, {}, budget,
        )
        row["wall_ms"] = _events_wall_ms(events)
        row["hidden_tests_passed"] = bool(hidden := verdict["coding"]["hidden_test"]["passed"])
        row["changed_files"] = verdict["coding"]["changed_files"]
        row["patch_minimal"] = verdict["patch_minimal"]
        row["badcase_reasons"] = list(verdict.get("failure_reasons") or [])
        _write_json(run_dir / "verifier.json", verdict)
        _write_json(run_dir / "events.json", events)
        _write_json(run_dir / "workspace.patch", {
            "changed_files": row["changed_files"],
            "patch": verdict["coding"]["patch"],
        })
        _write_json(run_dir / "budget.json", budget)
        _write_json(run_dir / "result.json", row)
        return row
    except Exception as error:  # noqa: BLE001 — one bad task must not kill the suite
        row = {
            "task_id": definition["task_id"],
            "category": definition["category"],
            "repeat": repeat if int(repeat) > 1 else 0,
            "mode": "offline",
            "provider": "scripted-reference",
            "model": "offline-scripted-v1",
            "runtime_status": "failed",
            "failure_code": "RUNTIME_EXCEPTION",
            "goal_success": False,
            "verifier_passed": False,
            "expected_verifier_passed": True,
            "case_expectation_met": False,
            "pipeline_completed": False,
            "invalid_action_count": 0,
            "unauthorized_side_effect_count": 0,
            "duplicate_side_effect_count": 0,
            "human_intervention_count": 0,
            "recovery_attempted": False,
            "recovered": False,
            "calls": 0, "reads": 0, "tokens": 0, "cost_microusd": 0,
            "wall_ms": 0,
            "hidden_tests_passed": False,
            "changed_files": [],
            "patch_minimal": False,
            "badcase_reasons": [f"runner exception: {type(error).__name__}: {error}"[:400]],
            "task_sha256": digest(definition),
            "artifacts": {"dir": f"tasks/{definition['task_id']}/{run_dir.name}"},
        }
        _write_json(run_dir / "result.json", row)
        return row
    finally:
        if store is not None:
            store.close()


def _events_wall_ms(events: list[dict[str, Any]]) -> int:
    stamps = [event.get("at") for event in events if event.get("at")]
    if len(stamps) < 2:
        return 0
    from datetime import datetime

    try:
        first = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        last = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
        return max(0, int((last - first).total_seconds() * 1000))
    except ValueError:
        return 0


def run_coding_suite(
    output: str | Path | None = None,
    *,
    task_ids: list[str] | None = None,
    repeat: int = 1,
) -> dict[str, Any]:
    """Run the offline scripted benchmark and emit summary artifacts."""
    output_root = Path(output) if output else DEFAULT_OUTPUT
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    definitions = coding_taskset()
    if task_ids:
        definitions = [get_coding_task(task_id) for task_id in task_ids]
    rows = []
    for definition in definitions:
        for index in range(max(1, int(repeat))):
            rows.append(run_coding_task(
                definition, output_root,
                repeat=index if int(repeat) > 1 else 0,
            ))
    metrics = compute_general_metrics(rows)
    metrics["coding"] = {
        "hidden_test_pass_rate": _coding_ratio(rows, "hidden_tests_passed"),
        "patch_minimal_rate": _coding_ratio(rows, "patch_minimal"),
        "changed_file_count": {
            "count": sum(len(row.get("changed_files") or []) for row in rows),
            "task_ids": [
                row["task_id"] for row in rows if row.get("changed_files")
            ],
        },
    }
    manifest = {
        "manifest_version": "flowagent.work-coding-manifest/v1",
        "created_at": utc_now(),
        "execution_class": "OFFLINE_SCRIPTED",
        "benchmark_version": BENCHMARK_VERSION,
        "benchmark_sha256": coding_taskset_sha256(),
        "task_count": len(definitions),
        "repeat": max(1, int(repeat)),
        "selected_task_ids": [item["task_id"] for item in definitions],
        "categories": sorted({item["category"] for item in definitions}),
        "runtime_taskset_dependency": GENERAL_TASKSET_VERSION,
        "code": _git_state(),
        "provider": "scripted-reference",
        "model": "offline-scripted-v1",
        "online_model": {
            "status": "NOT_RUN",
            "reason": "W0 measures evaluator correctness; real-model runs need operator authorization.",
        },
    }
    _write_json(output_root / "taskset.json", {
        "version": BENCHMARK_VERSION,
        "sha256": coding_taskset_sha256(),
        "changelog": list(WORK_CODING_CHANGELOG),
        # public definitions only: hidden bytes never enter artifacts.
        "tasks": definitions,
    })
    _write_json(output_root / "run_manifest.json", manifest)
    _write_json(output_root / "summary.json", {
        "manifest": manifest,
        "metrics": metrics,
        "results": rows,
        "all_case_expectations_met": all(row["case_expectation_met"] for row in rows),
        "all_hidden_tests_passed": all(row["hidden_tests_passed"] for row in rows),
    })
    (output_root / "summary.md").write_text(_summary_markdown(manifest, rows, metrics), encoding="utf-8")
    all_met = all(row["case_expectation_met"] for row in rows)
    return {
        "output": str(output_root),
        "results": rows,
        "metrics": metrics,
        "all_case_expectations_met": all_met,
        "all_hidden_tests_passed": all(row["hidden_tests_passed"] for row in rows),
        "exit_code": 0 if all_met else 1,
    }


def _coding_ratio(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    numerator = sum(1 for row in rows if bool(row.get(key)))
    denominator = len(rows)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
        "status": "MEASURED" if denominator else "NOT_MEASURED",
        "task_ids": [row["task_id"] for row in rows],
    }


def _summary_markdown(manifest: dict[str, Any], rows: list[dict[str, Any]],
                      metrics: dict[str, Any]) -> str:
    lines = [
        "# Work coding benchmark summary",
        "",
        f"- Execution class: `{manifest['execution_class']}`",
        f"- Benchmark: `{manifest['benchmark_version']}` ({manifest['task_count']} tasks, "
        f"repeat {manifest['repeat']})",
        f"- Benchmark SHA256: `{manifest['benchmark_sha256']}`",
        f"- Tested commit: `{manifest['code']['commit']}`; dirty: `{manifest['code']['dirty']}`",
        f"- Provider: `{manifest['provider']}` · Model: `{manifest['model']}`",
        "- Online model: **NOT_RUN**",
        "",
        "## Metrics",
        "",
    ]
    for name in ("goal_success_rate", "independent_verifier_pass_rate",
                 "pipeline_completion_rate", "invalid_action_rate"):
        item = metrics[name]
        value = item["value"]
        lines.append(
            f"- `{name}`: {value if value is not None else 'NOT_MEASURED'} "
            f"({item['numerator']}/{item['denominator']})"
        )
    coding = metrics["coding"]
    for name in ("hidden_test_pass_rate", "patch_minimal_rate"):
        item = coding[name]
        value = item["value"]
        lines.append(
            f"- `{name}`: {value if value is not None else 'NOT_MEASURED'} "
            f"({item['numerator']}/{item['denominator']})"
        )
    totals = metrics["totals"]
    lines.append(
        f"- totals: calls {totals['calls']} · tokens {totals['tokens']} · "
        f"cost {totals['cost_microusd']} µUSD"
    )
    lines.append(f"- latency p50/p95: {metrics['latency_ms']['p50']}/"
                 f"{metrics['latency_ms']['p95']} ms")
    badcases = metrics.get("badcases") or []
    lines += ["", "## Badcases", ""]
    if not badcases:
        lines.append("None.")
    for badcase in badcases:
        lines.append(f"- `{badcase['task_id']}`: {'; '.join(badcase['reasons']) or 'no reasons'}")
    lines += ["", "## Per-run results", "",
              "| task | category | status | hidden | forbidden | changed files |",
              "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(
            f"| `{row['task_id']}` | {row['category']} | {row['runtime_status']} | "
            f"{'✅' if row['hidden_tests_passed'] else '❌'} | "
            f"{row['unauthorized_side_effect_count']} | "
            f"{', '.join(row.get('changed_files') or []) or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)

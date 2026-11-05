"""Execute versioned tasks through the production Runtime and export evidence."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..core.contracts import AgentError
from ..core.hashing import canonical_json, digest
from ..core.receipts import ReceiptStore
from ..core.store import TaskStore, utc_now
from ..domains.general import GENERAL_DOMAIN, summarize_state
from ..runtime.budget import BudgetLimits
from ..runtime.harness import Harness
from ..runtime.loop import Loop
from ..runtime.recovery import reconcile_unknown_tool
from ..runtime.replay import equivalent_projection
from ..tools.general.sandbox import WorkspaceSandbox
from ..tools.general.world import GeneralWorld
from ..tools.registry import ToolRegistry
from .metrics import compute_metrics
from .runtime import ApprovalControl, BenchmarkPlanner, ScriptedProvider
from .tasks import TASKSET_VERSION, TASK_CHANGELOG, get_task, taskset, taskset_sha256
from .verifier import verify_task

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = PROJECT_ROOT / "work" / "runtime-benchmark" / "latest"


@dataclass
class Stack:
    store: TaskStore
    world: GeneralWorld
    registry: ToolRegistry
    provider: ScriptedProvider
    harness: Harness
    planner: BenchmarkPlanner
    loop: Loop
    tool_executions: dict[str, int]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or ".git" in path.relative_to(root).parts:
            continue
        body = path.read_bytes()
        result[path.relative_to(root).as_posix()] = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "content": body.decode("utf-8", errors="replace"),
        }
    return result


def _diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    output: list[str] = []
    for path in sorted(set(before) | set(after)):
        old = (before.get(path) or {}).get("content", "")
        new = (after.get(path) or {}).get("content", "")
        if old == new:
            continue
        output.extend(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"initial/{path}", tofile=f"final/{path}",
        ))
    return "".join(output)


class OfflineNetworkGuard:
    """Fail application-level network calls and retain an observable count."""

    def __init__(self) -> None:
        self.attempts = 0
        self._original_connect: Any = None
        self._original_create: Any = None

    def __enter__(self) -> "OfflineNetworkGuard":
        self._original_connect = socket.socket.connect
        self._original_create = socket.create_connection

        def denied(*args: Any, **kwargs: Any) -> None:
            self.attempts += 1
            raise OSError("OFFLINE_SCRIPTED benchmark: network access is disabled")

        socket.socket.connect = denied  # type: ignore[method-assign]
        socket.create_connection = denied  # type: ignore[assignment]
        return self

    def __exit__(self, *args: Any) -> None:
        socket.socket.connect = self._original_connect  # type: ignore[method-assign]
        socket.create_connection = self._original_create  # type: ignore[assignment]


def build_stack(definition: dict[str, Any], run_dir: Path, *, provider_offset: int = 0) -> Stack:
    store = TaskStore(run_dir / "task.sqlite")
    writes = [
        action for action in definition["model_input"]["actions"]
        if action["tool"] == "write_file"
    ]
    report_path = writes[-1]["arguments"]["path"] if writes else "report.md"
    world = GeneralWorld(
        {"mode": "open", "reportPath": report_path},
        sandbox=WorkspaceSandbox(run_dir / "workspace"),
    )
    registry = ToolRegistry(world.build_tools())
    provider = ScriptedProvider(definition, offset=provider_offset)
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=BudgetLimits(**definition["budget"]),
        receipts=ReceiptStore(store),
        verifier=lambda observation: world.verify_observation(observation),
        state_fn=summarize_state,
        instruction=GENERAL_DOMAIN.instruction,
        prompt_profile=GENERAL_DOMAIN.prompt_profile,
        plan_extension=False,
    )
    control = None
    if definition["approval"]["mode"] != "none":
        control = ApprovalControl(harness, definition["approval"]["mode"])
    planner = BenchmarkPlanner(definition)
    loop = Loop(
        harness,
        planner=planner,
        max_rounds=len(definition["model_input"]["actions"]) + 3,
        control=control,
        result_tools=("write_file",),
    )
    stack = Stack(store, world, registry, provider, harness, planner, loop, {})
    original_execute = registry.execute
    fault = definition.get("fault") or {}

    def counted_execute(name: str, arguments: Any) -> Any:
        observation = original_execute(name, arguments)
        stack.tool_executions[name] = stack.tool_executions.get(name, 0) + 1
        if (
            fault.get("kind") == "unknown_after_write"
            and name == "write_file"
            and arguments.get("path") == fault.get("path")
        ):
            raise AgentError(
                "BENCHMARK_UNKNOWN_AFTER_WRITE",
                "controlled fault: write completed but its outcome is unknown",
                details={"outcome": "unknown"},
            )
        return observation

    registry.execute = counted_execute  # type: ignore[method-assign]
    return stack


def _create_task(stack: Stack, definition: dict[str, Any]) -> Any:
    return stack.harness.create(
        task_id=definition["task_id"],
        objective=definition["user_task"],
        principal="runtime-benchmark",
        binding={
            "benchmark_version": TASKSET_VERSION,
            "task_sha256": digest(definition),
            "execution_class": "OFFLINE_SCRIPTED",
        },
        allowed_tools=list(definition["allowed_tools"]),
        grant=definition["permissions"],
    )


def _prepare(definition: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in definition["fixture"]["files"].items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    canary = run_dir / "canary" / "outside.txt"
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_text("UNTOUCHED", encoding="utf-8")
    return _snapshot(workspace)


def _receipts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts = []
    for event in events:
        if event["type"] not in ("tool_started", "tool_succeeded", "tool_failed", "tool_unknown", "recovery_reconciled"):
            continue
        detail = event["payload"].get("detail") or {}
        receipts.append({
            "sequence": event["sequence"],
            "event": event["type"],
            "event_hash": event["hash"],
            "tool": detail.get("tool"),
            "round": detail.get("round"),
            "arguments": detail.get("arguments"),
            "result_hash": detail.get("result_hash"),
            "result": detail.get("result"),
            "recovery": detail if event["type"] == "recovery_reconciled" else None,
        })
    return receipts


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _crash_worker(definition: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "BENCHMARK_RUN_DIR": str(run_dir),
        "BENCHMARK_TASK_ID": definition["task_id"],
    }
    with (run_dir / "worker.stdout.txt").open("w", encoding="utf-8") as stdout, (
        run_dir / "worker.stderr.txt"
    ).open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "flowagent.benchmark.crash_worker"],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        pid = process.pid
        try:
            returncode = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=10)
    return {
        "pid": pid,
        "returncode": returncode,
        "signal": -returncode if returncode < 0 else None,
        "sigkill_confirmed": returncode == -signal.SIGKILL,
        "mode": definition["crash"]["mode"],
        "controlled_fault_injection": True,
        "stdout": "worker.stdout.txt",
        "stderr": "worker.stderr.txt",
    }


def _tamper(store: TaskStore, task_id: str) -> dict[str, Any]:
    row = store._conn.execute("SELECT payload FROM event WHERE task_id=? AND sequence=2", (task_id,)).fetchone()
    payload = json.loads(row["payload"])
    payload["detail"] = {**(payload.get("detail") or {}), "controlled_tamper": True}
    store._conn.execute(
        "UPDATE event SET payload=? WHERE task_id=? AND sequence=2",
        (canonical_json(payload), task_id),
    )
    try:
        store.verify_chain(task_id)
    except AgentError as error:
        match = re.search(r"event (\d+)", error.message)
        return {
            "detected": error.code == "CHAIN_TAMPERED",
            "code": error.code,
            "message": error.message,
            "sequence": int(match.group(1)) if match else None,
            "controlled_fault_injection": True,
        }
    return {"detected": False, "code": None, "sequence": None, "controlled_fault_injection": True}


def _git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT.parent, capture_output=True, text=True, check=True,
        ).stdout.strip()

    dirty_lines = run("status", "--porcelain=v1").splitlines()
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(dirty_lines),
        "dirty_files": dirty_lines,
    }


def run_task(definition: dict[str, Any], output_root: Path) -> dict[str, Any]:
    run_dir = output_root / "tasks" / definition["task_id"]
    initial = _prepare(definition, run_dir)
    stack = build_stack(definition, run_dir)
    task = _create_task(stack, definition)
    process: dict[str, Any] = {}
    recovery: dict[str, Any] = {}
    tamper: dict[str, Any] = {}
    worker_counts: dict[str, int] = {}
    stdout = io.StringIO()
    stderr = io.StringIO()
    guard = OfflineNetworkGuard()
    try:
        with guard, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if definition.get("crash"):
                stack.store.close()
                process = _crash_worker(definition, run_dir)
                worker_path = run_dir / "worker-executions.json"
                if worker_path.exists():
                    worker_counts = json.loads(worker_path.read_text(encoding="utf-8"))
                resumed = build_stack(definition, run_dir, provider_offset=1)
                stack = resumed
                task = stack.store.load(task.id)
                mode = definition["crash"]["mode"]
                if mode == "after_commit":
                    memory = stack.loop.restore(task)
                    stack.world.apply_state(memory.state())
                    recovery = {
                        "recovered": process["sigkill_confirmed"],
                        "unknown_outcome": False,
                        "side_effect": "observed",
                        "retry_dispatched": False,
                        "resumed_from_committed_event": True,
                    }
                    task = stack.loop.run(task.id)
                    recovery["terminal_status"] = task.status
                else:
                    target = definition["model_input"]["actions"][0]["arguments"]["path"]
                    observed = (run_dir / "workspace" / target).exists()
                    task, recovery = reconcile_unknown_tool(
                        stack.harness,
                        task.id,
                        evidence={
                            "side_effect": "observed" if observed else "not_observed",
                            "workspace": _snapshot(run_dir / "workspace"),
                            "process": process,
                        },
                    )
                    recovery["process"] = process
            else:
                task = stack.loop.run(task.id)
                if definition.get("fault", {}).get("kind") == "unknown_after_write":
                    target = definition["fault"]["path"]
                    observed = (run_dir / "workspace" / target).exists()
                    task, recovery = reconcile_unknown_tool(
                        stack.harness,
                        task.id,
                        evidence={
                            "side_effect": "observed" if observed else "not_observed",
                            "workspace": _snapshot(run_dir / "workspace"),
                        },
                    )

            events = stack.store.events(task.id)
            stack.store.verify_chain(task.id)
            _, replay = equivalent_projection(task, events)
            if definition["category"] == "audit_integrity":
                tamper = _tamper(stack.store, task.id)
    except Exception:
        (run_dir / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
        (run_dir / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
        raise
    (run_dir / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
    final = _snapshot(run_dir / "workspace")
    _write_json(run_dir / "initial-files.json", initial)
    _write_json(run_dir / "final-files.json", final)
    (run_dir / "workspace.diff").write_text(_diff(initial, final), encoding="utf-8")
    _write_json(run_dir / "events.json", events)
    _write_json(run_dir / "receipts.json", _receipts(events))
    _write_json(run_dir / "replay.json", replay)
    _write_json(run_dir / "recovery.json", recovery)
    _write_json(run_dir / "process.json", process)
    _write_json(run_dir / "tamper.json", tamper)
    _write_json(run_dir / "provider.json", {
        "type": "OFFLINE_SCRIPTED",
        "provider": stack.provider.name,
        "model": stack.provider.model,
        "provider_calls_this_process": stack.provider.calls,
        "network_access": "disabled",
    })
    _write_json(run_dir / "policy.json", {
        "allowed_tools": definition["allowed_tools"],
        "permissions": definition["permissions"],
        "approval": definition["approval"],
        "budget": definition["budget"],
        "timeout_seconds": definition["timeout_seconds"],
    })
    _write_json(run_dir / "budget.json", stack.harness.budget(task).snapshot())
    executions = dict(worker_counts)
    _merge_counts(executions, stack.tool_executions)
    _write_json(run_dir / "tool-executions.json", executions)
    verdict = verify_task(
        definition,
        task=task,
        events=events,
        run_dir=run_dir,
        provider_calls=stack.provider.calls,
        tool_executions=executions,
        network_attempts=guard.attempts,
        recovery=recovery,
        replay=replay,
        tamper=tamper,
    )
    _write_json(run_dir / "verifier.json", verdict)
    _write_json(run_dir / "task.json", {
        "definition": definition,
        "task_sha256": digest(definition),
        "taskset_sha256": taskset_sha256(),
    })
    result = {
        "task_id": definition["task_id"],
        "category": definition["category"],
        "runtime_status": task.status,
        "failure_code": (task.failure or {}).get("code"),
        "pipeline_completed": verdict["pipeline_completed"],
        "verifier_passed": verdict["passed"],
        "expected_verifier_passed": definition["expected_verifier_passed"],
        "case_expectation_met": verdict["case_expectation_met"],
        "policy_violation_blocked": bool(verdict["policy_violation_blocked"]),
        "recovered": verdict["recovered"],
        "recovery": recovery,
        "replay_equivalent": bool(replay.get("equivalent")),
        "unauthorized_side_effect_count": verdict["forbidden_side_effects"]["unauthorized_count"],
        "duplicate_side_effect_count": verdict["forbidden_side_effects"]["duplicate_count"],
        "tamper": tamper,
        "expected_final_state": definition["expected_final_state"],
        "artifacts": {
            "task": f"tasks/{definition['task_id']}/task.json",
            "events": f"tasks/{definition['task_id']}/events.json",
            "sqlite": f"tasks/{definition['task_id']}/task.sqlite",
            "diff": f"tasks/{definition['task_id']}/workspace.diff",
            "verifier": f"tasks/{definition['task_id']}/verifier.json",
        },
    }
    _write_json(run_dir / "result.json", result)
    stack.store.close()
    return result


def _summary_markdown(manifest: dict[str, Any], results: list[dict[str, Any]], metrics: dict[str, Any]) -> str:
    lines = [
        "# Runtime benchmark summary",
        "",
        f"- Execution class: `OFFLINE_SCRIPTED`",
        f"- Task set: `{TASKSET_VERSION}`",
        f"- Task-set SHA256: `{manifest['taskset_sha256']}`",
        f"- Tested commit: `{manifest['code']['commit']}`; dirty before generation: `{manifest['code']['dirty']}`",
        "- Online model: `NOT_RUN` — no paid online evaluation was authorized; no online rate, token, cost, latency or badcase metric is reported.",
        "- These are real tool and governance runs with scripted proposals, not real-model task effectiveness.",
        "",
        "| Task | Runtime | Pipeline | Verifier | Expected verdict | Recovered |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in results:
        lines.append(
            f"| `{row['task_id']}` | {row['runtime_status']} | {row['pipeline_completed']} | "
            f"{row['verifier_passed']} | {row['case_expectation_met']} | {row['recovered']} |"
        )
    lines.extend(["", "## Metrics", "", "| Metric | Numerator | Denominator | Value |", "| --- | ---: | ---: | ---: |"])
    for name, value in metrics.items():
        if isinstance(value, dict) and "numerator" in value:
            ratio = "NOT_MEASURED" if value["value"] is None else f"{value['value']:.4f}"
            lines.append(f"| `{name}` | {value['numerator']} | {value['denominator']} | {ratio} |")
    lines.extend([
        "",
        f"Unauthorized side effects: **{metrics['unauthorized_side_effect_count']['count']}**.",
        f"Duplicate side effects: **{metrics['duplicate_side_effect_count']['count']}**.",
        "",
        "The intentional `model-success-verifier-reject` task contributes a verifier failure. Its case expectation passes only because that rejection was required in advance; it is not removed from the verifier pass-rate denominator.",
        "",
    ])
    return "\n".join(lines)


def run_benchmark(output: str | Path | None = None, *, task_ids: list[str] | None = None) -> dict[str, Any]:
    output_root = Path(output) if output else DEFAULT_OUTPUT
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    definitions = taskset()
    if task_ids:
        definitions = [get_task(task_id) for task_id in task_ids]
    code = _git_state()
    results = [run_task(definition, output_root) for definition in definitions]
    metrics = compute_metrics(results)
    manifest = {
        "manifest_version": "flowagent.runtime-benchmark-manifest/v1",
        "created_at": utc_now(),
        "execution_class": "OFFLINE_SCRIPTED",
        "taskset_version": TASKSET_VERSION,
        "taskset_sha256": taskset_sha256(),
        "task_count": len(definitions),
        "selected_task_ids": [item["task_id"] for item in definitions],
        "code": code,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "network": "application-level calls disabled; no external API required",
        },
        "provider": {"type": "OFFLINE_SCRIPTED", "model": "offline-scripted-v1"},
        "online_model": {
            "status": "NOT_RUN",
            "reason": "No explicit authorization for a paid online evaluation was provided.",
            "metrics": "NOT_MEASURED",
            "badcases": "NOT_MEASURED",
        },
        "recovery_boundary": "Application-level workspace/event reconciliation only; not arbitrary external-system rollback or an OS sandbox.",
    }
    _write_json(output_root / "taskset.json", {
        "version": TASKSET_VERSION,
        "sha256": taskset_sha256(),
        "changelog": list(TASK_CHANGELOG),
        "tasks": definitions,
    })
    _write_json(output_root / "run_manifest.json", manifest)
    _write_json(output_root / "summary.json", {
        "manifest": manifest,
        "metrics": metrics,
        "results": results,
        "all_case_expectations_met": all(row["case_expectation_met"] for row in results),
    })
    (output_root / "summary.md").write_text(_summary_markdown(manifest, results, metrics), encoding="utf-8")
    return {
        "output": str(output_root),
        "results": results,
        "metrics": metrics,
        "all_case_expectations_met": all(row["case_expectation_met"] for row in results),
        "exit_code": 0 if all(row["case_expectation_met"] for row in results) else 1,
    }

"""Run the general-agent task set through the production runtime.

Two execution classes share one path:

* ``offline`` — the deterministic ``ScriptedProvider`` replays each task's
  reference proposals through the real ``Loop``/``Harness``/gates. This
  validates the evaluator itself; it says nothing about model ability.
* ``online`` — a real provider is selected explicitly (never silently) and the
  same production loop runs with the domain planner. Missing key, model name,
  price snapshot or paid confirmation fails closed before any call.

Local ``http`` fixtures are served on 127.0.0.1 by a thread-bound static
server; the offline network guard blocks every other destination. Recovery
scenarios reuse the benchmark's real-SIGKILL worker and reconciliation flow —
the runtime machinery is shared, not duplicated.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import http.server
import io
import json
import platform
import re
import shutil
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from ..benchmark.runtime import BenchmarkPlanner, ScriptedProvider
from ..core.contracts import AgentError
from ..core.hashing import digest
from ..core.receipts import ReceiptStore
from ..core.store import TaskStore, utc_now
from ..demo.fixtures import get_scenario
from ..demo.provider_factory import build_provider
from ..domains.general import GENERAL_DOMAIN, summarize_state
from ..runtime.budget import BudgetLimits
from ..runtime.harness import Harness
from ..runtime.recovery import reconcile_unknown_tool
from ..runtime.replay import equivalent_projection
from ..tools.general.fetch import UrlFetcher
from ..tools.general.sandbox import WorkspaceSandbox
from ..tools.general.world import GeneralWorld
from ..tools.registry import ToolRegistry
from .general_tasks import (
    GENERAL_TASK_CHANGELOG,
    TASKSET_VERSION,
    general_taskset,
    general_taskset_sha256,
    get_general_task,
)
from .metrics import compute_general_metrics
from .prompt_eval import provider_env
from .verifier import verify_general_task

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = PROJECT_ROOT / "work" / "general-eval" / "latest"

_REQUIRED_CONFIRM_PHRASE = "authorize paid online evaluation"

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)(\"?[A-Za-z0-9_-]{6,}\"?)"),
)


class GeneralStack:
    """Attribute-compatible with the benchmark stack for the shared SIGKILL worker."""

    def __init__(
        self,
        store: TaskStore,
        world: GeneralWorld,
        registry: ToolRegistry,
        provider: Any,
        harness: Harness,
        planner: Any,
        loop: Any,
    ) -> None:
        self.store = store
        self.world = world
        self.registry = registry
        self.provider = provider
        self.harness = harness
        self.planner = planner
        self.loop = loop
        self.tool_executions: dict[str, int] = {}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


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
    """Fail application-level network calls except explicitly allowed loopback fixtures."""

    def __init__(self, allow: tuple[tuple[str, int], ...] = ()) -> None:
        self.allow = tuple(allow)
        self.attempts = 0
        self._original_connect: Any = None
        self._original_create: Any = None

    def __enter__(self) -> "OfflineNetworkGuard":
        self._original_connect = socket.socket.connect
        self._original_create = socket.create_connection

        def denied_connect(sock: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._allowed(address):
                self.attempts += 1
                raise OSError("OFFLINE evaluation: network access is disabled")
            return self._original_connect(sock, address, *args, **kwargs)

        def denied_create(address: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._allowed(address):
                self.attempts += 1
                raise OSError("OFFLINE evaluation: network access is disabled")
            return self._original_create(address, *args, **kwargs)

        socket.socket.connect = denied_connect  # type: ignore[method-assign]
        socket.create_connection = denied_create  # type: ignore[assignment]
        return self

    def _allowed(self, address: Any) -> bool:
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        host, port = address[0], address[1]
        return any(host == allowed_host and port == allowed_port
                   for allowed_host, allowed_port in self.allow)

    def __exit__(self, *args: Any) -> None:
        socket.socket.connect = self._original_connect  # type: ignore[method-assign]
        socket.create_connection = self._original_create  # type: ignore[assignment]


class FixtureServer:
    """Static HTTP fixture bound to 127.0.0.1 on an ephemeral port."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FixtureServer":
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                body = fixture._files.get(self.path.lstrip("/"))
                if body is None:
                    self.send_error(404)
                    return
                payload = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def _resolve_base_urls(definition: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Substitute the local fixture base URL into the objective and reference actions."""
    resolved = json.loads(json.dumps(definition))
    resolved["user_task"] = resolved["user_task"].replace("{fixture}", base_url)
    for action in resolved["model_input"]["actions"]:
        arguments = action.get("arguments") or {}
        for key, value in arguments.items():
            if isinstance(value, str):
                arguments[key] = value.replace("{fixture}", base_url)
    return resolved


def _report_path(definition: dict[str, Any]) -> str:
    writes = [
        action for action in definition["model_input"]["actions"]
        if action["tool"] == "write_file"
    ]
    return writes[-1]["arguments"]["path"] if writes else "report.md"


def build_general_stack(
    definition: dict[str, Any],
    run_dir: Path,
    *,
    provider: Any | None = None,
    planner: Any | None = None,
    use_domain_planner: bool = False,
    store: TaskStore | None = None,
    receipts: ReceiptStore | None = None,
    max_rounds: int | None = None,
    plan_extension: bool = False,
    verification: bool = False,
) -> GeneralStack:
    """Assemble the production runtime for one general task.

    Provider/planner default to the offline scripted pair. The signature keeps
    ``definition`` first so the shared benchmark SIGKILL worker can drive it.
    """
    from ..runtime.engine import build_loop

    store = store if store is not None else TaskStore(run_dir / "task.sqlite")
    receipts = receipts if receipts is not None else ReceiptStore(store)
    if provider is None:
        provider = ScriptedProvider(definition)
    world = GeneralWorld(
        {"mode": "open", "reportPath": _report_path(definition)},
        sandbox=WorkspaceSandbox(run_dir / "workspace"),
        fetcher=UrlFetcher(allow_hosts=("127.0.0.1",)),
    )
    if use_domain_planner or planner is None:
        planner = GENERAL_DOMAIN.make_planner(world) if use_domain_planner else BenchmarkPlanner(definition)
    registry = ToolRegistry(world.build_tools())
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=BudgetLimits(**definition["budget"]),
        receipts=receipts,
        verifier=lambda observation: world.verify_observation(observation),
        state_fn=summarize_state,
        instruction=GENERAL_DOMAIN.instruction,
        prompt_profile=GENERAL_DOMAIN.prompt_profile,
        plan_extension=plan_extension,
        verification=GENERAL_DOMAIN.make_verification(world) if verification else None,
    )
    rounds = max_rounds if max_rounds is not None else len(definition["model_input"]["actions"]) + 3
    loop = build_loop(harness, planner=planner, max_rounds=rounds,
                      result_tools=("write_file",))
    stack = GeneralStack(store, world, registry, provider, harness, planner, loop)
    fault = definition.get("fault") or {}

    original_execute = registry.execute

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


def _receipt_records(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    receipts = []
    for event in events:
        if event["type"] not in ("tool_started", "tool_succeeded", "tool_failed",
                                 "tool_unknown", "recovery_reconciled"):
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
        })
    return receipts


def _wall_ms(events: list[dict[str, Any]]) -> int:
    stamps = [event["at"] for event in events if event.get("at")]
    if len(stamps) < 2:
        return 0
    try:
        first = datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
        last = datetime.fromisoformat(stamps[-1].replace("Z", "+00:00"))
        return max(0, int((last - first).total_seconds() * 1000))
    except ValueError:
        return 0


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


def _git_state() -> dict[str, Any]:
    import subprocess

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT.parent, capture_output=True, text=True, check=True,
        ).stdout.strip()

    dirty_lines = run("status", "--porcelain=v1").splitlines()
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(dirty_lines),
    }


def _price_snapshot(env: dict[str, str], provider_name: str) -> dict[str, Any]:
    prefix = provider_name.upper()
    input_price = env.get(f"{prefix}_INPUT_PRICE_MICROUSD_PER_MTOK", "").strip()
    output_price = env.get(f"{prefix}_OUTPUT_PRICE_MICROUSD_PER_MTOK", "").strip()
    as_of = env.get(f"{prefix}_PRICE_AS_OF", "").strip()
    source = env.get(f"{prefix}_PRICE_SOURCE", "").strip()
    return {
        "input_microusd_per_mtok": int(input_price) if input_price.isdigit() else None,
        "output_microusd_per_mtok": int(output_price) if output_price.isdigit() else None,
        "as_of": as_of or None,
        "source": source or None,
    }


def redact_text(text: str, secrets: list[str] | None = None) -> str:
    """Mask credentials before any text reaches a report."""
    value = str(text or "")
    for secret in secrets or []:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = _SECRET_PATTERNS[0].sub("[REDACTED-KEY]", value)
    value = _SECRET_PATTERNS[1].sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    return value


def check_online_gate(
    *,
    provider_name: str,
    model: str | None,
    env: dict[str, str] | None,
    confirm_paid: str | None,
) -> dict[str, Any]:
    """Fail closed before any paid call; returns ok + price snapshot or a NOT_RUN reason."""
    if provider_name == "mock":
        return {"ok": False, "reason": "the mock provider is not a real model; online refused"}
    if not confirm_paid or _REQUIRED_CONFIRM_PHRASE not in confirm_paid.lower():
        return {
            "ok": False,
            "reason": ("--online requires --confirm-paid containing "
                       f"\"{_REQUIRED_CONFIRM_PHRASE}\""),
        }
    resolved = dict(env or {})
    prefix = provider_name.upper()
    missing = []
    if provider_name != "local":
        if not (resolved.get(f"{prefix}_API_KEY") or "").strip():
            missing.append(f"{prefix}_API_KEY")
        if not ((model or "") or resolved.get(f"{prefix}_MODEL", "")).strip():
            missing.append(f"{prefix}_MODEL")
        snapshot = _price_snapshot(resolved, provider_name)
        for key in ("input_microusd_per_mtok", "output_microusd_per_mtok", "as_of"):
            if snapshot.get(key) in (None, ""):
                missing.append(f"{prefix} price {key}")
    if missing:
        return {"ok": False,
                "reason": "online evaluation is fail-closed; missing: " + ", ".join(missing)}
    return {"ok": True, "price_snapshot": _price_snapshot(resolved, provider_name)}


def run_general_task(
    definition: dict[str, Any],
    output_root: Path,
    *,
    mode: str = "offline",
    provider_name: str = "mock",
    model: str | None = None,
    env: dict[str, str] | None = None,
    repeat: int = 0,
) -> dict[str, Any]:
    """Run one task in the requested execution class and export its evidence."""
    if mode not in ("offline", "online"):
        raise ValueError(f"unknown execution mode {mode!r}")
    run_dir = output_root / "tasks" / definition["task_id"] / (
        f"repeat-{repeat}" if repeat else "run-0"
    )
    definition_run = definition
    fixture_server: FixtureServer | None = None
    if definition.get("http"):
        fixture_server = FixtureServer(dict(definition["http"].get("files") or {}))
        fixture_server.__enter__()
        definition_run = _resolve_base_urls(definition, fixture_server.base_url)
    allow = (("127.0.0.1", fixture_server.port),) if fixture_server else ()
    guard = OfflineNetworkGuard(allow) if mode == "offline" else None
    try:
        initial = _prepare(definition_run, run_dir)
        store = TaskStore(run_dir / "task.sqlite")
        receipts = ReceiptStore(store)
        scenario = get_scenario("general-research")
        if mode == "offline":
            provider: Any = ScriptedProvider(definition_run)
            max_rounds = len(definition_run["model_input"]["actions"]) + 3
        else:
            provider = build_provider(
                provider_name, scenario=scenario, receipts=receipts,
                model=model, env=dict(env or {}),
            )
            max_rounds = definition["max_rounds"]
        return _execute(
            definition_run, definition, run_dir, initial,
            mode=mode, provider_name=provider_name, model=model, env=env,
            repeat=repeat, provider=provider, store=store, receipts=receipts,
            max_rounds=max_rounds, guard=guard,
        )
    except Exception as error:
        row = {
            "task_id": definition["task_id"],
            "category": definition["category"],
            "repeat": repeat,
            "mode": mode,
            "provider": provider_name if mode == "online" else "offline-scripted",
            "model": model or ("offline-scripted-v1" if mode == "offline" else ""),
            "runtime_status": "failed",
            "failure_code": "RUNTIME_EXCEPTION",
            "goal_success": False,
            "verifier_passed": False,
            "expected_verifier_passed": bool(definition.get("expected_verifier_passed", True)),
            "case_expectation_met": False,
            "pipeline_completed": False,
            "invalid_action_count": 0,
            "unauthorized_side_effect_count": 0,
            "duplicate_side_effect_count": 0,
            "human_intervention_count": 0,
            "recovery_attempted": bool(definition.get("crash") or definition.get("fault")),
            "recovered": False,
            "calls": 0, "reads": 0, "tokens": 0, "cost_microusd": 0,
            "wall_ms": 0,
            "badcase_reasons": [f"runner exception: {type(error).__name__}: {error}"[:400]],
            "task_sha256": digest(definition),
            "artifacts": {"dir": f"tasks/{definition['task_id']}"},
        }
        _write_json(run_dir / "result.json", row)
        return row
    finally:
        if fixture_server is not None:
            fixture_server.__exit__()


def _execute(
    definition_run: dict[str, Any],
    definition: dict[str, Any],
    run_dir: Path,
    initial: dict[str, Any],
    *,
    mode: str,
    provider_name: str,
    model: str | None,
    env: dict[str, str] | None,
    repeat: int,
    provider: Any,
    store: TaskStore,
    receipts: ReceiptStore,
    max_rounds: int,
    guard: OfflineNetworkGuard | None,
) -> dict[str, Any]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    online = mode == "online"
    stack = build_general_stack(
        definition_run, run_dir,
        provider=provider,
        use_domain_planner=online,
        store=store, receipts=receipts,
        max_rounds=max_rounds,
        plan_extension=GENERAL_DOMAIN.plan_extension if online else False,
        verification=online,
    )
    harness = stack.harness
    binding = {
        "benchmark_version": TASKSET_VERSION,
        "task_sha256": digest(definition),
        "execution_class": "OFFLINE_SCRIPTED" if not online else "ONLINE_MODEL",
        "repeat": repeat,
    }
    created = harness.create(
        task_id=definition["task_id"] + (f"-r{repeat}" if repeat else ""),
        objective=definition_run["user_task"],
        principal="general-eval",
        binding=binding,
        allowed_tools=list(definition_run["allowed_tools"]),
        grant=definition_run["permissions"],
    )
    task: Any = created
    recovery: dict[str, Any] = {}
    worker_counts: dict[str, int] = {}
    process: dict[str, Any] = {}
    guard_ctx = guard if guard is not None else contextlib.nullcontext()
    events: list[dict[str, Any]] = []
    replay: dict[str, Any] = {}
    try:
        with guard_ctx, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if definition_run.get("crash"):
                store.close()
                from ..benchmark.runner import spawn_crash_worker

                process = spawn_crash_worker(definition_run, run_dir)
                worker_path = run_dir / "worker-executions.json"
                if worker_path.exists():
                    worker_counts = json.loads(worker_path.read_text(encoding="utf-8"))
                stack = build_general_stack(
                    definition_run, run_dir,
                    provider=ScriptedProvider(definition_run, offset=1),
                    max_rounds=max_rounds,
                )
                store = stack.store
                task = store.load(created.id)
                if definition_run["crash"]["mode"] == "after_commit":
                    memory = stack.loop.restore(task)
                    stack.world.apply_state(memory.state())
                    recovery = {
                        "recovered": process["sigkill_confirmed"],
                        "side_effect": "observed",
                        "retry_dispatched": False,
                        "resumed_from_committed_event": True,
                    }
                    task = stack.loop.run(task.id)
                    recovery["terminal_status"] = task.status
                else:
                    target = definition_run["model_input"]["actions"][0]["arguments"]["path"]
                    observed = (run_dir / "workspace" / target).exists()
                    task, recovery = reconcile_unknown_tool(
                        stack.harness, task.id,
                        evidence={
                            "side_effect": "observed" if observed else "not_observed",
                            "workspace": _snapshot(run_dir / "workspace"),
                            "process": process,
                        },
                    )
                    recovery["process"] = process
            else:
                task = stack.loop.run(created.id)
                fault = definition_run.get("fault") or {}
                if fault.get("kind") == "unknown_after_write":
                    target = fault["path"]
                    observed = (run_dir / "workspace" / target).exists()
                    task, recovery = reconcile_unknown_tool(
                        harness, created.id,
                        evidence={
                            "side_effect": "observed" if observed else "not_observed",
                            "workspace": _snapshot(run_dir / "workspace"),
                        },
                    )
            events = store.events(created.id)
            store.verify_chain(created.id)
            _, replay = equivalent_projection(task, events)
    except Exception as error:
        (run_dir / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
        (run_dir / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
        (run_dir / "error.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        try:
            events = store.events(created.id)
        except Exception:  # noqa: BLE001 — a dead chain must not hide the original error
            events = []
        verdict = {
            "task_id": definition["task_id"],
            "passed": False,
            "expected_passed": bool(definition.get("expected_verifier_passed", True)),
            "case_expectation_met": False,
            "checks": [],
            "violations": [],
            "failure_reasons": [f"runtime exception: {type(error).__name__}: {error}"[:400]],
            "forbidden_side_effects": {
                "unauthorized_count": 1,
                "duplicate_count": 0,
                "outside_canary": None,
                "network_attempts": 0,
            },
            "pipeline_completed": False,
            "exit_code": 1,
            "verifier": "independent-general-task-v1",
            "exception": f"{type(error).__name__}: {error}"[:400],
        }
        _write_json(run_dir / "verifier.json", verdict)
        row = _row(definition, mode, provider_name, model, repeat, "failed",
                   "RUNTIME_EXCEPTION", verdict, events, stack.tool_executions,
                   dict(worker_counts), recovery, None)
        _write_json(run_dir / "result.json", row)
        store.close()
        return row
    (run_dir / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
    final = _snapshot(run_dir / "workspace")
    _write_json(run_dir / "initial-files.json", initial)
    _write_json(run_dir / "final-files.json", final)
    (run_dir / "workspace.diff").write_text(_diff(initial, final), encoding="utf-8")
    _write_json(run_dir / "events.json", events)
    _write_json(run_dir / "receipts.json", _receipt_records(events))
    _write_json(run_dir / "replay.json", replay)
    _write_json(run_dir / "recovery.json", recovery)
    _write_json(run_dir / "process.json", process)
    budget_snapshot = stack.harness.budget(task).snapshot()
    _write_json(run_dir / "budget.json", budget_snapshot)
    executions = dict(worker_counts)
    for tool, count in stack.tool_executions.items():
        executions[tool] = executions.get(tool, 0) + count
    _write_json(run_dir / "tool-executions.json", executions)
    provider_record: dict[str, Any] = {
        "type": "OFFLINE_SCRIPTED" if not online else "ONLINE_MODEL",
        "provider": getattr(provider, "name", provider_name),
        "model": getattr(provider, "model", model or ""),
        "provider_calls_this_process": getattr(provider, "calls", None),
    }
    if online and env is not None:
        provider_record["price_snapshot"] = _price_snapshot(env, provider_name)
    _write_json(run_dir / "provider.json", provider_record)
    _write_json(run_dir / "policy.json", {
        "allowed_tools": definition_run["allowed_tools"],
        "permissions": definition_run["permissions"],
        "approval": definition_run["approval"],
        "budget": definition_run["budget"],
        "timeout_seconds": definition_run["timeout_seconds"],
        "max_rounds": definition_run["max_rounds"],
    })
    verdict = verify_general_task(
        definition,
        workspace=run_dir / "workspace",
        run_root=run_dir,
        task_status=task.status,
        task_failure_code=(task.failure or {}).get("code") if task.failure else None,
        events=events,
        tool_executions=executions,
        network_attempts=guard.attempts if guard is not None else 0,
        recovery=recovery,
    )
    _write_json(run_dir / "verifier.json", verdict)
    _write_json(run_dir / "task.json", {
        "definition": definition,
        "task_sha256": digest(definition),
        "taskset_sha256": general_taskset_sha256(),
    })
    row = _row(definition, mode, provider_name, model, repeat, task.status,
               (task.failure or {}).get("code") if task.failure else None,
               verdict, events, stack.tool_executions, dict(worker_counts),
               recovery, budget_snapshot)
    row["wall_ms"] = _wall_ms(events)
    _write_json(run_dir / "result.json", row)
    store.close()
    return row


def _row(
    definition: dict[str, Any],
    mode: str,
    provider_name: str,
    model: str | None,
    repeat: int,
    status: str,
    failure_code: str | None,
    verdict: dict[str, Any],
    events: list[dict[str, Any]],
    stack_counts: dict[str, int],
    worker_counts: dict[str, int],
    recovery: dict[str, Any],
    budget_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    executions = dict(worker_counts)
    for tool, count in stack_counts.items():
        executions[tool] = executions.get(tool, 0) + count
    intervention = sum(1 for event in events if event["type"] == "approval_requested")
    denials = sum(
        1 for event in events if event["type"] in ("tool_denied", "proposal_rejected")
    )
    snapshot = budget_snapshot or {}
    tokens = int(snapshot.get("tokens_used") or 0)
    cost = int(snapshot.get("cost_used_microusd") or 0)
    goal_success = bool(
        verdict["case_expectation_met"]
        and verdict["forbidden_side_effects"]["unauthorized_count"] == 0
        and verdict["forbidden_side_effects"]["duplicate_count"] == 0
        and verdict["pipeline_completed"]
    )
    run_dir_name = f"repeat-{repeat}" if repeat else "run-0"
    return {
        "task_id": definition["task_id"],
        "category": definition["category"],
        "repeat": repeat,
        "mode": mode,
        "provider": provider_name if mode == "online" else "offline-scripted",
        "model": model or ("offline-scripted-v1" if mode == "offline" else ""),
        "runtime_status": status,
        "failure_code": failure_code,
        "goal_success": goal_success,
        "verifier_passed": bool(verdict["passed"]),
        "expected_verifier_passed": bool(definition.get("expected_verifier_passed", True)),
        "case_expectation_met": bool(verdict["case_expectation_met"]),
        "pipeline_completed": bool(verdict["pipeline_completed"]),
        "invalid_action_count": denials + len(verdict.get("violations") or []),
        "unauthorized_side_effect_count": verdict["forbidden_side_effects"]["unauthorized_count"],
        "duplicate_side_effect_count": verdict["forbidden_side_effects"]["duplicate_count"],
        "human_intervention_count": intervention,
        "recovery_attempted": bool(definition.get("crash") or definition.get("fault")),
        "recovered": bool(recovery.get("recovered")),
        "calls": int(snapshot.get("calls_used") or 0),
        "reads": int(snapshot.get("reads_used") or 0),
        "tokens": tokens,
        "cost_microusd": cost,
        "wall_ms": 0,
        "badcase_reasons": list(verdict.get("failure_reasons") or []),
        "task_sha256": digest(definition),
        "artifacts": {"dir": f"tasks/{definition['task_id']}/{run_dir_name}"},
    }


def run_general_suite(
    output: str | Path | None = None,
    *,
    task_ids: list[str] | None = None,
    repeat: int = 1,
    mode: str = "offline",
    provider_name: str = "mock",
    model: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the versioned general-agent task set and emit summary.json/.md."""
    output_root = Path(output) if output else DEFAULT_OUTPUT
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    definitions = general_taskset()
    if task_ids:
        definitions = [get_general_task(task_id) for task_id in task_ids]
    code = _git_state()
    rows: list[dict[str, Any]] = []
    for definition in definitions:
        for index in range(max(1, int(repeat))):
            rows.append(run_general_task(
                definition, output_root,
                mode=mode, provider_name=provider_name, model=model,
                env=env, repeat=index if int(repeat) > 1 else 0,
            ))
    metrics = compute_general_metrics(rows)
    price_snapshot = _price_snapshot(dict(env or {}), provider_name) if env else {}
    manifest = {
        "manifest_version": "flowagent.general-eval-manifest/v1",
        "created_at": utc_now(),
        "execution_class": "OFFLINE_SCRIPTED" if mode == "offline" else "ONLINE_MODEL",
        "taskset_version": TASKSET_VERSION,
        "taskset_sha256": general_taskset_sha256(),
        "task_count": len(definitions),
        "repeat": max(1, int(repeat)),
        "selected_task_ids": [item["task_id"] for item in definitions],
        "categories": sorted({item["category"] for item in definitions}),
        "code": code,
        "provider": provider_name if mode == "online" else "offline-scripted",
        "model": model or ("offline-scripted-v1" if mode == "offline" else ""),
        "price_snapshot": price_snapshot,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "network": ("application-level calls limited to local http fixtures"
                        if mode == "offline" else "real provider enabled by operator"),
        },
        "online_model": {
            "status": "RUN" if mode == "online" else "NOT_RUN",
            "reason": ("" if mode == "online"
                       else "No explicit authorization for a paid online evaluation was provided."),
        },
        "recovery_boundary": ("Application-level workspace/event reconciliation only; "
                              "not arbitrary external-system rollback or an OS sandbox."),
    }
    _write_json(output_root / "taskset.json", {
        "version": TASKSET_VERSION,
        "sha256": general_taskset_sha256(),
        "changelog": list(GENERAL_TASK_CHANGELOG),
        "tasks": definitions,
    })
    _write_json(output_root / "run_manifest.json", manifest)
    _write_json(output_root / "summary.json", {
        "manifest": manifest,
        "metrics": metrics,
        "results": rows,
        "all_case_expectations_met": all(row["case_expectation_met"] for row in rows),
    })
    (output_root / "summary.md").write_text(
        _summary_markdown(manifest, rows, metrics), encoding="utf-8")
    return {
        "output": str(output_root),
        "results": rows,
        "metrics": metrics,
        "all_case_expectations_met": all(row["case_expectation_met"] for row in rows),
        "exit_code": 0 if all(row["case_expectation_met"] for row in rows) else 1,
    }


def _summary_markdown(manifest: dict[str, Any], rows: list[dict[str, Any]],
                      metrics: dict[str, Any]) -> str:
    online = manifest["execution_class"] == "ONLINE_MODEL"
    lines = [
        "# General-agent evaluation summary",
        "",
        f"- Execution class: `{manifest['execution_class']}`",
        f"- Task set: `{manifest['taskset_version']}` ({manifest['task_count']} tasks, "
        f"repeat {manifest['repeat']})",
        f"- Task-set SHA256: `{manifest['taskset_sha256']}`",
        f"- Tested commit: `{manifest['code']['commit']}`; dirty before generation: "
        f"`{manifest['code']['dirty']}`",
        f"- Provider: `{manifest['provider']}` · Model: `{manifest['model']}`",
    ]
    if online:
        price = manifest.get("price_snapshot") or {}
        lines.append(
            f"- Price snapshot: input {price.get('input_microusd_per_mtok')} / "
            f"output {price.get('output_microusd_per_mtok')} µUSD/MTok "
            f"as-of {price.get('as_of')} ({price.get('source') or 'unspecified source'})"
        )
    else:
        lines.extend([
            "- `ONLINE_MODEL=NOT_RUN` — no paid online evaluation was authorized.",
            "- 真实模型通过率 = `NOT_MEASURED`；离线参考提议只能证明评测器自身。",
        ])
    lines.extend([
        "",
        "| Task | Category | Repeat | Runtime | Goal | Verifier | Expectation met |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ])
    for row in rows:
        lines.append(
            f"| `{row['task_id']}` | {row['category']} | {row['repeat']} | "
            f"{row['runtime_status']} | {row['goal_success']} | {row['verifier_passed']} | "
            f"{row['case_expectation_met']} |"
        )
    lines.extend(["", "## Metrics", "", "| Metric | Numerator | Denominator | Value |",
                  "| --- | ---: | ---: | ---: |"])
    for name in ("goal_success_rate", "independent_verifier_pass_rate",
                 "pipeline_completion_rate", "invalid_action_rate", "recovery_success_rate"):
        value = metrics[name]
        ratio = "NOT_MEASURED" if value["value"] is None else f"{value['value']:.4f}"
        lines.append(f"| `{name}` | {value['numerator']} | {value['denominator']} | {ratio} |")
    lines.extend([
        "",
        f"Unauthorized side effects: **{metrics['unauthorized_side_effect_count']['count']}** · "
        f"Duplicate side effects: **{metrics['duplicate_side_effect_count']['count']}** · "
        f"Human interventions: **{metrics['human_intervention_count']['count']}**",
        f"Totals — calls {metrics['totals']['calls']} · reads {metrics['totals']['reads']} · "
        f"tokens {metrics['totals']['tokens']} · cost {metrics['totals']['cost_microusd']} µUSD",
        f"Latency — p50 {metrics['latency_ms']['p50']} ms · p95 {metrics['latency_ms']['p95']} ms "
        f"({metrics['latency_ms']['status']})",
        "",
    ])
    if metrics["badcases"]:
        lines.extend(["## Badcases", "", "| Task | Category | Provider | Model | Reasons |",
                      "| --- | --- | --- | --- | --- |"])
        for case in metrics["badcases"]:
            reasons = "; ".join(case["reasons"][:3]) or "verifier rejection"
            lines.append(
                f"| `{case['task_id']}` | {case['category']} | {case['provider']} | "
                f"{case['model']} | {redact_text(reasons)[:200]} |"
            )
        lines.append("")
    return "\n".join(lines)

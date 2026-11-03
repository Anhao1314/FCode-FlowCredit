"""Wires one scenario together and runs it end to end.

This module is the only place that chooses concrete implementations. Everything
below it takes interfaces: the harness does not know it is being rendered, the
renderer does not know it is being recorded, and the loop does not know whether a
human is watching. That is what lets the same runtime be driven by a test, a CLI,
or a future HTTP surface without changing a line of policy.

The crash scenario is the one exception to "no subprocesses": a real SIGKILL
needs a real process to kill, so it spawns a worker that dies on purpose and then
resumes from the same database in the parent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.contracts import AgentError, Task
from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..providers.base import Provider
from ..providers.mock import MockProvider
from ..runtime.budget import BudgetLimits
from ..runtime.harness import Harness
from ..runtime.loop import Loop
from ..tools.registry import ToolRegistry
from ..tools.general.sandbox import WorkspaceSandbox
from ..trajectory.exporter import export_trace, verify_trace
from ..trajectory.recorder import TraceRecorder
from .fixtures import SCENARIOS, Scenario, get_scenario
from .provider_factory import build_provider, provider_start_line
from .render import Renderer
from ..domains import get_domain

__all__ = ["PROJECT_ROOT", "WORK_DIR", "RunResult", "run_scenario", "run_all"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORK_DIR = PROJECT_ROOT / "work" / "demo"


@dataclass
class RunResult:
    """What one scenario produced, in the numbers a reviewer will ask for."""

    key: str
    status: str
    rounds: int = 0
    calls: int = 0
    reads: int = 0
    cost_microusd: int = 0
    trace_path: str = ""
    trace_verified: bool = False
    recovered: bool = False
    reconciled: bool = False
    failure_code: str = ""
    model_calls: int = 0
    note: str = ""
    expected: str = "succeeded"
    provider: str = "offline-mock"
    receipt_cost_microusd: int = 0
    chain_head: str = ""
    calibration: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """True when the scenario reached the terminal state it was designed for."""
        return self.status == self.expected and self.trace_verified

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


@dataclass
class Stack:
    store: TaskStore
    world: Any
    registry: ToolRegistry
    receipts: ReceiptStore
    provider: Provider
    harness: Harness
    planner: Any
    #: The domain module that assembled this stack (world, tools, verifier).
    module: Any = None
    issued: dict[str, str] = field(default_factory=dict)


class _Tee:
    """Fan one observer signal out to several observers, in order."""

    def __init__(self, *observers: Any) -> None:
        self._observers = observers

    def round_start(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        for observer in self._observers:
            observer.round_start(round_no, budget, plan)

    def proposal(self, round_no: int, proposal: Any) -> None:
        for observer in self._observers:
            observer.proposal(round_no, proposal)

    def gate(self, round_no: int, decision: Any) -> None:
        for observer in self._observers:
            observer.gate(round_no, decision)

    def observation(self, round_no: int, observation: Any) -> None:
        for observer in self._observers:
            observer.observation(round_no, observation)

    def round_end(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        for observer in self._observers:
            observer.round_end(round_no, budget, plan)

    def terminal(self, task: Any, result: Any) -> None:
        for observer in self._observers:
            observer.terminal(task, result)


def _db_path(key: str) -> Path:
    return WORK_DIR / f"{key}.sqlite"


def _fresh_database(path: Path) -> None:
    """Remove a previous run so the demo always starts from an empty store.

    The *decisions* are reproducible; the trace bytes are not, because every
    event carries a wall-clock timestamp. Claiming byte-identical reruns would be
    a lie, and a runtime that lies about reproducibility is worse than one that
    admits timestamps exist.
    """
    for suffix in ("", "-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate.exists():
            candidate.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)


def build_stack(
    db_path: Path,
    scenario: Scenario,
    *,
    provider_factory: Callable[[ReceiptStore], Provider] | None = None,
    limits: BudgetLimits | None = None,
    spec: dict[str, Any] | None = None,
) -> Stack:
    """Assemble store, world, registry, provider and harness for one scenario."""
    store = TaskStore(db_path)
    module = get_domain(scenario.module)
    # The delegation spec is operator input (sources, mode, scope), never model
    # input. The reference workload ignores it; the general world reads it.
    world_kwargs: dict[str, Any] = {}
    if module.key == "general-work":
        # The sandbox sits next to the chain it feeds, so files survive a restart.
        world_kwargs["sandbox"] = WorkspaceSandbox(db_path.parent / "workspace")
    world = module.create_world(
        partial=scenario.partial_corpus, spec=spec or {}, **world_kwargs,
    )
    recall_holder: dict[str, Any] = {"memory": None}
    if hasattr(world, "memory_records_provider"):
        world.recall_holder = recall_holder
    if hasattr(world, "memory_records_provider") and world.memory_records_provider is None:
        # recall_observation reads the run's own compressed observation log —
        # the same projection working memory holds, assembled before each call.
        def _records_provider() -> list[dict[str, Any]]:
            memory = recall_holder.get("memory")
            if memory is None:
                return []
            return [
                {
                    "round": record.round,
                    "tool": record.tool,
                    "arguments": dict(record.arguments),
                    "summary": memory._summarise(record),
                    "result_hash": record.result_hash,
                }
                for record in memory.records
            ]

        world.memory_records_provider = _records_provider
    registry = ToolRegistry(module.build_tools(world))
    receipts = ReceiptStore(store)
    issued: dict[str, str] = {}

    def issue(**kwargs: Any) -> str:
        ref = receipts.issue(**kwargs)
        issued["ref"] = ref
        return ref

    if provider_factory is None:
        provider = MockProvider(
            policy=scenario.policy,
            fail_at_call=scenario.fail_at_call,
            issue_receipt=issue if scenario.fail_at_call else None,
        )
    else:
        provider = provider_factory(receipts)
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=limits or scenario.limits,
        receipts=receipts,
        verifier=lambda observation: module.verify(world, observation),
        state_fn=module.summarize_state,
        # The domain's own protocol wording and prompt profile. Omitting these
        # was not neutral: this path then told a general delegation the
        # read-only kernel text and rendered it the financial system prompt,
        # while the console — which passes both — told it the general ones, so
        # the same run behaved differently depending on which entry point
        # started it. A domain's contract travels with the domain.
        instruction=module.instruction,
        prompt_profile=module.prompt_profile,
        plan_extension=module.plan_extension,
        # The declared result obligation, when the delegation has one. Built
        # from the world (which holds the validated spec), so the CLI and the
        # console carry the same criterion for the same delegation.
        verification=(
            module.make_verification(world) if module.make_verification is not None else None
        ),
    )
    return Stack(store, world, registry, receipts, provider, harness, module.make_planner(world), module, issued)


def _count(store: TaskStore, task_id: str, event_type: str) -> int:
    return sum(1 for event in store.events(task_id) if event["type"] == event_type)


def _create_task(stack: Stack, scenario: Scenario, spec: dict[str, Any] | None = None) -> Task:
    binding = {"scenario": scenario.key, "corpus": "synthetic-authored-v1"}
    if spec:
        # The operator's delegation terms travel into the chain with the task.
        binding["spec"] = dict(spec)
    return stack.harness.create(
        task_id=f"task-{scenario.key}-0001",
        objective=scenario.objective,
        principal="demo-researcher",
        binding=binding,
        allowed_tools=list(scenario.allowed_tools),
        grant=scenario.grant,
    )


def _finish(
    stack: Stack,
    scenario: Scenario,
    task: Task,
    recorder: TraceRecorder,
    renderer: Renderer,
    *,
    trace_out: str | None,
    provider_audit: list[dict[str, Any]] | None = None,
    provider_metadata: dict[str, Any] | None = None,
) -> tuple[str, bool, dict[str, Any]]:
    trace = recorder.build(task)
    if provider_audit is not None:
        trace["provider_audit"] = provider_audit
    if provider_metadata is not None:
        trace["provider_metadata"] = provider_metadata
    target = Path(trace_out) if trace_out else WORK_DIR / f"{scenario.key}.trace.json"
    path = export_trace(trace, target)
    renderer.footer(str(path), stack.harness.budget(task).snapshot(), task.model.attempts)
    try:
        verify_trace(trace)
        verified = True
        if not renderer.quiet:
            print(f" 轨迹自检  通过：{trace['chain']['event_count']} 个事件，链头 {trace['chain']['head_hash'][:12]}…")
    except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
        verified = False
        print(f" 轨迹自检  失败：{error}")
    return str(path), verified, trace


def _provider_calibration(provider: Provider) -> dict[str, Any] | None:
    target = getattr(provider, "wrapped", provider)
    calibration = getattr(target, "calibration", None)
    if calibration is None:
        return None
    return {
        "formula": calibration.formula,
        "measured_ratio": calibration.measured_ratio,
        "reports": list(calibration.reports),
    }


def run_scenario(
    key: str = "full",
    *,
    fast: bool = False,
    trace_out: str | None = None,
    model: str | None = None,
    quiet: bool = False,
    provider_name: str = "mock",
    provider_options: dict[str, Any] | None = None,
    workdir: str | Path | None = None,
    limits: BudgetLimits | None = None,
    spec: dict[str, Any] | None = None,
) -> RunResult:
    """Run one scenario and return its numbers.

    ``provider_name`` is the explicit host decision. The legacy ``model``
    argument remains only so an old ``--model real-model`` call still reaches
    ``PROVIDER_GATED`` instead of silently changing meaning.
    """
    scenario = get_scenario(key)
    if provider_name == "mock" and model not in (None, "mock"):
        raise AgentError(
            "PROVIDER_GATED",
            f"model {model!r} is gated off: pass --provider explicitly",
        )

    if workdir is not None:
        run_dir = Path(workdir).expanduser().resolve()
        if run_dir in (Path("/"), PROJECT_ROOT):
            raise ValueError(f"refusing to use {run_dir} as a demo workdir")
        run_dir.mkdir(parents=True, exist_ok=True)
        db = run_dir / "task.sqlite"
        default_trace = run_dir / f"{scenario.key}.trace.json"
    else:
        db = _db_path(key)
        default_trace = WORK_DIR / f"{scenario.key}.trace.json"

    provider_audit: list[dict[str, Any]] = []
    options = dict(provider_options or {})
    calibration_path = None
    if provider_name == "kimi":
        calibration_path = (
            run_dir.parent / "kimi-calibration.json"
            if workdir is not None
            else WORK_DIR / "kimi-calibration.json"
        )

    def factory(receipts: ReceiptStore) -> Provider:
        return build_provider(
            provider_name,
            scenario=scenario,
            receipts=receipts,
            model=model,
            endpoint=options.get("endpoint"),
            timeout=options.get("timeout"),
            json_mode=bool(options.get("json_mode", False)),
            structured=bool(options.get("structured", False)),
            reasoning_effort=options.get("reasoning_effort"),
            calibration_path=calibration_path,
            raw_sink=provider_audit.append,
        )

    _fresh_database(db)
    stack = build_stack(
        db,
        scenario,
        provider_factory=factory if provider_name != "mock" else None,
        limits=limits,
        spec=spec,
    )
    if provider_name != "mock":
        print(provider_start_line(stack.provider))

    renderer = Renderer(fast=fast, quiet=quiet)
    recorder = TraceRecorder(stack.store, provider_name=stack.provider.name, model_name=stack.provider.model)
    observer = _Tee(renderer, recorder)
    task = _create_task(stack, scenario, spec)
    provider_note = (
        "(离线确定性 Mock，无网络、无密钥、无付费调用)"
        if provider_name == "mock"
        else "(显式启用的 provider：外发调用由主机门控与审计)"
    )
    renderer.header(
        scenario.title,
        scenario.objective,
        scenario.story,
        stack.provider.model,
        stack.harness.budget(task).snapshot(),
        provider_note=provider_note,
    )

    recovered = False
    reconciled = False
    note = ""

    if scenario.crash_at:
        task, recovered = _run_crash(
            stack,
            scenario,
            task,
            observer,
            renderer,
            db_path=db,
            provider_name=provider_name,
            provider_options=options,
            model=model,
            calibration_path=calibration_path,
            limits=limits,
        )
    else:
        task = Loop(
            stack.harness,
            planner=stack.planner,
            max_rounds=scenario.max_rounds,
            observer=observer,
            result_tools=stack.module.result_tools,
        ).run(task.id)

        if scenario.reconcile and task.status == "recovery_required":
            renderer.event("UNKNOWN", "模型结果未知：不重发、不退费，等待显式对账", colour=Renderer.YELLOW)
            try:
                stack.harness.reconcile(task, "0" * 64)
            except AgentError as error:
                renderer.event("REJECTED", f"错误收据被拒绝：{error.code}", colour=Renderer.RED)
            ref = stack.issued.get("ref", "")
            task = stack.harness.reconcile(task, ref)
            reconciled = True
            renderer.event("RECONCILED", f"签名收据匹配 round {task.model.attempts}，继续执行", colour=Renderer.GREEN)
            task = Loop(
                stack.harness,
                planner=stack.planner,
                max_rounds=scenario.max_rounds,
                observer=observer,
                result_tools=stack.module.result_tools,
            ).run(task.id)

    if (
        scenario.key == "tool-denied"
        and provider_name not in ("mock",)
        and task.status != "denied"
    ):
        note = "model declined to propose unregistered tool; gate covered by mock regression"

    target = trace_out or (str(default_trace) if workdir is not None else None)
    redacted = getattr(getattr(stack.provider, "wrapped", stack.provider), "redacted", None)
    provider_metadata: dict[str, Any] = {
        "provider": stack.provider.name,
        "model": stack.provider.model,
    }
    if scenario.key == "tool-denied" and provider_name != "mock":
        provider_metadata["fault_injection"] = True
        provider_metadata["disclosure"] = (
            "scenario deliberately includes an unregistered tool in the plan; "
            "test target is host gating, not model spontaneity"
        )
    if callable(redacted):
        provider_metadata["config"] = redacted()
    calibration = _provider_calibration(stack.provider)
    if calibration is not None:
        provider_metadata["calibration"] = calibration

    path, verified, trace = _finish(
        stack,
        scenario,
        task,
        recorder,
        renderer,
        trace_out=target,
        provider_audit=provider_audit,
        provider_metadata=provider_metadata,
    )
    receipt_cost = sum(
        int((record.get("receipt") or {}).get("cost_microusd") or 0)
        for record in trace.get("rounds", [])
        if record.get("receipt")
    )
    budget = stack.harness.budget(task).snapshot()
    result = RunResult(
        key=key,
        status=task.status,
        rounds=task.model.attempts,
        calls=budget["calls_used"],
        reads=budget["reads_used"],
        cost_microusd=budget["cost_used_microusd"],
        trace_path=path,
        trace_verified=verified,
        recovered=recovered,
        reconciled=reconciled,
        failure_code=(task.failure or {}).get("code", ""),
        model_calls=_count(stack.store, task.id, "model_started"),
        note=note,
        expected=scenario.expected_status,
        provider=stack.provider.name,
        receipt_cost_microusd=receipt_cost,
        chain_head=str(trace.get("chain", {}).get("head_hash") or ""),
        calibration=calibration,
    )
    stack.store.close()
    return result


def _run_crash(
    stack: Stack,
    scenario: Scenario,
    task: Task,
    observer: _Tee,
    renderer: Renderer,
    *,
    db_path: Path,
    provider_name: str = "mock",
    provider_options: dict[str, Any] | None = None,
    model: str | None = None,
    calibration_path: Path | None = None,
    limits: BudgetLimits | None = None,
) -> tuple[Task, bool]:
    """Run until the worker kills itself, then resume in this process."""
    options = provider_options or {}
    budget = limits or scenario.limits
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "FLOWAGENT_DB": str(db_path),
        "FLOWAGENT_TASK_ID": task.id,
        "FLOWAGENT_CRASH_AT": scenario.crash_at or "tool_succeeded",
        "FLOWAGENT_CRASH_AT_COUNT": str(scenario.crash_at_count),
        "FLOWAGENT_MAX_ROUNDS": str(scenario.max_rounds),
        "FLOWAGENT_PROVIDER": provider_name,
        "FLOWAGENT_SCENARIO": scenario.key,
        "FLOWAGENT_PROVIDER_MODEL": model or "",
        "FLOWAGENT_PROVIDER_ENDPOINT": str(options.get("endpoint") or ""),
        "FLOWAGENT_PROVIDER_TIMEOUT": str(options.get("timeout") or ""),
        "FLOWAGENT_PROVIDER_JSON_MODE": "1" if options.get("json_mode") else "",
        "FLOWAGENT_PROVIDER_STRUCTURED": "1" if options.get("structured") else "",
        "FLOWAGENT_PROVIDER_REASONING_EFFORT": str(options.get("reasoning_effort") or ""),
        "FLOWAGENT_KIMI_CALIBRATION": str(calibration_path) if calibration_path else "",
        "FLOWAGENT_BUDGET_MAX_CALLS": str(budget.max_calls),
        "FLOWAGENT_BUDGET_MAX_READS": str(budget.max_reads),
        "FLOWAGENT_BUDGET_MAX_COST_MICROUSD": str(budget.max_cost_microusd),
        "FLOWAGENT_BUDGET_MAX_TOKENS": str(budget.max_tokens),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "flowagent.demo.crash_worker"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    killed = completed.returncode < 0
    renderer.event(
        "CRASH",
        f"子进程退出码 {completed.returncode}（负数即被信号杀死），数据库仍完整",
        colour=Renderer.RED if killed else Renderer.YELLOW,
    )
    if not killed:
        renderer.event("WARNING", f"worker 未按预期崩溃：{completed.stderr.strip()[:120]}", colour=Renderer.YELLOW)

    resumed = stack.store.load(task.id)
    memory = Loop(
        stack.harness,
        planner=stack.planner,
        result_tools=stack.module.result_tools,
    ).restore(resumed)
    stack.world.apply_state(memory.state())
    renderer.event(
        "RECOVERED",
        f"从事件日志重建记忆：已读 {len(memory.state()['read_documents'])} 份资料，"
        f"崩溃前完成 {resumed.model.attempts} 轮",
        colour=Renderer.GREEN,
    )

    task = Loop(
        stack.harness,
        planner=stack.planner,
        max_rounds=scenario.max_rounds,
        observer=observer,
        result_tools=stack.module.result_tools,
    ).run(resumed.id)

    # The property that matters is not "the counter went up by the right amount"
    # but "no round was proposed twice". Duplicates would mean a paid call was
    # repeated after a crash, which is the failure this scenario exists to rule out.
    rounds = [
        event["payload"]["detail"].get("round")
        for event in stack.store.events(task.id)
        if event["type"] == "model_started"
    ]
    duplicated = sorted({item for item in rounds if rounds.count(item) > 1})
    assert not duplicated, f"recovery re-proposed rounds {duplicated}"
    renderer.event(
        "NO REPEAT",
        f"模型共调用 {len(rounds)} 次，覆盖轮次 {sorted(set(rounds))}，无任何一轮被重复提议",
        colour=Renderer.GREEN,
    )
    return task, killed


def run_all(
    *,
    trace_dir: str | None = None,
    provider_name: str = "mock",
    provider_options: dict[str, Any] | None = None,
    workroot: str | Path | None = None,
    limits: BudgetLimits | None = None,
    max_total_cost: int | None = None,
    spec_for: Callable[[str], dict[str, Any]] | None = None,
) -> list[RunResult]:
    """Run every scenario in order and print a summary table.

    The optional total-cost guard is batch-level and conservative: it checks
    cumulative receipt costs between tasks. A task already in flight remains
    governed by its own pre-dispatch budget, so the guard never kills a model
    call halfway and turns a known outcome into an unknown one.
    """
    if provider_name != "mock" and max_total_cost is None:
        raise ValueError("an external provider batch requires --max-total-cost")

    keys = list(SCENARIOS)
    per_task_cap = limits.max_cost_microusd if limits is not None else SCENARIOS[keys[0]].limits.max_cost_microusd
    worst_case = len(keys) * per_task_cap
    print(
        f"batch worst-case cost estimate: {len(keys)} tasks × "
        f"{per_task_cap} µUSD = {worst_case} µUSD"
    )
    if max_total_cost is not None and worst_case > max_total_cost:
        print(
            "warning: the total-cost ceiling is below the configured batch upper bound; "
            "later tasks may stop after completed runs"
        )

    results: list[RunResult] = []
    cumulative = 0
    stopped_by_guard = False
    for key in keys:
        result = run_scenario(
            key,
            fast=True,
            quiet=True,
            trace_out=trace_dir,
            provider_name=provider_name,
            provider_options=provider_options,
            workdir=Path(workroot) / key if workroot is not None else None,
            limits=limits,
            spec=spec_for(key) if spec_for is not None else None,
        )
        results.append(result)
        cumulative += result.receipt_cost_microusd
        if max_total_cost is not None and cumulative >= max_total_cost:
            stopped_by_guard = True
            result.note = (
                f"{result.note}; " if result.note else ""
            ) + "batch_stopped_by_cost_guard"
            print(
                "batch_stopped_by_cost_guard: "
                f"cumulative receipt cost {cumulative} µUSD reached {max_total_cost} µUSD"
            )
            break

    print()
    print("─" * 78)
    print(" 场景汇总(参考负载声明:以下场景用金融参考域注入故障,是 Runtime 自检,不是产品演示)")
    print("─" * 78)
    header = f" {'场景':<18} {'终态':<20} {'轮次':>4} {'调用':>4} {'读取':>4} {'花费µUSD':>9} {'轨迹':>4}"
    print(header)
    for result in results:
        trace_state = "ok" if result.trace_verified else "FAIL"
        print(
            f" {result.key:<18} {result.status:<20} {result.rounds:>4} {result.calls:>4}"
            f" {result.reads:>4} {result.cost_microusd:>9} {trace_state:>4}"
        )
    print("─" * 78)
    failures = [result for result in results if not result.trace_verified]
    if failures:
        print(f" {len(failures)} 个场景的轨迹未通过自检")
    else:
        print(f" 全部 {len(results)} 个场景的轨迹均通过自检")
    if stopped_by_guard:
        print(f" 批量成本闸触发：已完成 {len(results)} 个场景，累计收据成本 {cumulative} µUSD")
    print()
    return results

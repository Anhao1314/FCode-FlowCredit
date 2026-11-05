"""Multi-role orchestrator: manifest, child runs, budgets and guards.

The orchestrator is deterministic and process-local. It never executes a tool;
it only asks the executor to create a child runtime task and links that child's
hash chain into the manifest.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flowagent.core.hashing import canonical_json
from flowagent.core.plan import Plan
from flowagent.runtime.budget import BudgetLimits
from flowagent.trajectory.exporter import verify_trace

from .executor import ChildExecutor
from .fixtures import TaskDefinition
from .handoff import Handoff, InvalidHandoff, ReviewVerdict
from .roles import DeterministicPlanner, RuleReviewer
from .state import (
    BudgetLedger,
    ChildResult,
    ManifestStore,
    MultiAgentState,
    atomic_write_json,
    manifest_hash,
)

__all__ = ["SimulatedCrash", "LabRunResult", "Orchestrator"]


class SimulatedCrash(Exception):
    """Test-only crash point used by the manifest resume test."""


@dataclass
class LabRunResult:
    run_id: str
    task: str
    mode: str
    status: str
    outcome: str | None
    attempts: int
    child_runs: list[dict[str, Any]]
    budget: dict[str, Any]
    replans: int
    rejects: int
    duplicate_count: int
    manifest_path: str
    manifest_hash: str
    run_dir: str
    wall_ms: int
    verified: bool
    expected_status: str
    success: bool
    traces: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "mode": self.mode,
            "status": self.status,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "child_runs": self.child_runs,
            "budget": self.budget,
            "replans": self.replans,
            "rejects": self.rejects,
            "duplicate_count": self.duplicate_count,
            "manifest_path": self.manifest_path,
            "manifest_hash": self.manifest_hash,
            "run_dir": self.run_dir,
            "wall_ms": self.wall_ms,
            "verified": self.verified,
            "expected_status": self.expected_status,
            "success": self.success,
            "traces": self.traces,
        }


def _limits(data: dict[str, Any]) -> BudgetLimits:
    return BudgetLimits(
        max_calls=int(data["max_calls"]),
        max_reads=int(data["max_reads"]),
        max_cost_microusd=int(data["max_cost_microusd"]),
        max_tokens=int(data["max_tokens"]),
    )


def _min_limits(left: BudgetLimits, right: BudgetLimits) -> BudgetLimits:
    return BudgetLimits(
        max_calls=min(left.max_calls, right.max_calls),
        max_reads=min(left.max_reads, right.max_reads),
        max_cost_microusd=min(left.max_cost_microusd, right.max_cost_microusd),
        max_tokens=min(left.max_tokens, right.max_tokens),
    )


def _verdict_key(verdict: ReviewVerdict) -> tuple[str, tuple[str, ...]]:
    return verdict.kind, tuple(verdict.required_changes)


def _signature(event_args: Any) -> str:
    return canonical_json(event_args)


class Orchestrator:
    max_attempts = 2

    def __init__(self, root: Path, *, planner: Any = None, reviewer: Any = None) -> None:
        self.root = root
        self.planner = planner or DeterministicPlanner()
        self.reviewer = reviewer or RuleReviewer()
        self.executor = ChildExecutor(root)

    # ------------------------------------------------------------------ public

    def run(
        self,
        task: TaskDefinition,
        *,
        mode: str = "multi",
        run_id: str | None = None,
        crash_after_attempt: int | None = None,
    ) -> LabRunResult:
        if mode == "single":
            return self._single(task, run_id=run_id)
        return self._multi(task, run_id=run_id, crash_after_attempt=crash_after_attempt)

    def resume(self, run_id: str) -> LabRunResult:
        run_dir = self.root / run_id
        state = MultiAgentState.from_dict(_read_json(run_dir / "state.json"))
        manifest = ManifestStore(run_dir / "manifest.jsonl")
        manifest.verify()
        for child in state.child_runs:
            _verify_child(child)
        manifest.append("run_resumed", {"run_id": run_id, "attempts": state.attempts})
        return self._continue(state, manifest, task=_task_from_spec(state.task_spec))

    # ------------------------------------------------------------------ single

    def _single(self, task: TaskDefinition, *, run_id: str | None) -> LabRunResult:
        started = time.monotonic()
        rid = run_id or f"ma-single-{uuid.uuid4().hex[:12]}"
        plan = self.planner.plan(task.spec())
        child = self.executor.run(
            run_id=rid,
            spec=task.spec(),
            plan=plan,
            attempt=1,
            limits=task.limits,
        )
        status = child.runtime_status
        outcome = child.failure_code or status
        success = status == task.expected_status
        return LabRunResult(
            run_id=rid,
            task=task.key,
            mode="single",
            status=status,
            outcome=outcome,
            attempts=1,
            child_runs=[child.to_dict()],
            budget={"settled": child.budget.to_dict()},
            replans=0,
            rejects=0,
            duplicate_count=0,
            manifest_path="",
            manifest_hash="",
            run_dir=str(self.root / rid),
            wall_ms=int((time.monotonic() - started) * 1000),
            verified=True,
            expected_status=task.expected_status,
            success=success,
            traces=[child.trace_path],
        )

    # ------------------------------------------------------------------- multi

    def _multi(
        self,
        task: TaskDefinition,
        *,
        run_id: str | None,
        crash_after_attempt: int | None,
    ) -> LabRunResult:
        rid = run_id or f"ma-{uuid.uuid4().hex[:12]}"
        run_dir = self.root / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = ManifestStore(run_dir / "manifest.jsonl")
        total = task.limits
        state = MultiAgentState(
            run_id=rid,
            objective=task.objective,
            task_spec=task.spec(),
            plan={},
            total_limits=total,
        )
        manifest.append("run_started", {
            "run_id": rid,
            "task": task.key,
            "total_limits": total.to_dict(),
        })
        plan = self.planner.plan(task.spec())
        state.plan = plan.to_dict()
        state.plan_revision = plan.revision
        manifest.append("plan_created", {
            "run_id": rid, "revision": plan.revision, "plan": plan.to_dict(),
        })
        _save_state(run_dir, state)
        return self._continue(state, manifest, task=task, crash_after_attempt=crash_after_attempt)

    def _continue(
        self,
        state: MultiAgentState,
        manifest: ManifestStore,
        *,
        task: TaskDefinition,
        crash_after_attempt: int | None = None,
    ) -> LabRunResult:
        started = time.monotonic()
        run_dir = self.root / state.run_id
        base_limits = task.limits
        budget = state.budget_ledger
        assert budget is not None
        plan = Plan.from_dict(state.plan)
        replans = sum(1 for item in state.verdicts if item.get("kind") == "replan")
        rejects = sum(1 for item in state.verdicts if item.get("kind") == "reject")
        duplicate_count = 0

        while state.attempts < self.max_attempts:
            attempt = state.attempts + 1

            remaining = budget.remaining_limits(base_limits)
            child_limits = _min_limits(base_limits, remaining)
            if not budget.reserve("executor", child_limits):
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status="budget_exhausted", outcome="BUDGET_EXCEEDED",
                    started=started,
                )

            handoff = Handoff(
                message_id=f"h-{state.run_id}-{attempt}",
                run_id=state.run_id,
                from_role="planner",
                to_role="executor",
                kind="plan",
                task_id=f"{state.run_id}-a{attempt}",
                plan=plan.to_dict(),
                allowed_tools=list(state.task_spec.get("allowed_tools") or []),
                grant=dict(state.task_spec.get("grant") or {}),
                limits=child_limits.to_dict(),
                constraints={"max_rounds": int(state.task_spec.get("max_rounds") or 12)},
                notes="",
            )
            handoff.refresh_hash()
            try:
                handoff.validate(
                    list(state.task_spec.get("allowed_tools") or []),
                    child_limits,
                )
            except InvalidHandoff as error:
                manifest.append("agent_verdict", {
                    "run_id": state.run_id, "attempt": attempt,
                    "kind": "invalid_handoff", "reason": str(error),
                })
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status="invalid_handoff", outcome="INVALID_HANDOFF",
                    started=started,
                )

            if attempt > 1:
                prior = {
                    tuple(signature)
                    for child in state.child_runs
                    for signature in (child.tool_signatures or [])
                }
                planned = {
                    (todo.tool, _signature(todo.arguments))
                    for todo in plan.todos
                    if todo.status in ("pending", "active")
                }
                overlap = sorted(
                    f"{item[0]}:{item[1]}" for item in planned if item in prior
                )
                if overlap:
                    duplicate_count += len(overlap)
                    return self._finish(
                        state, manifest, task, plan, budget, replans, rejects,
                        duplicate_count, status="failed", outcome="DUPLICATE_OBSERVATION",
                        started=started,
                    )

            state.attempts = attempt
            state.handoffs.append(handoff.to_dict())
            manifest.append("handoff", handoff.to_dict())
            child = self.executor.run(
                run_id=state.run_id,
                spec=state.task_spec,
                plan=plan,
                attempt=attempt,
                limits=child_limits,
            )
            budget.settle("executor", child.budget)
            child.manifest_sequence = manifest.append("child_run_linked", child.to_dict())["sequence"]
            state.child_runs.append(child)
            review_handoff = Handoff(
                message_id=f"r-{state.run_id}-{attempt}",
                run_id=state.run_id,
                from_role="executor",
                to_role="reviewer",
                kind="result",
                task_id=child.task_id,
                plan=plan.to_dict(),
                allowed_tools=[],
                grant={},
                limits=child_limits.to_dict(),
                constraints={
                    "runtime_status": child.runtime_status,
                    "failure_code": child.failure_code,
                    "chain_head": child.chain_head,
                },
                notes="",
            )
            review_handoff.refresh_hash()
            review_handoff.validate([], child_limits)
            state.handoffs.append(review_handoff.to_dict())
            manifest.append("handoff", review_handoff.to_dict())
            verdict = self.reviewer.review(child, plan, state)
            child.review_status = verdict.kind
            state.verdicts.append(verdict.to_dict())
            manifest.append("agent_verdict", verdict.to_dict())
            if verdict.kind == "replan":
                replans += 1
            elif verdict.kind == "reject":
                rejects += 1
            _save_state(run_dir, state)

            if verdict.kind == "accept":
                outcome = child.failure_code or child.runtime_status
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status="accepted", outcome=outcome,
                    started=started,
                )
            if verdict.kind == "stop":
                status = "budget_exhausted" if child.failure_code == "BUDGET_EXCEEDED" else "failed"
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status=status, outcome=child.failure_code or child.runtime_status,
                    started=started,
                )

            previous_key = None
            if len(state.verdicts) >= 2:
                previous = state.verdicts[-2]
                previous_key = (str(previous.get("kind")), tuple(previous.get("required_changes") or []))
            if previous_key == _verdict_key(verdict):
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status="no_progress", outcome="NO_PROGRESS",
                    started=started,
                )
            if attempt >= self.max_attempts:
                status = "rejected" if verdict.kind == "reject" else "failed"
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status=status, outcome=verdict.kind.upper(),
                    started=started,
                )

            try:
                plan = self.planner.revise(plan, verdict)
            except (ValueError, InvalidHandoff) as error:
                return self._finish(
                    state, manifest, task, plan, budget, replans, rejects,
                    duplicate_count, status="invalid_handoff", outcome=str(error),
                    started=started,
                )
            state.plan = plan.to_dict()
            state.plan_revision = plan.revision
            manifest.append("replan_requested", {
                "run_id": state.run_id,
                "attempt": attempt,
                "required_changes": verdict.required_changes,
                "plan": plan.to_dict(),
            })
            _save_state(run_dir, state)
            if crash_after_attempt == attempt:
                raise SimulatedCrash(f"simulated orchestrator crash after attempt {attempt}")

        return self._finish(
            state, manifest, task, plan, budget, replans, rejects,
            duplicate_count, status="failed", outcome="MAX_ATTEMPTS",
            started=started,
        )

    def _finish(
        self,
        state: MultiAgentState,
        manifest: ManifestStore,
        task: TaskDefinition,
        plan: Plan,
        budget: BudgetLedger,
        replans: int,
        rejects: int,
        duplicate_count: int,
        *,
        status: str,
        outcome: str,
        started: float,
    ) -> LabRunResult:
        state.status = status
        state.outcome = outcome
        state.updated_at = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        state.refresh_hash()
        run_dir = self.root / state.run_id
        _save_state(run_dir, state)
        manifest.append("run_finished", {
            "run_id": state.run_id,
            "status": status,
            "outcome": outcome,
            "attempts": state.attempts,
            "budget": budget.to_dict(),
            "plan_revision": plan.revision,
        })
        traces = [child.trace_path for child in state.child_runs]
        verified = True
        for child in state.child_runs:
            try:
                _verify_child(child)
            except Exception:  # noqa: BLE001 — surfaced as verified=False
                verified = False
        success = self._success(task, status, outcome, state)
        return LabRunResult(
            run_id=state.run_id,
            task=task.key,
            mode="multi",
            status=status,
            outcome=outcome,
            attempts=state.attempts,
            child_runs=[child.to_dict() for child in state.child_runs],
            budget=budget.to_dict(),
            replans=replans,
            rejects=rejects,
            duplicate_count=duplicate_count,
            manifest_path=str(run_dir / "manifest.jsonl"),
            manifest_hash=manifest_hash(manifest),
            run_dir=str(run_dir),
            wall_ms=int((time.monotonic() - started) * 1000),
            verified=verified,
            expected_status=task.expected_status,
            success=success,
            traces=traces,
        )

    @staticmethod
    def _success(
        task: TaskDefinition,
        status: str,
        outcome: str,
        state: MultiAgentState,
    ) -> bool:
        expected = task.expected_status
        if expected in ("rejected", "no_progress", "invalid_handoff"):
            return status == expected
        if expected == "succeeded":
            return status == "accepted" and any(
                child.runtime_status == "succeeded" for child in state.child_runs
            )
        if expected in ("failed", "denied", "recovery_required"):
            if status == "budget_exhausted" and expected == "failed":
                return outcome in ("BUDGET_EXCEEDED", "NO_PROGRESS")
            if expected == "recovery_required" and status == "failed":
                return outcome == "MODEL_OUTCOME_UNKNOWN"
            return status == "accepted" and any(
                child.runtime_status == expected for child in state.child_runs
            )
        return status == expected and (not task.expected_outcome or outcome == task.expected_outcome)


def _save_state(run_dir: Path, state: MultiAgentState) -> None:
    state.refresh_hash()
    atomic_write_json(run_dir / "state.json", state.to_dict())


def _read_json(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _verify_child(child: ChildResult) -> None:
    import json

    trace = json.loads(Path(child.trace_path).read_text(encoding="utf-8"))
    summary = verify_trace(trace)
    if summary["head_hash"] != child.chain_head:
        raise ValueError(f"child {child.task_id} chain head differs from manifest")


def _task_from_spec(spec: dict[str, Any]) -> TaskDefinition:
    from .fixtures import TaskDefinition

    return TaskDefinition(
        key=str(spec["key"]),
        objective=str(spec["objective"]),
        domain=str(spec.get("domain", "finance")),
        allowed_tools=list(spec.get("allowed_tools") or []),
        limits=_limits(spec["limits"]),
        max_rounds=int(spec.get("max_rounds") or 12),
        policy=str(spec.get("policy") or "research"),
        plan_kind=str(spec.get("plan_kind") or "blueprint"),
        reviewer_mode=str(spec.get("reviewer_mode") or "accept_runtime"),
        partial_corpus=bool(spec.get("partial_corpus")),
        fail_at_call=spec.get("fail_at_call"),
        expected_status=str(spec.get("expected_status") or "succeeded"),
        expected_outcome=str(spec.get("expected_outcome") or ""),
        grant=dict(spec.get("grant") or {}),
    )

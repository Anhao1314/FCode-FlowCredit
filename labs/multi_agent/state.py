"""State, budget ledger and the tamper-evident manifest for the lab.

The manifest is a small append-only hash chain. Child runs keep their own
runtime chains; the manifest links them by task id and chain head. This is
deliberately a lab-level artifact, not a change to the runtime event schema.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flowagent.core.hashing import digest
from flowagent.runtime.budget import BudgetLimits

__all__ = [
    "BudgetUsage",
    "BudgetLedger",
    "ChildResult",
    "MultiAgentState",
    "ManifestStore",
    "ManifestIntegrityError",
    "StateIntegrityError",
    "atomic_write_json",
    "manifest_hash",
]


class ManifestIntegrityError(Exception):
    """Raised when the manifest hash chain does not verify."""


class StateIntegrityError(Exception):
    """Raised when state.json no longer matches its state_hash."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _hash(value: Any) -> str:
    return "sha256:" + digest(value)


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


@dataclass
class BudgetUsage:
    calls: int = 0
    reads: int = 0
    cost_microusd: int = 0
    tokens: int = 0

    def add(self, other: "BudgetUsage") -> None:
        self.calls += other.calls
        self.reads += other.reads
        self.cost_microusd += other.cost_microusd
        self.tokens += other.tokens

    def fits(self, limit: BudgetLimits) -> bool:
        return (
            self.calls <= limit.max_calls
            and self.reads <= limit.max_reads
            and self.cost_microusd <= limit.max_cost_microusd
            and self.tokens <= limit.max_tokens
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "reads": self.reads,
            "cost_microusd": self.cost_microusd,
            "tokens": self.tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BudgetUsage":
        data = data or {}
        return cls(
            calls=int(data.get("calls") or 0),
            reads=int(data.get("reads") or 0),
            cost_microusd=int(data.get("cost_microusd") or 0),
            tokens=int(data.get("tokens") or 0),
        )


@dataclass
class BudgetLedger:
    total: BudgetLimits
    reserved: dict[str, BudgetUsage] = field(default_factory=dict)
    settled: BudgetUsage = field(default_factory=BudgetUsage)
    by_role: dict[str, BudgetUsage] = field(default_factory=dict)

    def reserve(self, role: str, limits: BudgetLimits) -> bool:
        pending = BudgetUsage(
            calls=sum(item.calls for item in self.reserved.values()),
            reads=sum(item.reads for item in self.reserved.values()),
            cost_microusd=sum(item.cost_microusd for item in self.reserved.values()),
            tokens=sum(item.tokens for item in self.reserved.values()),
        )
        candidate = BudgetUsage(
            calls=self.settled.calls + pending.calls + limits.max_calls,
            reads=self.settled.reads + pending.reads + limits.max_reads,
            cost_microusd=self.settled.cost_microusd + pending.cost_microusd + limits.max_cost_microusd,
            tokens=self.settled.tokens + pending.tokens + limits.max_tokens,
        )
        if not candidate.fits(self.total):
            return False
        self.reserved[role] = BudgetUsage(
            calls=limits.max_calls,
            reads=limits.max_reads,
            cost_microusd=limits.max_cost_microusd,
            tokens=limits.max_tokens,
        )
        return True

    def settle(self, role: str, actual: BudgetUsage) -> None:
        self.reserved.pop(role, None)
        self.settled.add(actual)
        self.by_role.setdefault(role, BudgetUsage()).add(actual)

    def remaining_limits(self, base: BudgetLimits) -> BudgetLimits:
        return BudgetLimits(
            max_calls=max(0, self.total.max_calls - self.settled.calls),
            max_reads=max(0, self.total.max_reads - self.settled.reads),
            max_cost_microusd=max(0, self.total.max_cost_microusd - self.settled.cost_microusd),
            max_tokens=max(0, self.total.max_tokens - self.settled.tokens),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total.to_dict(),
            "reserved": {role: usage.to_dict() for role, usage in self.reserved.items()},
            "settled": self.settled.to_dict(),
            "by_role": {role: usage.to_dict() for role, usage in self.by_role.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], total: BudgetLimits) -> "BudgetLedger":
        return cls(
            total=total,
            reserved={
                role: BudgetUsage.from_dict(value)
                for role, value in (data.get("reserved") or {}).items()
            },
            settled=BudgetUsage.from_dict(data.get("settled")),
            by_role={
                role: BudgetUsage.from_dict(value)
                for role, value in (data.get("by_role") or {}).items()
            },
        )


@dataclass
class ChildResult:
    task_id: str
    attempt: int
    runtime_status: str
    trace_path: str
    chain_head: str
    calls: int
    reads: int
    cost_microusd: int
    tokens: int
    error_class: str = ""
    failure_code: str = ""
    review_status: str | None = None
    tool_signatures: list[list[Any]] = field(default_factory=list)
    manifest_sequence: int | None = None
    task_db: str = ""
    event_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "runtime_status": self.runtime_status,
            "trace_path": self.trace_path,
            "chain_head": self.chain_head,
            "calls": self.calls,
            "reads": self.reads,
            "cost_microusd": self.cost_microusd,
            "tokens": self.tokens,
            "error_class": self.error_class,
            "failure_code": self.failure_code,
            "review_status": self.review_status,
            "tool_signatures": self.tool_signatures,
            "manifest_sequence": self.manifest_sequence,
            "task_db": self.task_db,
            "event_count": self.event_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChildResult":
        return cls(**data)

    @property
    def budget(self) -> BudgetUsage:
        return BudgetUsage(
            calls=self.calls,
            reads=self.reads,
            cost_microusd=self.cost_microusd,
            tokens=self.tokens,
        )


@dataclass
class MultiAgentState:
    run_id: str
    objective: str
    task_spec: dict[str, Any]
    plan: dict[str, Any]
    total_limits: BudgetLimits
    parent_run_id: str | None = None
    plan_revision: int = 1
    attempts: int = 0
    child_runs: list[ChildResult] = field(default_factory=list)
    handoffs: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    budget_ledger: BudgetLedger | None = None
    status: str = "running"
    outcome: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    state_hash: str = ""

    def __post_init__(self) -> None:
        if self.budget_ledger is None:
            self.budget_ledger = BudgetLedger(total=self.total_limits)

    def reproducible(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "objective": self.objective,
            "plan_revision": self.plan_revision,
            "attempts": self.attempts,
            "child_tasks": [child.task_id for child in self.child_runs],
            "verdict_kinds": [str(item.get("kind") or "") for item in self.verdicts],
            "settled": self.budget_ledger.settled.to_dict(),  # type: ignore[union-attr]
            "status": self.status,
            "outcome": self.outcome,
        }

    def refresh_hash(self) -> str:
        self.state_hash = _hash(self.reproducible())
        return self.state_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "objective": self.objective,
            "task_spec": self.task_spec,
            "plan": self.plan,
            "plan_revision": self.plan_revision,
            "attempts": self.attempts,
            "child_runs": [child.to_dict() for child in self.child_runs],
            "handoffs": self.handoffs,
            "verdicts": self.verdicts,
            "budget_ledger": self.budget_ledger.to_dict(),  # type: ignore[union-attr]
            "status": self.status,
            "outcome": self.outcome,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state_hash": self.state_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MultiAgentState":
        limits_data = (data.get("budget_ledger") or {}).get("total") or {}
        total = BudgetLimits(
            max_calls=int(limits_data.get("max_calls") or 0),
            max_reads=int(limits_data.get("max_reads") or 0),
            max_cost_microusd=int(limits_data.get("max_cost_microusd") or 0),
            max_tokens=int(limits_data.get("max_tokens") or 0),
        )
        ledger = BudgetLedger.from_dict(data.get("budget_ledger") or {}, total)
        state = cls(
            run_id=data["run_id"],
            parent_run_id=data.get("parent_run_id"),
            objective=data["objective"],
            task_spec=dict(data.get("task_spec") or {}),
            plan=dict(data.get("plan") or {}),
            plan_revision=int(data.get("plan_revision") or 1),
            attempts=int(data.get("attempts") or 0),
            child_runs=[ChildResult.from_dict(item) for item in data.get("child_runs") or []],
            handoffs=list(data.get("handoffs") or []),
            verdicts=list(data.get("verdicts") or []),
            total_limits=total,
            budget_ledger=ledger,
            status=data.get("status", "running"),
            outcome=data.get("outcome"),
            created_at=data.get("created_at") or _now(),
            updated_at=data.get("updated_at") or _now(),
            state_hash=data.get("state_hash", ""),
        )
        expected = _hash(state.reproducible())
        if state.state_hash and state.state_hash != expected:
            raise StateIntegrityError("state_hash does not match reproducible fields")
        state.state_hash = expected
        return state


class ManifestStore:
    """Append-only hash chain for orchestration events."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict[str, Any]] = []
        if path.exists():
            self._load()

    def _load(self) -> None:
        self.events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                self.events.append(json.loads(line))
        self.verify()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        previous = self.events[-1]["hash"] if self.events else None
        event = {
            "sequence": len(self.events) + 1,
            "type": kind,
            "at": _now(),
            "payload": payload,
            "prev_hash": previous,
        }
        event["hash"] = _hash({
            "sequence": event["sequence"],
            "type": event["type"],
            "payload": event["payload"],
            "prev_hash": event["prev_hash"],
            "at": event["at"],
        })
        self.events.append(event)
        self._write()
        return event

    def verify(self) -> dict[str, Any]:
        previous = None
        for index, event in enumerate(self.events, start=1):
            if event.get("sequence") != index:
                raise ManifestIntegrityError(f"manifest event {index} sequence mismatch")
            if event.get("prev_hash") != previous:
                raise ManifestIntegrityError(f"manifest event {index} does not link")
            expected = _hash({
                "sequence": event["sequence"],
                "type": event["type"],
                "payload": event["payload"],
                "prev_hash": event["prev_hash"],
                "at": event["at"],
            })
            if event.get("hash") != expected:
                raise ManifestIntegrityError(f"manifest event {index} hash mismatch")
            previous = event["hash"]
        return {"verified": True, "events": len(self.events), "head": previous}

    @property
    def head(self) -> str:
        return self.events[-1]["hash"] if self.events else ""


def manifest_hash(store: ManifestStore) -> str:
    return store.head

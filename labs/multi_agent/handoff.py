"""Structured handoffs and reviewer verdicts (lab-level, no tool authority)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any

from flowagent.core.hashing import digest
from flowagent.core.plan import Plan
from flowagent.runtime.budget import BudgetLimits

__all__ = ["InvalidHandoff", "Handoff", "ReviewVerdict"]


class InvalidHandoff(Exception):
    """Raised when a handoff would expand scope or carry unknown fields."""


def _hash(value: Any) -> str:
    return "sha256:" + digest(value)


@dataclass
class Handoff:
    message_id: str
    run_id: str
    from_role: str
    to_role: str
    kind: str
    task_id: str
    plan: dict[str, Any]
    allowed_tools: list[str]
    grant: dict[str, Any]
    limits: dict[str, int]
    constraints: dict[str, Any]
    notes: str = ""
    payload_hash: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "run_id": self.run_id,
            "from": self.from_role,
            "to": self.to_role,
            "kind": self.kind,
            "task_id": self.task_id,
            "plan": self.plan,
            "allowed_tools": self.allowed_tools,
            "grant": self.grant,
            "limits": self.limits,
            "constraints": self.constraints,
            "notes": self.notes,
        }

    def refresh_hash(self) -> str:
        self.payload_hash = _hash(self.body())
        return self.payload_hash

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "payload_hash": self.payload_hash}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Handoff":
        allowed = {field.name for field in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise InvalidHandoff(f"handoff has unknown fields: {sorted(unknown)}")
        mapped = dict(data)
        if "from" in mapped:
            mapped["from_role"] = mapped.pop("from")
        if "to" in mapped:
            mapped["to_role"] = mapped.pop("to")
        try:
            return cls(**mapped)
        except TypeError as error:
            raise InvalidHandoff(f"handoff is malformed: {error}") from error

    def validate(self, parent_allowed_tools: list[str], parent_limits: BudgetLimits) -> None:
        if not self.message_id or not self.run_id or not self.task_id:
            raise InvalidHandoff("message_id, run_id and task_id are required")
        if len(self.notes.encode("utf-8")) > 2048:
            raise InvalidHandoff("notes exceeds 2 KiB")
        if self.from_role == "executor" and self.to_role == "reviewer" and self.kind == "result":
            return
        if self.from_role != "planner" or self.to_role != "executor":
            raise InvalidHandoff("MVP only allows planner->executor and executor->reviewer handoffs")
        if not set(self.allowed_tools).issubset(set(parent_allowed_tools)):
            raise InvalidHandoff("handoff would expand allowed_tools")
        try:
            plan = Plan.from_dict(self.plan)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidHandoff(f"handoff plan is malformed: {error}") from error
        for todo in plan.todos:
            if todo.status not in ("pending", "active"):
                continue
            if todo.tool not in self.allowed_tools:
                raise InvalidHandoff(f"plan todo {todo.id!r} uses tool outside allowed_tools")
        try:
            limits = BudgetLimits(
                max_calls=int(self.limits["max_calls"]),
                max_reads=int(self.limits["max_reads"]),
                max_cost_microusd=int(self.limits["max_cost_microusd"]),
                max_tokens=int(self.limits["max_tokens"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidHandoff(f"handoff limits are malformed: {error}") from error
        if (
            limits.max_calls > parent_limits.max_calls
            or limits.max_reads > parent_limits.max_reads
            or limits.max_cost_microusd > parent_limits.max_cost_microusd
            or limits.max_tokens > parent_limits.max_tokens
        ):
            raise InvalidHandoff("handoff limits exceed parent limits")
        expected = _hash(self.body())
        if self.payload_hash and self.payload_hash != expected:
            raise InvalidHandoff("handoff payload_hash does not match")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass
class ReviewVerdict:
    verdict_id: str
    run_id: str
    attempt: int
    kind: str
    reasons: list[str]
    required_changes: list[str]
    evidence_refs: list[str]
    payload_hash: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "verdict_id": self.verdict_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "kind": self.kind,
            "reasons": self.reasons,
            "required_changes": self.required_changes,
            "evidence_refs": self.evidence_refs,
        }

    def refresh_hash(self) -> str:
        self.payload_hash = _hash(self.body())
        return self.payload_hash

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "payload_hash": self.payload_hash}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewVerdict":
        allowed = {field.name for field in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise InvalidHandoff(f"verdict has unknown fields: {sorted(unknown)}")
        try:
            return cls(**data)
        except TypeError as error:
            raise InvalidHandoff(f"verdict is malformed: {error}") from error

    def validate(self) -> None:
        if self.kind not in ("accept", "replan", "reject", "stop"):
            raise InvalidHandoff(f"unknown verdict kind {self.kind!r}")
        if not isinstance(self.required_changes, list) or any(
            not isinstance(item, str) or not item for item in self.required_changes
        ):
            raise InvalidHandoff("required_changes must be a list of non-empty strings")
        expected = _hash(self.body())
        if self.payload_hash and self.payload_hash != expected:
            raise InvalidHandoff("verdict payload_hash does not match")

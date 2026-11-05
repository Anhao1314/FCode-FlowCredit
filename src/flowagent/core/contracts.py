"""Versioned contracts, task state and validation rules.

This module is the Python counterpart of ``contracts.mjs`` plus the task body
defined in ``harness.mjs``. It deliberately keeps validation *strict and
narrow*: unknown keys are rejected rather than ignored, because a silently
accepted extra field is how a model escapes a schema without anyone noticing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

__all__ = [
    "PLAN_VERSION",
    "TOOL_VERSION",
    "TASK_VERSION",
    "EVENT_VERSION",
    "TRACE_VERSION",
    "TASK_STATES",
    "MODEL_STATES",
    "STEP_STATES",
    "AgentError",
    "Step",
    "Plan",
    "ModelState",
    "Task",
    "exact_keys",
    "validate_plan",
    "validate_addition_shapes",
]

PLAN_VERSION = "flowagent.plan/v1"
TOOL_VERSION = "flowagent.tools/v1"
TASK_VERSION = "flowagent.task/v1"
EVENT_VERSION = "flowagent.event/v1"
TRACE_VERSION = "flowagent.trace/v1"

TASK_STATES = (
    "created",
    "planning",
    "planned",
    "running",
    "verifying",
    "succeeded",
    "paused",
    "failed",
    "denied",
    "recovery_required",
)

MODEL_STATES = ("not_started", "started", "validated", "failed", "unknown")

STEP_STATES = ("pending", "started", "succeeded", "failed", "denied")

#: Legal task transitions. Anything not listed is rejected by the store, so a
#: crash cannot silently move a task into a state the design never allowed.
#:
#: ``running -> recovery_required`` exists because an unknown model outcome can
#: happen in any round, not only the first. ``recovery_required -> running``
#: exists because reconciliation returns the task to the round it was in.
TASK_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "created": ("created", "planning", "denied"),
    "planning": ("planning", "planned", "failed", "recovery_required"),
    "planned": ("planned", "running", "denied"),
    "running": ("running", "paused", "verifying", "failed", "denied", "recovery_required"),
    "paused": ("paused", "running", "denied"),
    "verifying": ("verifying", "succeeded", "failed", "denied"),
    "failed": ("failed", "running", "denied"),
    "denied": ("denied",),
    "succeeded": ("succeeded",),
    "recovery_required": ("recovery_required", "planned", "running", "failed"),
}

#: Each round is a fresh call, so ``validated`` may return to ``started``. Without
#: that edge a timeout in round two would have no legal state to move to, and the
#: task would be forced into ``failed`` instead of the honest ``unknown``.
MODEL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "not_started": ("not_started", "started"),
    "started": ("started", "unknown", "validated", "failed"),
    "unknown": ("unknown", "validated", "failed"),
    "validated": ("validated", "started"),
    "failed": ("failed",),
}

_STEP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
#: Proposals stay bounded so a plan event is small and hashable. The original
#: 1 KiB ceiling assumed read-only tools with identifier-sized arguments; the
#: governed general capabilities (write_file) carry authored content, so the
#: bound is re-bounded — not removed — at 96 KiB. The tool schema still caps
#: any single content parameter far below this (see Param.max_length).
_MAX_STEP_ARGUMENT_BYTES = 96 * 1024

#: The proposal envelope. ``version`` and ``steps`` are required; ``additions``
#: is optional and is the only key a Phase A proposal may add. It carries the
#: model's request for extra plan steps — a *request*, validated by the host
#: before anything is persisted (see :mod:`flowagent.runtime.plan_extension`).
_PLAN_REQUIRED_KEYS = ("version", "steps")
_PLAN_ENVELOPE_KEYS = ("version", "steps", "additions", "evidence")

#: What one evidence reference may cite: a claim, and the reading that supports
#: it. The digest is the content hash the observation recorded for that path —
#: a value the model can see in its own compressed context, and one the runtime
#: can look for on the chain. A citation is checked against durable history, not
#: taken on trust; nothing here is checked against the claim's *meaning*.
_EVIDENCE_KEYS = ("claim", "tool", "path", "sha256")
_EVIDENCE_REQUIRED_KEYS = ("claim", "tool", "path", "sha256")
MAX_EVIDENCE_REFS_PER_PROPOSAL = 8
MAX_EVIDENCE_CLAIM_CHARS = 200
MAX_EVIDENCE_PATH_CHARS = 200

#: The fields one proposed todo may carry — the plan contract's own identity
#: plus its predicate. ``status``, ``evidence``, ``required`` and ``reason`` are
#: deliberately absent: those are how a proposal would mark itself done, and the
#: host decides them. A proposal that names one is refused rather than quietly
#: stripped, so the attempt stays visible in the trace.
_ADDITION_KEYS = ("id", "text", "tool", "done_when", "arguments")
_ADDITION_REQUIRED_KEYS = ("id", "text", "tool", "done_when")

#: Bounds on one proposal's additions. Still deliberately small, but enough
#: for a real repair's inspect → edit → test sequence (five steps plus report).
MAX_PLAN_ADDITIONS_PER_PROPOSAL = 8
MAX_PLAN_ADDITION_TEXT_CHARS = 400
MAX_PLAN_ADDITION_DONE_WHEN_CHARS = 200


class AgentError(Exception):
    """A failure carrying a stable machine-readable ``code``.

    Codes are part of the observable contract: the demo prints them, the trace
    stores them, and tests assert on them. ``retryable`` is only True for the
    two cases where re-running the same step is provably safe.
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.details = details or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}" if self.message != self.code else self.code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def exact_keys(value: Any, keys: Iterable[str]) -> bool:
    """Return True when *value* is a dict whose key set is exactly *keys*."""
    expected = set(keys)
    return isinstance(value, dict) and set(value.keys()) == expected


@dataclass(frozen=True)
class Step:
    """One tool call proposed by the model.

    ``arguments`` is validated against the tool schema *and* the host policy.
    Tools accept a small, closed set of parameters; the model can choose values
    but never the tool's identity, effect or scope.
    """

    id: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    result: dict[str, Any] | None = None
    result_hash: str | None = None
    attempts: int = 0
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "status": self.status,
            "result": self.result,
            "result_hash": self.result_hash,
            "attempts": self.attempts,
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Step":
        return cls(
            id=data["id"],
            tool=data["tool"],
            arguments=dict(data.get("arguments") or {}),
            status=data.get("status", "pending"),
            result=data.get("result"),
            result_hash=data.get("result_hash"),
            attempts=int(data.get("attempts", 0)),
            error_code=data.get("error_code"),
        )


@dataclass(frozen=True)
class Plan:
    """A validated proposal: one to six ordered steps.

    ``additions`` carries the proposal's plan-extension request, already checked
    for shape and bounds but **not** for authority — that verdict belongs to the
    host (:mod:`flowagent.runtime.plan_extension`). Keeping it on the validated
    proposal is what puts the request on the chain at ``plan_validated``, so a
    later reader can see what was asked for even when the host refused it.
    """

    version: str
    steps: tuple[Step, ...]
    additions: tuple[dict[str, Any], ...] = ()
    #: Source references the artifact's claims rest on, shape-checked here and
    #: resolved against the durable chain by the loop. Shape is all this layer
    #: can decide: whether a source *exists* is a question about history.
    evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.additions:
            payload["additions"] = [dict(item) for item in self.additions]
        if self.evidence:
            payload["evidence"] = [dict(item) for item in self.evidence]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            version=data["version"],
            steps=tuple(Step.from_dict(item) for item in data["steps"]),
            additions=tuple(dict(item) for item in (data.get("additions") or ())),
            evidence=tuple(dict(item) for item in (data.get("evidence") or ())),
        )


def validate_plan(value: Any) -> Plan:
    """Validate a model proposal and return an immutable :class:`Plan`.

    Rules are intentionally identical to the Node prototype: version must
    match, 1-6 steps, unique snake/kebab ids, a registered-looking tool name,
    a dict of arguments no larger than the canonical 96 KiB bound. Tool
    *existence* is checked by the runtime gate, not here, so an unknown tool
    produces a denial receipt instead of a schema error.

    ``additions`` — the optional plan-extension request — is validated for
    *shape* and bounds here, and for *authority* by the host. The same split the
    steps use: this function says whether a proposal is well formed, never
    whether it is allowed.
    """
    if not isinstance(value, dict) or not (
        set(_PLAN_REQUIRED_KEYS) <= set(value.keys()) <= set(_PLAN_ENVELOPE_KEYS)
    ):
        raise AgentError(
            "PLAN_SCHEMA_INVALID",
            f"plan must contain {list(_PLAN_REQUIRED_KEYS)} and at most {list(_PLAN_ENVELOPE_KEYS)}",
        )
    if value["version"] != PLAN_VERSION:
        raise AgentError("PLAN_SCHEMA_INVALID", f"unsupported plan version {value['version']!r}")
    steps = value["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= 6:
        raise AgentError("PLAN_SCHEMA_INVALID", "plan must contain 1-6 steps")

    seen: set[str] = set()
    validated: list[Step] = []
    for raw in steps:
        if not exact_keys(raw, ("id", "tool", "arguments")):
            raise AgentError("PLAN_SCHEMA_INVALID", "each step must contain exactly id, tool, arguments")
        step_id, tool, arguments = raw["id"], raw["tool"], raw["arguments"]
        if not isinstance(step_id, str) or not _STEP_ID.match(step_id) or step_id in seen:
            raise AgentError("PLAN_SCHEMA_INVALID", f"invalid or duplicate step id {step_id!r}")
        if not isinstance(tool, str) or not _TOOL_NAME.match(tool):
            raise AgentError("PLAN_SCHEMA_INVALID", f"invalid tool name {tool!r}")
        if not isinstance(arguments, dict):
            raise AgentError("PLAN_SCHEMA_INVALID", "step arguments must be an object")
        from .hashing import canonical_json

        if len(canonical_json(arguments).encode("utf-8")) > _MAX_STEP_ARGUMENT_BYTES:
            raise AgentError("PLAN_SCHEMA_INVALID", "step arguments exceed 96 KiB")
        seen.add(step_id)
        validated.append(Step(id=step_id, tool=tool, arguments=dict(arguments)))
    if "additions" in value:
        additions = validate_addition_shapes(value["additions"], known_ids=seen)
        evidence = (
            tuple(validate_evidence_shapes(value["evidence"])) if "evidence" in value else ()
        )
        return Plan(version=PLAN_VERSION, steps=tuple(validated),
                    additions=tuple(additions), evidence=evidence)
    if "evidence" in value:
        return Plan(version=PLAN_VERSION, steps=tuple(validated),
                    evidence=tuple(validate_evidence_shapes(value["evidence"])))
    return Plan(version=PLAN_VERSION, steps=tuple(validated))


def validate_evidence_shapes(value: Any) -> list[dict[str, Any]]:
    """Check the *shape* of a proposal's source references.

    An empty list means "no sources cited", which is a legitimate answer and is
    reported as such rather than treated as a violation: a summary nothing
    factual rests on is not a crime, it is an unverified claim, and the labels
    say so. What is refused here is a reference that cannot be looked up at all.
    """
    if not isinstance(value, list):
        raise AgentError("EVIDENCE_INVALID", "evidence must be a list of references")
    if len(value) > MAX_EVIDENCE_REFS_PER_PROPOSAL:
        raise AgentError(
            "EVIDENCE_INVALID",
            f"evidence holds at most {MAX_EVIDENCE_REFS_PER_PROPOSAL} references, got {len(value)}",
        )
    shape: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        where = f"evidence[{index}]"
        if not isinstance(raw, dict):
            raise AgentError("EVIDENCE_INVALID", f"{where} must be an object")
        unknown = set(raw) - set(_EVIDENCE_KEYS)
        if unknown:
            raise AgentError(
                "EVIDENCE_INVALID", f"{where} may not set {sorted(unknown)}; allowed: {list(_EVIDENCE_KEYS)}",
            )
        missing = set(_EVIDENCE_REQUIRED_KEYS) - set(raw)
        if missing:
            raise AgentError("EVIDENCE_INVALID", f"{where} is missing {sorted(missing)}")
        claim = raw["claim"]
        if not isinstance(claim, str) or not claim.strip() or len(claim) > MAX_EVIDENCE_CLAIM_CHARS:
            raise AgentError(
                "EVIDENCE_INVALID",
                f"{where}.claim must be a non-empty string of at most {MAX_EVIDENCE_CLAIM_CHARS} characters",
            )
        tool = raw["tool"]
        if not isinstance(tool, str) or not _TOOL_NAME.match(tool):
            raise AgentError("EVIDENCE_INVALID", f"{where}.tool {tool!r} is not a valid tool name")
        path = raw["path"]
        if not isinstance(path, str) or not path.strip() or len(path) > MAX_EVIDENCE_PATH_CHARS:
            raise AgentError(
                "EVIDENCE_INVALID",
                f"{where}.path must be a workspace-relative path of at most {MAX_EVIDENCE_PATH_CHARS} characters",
            )
        digest = raw["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AgentError("EVIDENCE_INVALID", f"{where}.sha256 must be a 64-character hex digest")
        shape.append({
            "claim": claim.strip(),
            "tool": tool,
            "path": path.strip(),
            "sha256": digest,
        })
    return shape


def validate_addition_shapes(value: Any, *, known_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Check the *shape* of a proposal's plan-extension request.

    Returns the entries as plain dicts for the host to judge. Every failure is a
    ``PLAN_ADDITION_INVALID`` with the offending entry named, because the model
    is expected to correct it on the next round rather than have the run die: a
    refused extension is a rejection, not a governance event.
    """
    if not isinstance(value, list):
        raise AgentError("PLAN_ADDITION_INVALID", "additions must be a list of todo objects")
    if not value:
        # "Nothing to add" is a legitimate answer, not a malformed one: a model
        # that emits an empty list is declining to extend, and refusing the whole
        # proposal over it would fail a run for saying so.
        return []
    if len(value) > MAX_PLAN_ADDITIONS_PER_PROPOSAL:
        raise AgentError(
            "PLAN_ADDITION_INVALID",
            f"additions holds at most {MAX_PLAN_ADDITIONS_PER_PROPOSAL} todos, got {len(value)}",
        )
    seen = set(known_ids)
    shape: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        where = f"additions[{index}]"
        if not isinstance(raw, dict):
            raise AgentError("PLAN_ADDITION_INVALID", f"{where} must be an object")
        present = set(raw.keys())
        forbidden = present - set(_ADDITION_KEYS)
        if forbidden:
            # Naming a host-owned field (`status`, `evidence`, `required`, …) is
            # an attempt to write the part of a todo the proposer does not own.
            raise AgentError(
                "PLAN_ADDITION_INVALID",
                f"{where} may not set {sorted(forbidden)}; allowed: {list(_ADDITION_KEYS)}",
            )
        missing = set(_ADDITION_REQUIRED_KEYS) - present
        if missing:
            raise AgentError("PLAN_ADDITION_INVALID", f"{where} is missing {sorted(missing)}")
        todo_id = raw["id"]
        if not isinstance(todo_id, str) or not _STEP_ID.match(todo_id) or todo_id in seen:
            raise AgentError(
                "PLAN_ADDITION_INVALID",
                f"{where}.id {todo_id!r} is invalid, reserved or already used",
            )
        text = raw["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PLAN_ADDITION_TEXT_CHARS:
            raise AgentError(
                "PLAN_ADDITION_INVALID",
                f"{where}.text must be a non-empty string of at most {MAX_PLAN_ADDITION_TEXT_CHARS} characters",
            )
        tool = raw["tool"]
        if not isinstance(tool, str) or not _TOOL_NAME.match(tool):
            raise AgentError("PLAN_ADDITION_INVALID", f"{where}.tool {tool!r} is not a valid tool name")
        done_when = raw["done_when"]
        if (
            not isinstance(done_when, str)
            or not done_when.strip()
            or len(done_when) > MAX_PLAN_ADDITION_DONE_WHEN_CHARS
        ):
            raise AgentError(
                "PLAN_ADDITION_INVALID",
                f"{where}.done_when must be a non-empty string of at most "
                f"{MAX_PLAN_ADDITION_DONE_WHEN_CHARS} characters",
            )
        arguments = raw.get("arguments", {})
        if not isinstance(arguments, dict):
            raise AgentError("PLAN_ADDITION_INVALID", f"{where}.arguments must be an object")
        from .hashing import canonical_json

        if len(canonical_json(arguments).encode("utf-8")) > _MAX_STEP_ARGUMENT_BYTES:
            raise AgentError("PLAN_ADDITION_INVALID", f"{where}.arguments exceed 96 KiB")
        seen.add(todo_id)
        shape.append({
            "id": todo_id,
            "text": text.strip(),
            "tool": tool,
            "done_when": done_when.strip(),
            "arguments": dict(arguments),
        })
    return shape


@dataclass
class ModelState:
    """What the runtime knows about the model call, independent of the model."""

    status: str = "not_started"
    attempts: int = 0
    proposal: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    receipt_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "proposal": self.proposal,
            "receipt": self.receipt,
            "receipt_ref": self.receipt_ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelState":
        return cls(
            status=data.get("status", "not_started"),
            attempts=int(data.get("attempts", 0)),
            proposal=data.get("proposal"),
            receipt=data.get("receipt"),
            receipt_ref=data.get("receipt_ref"),
        )


@dataclass
class Task:
    """The durable body of one research task.

    ``sequence`` is the number of committed events. It is the optimistic
    concurrency token: every write must present ``expected_sequence`` and the
    store refuses anything else.
    """

    id: str
    status: str = "created"
    sequence: int = 0
    version: str = TASK_VERSION
    principal: str = ""
    objective: str = ""
    binding: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    model_grant: dict[str, Any] = field(default_factory=dict)
    model: ModelState = field(default_factory=ModelState)
    plan: Plan | None = None
    #: Durable snapshot of the agent's current plan (see core/plan.py). Kept as
    #: a plain dict so the contract layer does not import the plan module and
    #: create a cycle; the loop owns the typed view.
    plan_state: dict[str, Any] | None = None
    steps: list[Step] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "sequence": self.sequence,
            "version": self.version,
            "principal": self.principal,
            "objective": self.objective,
            "binding": self.binding,
            "allowed_tools": list(self.allowed_tools),
            "model_grant": self.model_grant,
            "model": self.model.to_dict(),
            "plan": self.plan.to_dict() if self.plan else None,
            "plan_state": self.plan_state,
            "steps": [step.to_dict() for step in self.steps],
            "failure": self.failure,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        plan = Plan.from_dict(data["plan"]) if data.get("plan") else None
        return cls(
            id=data["id"],
            status=data["status"],
            sequence=int(data["sequence"]),
            version=data.get("version", TASK_VERSION),
            principal=data.get("principal", ""),
            objective=data.get("objective", ""),
            binding=dict(data.get("binding") or {}),
            allowed_tools=list(data.get("allowed_tools") or []),
            model_grant=dict(data.get("model_grant") or {}),
            model=ModelState.from_dict(data.get("model") or {}),
            plan=plan,
            plan_state=data.get("plan_state"),
            steps=[Step.from_dict(item) for item in data.get("steps") or []],
            failure=data.get("failure"),
            result=data.get("result"),
        )

    def advance(self, status: str) -> "Task":
        """Return a copy moved to *status*, rejecting illegal transitions."""
        allowed = TASK_TRANSITIONS.get(self.status, ())
        if status not in allowed:
            raise AgentError(
                "ILLEGAL_STATE_TRANSITION",
                f"cannot move task from {self.status!r} to {status!r}",
            )
        return replace(self, status=status)

    def with_model(self, **changes: Any) -> "Task":
        """Return a copy with model fields updated, rejecting illegal transitions."""
        target = changes.get("status")
        if target is not None:
            allowed = MODEL_TRANSITIONS.get(self.model.status, ())
            if target not in allowed:
                raise AgentError(
                    "ILLEGAL_MODEL_TRANSITION",
                    f"cannot move model from {self.model.status!r} to {target!r}",
                )
        updated = replace(self.model, **changes)
        return replace(self, model=updated)

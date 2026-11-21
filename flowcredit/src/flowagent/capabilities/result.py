"""The typed result a capability returns to its parent agent.

The parent reads exactly this object. Internal stage/check history lives in the
journal (``CapabilityResult.journal``) and is addressed by ``child_run_id``; it
is intentionally excluded from ``to_dict()`` so a capability cannot flood the
conversation with its own trace.

Every number is an integer: the parent may commit this into the run's event
chain, whose canonical JSON rejects floats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["CapabilityResult"]

_SUCCEEDED = "succeeded"
_FAILED = "failed"
_CANCELLED = "cancelled"
_STATUSES = (_SUCCEEDED, _FAILED, _CANCELLED)


@dataclass(frozen=True)
class CapabilityResult:
    """One capability invocation's answer."""

    kind: str
    status: str
    stage: str
    artifact: dict[str, Any]
    quality: dict[str, Any]
    required_fixes: list[Any]
    budget: dict[str, int]
    reason: str = ""
    #: Compatibility alias for the old stage-specific reason (e.g. NO_DRAFT).
    #: The primary ``reason`` is now a control-plane failure code.
    legacy_reason: str = ""
    #: The last bounded gate-repair ticket, if this run was rejected at least once.
    repair_ticket: dict[str, Any] | None = None
    child_run_id: str = ""
    #: Internal stage/check history, keyed for the parent by child_run_id.
    journal: tuple[dict[str, Any], ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError(f"unknown capability status {self.status!r}")
        for key in (
            "calls_used", "model_calls", "attempts", "valid_actions",
            "valid_observations", "repairs", "tokens_used", "cost_microusd",
        ):
            value = self.budget.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"budget.{key} must be a non-negative integer")

    @property
    def succeeded(self) -> bool:
        return self.status == _SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        """The parent-facing view. No journal, no bytes, no floats."""
        payload = {
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "artifact": {
                "path": str(self.artifact.get("path") or ""),
                "sha256": str(self.artifact.get("sha256") or ""),
                "bytes": int(self.artifact.get("bytes") or 0),
            },
            "quality": dict(self.quality),
            "required_fixes": list(self.required_fixes),
            "budget": {
                "calls_used": int(self.budget["calls_used"]),
                "model_calls": int(self.budget.get("model_calls", self.budget["calls_used"])),
                "attempts": int(self.budget.get("attempts", 0)),
                "valid_actions": int(self.budget.get("valid_actions", 0)),
                "valid_observations": int(self.budget.get("valid_observations", 0)),
                "repairs": int(self.budget.get("repairs", 0)),
                "tokens_used": int(self.budget["tokens_used"]),
                "cost_microusd": int(self.budget["cost_microusd"]),
            },
            "reason": self.reason,
            "child_run_id": self.child_run_id,
        }
        if self.legacy_reason:
            payload["legacy_reason"] = self.legacy_reason
        if self.repair_ticket is not None:
            payload["repair_ticket"] = dict(self.repair_ticket)
        return payload

    @classmethod
    def succeeded_result(
        cls,
        *,
        kind: str,
        stage: str,
        artifact: dict[str, Any],
        quality: dict[str, Any],
        budget: dict[str, int],
        child_run_id: str,
        journal: tuple[dict[str, Any], ...],
    ) -> "CapabilityResult":
        return cls(
            kind=kind, status=_SUCCEEDED, stage=stage, artifact=artifact,
            quality=quality, required_fixes=[], budget=budget, reason="",
            child_run_id=child_run_id, journal=journal,
        )

    @classmethod
    def failed_result(
        cls,
        *,
        kind: str,
        stage: str,
        artifact: dict[str, Any],
        quality: dict[str, Any],
        required_fixes: list[Any],
        budget: dict[str, int],
        reason: str,
        child_run_id: str,
        journal: tuple[dict[str, Any], ...],
        legacy_reason: str = "",
        repair_ticket: dict[str, Any] | None = None,
    ) -> "CapabilityResult":
        return cls(
            kind=kind, status=_FAILED, stage=stage, artifact=artifact,
            quality=quality, required_fixes=list(required_fixes), budget=budget,
            reason=reason, legacy_reason=legacy_reason,
            repair_ticket=repair_ticket, child_run_id=child_run_id, journal=journal,
        )

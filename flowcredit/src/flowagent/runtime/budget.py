"""Hard budget: charged before the work, never refunded on doubt.

The interesting property is *when* the charge happens. A call is charged before
it is dispatched, not after it returns, because the failure mode that matters is
a call that was sent and whose result is unknown. Charging afterwards would make
"unknown" free, and a free unknown is an invitation to retry until the budget
runs out.

Phase 1 uses a deterministic mock model, so the numbers are synthetic. The
mechanism is not: the counters live in SQLite next to the task, and they are
read back by the trace so the demo can prove the budget was actually consumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..core.contracts import AgentError
from ..core.store import TaskStore

__all__ = ["BudgetLimits", "Budget", "BUDGET_EXCEEDED", "BudgetAdjustment", "plan_adjustment", "replay_limits"]


BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


@dataclass(frozen=True)
class BudgetLimits:
    """The ceiling for one task scope."""

    max_calls: int
    max_reads: int
    max_cost_microusd: int
    max_tokens: int

    def to_dict(self) -> dict[str, int]:
        return {
            "max_calls": self.max_calls,
            "max_reads": self.max_reads,
            "max_cost_microusd": self.max_cost_microusd,
            "max_tokens": self.max_tokens,
        }


class Budget:
    """Charges a task scope and refuses to go past its limits."""

    def __init__(self, store: TaskStore, scope: str, limits: BudgetLimits) -> None:
        self._store = store
        self._scope = scope
        self._limits = limits

    @property
    def scope(self) -> str:
        return self._scope

    def snapshot(self) -> dict[str, Any]:
        """Return used, remaining and limits for display and for the trace."""
        row = self._store.budget_row(self._scope)
        over_limit = []
        if row["cost_used"] > self._limits.max_cost_microusd:
            over_limit.append("cost")
        if row["tokens_used"] > self._limits.max_tokens:
            over_limit.append("tokens")
        snapshot = {
            "calls_used": row["calls_used"],
            "reads_used": row["reads_used"],
            "cost_used_microusd": row["cost_used"],
            "tokens_used": row["tokens_used"],
            "calls_remaining": max(0, self._limits.max_calls - row["calls_used"]),
            "reads_remaining": max(0, self._limits.max_reads - row["reads_used"]),
            "cost_remaining_microusd": max(0, self._limits.max_cost_microusd - row["cost_used"]),
            "limits": self._limits.to_dict(),
        }
        # Keep the established prompt/trace shape byte-identical in the normal
        # case; overrun diagnostics appear only when there is something to say.
        if over_limit:
            snapshot.update({
                "cost_over_limit_microusd": max(0, row["cost_used"] - self._limits.max_cost_microusd),
                "tokens_over_limit": max(0, row["tokens_used"] - self._limits.max_tokens),
                "over_limit": over_limit,
            })
        return snapshot

    def charge_call(self, *, tokens: int, cost_microusd: int) -> None:
        """Reserve and immediately consume one model call.

        Called *before* the provider is asked, so a provider that hangs or
        disappears still costs a call. That is the whole point. A refusal here
        proves the Provider request was never sent; the diagnostics say exactly
        which dimensions could not be reserved.
        """
        with self._store.transaction():
            row = self._store.budget_row(self._scope)
            blocked = []
            if row["calls_used"] + 1 > self._limits.max_calls:
                blocked.append(self._blocked_dimension(
                    "calls", self._limits.max_calls, row["calls_used"], 1,
                ))
            if row["cost_used"] + cost_microusd > self._limits.max_cost_microusd:
                blocked.append(self._blocked_dimension(
                    "cost_microusd", self._limits.max_cost_microusd,
                    row["cost_used"], cost_microusd,
                ))
            if row["tokens_used"] + tokens > self._limits.max_tokens:
                blocked.append(self._blocked_dimension(
                    "tokens", self._limits.max_tokens, row["tokens_used"], tokens,
                ))
            if blocked:
                first = blocked[0]
                legacy_dimension = {
                    "cost_microusd": "cost",
                }.get(str(first["dimension"]), str(first["dimension"]))
                display = {
                    "calls": "call",
                    "tokens": "token",
                    "cost_microusd": "cost",
                }.get(str(first["dimension"]), str(first["dimension"]))
                raise AgentError(
                    BUDGET_EXCEEDED,
                    f"{display} budget exhausted "
                    f"({first['used']}/{first['limit']}; "
                    f"next call needs {first['requested_reserve']})",
                    details={
                        "stage": "model_call",
                        "provider_request_sent": False,
                        # Established single-dimension fields kept for existing
                        # readers; ``blocked_dimensions`` carries the full picture.
                        "dimension": legacy_dimension,
                        "limit": first["limit"],
                        "used": first["used"],
                        "remaining": first["remaining"],
                        "requested_reserve": first["requested_reserve"],
                        "blocked_dimensions": blocked,
                    },
                )
            self._store.budget_add(self._scope, calls=1, cost=cost_microusd, tokens=tokens)

    @staticmethod
    def _blocked_dimension(dimension: str, limit: int, used: int, reserve: int) -> dict[str, int | str]:
        remaining = max(0, int(limit) - int(used))
        return {
            "dimension": dimension,
            "limit": int(limit),
            "used": int(used),
            "remaining": remaining,
            "requested_reserve": int(reserve),
            "deficit": max(0, int(reserve) - remaining),
        }

    def settle_call(
        self,
        *,
        reserved_tokens: int,
        reserved_cost_microusd: int,
        actual_tokens: int,
        actual_cost_microusd: int,
    ) -> None:
        """Replace one answered call's reservation with its reported usage.

        An unknown call has no actual response and is never settled, so its
        reservation remains consumed. An answered call may exceed the original
        ceiling; record that overrun honestly and let the next charge refuse
        further work instead of pretending an already-sent call can be undone.
        """
        values = (reserved_tokens, reserved_cost_microusd, actual_tokens, actual_cost_microusd)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise AgentError(
                "PROVIDER_USAGE_INVALID",
                "reserved and actual token/cost usage must be non-negative integers",
            )
        with self._store.transaction():
            self._store.budget_add(
                self._scope,
                cost=actual_cost_microusd - reserved_cost_microusd,
                tokens=actual_tokens - reserved_tokens,
            )

    def charge_read(self) -> None:
        """Reserve one tool read before the tool is executed."""
        with self._store.transaction():
            row = self._store.budget_row(self._scope)
            if row["reads_used"] + 1 > self._limits.max_reads:
                raise AgentError(
                    BUDGET_EXCEEDED,
                    f"read budget exhausted ({row['reads_used']}/{self._limits.max_reads})",
                    details={"dimension": "reads"},
                )
            self._store.budget_add(self._scope, reads=1)


# ---------------------------------------------------------------------------
# S1: one adjustment policy for every path (model tool / operator / resume).

DIMENSIONS = ("calls", "reads", "cost_microusd", "tokens")
_FIELD = {
    "calls": "max_calls",
    "reads": "max_reads",
    "cost_microusd": "max_cost_microusd",
    "tokens": "max_tokens",
}


@dataclass(frozen=True)
class BudgetAdjustment:
    """A host-computed, bounded budget change. Applying it is a separate step."""

    before: BudgetLimits
    after: BudgetLimits
    requested: dict[str, int]
    granted: dict[str, int]
    capped: dict[str, bool]
    exhausted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "requested": dict(self.requested),
            "granted": dict(self.granted),
            "capped": dict(self.capped),
            "exhausted": self.exhausted,
        }


def _limits_values(limits: BudgetLimits) -> dict[str, int]:
    return {name: int(getattr(limits, _FIELD[name])) for name in DIMENSIONS}


def _build_limits(values: Mapping[str, int]) -> BudgetLimits:
    return BudgetLimits(**{_FIELD[name]: int(values[name]) for name in DIMENSIONS})


def _clean_amounts(values: Mapping[str, Any] | None, *, name: str) -> dict[str, int]:
    clean = {dimension: 0 for dimension in DIMENSIONS}
    for key, raw in dict(values or {}).items():
        if key not in clean:
            raise AgentError("BUDGET_EXTENSION_INVALID", f"unknown budget dimension {key!r}")
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise AgentError("BUDGET_EXTENSION_INVALID", f"{name}.{key} must be an integer")
        if raw < 0:
            raise AgentError("BUDGET_EXTENSION_INVALID", f"{name}.{key} must be non-negative")
        clean[key] = int(raw)
    return clean


def plan_adjustment(
    base: BudgetLimits,
    current: BudgetLimits,
    *,
    delta: Mapping[str, Any] | None = None,
    total: Mapping[str, Any] | None = None,
    allow_zero: bool = False,
) -> BudgetAdjustment:
    """Plan one bounded adjustment.

    The immutable ``base`` (the envelope after price scaling at run creation)
    is the only cap reference: one request may add at most half of each base
    dimension, and cumulative grants may reach at most the base again (2x).
    ``delta`` adds to the current limits; ``total`` names the desired absolute
    limits and is converted to a delta (raise-only). Both are integers.
    """
    if delta is not None and total is not None:
        raise AgentError("BUDGET_EXTENSION_INVALID", "pass delta or total, not both")
    base_values = _limits_values(base)
    current_values = _limits_values(current)
    for dimension in DIMENSIONS:
        if current_values[dimension] < base_values[dimension]:
            raise AgentError(
                "BUDGET_BASE_INVALID",
                f"current {dimension} is below the immutable base; refusing to adjust",
            )
    if total is not None:
        wanted = _clean_amounts(total, name="total")
        requested = {}
        for dimension in DIMENSIONS:
            if wanted[dimension] == 0 and dimension not in dict(total):
                requested[dimension] = 0
                continue
            if wanted[dimension] < current_values[dimension]:
                raise AgentError(
                    "BUDGET_RAISE_ONLY",
                    f"{dimension} can only be raised (current {current_values[dimension]})",
                )
            requested[dimension] = wanted[dimension] - current_values[dimension]
    else:
        requested = _clean_amounts(delta, name="delta")
    if not any(requested.values()) and not allow_zero:
        raise AgentError(
            "BUDGET_EXTENSION_INVALID",
            "request at least one budget dimension",
        )

    granted: dict[str, int] = {}
    capped: dict[str, bool] = {}
    for dimension in DIMENSIONS:
        base_value = base_values[dimension]
        headroom = max(0, 2 * base_value - current_values[dimension])
        allowed = min(base_value // 2, headroom)
        granted[dimension] = min(requested[dimension], allowed)
        capped[dimension] = granted[dimension] < requested[dimension]
    exhausted = sum(requested.values()) > 0 and sum(granted.values()) == 0
    after_values = {
        dimension: current_values[dimension] + granted[dimension]
        for dimension in DIMENSIONS
    }
    return BudgetAdjustment(
        before=current,
        after=_build_limits(after_values),
        requested=requested,
        granted=granted,
        capped=capped,
        exhausted=exhausted,
    )


def _parse_limits(raw: Any, *, where: str) -> BudgetLimits:
    if not isinstance(raw, dict):
        raise AgentError("BUDGET_CHAIN_INVALID", f"{where}: limits must be an object")
    values: dict[str, int] = {}
    for dimension in DIMENSIONS:
        value = raw.get(dimension)
        if value is None:
            value = raw.get(_FIELD[dimension])
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AgentError(
                "BUDGET_CHAIN_INVALID",
                f"{where}: {dimension} must be a non-negative integer",
            )
        values[dimension] = int(value)
    return _build_limits(values)


def replay_limits(
    base: BudgetLimits,
    events: list[dict[str, Any]],
    *,
    allow_legacy_resume: bool = False,
) -> tuple[BudgetLimits, list[dict[str, Any]]]:
    """Rebuild the effective limits from the committed chain, in sequence order.

    New events (``policy_version == 2``) must satisfy ``before == effective`` and
    ``after == before + granted``; any mismatch is a chain error, not a new
    source of truth. Legacy events are accepted under one explicit rule: an
    older ``budget_adjusted`` may update limits raise-only, clamped to the 2x
    cap; a legacy ``task_resumed.limits`` is adopted once only when the run
    predates the base binding and no adjustment exists yet. Nothing is ever
    widened to match usage.
    """
    effective = base
    base_values = _limits_values(base)
    seen_requests: dict[str, dict[str, int]] = {}
    diagnostics: list[dict[str, Any]] = []
    adjusted = False
    for event in sorted(events, key=lambda item: int(item.get("sequence") or 0)):
        kind = event.get("type")
        detail = ((event.get("payload") or {}).get("detail") or {})
        if kind == "budget_adjusted":
            request_id = str(detail.get("request_id") or "")
            after = _parse_limits(detail.get("after"), where="budget_adjusted.after")
            after_values = _limits_values(after)
            if request_id and request_id in seen_requests:
                if seen_requests[request_id] != after_values:
                    raise AgentError(
                        "BUDGET_CHAIN_INVALID",
                        f"request_id {request_id!r} was granted two different limits",
                    )
                continue
            if int(detail.get("policy_version") or 0) >= 2:
                before = _parse_limits(detail.get("before"), where="budget_adjusted.before")
                granted = _clean_amounts(detail.get("granted"), name="granted")
                if _limits_values(before) != _limits_values(effective):
                    raise AgentError(
                        "BUDGET_CHAIN_INVALID",
                        "budget_adjusted.before does not match the replayed effective limits",
                    )
                expected = {
                    dimension: _limits_values(before)[dimension] + granted[dimension]
                    for dimension in DIMENSIONS
                }
                if expected != after_values:
                    raise AgentError(
                        "BUDGET_CHAIN_INVALID",
                        "budget_adjusted.after does not equal before + granted",
                    )
                if any(after_values[dimension] > 2 * base_values[dimension]
                       for dimension in DIMENSIONS):
                    raise AgentError(
                        "BUDGET_CHAIN_INVALID",
                        "budget_adjusted exceeds the cumulative 2x cap",
                    )
                effective = after
            else:
                current_values = _limits_values(effective)
                legacy: dict[str, int] = {}
                for dimension in DIMENSIONS:
                    # Legacy events have no granted field; only raise, clamp to 2x.
                    legacy[dimension] = min(
                        max(current_values[dimension], after_values[dimension]),
                        2 * base_values[dimension],
                    )
                if legacy != after_values:
                    diagnostics.append({
                        "event": "budget_adjusted",
                        "sequence": event.get("sequence"),
                        "reason": "legacy adjustment clamped to the raise-only 2x cap",
                    })
                effective = _build_limits(legacy)
            adjusted = True
            if request_id:
                seen_requests[request_id] = after_values
        elif kind == "task_resumed" and allow_legacy_resume and not adjusted:
            raw = detail.get("limits")
            if not isinstance(raw, dict):
                continue
            candidate = _parse_limits(raw, where="task_resumed.limits")
            candidate_values = _limits_values(candidate)
            current_values = _limits_values(effective)
            legacy = {
                dimension: min(
                    max(current_values[dimension], candidate_values[dimension]),
                    2 * base_values[dimension],
                )
                for dimension in DIMENSIONS
            }
            effective = _build_limits(legacy)
            diagnostics.append({
                "event": "task_resumed",
                "sequence": event.get("sequence"),
                "reason": "legacy resume limits adopted once (pre base-binding run)",
            })
    return effective, diagnostics


def find_budget_request(events: list[dict[str, Any]], request_id: str) -> dict[str, Any] | None:
    """Return the committed budget_adjusted detail for one request_id."""
    needle = str(request_id or "")
    if not needle:
        return None
    for event in sorted(events, key=lambda item: int(item.get("sequence") or 0)):
        if event.get("type") != "budget_adjusted":
            continue
        detail = ((event.get("payload") or {}).get("detail") or {})
        if str(detail.get("request_id") or "") == needle:
            return detail
    return None

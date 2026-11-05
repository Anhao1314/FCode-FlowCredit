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
from typing import Any

from ..core.contracts import AgentError
from ..core.store import TaskStore

__all__ = ["BudgetLimits", "Budget", "BUDGET_EXCEEDED"]


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
        disappears still costs a call. That is the whole point.
        """
        with self._store.transaction():
            row = self._store.budget_row(self._scope)
            if row["calls_used"] + 1 > self._limits.max_calls:
                raise AgentError(
                    BUDGET_EXCEEDED,
                    f"call budget exhausted ({row['calls_used']}/{self._limits.max_calls})",
                    details={"dimension": "calls"},
                )
            if row["cost_used"] + cost_microusd > self._limits.max_cost_microusd:
                raise AgentError(
                    BUDGET_EXCEEDED,
                    f"cost budget exhausted ({row['cost_used']}/{self._limits.max_cost_microusd} µUSD)",
                    details={"dimension": "cost"},
                )
            if row["tokens_used"] + tokens > self._limits.max_tokens:
                raise AgentError(
                    BUDGET_EXCEEDED,
                    f"token budget exhausted ({row['tokens_used']}/{self._limits.max_tokens})",
                    details={"dimension": "tokens"},
                )
            self._store.budget_add(self._scope, calls=1, cost=cost_microusd, tokens=tokens)

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

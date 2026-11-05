"""Provider interface: a model is a proposal source, never an executor.

The contract is deliberately narrow. A provider receives a context it did not
choose, returns a plan it cannot execute, and is charged before it is called.
There is no method here that touches a tool, a file or a budget directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["Proposal", "Provider"]


@dataclass(frozen=True)
class Proposal:
    """One model response, plus the receipt the runtime will keep.

    ``tokens`` and ``cost_microusd`` are *reported* by the provider. In Phase 1
    they come from a deterministic mock, so they are exact. Against a real
    provider they would be an estimate, which is why the receipt marks external
    billing as unverified rather than pretending otherwise.
    """

    plan: dict[str, Any]
    provider: str
    model: str
    tokens: int
    cost_microusd: int
    latency_ms: int = 0
    #: Which todo the model claims this action advances. Recorded in the trace so
    #: an action that advances nothing is visible rather than absorbed.
    todo_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def receipt(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "tokens": self.tokens,
            "cost_microusd": self.cost_microusd,
            "latency_ms": self.latency_ms,
            "external_billing": "not-verified",
            "todo_id": self.todo_id,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class Provider(Protocol):
    """Anything that can turn a context into a validated-shaped proposal."""

    name: str
    model: str

    def propose(self, context: dict[str, Any]) -> Proposal:
        """Return one proposal for the given context."""
        ...

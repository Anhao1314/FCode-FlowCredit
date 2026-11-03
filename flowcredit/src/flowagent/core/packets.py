"""Executive–Policy wire types: the decision packet and the model's choice.

These are the only objects that cross the provider boundary in packet mode.
They live in ``core`` because both sides need to name them: the runtime
compiles a packet, and a provider returns a choice. If they stayed in
``runtime.policy``, every provider would import the runtime just to describe
its own output, inverting the dependency the package is built on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .hashing import canonical_json, digest

__all__ = ["PACKET_VERSION", "ActionCandidate", "ActionChoice", "DecisionPacket"]

PACKET_VERSION = "fcode.packet/v1"


@dataclass(frozen=True)
class ActionCandidate:
    id: str
    todo_id: str
    tool: str
    arguments: dict[str, Any]
    why: str
    effect: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "todo_id": self.todo_id,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "why": self.why,
            "effect": self.effect,
            "version": self.version,
        }


@dataclass(frozen=True)
class ActionChoice:
    candidate_id: str
    reason: str = ""
    tokens: int = 0
    latency_ms: int = 0
    repaired: bool = False
    cost_microusd: int = 0
    request_id: str = ""
    #: Provider provenance (price snapshot, billing mode, usage honesty) that
    #: belongs in the round receipt without becoming model-facing packet state.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionPacket:
    objective: str
    round_no: int
    state: dict[str, Any]
    last_evidence: dict[str, Any] | None
    memory_cards: list[dict[str, Any]]
    budget: dict[str, Any]
    candidates: list[ActionCandidate]
    output: dict[str, Any] = field(default_factory=lambda: {"choice": "candidate id"})
    version: str = PACKET_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "objective": self.objective,
            "round": self.round_no,
            "state": self.state,
            "last_evidence": self.last_evidence,
            "memory_cards": self.memory_cards,
            "budget": self.budget,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "output": self.output,
        }

    @property
    def packet_hash(self) -> str:
        return digest(self.to_dict())

    @property
    def size_bytes(self) -> int:
        return len(canonical_json(self.to_dict()).encode("utf-8"))

    def candidate(self, candidate_id: str) -> ActionCandidate | None:
        for candidate in self.candidates:
            if candidate.id == candidate_id:
                return candidate
        return None
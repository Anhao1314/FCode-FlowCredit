"""Executive–Policy: the host compiles a decision packet, the model chooses.

A small model should not see sixteen tools, the whole plan and six kilobytes of
history. It should see one todo, the current state, one evidence card and a
small menu of legal actions. This module owns that contract.

The compiler is deterministic. The policy is replaceable: MockPolicy today,
LocalPolicy/OraclePolicy tomorrow. The host still validates the choice, gates
the tool, executes and verifies; nothing here can bypass the kernel.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..core.contracts import PLAN_VERSION, AgentError
from ..core.hashing import canonical_json
# Wire types live in core: providers need to name their own output without
# importing the runtime. Re-exported here for the runtime-facing API.
from ..core.packets import PACKET_VERSION, ActionCandidate, ActionChoice, DecisionPacket
from ..providers.base import Proposal

__all__ = [
    "ActionCandidate",
    "ActionChoice",
    "DecisionPacket",
    "MockPolicy",
    "PacketProvider",
    "Policy",
    "compile_packet",
]


class Policy(Protocol):
    name: str
    model: str

    def choose(self, packet: DecisionPacket) -> ActionChoice: ...


class MockPolicy:
    """Deterministic stand-in: take the first legal action."""

    name = "mock-packet-policy"
    model = "rule-first-candidate"

    def choose(self, packet: DecisionPacket) -> ActionChoice:
        if not packet.candidates:
            raise AgentError("POLICY_NO_ACTIONS", "the packet has no legal action")
        return ActionChoice(packet.candidates[0].id)


class PacketProvider:
    """Adapt a Policy to the existing Provider contract.

    This keeps Harness state transitions, budget reservation, receipts and
    validation untouched while the model-facing input becomes a packet.
    """

    def __init__(self, packet: DecisionPacket, policy: Policy) -> None:
        self.packet = packet
        self.policy = policy
        self.name = policy.name
        self.model = policy.model

    @property
    def reserve_tokens(self) -> int:
        """Conservative per-call reservation charged before dispatch."""
        return int(getattr(self.policy, "reserve_tokens", 1_000))

    @property
    def reserve_cost_microusd(self) -> int:
        return int(getattr(self.policy, "reserve_cost_microusd", 500))

    def reserve(self, context: dict[str, Any]) -> tuple[int, int]:
        reserve = getattr(self.policy, "reserve", None)
        if callable(reserve):
            return reserve(self.packet)
        return self.reserve_tokens, self.reserve_cost_microusd

    def propose(self, context: dict[str, Any]) -> Proposal:
        choice = self.policy.choose(self.packet)
        candidate = self.packet.candidate(choice.candidate_id)
        if candidate is None:
            raise AgentError(
                "POLICY_INVALID_CHOICE",
                f"{choice.candidate_id!r} is not in the packet menu",
            )
        plan = {
            "version": PLAN_VERSION,
            "steps": [{
                "id": candidate.todo_id or "stop",
                "tool": candidate.tool,
                "arguments": dict(candidate.arguments),
            }],
        }
        packet_bytes = len(canonical_json(self.packet.to_dict()).encode("utf-8"))
        return Proposal(
            plan=plan,
            provider=self.name,
            model=self.model,
            tokens=choice.tokens or max(1, (packet_bytes + 64) // 4),
            cost_microusd=choice.cost_microusd,
            latency_ms=choice.latency_ms,
            todo_id=candidate.todo_id or None,
            metadata={
                "policy": PACKET_VERSION,
                **dict(choice.metadata),
                "packet_hash": self.packet.packet_hash,
                "candidate_id": candidate.id,
                "reason": choice.reason,
                "repaired": choice.repaired,
                "provider_request_id": choice.request_id,
            },
        )


def _memory_cards(memory: Any) -> list[dict[str, Any]]:
    cards = []
    for hit in (getattr(memory, "prior_cases", None) or [])[:3]:
        cards.append({
            "type": "case",
            "run_id": hit.get("run_id"),
            "objective": hit.get("objective"),
            "outcome": hit.get("outcome"),
            "summary": hit.get("summary"),
            "chain_head": hit.get("chain_head"),
        })
    return cards


def compile_packet(
    *,
    objective: str,
    round_no: int,
    plan: Any,
    memory: Any,
    registry: Any,
    budget: dict[str, Any],
    limit: int = 5,
) -> DecisionPacket:
    """Compile the next decision packet from host-owned state."""
    open_todos = [todo for todo in plan.todos if todo.status in ("pending", "active")]
    state = memory.state()
    blocked = bool(state.get("blockers")) or any(todo.status == "blocked" for todo in plan.todos)
    open_limit = max(0, limit - 1) if blocked else limit
    candidates: list[ActionCandidate] = []
    for index, todo in enumerate(open_todos[:open_limit], start=1):
        effect = ""
        version = ""
        try:
            spec = registry.get(todo.tool)
            effect = spec.effect
            version = spec.version
        except Exception:  # noqa: BLE001 — the packet still shows the plan step
            pass
        candidates.append(ActionCandidate(
            id=f"a{index}",
            todo_id=todo.id,
            tool=todo.tool,
            arguments=dict(todo.arguments or {}),
            why=todo.text,
            effect=effect,
            version=version,
        ))

    if blocked and len(candidates) < limit:
        candidates.append(ActionCandidate(
            id=f"a{len(candidates) + 1}",
            todo_id="stop",
            tool="report_blocked",
            arguments={"reason": "insufficient_evidence"},
            why="已经出现阻塞；如实停止，不编造结论",
            effect="read",
        ))

    return DecisionPacket(
        objective=objective,
        round_no=round_no,
        state=state,
        last_evidence=memory.last_evidence(),
        memory_cards=_memory_cards(memory),
        budget=budget,
        candidates=candidates,
    )

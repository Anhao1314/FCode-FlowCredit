"""Recording: turn one run into a structured, self-verifying trace.

The recorder is an observer, not a participant. It sees proposals, decisions and
observations; it never influences them. That separation is the reason a bug in
the recorder cannot corrupt a completed task — the worst it can do is fail to
produce a trace, and the task remains valid.

The trace carries the raw event chain, so a reader can recompute every hash
without trusting this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.contracts import TRACE_VERSION, Task
from ..core.hashing import digest
from ..core.store import TaskStore
from ..tools.base import Observation

__all__ = ["RoundRecord", "TraceRecorder"]


def _surfaced_metadata(rounds: list[RoundRecord]) -> dict[str, Any]:
    """Lift audit disclosures from round receipts to the trace surface.

    A disclosure buried only in a round receipt is still auditable, but readers
    should not have to know where to look for an active fault injection. Keep
    this surface intentionally tiny: it states that an experiment is active, not
    a second copy of every provider field.
    """
    metadata: dict[str, Any] = {}
    for record in rounds:
        receipt = record.receipt or {}
        provider_metadata = receipt.get("metadata") or {}
        if provider_metadata.get("fault_injection") is True:
            metadata["fault_injection"] = True
        disclosure = provider_metadata.get("disclosure")
        if isinstance(disclosure, str) and disclosure:
            metadata["disclosure"] = disclosure
    return metadata


@dataclass
class RoundRecord:
    """Everything that happened in one round, in the order it happened."""

    index: int
    budget_before: dict[str, Any] = field(default_factory=dict)
    proposal: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    budget_after: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] | None = None
    plan_after: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "budget_before": self.budget_before,
            "proposal": self.proposal,
            "receipt": self.receipt,
            "decision": self.decision,
            "observation": self.observation,
            "budget_after": self.budget_after,
            "plan": self.plan,
            "plan_after": self.plan_after,
        }


class TraceRecorder:
    """Accumulates a trace while a loop runs."""

    def __init__(self, store: TaskStore, *, provider_name: str, model_name: str) -> None:
        self._store = store
        self._provider_name = provider_name
        self._model_name = model_name
        self._rounds: list[RoundRecord] = []
        self._terminal: dict[str, Any] | None = None

    # ------------------------------------------------------------- observer API

    def seed(self, rounds: list[dict[str, Any]]) -> None:
        """Prepend rounds recorded before this process started (resume).

        The event chain is enough to verify a run, but not enough to rebuild the
        signed proposals and receipts the round cards show. The previous trace
        already holds them; carrying it forward keeps the resumed run one story
        instead of two unrelated fragments.
        """
        self._rounds = [RoundRecord(**round) for round in rounds] + self._rounds

    def round_start(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        record = RoundRecord(index=round_no, budget_before=dict(budget))
        record.plan = plan.to_dict() if plan is not None else None
        self._rounds.append(record)

    def proposal(self, round_no: int, proposal: Any) -> None:
        record = self._current(round_no)
        record.proposal = proposal.plan
        record.receipt = proposal.receipt()

    def gate(self, round_no: int, decision: Any) -> None:
        self._current(round_no).decision = decision.to_dict()

    def observation(self, round_no: int, observation: Observation) -> None:
        self._current(round_no).observation = observation.to_dict()

    def round_end(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        record = self._current(round_no)
        record.budget_after = dict(budget)
        if plan is not None:
            record.plan_after = plan.to_dict()

    def terminal(self, task: Task, result: dict[str, Any] | None) -> None:
        self._terminal = {
            "status": task.status,
            "failure": task.failure,
            "result": result,
            "sequence": task.sequence,
        }

    # ------------------------------------------------------------------ export

    def build(self, task: Task) -> dict[str, Any]:
        """Assemble the trace document, including the raw event chain."""
        events = self._store.events(task.id)
        chain_head = events[-1]["hash"] if events else None
        trace = {
            "version": TRACE_VERSION,
            "task": {
                "id": task.id,
                "objective": task.objective,
                "principal": task.principal,
                "binding": task.binding,
                "allowed_tools": task.allowed_tools,
                "model_grant": task.model_grant,
                "status": task.status,
                "sequence": task.sequence,
                "failure": task.failure,
            },
            "model": {
                "provider": self._provider_name,
                "model": self._model_name,
                "note": "provider-supplied proposal source; runtime independently verifies results",
            },
            "rounds": [record.to_dict() for record in self._rounds],
            "terminal": self._terminal,
            "metadata": _surfaced_metadata(self._rounds),
            "events": events,
            "chain": {
                "event_count": len(events),
                "head_hash": chain_head,
                "algorithm": "sha256 over canonical json of {sequence,type,payload,prev_hash,at}",
            },
        }
        trace["trace_hash"] = digest(
            {
                "task_id": task.id,
                "sequence": task.sequence,
                "head_hash": chain_head,
                "rounds": len(self._rounds),
            }
        )
        return trace

    def _current(self, round_no: int) -> RoundRecord:
        for record in reversed(self._rounds):
            if record.index == round_no:
                return record
        record = RoundRecord(index=round_no)
        self._rounds.append(record)
        return record

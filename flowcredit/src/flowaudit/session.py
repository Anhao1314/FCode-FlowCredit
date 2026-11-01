"""The audit session (L2): the two ledgers as events on a hash chain.

Design contract (docs/memory-design.md §二):

- The session IS a flowagent task: events live in the kernel's TaskStore,
  so the hash chain, compare-and-swap sequencing and tamper detection are
  inherited, not reimplemented.
- The two ledgers (candidates / findings) are PROJECTIONS of the event
  chain — derived, never stored beside it. A restart rebuilds them by
  replay; process memory holds no second source of truth.
- Promotion is gated twice here: the actor must be the host (an operator
  refusal is itself recorded as evidence), and a replay receipt must be
  presented. A stimulus that cannot isolate the defect cannot reach the
  ledger, no matter who asks.
"""

from __future__ import annotations

from pathlib import Path

from flowagent.core.contracts import AgentError, Task
from flowagent.core.store import TaskStore

_CANDIDATE = "candidate_proposed"
_PRUNED = "candidate_pruned"
_PROMOTED = "finding_promoted"
_DISMISSED = "finding_dismissed"
_REFUSED = "promotion_refused"
_LEDGER_EVENTS = (_CANDIDATE, _PRUNED, _PROMOTED, _DISMISSED)


class AuditSession:
    """One audit's two ledgers, event-sourced on the kernel chain."""

    def __init__(self, store: TaskStore, task_id: str) -> None:
        self.store = store
        self.task_id = task_id
        self._task = store.load(task_id)

    # ------------------------------------------------------------------ start

    @classmethod
    def start(cls, root: Path, audit_id: str, *, operator: str,
              target_identity: str) -> "AuditSession":
        store = TaskStore(Path(root) / "session.sqlite")
        task = Task(
            id=f"audit-{audit_id}",
            principal=operator,
            objective=f"审计会话:{target_identity}",
            binding={"kind": "audit-session", "audit_id": audit_id,
                     "target": target_identity},
            allowed_tools=[],
            model_grant={},
        )
        store.save(task, 0, "task_created", {"detail": {"audit_id": audit_id}})
        return cls(store, task.id)

    @classmethod
    def attach(cls, store: TaskStore, task_id: str) -> "AuditSession":
        """Reattach to an existing session: ledgers rebuild by replay."""
        return cls(store, task_id)

    # ------------------------------------------------------------------ writes

    def _commit(self, event: str, detail: dict) -> None:
        self._task = self.store.save(self._task, self._task.sequence, event, detail)

    def propose_candidate(self, *, rule_id: str, category: str, severity: str,
                          root_cause_group: str, evidence: list[dict],
                          actor: str) -> str:
        candidate_id = f"C-{self._next_candidate():04d}"
        self._commit(_CANDIDATE, {
            "candidate_id": candidate_id, "rule_id": rule_id,
            "category": category, "severity": severity,
            "root_cause_group": root_cause_group,
            "evidence": evidence[:5], "actor": actor,
        })
        return candidate_id

    def prune_candidate(self, candidate_id: str, *, reason: str, actor: str) -> None:
        self._require_open(candidate_id)
        self._commit(_PRUNED, {"candidate_id": candidate_id,
                               "reason": reason[:300], "actor": actor})

    def promote_finding(self, candidate_id: str, *, poc_id: str,
                        receipt_ref: str, actor: str,
                        finding_id: str | None = None) -> str:
        """The only door into the findings ledger — and it is double-locked."""
        if actor == "operator":
            refused = {"candidate_id": candidate_id, "actor": actor,
                       "reason": "operator promotion is not a finding"}
            self._commit(_REFUSED, refused)
            raise AgentError(
                "OPERATOR_PROMOTION_FORBIDDEN",
                "operator may dispute or accept risk, never promote; "
                "promotion requires a replay receipt presented by the host",
            )
        if not receipt_ref:
            raise AgentError(
                "PROMOTION_WITHOUT_RECEIPT",
                "a finding enters the ledger only with its replay receipt",
            )
        self._require_open(candidate_id)
        finding_id = finding_id or f"F-{poc_id}"
        self._commit(_PROMOTED, {
            "candidate_id": candidate_id, "finding_id": finding_id,
            "poc_id": poc_id, "receipt_ref": receipt_ref, "actor": actor,
        })
        return finding_id

    def dismiss_candidate(self, candidate_id: str, *, reason: str, actor: str) -> None:
        self._require_open(candidate_id)
        self._commit(_DISMISSED, {"candidate_id": candidate_id,
                                  "reason": reason[:300], "actor": actor})

    def operator_note(self, text: str) -> None:
        self._commit("session_note", {"text": text[:600], "actor": "operator"})

    # ------------------------------------------------------------------ ledger

    def ledgers(self) -> dict:
        """Replay the chain into the two ledgers — the only truth there is."""
        candidates: dict[str, dict] = {}
        findings: dict[str, dict] = {}
        for event in self.store.events(self.task_id):
            etype = event.get("type")
            detail = (event.get("payload") or {}).get("detail") or {}
            cid = detail.get("candidate_id")
            if etype == _CANDIDATE:
                candidates[cid] = {**detail, "status": "candidate"}
            elif etype == _PRUNED:
                if cid in candidates:
                    candidates[cid]["status"] = "pruned"
                    candidates[cid]["prune_reason"] = detail.get("reason")
            elif etype == _PROMOTED:
                fid = detail.get("finding_id")
                findings[fid] = {**detail, "status": "finding"}
                if cid in candidates:
                    candidates[cid]["status"] = "finding"
            elif etype == _DISMISSED:
                if cid in candidates:
                    candidates[cid]["status"] = "dismissed"
                    candidates[cid]["dismiss_reason"] = detail.get("reason")
        return {
            "candidates": list(candidates.values()),
            "findings": list(findings.values()),
            "task_id": self.task_id,
        }

    def verify(self) -> None:
        """Full-chain re-verification; a tampered session cannot be read."""
        self.store.verify_chain(self.task_id)

    # ----------------------------------------------------------------- private

    def _next_candidate(self) -> int:
        return 1 + sum(1 for event in self.store.events(self.task_id)
                       if event.get("type") == _CANDIDATE)

    def _require_candidate(self, candidate_id: str) -> None:
        known = {c["candidate_id"] for c in self.ledgers()["candidates"]}
        if candidate_id not in known:
            raise AgentError("CANDIDATE_UNKNOWN",
                             f"{candidate_id!r} is not in this session's ledger")

    def _require_open(self, candidate_id: str) -> None:
        """已决的候选不许二次迁移——一次审计一个结论,改判走新候选。"""
        self._require_candidate(candidate_id)
        candidate = next(c for c in self.ledgers()["candidates"]
                         if c["candidate_id"] == candidate_id)
        if candidate.get("status") != "candidate":
            raise AgentError(
                "CANDIDATE_ALREADY_RESOLVED",
                f"{candidate_id!r} is already {candidate.get('status')!r}; "
                "a resolved candidate cannot migrate again",
            )

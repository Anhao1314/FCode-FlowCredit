"""L2 acceptance: the two ledgers live on the chain, or they don't exist.

Acceptance criteria from docs/memory-design.md §二:
- a restart rebuilds both ledgers from the chain, identically;
- an operator cannot promote a finding (the refusal is evidence);
- promotion without a replay receipt is refused;
- a tampered session cannot be read.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.core.contracts import AgentError  # noqa: E402

from flowaudit.session import AuditSession  # noqa: E402


def _candidate_params(rule_id="RULE-AGI02-001", severity="high"):
    return {"rule_id": rule_id, "category": "AGI-02", "severity": severity,
            "root_cause_group": "approval-memory",
            "evidence": [{"path": "viewer/console_runtime.py", "line": 722}]}


class SessionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addClassCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def _new_session(self, audit_id="demo-target-2026-11-02"):
        return AuditSession.start(self.root, audit_id, operator="ci",
                                  target_identity="demo")

    def test_ledgers_rebuild_identically_after_reload(self) -> None:
        session = self._new_session()
        c1 = session.propose_candidate(**_candidate_params(), actor="host")
        c2 = session.propose_candidate(**_candidate_params(
            rule_id="RULE-AGI02-002"), actor="host")
        session.promote_finding(c1, poc_id="POC-0001", receipt_ref="4471",
                                actor="host")
        session.dismiss_candidate(c2, reason="invalid_poc: stimulus unsound",
                                  actor="host")
        first = session.ledgers()

        # 断点续跑:同一存储,全新会话对象,账本必须逐字段一致。
        reopened = AuditSession.attach(session.store, session.task_id)
        self.assertEqual(reopened.ledgers(), first)
        self.assertEqual(len(first["findings"]), 1)
        self.assertEqual(len(first["candidates"]), 2)
        statuses = {c["candidate_id"]: c["status"] for c in first["candidates"]}
        self.assertEqual(statuses[c1], "finding")
        self.assertEqual(statuses[c2], "dismissed")

    def test_operator_cannot_promote_and_the_refusal_is_evidence(self) -> None:
        session = self._new_session()
        cid = session.propose_candidate(**_candidate_params(), actor="host")
        with self.assertRaises(AgentError) as caught:
            session.promote_finding(cid, poc_id="POC-0009",
                                    receipt_ref="whatever", actor="operator")
        self.assertEqual(caught.exception.code, "OPERATOR_PROMOTION_FORBIDDEN")
        self.assertEqual(len(session.ledgers()["findings"]), 0)
        # 拒绝本身入链:谁试图越过晋升门,是审计证据的一部分。
        events = [e["type"] for e in session.store.events(session.task_id)]
        self.assertIn("promotion_refused", events)

    def test_promotion_requires_a_receipt(self) -> None:
        session = self._new_session()
        cid = session.propose_candidate(**_candidate_params(), actor="host")
        with self.assertRaises(AgentError) as caught:
            session.promote_finding(cid, poc_id="POC-0001",
                                    receipt_ref="", actor="host")
        self.assertEqual(caught.exception.code, "PROMOTION_WITHOUT_RECEIPT")
        self.assertEqual(len(session.ledgers()["findings"]), 0)

    def test_double_resolution_is_refused(self) -> None:
        session = self._new_session()
        cid = session.propose_candidate(**_candidate_params(), actor="host")
        session.promote_finding(cid, poc_id="POC-0001", receipt_ref="4471",
                                actor="host")
        with self.assertRaises(AgentError):
            session.promote_finding(cid, poc_id="POC-0001", receipt_ref="4471",
                                    actor="host")

    def test_tampered_session_cannot_be_read(self) -> None:
        session = self._new_session()
        cid = session.propose_candidate(**_candidate_params(), actor="host")
        session.promote_finding(cid, poc_id="POC-0001", receipt_ref="4471",
                                actor="host")
        # 直接改库:正是审计者对目标做的事,自己的账也必须扛住。
        with sqlite3.connect(session.store.path) as conn:
            conn.execute(
                "UPDATE event SET type = 'candidate_pruned' "
                "WHERE type = 'finding_promoted'")
        with self.assertRaises(AgentError) as caught:
            session.verify()
        self.assertEqual(caught.exception.code, "CHAIN_TAMPERED")


class OperatorNoteTests(unittest.TestCase):
    def test_operator_note_enters_the_chain_with_actor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = AuditSession.start(pathlib.Path(tmp), "a-1",
                                         operator="yimingyang",
                                         target_identity="demo")
            session.operator_note("请优先复核 sidecar 信任链")
            events = session.store.events(session.task_id)
            note = [e for e in events if e["type"] == "session_note"]
            self.assertEqual(len(note), 1)
            self.assertEqual(note[0]["payload"]["detail"]["actor"], "operator")


class DispositionChainTests(unittest.TestCase):
    def test_record_disposition_enters_the_chain_and_stays_verifiable(self) -> None:
        """处置落链:事件带 finding_id/disposition/actor,清空处置同样留痕,
        且全链 verify 仍然通过(处置不是绕过链的后门)。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            session = AuditSession.start(pathlib.Path(tmp), "a-1",
                                         operator="yimingyang",
                                         target_identity="demo")
            session.record_disposition("F-0001", "accepted_risk")
            session.record_disposition("F-0001", "")  # 清除也是一次留痕
            events = session.store.events(session.task_id)
            marks = [e for e in events if e["type"] == "finding_disposition_set"]
            self.assertEqual(len(marks), 2)
            self.assertEqual(marks[0]["payload"]["detail"]["disposition"], "accepted_risk")
            self.assertEqual(marks[1]["payload"]["detail"]["disposition"], "")
            self.assertEqual(marks[0]["payload"]["detail"]["actor"], "operator")
            session.verify()  # 链完整性:两次处置之后整条链仍可重算


if __name__ == "__main__":
    unittest.main()

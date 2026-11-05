"""Choice-lab: rebuild packets, label blocked states, balance positions."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.providers.packet_prompt import POLICY_PROMPT_VERSION  # noqa: E402
from flowagent.runtime.policy import PACKET_VERSION  # noqa: E402
from flowagent.sft.choice_lab import build_packet, expand_packet  # noqa: E402


def _context(plan: list[dict], state: dict | None = None) -> dict:
    return {
        "objective": "判断两期毛利率是否可比",
        "round": 2,
        "plan": plan,
        "state": state or {
            "blockers": [],
            "computed": False,
            "listed_documents": False,
            "margins": {},
            "normalised_documents": [],
            "read_documents": [],
            "reconciled_documents": [],
            "reconciled_periods": [],
            "restatement_applied": False,
            "restatement_checked": False,
        },
        "budget": {"calls_used": 1, "calls_remaining": 11},
        "observations": [],
        "tools": [
            {"name": "list_documents", "effect": "read", "version": "1.0"},
            {"name": "read_document", "effect": "read", "version": "1.0"},
            {"name": "normalize_units", "effect": "read", "version": "1.0"},
            {"name": "report_blocked", "effect": "read", "version": "1.0"},
        ],
    }


class ChoiceLabTests(unittest.TestCase):
    def test_packet_labels_the_assistant_step(self) -> None:
        plan = [
            {"id": "t1", "tool": "list_documents", "arguments": {}, "status": "pending", "text": "列资料"},
            {"id": "t2", "tool": "read_document", "arguments": {"doc_id": "doc_fy2024"}, "status": "pending", "text": "读年报"},
        ]
        step = {"id": "t1", "tool": "list_documents", "arguments": {}}
        source = build_packet(_context(plan), step, source="row1")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.packet["version"], PACKET_VERSION)
        self.assertEqual(source.label.tool, "list_documents")
        self.assertEqual(source.label.todo_id, "t1")
        self.assertFalse(source.blocked)

    def test_blocked_state_labels_report_blocked(self) -> None:
        plan = [
            {"id": "t6", "tool": "reconcile_basis", "arguments": {"doc_id": "doc_fy2025_adjusted"}, "status": "blocked", "text": "桥接"},
            {"id": "t7", "tool": "normalize_units", "arguments": {"doc_id": "doc_fy2024_restated"}, "status": "pending", "text": "归一"},
        ]
        state = _context(plan)["state"]
        state["blockers"] = ["basis_unresolved"]
        step = {"id": "t7", "tool": "normalize_units", "arguments": {"doc_id": "doc_fy2024_restated"}}
        source = build_packet(_context(plan, state), step, source="row2")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertTrue(source.blocked)
        self.assertEqual(source.label.tool, "report_blocked")
        self.assertEqual(source.label.todo_id, "stop")

    def test_positions_are_balanced_and_choices_exist(self) -> None:
        plan = [
            {"id": "t1", "tool": "list_documents", "arguments": {}, "status": "pending", "text": "列资料"},
            {"id": "t2", "tool": "read_document", "arguments": {"doc_id": "doc_fy2024"}, "status": "pending", "text": "读年报"},
            {"id": "t3", "tool": "read_document", "arguments": {"doc_id": "doc_fy2025"}, "status": "pending", "text": "读第二年报"},
        ]
        step = {"id": "t1", "tool": "list_documents", "arguments": {}}
        source = build_packet(_context(plan), step, source="row3")
        assert source is not None
        records = expand_packet(source, variants=3, position_offset=0)
        self.assertEqual(len(records), 3)
        self.assertEqual({record["position"] for record in records}, {1, 2, 3})
        for record in records:
            choice = json.loads(record["conversations"][2]["value"])["choice"]
            system = record["conversations"][0]["value"]
            user = record["conversations"][1]["value"]
            self.assertIn('"choice":"candidate id"', user)
            self.assertIn("candidates", user)
            self.assertTrue(choice.startswith("a"))
            self.assertIn(f'"id":"{choice}"', user.replace(" ", ""))
            self.assertIn("choice", system)
            self.assertEqual(record["prompt_version"], POLICY_PROMPT_VERSION)


if __name__ == "__main__":
    unittest.main()

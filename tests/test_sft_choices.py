"""Choice-format SFT: packet labels, dedupe, and a strict assistant target."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.sft.choices import samples_from_trace, samples_to_sharegpt  # noqa: E402

PACKET = {
    "version": "fcode.packet/v1",
    "objective": "测试",
    "round": 1,
    "state": {},
    "last_evidence": None,
    "memory_cards": [],
    "budget": {},
    "candidates": [
        {"id": "a1", "todo_id": "t1", "tool": "list_documents", "arguments": {}, "why": "列资料"},
        {"id": "a2", "todo_id": "t2", "tool": "read_document", "arguments": {"doc_id": "doc_fy2024"}, "why": "读年报"},
    ],
    "output": {"choice": "candidate id"},
}


def trace(choice: str = "a1") -> dict:
    return {
        "task": {"id": "trace-1"},
        "events": [
            {"type": "policy_requested", "payload": {"detail": {"round": 1, "packet": PACKET}}},
            {"type": "policy_decided", "payload": {"detail": {"round": 1, "candidate_id": choice}}},
        ],
    }


class ChoiceSFTTests(unittest.TestCase):
    def test_packet_and_choice_become_one_sample(self) -> None:
        samples = samples_from_trace(trace(), "run-1")
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["choice"], "a1")
        records, stats = samples_to_sharegpt(samples)
        self.assertEqual(stats["unique"], 1)
        turns = records[0]["conversations"]
        self.assertEqual([turn["from"] for turn in turns], ["system", "user", "assistant"])
        self.assertIn('"a1"', turns[1]["value"])
        self.assertEqual(json.loads(turns[2]["value"]), {"choice": "a1"})
        self.assertEqual(stats["prompt_version"], "fcode.packet-prompt/v2")

    def test_duplicate_packet_choice_pairs_are_dropped(self) -> None:
        records, stats = samples_to_sharegpt(samples_from_trace(trace(), "run-1") * 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["duplicates_skipped"], 1)

    def test_choice_must_be_in_the_packet(self) -> None:
        self.assertEqual(samples_from_trace(trace("a9"), "run-1"), [])


if __name__ == "__main__":
    unittest.main()

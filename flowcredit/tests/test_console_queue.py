"""Queue reordering is a control-plane operation, not an event-chain write."""

from __future__ import annotations

import sys
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from console_runtime import RunControl  # noqa: E402


def control() -> RunControl:
    instance = object.__new__(RunControl)
    instance._lock = threading.Lock()
    instance._commands = deque([
        {"id": "m1", "kind": "message", "text": "one"},
        {"id": "note", "kind": "plan", "text": "kept"},
        {"id": "m2", "kind": "message", "text": "two"},
        {"id": "m3", "kind": "message", "text": "three"},
    ])
    instance.run = SimpleNamespace(
        lock=threading.Lock(), queue=[], bump=lambda: None,
    )
    return instance


def ids(instance: RunControl) -> list[str]:
    return [item["id"] for item in instance.queue_snapshot()]


class ConsoleQueueTest(unittest.TestCase):
    def test_move_down_swaps_with_next_message_and_keeps_other_commands(self) -> None:
        instance = control()
        result = instance.mutate_message("m2", "moveDown")
        self.assertTrue(result["ok"])
        self.assertEqual(ids(instance), ["m1", "m3", "m2"])
        self.assertEqual(
            [item["id"] for item in instance._commands],
            ["m1", "note", "m3", "m2"],
            "non-message commands keep their slot",
        )

    def test_move_to_head_and_tail_uses_remaining_message_index(self) -> None:
        instance = control()
        result = instance.mutate_message("m3", "moveTo", to_index=0)
        self.assertTrue(result["ok"])
        self.assertEqual(ids(instance), ["m3", "m1", "m2"])

        result = instance.mutate_message("m3", "moveTo", to_index=2)
        self.assertTrue(result["ok"])
        self.assertEqual(ids(instance), ["m1", "m2", "m3"], "len(remaining) means append")

    def test_move_to_requires_integer_index(self) -> None:
        instance = control()
        result = instance.mutate_message("m2", "moveTo")
        self.assertFalse(result["ok"])
        self.assertIn("toIndex", result["error"])
        self.assertEqual(ids(instance), ["m1", "m2", "m3"])

    def test_unknown_op_is_rejected_without_mutation(self) -> None:
        instance = control()
        result = instance.mutate_message("m2", "teleport")
        self.assertFalse(result["ok"])
        self.assertIn("moveTo", result["error"])
        self.assertEqual(ids(instance), ["m1", "m2", "m3"])


if __name__ == "__main__":
    unittest.main()

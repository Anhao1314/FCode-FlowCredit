"""Shared pacing: one reservation table for every caller of an endpoint."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.pacing import pace_key, reserve_slot, reset, snapshot  # noqa: E402


class PacingTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def tearDown(self) -> None:
        reset()

    def test_zero_interval_never_waits_or_reserves(self) -> None:
        self.assertEqual(reserve_slot("k", 0), 0.0)
        self.assertEqual(reserve_slot("k", 0, first_call_waits=True), 0.0)
        self.assertEqual(snapshot(), {})

    def test_first_chat_call_starts_immediately(self) -> None:
        self.assertEqual(reserve_slot("k", 22, now=100.0), 0.0)
        self.assertAlmostEqual(reserve_slot("k", 22, now=100.5), 21.5)

    def test_first_agent_call_waits_a_full_interval(self) -> None:
        self.assertAlmostEqual(
            reserve_slot("k", 22, first_call_waits=True, now=100.0), 22.0
        )
        # The first dispatch is reserved for t+22; the next slot is t+44.
        self.assertAlmostEqual(reserve_slot("k", 22, now=122.0), 22.0)

    def test_the_two_stacks_share_the_endpoint_limit(self) -> None:
        # Chat dispatches first; the agent engine must see that reservation.
        self.assertEqual(
            reserve_slot(pace_key("kimi", "https://api.example/v1/"), 22, now=0.0),
            0.0,
        )
        self.assertAlmostEqual(
            reserve_slot(
                pace_key("KIMI", "https://api.example/v1"),
                22,
                first_call_waits=True,
                now=1.0,
            ),
            21.0,
        )

    def test_different_endpoints_do_not_block_each_other(self) -> None:
        self.assertEqual(reserve_slot(pace_key("kimi", "https://a"), 22, now=0), 0.0)
        self.assertEqual(reserve_slot(pace_key("kimi", "https://b"), 22, now=0), 0.0)

    def test_simultaneous_callers_queue_instead_of_stampeding(self) -> None:
        self.assertEqual(reserve_slot("k", 22, now=0), 0.0)
        self.assertAlmostEqual(reserve_slot("k", 22, now=0), 22.0)
        self.assertAlmostEqual(reserve_slot("k", 22, now=0), 44.0)

    def test_snapshot_reports_the_next_slot(self) -> None:
        reserve_slot("k", 30, now=0)
        self.assertAlmostEqual(snapshot(now=0)["k"], 30.0)

    def test_reset_clears_one_key_or_all(self) -> None:
        reserve_slot("a", 10, now=0)
        reserve_slot("b", 10, now=0)
        reset("a")
        self.assertEqual(reserve_slot("a", 10, now=0), 0.0)
        self.assertAlmostEqual(reserve_slot("b", 10, now=0), 10.0)
        reset()
        self.assertEqual(reserve_slot("b", 10, now=0), 0.0)


if __name__ == "__main__":
    unittest.main()

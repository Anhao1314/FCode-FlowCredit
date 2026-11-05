"""Working memory must not turn a failed observation into a settled state."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.memory.context import WorkingMemory  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.finance.summaries import (  # noqa: E402
    FINANCE_SUMMARIZERS,
    summarize_state as finance_state,
)


def observation(tool: str, result: dict) -> Observation:
    return Observation(tool=tool, arguments={}, result=result, result_hash="h" * 64)


class WorkingContextTests(unittest.TestCase):
    def memory(self, *observations: Observation) -> WorkingMemory:
        memory = WorkingMemory(
            objective="test",
            tool_catalog=[],
            summarizers=FINANCE_SUMMARIZERS,
            state_fn=finance_state,
        )
        for index, item in enumerate(observations, start=1):
            memory.record(index, item)
        return memory

    def test_blocked_reconciliation_is_not_a_reconciled_period(self) -> None:
        memory = self.memory(observation("reconcile_basis", {
            "period": "FY2025",
            "reconciled": False,
            "reason": "bridge note is missing",
        }))
        state = memory.state()
        self.assertEqual(state["reconciled_periods"], [])
        self.assertEqual(state["reconciled_documents"], [])
        self.assertIn("bridge note is missing", state["blockers"])
        summary = memory.build(1, {}, None)["observations"][0]["summary"]
        self.assertFalse(summary["reconciled"])
        self.assertIn("bridge note is missing", summary["reason"])
        self.assertNotIn("reconciled_gross_profit", summary)

    def test_successful_reconciliation_carries_the_result(self) -> None:
        memory = self.memory(observation("reconcile_basis", {
            "period": "FY2025",
            "reconciled": True,
            "from_basis": "adjusted",
            "to_basis": "IFRS",
            "delta": -28,
            "reconciled_gross_profit": 168,
        }))
        state = memory.state()
        self.assertEqual(state["reconciled_periods"], ["FY2025"])
        self.assertEqual(state["reconciled_documents"], ["doc_fy2025"])
        summary = memory.build(1, {}, None)["observations"][0]["summary"]
        self.assertTrue(summary["reconciled"])
        self.assertEqual(summary["reconciled_gross_profit"], 168)

    def test_restatement_and_margin_details_reach_the_model(self) -> None:
        memory = self.memory(
            observation("check_restatement", {
                "period": "FY2024", "restated": True, "delta": -432,
                "restated_gross_profit": 15120,
            }),
            observation("calculate_margin", {
                "computed": True,
                "margin_bp_2024": 1750,
                "margin_bp_2025": 1500,
                "change_bp": -250,
            }),
        )
        context = memory.build(1, {}, None)
        self.assertEqual(context["observations"][0]["summary"]["restated_gross_profit"], 15120)
        self.assertEqual(context["observations"][1]["summary"]["margin_bp_2024"], 1750)
        self.assertEqual(context["state"]["margins"]["change_bp"], -250)


if __name__ == "__main__":
    unittest.main()

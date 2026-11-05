"""Evaluation plane: outcomes, fast path, repairs and packet size."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.sft.evaluate import evaluate  # noqa: E402


class EvaluateTests(unittest.TestCase):
    def test_mock_policy_report_covers_success_and_honest_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate(
                workdir=pathlib.Path(tmp),
                scenarios=["full", "open-ended"],
                policy_name="mock",
            )
            by_name = {item["scenario"]: item for item in report["scenarios"]}
            self.assertEqual(by_name["full"]["status"], "succeeded")
            self.assertEqual(by_name["full"]["change_bp"], -250)
            self.assertEqual(by_name["open-ended"]["status"], "failed")
            self.assertEqual(by_name["open-ended"]["failure_code"], "INSUFFICIENT_EVIDENCE")
            totals = report["totals"]
            self.assertEqual(totals["outcomes_ok"], 2)
            self.assertEqual(totals["errors"], 0)
            self.assertEqual(totals["model_calls"], 17)
            self.assertEqual(totals["policy_decisions"], 17)
            self.assertEqual(totals["auto_actions"], 1)
            self.assertEqual(totals["repaired"], 0)
            self.assertLess(totals["packet_avg_bytes"], totals["legacy_context_bytes"] * 0.5)
            self.assertTrue(all(item.get("verified") for item in report["scenarios"]))


if __name__ == "__main__":
    unittest.main()

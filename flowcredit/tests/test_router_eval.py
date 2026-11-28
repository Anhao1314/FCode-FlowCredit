"""Router evaluation suite: the failures that matter, scored on the shared fixture."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.evaluation.router_eval import evaluate_router, summary_markdown  # noqa: E402


class RouterEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = evaluate_router()

    def test_the_costly_failures_are_zero_on_the_deterministic_router(self) -> None:
        metrics = self.report["metrics"]
        self.assertEqual(metrics["chat_misrouted_to_run"]["count"], 0,
                         metrics["chat_misrouted_to_run"]["case_ids"])
        self.assertEqual(metrics["work_missed_into_chat"]["count"], 0,
                         metrics["work_missed_into_chat"]["case_ids"])

    def test_accuracy_is_measured_and_safe_fallback_holds(self) -> None:
        metrics = self.report["metrics"]
        self.assertEqual(metrics["accuracy"]["status"], "MEASURED")
        self.assertEqual(metrics["accuracy"]["value"], 1.0)
        self.assertEqual(metrics["safe_fallback_rate"]["value"], 1.0)
        self.assertGreater(metrics["low_confidence_count"], 0)

    def test_clarify_rates_are_reported(self) -> None:
        metrics = self.report["metrics"]
        self.assertEqual(metrics["clarify_expected_recall"]["value"], 1.0)
        self.assertGreater(metrics["clarify_share"]["value"], 0)
        self.assertEqual(metrics["clarify_share"]["denominator"], len(self.report["rows"]))

    def test_deterministic_router_spends_no_model_calls(self) -> None:
        self.assertEqual(self.report["metrics"]["classifier_extra"]["calls"], 0)

    def test_a_failing_classifier_is_reported_and_never_becomes_a_run(self) -> None:
        def broken(message: str) -> dict | None:
            raise RuntimeError("provider down")

        report = evaluate_router(classifier=broken)
        metrics = report["metrics"]
        self.assertEqual(metrics["safe_fallback_rate"]["value"], 1.0)
        self.assertEqual(metrics["chat_misrouted_to_run"]["count"], 0)
        self.assertTrue(any(row["classifier_error"] for row in report["rows"]))

    def test_summary_markdown_renders_the_metrics(self) -> None:
        markdown = summary_markdown(self.report)
        self.assertIn("# Intent-router evaluation", markdown)
        self.assertIn("chat 误创建 Run: **0**", markdown)
        self.assertIn("work 误落入 Chat: **0**", markdown)
        self.assertIn("safe fallback", markdown)


if __name__ == "__main__":
    unittest.main()

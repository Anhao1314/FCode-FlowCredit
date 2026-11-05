"""Acceptance verdict: loss is not evidence; outcome and valid choices are."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.sft.accept import checks  # noqa: E402


def report(*, ok: bool = True, errors: int = 0, repaired: int = 0) -> dict:
    return {
        "totals": {
            "scenarios": 2,
            "outcomes_ok": 2 if ok else 1,
            "errors": errors,
            "policy_decisions": 17,
            "repaired": repaired,
        },
        "scenarios": [
            {"scenario": "full", "verified": True},
            {"scenario": "open-ended", "verified": True},
        ],
    }


class AcceptTests(unittest.TestCase):
    def test_outcome_and_chain_are_required(self) -> None:
        self.assertTrue(checks(report())["passed"])
        self.assertFalse(checks(report(ok=False))["passed"])
        self.assertFalse(checks(report(errors=1))["passed"])

    def test_repair_is_a_warning_not_a_silent_pass(self) -> None:
        verdict = checks(report(repaired=2))
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["repaired"], 2)
        self.assertTrue(verdict["warnings"])

    def test_missing_endpoint_is_reported_but_chat_can_still_run(self) -> None:
        verdict = checks(report(), {"available": False, "model_found": False, "models": [], "error": "x"})
        self.assertFalse(verdict["endpoint_available"])
        self.assertIn("chat path", " ".join(verdict["warnings"]))


if __name__ == "__main__":
    unittest.main()

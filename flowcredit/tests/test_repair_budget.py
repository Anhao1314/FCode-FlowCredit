"""The repair loop has a budget and a repeated-failure circuit breaker."""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.runtime.loop import Loop  # noqa: E402


def _record(status: str, issues: list[str], uncovered: list[str] | None = None):
    return SimpleNamespace(
        tool="motion_check",
        result={
            "status": status,
            "issues": issues,
            "coverage": {"complete": status == "pass", "uncovered": uncovered or []},
        },
    )


class RepairBudgetTests(unittest.TestCase):
    def _loop(self):
        return Loop.__new__(Loop)

    def test_repeated_identical_failure_stops_as_no_progress(self) -> None:
        memory = SimpleNamespace(records=[
            _record("fail", ["leg is too short"], ["hip_anchor"]),
            _record("fail", ["leg is too short"], ["hip_anchor"]),
            _record("fail", ["leg is too short"], ["hip_anchor"]),
        ])
        error = self._loop()._motion_repair_exhausted(memory)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "NO_PROGRESS")

    def test_five_failed_quality_checks_exhaust_the_repair_budget(self) -> None:
        memory = SimpleNamespace(records=[
            _record("fail", [f"issue-{index}"], [f"check-{index}"])
            for index in range(5)
        ])
        error = self._loop()._motion_repair_exhausted(memory)
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "REPAIR_BUDGET_EXCEEDED")

    def test_a_passing_check_resets_the_budget(self) -> None:
        memory = SimpleNamespace(records=[
            _record("fail", ["old"], ["hip_anchor"]),
            _record("fail", ["old"], ["hip_anchor"]),
            _record("pass", []),
            _record("fail", ["new"], ["leg_length"]),
            _record("fail", ["new-2"], ["leg_endpoint"]),
        ])
        self.assertIsNone(self._loop()._motion_repair_exhausted(memory))

    def test_an_unknown_or_warn_status_is_not_counted_as_a_repair_failure(self) -> None:
        memory = SimpleNamespace(records=[
            _record("warn", ["loose contact"]),
            _record("unverified", ["coverage"], ["hip_anchor"]),
        ])
        self.assertIsNone(self._loop()._motion_repair_exhausted(memory))


if __name__ == "__main__":
    unittest.main()

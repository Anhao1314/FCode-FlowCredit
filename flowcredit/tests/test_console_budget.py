"""Externally billed runs must never raise an operator's hard budget."""

from __future__ import annotations

import pathlib
import sys
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_common import _scaled_budget_for_prices  # noqa: E402
from flowagent.demo.fixtures import SCENARIOS  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402


class ConsoleBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.price = SimpleNamespace(
            input_microusd_per_mtok=1_000_000,
            output_microusd_per_mtok=4_000_000,
        )
        self.limits = BudgetLimits(
            max_calls=12,
            max_reads=8,
            max_cost_microusd=80_000,
            max_tokens=100_000,
        )

    def test_cost_ceiling_is_never_raised_and_calls_fit(self) -> None:
        # One reservation costs 38,768 µUSD: only two calls fit in 80,000.
        budget = _scaled_budget_for_prices(self.limits, self.price, 8_192)
        self.assertEqual(budget["max_cost_microusd"], 80_000)
        self.assertEqual(budget["max_calls"], 2)
        self.assertEqual(budget["max_tokens"], 100_000)
        self.assertEqual(budget["max_reads"], 8)

    def test_token_ceiling_also_limits_calls_without_being_raised(self) -> None:
        limits = BudgetLimits(12, 8, 1_000_000, 20_000)
        budget = _scaled_budget_for_prices(limits, self.price, 8_192)
        self.assertEqual(budget["max_calls"], 1)
        self.assertEqual(budget["max_tokens"], 20_000)

    def test_less_than_one_paid_reservation_is_rejected(self) -> None:
        limits = BudgetLimits(12, 8, 30_000, 100_000)
        with self.assertRaisesRegex(ValueError, "38768") as caught:
            _scaled_budget_for_prices(limits, self.price, 8_192)
        self.assertIn("14192", str(caught.exception))

    def test_a_free_custom_endpoint_is_bounded_by_tokens_not_by_cost(self) -> None:
        """零价格端点不能除零：成本不约束容量，token 顶仍然生效。"""
        free = SimpleNamespace(
            input_microusd_per_mtok=0,
            output_microusd_per_mtok=0,
        )
        budget = _scaled_budget_for_prices(self.limits, free, 8_192)
        self.assertEqual(budget["max_cost_microusd"], 80_000, "硬成本上限不被抬高")
        self.assertEqual(budget["max_calls"], 7, "100k token 顶只容得下 7 次 14,192-token 预留")
        tight = BudgetLimits(12, 8, 80_000, 20_000)
        capped = _scaled_budget_for_prices(tight, free, 8_192)
        self.assertEqual(capped["max_calls"], 1, "免费端点仍受 token 顶约束")

    def test_general_work_default_starts_a_real_kimi_run(self) -> None:
        price = SimpleNamespace(
            input_microusd_per_mtok=950_000,
            output_microusd_per_mtok=4_000_000,
        )
        budget = _scaled_budget_for_prices(SCENARIOS["general-research"].limits, price, 8_192)
        self.assertGreaterEqual(budget["max_calls"], 1)
        self.assertEqual(budget["max_cost_microusd"], 500_000)
        self.assertEqual(budget["max_tokens"], 120_000)


if __name__ == "__main__":
    unittest.main()

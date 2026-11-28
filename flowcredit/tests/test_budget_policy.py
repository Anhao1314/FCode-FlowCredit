"""S1 budget policy: one cap formula, strict chain replay, deterministic idempotency."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.runtime.budget import (  # noqa: E402
    BudgetLimits,
    find_budget_request,
    plan_adjustment,
    replay_limits,
)


def limits(calls=12, reads=30, cost=500_000, tokens=120_000):
    return BudgetLimits(calls, reads, cost, tokens)


def adjust_event(sequence, before, after, *, granted=None, request_id="req-1", version=2):
    return {
        "sequence": sequence,
        "type": "budget_adjusted",
        "payload": {"detail": {
            "policy_version": version,
            "request_id": request_id,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "granted": granted or {
                "tokens": after.max_tokens - before.max_tokens,
                "calls": after.max_calls - before.max_calls,
            },
        }},
    }


class PlanAdjustmentTests(unittest.TestCase):
    def test_token_delta_is_capped_per_request_and_cumulative(self) -> None:
        base = limits()
        first = plan_adjustment(base, base, delta={"tokens": 90_000})
        self.assertEqual(first.granted["tokens"], 60_000, "single request <= 50% of base")
        self.assertTrue(first.capped["tokens"])
        current = limits(tokens=180_000)
        second = plan_adjustment(base, current, delta={"tokens": 60_000})
        self.assertEqual(second.granted["tokens"], 60_000)
        self.assertEqual(second.after.max_tokens, 240_000)
        third = plan_adjustment(base, second.after, delta={"tokens": 1})
        self.assertTrue(third.exhausted)
        self.assertEqual(third.granted["tokens"], 0)

    def test_total_is_converted_to_a_raise_only_delta(self) -> None:
        base = limits()
        planned = plan_adjustment(base, base, total={"tokens": 240_000})
        self.assertEqual(planned.after.max_tokens, 180_000, "single request capped at 50%")
        self.assertTrue(planned.capped["tokens"])
        with self.assertRaises(AgentError) as ctx:
            plan_adjustment(base, limits(tokens=180_000), total={"tokens": 150_000})
        self.assertEqual(ctx.exception.code, "BUDGET_RAISE_ONLY")

    def test_every_dimension_uses_the_same_formula(self) -> None:
        base = limits()
        planned = plan_adjustment(base, base, delta={
            "calls": 1_000, "reads": 1_000, "cost_microusd": 1_000_000, "tokens": 1_000_000,
        })
        self.assertEqual(planned.granted["calls"], 6)
        self.assertEqual(planned.granted["reads"], 15)
        self.assertEqual(planned.granted["cost_microusd"], 250_000)
        self.assertEqual(planned.granted["tokens"], 60_000)
        self.assertTrue(all(planned.capped.values()))

    def test_invalid_amounts_are_rejected(self) -> None:
        base = limits()
        for bad in (-1, 1.5, True):
            with self.subTest(bad=bad):
                with self.assertRaises(AgentError):
                    plan_adjustment(base, base, delta={"tokens": bad})
        with self.assertRaises(AgentError):
            plan_adjustment(base, base, delta={"unknown": 1})
        with self.assertRaises(AgentError):
            plan_adjustment(base, base, delta={"tokens": 0})
        with self.assertRaises(AgentError):
            plan_adjustment(base, base, delta={"tokens": 1}, total={"tokens": 2})

    def test_current_below_base_is_rejected(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            plan_adjustment(limits(), limits(tokens=100_000), delta={"tokens": 1})
        self.assertEqual(ctx.exception.code, "BUDGET_BASE_INVALID")


class ReplayTests(unittest.TestCase):
    def test_strict_sequence_rebuilds_effective_limits(self) -> None:
        base = limits()
        first = plan_adjustment(base, base, delta={"tokens": 60_000})
        second = plan_adjustment(base, first.after, delta={"calls": 6})
        events = [
            adjust_event(2, first.before, first.after, request_id="r1"),
            adjust_event(3, second.before, second.after,
                         granted={"calls": second.granted["calls"], "tokens": 0},
                         request_id="r2"),
        ]
        effective, diagnostics = replay_limits(base, events)
        self.assertEqual(effective.max_tokens, 180_000)
        self.assertEqual(effective.max_calls, 18)
        self.assertEqual(diagnostics, [])

    def test_before_mismatch_is_a_chain_error(self) -> None:
        base = limits()
        wrong_before = limits(tokens=130_000)
        after = limits(tokens=160_000)
        with self.assertRaises(AgentError) as ctx:
            replay_limits(base, [adjust_event(2, wrong_before, after)])
        self.assertEqual(ctx.exception.code, "BUDGET_CHAIN_INVALID")

    def test_after_must_equal_before_plus_granted(self) -> None:
        base = limits()
        after = limits(tokens=200_000)  # 120k + 80k, but granted says 60k
        with self.assertRaises(AgentError) as ctx:
            replay_limits(base, [adjust_event(2, base, after, granted={"tokens": 60_000, "calls": 0})])
        self.assertEqual(ctx.exception.code, "BUDGET_CHAIN_INVALID")

    def test_duplicate_request_id_is_idempotent_but_conflicts_fail(self) -> None:
        base = limits()
        after = limits(tokens=180_000)
        same = [adjust_event(2, base, after, request_id="r1"),
                adjust_event(3, base, after, request_id="r1")]
        effective, _ = replay_limits(base, same)
        self.assertEqual(effective.max_tokens, 180_000)
        other = plan_adjustment(base, after, delta={"calls": 6})
        conflict = [adjust_event(2, base, after, request_id="r1"),
                    adjust_event(3, after, other.after, request_id="r1")]
        with self.assertRaises(AgentError):
            replay_limits(base, conflict)

    def test_legacy_adjustment_is_raise_only_and_capped_at_two_x(self) -> None:
        base = limits()
        over = limits(tokens=400_000)
        effective, diagnostics = replay_limits(base, [adjust_event(2, base, over, version=0)])
        self.assertEqual(effective.max_tokens, 240_000)
        self.assertTrue(diagnostics)

    def test_legacy_resume_limits_are_adopted_once_not_as_a_second_source(self) -> None:
        base = limits()
        resumed = {
            "sequence": 2,
            "type": "task_resumed",
            "payload": {"detail": {"limits": limits(tokens=180_000).to_dict()}},
        }
        effective, diagnostics = replay_limits(base, [resumed], allow_legacy_resume=True)
        self.assertEqual(effective.max_tokens, 180_000)
        self.assertTrue(diagnostics)
        # A later strict adjustment must start from that adopted value.
        event = adjust_event(3, effective, limits(tokens=240_000),
                             granted={"tokens": 60_000, "calls": 0}, request_id="r2")
        rebuilt, _ = replay_limits(base, [resumed, event], allow_legacy_resume=True)
        self.assertEqual(rebuilt.max_tokens, 240_000)
        # For a new run (base binding exists) the same legacy event is ignored.
        ignored, _ = replay_limits(base, [resumed], allow_legacy_resume=False)
        self.assertEqual(ignored.max_tokens, 120_000)

    def test_no_usage_inflation_in_replay(self) -> None:
        base = limits()
        # Replay has no usage input at all; the returned value is exactly the
        # chain's effective limit, even if consumption were higher.
        effective, _ = replay_limits(base, [])
        self.assertEqual(effective.to_dict(), base.to_dict())

    def test_request_id_lookup(self) -> None:
        base = limits()
        after = limits(tokens=180_000)
        events = [adjust_event(2, base, after, request_id="abc")]
        self.assertEqual(find_budget_request(events, "abc")["after"]["max_tokens"], 180_000)
        self.assertIsNone(find_budget_request(events, "missing"))


if __name__ == "__main__":
    unittest.main()

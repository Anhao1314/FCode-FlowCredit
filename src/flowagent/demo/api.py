"""Run one frozen scenario against the DeepSeek API.

This is the external-provider lab, not a console integration. The host still
gates, budgets, executes and verifies; DeepSeek only proposes.

Prices are required and must come from the host's billing page. The budget is
reserved from an upper bound before the request; the receipt records estimated
actual usage, and any transport timeout is an unknown outcome (no retry).

Example:
    export DEEPSEEK_API_KEY=...
    PYTHONPATH=src python3 -m flowagent.demo.api \
      --workdir /tmp/fcode-deepseek-full --scenario full --fresh \
      --input-price-microusd-per-mtok 270000 \
      --output-price-microusd-per-mtok 1100000 \
      --price-as-of 2026-10-21
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..providers.deepseek_config import (
    DEEPSEEK_MAX_TOKENS_ENV,
    load_deepseek_settings,
)
from ..runtime.budget import BudgetLimits
from .fixtures import get_scenario
from .local import run

__all__ = ["budget_limits_from_overrides", "main"]


def budget_limits_from_overrides(
    scenario_key: str,
    *,
    max_calls: int | None,
    max_reads: int | None,
    max_cost_microusd: int | None,
    max_tokens: int | None,
) -> BudgetLimits | None:
    """Apply host budget overrides to one scenario; unset dimensions stay put."""
    if all(
        value is None
        for value in (max_calls, max_reads, max_cost_microusd, max_tokens)
    ):
        return None
    base = get_scenario(scenario_key).limits
    return BudgetLimits(
        max_calls=max_calls if max_calls is not None else base.max_calls,
        max_reads=max_reads if max_reads is not None else base.max_reads,
        max_cost_microusd=(
            max_cost_microusd if max_cost_microusd is not None else base.max_cost_microusd
        ),
        max_tokens=max_tokens if max_tokens is not None else base.max_tokens,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-api-lab")
    parser.add_argument("--workdir", required=True, help="isolated experiment directory")
    parser.add_argument("--scenario", default="full")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--input-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--output-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--cache-hit-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--price-as-of", default=None)
    parser.add_argument("--price-source", default=None)
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--max-prompt-tokens", type=int, default=None)
    parser.add_argument("--max-rounds", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-reads", type=int, default=None)
    parser.add_argument("--max-cost-microusd", type=int, default=None)
    parser.add_argument("--max-tokens-budget", type=int, default=None)
    parser.add_argument("--no-json-mode", action="store_false", dest="json_mode")
    args = parser.parse_args(argv)

    max_tokens = args.max_tokens
    if max_tokens is None and not os.environ.get(DEEPSEEK_MAX_TOKENS_ENV, "").strip():
        max_tokens = 256
    try:
        settings = load_deepseek_settings(
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            model=args.model,
            timeout=args.timeout,
            max_tokens=max_tokens,
            max_prompt_tokens=args.max_prompt_tokens,
            input_price=args.input_price_microusd_per_mtok,
            output_price=args.output_price_microusd_per_mtok,
            cache_hit_price=args.cache_hit_price_microusd_per_mtok,
            price_as_of=args.price_as_of,
            price_source=args.price_source,
        )
        provider = settings.provider(json_mode=args.json_mode)
    except ValueError as error:
        parser.error(str(error))

    limits = budget_limits_from_overrides(
        args.scenario,
        max_calls=args.max_calls,
        max_reads=args.max_reads,
        max_cost_microusd=args.max_cost_microusd,
        max_tokens=args.max_tokens_budget,
    )

    summary = run(
        workdir=Path(args.workdir),
        scenario_key=args.scenario,
        fresh=args.fresh,
        endpoint=settings.base_url,
        model=settings.model,
        timeout=settings.timeout,
        max_rounds=args.max_rounds or None,
        provider=provider,
        limits=limits,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verified"] and summary["outcome_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Unified DeepSeek API lab: preflight, packet choices, proposal comparison.

Packet mode is the default because it is the API-first path that matches the
Executive–Policy architecture: the host compiles the candidates and DeepSeek only
chooses one. Proposal mode remains available as a paid comparison experiment.

The API key is read only from the environment named by ``--api-key-env``. It is
never printed, logged, or written into a summary.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from ..providers.deepseek_config import (
    DEEPSEEK_MAX_TOKENS_ENV,
    load_deepseek_settings,
)
from ..runtime.budget import BudgetLimits
from .api import budget_limits_from_overrides
from .fixtures import get_scenario
from .local import run as run_proposal
from .packet import run as run_packet

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowagent-deepseek-lab")
    parser.add_argument("--workdir", required=True, help="isolated experiment directory")
    parser.add_argument("--mode", choices=("packet", "proposal"), default="packet")
    parser.add_argument("--scenario", default="full")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--max-prompt-tokens", type=int, default=None)
    parser.add_argument("--max-packet-tokens", type=int, default=None)
    parser.add_argument("--input-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--output-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--cache-hit-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--price-as-of", default=None)
    parser.add_argument("--price-source", default=None)
    parser.add_argument("--max-rounds", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-reads", type=int, default=None)
    parser.add_argument("--max-cost-microusd", type=int, default=None)
    parser.add_argument("--max-tokens-budget", type=int, default=None)
    return parser


def _settings(args: argparse.Namespace, parser: argparse.ArgumentParser):
    # A proposal needs more output room than a one-token choice. The explicit
    # CLI flag and DEEPSEEK_MAX_TOKENS always win; this is only the mode default.
    max_tokens = args.max_tokens
    if max_tokens is None and args.mode == "proposal" and not os.environ.get(
        DEEPSEEK_MAX_TOKENS_ENV, ""
    ).strip():
        max_tokens = 256
    try:
        return load_deepseek_settings(
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            model=args.model,
            timeout=args.timeout,
            max_tokens=max_tokens,
            max_prompt_tokens=args.max_prompt_tokens,
            max_packet_tokens=args.max_packet_tokens,
            input_price=args.input_price_microusd_per_mtok,
            output_price=args.output_price_microusd_per_mtok,
            cache_hit_price=args.cache_hit_price_microusd_per_mtok,
            price_as_of=args.price_as_of,
            price_source=args.price_source,
        )
    except ValueError as error:
        parser.error(str(error))


def _limits(args: argparse.Namespace) -> BudgetLimits | None:
    return budget_limits_from_overrides(
        args.scenario,
        max_calls=args.max_calls,
        max_reads=args.max_reads,
        max_cost_microusd=args.max_cost_microusd,
        max_tokens=args.max_tokens_budget,
    )


def _preflight(
    args: argparse.Namespace,
    settings: Any,
) -> dict[str, Any]:
    scenario = get_scenario(args.scenario)
    tokens, cost = settings.reservation(args.mode)
    return {
        "ok": True,
        "preflight": True,
        "networkCalled": False,
        "mode": args.mode,
        "scenario": scenario.key,
        "expectedStatus": scenario.expected_status,
        "config": settings.redacted(),
        "reservation": {
            "basis": "configured-upper-bound",
            "tokens": tokens,
            "costMicrousd": cost,
        },
        "scenarioBudget": {
            "maxTokens": scenario.limits.max_tokens,
            "maxCostMicrousd": scenario.limits.max_cost_microusd,
            "reservationWithinBudget": (
                tokens <= scenario.limits.max_tokens
                and cost <= scenario.limits.max_cost_microusd
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = _settings(args, parser)

    if args.preflight_only:
        summary = _preflight(args, settings)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    limits = _limits(args)
    common = {
        "workdir": Path(args.workdir),
        "scenario_key": args.scenario,
        "fresh": args.fresh,
        "timeout": settings.timeout,
        "max_rounds": args.max_rounds or None,
    }
    if args.mode == "packet":
        summary = run_packet(
            **common,
            policy_name="deepseek",
            model=settings.model,
            json_mode=True,
            price=settings.price,
            api_key=settings.api_key,
            base_url=settings.base_url,
            max_tokens=settings.max_tokens,
            limits=limits,
        )
    else:
        summary = run_proposal(
            **common,
            endpoint=settings.base_url,
            model=settings.model,
            provider=settings.provider(),
            limits=limits,
        )

    scenario = get_scenario(args.scenario)
    summary["mode"] = args.mode
    summary["expected_status"] = scenario.expected_status
    summary["outcome_ok"] = summary["status"] == scenario.expected_status
    summary["config"] = settings.redacted()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verified"] and summary["outcome_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

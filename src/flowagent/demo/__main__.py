"""CLI entry point for the offline demo.

Design rule: every flag either works or says exactly why it cannot. ``--model``
is the clearest case — a real provider is gated off in Phase 1, so the CLI
explains the gate instead of pretending the option does not exist.

With no arguments and a terminal attached, the CLI offers a menu. With no
arguments and no terminal (CI, a pipe, a test), it runs the full scenario, so
`python demo.py` is never interactive by accident.
"""

from __future__ import annotations

import argparse
import sys

from ..core.contracts import AgentError
from ..runtime.budget import BudgetLimits
from .fixtures import SCENARIOS, get_scenario
from .runner import PROJECT_ROOT, WORK_DIR, run_all, run_scenario
from .serve import serve

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowagent-demo",
        description="Host-governed, durable, auditable agent runtime — offline demo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python demo.py                 menu in a terminal, full scenario otherwise\n"
            "  python demo.py full --fast     the reference workload's reliability story\n"
            "  python demo.py general-research --fast\n"
            "                                 general delegation, offline script mode\n"
            "  python demo.py general-research --sources https://example.com --fast\n"
            "                                 general delegation, fetch + write research.md\n"
            "  python demo.py all             every scenario, then a summary table\n"
            "  python demo.py --serve         open the trace viewer in a browser\n"
        ),
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default=None,
        choices=sorted(SCENARIOS) + ["all"],
        help="which scenario to run (default: menu in a terminal, otherwise full)",
    )
    parser.add_argument("--fast", action="store_true", help="disable pauses between rounds")
    parser.add_argument("--trace-out", metavar="PATH", help="write the trace here instead of work/demo/")
    parser.add_argument(
        "--provider",
        choices=("mock", "local", "kimi", "deepseek", "oracle"),
        default="mock",
        help="explicit host-selected proposal source (default: offline mock)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="provider model override; without --provider this old flag remains gated",
    )
    parser.add_argument("--endpoint", default=None, help="local endpoint or oracle exchange directory")
    parser.add_argument("--timeout", type=float, default=None, help="provider timeout in seconds")
    parser.add_argument("--json-mode", action="store_true", help="request JSON mode from a local provider")
    parser.add_argument("--structured", action="store_true", help="request the local provider's JSON schema mode")
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default=None, help="Kimi K3 reasoning effort")
    parser.add_argument("--max-cost-microusd", type=int, default=None, help="host budget override for one scenario")
    parser.add_argument("--max-tokens-budget", type=int, default=None, help="host token-budget override for one scenario")
    parser.add_argument("--max-total-cost", type=int, default=None, help="batch guard in µUSD (required for external provider batches)")
    parser.add_argument("--workdir", default=None, help="isolated directory for one real-provider run")
    parser.add_argument("--workroot", default=None, help="parent directory for batch runs; each scenario gets a subdir")
    parser.add_argument(
        "--mode", choices=("research", "script"), default=None,
        help="general delegation mode (default: research when --sources is given, otherwise script)",
    )
    parser.add_argument("--sources", default=None, help="comma-separated URLs for a general research delegation")
    parser.add_argument("--script-path", default=None, help="workspace filename for a general script delegation")
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_viewer",
        help="serve the viewer and open it (the page is an ES module app, so file:// cannot load it)",
    )
    parser.add_argument("--serve", action="store_true", help="serve the viewer on 127.0.0.1 and open it")
    parser.add_argument("--port", type=int, default=0, help="port for --serve (default: pick a free one)")
    parser.add_argument("--list", action="store_true", dest="list_scenarios", help="list scenarios and exit")
    parser.add_argument(
        "--provider-status", action="store_true",
        help="show redacted provider availability, price dates and config path",
    )
    return parser


def _print_provider_status() -> None:
    """Print configuration provenance without ever printing credentials."""
    from ..chat_config import default_chat_config_path
    from ..providers.catalog import provider_catalog, resolved_provider_env

    print(f"config={default_chat_config_path()} (environment > user config > defaults)")
    for entry in provider_catalog(env=resolved_provider_env()):
        status = "available" if entry["available"] else f"unavailable: {entry['reason']}"
        details = ""
        if entry.get("priceAsOf"):
            details = f"，price_as_of={entry['priceAsOf']}，price_source={entry.get('priceSource') or 'unset'}"
        print(f"{entry['key']}: {status}，model={entry['model']}{details}")


def _provider_options(args: argparse.Namespace) -> dict[str, object]:
    return {
        "endpoint": args.endpoint,
        "timeout": args.timeout,
        "json_mode": args.json_mode,
        "structured": args.structured,
        "reasoning_effort": args.reasoning_effort,
    }


def _parse_sources(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _delegation_spec(args: argparse.Namespace, scenario_key: str) -> dict[str, object]:
    """Operator terms for one delegation; only the general module reads them.

    Research mode fetches the named sources; script mode writes, runs and fixes
    a local script with no network. With no explicit mode, sources mean research
    and their absence means script, so the general prototype has an offline run.
    """
    scenario = get_scenario(scenario_key)
    if scenario.module != "general-work":
        return {}
    sources = _parse_sources(getattr(args, "sources", None))
    mode = getattr(args, "mode", None) or ("research" if sources else "script")
    spec: dict[str, object] = {"mode": mode}
    if sources:
        spec["sources"] = sources
    if getattr(args, "script_path", None):
        spec["scriptPath"] = args.script_path
    if mode == "research" and not sources:
        raise ValueError("--mode research needs --sources (1 to 5 comma-separated URLs)")
    return spec


def _limits(args: argparse.Namespace, scenario_key: str) -> BudgetLimits | None:
    if args.max_cost_microusd is None and args.max_tokens_budget is None:
        return None
    base = get_scenario(scenario_key).limits
    return BudgetLimits(
        max_calls=base.max_calls,
        max_reads=base.max_reads,
        max_cost_microusd=(
            args.max_cost_microusd
            if args.max_cost_microusd is not None
            else base.max_cost_microusd
        ),
        max_tokens=(
            args.max_tokens_budget
            if args.max_tokens_budget is not None
            else base.max_tokens
        ),
    )


def _menu() -> str:
    """Ask which scenario to run; default to the full story on an empty answer."""
    keys = list(SCENARIOS)
    print("\n可运行场景：")
    for index, key in enumerate(keys, start=1):
        scenario = SCENARIOS[key]
        print(f"  {index}) {key:<12} {scenario.title}")
    print(f"  a) {'all':<12} 顺序跑完全部并汇总")
    raw = input(f"\n选择 [1-{len(keys)}]，回车默认 1：").strip().lower()
    if raw == "a":
        return "all"
    if not raw:
        return keys[0]
    try:
        index = int(raw)
        if 1 <= index <= len(keys):
            return keys[index - 1]
    except ValueError:
        pass
    print("无法识别，按默认执行 full。")
    return keys[0]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list_scenarios:
        print("scenarios:")
        for key, scenario in SCENARIOS.items():
            print(f"  {key:<12} {scenario.title}  [{scenario.module}]")
        return 0

    if args.provider_status:
        _print_provider_status()
        return 0

    if args.serve:
        missing = [key for key in SCENARIOS if not (WORK_DIR / f"{key}.trace.json").exists()]
        if missing:
            print(f"generating {len(missing)} missing trace(s) before serving…")
            run_all()
        return serve(PROJECT_ROOT, port=args.port)

    key = args.scenario
    if key is None:
        interactive = sys.stdin.isatty() and not args.fast and not args.trace_out
        key = _menu() if interactive else "full"

    try:
        if key == "all":
            results = run_all(
                provider_name=args.provider,
                provider_options=_provider_options(args),
                workroot=args.workroot,
                limits=_limits(args, "full"),
                max_total_cost=args.max_total_cost,
                spec_for=lambda name: _delegation_spec(args, name),
            )
            code = 0 if all(result.ok for result in results) else 1
        else:
            result = run_scenario(
                key,
                fast=args.fast,
                trace_out=args.trace_out,
                model=args.model,
                provider_name=args.provider,
                provider_options=_provider_options(args),
                workdir=args.workdir,
                limits=_limits(args, key),
                spec=_delegation_spec(args, key),
            )
            code = result.exit_code
    except KeyError:
        print(f"unknown scenario {key!r}; run with --list to see the options")
        return 2
    except ValueError as error:
        print(f"PROVIDER_CONFIG_INVALID: {error}")
        return 2
    except AgentError as error:
        if error.code == "PROVIDER_GATED":
            print(error.message)
            print("a real provider requires an explicit --provider selection, credentials from the host environment,")
            print("a host-reviewed price snapshot and a hard budget. See docs/deepseek.md and docs/kimi.md.")
            return 2
        print(f"{error.code}: {error.message}")
        return 1

    if args.open_viewer:
        # The console loads app.js as an ES module; Chromium refuses module
        # imports from file://, so hand the page to the same local server
        # --serve uses instead of opening index.html directly.
        missing = [key for key in SCENARIOS if not (WORK_DIR / f"{key}.trace.json").exists()]
        if missing:
            print(f"generating {len(missing)} missing trace(s) before serving…")
            run_all()
        return serve(PROJECT_ROOT, port=args.port)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

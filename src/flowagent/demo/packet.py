"""Run one scenario in Executive–Policy packet mode.

The host compiles a compact DecisionPacket for each round; MockPolicy picks the
first legal candidate. The experiment reports which rounds skipped the model
(fast path) and how much smaller the packet is than the legacy context.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from ..core.hashing import canonical_json
from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..domains.finance import FINANCE_DOMAIN, verify_observation
from ..memory.cases import CaseIndex
from ..memory.facts import FactStore
from ..providers.deepseek import DeepSeekPolicy, PriceSnapshot
from ..providers.deepseek_config import load_deepseek_settings
from ..providers.local_policy import LocalPolicy
from ..providers.mock import MockProvider
from ..runtime.budget import BudgetLimits
from ..runtime.harness import Harness
from ..runtime.loop import Loop
from ..runtime.policy import MockPolicy, compile_packet
from ..tools.artifacts import ArtifactStore
from ..tools.connectors import default_registry
from ..tools.evidence import EvidenceStore
from ..tools.finance.facts import project_facts
from ..tools.finance.fixtures import FinanceWorld
from ..tools.finance.planner import FinancePlanner
from ..tools.finance.tools import build_finance_tools
from ..tools.registry import ToolRegistry
from ..trajectory.exporter import export_trace, verify_trace
from ..trajectory.recorder import TraceRecorder
from .fixtures import get_scenario

__all__ = ["run"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def run(
    *,
    workdir: Path,
    scenario_key: str,
    fresh: bool,
    policy_name: str = "mock",
    endpoint: str = "http://127.0.0.1:11434/v1",
    model: str = "qwen3-4b-fcode",
    json_mode: bool = False,
    timeout: float = 120.0,
    price: PriceSnapshot | None = None,
    api_key: str = "",
    base_url: str = "https://api.deepseek.com/v1",
    max_tokens: int = 64,
    max_rounds: int | None = None,
    limits: BudgetLimits | None = None,
) -> dict[str, Any]:
    scenario = get_scenario(scenario_key)
    workdir = workdir.expanduser().resolve()
    if workdir in (Path("/"), PROJECT_ROOT):
        raise ValueError(f"refusing to use {workdir} as a lab workdir")
    if fresh and workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    store = TaskStore(workdir / "task.sqlite")
    world = FinanceWorld(partial=scenario.partial_corpus)
    cases = CaseIndex(workdir / "cases.sqlite")
    facts = FactStore(workdir / "facts.sqlite")
    registry = ToolRegistry(build_finance_tools(
        world,
        EvidenceStore(workdir / "evidence"),
        ArtifactStore(workdir / "artifacts"),
        default_registry(),
        cases,
        facts,
    ))
    receipts = ReceiptStore(store)
    provider = MockProvider(policy=scenario.policy)
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=limits or scenario.limits,
        receipts=receipts,
        verifier=lambda observation: verify_observation(world, observation),
    )
    if policy_name == "local":
        policy = LocalPolicy(
            base_url=endpoint, model=model, timeout=timeout, json_mode=json_mode,
        )
    elif policy_name == "deepseek":
        if price is None:
            raise ValueError("deepseek policy requires a price snapshot")
        policy = DeepSeekPolicy(
            api_key=api_key,
            price=price,
            model=model,
            base_url=base_url,
            timeout=timeout,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    else:
        policy = MockPolicy()
    recorder = TraceRecorder(
        store,
        provider_name=policy.name,
        model_name=policy.model,
    )
    task = harness.create(
        task_id=f"task-packet-{scenario.key}",
        objective=scenario.objective,
        principal="packet-lab",
        binding={"scenario": scenario.key, "module": "finance-research", "source": "packet-lab"},
        allowed_tools=list(scenario.allowed_tools),
        grant=dict(scenario.grant),
    )
    loop = Loop(
        harness,
        planner=FinancePlanner(),
        max_rounds=max_rounds or scenario.max_rounds,
        observer=recorder,
        policy=policy,
        result_tools=FINANCE_DOMAIN.result_tools,
    )
    memory = loop.restore(task)
    plan = loop.rebuild_plan(task, memory)
    budget = harness.budget(task).snapshot()
    packet = compile_packet(
        objective=task.objective,
        round_no=1,
        plan=plan,
        memory=memory,
        registry=harness.registry,
        budget=budget,
    )
    legacy = memory.build(1, budget, plan)
    packet_bytes = len(canonical_json(packet.to_dict()).encode("utf-8"))
    legacy_bytes = len(canonical_json(legacy).encode("utf-8"))

    try:
        task = loop.run(task.id)
        final = recorder.build(store.load(task.id))
        run_id = f"packet-{scenario.key}"
        cases.upsert(final, run_id=run_id)
        facts.upsert_run(run_id=run_id, facts=project_facts(final, run_id=run_id))
        trace_path = export_trace(final, workdir / "trace.json")
        try:
            verify_trace(final)
            verified = True
        except Exception:  # noqa: BLE001 — surfaced in the summary
            verified = False
        final_budget = harness.budget(store.load(task.id)).snapshot()
        events = final["events"]
        types = [event["type"] for event in events]
        requested = [
            event["payload"].get("detail") or {}
            for event in events if event["type"] == "policy_requested"
        ]
        decided = [
            event["payload"].get("detail") or {}
            for event in events if event["type"] == "policy_decided"
        ]
    finally:
        store.close()

    packet_sizes = [int(item.get("packet_bytes") or 0) for item in requested]
    packet_avg = round(sum(packet_sizes) / len(packet_sizes)) if packet_sizes else packet_bytes
    latencies = [int(item.get("latency_ms") or 0) for item in decided]
    return {
        "scenario": scenario.key,
        "policy": policy.name,
        "model": policy.model,
        "status": task.status,
        "failure_code": (task.failure or {}).get("code", ""),
        "change_bp": (task.result or {}).get("change_bp"),
        "rounds": len(final.get("rounds") or []),
        "events": len(events),
        "model_calls": types.count("model_started"),
        "policy_decisions": types.count("policy_decided"),
        "auto_actions": types.count("auto_action"),
        "repaired": sum(1 for item in decided if item.get("repaired")),
        "policy_tokens": sum(int(item.get("tokens") or 0) for item in decided),
        "policy_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
        "cost_microusd": final_budget.get("cost_used_microusd"),
        "tokens": final_budget.get("tokens_used"),
        "packet_bytes": packet_bytes,
        "packet_avg_bytes": packet_avg,
        "legacy_context_bytes": legacy_bytes,
        "reduction_pct": round((1 - packet_bytes / legacy_bytes) * 100, 1) if legacy_bytes else 0.0,
        "avg_reduction_pct": round((1 - packet_avg / legacy_bytes) * 100, 1) if legacy_bytes else 0.0,
        "trace": str(trace_path),
        "verified": verified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-packet-lab")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--scenario", default="full")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--policy", choices=("mock", "local", "deepseek"), default="mock")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default=None)
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--input-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--output-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--cache-hit-price-microusd-per-mtok", type=int, default=None)
    parser.add_argument("--price-as-of", default=None)
    parser.add_argument("--price-source", default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--max-rounds", type=int, default=0)
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-reads", type=int, default=None)
    parser.add_argument("--max-cost-microusd", type=int, default=None)
    parser.add_argument("--max-tokens-budget", type=int, default=None)
    args = parser.parse_args(argv)

    price = None
    api_key = ""
    model = args.model or "qwen3-4b-fcode"
    json_mode = args.json_mode
    timeout = args.timeout if args.timeout is not None else 120.0
    base_url = args.base_url or "https://api.deepseek.com/v1"
    max_tokens = args.max_tokens if args.max_tokens is not None else 64
    limits = None
    if args.policy == "deepseek":
        try:
            settings = load_deepseek_settings(
                api_key_env=args.api_key_env,
                base_url=args.base_url,
                model=args.model,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                max_packet_tokens=None,
                input_price=args.input_price_microusd_per_mtok,
                output_price=args.output_price_microusd_per_mtok,
                cache_hit_price=args.cache_hit_price_microusd_per_mtok,
                price_as_of=args.price_as_of,
                price_source=args.price_source,
            )
        except ValueError as error:
            parser.error(str(error))
        price = settings.price
        api_key = settings.api_key
        model = settings.model
        json_mode = True
        timeout = settings.timeout
        base_url = settings.base_url
        max_tokens = settings.max_tokens
        from .api import budget_limits_from_overrides

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
        policy_name=args.policy,
        endpoint=args.endpoint,
        model=model,
        json_mode=json_mode,
        timeout=timeout,
        price=price,
        api_key=api_key,
        base_url=base_url,
        max_tokens=max_tokens,
        max_rounds=args.max_rounds or None,
        limits=limits,
    )
    expected = get_scenario(args.scenario).expected_status
    summary["expected_status"] = expected
    summary["outcome_ok"] = summary["status"] == expected
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verified"] and summary["outcome_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

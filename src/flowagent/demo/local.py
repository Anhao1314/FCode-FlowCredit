"""Run one scenario with a local OpenAI-compatible model.

The runtime, tools, gates and chain are the production ones; only the
proposal source changes. This is the full-proposal counterpart to the packet
acceptance harness: it exercises the prompt version the model was trained on
and verifies the resulting trace.

Example::

    PYTHONPATH=src python3 -m flowagent.demo.local \
      --workdir /tmp/fcode-local-full --scenario full --fresh \
      --endpoint http://127.0.0.1:11434/v1 --model fcode-qwen3-4b
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..domains.finance import FINANCE_DOMAIN, verify_observation
from ..tools.finance.summaries import summarize_state as _finance_state
from ..memory.cases import CaseIndex
from ..memory.facts import FactStore
from ..providers.base import Provider
from ..providers.local import LocalProvider
from ..providers.prompt import PROMPT_VERSION
from ..runtime.budget import BudgetLimits
from ..runtime.harness import Harness
from ..runtime.loop import Loop
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
from .oracle import LabObserver

__all__ = ["run", "main"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def run(
    *,
    workdir: Path,
    scenario_key: str,
    fresh: bool,
    endpoint: str,
    model: str,
    timeout: float,
    max_rounds: int | None = None,
    json_mode: bool = False,
    structured: bool = False,
    provider: Provider | None = None,
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
    if provider is None:
        provider = LocalProvider(
            base_url=endpoint,
            model=model,
            timeout=timeout,
            json_mode=json_mode,
            structured=structured,
        )
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=limits or scenario.limits,
        receipts=receipts,
        verifier=lambda observation: verify_observation(world, observation),
        state_fn=_finance_state,
    )
    recorder = TraceRecorder(store, provider_name=provider.name, model_name=provider.model)
    observer = LabObserver(recorder)
    task = harness.create(
        task_id=f"task-local-{scenario.key}-0001",
        objective=scenario.objective,
        principal="local-operator",
        binding={
            "scenario": scenario.key,
            "corpus": "synthetic-authored-v1",
            "source": "local-lab",
            "prompt_version": PROMPT_VERSION,
        },
        allowed_tools=list(scenario.allowed_tools),
        grant=scenario.grant,
    )

    print(f"[lab] scenario={scenario.key} workdir={workdir}", flush=True)
    print(f"[lab] prompt_version={PROMPT_VERSION} model={model} endpoint={endpoint}", flush=True)
    try:
        task = Loop(
            harness,
            planner=FinancePlanner(),
            max_rounds=max_rounds or scenario.max_rounds,
            observer=observer,
            result_tools=FINANCE_DOMAIN.result_tools,
        ).run(task.id)
        final = recorder.build(store.load(task.id))
        run_id = f"lab-{scenario.key}"
        cases.upsert(final, run_id=run_id)
        facts.upsert_run(run_id=run_id, facts=project_facts(final, run_id=run_id))
        trace_path = export_trace(final, workdir / "trace.json")
        try:
            verify_trace(final)
            verified = True
        except Exception:  # noqa: BLE001 — surfaced in the summary
            verified = False
        budget = harness.budget(store.load(task.id)).snapshot()
    finally:
        store.close()

    return {
        "scenario": scenario.key,
        "expected_status": scenario.expected_status,
        "status": task.status,
        "outcome_ok": task.status == scenario.expected_status,
        "failure": task.failure,
        "rounds": len(final.get("rounds") or []),
        "events": len(final.get("events") or []),
        "calls": budget.get("calls_used"),
        "cost_microusd": budget.get("cost_used_microusd"),
        "tokens": budget.get("tokens_used"),
        "trace": str(trace_path),
        "verified": verified,
        "prompt_version": PROMPT_VERSION,
        "provider": provider.name,
        "model": provider.model,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-local-lab")
    parser.add_argument("--workdir", required=True, help="isolated experiment directory")
    parser.add_argument("--scenario", default="full")
    parser.add_argument("--fresh", action="store_true", help="delete the workdir first")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--model", default="fcode-qwen3-4b")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-rounds", type=int, default=0)
    parser.add_argument("--json-mode", action="store_true", help="constrain the endpoint to JSON output")
    parser.add_argument("--structured", action="store_true", help="constrain output to the plan schema")
    args = parser.parse_args(argv)

    summary = run(
        workdir=Path(args.workdir),
        scenario_key=args.scenario,
        fresh=args.fresh,
        endpoint=args.endpoint,
        model=args.model,
        timeout=args.timeout,
        max_rounds=args.max_rounds or None,
        json_mode=args.json_mode,
        structured=args.structured,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verified"] and summary["outcome_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

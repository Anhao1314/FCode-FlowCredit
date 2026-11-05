"""Run one scenario with the file-backed oracle as the model.

This is the experiment rig for the LLM layer. The runtime and tools are the
production ones; only the proposal source changes. The operator writes one
action per round into ``<workdir>/exchange`` after reading the prompt the
runtime actually rendered.

Example::

    PYTHONPATH=src python3 -m flowagent.demo.oracle \
      --workdir /tmp/fcode-oracle-full --scenario full --fresh

The process blocks on each round. In another terminal, read
``round-NN.request.json`` and write ``round-NN.reply.json``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..domains.finance import FINANCE_DOMAIN, verify_observation
from ..tools.finance.summaries import summarize_state as _finance_state
from ..memory.cases import CaseIndex
from ..memory.facts import FactStore
from ..providers.oracle import OracleFileProvider
from ..providers.prompt import PROMPT_VERSION
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

__all__ = ["LabObserver", "run"]

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class LabObserver:
    """Print what the runtime decided, and record it on the trace."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder

    @staticmethod
    def _line(text: str) -> None:
        print(text, flush=True)

    def round_start(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        self.recorder.round_start(round_no, budget, plan)
        self._line(f"\n[round {round_no}] calls={budget.get('calls_used')} cost={budget.get('cost_used_microusd')}")

    def proposal(self, round_no: int, proposal: Any) -> None:
        self.recorder.proposal(round_no, proposal)
        steps = proposal.plan.get("steps") or []
        step = steps[0] if steps else {}
        self._line(f"  propose {step.get('tool')} {step.get('arguments') or {}} todo={proposal.todo_id}")

    def gate(self, round_no: int, decision: Any) -> None:
        self.recorder.gate(round_no, decision)
        verdict = "PASS" if decision.allowed else f"DENIED {decision.code}: {decision.reason}"
        self._line(f"  gate    {verdict}")

    def observation(self, round_no: int, observation: Any) -> None:
        self.recorder.observation(round_no, observation)
        self._line(f"  result  {observation.tool} hash={observation.result_hash[:12]}")

    def round_end(self, round_no: int, budget: dict[str, Any], plan: Any = None) -> None:
        self.recorder.round_end(round_no, budget, plan)

    def terminal(self, task: Any, result: dict[str, Any] | None) -> None:
        self.recorder.terminal(task, result)
        failure = task.failure or {}
        self._line(f"\n[terminal] {task.status} {failure.get('code') or ''}".rstrip())


def run(
    *,
    workdir: Path,
    scenario_key: str,
    fresh: bool,
    timeout: float,
    max_rounds: int | None = None,
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
    provider = OracleFileProvider(workdir / "exchange", timeout=timeout)
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=scenario.limits,
        receipts=receipts,
        verifier=lambda observation: verify_observation(world, observation),
        state_fn=_finance_state,
    )
    recorder = TraceRecorder(store, provider_name=provider.name, model_name=provider.model)
    observer = LabObserver(recorder)
    task = harness.create(
        task_id=f"task-oracle-{scenario.key}-0001",
        objective=scenario.objective,
        principal="oracle-operator",
        binding={
            "scenario": scenario.key,
            "corpus": "synthetic-authored-v1",
            "source": "oracle-lab",
            "prompt_version": PROMPT_VERSION,
        },
        allowed_tools=list(scenario.allowed_tools),
        grant=scenario.grant,
    )

    print(f"[lab] scenario={scenario.key} workdir={workdir}", flush=True)
    print(f"[lab] prompt_version={PROMPT_VERSION} timeout={timeout}s", flush=True)
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
        "status": task.status,
        "failure": task.failure,
        "rounds": len(final.get("rounds") or []),
        "events": len(final.get("events") or []),
        "calls": budget.get("calls_used"),
        "cost_microusd": budget.get("cost_used_microusd"),
        "tokens": budget.get("tokens_used"),
        "trace": str(trace_path),
        "verified": verified,
        "prompt_version": PROMPT_VERSION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowagent-oracle-lab")
    parser.add_argument("--workdir", required=True, help="isolated experiment directory")
    parser.add_argument("--scenario", default="full")
    parser.add_argument("--fresh", action="store_true", help="delete the workdir first")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-rounds", type=int, default=0)
    args = parser.parse_args(argv)

    summary = run(
        workdir=Path(args.workdir),
        scenario_key=args.scenario,
        fresh=args.fresh,
        timeout=args.timeout,
        max_rounds=args.max_rounds or None,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Turn FCode traces into an SFT dataset for the local-model provider.

Two sources, one renderer (:mod:`flowagent.providers.prompt`):

* **reconstruction** — every signed trace on disk becomes samples. The round
  context is rebuilt with the *real* :class:`WorkingMemory` fed from earlier
  rounds' observations, plus the ``budget_before`` and ``plan`` recorded in
  the round itself, so what the model sees in training is byte-identical to
  what it will see in inference. ``tests/test_sft_export.py`` proves that
  equality against a live loop run; this module never hand-rolls the context.
* **generation** — the offline stack is deterministic and objective-independent
  (the planner returns a fixed blueprint), so fresh episodes are generated
  in-process with varied objective phrasings while a recording provider
  captures the exact contexts. That is the whole augmentation strategy:
  diversity comes from real runs, never from editing samples by hand.

Rounds the model was never asked about (reconciliation replays) and traces
with injected-failure policies (escalation) are excluded by design — the
dataset must imitate the *research* policy only.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..core.plan import Plan
from ..core.receipts import ReceiptStore
from ..core.store import TaskStore
from ..demo.fixtures import get_scenario
from ..memory.context import WorkingMemory
from ..providers.base import Proposal
from ..providers.mock import MockProvider
from ..providers.prompt import PROMPT_VERSION, build_target_json, render_prompt
from ..runtime.harness import Harness
from ..runtime.loop import Loop
from ..tools.base import Observation
from ..tools.finance.fixtures import FinanceWorld
from ..tools.finance.planner import FinancePlanner
from ..tools.finance.tools import build_finance_tools
from ..tools.registry import ToolRegistry
from ..domains.finance import FINANCE_DOMAIN, verify_observation as verify_finance

__all__ = ["reconstruct_trace", "generate_episodes", "samples_to_sharegpt", "export_main"]

#: Scenarios whose proposals follow the research policy. tool-denied injects an
#: escalation on purpose; training on it would teach the model to break the gate.
POLICY_SCENARIOS = {"full", "budget", "budget-resume", "crash", "unknown", "open-ended"}

GENERATION_SCENARIOS = ["full", "budget", "budget-resume", "unknown", "open-ended"]


def _tool_catalog() -> list[dict[str, Any]]:
    return ToolRegistry(build_finance_tools(FinanceWorld())).catalog()


def _model_asked_rounds(trace: dict[str, Any]) -> set[int]:
    return {
        int((event.get("payload") or {}).get("detail", {}).get("round", 0))
        for event in trace.get("events", [])
        if event.get("type") == "model_started"
    }


def reconstruct_trace(trace: dict[str, Any], *, source_id: str = "trace") -> list[dict[str, Any]]:
    """Rebuild (context, steps, todo_id) for every round the model was asked.

    Only ``tool_succeeded`` observations feed the working memory — the same
    filter :meth:`Loop.restore` uses, so a resumed or crashed trace replays
    into the same memory the live loop had.
    """
    task = trace.get("task") or {}
    objective = task.get("objective", "")
    scenario = (task.get("binding") or {}).get("scenario", "")
    if scenario and scenario not in POLICY_SCENARIOS:
        return []
    catalog = _tool_catalog()
    asked = _model_asked_rounds(trace)
    prior_cases: list[dict[str, Any]] = []
    for event in trace.get("events") or []:
        if event.get("type") == "memory_recalled":
            detail = event.get("payload", {}).get("detail") or {}
            prior_cases = list(detail.get("hits") or [])
    from ..tools.finance.summaries import FINANCE_SUMMARIZERS, summarize_state as finance_state

    memory = WorkingMemory(
        objective=objective,
        tool_catalog=catalog,
        prior_cases=prior_cases,
        summarizers=FINANCE_SUMMARIZERS,
        state_fn=finance_state,
    )
    samples: list[dict[str, Any]] = []
    for record in trace.get("rounds") or []:
        index = int(record.get("index", 0))
        proposal = record.get("proposal")
        if index not in asked or not proposal or not proposal.get("steps"):
            continue
        plan_obj = Plan.from_dict(record["plan"]) if record.get("plan") else Plan(goal=objective)
        context = memory.build(
            round_no=index,
            budget_snapshot=record.get("budget_before") or {},
            plan=plan_obj,
        )
        # harness.propose hands the provider {**context, "intent": ...}; the
        # intent is fully derivable, so training sees the same object.
        from ..core.hashing import digest

        context = {
            **context,
            "intent": {
                "task_id": task.get("id", ""),
                "round": index,
                "objective": objective,
                "state_hash": digest(context["state"]),
            },
        }
        receipt = record.get("receipt") or {}
        samples.append({
            "context": context,
            "steps": proposal["steps"],
            "todo_id": receipt.get("todo_id"),
            "source": f"{source_id}#r{index}",
        })
        observation = record.get("observation")
        if observation and observation.get("tool"):
            memory.record(
                index,
                Observation(
                    tool=observation["tool"],
                    arguments=dict(observation.get("arguments") or {}),
                    result=dict(observation.get("result") or {}),
                    result_hash=observation.get("result_hash", ""),
                ),
            )
    return samples


OBJECTIVE_TEMPLATES = [
    "判断恒星智算 2024 与 2025 两期合并毛利率是否可比，并在可比时给出变化",
    "评估恒星智算近两年合并毛利率的可比性，可比则计算变动幅度",
    "核对恒星智算 FY2024 与 FY2025 的毛利率口径，一致时报告增减",
    "恒星智算两年毛利率能直接对比吗？请先核对口径与重述再下结论",
    "分析恒星智算 2024/2025 合并毛利率变化，注意单位与会计准则差异",
    "复核恒星智算两期毛利率：先确认可比性，再给出百分点变化",
    "请比较恒星智算两年合并毛利率；若口径不可比请明确说明原因",
    "对恒星智算 2024 与 2025 年合并毛利率做可比性审查并量化变化",
    "查证恒星智算毛利率口径（单位、准则、重述），得出可比结论",
    "恒星智算 FY2024→FY2025 毛利率变动多少？不可比时不要给数",
    "审计式复核：恒星智算两期毛利率是否同口径，变化几何",
    "给定合成语料，判断恒星智算两期毛利率可比性并给出变化值",
    "研究任务：恒星智算毛利率两年对比，要求口径一致后再计算",
    "毛利率可比性检查（恒星智算 2024 vs 2025），输出变化或阻塞原因",
    "请以 IFRS 口径统一恒星智算两年数据后计算毛利率变化",
    "恒星智算的毛利率两年间改善还是恶化？先验证可比性",
    "比较恒星智算合并毛利率 FY2024 与 FY2025，说明重述影响",
    "核对单位与重述后，报告恒星智算两期毛利率的变化",
    "恒星智算毛利率分析：可比性、口径桥接、最终变化",
    "两年毛利率对比研究（恒星智算），不满足证据时如实阻塞",
]


class RecordingProvider:
    """Delegates to the mock and records every (context, proposal) pair."""

    name = "offline-mock"
    model = "offline-deterministic-mock-v1"

    def __init__(self, inner: MockProvider) -> None:
        self.inner = inner
        self.captured: list[dict[str, Any]] = []
        self.proposals: list[Proposal] = []

    def propose(self, context: dict[str, Any]) -> Proposal:
        proposal = self.inner.propose(context)
        self.captured.append(copy.deepcopy(context))
        self.proposals.append(proposal)
        return proposal


def generate_episodes(episodes_per_scenario: int) -> list[dict[str, Any]]:
    """Run fresh offline episodes and capture contexts directly.

    The stack is the same one ``tests/test_control.py`` drives; the recording
    provider wraps the mock so the captured contexts are the literal argument
    objects the runtime handed over, not a reconstruction.
    """
    samples: list[dict[str, Any]] = []
    for scenario_key in GENERATION_SCENARIOS:
        scenario = get_scenario(scenario_key)
        for episode in range(episodes_per_scenario):
            objective = OBJECTIVE_TEMPLATES[(episode + len(scenario_key)) % len(OBJECTIVE_TEMPLATES)]
            with tempfile.TemporaryDirectory() as tmp:
                store = TaskStore(Path(tmp) / "task.sqlite")
                world = FinanceWorld(partial=scenario.partial_corpus)
                registry = ToolRegistry(build_finance_tools(world))
                receipts = ReceiptStore(store)
                provider = RecordingProvider(MockProvider(policy=scenario.policy))
                harness = Harness(
                    store=store,
                    registry=registry,
                    provider=provider,
                    world=world,
                    limits=scenario.limits,
                    receipts=receipts,
                    verifier=lambda observation: verify_finance(world, observation),
                )
                task = harness.create(
                    task_id=f"sft-{scenario_key}-{episode}",
                    objective=objective,
                    principal="sft-exporter",
                    binding={"scenario": scenario_key, "corpus": "synthetic-authored-v1", "source": "sft-generation"},
                    allowed_tools=list(scenario.allowed_tools),
                    grant=dict(scenario.grant),
                )
                Loop(
                    harness,
                    planner=FinancePlanner(),
                    max_rounds=scenario.max_rounds,
                    result_tools=FINANCE_DOMAIN.result_tools,
                ).run(task.id)
                store.close()
            for index, (context, proposal) in enumerate(zip(provider.captured, provider.proposals), start=1):
                samples.append({
                    "context": context,
                    "steps": proposal.plan["steps"],
                    "todo_id": proposal.todo_id,
                    "source": f"gen:{scenario_key}-{episode}#r{index}",
                })
    return samples


def samples_to_sharegpt(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Render samples through the shared prompt contract and dedupe."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for sample in samples:
        system, user = render_prompt(sample["context"])
        if user in seen:
            skipped += 1
            continue
        seen.add(user)
        target = build_target_json(sample["steps"], sample.get("todo_id"))
        records.append({
            "conversations": [
                {"from": "system", "value": system},
                {"from": "user", "value": user},
                {"from": "assistant", "value": target},
            ]
        })
    return records, {"unique": len(records), "duplicates_skipped": skipped}


def export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowagent-sft-export",
        description="Export signed FCode traces (and fresh generated episodes) as an SFT dataset.",
    )
    parser.add_argument("--out", required=True, help="output JSONL path (sharegpt format)")
    parser.add_argument("--traces", nargs="*", default=[], help="trace JSON files or globs")
    parser.add_argument("--generate", type=int, default=0, help="episodes per scenario to generate in-process")
    parser.add_argument("--stats-out", default="", help="optional JSON stats path")
    parser.add_argument(
        "--module",
        default="finance-research",
        help="domain module this dataset trains (datasets are per-module)",
    )
    args = parser.parse_args(argv)

    from ..domains import get_domain

    module = get_domain(args.module)
    if module.key != "finance-research":
        parser.error(f"module {module.key!r} has no exporter yet")

    import glob

    samples: list[dict[str, Any]] = []
    trace_files: list[str] = []
    for pattern in args.traces:
        trace_files.extend(sorted(glob.glob(pattern)))
    for path in trace_files:
        try:
            trace = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print(f"skip {path}: {error}", file=sys.stderr)
            continue
        samples.extend(reconstruct_trace(trace, source_id=Path(path).stem))
    generated = generate_episodes(args.generate) if args.generate else []
    samples.extend(generated)

    records, stats = samples_to_sharegpt(samples)
    stats["from_traces"] = len(samples) - len(generated)
    stats["generated"] = len(generated)
    stats["prompt_version"] = PROMPT_VERSION
    stats["module"] = module.key

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} samples -> {out}  ({stats})")
    if args.stats_out:
        Path(args.stats_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(export_main())

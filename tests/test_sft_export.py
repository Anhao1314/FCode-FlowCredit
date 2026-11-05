"""The SFT exporter must reproduce exactly what the runtime showed the model.

This is the linchpin test of the behaviour-cloning contract: a live loop runs
with a recording provider that captures the literal context objects, the trace
is exported to disk, and the exporter reconstructs contexts from that trace.
Any difference — one byte of JSON, one missing observation — would be a silent
distribution shift between training and inference, so the comparison is on the
rendered prompt strings, not on Python dicts.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.providers.prompt import (  # noqa: E402
    PLAN_VERSION,
    build_target_json,
    parse_proposal_text,
    render_prompt,
)
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.sft.export import (  # noqa: E402
    RecordingProvider,
    reconstruct_trace,
    samples_to_sharegpt,
)
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import export_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation as verify_finance  # noqa: E402


def run_episode(
    scenario_key: str,
    tmp: Path,
    prior_cases: list[dict] | None = None,
) -> tuple[dict, list, list]:
    """Run one real episode; return (trace dict, captured contexts, proposals)."""
    scenario = get_scenario(scenario_key)
    store = TaskStore(tmp / "task.sqlite")
    world = FinanceWorld(partial=scenario.partial_corpus)
    registry = ToolRegistry(build_finance_tools(world))
    receipts = ReceiptStore(store)
    provider = RecordingProvider(MockProvider(policy=scenario.policy))
    recorder = TraceRecorder(store, provider_name=provider.name, model_name=provider.model)
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        verifier=lambda observation: verify_finance(world, observation),
        state_fn=FINANCE_DOMAIN.summarize_state,
        limits=scenario.limits,
        receipts=receipts,
    )
    task = harness.create(
        task_id=f"fidelity-{scenario_key}",
        objective=scenario.objective,
        principal="test",
        binding={"scenario": scenario_key, "corpus": "synthetic-authored-v1", "source": "sft-fidelity"},
        allowed_tools=list(scenario.allowed_tools),
        grant=dict(scenario.grant),
    )
    if prior_cases:
        task = harness.record_recall(task, prior_cases)
    Loop(
        harness,
        planner=FinancePlanner(),
        max_rounds=scenario.max_rounds,
        observer=recorder,
        result_tools=FINANCE_DOMAIN.result_tools,
    ).run(task.id)
    trace = recorder.build(store.load(task.id))
    store.close()
    return trace, provider.captured, provider.proposals


class FidelityTests(unittest.TestCase):
    def test_finance_prompt_v10_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, captured, _ = run_episode("full", Path(tmp))
            canonical = json.dumps(
                captured,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.assertEqual(
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "f2b00e0d56e2bf080eacad6ea06879847c95e21e6c3d5354c8d4ebd5884db3e1",
            )

    def test_reconstructed_prompts_match_live_contexts_byte_for_byte(self) -> None:
        for scenario_key in ("full", "budget", "unknown", "open-ended", "budget-resume"):
            with self.subTest(scenario=scenario_key), tempfile.TemporaryDirectory() as tmp:
                trace, captured, proposals = run_episode(scenario_key, Path(tmp))
                samples = reconstruct_trace(trace, source_id=f"fidelity-{scenario_key}")
                self.assertEqual(
                    len(samples), len(captured),
                    f"{scenario_key}: sample count must equal rounds the model was asked",
                )
                for sample, context, proposal in zip(samples, captured, proposals):
                    live_system, live_user = render_prompt(context)
                    rebuilt_system, rebuilt_user = render_prompt(sample["context"])
                    self.assertEqual(live_system, rebuilt_system)
                    self.assertEqual(live_user, rebuilt_user, f"{sample['source']} drifted")
                    self.assertEqual(
                        build_target_json(sample["steps"], sample["todo_id"]),
                        build_target_json(proposal.plan["steps"], proposal.todo_id),
                    )

    def test_export_replays_the_frozen_memory_recall(self) -> None:
        hits = [
            {
                "run_id": "run-older",
                "objective": "恒星智算两期毛利率可比性",
                "outcome": "succeeded",
                "summary": "FY2024 17.50%，FY2025 15.00%，下降 2.50 个百分点。",
                "chain_head": "b" * 64,
                "tags": ["finance-research", "FY2024"],
                "created_at": "2026-09-01T00:00:00+00:00",
                "archived": False,
                "score_bp": 8123,
                "breakdown": {"relevance_bp": 7000, "importance_bp": 9000, "recency_bp": 9000},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            trace, captured, _ = run_episode("full", Path(tmp), prior_cases=hits)
            samples = reconstruct_trace(trace, source_id="recall")
            self.assertEqual(len(samples), len(captured))
            for sample, context in zip(samples, captured):
                self.assertIn("prior_cases", context)
                self.assertEqual(context["prior_cases"][0]["run_id"], "run-older")
                self.assertEqual(
                    render_prompt(sample["context"]),
                    render_prompt(context),
                )

    def test_reconstruction_ignores_reconciled_and_escalation_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # escalation scenario must yield zero samples by policy
            trace, _, _ = run_episode("tool-denied", Path(tmp))
            self.assertEqual(reconstruct_trace(trace, source_id="td"), [])
        with tempfile.TemporaryDirectory() as tmp:
            # the unknown scenario replays one proposal without asking the model
            trace, captured, _ = run_episode("unknown", Path(tmp))
            samples = reconstruct_trace(trace, source_id="unk")
            self.assertEqual(len(samples), len(captured))
            asked_rounds = {
                event["payload"]["detail"]["round"]
                for event in trace["events"] if event["type"] == "model_started"
            }
            for sample in samples:
                self.assertIn(int(sample["source"].rsplit("r", 1)[1]), asked_rounds)

    def test_sharegpt_records_are_valid_training_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace, _, _ = run_episode("full", Path(tmp))
            samples = reconstruct_trace(trace, source_id="full")
            records, stats = samples_to_sharegpt(samples)
            self.assertGreater(records[0]["conversations"][0]["value"].find("FCode"), 0)
            for record in records:
                turns = record["conversations"]
                self.assertEqual([t["from"] for t in turns], ["system", "user", "assistant"])
                user = json.loads(turns[1]["value"])
                self.assertIn("context", user)
                assistant = parse_proposal_text(turns[2]["value"])
                self.assertEqual(assistant["version"], PLAN_VERSION)
                self.assertTrue(assistant["steps"])

    def test_parse_proposal_text_rejects_garbage(self) -> None:
        two_actions = (
            '{"version": "%s", "steps": ['
            '{"tool": "read_file", "arguments": {"path": "a"}},'
            '{"tool": "read_file", "arguments": {"path": "b"}}]}'
        ) % PLAN_VERSION
        unknown_field = (
            '{"version": "%s", "steps": [{"tool": "read_file"}], "note": "ignored"}'
        ) % PLAN_VERSION
        for bad in ("no json", "[]", '{"steps": []}', '{"steps": [{"tool": 1}]}',
                    '{"version": "wrong", "steps": [{"tool": "list_documents"}]}',
                    '{"version": "%s", "steps": [{"tool": "bad name"}]}' % PLAN_VERSION,
                    two_actions, unknown_field):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_proposal_text(bad)

    def test_parse_proposal_text_tolerates_thinking_and_prose(self) -> None:
        good = '{"version": "%s", "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}], "todo_id": "t1"}' % PLAN_VERSION
        for wrapper in (
            lambda t: t,
            lambda t: "<think>理由…</think>" + t,
            lambda t: "好的，下一步是：\n" + t + "\n以上。",
        ):
            parsed = parse_proposal_text(wrapper(good))
            self.assertEqual(parsed["todo_id"], "t1")


if __name__ == "__main__":
    unittest.main()

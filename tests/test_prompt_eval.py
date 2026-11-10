"""Prompt eval harness: success is host-computed, injection escapes are counted."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.evaluation.prompt_eval import (  # noqa: E402
    aggregate,
    provider_env,
    run_task,
    task_by_id,
)
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.providers.prompt import PROMPT_PROFILES  # noqa: E402


class ScriptedProvider:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.calls = 0

    def propose(self, context: dict) -> Proposal:
        self.calls += 1
        if not self.steps:
            step = {"id": "stop", "tool": "report_blocked",
                    "arguments": {"reason": "script exhausted"}}
        else:
            step = self.steps.pop(0)
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": [step]},
            provider=self.name, model=self.model, tokens=10, cost_microusd=1,
        )


def _read(path: str) -> dict:
    return {"id": "read", "tool": "read_file", "arguments": {"path": path}}


def _write(path: str, content: str) -> dict:
    return {"id": "write", "tool": "write_file",
            "arguments": {"path": path, "content": content}}


class PromptEvalTests(unittest.TestCase):
    def test_normal_task_succeeds_without_violations(self) -> None:
        provider = ScriptedProvider([
            _read("README.md"),
            _write("SUMMARY.md", "Payroll helpers live in stats.py.\n"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            row = run_task(
                task_by_id("readme-summary"),
                profile="general",
                provider_name="scripted",
                workdir=pathlib.Path(tmp) / "task",
                provider_builder=lambda *args, **kwargs: provider,
            )
        self.assertEqual(row["status"], "succeeded")
        self.assertTrue(row["success"], row)
        self.assertTrue(row["expected_ok"])
        self.assertEqual(row["violations"], [])
        self.assertEqual(row["model_calls"], 2)
        self.assertEqual(row["rounds"], 2)
        self.assertIn("stats.py", row["report_excerpt"])
        self.assertEqual(row["prompt_version"], "fcode.prompt/general-v9")

    def test_injected_write_is_counted_and_fails_success(self) -> None:
        provider = ScriptedProvider([
            _read("README.md"),
            _write("PWNED.md", "owned\n"),
            _write("SUMMARY.md", "Payroll project.\n"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            row = run_task(
                task_by_id("injection-readme"),
                profile="general",
                provider_name="scripted",
                workdir=pathlib.Path(tmp) / "task",
                provider_builder=lambda *args, **kwargs: provider,
            )
        self.assertEqual(row["status"], "succeeded")
        self.assertFalse(row["success"], "a forbidden write must fail the task")
        self.assertTrue(row["violations"], row)
        self.assertEqual(row["violations"][0]["tool"], "write_file")
        self.assertIn("PWNED.md", json.dumps(row["violations"]))

    def test_candidate_prompt_runs_as_a_versioned_variant(self) -> None:
        provider = ScriptedProvider([
            _read("README.md"),
            _write("SUMMARY.md", "Payroll helpers live in stats.py.\n"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = pathlib.Path(tmp) / "candidate.txt"
            prompt_file.write_text("candidate system prompt", encoding="utf-8")
            row = run_task(
                task_by_id("readme-summary"),
                profile="general",
                provider_name="scripted",
                workdir=pathlib.Path(tmp) / "task",
                system_prompt_file=prompt_file,
                variant_label="general-v10-candidate",
                provider_builder=lambda *args, **kwargs: provider,
            )
        self.assertEqual(row["prompt_version"], "general-v10-candidate")
        self.assertFalse(any(key.startswith("eval-candidate-") for key in PROMPT_PROFILES))

    def test_aggregate_reports_tree_totals(self) -> None:
        rows = [
            {"success": True, "violations": [], "tokens": 10, "cost_microusd": 2,
             "rounds": 2, "wall_s": 1.0},
            {"success": False, "violations": [{"tool": "write_file"}], "tokens": 20,
             "cost_microusd": 3, "rounds": 3, "wall_s": 2.0},
        ]
        totals = aggregate(rows)
        self.assertEqual(totals["tasks"], 2)
        self.assertEqual(totals["success"], 1)
        self.assertEqual(totals["success_rate"], 0.5)
        self.assertEqual(totals["violations"], 1)
        self.assertEqual(totals["tokens"], 30)
        self.assertEqual(totals["cost_microusd"], 5)

    def test_provider_env_reads_the_saved_chat_config(self) -> None:
        config = {
            "providers": {
                "deepseek": {
                    "api_key": "test-key",
                    "model": "deepseek-test",
                    "input_price_microusd_per_mtok": 140000,
                    "output_price_microusd_per_mtok": 560000,
                    "price_as_of": "2026-11-09",
                    "price_source": "test",
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "chat-config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                env = provider_env(provider="deepseek", config_path=path)
        self.assertEqual(env["DEEPSEEK_API_KEY"], "test-key")
        self.assertEqual(env["DEEPSEEK_MODEL"], "deepseek-test")
        self.assertEqual(env["DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK"], "140000")


if __name__ == "__main__":
    unittest.main()
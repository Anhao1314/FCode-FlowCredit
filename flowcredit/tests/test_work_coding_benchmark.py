"""W0 Work coding benchmark: hidden tests stay host-held and offline runs verify."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.benchmark.runtime import ScriptedProvider  # noqa: E402
from flowagent.evaluation.coding_eval import run_coding_suite, run_coding_task  # noqa: E402
from flowagent.evaluation.coding_tasks import (  # noqa: E402
    BENCHMARK_VERSION,
    coding_taskset,
    coding_taskset_sha256,
    get_coding_task,
    hidden_tests_for,
)
from flowagent.core.hashing import digest  # noqa: E402


class TasksetContractTests(unittest.TestCase):
    def test_twelve_tasks_cover_the_declared_categories(self) -> None:
        tasks = coding_taskset()
        self.assertEqual(len(tasks), 12)
        self.assertEqual(
            sorted(task["category"] for task in tasks),
            sorted([
                "bug_single_file", "bug_single_file", "bug_single_file",
                "feature_multi_file", "feature_multi_file",
                "regression", "regression",
                "refactor", "refactor",
                "frontend", "frontend",
                "test_repair",
            ]),
        )
        self.assertTrue(all(task["version"] == BENCHMARK_VERSION for task in tasks))

    def test_public_taskset_never_carries_hidden_test_bytes(self) -> None:
        for task in coding_taskset():
            self.assertNotIn("source", task["hidden_test"])
            self.assertNotIn("hidden_test_source", task)
            self.assertTrue(task["hidden_test"]["content_sha256"])
            # The identity is still audit-visible.
            self.assertIsInstance(task["hidden_test"]["content_bytes"], int)
            self.assertGreater(task["hidden_test"]["content_bytes"], 0)

    def test_hidden_loader_returns_private_bytes_outside_workspace_contract(self) -> None:
        task = get_coding_task("single-file-off-by-one")
        hidden = hidden_tests_for(task["task_id"])["test.py"]
        self.assertIn("assert sum_to_n(100) == 5050", hidden)
        self.assertEqual(task["hidden_test"]["content_sha256"], digest(hidden))
        # The public definition is the only shape a model-facing artifact gets.
        public = get_coding_task(task["task_id"])
        self.assertNotIn(hidden, json.dumps(public, ensure_ascii=False))

    def test_forbidden_rules_protect_tests_not_implementation(self) -> None:
        task = get_coding_task("single-file-off-by-one")
        protected = {rule["path_contains"] for rule in task["forbidden"]}
        self.assertIn("tests/", protected)
        self.assertIn("sum_to_n.py", task["fixture"]["files"])

    def test_benchmark_hash_covers_public_and_hidden_identity(self) -> None:
        first = coding_taskset_sha256()
        self.assertEqual(first, coding_taskset_sha256())
        self.assertIsInstance(first, str)
        self.assertEqual(len(first), 64)


class OfflineRunnerTests(unittest.TestCase):
    def test_scripted_reference_passes_hidden_tests_and_explicit_denominators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_coding_suite(
                Path(tmp) / "out", task_ids=[
                    "single-file-off-by-one",
                    "multi-file-student-rate",
                    "regression-stale-cache",
                ],
            )
            self.assertTrue(result["all_case_expectations_met"])
            self.assertTrue(result["all_hidden_tests_passed"])
            self.assertEqual(result["metrics"]["goal_success_rate"]["denominator"], 3)
            self.assertEqual(result["metrics"]["goal_success_rate"]["numerator"], 3)
            self.assertEqual(result["metrics"]["coding"]["hidden_test_pass_rate"]["numerator"], 3)
            # Scripted proposals are charged like model dispatches; W0's
            # denominator is runs, not zero-cost evaluator execution.
            self.assertGreater(result["metrics"]["totals"]["calls"], 0)
            summary = json.loads(
                (Path(result["output"]) / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["all_hidden_tests_passed"])
            self.assertEqual(summary["manifest"]["online_model"]["status"], "NOT_RUN")
            self.assertNotIn(
                "assert sum_to_n(100) == 5050",
                (Path(result["output"]) / "taskset.json").read_text(encoding="utf-8"),
            )

    def test_hidden_tests_are_materialised_outside_the_model_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_coding_suite(
                Path(tmp) / "out", task_ids=["single-file-off-by-one"],
            )
            run_dir = Path(result["output"]) / "tasks" / "single-file-off-by-one" / "run-0"
            workspace = run_dir / "workspace"
            hidden_outside = run_dir / "hidden" / "test.py"
            self.assertTrue(hidden_outside.is_file())
            self.assertFalse((workspace / "hidden").exists())
            self.assertFalse((workspace / "test_hidden.py").exists())
            self.assertNotIn("hidden", [p.name for p in workspace.rglob("*")])
            verdict = json.loads((run_dir / "verifier.json").read_text(encoding="utf-8"))
            self.assertTrue(verdict["coding"]["hidden_test"]["passed"])
            self.assertTrue(verdict["coding"]["hidden_test"]["hidden_path"].startswith("hidden/"))

    def test_a_wrong_reference_repair_fails_hidden_tests(self) -> None:
        class WrongRepairProvider:
            name = "wrong-scripted"
            model = "offline-wrong-v1"

            def __init__(self) -> None:
                self.calls = 0

            def propose(self, context):  # noqa: ANN001
                self.calls += 1
                if self.calls == 1:
                    # A plausible-looking but non-repairing edit.
                    return ScriptedProvider({
                        "model_input": {"actions": [{
                            "id": "bad-edit", "tool": "edit_file",
                            "arguments": {
                                "path": "sum_to_n.py",
                                "old_text": "return sum(range(1, n))",
                                "new_text": "return sum(range(1, n)) + 1",
                            },
                        }]},
                    }).propose(context)
                return ScriptedProvider({
                    "model_input": {"actions": [{
                        "id": "deliver-report", "tool": "write_file",
                        "arguments": {"path": "REPORT.md", "content": "done"},
                    }]},
                }).propose(context)

        with tempfile.TemporaryDirectory() as tmp:
            result = run_coding_task(
                get_coding_task("single-file-off-by-one"),
                Path(tmp) / "out", provider=WrongRepairProvider(),
            )
            # The loop may stop honestly for an unfulfilled plan step; what
            # matters is that the wrong repair cannot pass hidden tests.
            self.assertIn(result["runtime_status"], ("succeeded", "failed"))
            self.assertFalse(result["hidden_tests_passed"])
            self.assertFalse(result["verifier_passed"])
            self.assertFalse(result["goal_success"])
            self.assertTrue(result["badcase_reasons"])


if __name__ == "__main__":
    unittest.main()

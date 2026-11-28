"""General-agent evaluation: versioned tasks, independent verifier, fail-closed online gate."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.hashing import digest  # noqa: E402
from flowagent.evaluation.general_eval import (  # noqa: E402
    OfflineNetworkGuard,
    check_online_gate,
    redact_text,
    run_general_suite,
)
from flowagent.evaluation.general_tasks import (  # noqa: E402
    TASKSET_VERSION,
    general_categories,
    general_taskset,
    general_taskset_sha256,
    get_general_task,
)
from flowagent.evaluation.metrics import compute_general_metrics, percentile  # noqa: E402

REQUIRED_CATEGORIES = {
    "coding", "file_delivery", "research", "multi_step",
    "recovery", "adversarial", "negative",
}


class TaskSetTests(unittest.TestCase):
    def test_taskset_is_versioned_and_large_enough(self) -> None:
        tasks = general_taskset()
        self.assertGreaterEqual(len(tasks), 24)
        self.assertEqual(TASKSET_VERSION, "general-agent-benchmark/v1")
        ids = [task["task_id"] for task in tasks]
        self.assertEqual(len(ids), len(set(ids)), "task ids must be unique")

    def test_required_categories_are_covered(self) -> None:
        self.assertTrue(REQUIRED_CATEGORIES.issubset(set(general_categories())))

    def test_every_task_declares_the_full_contract(self) -> None:
        for task in general_taskset():
            with self.subTest(task=task["task_id"]):
                self.assertEqual(task["version"], TASKSET_VERSION)
                self.assertTrue(task["rationale"].strip())
                self.assertTrue(task["user_task"].strip())
                self.assertTrue(task["model_input"]["actions"], "offline reference actions required")
                self.assertIn("independent_verifier", task)
                self.assertIn("forbidden_side_effects", task)
                self.assertIn("OFFLINE_SCRIPTED", task["applicability"])
                self.assertIn("ONLINE_MODEL", task["applicability"])
                self.assertTrue(task["allowed_tools"])
                self.assertTrue(task["budget"]["max_calls"] > 0)
                self.assertTrue(task["timeout_seconds"] > 0)
                self.assertEqual(digest(task), digest(json.loads(json.dumps(task))),
                                 "definition digest must be stable")

    def test_taskset_sha256_is_stable_and_task_lookup_works(self) -> None:
        first = general_taskset_sha256()
        self.assertEqual(first, general_taskset_sha256())
        task = get_general_task("coding-fix-off-by-one")
        self.assertEqual(task["category"], "coding")
        with self.assertRaises(KeyError):
            get_general_task("no-such-task")


class MetricsTests(unittest.TestCase):
    def test_empty_denominators_are_not_measured(self) -> None:
        metrics = compute_general_metrics([])
        self.assertEqual(metrics["goal_success_rate"]["status"], "NOT_MEASURED")
        self.assertIsNone(metrics["goal_success_rate"]["value"])
        self.assertEqual(metrics["recovery_success_rate"]["status"], "NOT_MEASURED")
        self.assertEqual(metrics["latency_ms"]["status"], "NOT_MEASURED")

    def test_percentile_is_nearest_rank_on_integers(self) -> None:
        self.assertEqual(percentile([10, 20, 30, 40], 0.5), 20)
        self.assertEqual(percentile([10, 20, 30, 40], 0.95), 40)
        self.assertEqual(percentile([], 0.5), 0)

    def test_rates_split_by_denominator(self) -> None:
        rows = [
            {"task_id": "a", "category": "coding", "provider": "offline-scripted", "model": "m",
             "goal_success": True, "verifier_passed": True, "case_expectation_met": True,
             "pipeline_completed": True, "invalid_action_count": 0,
             "unauthorized_side_effect_count": 0, "duplicate_side_effect_count": 0,
             "human_intervention_count": 0, "recovery_attempted": False, "recovered": False,
             "runtime_status": "succeeded", "wall_ms": 100},
            {"task_id": "b", "category": "recovery", "provider": "offline-scripted", "model": "m",
             "goal_success": False, "verifier_passed": False, "case_expectation_met": False,
             "pipeline_completed": True, "invalid_action_count": 1,
             "unauthorized_side_effect_count": 0, "duplicate_side_effect_count": 0,
             "human_intervention_count": 0, "recovery_attempted": True, "recovered": True,
             "runtime_status": "failed", "failure_code": "TOOL_OUTCOME_UNKNOWN", "wall_ms": 300},
        ]
        metrics = compute_general_metrics(rows)
        self.assertEqual(metrics["goal_success_rate"]["value"], 0.5)
        self.assertEqual(metrics["recovery_success_rate"]["numerator"], 1)
        self.assertEqual(metrics["recovery_success_rate"]["denominator"], 1)
        self.assertEqual(metrics["invalid_action_rate"]["numerator"], 1)
        self.assertEqual(metrics["latency_ms"]["p50"], 100)
        self.assertEqual(metrics["latency_ms"]["p95"], 300)
        self.assertEqual(metrics["badcases"][0]["task_id"], "b")


class RedactionTests(unittest.TestCase):
    def test_keys_never_reach_a_report(self) -> None:
        text = "failed to call https://api.deepseek.com with key sk-abc123def456 and API_KEY=plainsecret"
        redacted = redact_text(text, ["plainsecret"])
        self.assertNotIn("sk-abc123def456", redacted)
        self.assertNotIn("plainsecret", redacted)
        self.assertIn("[REDACTED]", redacted)


class OnlineGateTests(unittest.TestCase):
    def test_missing_confirmation_fails_closed(self) -> None:
        verdict = check_online_gate(provider_name="deepseek", model="m", env={}, confirm_paid="yes")
        self.assertFalse(verdict["ok"])
        self.assertIn("confirm-paid", verdict["reason"])

    def test_mock_provider_is_never_an_online_model(self) -> None:
        verdict = check_online_gate(
            provider_name="mock", model=None, env={}, confirm_paid="authorize paid online evaluation")
        self.assertFalse(verdict["ok"])

    def test_missing_key_model_or_price_fails_closed(self) -> None:
        base = "authorize paid online evaluation"
        verdict = check_online_gate(provider_name="deepseek", model=None, env={}, confirm_paid=base)
        self.assertFalse(verdict["ok"])
        self.assertIn("DEEPSEEK_API_KEY", verdict["reason"])
        verdict = check_online_gate(
            provider_name="deepseek", model="m",
            env={"DEEPSEEK_API_KEY": "k", "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK": "1"},
            confirm_paid=base,
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("output_microusd_per_mtok", verdict["reason"])

    def test_local_provider_needs_no_price_snapshot(self) -> None:
        verdict = check_online_gate(
            provider_name="local", model=None,
            env={"FCODE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1"},
            confirm_paid="authorize paid online evaluation",
        )
        self.assertTrue(verdict["ok"])

    def test_complete_configuration_passes(self) -> None:
        verdict = check_online_gate(
            provider_name="deepseek", model="deepseek-chat",
            env={
                "DEEPSEEK_API_KEY": "k",
                "DEEPSEEK_MODEL": "deepseek-chat",
                "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK": "1000",
                "DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK": "2000",
                "DEEPSEEK_PRICE_AS_OF": "2026-11-22",
            },
            confirm_paid="I authorize paid online evaluation for this benchmark run",
        )
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["price_snapshot"]["as_of"], "2026-11-22")


class OfflineSuiteTests(unittest.TestCase):
    """The offline suite validates the evaluator itself on the full task set."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="fcode-general-eval-")
        cls.result = run_general_suite(pathlib.Path(cls._tmp.name) / "out")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_task_reaches_its_authored_expectation(self) -> None:
        self.assertTrue(self.result["all_case_expectations_met"],
                        [row["badcase_reasons"] for row in self.result["results"]
                         if not row["case_expectation_met"]])
        self.assertEqual(len(self.result["results"]), len(general_taskset()))

    def test_summary_files_and_manifest_are_written(self) -> None:
        root = pathlib.Path(self.result["output"])
        for name in ("summary.json", "summary.md", "run_manifest.json", "taskset.json"):
            self.assertTrue((root / name).is_file(), name)
        manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["taskset_version"], TASKSET_VERSION)
        self.assertEqual(manifest["taskset_sha256"], general_taskset_sha256())
        self.assertEqual(manifest["online_model"]["status"], "NOT_RUN")
        self.assertTrue(manifest["code"]["commit"])
        summary_md = (root / "summary.md").read_text(encoding="utf-8")
        self.assertIn("ONLINE_MODEL=NOT_RUN", summary_md)
        self.assertIn("NOT_MEASURED", summary_md)

    def test_recovery_tasks_reconcile_without_duplicates(self) -> None:
        rows = {row["task_id"]: row for row in self.result["results"]}
        for task_id in ("recovery-unknown-write-once", "recovery-crash-after-side-effect",
                        "recovery-crash-resume"):
            row = rows[task_id]
            self.assertTrue(row["recovered"], task_id)
            self.assertEqual(row["duplicate_side_effect_count"], 0, task_id)
            self.assertEqual(row["unauthorized_side_effect_count"], 0, task_id)

    def test_negative_tasks_stop_honestly(self) -> None:
        rows = {row["task_id"]: row for row in self.result["results"]}
        for task_id in ("negative-missing-input", "negative-fabrication-guard",
                        "negative-denied-tool"):
            row = rows[task_id]
            self.assertEqual(row["runtime_status"], "failed", task_id)
            self.assertEqual(row["failure_code"], "INSUFFICIENT_EVIDENCE", task_id)
            self.assertTrue(row["case_expectation_met"], task_id)

    def test_injection_tasks_produce_no_injected_artifact(self) -> None:
        root = pathlib.Path(self.result["output"])
        for task_id in ("injection-readme-pwned", "injection-secret-exfil",
                        "injection-tool-result"):
            workspace = root / "tasks" / task_id / "run-0" / "workspace"
            self.assertFalse((workspace / "PWNED.md").exists(), task_id)
            self.assertFalse((workspace / "SECRET.txt").exists(), task_id)

    def test_research_tasks_use_only_the_local_fixture(self) -> None:
        root = pathlib.Path(self.result["output"])
        data = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        rows = {row["task_id"]: row for row in data["results"]}
        for task_id in ("research-single-source", "research-two-source-conflict",
                        "research-quote-evidence"):
            self.assertTrue(rows[task_id]["goal_success"], task_id)
        provider = json.loads(
            (root / "tasks" / "research-single-source" / "run-0" / "provider.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(provider["type"], "OFFLINE_SCRIPTED")

    def test_metrics_report_every_required_rate(self) -> None:
        metrics = self.result["metrics"]
        for name in ("goal_success_rate", "independent_verifier_pass_rate",
                     "pipeline_completion_rate", "invalid_action_rate"):
            self.assertIn(name, metrics)
            self.assertEqual(metrics[name]["denominator"], len(self.result["results"]))
        recovery_rows = sum(1 for row in self.result["results"] if row["recovery_attempted"])
        self.assertGreater(recovery_rows, 0)
        self.assertEqual(metrics["recovery_success_rate"]["denominator"], recovery_rows,
                         "recovery rate keeps its own denominator")
        self.assertEqual(metrics["unauthorized_side_effect_count"]["count"], 0)
        self.assertEqual(metrics["duplicate_side_effect_count"]["count"], 0)
        self.assertGreater(metrics["latency_ms"]["p50"], 0)

    def test_repeat_keeps_every_raw_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fcode-general-eval-repeat-") as tmp:
            completed = run_general_suite(
                pathlib.Path(tmp) / "out",
                task_ids=["delivery-json-settings", "delivery-sum-report"],
                repeat=2,
            )
        self.assertEqual(len(completed["results"]), 4)
        repeats = sorted(row["repeat"] for row in completed["results"])
        self.assertEqual(repeats, [0, 0, 1, 1])
        self.assertTrue(all(row["case_expectation_met"] for row in completed["results"]))


class OfflineNetworkGuardTests(unittest.TestCase):
    def test_blocks_disallowed_and_allows_listed_loopback(self) -> None:
        import socket

        with OfflineNetworkGuard() as guard:
            with self.assertRaises(OSError):
                socket.create_connection(("example.com", 80), timeout=0.1)
            self.assertEqual(guard.attempts, 1)
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            with OfflineNetworkGuard(allow=(("127.0.0.1", port),)):
                client = socket.create_connection(("127.0.0.1", port), timeout=2)
                client.close()
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()

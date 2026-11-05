"""End-to-end evidence for the versioned production-Runtime task benchmark."""

from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.benchmark.runner import build_stack, run_benchmark  # noqa: E402
from flowagent.benchmark.tasks import get_task, taskset, taskset_sha256  # noqa: E402
from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.runtime.recovery import find_unfinished_tool, reconcile_unknown_tool  # noqa: E402


class RuntimeBenchmarkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / "work").mkdir(exist_ok=True)
        cls.output = pathlib.Path(tempfile.mkdtemp(prefix="runtime-benchmark-test-", dir=ROOT / "work"))
        cls.completed = run_benchmark(cls.output)
        cls.results = {row["task_id"]: row for row in cls.completed["results"]}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.output)

    def artifact(self, task_id: str, name: str) -> dict:
        return json.loads((self.output / "tasks" / task_id / name).read_text(encoding="utf-8"))

    def test_versioned_task_set_has_required_contract(self) -> None:
        tasks = taskset()
        self.assertEqual(len(tasks), 19)
        self.assertEqual(len(taskset_sha256()), 64)
        required = {
            "task_id", "category", "version", "fixture", "user_task", "allowed_tools",
            "permissions", "budget", "approval", "expected_final_state",
            "forbidden_side_effects", "expected_governance_events", "independent_verifier",
            "rationale", "applicability",
        }
        for task in tasks:
            self.assertFalse(required - set(task), task["task_id"])
            self.assertIn("OFFLINE_SCRIPTED", task["applicability"])
            self.assertIn("ONLINE_MODEL", task["applicability"])

    def test_all_case_expectations_and_metric_denominators(self) -> None:
        self.assertTrue(self.completed["all_case_expectations_met"])
        metrics = self.completed["metrics"]
        self.assertEqual((metrics["pipeline_completion_rate"]["numerator"], metrics["pipeline_completion_rate"]["denominator"]), (19, 19))
        self.assertEqual((metrics["independent_verifier_pass_rate"]["numerator"], metrics["independent_verifier_pass_rate"]["denominator"]), (18, 19))
        self.assertEqual((metrics["policy_block_accuracy"]["numerator"], metrics["policy_block_accuracy"]["denominator"]), (4, 4))
        self.assertEqual((metrics["approval_gate_accuracy"]["numerator"], metrics["approval_gate_accuracy"]["denominator"]), (2, 2))
        self.assertEqual((metrics["budget_enforcement_accuracy"]["numerator"], metrics["budget_enforcement_accuracy"]["denominator"]), (3, 3))
        self.assertEqual((metrics["crash_recovery_rate"]["numerator"], metrics["crash_recovery_rate"]["denominator"]), (3, 3))
        self.assertEqual((metrics["unknown_outcome_safe_handling_rate"]["numerator"], metrics["unknown_outcome_safe_handling_rate"]["denominator"]), (3, 3))
        self.assertEqual(metrics["unauthorized_side_effect_count"]["count"], 0)
        self.assertEqual(metrics["duplicate_side_effect_count"]["count"], 0)

    def test_normal_chain_contains_host_gates_and_independent_verdict(self) -> None:
        events = self.artifact("single-file-modify", "events.json")
        types = [event["type"] for event in events]
        self.assertEqual(types[:5], ["task_created", "model_started", "plan_validated", "execution_started", "tool_started"])
        self.assertIn("tool_succeeded", types)
        self.assertIn("verification_started", types)
        self.assertEqual(types[-1], "task_succeeded")
        verdict = self.artifact("single-file-modify", "verifier.json")
        self.assertTrue(verdict["passed"])
        self.assertEqual(verdict["exit_code"], 0)
        receipts = self.artifact("single-file-modify", "receipts.json")
        self.assertTrue(any(item["result_hash"] for item in receipts))

    def test_policy_approval_and_budget_denials_have_no_side_effect(self) -> None:
        for task_id in (
            "invalid-schema-request", "unknown-tool-request", "unauthorized-workspace-path",
            "unauthorized-network-tool", "approval-denied-write", "budget-call-exhaustion",
            "budget-token-exhaustion", "budget-tool-exhaustion",
        ):
            with self.subTest(task_id=task_id):
                verdict = self.artifact(task_id, "verifier.json")
                self.assertTrue(verdict["passed"], verdict["failure_reasons"])
                self.assertEqual(verdict["forbidden_side_effects"]["unauthorized_count"], 0)
        denied = self.artifact("approval-denied-write", "events.json")
        self.assertNotIn("tool_started", [event["type"] for event in denied])
        outside = self.output / "tasks" / "unauthorized-workspace-path" / "canary" / "outside.txt"
        self.assertEqual(outside.read_text(encoding="utf-8"), "UNTOUCHED")

    def test_real_sigkill_unknown_outcome_and_no_duplicate(self) -> None:
        for task_id in ("crash-before-tool-dispatch", "crash-after-side-effect", "crash-state-replay-resume"):
            with self.subTest(task_id=task_id):
                process = self.artifact(task_id, "process.json")
                self.assertEqual(process["returncode"], -9)
                self.assertTrue(process["sigkill_confirmed"])
                self.assertTrue(self.artifact(task_id, "recovery.json")["recovered"])
                self.assertTrue(self.artifact(task_id, "replay.json")["equivalent"])
        unknown = self.artifact("crash-after-side-effect", "recovery.json")
        self.assertEqual(unknown["side_effect"], "observed")
        self.assertFalse(unknown["retry_dispatched"])
        self.assertEqual(self.artifact("crash-after-side-effect", "tool-executions.json")["write_file"], 1)
        replayed = self.artifact("crash-state-replay-resume", "tool-executions.json")
        self.assertEqual(replayed["write_file"], 2)

    def test_budget_survives_recovery(self) -> None:
        budget = self.artifact("crash-after-side-effect", "budget.json")
        self.assertEqual(budget["calls_used"], 1)
        self.assertEqual(budget["reads_used"], 1)
        self.assertEqual(budget["tokens_used"], 1000)
        self.assertEqual(budget["limits"], get_task("crash-after-side-effect")["budget"])

    def test_tamper_detection_identifies_event(self) -> None:
        tamper = self.artifact("hash-chain-tamper-detection", "tamper.json")
        self.assertTrue(tamper["detected"])
        self.assertEqual(tamper["code"], "CHAIN_TAMPERED")
        self.assertEqual(tamper["sequence"], 2)

    def test_model_claim_does_not_override_independent_failure(self) -> None:
        verdict = self.artifact("model-success-verifier-reject", "verifier.json")
        self.assertEqual(self.results[verdict["task_id"]]["runtime_status"], "succeeded")
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["exit_code"], 1)
        self.assertTrue(verdict["case_expectation_met"])
        self.assertTrue(any(check["name"] == "model_claimed_done" and check["actual"] for check in verdict["checks"]))

    def test_recovery_refuses_missing_duplicate_and_out_of_order_evidence(self) -> None:
        normal_dir = self.output / "tasks" / "single-file-modify"
        normal = build_stack(get_task("single-file-modify"), normal_dir)
        with self.assertRaises(AgentError) as missing:
            reconcile_unknown_tool(normal.harness, "single-file-modify", evidence={"side_effect": "not_observed"})
        self.assertEqual(missing.exception.code, "RECOVERY_NOT_REQUIRED")
        normal.store.close()

        unknown_dir = self.output / "tasks" / "unknown-outcome-no-retry"
        unknown = build_stack(get_task("unknown-outcome-no-retry"), unknown_dir)
        with self.assertRaises(AgentError) as duplicate:
            reconcile_unknown_tool(unknown.harness, "unknown-outcome-no-retry", evidence={"side_effect": "observed"})
        self.assertEqual(duplicate.exception.code, "RECOVERY_ALREADY_RESOLVED")
        unknown.store.close()

        with self.assertRaises(AgentError) as ordering:
            find_unfinished_tool([{
                "sequence": 1,
                "type": "tool_succeeded",
                "payload": {"detail": {"tool": "write_file", "round": 1}},
            }])
        self.assertEqual(ordering.exception.code, "EVENT_ORDER_INVALID")

    def test_required_artifacts_and_online_boundary(self) -> None:
        required = {
            "task.json", "events.json", "receipts.json", "workspace.diff", "verifier.json",
            "stdout.txt", "stderr.txt", "provider.json", "policy.json", "budget.json",
        }
        for task_id in self.results:
            with self.subTest(task_id=task_id):
                present = {path.name for path in (self.output / "tasks" / task_id).iterdir()}
                self.assertFalse(required - present)
        manifest = json.loads((self.output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["execution_class"], "OFFLINE_SCRIPTED")
        self.assertEqual(manifest["online_model"]["status"], "NOT_RUN")
        self.assertEqual(manifest["online_model"]["metrics"], "NOT_MEASURED")
        self.assertEqual(manifest["taskset_sha256"], taskset_sha256())


if __name__ == "__main__":
    unittest.main()

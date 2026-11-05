"""Multi-agent lab tests: deterministic roles, child chains, manifest."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from labs.multi_agent.fixtures import get_task  # noqa: E402
from labs.multi_agent.orchestrator import Orchestrator, SimulatedCrash  # noqa: E402
from labs.multi_agent.roles import RuleReviewer  # noqa: E402
from labs.multi_agent.state import ManifestStore, MultiAgentState  # noqa: E402


def _state(result) -> MultiAgentState:
    return MultiAgentState.from_dict(
        json.loads((Path(result.run_dir) / "state.json").read_text(encoding="utf-8"))
    )


def _trace(child: dict) -> dict:
    return json.loads(Path(child["trace_path"]).read_text(encoding="utf-8"))


class MultiAgentLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fcode-ma-")
        self.root = Path(self._tmp.name) / ".runs"
        self.orch = Orchestrator(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_t1_normal_path(self) -> None:
        result = self.orch.run(get_task("full"), mode="multi")
        state = _state(result)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.outcome, "succeeded")
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(result.child_runs), 1)
        self.assertEqual(result.child_runs[0]["runtime_status"], "succeeded")
        self.assertEqual(len(state.handoffs), 2)  # planner->executor, executor->reviewer
        self.assertEqual(len(state.verdicts), 1)
        self.assertEqual(state.verdicts[0]["kind"], "accept")
        self.assertEqual(result.replans, 0)
        self.assertFalse(result.rejects)
        self.assertTrue(result.verified)
        verify_trace(_trace(result.child_runs[0]))
        self.assertTrue(ManifestStore(Path(result.manifest_path)).verify()["verified"])

    def test_t2_tool_failure_replans(self) -> None:
        result = self.orch.run(get_task("tool-denied-recoverable"), mode="multi")
        state = _state(result)
        self.assertEqual(result.status, "accepted")
        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 2)
        self.assertEqual([child["runtime_status"] for child in result.child_runs], ["denied", "succeeded"])
        self.assertEqual(result.replans, 1)
        self.assertEqual(result.duplicate_count, 0)
        self.assertEqual(state.verdicts[0]["kind"], "replan")
        self.assertEqual(state.verdicts[0]["required_changes"], ["drop:t0"])
        self.assertEqual(state.verdicts[1]["kind"], "accept")
        for child in result.child_runs:
            verify_trace(_trace(child))

    def test_t3_budget_stops_before_dispatch(self) -> None:
        result = self.orch.run(get_task("budget"), mode="multi")
        child = result.child_runs[0]
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(child["runtime_status"], "failed")
        self.assertEqual(child["calls"], 4)
        self.assertEqual(child["reads"], 4)
        self.assertEqual(_trace(child)["events"][-1]["type"], "task_failed")
        model_starts = sum(
            1 for event in _trace(child)["events"] if event["type"] == "model_started"
        )
        tool_starts = sum(
            1 for event in _trace(child)["events"] if event["type"] == "tool_started"
        )
        # FCode writes model_started before charge_call, so the rejected fifth
        # attempt is visible as a start event even though the provider was never
        # called. The charged call count and executed tool count stay at four.
        self.assertEqual(model_starts, 5)
        self.assertEqual(tool_starts, 4)
        self.assertEqual(result.attempts, 1)
        self.assertTrue(result.success)

    def test_t4_reviewer_rejects_twice(self) -> None:
        result = self.orch.run(get_task("toy-reviewer-reject"), mode="multi")
        state = _state(result)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.child_runs), 2)
        self.assertEqual(result.rejects, 2)
        self.assertEqual([child["runtime_status"] for child in result.child_runs], ["succeeded", "succeeded"])
        self.assertEqual([child["review_status"] for child in result.child_runs], ["reject", "reject"])
        self.assertEqual([item["kind"] for item in state.verdicts], ["reject", "reject"])
        self.assertEqual(len(state.verdicts), 2)
        for child in result.child_runs:
            verify_trace(_trace(child))

    def test_t5_no_progress_guard(self) -> None:
        result = self.orch.run(get_task("toy-replan-forever"), mode="multi")
        state = _state(result)
        self.assertEqual(result.status, "no_progress")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(result.child_runs), 2)
        self.assertEqual([item["kind"] for item in state.verdicts], ["replan", "replan"])
        self.assertEqual(result.outcome, "NO_PROGRESS")

    def test_t6_handoff_cannot_expand_scope(self) -> None:
        result = self.orch.run(get_task("toy-out-of-scope"), mode="multi")
        self.assertEqual(result.status, "invalid_handoff")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(result.child_runs), 1)
        state = _state(result)
        self.assertEqual(len(state.handoffs), 2)  # child1 plan + result; no child2
        self.assertEqual(state.verdicts[0]["kind"], "replan")
        # The rejected revision never produced a child2 trace.
        self.assertEqual(len(result.child_runs), 1)

    def test_t7_manifest_resume_does_not_repeat_child(self) -> None:
        task = get_task("tool-denied-recoverable")
        with self.assertRaises(SimulatedCrash):
            self.orch.run(task, mode="multi", run_id="resume-case", crash_after_attempt=1)
        before = MultiAgentState.from_dict(
            json.loads((self.root / "resume-case" / "state.json").read_text(encoding="utf-8"))
        )
        child1 = before.child_runs[0]
        store = TaskStore(Path(child1.task_db))
        events_before = len(store.events(child1.task_id))
        store.close()
        self.assertEqual(events_before, child1.event_count)
        manifest_before = ManifestStore(self.root / "resume-case" / "manifest.jsonl")
        self.assertTrue(manifest_before.verify()["verified"])

        result = Orchestrator(self.root).resume("resume-case")
        after = _state(result)
        self.assertEqual(result.status, "accepted")
        self.assertEqual([child["runtime_status"] for child in result.child_runs], ["denied", "succeeded"])
        store = TaskStore(Path(child1.task_db))
        events_after = len(store.events(child1.task_id))
        store.close()
        self.assertEqual(events_after, events_before)
        self.assertEqual(after.child_runs[0].event_count, child1.event_count)
        self.assertTrue(ManifestStore(Path(result.manifest_path)).verify()["verified"])

    def test_t8_reviewer_has_no_tools(self) -> None:
        result = self.orch.run(get_task("full"), mode="multi")
        reviewer = RuleReviewer()
        self.assertFalse(hasattr(reviewer, "registry"))
        self.assertFalse(hasattr(reviewer, "harness"))
        self.assertFalse(hasattr(reviewer, "tool_registry"))
        manifest = ManifestStore(Path(result.manifest_path))
        verdicts = [event for event in manifest.events if event["type"] == "agent_verdict"]
        self.assertEqual(len(verdicts), 1)
        payload = verdicts[0]["payload"]
        self.assertNotIn("tool_started", payload)
        self.assertNotIn("tool_succeeded", payload)
        self.assertNotIn("tool", payload)
        child_events = _trace(result.child_runs[0])["events"]
        self.assertTrue(any(event["type"] == "tool_succeeded" for event in child_events))


if __name__ == "__main__":
    unittest.main()

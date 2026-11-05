"""A human review is a signed event, not a rewrite of the run.

The review decision must land in the same hash chain as the evidence it judges:
the task stays terminal, the new event links to the previous head, and the
rebuilt trace still passes independent verification. It must also be impossible
to review a run that has not finished.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import Task  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation  # noqa: E402
from flowagent.memory.cases import project_case  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.review import record_review  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.trajectory.exporter import verify_trace  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402


def run_full(tmp: pathlib.Path) -> tuple[TaskStore, Task, dict]:
    """Run the full fixture to a succeeded terminal trace, offline."""
    scenario = get_scenario("full")
    store = TaskStore(tmp / "task.sqlite")
    world = FinanceWorld(partial=scenario.partial_corpus)
    registry = ToolRegistry(build_finance_tools(world))
    receipts = ReceiptStore(store)
    provider = MockProvider(policy=scenario.policy)
    recorder = TraceRecorder(store, provider_name=provider.name, model_name=provider.model)
    harness = Harness(
        store=store,
        registry=registry,
        provider=provider,
        world=world,
        limits=scenario.limits,
        receipts=receipts,
        verifier=lambda observation: verify_observation(world, observation),
    )
    task = harness.create(
        task_id="task-review",
        objective=scenario.objective,
        principal="test",
        binding={"module": "finance-research", "scenario": "full"},
        allowed_tools=list(scenario.allowed_tools),
        grant=dict(scenario.grant),
    )
    Loop(
        harness,
        planner=FinancePlanner(),
        max_rounds=scenario.max_rounds,
        observer=recorder,
        result_tools=FINANCE_DOMAIN.result_tools,
    ).run(task.id)
    task = store.load(task.id)
    return store, task, recorder.build(task)


class ReviewEventTests(unittest.TestCase):
    def test_review_appends_to_the_chain_without_changing_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, task, trace = run_full(pathlib.Path(tmp))
            try:
                self.assertEqual(task.status, "succeeded")
                self.assertTrue(trace["rounds"])
                before = project_case(trace, run_id="run-review")
                previous_head = trace["chain"]["head_hash"]

                final, event = record_review(
                    store,
                    task.id,
                    trace,
                    decision="accept",
                    reason="证据充分，可以签字",
                    reviewer="林岚",
                )

                self.assertEqual(event["type"], "review_decided")
                self.assertEqual(event["sequence"], task.sequence + 1)
                self.assertEqual(event["prev_hash"], previous_head)
                self.assertEqual(
                    event["payload"]["detail"],
                    {"decision": "accept", "reason": "证据充分，可以签字", "reviewer": "林岚"},
                )

                # The chain still verifies, and the review is its new head.
                self.assertTrue(verify_trace(final)["verified"])
                self.assertEqual(final["chain"]["head_hash"], event["hash"])
                self.assertEqual(final["events"][-1], event)

                # The decision is an annotation on a terminal run, not a new outcome.
                self.assertEqual(final["task"]["status"], "succeeded")
                self.assertEqual(final["terminal"]["status"], "succeeded")
                self.assertEqual(final["terminal"]["result"]["change_bp"], -250)
                self.assertEqual(len(final["rounds"]), len(trace["rounds"]))
                self.assertEqual(final["model"]["provider"], trace["model"]["provider"])
                self.assertEqual(final["model"]["model"], trace["model"]["model"])

                after = project_case(final, run_id="run-review")
                self.assertEqual(after["human_decisions"], before["human_decisions"] + 1)
                self.assertEqual(after["outcome"], "succeeded")

                reloaded = store.load(task.id)
                self.assertEqual(reloaded.status, "succeeded")
                self.assertEqual(reloaded.sequence, task.sequence + 1)
            finally:
                store.close()

    def test_review_rejects_non_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "task.sqlite")
            try:
                task = store.create(Task(id="task-open", objective="still running", principal="test"))
                recorder = TraceRecorder(
                    store, provider_name="offline-mock", model_name="offline-deterministic-mock-v1"
                )
                trace = recorder.build(store.load(task.id))
                sequence = task.sequence

                with self.assertRaises(ValueError):
                    record_review(store, task.id, trace, decision="accept", reason="", reviewer="")

                self.assertEqual(store.load(task.id).sequence, sequence)
                self.assertEqual([event["type"] for event in store.events(task.id)], ["task_created"])
            finally:
                store.close()

    def test_review_validates_decision_and_lengths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, task, trace = run_full(pathlib.Path(tmp))
            try:
                invalid = (
                    {"decision": "approved", "reason": "", "reviewer": ""},
                    {"decision": "accept", "reason": "x" * 1001, "reviewer": ""},
                    {"decision": "accept", "reason": "", "reviewer": "x" * 81},
                )
                for kwargs in invalid:
                    with self.subTest(decision=kwargs["decision"], reason_len=len(kwargs["reason"])):
                        with self.assertRaises(ValueError):
                            record_review(store, task.id, trace, **kwargs)
                self.assertEqual(store.load(task.id).sequence, task.sequence)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

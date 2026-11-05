"""Plan tests: the agent's intent must be durable, checkable and revisable.

A plan that cannot be inspected is just a comment. These tests pin the three
properties that make it real: every todo names the evidence that settles it, a
blocked todo stops the run instead of being quietly skipped, and the plan
recovered from the log equals the plan built step by step.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError, Task  # noqa: E402
from flowagent.core.plan import Plan, Todo  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import BLUEPRINT, FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation as verify_finance


def observation(tool: str, result: dict, arguments: dict | None = None, *, digest: str = "h") -> Observation:
    return Observation(tool=tool, arguments=arguments or {}, result=result, result_hash=digest)


class DecomposeTest(unittest.TestCase):
    def test_blueprint_becomes_a_plan(self) -> None:
        plan = FinancePlanner().decompose("compare two periods")
        self.assertEqual(plan.revision, 1)
        self.assertEqual(len(plan.todos), len(BLUEPRINT))
        self.assertTrue(all(todo.status == "pending" for todo in plan.todos))
        self.assertEqual(plan.next_open().id, "t1")

    def test_plan_copies_do_not_alias_the_blueprint(self) -> None:
        planner = FinancePlanner()
        first = planner.decompose("a")
        second = planner.decompose("b")
        first.mark("t1", "done", evidence=("x",))
        self.assertEqual(second.todos[0].status, "pending")
        self.assertEqual(BLUEPRINT[0].status, "pending")


class AdvanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = FinancePlanner()
        self.plan = self.planner.decompose("compare")

    def test_a_matching_observation_settles_its_todo_with_evidence(self) -> None:
        plan = self.planner.advance(self.plan, observation("list_documents", {"documents": [{"id": "d"}]}, digest="abc123"))
        self.assertEqual(plan.todos[0].status, "done")
        self.assertEqual(plan.todos[0].evidence, ("abc123",))

    def test_an_off_plan_observation_changes_nothing(self) -> None:
        # The bridge document is readable but is not a todo: the plan must not
        # absorb an action it never asked for.
        plan = self.planner.advance(
            self.plan,
            observation("read_document", {"id": "doc_fy2025_bridge"}, {"doc_id": "doc_fy2025_bridge"}),
        )
        self.assertTrue(all(todo.status == "pending" for todo in plan.todos))

    def test_a_refusal_blocks_the_todo_instead_of_skipping_it(self) -> None:
        plan = self.plan
        for tool, args in (
            ("list_documents", {}),
            ("read_document", {"doc_id": "doc_fy2024"}),
            ("read_document", {"doc_id": "doc_fy2025"}),
            ("check_restatement", {"doc_id": "doc_fy2024"}),
            ("read_document", {"doc_id": "doc_fy2025_adjusted"}),
            ("reconcile_basis", {"doc_id": "doc_fy2025_adjusted"}),
            ("normalize_units", {"doc_id": "doc_fy2024_restated"}),
            ("normalize_units", {"doc_id": "doc_fy2025"}),
        ):
            plan = self.planner.advance(plan, observation(tool, {"id": args.get("doc_id"), "documents": [{"id": "d"}], "period": "FY2025"}, args))

        blocked = self.planner.advance(plan, observation("calculate_margin", {"computed": False, "blockers": ["missing period"]}))
        self.assertEqual(blocked.todos[-1].status, "blocked")
        self.assertIn("missing period", blocked.todos[-1].reason)
        self.assertTrue(blocked.is_blocked())
        self.assertFalse(blocked.is_complete())

    def test_completing_every_todo_completes_the_plan(self) -> None:
        plan = self.plan
        for tool, args in (
            ("list_documents", {}),
            ("read_document", {"doc_id": "doc_fy2024"}),
            ("read_document", {"doc_id": "doc_fy2025"}),
            ("check_restatement", {"doc_id": "doc_fy2024"}),
            ("read_document", {"doc_id": "doc_fy2025_adjusted"}),
            ("reconcile_basis", {"doc_id": "doc_fy2025_adjusted"}),
            ("normalize_units", {"doc_id": "doc_fy2024_restated"}),
            ("normalize_units", {"doc_id": "doc_fy2025"}),
        ):
            plan = self.planner.advance(plan, observation(tool, {"id": args.get("doc_id"), "documents": [{"id": "d"}], "period": "FY2025"}, args))
        plan = self.planner.advance(plan, observation("calculate_margin", {"computed": True, "change_bp": -250}))
        self.assertTrue(plan.is_complete())
        self.assertFalse(plan.is_blocked())


class RevisionTest(unittest.TestCase):
    def test_a_revision_increments_and_abandons(self) -> None:
        plan = FinancePlanner().decompose("g")
        revised = plan.revise(drop=("t9",), reason="cannot compute")
        self.assertEqual(revised.revision, 2)
        self.assertEqual(revised.todos[-1].status, "abandoned")
        self.assertEqual(revised.todos[-1].reason, "cannot compute")

    def test_a_done_todo_cannot_be_reopened(self) -> None:
        plan = FinancePlanner().decompose("g").mark("t1", "done")
        with self.assertRaises(AgentError) as caught:
            plan.mark("t1", "pending")
        self.assertEqual(caught.exception.code, "TODO_REGRESSION")

    def test_an_unknown_todo_is_refused(self) -> None:
        plan = FinancePlanner().decompose("g")
        with self.assertRaises(AgentError) as caught:
            plan.mark("t99", "done")
        self.assertEqual(caught.exception.code, "TODO_UNKNOWN")


class RequiredOptionalTest(unittest.TestCase):
    def test_todo_required_round_trips_and_legacy_plans_default_true(self) -> None:
        optional = Todo(
            id="o1",
            text="optional",
            tool="read_document",
            done_when="read",
            required=False,
        )
        restored = Todo.from_dict(optional.to_dict())
        self.assertFalse(restored.required)
        self.assertFalse(optional.to_dict()["required"])

        legacy = optional.to_dict()
        del legacy["required"]
        self.assertTrue(Todo.from_dict(legacy).required)

        plan = Plan(goal="g", todos=[optional])
        self.assertFalse(Plan.from_dict(plan.to_dict()).todos[0].required)

    def test_optional_blocked_completes_but_required_blocked_does_not(self) -> None:
        required_done = Todo(
            id="r1", text="required", tool="t", done_when="d", status="done"
        )
        optional_blocked = Todo(
            id="o1",
            text="optional",
            tool="t",
            done_when="d",
            status="blocked",
            required=False,
            reason="not disclosed",
        )
        plan = Plan(goal="g", todos=[required_done, optional_blocked])
        self.assertTrue(plan.is_complete())
        self.assertFalse(plan.is_blocked())

        required_blocked = Todo(
            id="r2",
            text="required",
            tool="t",
            done_when="d",
            status="blocked",
            reason="missing",
        )
        failed = Plan(goal="g", todos=[required_done, required_blocked])
        self.assertFalse(failed.is_complete())
        self.assertTrue(failed.is_blocked())


class ReplayTest(unittest.TestCase):
    """The plan rebuilt from the log must equal the plan built incrementally."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.store = TaskStore(self.tmp / "task.sqlite")
        world = FinanceWorld()
        registry = ToolRegistry(build_finance_tools(world))
        receipts = ReceiptStore(self.store)
        self.harness = Harness(
            store=self.store,
            registry=registry,
            provider=MockProvider(),
            world=world,
            verifier=lambda observation: verify_finance(world, observation),
            limits=BudgetLimits(12, 20, 20_000, 20_000),
            receipts=receipts,
        )
        self.planner = FinancePlanner()

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rebuild_from_log_matches_incremental_progress(self) -> None:
        task = self.harness.create(
            task_id="task-replay",
            objective="compare",
            principal="p",
            binding={},
            allowed_tools=list(self.harness.registry.names),
            grant={"allowed_tools": list(self.harness.registry.names)},
        )
        loop = Loop(
            self.harness,
            planner=self.planner,
            max_rounds=12,
            result_tools=FINANCE_DOMAIN.result_tools,
        )
        done = loop.run(task.id)
        self.assertEqual(done.status, "succeeded")
        # One result observation keeps the historical single-result shape.
        self.assertNotIn("results", done.result)
        self.assertEqual(done.result["verification"], "runtime-recomputed")

        stored = Plan.from_dict(done.plan_state)
        memory = loop.restore(done)
        replayed = loop.rebuild_plan(done, memory)
        self.assertEqual(
            [todo.status for todo in stored.todos],
            [todo.status for todo in replayed.todos],
            "the stored snapshot and the replayed plan must agree",
        )
        self.assertTrue(replayed.is_complete())
        self.assertEqual(len(replayed.todos), 9)

    def test_the_plan_is_written_into_the_event_chain(self) -> None:
        task = self.harness.create(
            task_id="task-events",
            objective="compare",
            principal="p",
            binding={},
            allowed_tools=list(self.harness.registry.names),
            grant={"allowed_tools": list(self.harness.registry.names)},
        )
        Loop(
            self.harness,
            planner=self.planner,
            max_rounds=12,
            result_tools=FINANCE_DOMAIN.result_tools,
        ).run(task.id)
        events = self.store.events(task.id)
        updates = [event for event in events if event["type"] == "plan_updated"]
        self.assertGreaterEqual(len(updates), 9)
        self.assertIn("todos", updates[0]["payload"]["detail"]["plan"])


if __name__ == "__main__":
    unittest.main()

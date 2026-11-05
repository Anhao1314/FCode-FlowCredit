"""Case memory: deterministic projections, explainable search, honest tombstones.

The memory must not hallucinate: a case is derived from a signed trace, so the
same trace always yields the same row and the same ranking. Deletion hides a
case from default search but never destroys the evidence reference.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation  # noqa: E402
from flowagent.memory.cases import CaseIndex, project_case  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.connectors import default_registry  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.tools.workspace import build_workspace_tools  # noqa: E402
from flowagent.trajectory.recorder import TraceRecorder  # noqa: E402


def run_trace(tmp: pathlib.Path) -> dict:
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
        task_id="task-case",
        objective=scenario.objective,
        principal="test",
        binding={"module": "finance-research", "scenario": "full", "corpus": "synthetic-authored-v1"},
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
    trace = recorder.build(store.load(task.id))
    store.close()
    return trace


def synthetic_trace(
    objective: str,
    *,
    outcome: str = "succeeded",
    result: dict | None = None,
    failure: dict | None = None,
    created: str = "2026-10-20T00:00:00+00:00",
    citations: list | None = None,
    human: bool = False,
) -> dict:
    result = dict(result or {})
    if citations:
        result["citations"] = citations
    events = [{"type": "task_created", "at": created, "payload": {"detail": {}}}]
    if human:
        events.append({"type": "operator_message", "at": created, "payload": {"detail": {"round": 1, "text": "x"}}})
    return {
        "task": {"objective": objective, "binding": {"module": "finance-research", "scenario": "full"}},
        "terminal": {"status": outcome, "result": result, "failure": failure or {}},
        "events": events,
        "rounds": [{"index": 1, "budget_after": {"cost_used_microusd": 4500}}],
        "chain": {"head_hash": "a" * 64},
    }


class CaseProjectionTests(unittest.TestCase):
    def test_projection_is_complete_and_cited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = run_trace(pathlib.Path(tmp))
        case = project_case(trace, run_id="run-1")
        self.assertEqual(case["module"], "finance-research")
        self.assertEqual(case["template"], "full")
        self.assertEqual(case["outcome"], "succeeded")
        self.assertEqual(case["rounds"], 9)
        self.assertEqual(case["cost_microusd"], 4500)
        self.assertEqual(case["chain_head"], trace["chain"]["head_hash"])
        self.assertIn("doc_fy2024_restated", case["key_citations"])
        self.assertIn("FY2024", case["tags"])
        self.assertIn("17.50%", case["summary"])
        self.assertTrue(case["created_at"])

    def test_rebuild_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = run_trace(pathlib.Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            first = CaseIndex(pathlib.Path(tmp) / "a.sqlite")
            first.upsert(trace, run_id="run-1")
            second = CaseIndex(pathlib.Path(tmp) / "b.sqlite")
            second.upsert(trace, run_id="run-1")
            rows_a = sqlite3.connect(pathlib.Path(tmp) / "a.sqlite").execute(
                "SELECT * FROM cases ORDER BY run_id").fetchall()
            rows_b = sqlite3.connect(pathlib.Path(tmp) / "b.sqlite").execute(
                "SELECT * FROM cases ORDER BY run_id").fetchall()
        self.assertEqual(rows_a, rows_b)


class CaseIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.index = CaseIndex(pathlib.Path(self._tmp.name) / "cases.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_upsert_is_idempotent(self) -> None:
        trace = synthetic_trace("判断恒星智算两期毛利率是否可比")
        self.index.upsert(trace, run_id="run-1")
        self.index.upsert(trace, run_id="run-1")
        self.assertEqual(self.index.count(), 1)
        self.assertEqual(self.index.get("run-1")["outcome"], "succeeded")

    def test_search_is_explainable_and_ranks_relevance(self) -> None:
        self.index.upsert(
            synthetic_trace(
                "判断恒星智算 2024 与 2025 两期合并毛利率是否可比",
                result={"change_bp": -250, "verification": "runtime-recomputed"},
            ),
            run_id="run-a",
        )
        self.index.upsert(
            synthetic_trace("开一家咖啡店的选址研究", outcome="failed", failure={"code": "PLAN_BLOCKED"},
                            created="2025-01-01T00:00:00+00:00"),
            run_id="run-b",
        )
        hits = self.index.search("恒星智算 毛利率", limit=2)
        self.assertEqual(hits[0]["run_id"], "run-a")
        self.assertGreater(hits[0]["breakdown"]["relevance_bp"], hits[1]["breakdown"]["relevance_bp"])
        self.assertEqual(
            set(hits[0]["breakdown"]), {"relevance_bp", "importance_bp", "recency_bp"},
        )
        self.assertIn("chain_head", hits[0])

    def test_related_cases_share_documents(self) -> None:
        self.index.upsert(synthetic_trace(
            "恒星智算 2024 毛利率", result={"citations": [{"doc_id": "doc_x"}]},
        ), run_id="run-a")
        self.index.upsert(synthetic_trace(
            "恒星智算 2025 毛利率", result={"citations": [{"doc_id": "doc_x"}, {"doc_id": "doc_y"}]},
        ), run_id="run-b")
        self.index.upsert(synthetic_trace(
            "另一家公司", result={"citations": [{"doc_id": "doc_z"}]},
        ), run_id="run-c")
        related = self.index.related("run-a")
        self.assertEqual(related[0]["run_id"], "run-b")
        self.assertIn("doc_x", related[0]["shared"])
        self.assertEqual([item["run_id"] for item in related], ["run-b"])

    def test_tombstone_hides_from_default_search_only(self) -> None:
        self.index.upsert(synthetic_trace("判断恒星智算两期毛利率"), run_id="run-a")
        self.assertTrue(self.index.tombstone("run-a"))
        self.assertEqual(len(self.index.search("恒星智算")), 0)
        included = self.index.search("恒星智算", include_archived=True)
        self.assertEqual(len(included), 1)
        self.assertTrue(included[0]["archived"])
        self.assertTrue(self.index.get("run-a")["archived"])

    def test_index_has_no_side_effects_until_used(self) -> None:
        path = pathlib.Path(self._tmp.name) / "lazy" / "cases.sqlite"
        CaseIndex(path)
        self.assertFalse(path.parent.exists())
        self.assertFalse(path.exists())


class CaseToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.index = CaseIndex(root / "cases.sqlite")
        self.index.upsert(synthetic_trace("判断恒星智算两期毛利率是否可比"), run_id="run-a")
        self.registry = ToolRegistry(
            build_workspace_tools(
                EvidenceStore(root),
                ArtifactStore(root / "artifacts"),
                default_registry(),
                self.index,
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_search_cases_tool_returns_hits(self) -> None:
        observation = self.registry.execute("search_cases", {"query": "恒星智算 毛利率"})
        self.assertEqual(observation.tool_version, "1.0")
        self.assertGreaterEqual(observation.result["count"], 1)
        self.assertEqual(observation.result["hits"][0]["run_id"], "run-a")

    def test_read_case_tool_returns_provenance(self) -> None:
        observation = self.registry.execute("read_case", {"run_id": "run-a"})
        self.assertEqual(observation.result["run_id"], "run-a")
        self.assertEqual(len(observation.result["chain_head"]), 64)
        self.assertIn("key_citations", observation.result)
        self.assertEqual(observation.result["related_cases"], [])

    def test_unknown_case_is_a_clean_tool_error(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.execute("read_case", {"run_id": "run-missing"})
        self.assertEqual(ctx.exception.code, "CASE_NOT_FOUND")


class MemoryRecallTests(unittest.TestCase):
    """The recall is frozen into the chain, so replay sees the same prompt."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        scenario = get_scenario("full")
        self.store = TaskStore(root / "task.sqlite")
        world = FinanceWorld(partial=scenario.partial_corpus)
        self.harness = Harness(
            store=self.store,
            registry=ToolRegistry(build_finance_tools(world)),
            provider=MockProvider(policy=scenario.policy),
            world=world,
            limits=scenario.limits,
            receipts=ReceiptStore(self.store),
            verifier=lambda observation: verify_observation(world, observation),
        )
        self.task = self.harness.create(
            task_id="task-recall",
            objective=scenario.objective,
            principal="test",
            binding={"module": "finance-research", "scenario": "full"},
            allowed_tools=list(scenario.allowed_tools),
            grant=dict(scenario.grant),
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    @staticmethod
    def _hit(index: int) -> dict:
        return {
            "run_id": f"run-{index}",
            "objective": f"旧案 {index}",
            "outcome": "succeeded",
            "summary": "两期毛利率可比。",
            "chain_head": str(index) * 64,
            "tags": ["finance-research"],
            "created_at": "2026-09-01T00:00:00+00:00",
            "archived": False,
            "score_bp": 9000 - index,
            "breakdown": {"relevance_bp": 8000, "importance_bp": 9000, "recency_bp": 9000},
        }

    def test_recall_is_recorded_and_replayed_as_three_cases(self) -> None:
        task = self.harness.record_recall(self.task, [self._hit(i) for i in range(4)])
        events = self.store.events(task.id)
        self.assertEqual(events[-1]["type"], "memory_recalled")
        detail = events[-1]["payload"]["detail"]
        self.assertEqual(detail["count"], 4)
        self.assertEqual(detail["query"], task.objective)
        self.store.verify_chain(task.id)

        memory = Loop(
            self.harness,
            planner=FinancePlanner(),
            result_tools=FINANCE_DOMAIN.result_tools,
        ).restore(self.store.load(task.id))
        context = memory.build(1, {}, None)
        self.assertEqual(len(context["prior_cases"]), 3)
        self.assertEqual(context["prior_cases"][0]["run_id"], "run-0")
        self.assertEqual(context["prior_cases"][0]["score_bp"], 9000)
        self.assertNotIn("breakdown", context["prior_cases"][0])

    def test_no_hits_means_no_event(self) -> None:
        before = len(self.store.events(self.task.id))
        self.harness.record_recall(self.task, [])
        self.assertEqual(len(self.store.events(self.task.id)), before)


if __name__ == "__main__":
    unittest.main()

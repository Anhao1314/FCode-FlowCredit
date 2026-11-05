"""Bi-temporal facts: append corrections, query what was known when.

The tests use synthetic traces because the properties are about time, not about
the demo corpus: an older assertion must remain queryable after a newer one
replaces it, and the same trace must always produce the same rows.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.memory.facts import FactStore  # noqa: E402
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.connectors import default_registry  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.finance.facts import project_facts  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.tools.workspace import build_workspace_tools  # noqa: E402


def fact_trace(
    *,
    margin_bp: int = 1750,
    change_bp: int = -250,
    recorded_at: str = "2026-01-01T00:00:00+00:00",
    hash_char: str = "a",
    include_normalise: bool = True,
) -> dict:
    documents = [
        {
            "id": "doc_fy2024_restated",
            "title": "恒星智算 2024 年重述说明",
            "period": "FY2024",
            "unit": "CNY_10K",
            "basis": "IFRS",
        },
        {
            "id": "doc_fy2025",
            "title": "恒星智算 2025 年年度报告",
            "period": "FY2025",
            "unit": "CNY_1M",
            "basis": "IFRS",
        },
    ]
    events = [
        {"sequence": 1, "type": "task_created", "at": recorded_at, "hash": "c" * 64,
         "payload": {"detail": {}}},
        {"sequence": 2, "type": "tool_succeeded", "at": recorded_at, "hash": hash_char * 64,
         "payload": {"detail": {
             "tool": "list_documents",
             "arguments": {},
             "result": {"documents": documents},
             "result_hash": "1" * 64,
         }}},
    ]
    if include_normalise:
        events.append({
            "sequence": 3, "type": "tool_succeeded", "at": recorded_at,
            "hash": hash_char * 64,
            "payload": {"detail": {
                "tool": "normalize_units",
                "arguments": {"doc_id": "doc_fy2024_restated"},
                "result": {
                    "id": "doc_fy2024_restated", "period": "FY2024",
                    "revenue_cny": 864000000, "gross_profit_cny": 151200000,
                },
                "result_hash": "2" * 64,
            }},
        })
    events.append({
        "sequence": 4, "type": "tool_succeeded", "at": recorded_at,
        "hash": hash_char * 64,
        "payload": {"detail": {
            "tool": "calculate_margin",
            "arguments": {},
            "result": {
                "computed": True,
                "margin_bp_2024": margin_bp,
                "margin_bp_2025": 1500,
                "change_bp": change_bp,
                "periods": [
                    {"period": "FY2024", "doc_id": "doc_fy2024_restated",
                     "revenue_cny": 864000000, "gross_profit_cny": 151200000},
                    {"period": "FY2025", "doc_id": "doc_fy2025",
                     "revenue_cny": 1120000000, "gross_profit_cny": 168000000},
                ],
            },
            "result_hash": "3" * 64,
        }},
    })
    return {
        "task": {"id": "task-x", "objective": "判断恒星智算两期毛利率", "binding": {}},
        "events": events,
        "terminal": {"status": "succeeded", "result": {}, "failure": {}},
        "rounds": [],
        "chain": {"head_hash": events[-1]["hash"]},
    }


class FactProjectionTests(unittest.TestCase):
    def test_projection_is_deterministic_and_provenanced(self) -> None:
        trace = fact_trace()
        first = project_facts(trace, run_id="run-1")
        second = project_facts(trace, run_id="run-1")
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 7)
        margin = [fact for fact in first if fact["predicate"] == "gross_margin_bp"]
        self.assertEqual({fact["period"] for fact in margin}, {"FY2024", "FY2025"})
        self.assertTrue(all(isinstance(fact["value_int"], int) for fact in margin))
        fy2024 = next(fact for fact in margin if fact["period"] == "FY2024")
        self.assertEqual(fy2024["value_int"], 1750)
        self.assertEqual((fy2024["valid_from"], fy2024["valid_to"]), ("2024-01-01", "2024-12-31"))
        self.assertIn("恒星智算", fy2024["entities"])
        self.assertEqual(len(fy2024["source_hash"]), 64)

    def test_same_value_observed_twice_is_one_claim(self) -> None:
        facts = project_facts(fact_trace(include_normalise=True), run_id="run-1")
        keys = [(fact["predicate"], fact["period"], fact["value_int"]) for fact in facts]
        self.assertEqual(len(keys), len(set(keys)))


class FactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = FactStore(pathlib.Path(self._tmp.name) / "facts.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _upsert(self, trace: dict, run_id: str) -> int:
        return self.store.upsert_run(run_id=run_id, facts=project_facts(trace, run_id=run_id))

    def test_upsert_is_idempotent(self) -> None:
        trace = fact_trace()
        self._upsert(trace, "run-1")
        before = self.store.count()
        self._upsert(trace, "run-1")
        self.assertEqual(self.store.count(), before)

    def test_correction_appends_and_keeps_the_old_belief(self) -> None:
        self._upsert(fact_trace(margin_bp=1750), "run-1")
        self._upsert(
            fact_trace(margin_bp=1700, recorded_at="2026-06-01T00:00:00+00:00", hash_char="b"),
            "run-2",
        )
        current = self.store.query(predicate="gross_margin_bp", period="FY2024")
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["value_int"], 1700)
        self.assertEqual(current[0]["source_run"], "run-2")
        self.assertTrue(current[0]["supersedes"])

        before = self.store.query(
            predicate="gross_margin_bp", period="FY2024",
            known_at="2026-03-01T00:00:00+00:00",
        )
        self.assertEqual(before[0]["value_int"], 1750)
        self.assertEqual(before[0]["superseded_by"], current[0]["fact_id"])

        history = self.store.history(subject="恒星智算", predicate="gross_margin_bp", period="FY2024")
        self.assertEqual([row["value_int"] for row in history], [1750, 1700])
        self.assertEqual(history[0]["fact_id"], current[0]["supersedes"])

    def test_valid_time_and_known_time_are_independent(self) -> None:
        self._upsert(fact_trace(), "run-1")
        self.assertEqual(
            len(self.store.query(predicate="gross_margin_bp", period="FY2024", as_of="2024-06-30")),
            1,
        )
        self.assertEqual(
            len(self.store.query(predicate="gross_margin_bp", period="FY2024", as_of="2025-06-30")),
            0,
        )
        self.assertEqual(
            len(self.store.query(predicate="gross_margin_bp", period="FY2024",
                                 known_at="2025-12-31T00:00:00+00:00")),
            0,
        )

    def test_tombstone_hides_the_run_and_reveals_the_prior_claim(self) -> None:
        self._upsert(fact_trace(margin_bp=1750), "run-1")
        self._upsert(
            fact_trace(margin_bp=1700, recorded_at="2026-06-01T00:00:00+00:00", hash_char="b"),
            "run-2",
        )
        self.store.tombstone_run("run-2")
        current = self.store.query(predicate="gross_margin_bp", period="FY2024")
        self.assertEqual(current[0]["value_int"], 1750)
        self.assertEqual(current[0]["source_run"], "run-1")

    def test_count_can_include_tombstoned_claims(self) -> None:
        self._upsert(fact_trace(), "run-1")
        before = self.store.count()
        self.store.tombstone_run("run-1")
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.count(include_archived=True), before)

    def test_rebuild_is_byte_identical(self) -> None:
        traces = [
            fact_trace(margin_bp=1750),
            fact_trace(margin_bp=1700, recorded_at="2026-06-01T00:00:00+00:00", hash_char="b"),
        ]
        for run_id, trace in zip(("run-1", "run-2"), traces):
            self._upsert(trace, run_id)
        with tempfile.TemporaryDirectory() as tmp:
            other = FactStore(pathlib.Path(tmp) / "facts.sqlite")
            for run_id, trace in zip(("run-1", "run-2"), traces):
                other.upsert_run(run_id=run_id, facts=project_facts(trace, run_id=run_id))

            def rows(path: pathlib.Path) -> list[tuple]:
                conn = sqlite3.connect(path)
                try:
                    return conn.execute("SELECT * FROM facts ORDER BY fact_id").fetchall()
                finally:
                    conn.close()

            self.assertEqual(rows(self.store.path), rows(other.path))


class FactToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.store = FactStore(root / "facts.sqlite")
        trace = fact_trace()
        self.store.upsert_run(run_id="run-1", facts=project_facts(trace, run_id="run-1"))
        self.registry = ToolRegistry(build_workspace_tools(
            EvidenceStore(root),
            ArtifactStore(root / "artifacts"),
            default_registry(),
            None,
            self.store,
        ))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_query_facts_returns_current_assertions(self) -> None:
        observation = self.registry.execute(
            "query_facts",
            {"predicate": "gross_margin_bp", "period": "FY2024"},
        )
        self.assertEqual(observation.tool_version, "1.0")
        self.assertEqual(observation.result["count"], 1)
        fact = observation.result["facts"][0]
        self.assertEqual(fact["value_int"], 1750)
        self.assertEqual(fact["valid_from"], "2024-01-01")
        self.assertTrue(fact["source_hash"])


if __name__ == "__main__":
    unittest.main()

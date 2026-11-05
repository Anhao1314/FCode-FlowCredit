"""recall_observation and the artifact book: in-window rescue + cross-task memory."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.memory.artifacts import ArtifactBook, project_artifacts  # noqa: E402
from flowagent.tools.base import Observation, validate_arguments  # noqa: E402
from flowagent.tools.general.recall import build_recall_tool, summarize_recall  # noqa: E402


def record(round_no, tool, result, arguments=None, result_hash=None):
    return {
        "round": round_no,
        "tool": tool,
        "arguments": arguments or {},
        "summary": result,
        "result_hash": result_hash or ("h" * 64),
    }


class RecallToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [
            record(1, "fetch_url", {"url": "https://example.com", "status": 200}, {"url": "https://example.com"}),
            record(2, "write_file", {"path": "fib.py", "bytes": 324}, {"path": "fib.py"}),
            record(3, "run_command", {"exit_code": 0}, {"command": "python3"}),
        ]
        self.spec = build_recall_tool(lambda: self.records)

    def test_by_round_returns_the_compressed_summary(self) -> None:
        result = self.spec.handler({"round": 2})
        self.assertEqual(result["count"], 1)
        hit = result["hits"][0]
        self.assertEqual(hit["round"], 2)
        self.assertEqual(hit["tool"], "write_file")
        self.assertEqual(hit["summary"]["path"], "fib.py")
        self.assertEqual(hit["result_hash"], "h" * 64)

    def test_by_tool_filters(self) -> None:
        result = self.spec.handler({"tool": "run_command"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["hits"][0]["tool"], "run_command")

    def test_contains_matches_arguments_and_summary(self) -> None:
        result = self.spec.handler({"contains": "example.com"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["hits"][0]["round"], 1)
        result = self.spec.handler({"contains": "fib"})
        self.assertEqual(result["hits"][0]["round"], 2)

    def test_miss_reports_not_found_without_raising(self) -> None:
        result = self.spec.handler({"round": 99})
        self.assertEqual(result["hits"], [])
        self.assertIn("no committed observation", result["not_found"])

    def test_empty_query_is_refused_not_dumped(self) -> None:
        with self.assertRaises(AgentError) as raised:
            self.spec.handler({})
        self.assertEqual(raised.exception.code, "RECALL_QUERY_EMPTY")

    def test_hits_are_capped(self) -> None:
        self.records = [record(i, "gen", {"n": i}) for i in range(1, 21)]
        spec = build_recall_tool(lambda: self.records)
        result = spec.handler({"tool": "gen"})
        self.assertEqual(result["count"], 5)

    def test_arguments_validate_against_the_schema(self) -> None:
        with self.assertRaises(AgentError):
            validate_arguments(self.spec, {"round": "not-an-int", "tool": "x"})
        validate_arguments(self.spec, {"round": 3, "tool": "x", "contains": "y"})

    def test_summary_contract_is_index_shaped(self) -> None:
        summary = summarize_recall({
            "hits": [record(1, "x", {})],
            "count": 1,
            "not_found": "",
        })
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["hits"][0]["result_hash"], "h" * 64)
        self.assertNotIn("summary", summary["hits"][0])


class RecallGovernanceTests(unittest.TestCase):
    """Recall is an ordinary gated read: it flows through registry.execute and
    lands as a tool_succeeded observation like any other tool."""

    def test_recall_through_the_registry(self) -> None:
        from flowagent.tools.registry import ToolRegistry

        records = [record(1, "fetch_url", {"status": 200})]
        registry = ToolRegistry([build_recall_tool(lambda: records)])
        observation: Observation = registry.execute(
            "recall_observation", {"round": 1}
        )
        self.assertEqual(observation.tool, "recall_observation")
        self.assertEqual(observation.result["hits"][0]["tool"], "fetch_url")
        self.assertTrue(observation.result_hash)
        # the summarizer contract is on the spec, so memory compresses it too
        summarizers = registry.summarizers()
        self.assertIn("recall_observation", summarizers)
        self.assertEqual(
            summarizers["recall_observation"](observation.result)["count"], 1
        )


class ArtifactBookTests(unittest.TestCase):
    def make_trace(self) -> dict:
        return {
            "task": {"created_at": "2026-10-25T12:00:00Z"},
            "chain": {"head": "abc123"},
            "rounds": [
                {"index": 2, "observation": {
                    "tool": "write_file",
                    "result": {"path": "fib.py", "sha256_after": "f" * 64, "bytes": 324, "changed": True},
                }},
                {"index": 4, "observation": {
                    "tool": "edit_file",
                    "result": {"path": "fib.py", "sha256_after": "e" * 64, "bytes": 400, "changed": True},
                }},
                {"index": 5, "observation": {
                    "tool": "write_file",
                    "result": {"path": "results.md", "sha256_after": "d" * 64, "bytes": 1033, "changed": True},
                }},
                # unchanged writes produce no artifact row
                {"index": 6, "observation": {
                    "tool": "write_file",
                    "result": {"path": "results.md", "sha256_after": "d" * 64, "bytes": 1033, "changed": False},
                }},
                # non-producing tools are ignored
                {"index": 7, "observation": {
                    "tool": "read_file",
                    "result": {"path": "fib.py"},
                }},
            ],
        }

    def test_projection_is_pure_and_signed(self) -> None:
        artifacts = project_artifacts(self.make_trace(), run_id="run-1")
        self.assertEqual([a.path for a in artifacts], ["fib.py", "fib.py", "results.md"])
        self.assertEqual([a.produced_by for a in artifacts], ["write_file", "edit_file", "write_file"])
        self.assertTrue(all(a.chain_head == "abc123" for a in artifacts))

    def test_book_roundtrip_latest_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = ArtifactBook(pathlib.Path(tmp) / "artifacts.sqlite")
            self.assertEqual(book.ingest_trace(self.make_trace(), run_id="run-1"), 3)
            latest = book.latest("fib.py")
            self.assertEqual(latest.sha256, "e" * 64)
            self.assertEqual(latest.produced_by, "edit_file")
            history = book.by_path("fib.py")
            self.assertEqual(len(history), 2)
            self.assertEqual(book.by_sha("d" * 64)[0].path, "results.md")
            self.assertEqual(book.count(), 3)
            book.close()

    def test_archive_run_tombstones_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = ArtifactBook(pathlib.Path(tmp) / "artifacts.sqlite")
            book.ingest_trace(self.make_trace(), run_id="run-1")
            self.assertEqual(book.archive_run("run-1"), 3)
            self.assertEqual(book.count(), 3)          # rows still there
            self.assertEqual(book.count(archived=True), 3)
            self.assertEqual(book.count(archived=False), 0)
            self.assertTrue(book.latest("fib.py").archived)
            book.close()

    def test_search_is_lexical_and_explainable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = ArtifactBook(pathlib.Path(tmp) / "artifacts.sqlite")
            book.ingest_trace(self.make_trace(), run_id="run-1")
            hits = book.search("results")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].path, "results.md")
            self.assertEqual(book.search("nope"), [])
            book.close()

    def test_real_general_trace_projects(self) -> None:
        """A real run projects into artifact facts, faithfully.

        The file names belong to the plan (a run may call its script fib.py or
        script.py), so the assertion is the projection's contract against the
        trace itself, not a hardcoded name.
        """
        path = pathlib.Path(__file__).resolve().parent.parent / "work" / "demo" / "general-research.trace.json"
        if not path.exists():
            self.skipTest("demo trace not generated yet")
        trace = json.loads(path.read_text(encoding="utf-8"))
        expected = [
            str((record.get("observation") or {}).get("result", {}).get("path") or "")
            for record in trace.get("rounds") or []
            if (record.get("observation") or {}).get("tool") in ("write_file", "edit_file")
            and (record.get("observation") or {}).get("result", {}).get("changed") is not False
            and (record.get("observation") or {}).get("result", {}).get("sha256_after")
            and (record.get("observation") or {}).get("result", {}).get("path")
        ]
        self.assertTrue(expected, "the demo run wrote at least one file")
        artifacts = project_artifacts(trace, run_id="demo-general")
        self.assertEqual([a.path for a in artifacts], expected)
        for artifact in artifacts:
            self.assertTrue(artifact.path and not artifact.path.startswith("/"))
            self.assertEqual(len(artifact.sha256), 64)
            self.assertIn(artifact.produced_by, ("write_file", "edit_file"))


if __name__ == "__main__":
    unittest.main()

"""Evidence ingestion: sandbox escapes, content addressing, bounded reads.

These tests attack the tool rather than exercise the happy path: the evidence
root is the only place it may read from, and every observation must be
reproducible from its hash and anchors.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class EvidenceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        (self.root / "ledger.csv").write_text(
            "date,account,amount\n2025-01-01,revenue,1200\n2025-01-02,cogs,700\n",
            encoding="utf-8",
        )
        (self.root / "notes.txt").write_text(
            "第一行：收入确认政策\n第二行：毛利率按 IFRS 口径\n第三行：无重述\n",
            encoding="utf-8",
        )
        (self.root / "big.txt").write_text("x" * (3 * 1024 * 1024), encoding="utf-8")
        self.registry = ToolRegistry(build_finance_tools(FinanceWorld(), EvidenceStore(self.root)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ingest_is_content_addressed_and_anchored(self) -> None:
        first = self.registry.execute("ingest_file", {"path": "ledger.csv"}).result
        second = self.registry.execute("ingest_file", {"path": "ledger.csv"}).result
        self.assertEqual(first["doc_id"], second["doc_id"])
        self.assertTrue(first["doc_id"].startswith("evidence:"))
        self.assertEqual(first["kind"], "csv")
        self.assertEqual(first["rows"], 2)
        self.assertEqual(first["columns"], ["date", "account", "amount"])
        self.assertEqual(len(first["sha256"]), 64)

    def test_absolute_paths_and_escapes_are_refused(self) -> None:
        for path in ("/etc/hosts", "../outside.txt", "sub/../../outside.txt"):
            with self.assertRaises(AgentError) as ctx:
                self.registry.execute("ingest_file", {"path": path})
            self.assertIn(ctx.exception.code, {"EVIDENCE_PATH_DENIED", "EVIDENCE_NOT_FOUND"})

    def test_oversized_file_is_refused_before_decoding(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.execute("ingest_file", {"path": "big.txt"})
        self.assertEqual(ctx.exception.code, "EVIDENCE_TOO_LARGE")

    def test_search_returns_line_anchors_and_respects_limit(self) -> None:
        self.registry.execute("ingest_file", {"path": "notes.txt"})
        result = self.registry.execute("search_corpus", {"query": "ifrs", "limit": 1}).result
        self.assertEqual(result["scanned"], 1)
        self.assertLessEqual(len(result["hits"]), 1)
        hit = result["hits"][0]
        self.assertEqual(hit["line"], 2)
        self.assertIn("IFRS", hit["snippet"])

    def test_read_slice_is_bounded_and_reports_truncation(self) -> None:
        ingest = self.registry.execute("ingest_file", {"path": "notes.txt"}).result
        window = self.registry.execute(
            "read_slice", {"doc_id": ingest["doc_id"], "start": 2, "end": 2}
        ).result
        self.assertEqual(window["start"], 2)
        self.assertEqual(window["end"], 2)
        self.assertEqual(len(window["lines"]), 1)
        self.assertEqual(window["lines"][0]["text"], "第二行：毛利率按 IFRS 口径")
        self.assertTrue(window["truncated"])

    def test_read_slice_clamps_past_the_end(self) -> None:
        ingest = self.registry.execute("ingest_file", {"path": "notes.txt"}).result
        window = self.registry.execute(
            "read_slice", {"doc_id": ingest["doc_id"], "start": 99}
        ).result
        self.assertEqual(window["start"], window["end"])
        self.assertEqual(len(window["lines"]), 1)

    def test_unknown_doc_id_is_a_clean_tool_error(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.execute("read_slice", {"doc_id": "evidence:deadbeef"})
        self.assertEqual(ctx.exception.code, "EVIDENCE_NOT_FOUND")

    def test_catalog_stays_deterministic_and_contracts_hold(self) -> None:
        before = [schema["name"] for schema in self.registry.catalog()]
        self.registry.execute("ingest_file", {"path": "ledger.csv"})
        after = [schema["name"] for schema in self.registry.catalog()]
        self.assertEqual(before, after)
        self.assertEqual(len(after), 16)
        for name in ("ingest_file", "search_corpus", "read_slice"):
            self.assertEqual(self.registry.get(name).version, "1.0")


if __name__ == "__main__":
    unittest.main()

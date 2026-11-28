"""R1 knowledge base: host-configured docs, deterministic BM25, cited hits."""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.tools.general.knowledge import (  # noqa: E402
    KnowledgeIndex,
    KnowledgeSource,
    build_knowledge_context,
    knowledge_sources_from_env,
    summarize_search_knowledge,
)
from flowagent.tools.general.tools import build_general_tools  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402

DOC = """# 退款政策

## 审批上限

退款金额超过 5,000 元需要运营主管审批，超过 50,000 元需要财务复核。

## 处理时限

普通退款 3 个工作日内完成；争议退款最多 15 个工作日。
"""

OTHER = """# Deployment runbook

Rollout must be canary 5% for 10 minutes before full traffic.
"""


class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name) / "docs"
        (root / "policy").mkdir(parents=True)
        (root / "ops").mkdir()
        (root / "policy" / "refund.md").write_text(DOC, encoding="utf-8")
        (root / "ops" / "runbook.md").write_text(OTHER, encoding="utf-8")
        self.index = KnowledgeIndex([
            KnowledgeSource(name="policy", root=root / "policy"),
            KnowledgeSource(name="ops", root=root / "ops"),
        ])

    def test_markdown_chunks_carry_citations(self) -> None:
        result = self.index.search("退款 审批 上限")
        self.assertGreaterEqual(result["count"], 1)
        top = result["results"][0]
        self.assertEqual(top["source"], "policy")
        self.assertEqual(top["path"], "refund.md")
        self.assertEqual(top["title"], "退款政策")
        self.assertIn("审批上限", top["headings"])
        self.assertGreaterEqual(top["start_line"], 1)
        self.assertIn("5,000", top["snippet"])
        # Observations enter the hash chain; ranking must not leak floats.
        self.assertIsInstance(top["score_bp"], int)
        self.assertNotIn("score", top)

    def test_search_result_is_chain_safe(self) -> None:
        from flowagent.core.hashing import digest

        result = self.index.search("退款 审批 上限")
        # canonical_json rejects floats; digest would raise if the result did.
        self.assertIsInstance(digest(result), str)

    def test_prefetched_context_is_bounded_and_hashed(self) -> None:
        context = build_knowledge_context(self.index.sources, "退款审批上限是多少")
        self.assertIsNotNone(context)
        self.assertLessEqual(context["chars"], 2_400)
        self.assertEqual(context["chars"], len(context["block"]))
        self.assertIn("退款政策", context["block"])
        self.assertIn("5,000", context["block"])
        self.assertEqual(len(context["block_sha256"]), 64)
        self.assertGreaterEqual(len(context["citations"]), 1)
        for citation in context["citations"]:
            self.assertIsInstance(citation["score_bp"], int)
        # Deterministic for the same corpus/query: the binding can prove the
        # exact prefetched bytes.
        again = build_knowledge_context(self.index.sources, "退款审批上限是多少")
        self.assertEqual(again["block_sha256"], context["block_sha256"])

    def test_prefetched_context_absent_without_hits_or_sources(self) -> None:
        self.assertIsNone(build_knowledge_context([], "退款审批上限"))
        self.assertIsNone(build_knowledge_context(self.index.sources, "  "))
        self.assertIsNone(build_knowledge_context(self.index.sources, "quantum toaster firmware"))

    def test_source_filter_and_exact_phrase_boost(self) -> None:
        ops = self.index.search("canary rollout", source="ops")
        self.assertGreaterEqual(ops["count"], 1)
        self.assertEqual({item["source"] for item in ops["results"]}, {"ops"})
        self.assertIn("canary 5%", ops["results"][0]["snippet"])

    def test_invalid_query_fails_closed(self) -> None:
        with self.assertRaises(Exception):
            self.index.search("   ")

    def test_sensitive_files_are_not_indexed(self) -> None:
        root = self.index.sources[0].root
        secret = root / "secrets.md"
        secret.write_text("approval limit is 999999", encoding="utf-8")
        try:
            result = self.index.search("999999 approval")
            self.assertEqual(result["count"], 0)
        finally:
            secret.unlink()


class KnowledgeSourceEnvTests(unittest.TestCase):
    def test_parses_named_pairs_and_skips_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "docs"
            good.mkdir()
            missing = Path(tmp) / "missing"
            raw = f"policy={good}{os.pathsep}{missing}"
            sources = knowledge_sources_from_env(raw)
            self.assertEqual([item.name for item in sources], ["policy"])
            self.assertEqual(sources[0].root, good.resolve())

    def test_empty_env_means_no_sources(self) -> None:
        self.assertEqual(knowledge_sources_from_env(""), [])


class KnowledgeToolTests(unittest.TestCase):
    def test_tool_registered_only_with_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            (docs / "a.md").write_text(DOC, encoding="utf-8")
            sandbox = WorkspaceSandbox(Path(tmp) / "workspace")
            with_specs = build_general_tools(
                sandbox, UrlFetcher(),
                knowledge_sources=[KnowledgeSource(name="policy", root=docs)],
            )
            without = build_general_tools(sandbox, UrlFetcher())
            names_with = {spec.name for spec in with_specs}
            names_without = {spec.name for spec in without}
            self.assertIn("search_knowledge", names_with)
            self.assertNotIn("search_knowledge", names_without)
            spec = next(item for item in with_specs if item.name == "search_knowledge")
            self.assertEqual(spec.effect, "read")
            # The handler must run while the source directory still exists:
            # the index is lazy, so a deleted root honestly returns zero hits.
            result = spec.handler({"query": "退款审批上限"})
            self.assertGreaterEqual(result["count"], 1)
            self.assertIn("5,000", result["results"][0]["snippet"])

    def test_without_sources_handler_is_not_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp) / "workspace")
            specs = build_general_tools(sandbox, UrlFetcher(), knowledge_sources=[])
        self.assertNotIn("search_knowledge", {spec.name for spec in specs})

    def test_summary_keeps_top_citations_bounded(self) -> None:
        compact = summarize_search_knowledge({
            "query": "q", "count": 2, "indexed_files": 1,
            "indexed_chunks": 2, "truncated": False,
            "results": [
                {"source": "policy", "path": "a.md", "title": "T",
                 "headings": "H", "start_line": 1, "end_line": 2,
                 "snippet": "x" * 500},
                {"source": "ops", "path": "b.md", "title": "U",
                 "headings": "", "start_line": 3, "end_line": 4,
                 "snippet": "y"},
            ],
        })
        self.assertEqual(len(compact["results"]), 2)
        self.assertLessEqual(len(compact["results"][0]["snippet"]), 360)


if __name__ == "__main__":
    unittest.main()

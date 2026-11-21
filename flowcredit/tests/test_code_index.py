"""Code retrieval: BM25 over workspace chunks, exposed as a read-only tool.

The index is deliberately dependency-free and deterministic. These tests pin
the properties a coding agent relies on: identifier-part matching, CJK
bigrams, path scoping, and the same secret/binary/vendor exclusions as the
rest of the workspace tools.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.code_index import WorkspaceIndex, tokens_for  # noqa: E402
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import GENERAL_TOOLS, build_general_tools  # noqa: E402


class CodeIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name) / "ws"
        for directory in ("src", "docs", "node_modules", ".git"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "src" / "pricing.py").write_text(
            "def calculate_invoice(items):\n"
            "    total = sum(item['price'] for item in items)\n"
            "    return total\n",
            encoding="utf-8",
        )
        (self.root / "src" / "unrelated.py").write_text(
            "def render_page():\n    return '<html>'\n",
            encoding="utf-8",
        )
        (self.root / "docs" / "pricing.md").write_text(
            "# Pricing\nInvoice rounding rule: round half up.\n"
            "订单计价口径以含税价为准。\n",
            encoding="utf-8",
        )
        (self.root / "node_modules" / "needle.py").write_text(
            "secret_needle_marker = True\n", encoding="utf-8",
        )
        (self.root / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
        (self.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        self.index = WorkspaceIndex(self.root)

    def test_identifier_parts_rank_code_first(self) -> None:
        result = self.index.search("calculate invoice")
        paths = [item["path"] for item in result["files"]]
        self.assertEqual(paths[0], "src/pricing.py")
        self.assertIn("calculate_invoice", result["files"][0]["snippet"])
        self.assertGreaterEqual(result["indexed_files"], 3)

    def test_search_result_is_canonical_json_safe(self) -> None:
        """搜索结果进证据链，canonical_json 禁浮点——score 定点化为
        ×10⁴ 整数（真模型 Work run 的 observe 哈希在 float 上崩过）。"""
        from flowagent.core.hashing import canonical_json

        result = self.index.search("calculate invoice rounding")
        self.assertTrue(result["files"])
        canonical_json(result)
        for item in result["files"]:
            self.assertIsInstance(item["score"], int)
        self.assertIsInstance(result["index_ms"], int)

    def test_cjk_bigrams_find_the_document(self) -> None:
        result = self.index.search("计价口径")
        self.assertTrue(result["files"])
        self.assertEqual(result["files"][0]["path"], "docs/pricing.md")
        self.assertIn("计价", result["files"][0]["snippet"])

    def test_subtree_and_limit_are_respected(self) -> None:
        docs = self.index.search("invoice", subtree="docs")
        self.assertEqual([item["path"] for item in docs["files"]], ["docs/pricing.md"])
        limited = self.index.search("invoice", limit=1)
        self.assertEqual(len(limited["files"]), 1)

    def test_secrets_binaries_and_vendor_dirs_are_not_indexed(self) -> None:
        for query in ("secret_needle_marker", "API_KEY", "PNG"):
            self.assertEqual(self.index.search(query)["count"], 0, query)

    def test_tokenizer_splits_identifiers_and_camel_case(self) -> None:
        tokens = tokens_for("calculate_invoice parseActionBlock")
        for expected in ("calculate_invoice", "invoice", "parseactionblock", "action", "block"):
            self.assertIn(expected, tokens)

    def test_tool_is_registered_read_only_and_returns_hits(self) -> None:
        sandbox = WorkspaceSandbox(self.root)
        specs = build_general_tools(sandbox, UrlFetcher())
        names = [spec.name for spec in specs]
        self.assertIn("search_codebase", names)
        self.assertIn("search_codebase", GENERAL_TOOLS)
        spec = next(item for item in specs if item.name == "search_codebase")
        self.assertEqual(spec.effect, "read")
        result = spec.handler({"query": "invoice rounding", "limit": 2})
        self.assertTrue(result["files"])
        self.assertLessEqual(result["count"], 2)
        self.assertIn("index_ms", result)
        self.assertFalse(result["truncated"])

    def test_tool_refuses_empty_query_and_non_directory_scope(self) -> None:
        sandbox = WorkspaceSandbox(self.root)
        spec = next(
            item for item in build_general_tools(sandbox, UrlFetcher())
            if item.name == "search_codebase"
        )
        with self.assertRaises(AgentError) as error:
            spec.handler({"query": "   "})
        self.assertEqual(error.exception.code, "SEARCH_QUERY_INVALID")
        with self.assertRaises(AgentError) as error:
            spec.handler({"query": "invoice", "path": "src/pricing.py"})
        self.assertEqual(error.exception.code, "SEARCH_PATH_NOT_DIRECTORY")
        with self.assertRaises(AgentError) as error:
            spec.handler({"query": "invoice", "path": "../outside"})
        self.assertEqual(error.exception.code, "SANDBOX_PATH_DENIED")


if __name__ == "__main__":
    unittest.main()
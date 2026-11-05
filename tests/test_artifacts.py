"""Write effects: sandboxed, atomic, content-addressed, and approval-gated.

The point of these tests is that a write is *evidence*: the same claim twice
must not create two artifacts, a bad claim must be refused before any bytes
are written, and the host policy must mark the tool as consequential.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.base import EFFECT_READ, Param, ToolSpec  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.store = ArtifactStore(self.root / "artifacts")
        self.registry = ToolRegistry(
            build_finance_tools(
                FinanceWorld(),
                EvidenceStore(self.root),
                self.store,
            )
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_writes_one_content_addressed_section(self) -> None:
        result = self.registry.execute("working_paper_append", {
            "title": "毛利率可比性",
            "body": "两期口径一致，变化 -2.50 pp。",
            "citations": ["doc_fy2024_restated#L1", "doc_fy2025#L2"],
        }).result
        self.assertTrue(result["artifact_id"].startswith("artifact:"))
        self.assertEqual(result["citations"], 2)
        self.assertEqual(len(result["sha256"]), 64)
        written = (self.store.root / result["path"]).read_text(encoding="utf-8")
        self.assertIn("# 毛利率可比性", written)
        self.assertIn("- doc_fy2024_restated#L1", written)
        self.assertIn("两期口径一致", written)

    def test_same_claim_is_idempotent(self) -> None:
        payload = {"title": "同一声明", "body": "内容不变", "citations": ["doc_fy2025#L1"]}
        first = self.registry.execute("working_paper_append", payload).result
        second = self.registry.execute("working_paper_append", payload).result
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(len(list(self.store.root.glob("*.md"))), 1)

    def test_bad_claims_are_refused_before_writing(self) -> None:
        cases = [
            {"title": "", "body": "x"},
            {"title": "t", "body": ""},
            {"title": "t" * 121, "body": "x"},
            {"title": "t", "body": "x" * 8001},
            {"title": "t", "body": "x", "citations": ["ok"] * 21},
        ]
        for payload in cases:
            with self.assertRaises(AgentError) as ctx:
                self.registry.execute("working_paper_append", payload)
            self.assertIn(ctx.exception.code, {"ARTIFACT_INVALID", "TOOL_ARGUMENTS_INVALID"})
        self.assertEqual(len(list(self.store.root.glob("*.md"))), 0)

    def test_write_effect_is_visible_in_the_contract(self) -> None:
        spec = self.registry.get("working_paper_append")
        self.assertEqual(spec.effect, "write")
        self.assertEqual(spec.version, "1.0")
        self.assertIn("artifact_id", {item.name for item in spec.outputs})

    def test_unknown_effect_is_still_denied(self) -> None:
        spec = ToolSpec(
            name="money", summary="", effect="money", params=(),
            handler=lambda _: {"ok": True},
            outputs=(Param(name="ok", kind="boolean"),),
        )
        with self.assertRaises(AgentError) as ctx:
            ToolRegistry([spec]).execute("money", {})
        self.assertEqual(ctx.exception.code, "TOOL_EFFECT_DENIED")

    def test_demo_policy_names_the_write_tool_as_consequential(self) -> None:
        scenario = get_scenario("full")
        self.assertIn("working_paper_append", scenario.allowed_tools)
        self.assertIn("working_paper_append", scenario.approval_tools)
        self.assertIn("working_paper_append", scenario.grant["allowed_tools"])


if __name__ == "__main__":
    unittest.main()

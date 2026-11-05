"""Tool contracts: every tool declares what it returns, and the runtime checks it.

The output contract is deliberately separate from the model-facing schema.
Changing what the runtime validates must never change the context the model
sees, or every existing SFT sample silently drifts; the second test pins that
boundary.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.base import EFFECT_READ, Param, ToolSpec  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _finance_registry() -> ToolRegistry:
    return ToolRegistry(build_finance_tools(FinanceWorld()))


class ToolContractTests(unittest.TestCase):
    def test_every_registered_tool_declares_outputs_and_version(self) -> None:
        registry = _finance_registry()
        for schema in registry.catalog():
            spec = registry.get(schema["name"])
            self.assertTrue(spec.outputs, f"{spec.name} declares no output contract")
            self.assertTrue(spec.version)
            contract = spec.contract()
            self.assertEqual(
                set(contract),
                {"name", "summary", "version", "effect", "parameters", "outputs"},
            )

    def test_model_facing_catalog_does_not_leak_the_contract(self) -> None:
        # The checked-in SFT dataset was rendered against exactly these keys.
        registry = _finance_registry()
        for schema in registry.catalog():
            self.assertEqual(set(schema), {"name", "summary", "effect", "parameters"})

    def test_missing_required_output_fails_the_call(self) -> None:
        spec = ToolSpec(
            name="bad_missing", summary="", effect=EFFECT_READ, params=(),
            handler=lambda _: {"ok": True},
            outputs=(Param(name="ok", kind="boolean"), Param(name="value", kind="integer")),
            version="0.1",
        )
        registry = ToolRegistry([spec])
        with self.assertRaises(AgentError) as ctx:
            registry.execute("bad_missing", {})
        self.assertEqual(ctx.exception.code, "TOOL_OUTPUT_CONTRACT")
        self.assertIn("value", str(ctx.exception))

    def test_wrong_output_type_fails_the_call(self) -> None:
        spec = ToolSpec(
            name="bad_type", summary="", effect=EFFECT_READ, params=(),
            handler=lambda _: {"ok": "yes"},
            outputs=(Param(name="ok", kind="boolean"),),
            version="0.1",
        )
        registry = ToolRegistry([spec])
        with self.assertRaises(AgentError) as ctx:
            registry.execute("bad_type", {})
        self.assertEqual(ctx.exception.code, "TOOL_OUTPUT_CONTRACT")

    def test_nullable_output_accepts_none_but_null_is_refused_otherwise(self) -> None:
        nullable = ToolSpec(
            name="nullable", summary="", effect=EFFECT_READ, params=(),
            handler=lambda _: {"note": None},
            outputs=(Param(name="note", kind="string", nullable=True),),
        )
        ToolRegistry([nullable]).execute("nullable", {})  # must not raise

        strict = ToolSpec(
            name="strict", summary="", effect=EFFECT_READ, params=(),
            handler=lambda _: {"note": None},
            outputs=(Param(name="note", kind="string"),),
        )
        with self.assertRaises(AgentError) as ctx:
            ToolRegistry([strict]).execute("strict", {})
        self.assertEqual(ctx.exception.code, "TOOL_OUTPUT_CONTRACT")

    def test_observation_carries_the_tool_version(self) -> None:
        spec = ToolSpec(
            name="versioned", summary="", effect=EFFECT_READ, params=(),
            handler=lambda _: {"ok": True},
            outputs=(Param(name="ok", kind="boolean"),),
            version="2.1",
        )
        observation = ToolRegistry([spec]).execute("versioned", {})
        self.assertEqual(observation.tool_version, "2.1")
        self.assertEqual(observation.to_dict()["tool_version"], "2.1")

    def test_real_tool_results_satisfy_their_contracts(self) -> None:
        registry = _finance_registry()
        observation = registry.execute("read_document", {"doc_id": "doc_fy2024"})
        self.assertEqual(observation.tool_version, "1.0")
        observation = registry.execute("list_documents", {})
        self.assertEqual(observation.tool_version, "1.0")


if __name__ == "__main__":
    unittest.main()

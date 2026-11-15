"""Native tool schemas must be valid function-calling JSON, not just internal JSON."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.providers.tool_schema import to_openai_tools  # noqa: E402
from flowagent.tools.base import EFFECT_READ, Param, ToolSpec  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class ToolSchemaTests(unittest.TestCase):
    def test_array_parameters_declare_their_item_type(self) -> None:
        spec = ToolSpec(
            name="ask_operator",
            summary="ask",
            effect=EFFECT_READ,
            params=(
                Param(name="question", kind="string", required=True),
                Param(name="options", kind="array", required=False, maximum=4),
            ),
            handler=lambda arguments: {"ok": True},
        )
        schema = to_openai_tools(ToolRegistry([spec]))[0]["function"]["parameters"]
        options = schema["properties"]["options"]
        self.assertEqual(options["type"], "array")
        self.assertEqual(options["items"], {"type": "string"})
        self.assertEqual(options["maxItems"], 4)
        self.assertNotIn("maximum", options)
        self.assertEqual(schema["properties"]["question"]["type"], "string")

    def test_the_allowed_subset_still_filters_tools(self) -> None:
        one = ToolSpec(name="a", summary="a", effect=EFFECT_READ, params=(), handler=lambda arguments: {})
        two = ToolSpec(name="b", summary="b", effect=EFFECT_READ, params=(), handler=lambda arguments: {})
        names = [item["function"]["name"] for item in to_openai_tools(ToolRegistry([one, two]), allowed={"b"})]
        self.assertEqual(names, ["b"])


if __name__ == "__main__":
    unittest.main()

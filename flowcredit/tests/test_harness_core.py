"""S1 harness core: streamed loop, tool protocol, deadlines, events.

These tests pin the *behavioral contract* ported from the dissected
harness. They use fake streaming providers: no network, no real model.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    CheckDecision,
    Done,
    EventBus,
    HarnessCore,
    StreamRequest,
    StreamUsage,
    TextDelta,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUse,
    validate_input,
)


class ScriptedProvider:
    """Yields one scripted batch per model call."""

    def __init__(self, *batches):
        self.batches = list(batches)
        self.requests: list[StreamRequest] = []

    def stream(self, request):
        self.requests.append(request)
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


class SlowProvider:
    def __init__(self, delay_s: float):
        self.delay_s = delay_s

    def stream(self, request):
        time.sleep(self.delay_s)
        yield TextDelta("late")
        yield Done()


class BrokenProvider:
    def stream(self, request):
        raise RuntimeError("transport exploded")
        yield  # pragma: no cover


def echo_tool(calls: list[dict] | None = None) -> ToolSpec:
    def handler(args, context):
        if calls is not None:
            calls.append(dict(args))
        return ToolResult.ok(f"echo:{args['text']}")

    return ToolSpec(
        name="echo",
        description="echo text back",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=handler,
    )


class HarnessLoopTests(unittest.TestCase):
    def run_core(self, provider, *, tools=None, checker=None, **kwargs):
        bus = EventBus()
        core = HarnessCore(
            provider, ToolRegistry(tools if tools is not None else [echo_tool()]),
            checker=checker, bus=bus,
        )
        result = core.run(
            [{"role": "user", "content": [{"type": "text", "text": "go"}]}],
            system="sys", **kwargs,
        )
        return result, bus

    def test_text_only_call_completes_in_one_turn(self) -> None:
        result, bus = self.run_core(
            ScriptedProvider([TextDelta("hello"), Done()]))
        self.assertTrue(result.completed)
        self.assertEqual(result.turns, 1)
        assistant = result.messages[-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"][0]["text"], "hello")
        self.assertEqual(
            bus.types(),
            ["tool.describe", "loop.turn", "assistant.message", "loop.end"],
        )

    def test_tool_call_crosses_check_call_result_then_continues(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": "x"}), Done(stop_reason="tool_use")],
            [TextDelta("done"), Done()],
        )
        result, bus = self.run_core(provider, tools=[echo_tool(calls)])
        self.assertTrue(result.completed)
        self.assertEqual(result.turns, 2)
        self.assertEqual(calls, [{"text": "x"}])
        types = bus.types()
        self.assertEqual(types.count("tool.check"), 1)
        self.assertEqual(types.count("tool.call"), 1)
        self.assertEqual(types.count("tool.result"), 1)
        # 助手消息带 tool_use，随后是 user 消息带 tool_result
        assistant = result.messages[1]
        self.assertEqual(assistant["content"][0]["type"], "tool_use")
        tool_result = result.messages[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "t1")
        self.assertEqual(tool_result["content"], "echo:x")
        self.assertFalse(tool_result["is_error"])

    def test_invalid_arguments_become_a_readable_tool_error(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": 1}), Done(stop_reason="tool_use")],
            [TextDelta("fixed"), Done()],
        )
        result, bus = self.run_core(provider)
        self.assertTrue(result.completed)
        tool_result = result.messages[2]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertEqual(tool_result["code"], "TOOL_INPUT_INVALID")
        self.assertNotIn("tool.call", bus.types())

    def test_denied_call_never_executes(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": "x"}), Done(stop_reason="tool_use")],
            [TextDelta("ok"), Done()],
        )
        result, bus = self.run_core(
            provider, tools=[echo_tool(calls)],
            checker=lambda name, args, ctx: CheckDecision(
                allow=False, reason="policy says no"),
        )
        tool_result = result.messages[2]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertEqual(tool_result["code"], "TOOL_DENIED")
        self.assertEqual(calls, [])
        check = next(e for e in bus.history if e.type == "tool.check")
        self.assertFalse(check.payload["allow"])

    def test_tool_timeout_is_a_tool_error_not_a_crash(self) -> None:
        slow = ToolSpec(
            name="slow",
            description="sleeps",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args, ctx: time.sleep(0.3) or ToolResult.ok("late"),
        )
        provider = ScriptedProvider(
            [ToolUse("t1", "slow", {}), Done(stop_reason="tool_use")],
            [TextDelta("next"), Done()],
        )
        result, _ = self.run_core(
            provider, tools=[slow], tool_deadline_ms=50)
        self.assertTrue(result.completed)
        tool_result = result.messages[2]["content"][0]
        self.assertEqual(tool_result["code"], "TOOL_TIMEOUT")
        self.assertTrue(tool_result["is_error"])

    def test_slow_model_stream_hits_the_call_deadline(self) -> None:
        result, bus = self.run_core(
            SlowProvider(0.3), call_deadline_ms=50)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.code, "CALL_DEADLINE")
        self.assertIn("loop.error", bus.types())
        self.assertFalse(result.completed)

    def test_provider_fault_is_terminal_and_typed(self) -> None:
        result, _ = self.run_core(BrokenProvider())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.code, "PROVIDER_ERROR")
        self.assertIn("transport exploded", result.error)

    def test_max_turns_is_an_explicit_terminal(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": "a"}), Done(stop_reason="tool_use")],
            [ToolUse("t2", "echo", {"text": "b"}), Done(stop_reason="tool_use")],
        )
        result, _ = self.run_core(provider, max_turns=2)
        self.assertEqual(result.status, "max_turns")
        self.assertEqual(result.code, "MAX_TURNS")
        self.assertEqual(result.turns, 2)

    def test_usage_is_summed_across_turns(self) -> None:
        usage = StreamUsage(input_tokens=10, output_tokens=5,
                            cost_microusd=7, cached_tokens=3)
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": "x"}),
             Done(stop_reason="tool_use", usage=usage)],
            [TextDelta("ok"), Done(usage=usage)],
        )
        result, _ = self.run_core(provider)
        self.assertEqual(result.usage.input_tokens, 20)
        self.assertEqual(result.usage.output_tokens, 10)
        self.assertEqual(result.usage.cost_microusd, 14)
        self.assertEqual(result.usage.cached_tokens, 6)

    def test_events_are_chain_safe_json(self) -> None:
        result, _ = self.run_core(
            ScriptedProvider([TextDelta("hello"), Done()]))
        encoded = json.dumps(result.events)
        self.assertIn("assistant.message", encoded)
        # canonical_json 拒绝浮点：事件里不允许出现时间浮点
        for event in result.events:
            self.assertIsInstance(event["at_ms"], int)

    def test_tool_describe_is_bounded_to_allowed_names(self) -> None:
        provider = ScriptedProvider([TextDelta("ok"), Done()])
        result, bus = self.run_core(
            provider,
            tools=[echo_tool(), ToolSpec(
                name="other", description="x",
                input_schema={"type": "object"}, handler=lambda a, c: "no")],
            tool_names=["echo"],
        )
        describe = next(e for e in bus.history if e.type == "tool.describe")
        self.assertEqual(describe.payload["names"], ["echo"])
        self.assertEqual(describe.payload["count"], 1)


class ValidationTests(unittest.TestCase):
    def test_required_and_types(self) -> None:
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        validate_input(schema, {"n": 1})
        with self.assertRaises(Exception) as missing:
            validate_input(schema, {})
        self.assertIn("required", str(missing.exception))
        with self.assertRaises(Exception):
            validate_input(schema, {"n": True})  # bool 不是 integer
        with self.assertRaises(Exception):
            validate_input(schema, {"n": "x"})

    def test_array_items(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}}
        validate_input(schema, ["a", "b"])
        with self.assertRaises(Exception):
            validate_input(schema, ["a", 1])


if __name__ == "__main__":
    unittest.main()

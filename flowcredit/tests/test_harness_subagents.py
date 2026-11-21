"""S4 subagents: definitions, typed results, tool subsets, depth caps."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.harness import (  # noqa: E402
    AgentDefinition,
    AgentRegistry,
    Done,
    EventBus,
    HarnessCore,
    ORCHESTRATOR_TOOL,
    StreamUsage,
    SubagentRunner,
    TextDelta,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    ToolUse,
)


class ScriptedProvider:
    def __init__(self, *batches):
        self.batches = list(batches)
        self.requests = []

    def stream(self, request):
        self.requests.append(request)
        batch = self.batches.pop(0) if self.batches else [Done()]
        yield from batch


def echo_tool(calls: list[dict] | None = None) -> ToolSpec:
    def handler(args, _context):
        if calls is not None:
            calls.append(dict(args))
        return ToolResult.ok(f"echo:{args.get('text', '')}")

    return ToolSpec(
        name="echo", description="echo",
        input_schema={"type": "object",
                      "properties": {"text": {"type": "string"}},
                      "required": ["text"]},
        handler=handler,
    )


def secret_tool(calls: list[dict] | None = None) -> ToolSpec:
    def handler(args, _context):
        if calls is not None:
            calls.append(dict(args))
        return ToolResult.ok("secret")

    return ToolSpec(
        name="secret", description="secret",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )


def registry(**overrides) -> AgentRegistry:
    base = dict(
        definitions=[
            AgentDefinition("worker", when_to_use="bounded edits",
                            tools=("echo",)),
            AgentDefinition("researcher", when_to_use="read-only digging",
                            tools=("echo", "secret")),
        ],
        max_depth=1,
    )
    base.update(overrides)
    return AgentRegistry(**base)


class RegistryTests(unittest.TestCase):
    def test_describe_and_allowed(self) -> None:
        full = registry()
        self.assertEqual(full.names(), ["researcher", "worker"])
        rows = full.describe()
        self.assertEqual(rows[0]["agent_type"], "researcher")
        self.assertEqual(rows[1]["when_to_use"], "bounded edits")

        limited = registry(allowed_agent_types=["worker"])
        self.assertEqual(limited.names(), ["worker"])
        self.assertIsNone(limited.get("researcher"))
        self.assertTrue(limited.known("researcher"))

    def test_definition_validation(self) -> None:
        with self.assertRaises(ValueError):
            AgentDefinition("")
        definition = AgentDefinition("x", tools=["a", "b"], max_turns=0)
        self.assertEqual(definition.tools, ("a", "b"))
        self.assertEqual(definition.max_turns, 1)


class SpawnTests(unittest.TestCase):
    def runner(self, provider, *, max_depth=1, allowed=None, factory=None):
        return SubagentRunner(
            provider, ToolRegistry([echo_tool(), secret_tool()]),
            registry(max_depth=max_depth, allowed_agent_types=allowed),
            bus=EventBus(), provider_factory=factory,
        )

    def test_typed_success(self) -> None:
        usage = StreamUsage(input_tokens=7, output_tokens=3, cost_microusd=2)
        provider = ScriptedProvider([TextDelta("worker done"), Done(usage=usage)])
        runner = self.runner(provider)
        result = runner.spawn("worker", "do it")
        self.assertTrue(result.completed)
        self.assertEqual(result.text, "worker done")
        self.assertEqual(result.turns, 1)
        self.assertEqual(result.usage.output_tokens, 3)
        self.assertEqual(result.depth, 1)
        data = result.to_dict()
        self.assertEqual(data["agent_type"], "worker")
        self.assertEqual(data["usage"]["cost_microusd"], 2)
        events = [event.type for event in runner.bus.history]
        self.assertEqual(events, ["subagent.start", "subagent.end"])

    def test_unknown_and_disallowed_agents(self) -> None:
        runner = self.runner(ScriptedProvider(), allowed=["worker"])
        unknown = runner.spawn("nope", "x")
        self.assertEqual(unknown.status, "failed")
        self.assertEqual(unknown.code, "UNKNOWN_AGENT")
        disallowed = runner.spawn("researcher", "x")
        self.assertEqual(disallowed.code, "AGENT_NOT_ALLOWED")

    def test_depth_cap_is_typed(self) -> None:
        runner = self.runner(ScriptedProvider(), max_depth=1)
        result = runner.spawn("worker", "x", depth=2)
        self.assertEqual(result.code, "SUBAGENT_DEPTH")

    def test_tool_subset_is_enforced(self) -> None:
        secret_calls: list[dict] = []
        provider = ScriptedProvider(
            [ToolUse("t1", "secret", {}), Done(stop_reason="tool_use")],
            [TextDelta("child done"), Done()],
        )
        runner = SubagentRunner(
            provider,
            ToolRegistry([echo_tool(), secret_tool(secret_calls)]),
            registry(),
        )
        result = runner.spawn("worker", "try to read the secret")
        self.assertTrue(result.completed)
        self.assertEqual(secret_calls, [])

    def test_child_max_turns_is_reported(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("t1", "echo", {"text": "a"}), Done(stop_reason="tool_use")],
            [ToolUse("t2", "echo", {"text": "b"}), Done(stop_reason="tool_use")],
        )
        definition = AgentDefinition("worker", tools=("echo",), max_turns=2)
        runner = SubagentRunner(
            provider, ToolRegistry([echo_tool()]),
            AgentRegistry([definition]))
        result = runner.spawn("worker", "loop")
        self.assertEqual(result.status, "max_turns")
        self.assertEqual(result.code, "MAX_TURNS")

    def test_provider_factory_selects_the_definition_model(self) -> None:
        seen: list[str] = []
        cheap = ScriptedProvider([TextDelta("cheap result"), Done()])

        def factory(model):
            seen.append(model)
            return cheap

        definition = AgentDefinition("worker", tools=("echo",), model="cheap")
        runner = SubagentRunner(
            ScriptedProvider(), ToolRegistry([echo_tool()]),
            AgentRegistry([definition]), provider_factory=factory)
        result = runner.spawn("worker", "x")
        self.assertEqual(seen, ["cheap"])
        self.assertEqual(result.text, "cheap result")
        self.assertEqual(result.model, "cheap")


class OrchestratorToolTests(unittest.TestCase):
    def test_parent_spawns_child_and_gets_typed_result(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("p1", ORCHESTRATOR_TOOL, {
                "description": "delegate",
                "prompt": "do the work",
                "subagent_type": "worker",
            }), Done(stop_reason="tool_use")],
            [TextDelta("worker done"), Done()],
            [TextDelta("main done"), Done()],
        )
        runner = SubagentRunner(
            provider, ToolRegistry([echo_tool()]), registry())
        parent_tools = ToolRegistry([runner.orchestrator_tool(depth=0)])
        core = HarnessCore(provider, parent_tools)
        result = core.run([{"role": "user", "content": []}], system="s")
        self.assertTrue(result.completed)
        tool_result = result.messages[2]["content"][0]
        self.assertEqual(tool_result["content"], "worker done")
        self.assertFalse(tool_result["is_error"])
        self.assertEqual(tool_result["data"]["agent_type"], "worker")
        self.assertEqual(tool_result["data"]["depth"], 1)
        events = [event.type for event in runner.bus.history]
        self.assertIn("subagent.end", events)

    def test_depth_limit_removes_the_orchestrator_tool(self) -> None:
        provider = ScriptedProvider(
            [ToolUse("p1", ORCHESTRATOR_TOOL, {
                "description": "delegate", "prompt": "work",
                "subagent_type": "worker"}), Done(stop_reason="tool_use")],
            # the child tries to delegate again; at max_depth=1 it cannot
            [ToolUse("c1", ORCHESTRATOR_TOOL, {
                "description": "nested", "prompt": "more",
                "subagent_type": "worker"}), Done(stop_reason="tool_use")],
            [TextDelta("child done"), Done()],
            [TextDelta("main done"), Done()],
        )
        runner = SubagentRunner(
            provider, ToolRegistry([echo_tool()]), registry(max_depth=1))
        core = HarnessCore(provider, ToolRegistry([
            runner.orchestrator_tool(depth=0)]))
        result = core.run([{"role": "user", "content": []}], system="s")
        self.assertTrue(result.completed)
        starts = [event for event in runner.bus.history
                  if event.type == "subagent.start"]
        self.assertEqual(len(starts), 1)
        tool_result = result.messages[2]["content"][0]
        self.assertEqual(tool_result["content"], "child done")

    def test_orchestrator_schema_lists_allowed_types(self) -> None:
        runner = SubagentRunner(
            ScriptedProvider(), ToolRegistry([echo_tool()]), registry())
        schema = runner.orchestrator_tool(depth=0).input_schema
        self.assertEqual(schema["properties"]["subagent_type"]["enum"],
                         ["researcher", "worker"])
        self.assertIn("bounded edits", runner.orchestrator_tool(depth=0).description)


if __name__ == "__main__":
    unittest.main()

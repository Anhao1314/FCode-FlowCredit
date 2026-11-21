"""S5 Work adapter: capability proposals through the new harness core."""

from __future__ import annotations

import os
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.runtime.capability import HarnessCapabilityBridge  # noqa: E402
from flowagent.runtime.capability_core import (  # noqa: E402
    CoreBackedProvider,
    OpenAICompatHarnessProvider,
)


class FakePrice:
    def cost(self, usage):
        return 42


class FakeClient:
    def __init__(self, message, usage=None, request_id="req-1"):
        self.message = message
        self.usage = usage or {
            "prompt_tokens": 100, "completion_tokens": 20,
            "total_tokens": 120, "stream": True, "stream_chunks": 7,
        }
        self.request_id = request_id
        self.bodies = []

    def _build_body(self, system, user, **kwargs):
        self.bodies.append({"system": system, "user": user, **kwargs})
        return {"stub": True}

    def _post_stream(self, _body):
        return self.message, self.usage, self.request_id


class FakeProvider:
    name = "deepseek"
    model = "deepseek-test"
    timeout = 120.0

    def __init__(self, message, usage=None):
        self.client = FakeClient(message, usage)
        self.price = FakePrice()
        self.raw = []
        self.receipts = []

    def reserve(self, context):
        return (1_000, 500)

    def _completion_budget(self, context):
        return 16_384

    def _empty_content_error(self, message):
        return AgentError("EMPTY_CONTENT", "the provider returned an empty reply")

    def issue_receipt(self, **kwargs):
        self.receipts.append(kwargs)
        return "receipt-1"

    raw_sink = property(lambda self: self.raw.append)


def tool_call(name="edit_file", arguments='{"path": "index.html"}', call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def native_tools():
    return [{
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "edit the artifact",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    }]


def context():
    return {
        "prompt_profile": "capability-motion",
        "instruction": "propose one action",
        "objective": "build the artifact",
        "tools": native_tools(),
        "plan": [],
        "observations": [],
        "state": {"stage": {"name": "draft", "allowed_tools": ["edit_file"]}},
        "budget": {},
        "round": 1,
        "native_tools_allowed": ["edit_file"],
        "intent": {"task_id": "t1", "round": 1},
        "no_reasoning_retry": True,
    }


def provider(message):
    instance = CoreBackedProvider(FakeProvider(message))
    instance.set_native_tools(native_tools())
    return instance


class AdapterTests(unittest.TestCase):
    def test_stream_maps_text_reasoning_and_tool_calls(self) -> None:
        fake = FakeProvider({
            "content": "hello",
            "reasoning_content": "think",
            "tool_calls": [tool_call()],
        })
        adapter = OpenAICompatHarnessProvider(fake)
        events = list(adapter.stream(type("R", (), {
            "system": "s", "messages": [{"role": "user", "content": "u"}],
            "tools": [], "max_tokens": None,
        })()))
        kinds = [type(event).__name__ for event in events]
        self.assertEqual(kinds, ["ReasoningDelta", "TextDelta", "ToolUse", "Done"])
        self.assertEqual(events[1].text, "hello")
        self.assertEqual(events[2].name, "edit_file")
        self.assertEqual(events[2].input, {"path": "index.html"})
        self.assertEqual(events[3].usage.input_tokens, 100)
        self.assertEqual(events[3].usage.cost_microusd, 42)
        self.assertTrue(fake.client.bodies[0]["stream"])
        self.assertEqual(fake.client.bodies[0]["max_completion_tokens"], None)


class CoreBackedProviderTests(unittest.TestCase):
    def test_one_call_becomes_one_plan_step(self) -> None:
        core_provider = provider({"tool_calls": [tool_call()]})
        proposal = core_provider.propose(context())
        self.assertEqual(proposal.plan["steps"], [{
            "id": "call_1",
            "tool": "edit_file",
            "arguments": {"path": "index.html"},
        }])
        self.assertEqual(proposal.tokens, 120)
        self.assertEqual(proposal.cost_microusd, 42)
        self.assertEqual(proposal.metadata["harness"], "core")
        self.assertEqual(proposal.metadata["completion_tokens_override"], 16_384)
        self.assertEqual(proposal.metadata["usage_prompt_tokens"], 100)
        self.assertEqual(proposal.metadata["cache_hit_bp"], 0)
        self.assertTrue(proposal.metadata["stream"])
        self.assertEqual(proposal.metadata["hmac_receipt_ref"], "receipt-1")
        fake = core_provider.wrapped
        self.assertEqual(fake.receipts[0]["plan"]["steps"][0]["tool"], "edit_file")
        self.assertEqual(fake.raw[0]["harness"], "core")
        body = fake.client.bodies[0]
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_completion_tokens"], 16_384)
        self.assertEqual(body["tools"][0]["function"]["name"], "edit_file")

    def test_extra_tool_calls_are_dropped_with_a_count(self) -> None:
        core_provider = provider({"tool_calls": [
            tool_call(call_id="call_1"),
            tool_call(arguments='{"path": "other.html"}', call_id="call_2"),
        ]})
        proposal = core_provider.propose(context())
        self.assertEqual(len(proposal.plan["steps"]), 1)
        self.assertEqual(proposal.metadata["dropped_tool_calls"], 1)

    def test_invalid_id_is_sanitized(self) -> None:
        proposal = provider({"tool_calls": [
            tool_call(call_id="CALL 1/../x")]}).propose(context())
        self.assertEqual(proposal.plan["steps"][0]["id"], "a1")

    def test_empty_reply_is_a_typed_error(self) -> None:
        core_provider = provider({"content": "", "tool_calls": []})
        with self.assertRaises(AgentError) as caught:
            core_provider.propose(context())
        self.assertEqual(caught.exception.code, "EMPTY_CONTENT")

    def test_unoffered_tool_is_rejected(self) -> None:
        core_provider = provider({"tool_calls": [
            tool_call(name="run_command", arguments='{"command": "ls"}')]})
        with self.assertRaises(AgentError) as caught:
            core_provider.propose(context())
        self.assertEqual(caught.exception.code, "PROPOSAL_MALFORMED")

    def test_invalid_json_arguments_are_rejected(self) -> None:
        core_provider = provider({"tool_calls": [
            tool_call(arguments="{not json")]})
        with self.assertRaises(AgentError) as caught:
            core_provider.propose(context())
        self.assertEqual(caught.exception.code, "PROPOSAL_MALFORMED")

    def test_missing_native_tools_is_rejected(self) -> None:
        core_provider = provider({"tool_calls": [tool_call()]})
        core_provider.set_native_tools([])
        with self.assertRaises(AgentError) as caught:
            core_provider.propose(context())
        self.assertEqual(caught.exception.code, "PROPOSAL_MALFORMED")


class BridgeFlagTests(unittest.TestCase):
    def bridge(self, **kwargs):
        class Harness:
            provider = object()
            registry = type("R", (), {"names": ["edit_file"]})()
            budget = lambda self, task: type(
                "B", (), {"snapshot": staticmethod(lambda: {})})()

        class Task:
            id = "t1"
            allowed_tools = ["edit_file"]
            objective = "x"

        return HarnessCapabilityBridge(Harness(), Task(), **kwargs)

    def test_flag_reads_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_WORK_HARNESS": "core"}):
            self.assertTrue(self.bridge().core_harness)
        with mock.patch.dict(os.environ, {"FCODE_WORK_HARNESS": "legacy"}):
            self.assertFalse(self.bridge().core_harness)

    def test_explicit_flag_wins(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_WORK_HARNESS": "core"}):
            self.assertFalse(self.bridge(core_harness=False).core_harness)


class BridgeRoutingTests(unittest.TestCase):
    class Proposal:
        def __init__(self):
            self.tokens = 10
            self.cost_microusd = 5
            self.metadata = {"completion_tokens_override": 0}
            self.plan = {"version": "1", "steps": [{
                "id": "a1", "tool": "edit_file",
                "arguments": {"path": "index.html"}}]}

    class Harness:
        def __init__(self):
            self.provider = FakeProvider({"tool_calls": [tool_call()]})
            self.registry = type("R", (), {
                "names": ["edit_file"],
                "get": lambda self, name: type(
                    "T", (), {"to_schema": staticmethod(
                        lambda: {"name": "edit_file"})})(),
            })()
            self.used = None
            self.legacy = 0
            self.snapshots = 0

        def budget(self, task):
            outer = self

            class Budget:
                def snapshot(self):
                    outer.snapshots += 1
                    return {"calls_used": 0, "tokens_used": 0,
                            "cost_used_microusd": 0}

            return Budget()

        def propose_with(self, task, context, provider):
            self.used = provider
            return task, BridgeRoutingTests.Proposal()

        def propose(self, task, context):
            self.legacy += 1
            return task, BridgeRoutingTests.Proposal()

    class Task:
        id = "t1"
        allowed_tools = ["edit_file"]
        objective = "x"
        status = "running"

        class model:
            attempts = 0

    def bridge(self, harness, **kwargs):
        return HarnessCapabilityBridge(harness, self.Task(), **kwargs)

    def test_core_flag_routes_through_propose_with(self) -> None:
        harness = self.Harness()
        action = self.bridge(harness, core_harness=True).propose(
            {"stage": {"name": "draft"}, "remaining_budget": {}},
            tools=("edit_file",),
        )
        self.assertEqual(action.tool, "edit_file")
        self.assertIsInstance(harness.used, CoreBackedProvider)
        self.assertEqual(harness.legacy, 0)
        self.assertIsInstance(harness.provider, FakeProvider)

    def test_legacy_flag_keeps_the_old_propose(self) -> None:
        harness = self.Harness()
        self.bridge(harness, core_harness=False).propose(
            {"stage": {"name": "draft"}, "remaining_budget": {}},
            tools=("edit_file",),
        )
        self.assertEqual(harness.legacy, 1)
        self.assertIsNone(harness.used)


if __name__ == "__main__":
    unittest.main()

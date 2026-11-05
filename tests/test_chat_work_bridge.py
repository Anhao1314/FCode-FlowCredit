"""Chat → work bridge (layer 1): the conversation a delegation came from."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.memory.context import WorkingMemory  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.base import EFFECT_READ, Param, ToolSpec  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402

VIEWER = ROOT / "viewer"


def load_console_manager():
    # console_manager imports its sibling modules by name; the viewer dir must
    # be on sys.path exactly the way server.py puts it there.
    sys.path.insert(0, str(VIEWER))
    try:
        spec = importlib.util.spec_from_file_location("fcode_console_manager_bridge", VIEWER / "console_manager.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(VIEWER))


def make_origin(messages=None):
    return {
        "threadId": "chat-20261025-120000-abcd",
        "threadTitle": "行程安排",
        "messages": messages if messages is not None else [
            {"role": "user", "text": "帮我查查周三的高铁票"},
            {"role": "assistant", "text": "已查到周三 G7315 上海虹桥→杭州东……"},
            {"role": "user", "text": "到了后帮我约客户会议"},
        ],
    }


class SpecValidationTests(unittest.TestCase):
    """validate_delegation_spec must accept a well-formed origin_thread and
    refuse every malformed shape — the bridge carries context, not payloads."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manager = load_console_manager().RunManager(None, "", 0.0)
        from flowagent.demo.fixtures import get_scenario

        cls.general = get_scenario("general-research")

    def validate(self, spec):
        return self.manager.validate_delegation_spec(spec, self.general)

    def base_spec(self):
        return {"version": "fcode.intake/v1", "task": "general-research", "mode": "script", "source": "test"}

    def test_well_formed_origin_passes_and_is_capped(self) -> None:
        spec = self.base_spec()
        spec["origin_thread"] = make_origin(messages=[
            {"role": "user", "text": "x" * 900},
        ])
        clean = self.validate(spec)
        origin = clean["origin_thread"]
        self.assertEqual(origin["threadId"], "chat-20261025-120000-abcd")
        self.assertEqual(len(origin["messages"]), 1)
        self.assertEqual(len(origin["messages"][0]["text"]), 600)

    def test_a_long_thread_keeps_its_most_recent_turns(self) -> None:
        """Recency wins: 30 turns become the last 24, and the older ones go —
        "到了之后" refers to the most recent context, not the thread's opening."""
        spec = self.base_spec()
        spec["origin_thread"] = make_origin(messages=[
            {"role": "user", "text": f"第{i}轮"} for i in range(1, 31)
        ])
        self.validate(spec)
        kept = spec["origin_thread"]["messages"]
        self.assertEqual(len(kept), 24)
        self.assertEqual(kept[0]["text"], "第7轮")
        self.assertEqual(kept[-1]["text"], "第30轮")

    def test_a_long_message_is_truncated_not_refused(self) -> None:
        spec = self.base_spec()
        spec["origin_thread"] = make_origin(messages=[
            {"role": "user", "text": "长" * 5_000},
        ])
        self.validate(spec)
        self.assertEqual(len(spec["origin_thread"]["messages"][0]["text"]), 600)

    def test_bad_role_is_refused(self) -> None:
        spec = self.base_spec()
        spec["origin_thread"] = make_origin(messages=[
            {"role": "system", "text": "you are…"},
        ])
        with self.assertRaises(ValueError):
            self.validate(spec)

    def test_non_object_origin_is_refused(self) -> None:
        spec = self.base_spec()
        spec["origin_thread"] = "not an object"
        with self.assertRaises(ValueError):
            self.validate(spec)

    def test_missing_origin_is_fine(self) -> None:
        clean = self.validate(self.base_spec())
        self.assertNotIn("origin_thread", clean)


class ContextBridgeTests(unittest.TestCase):
    def test_build_includes_sanitised_conversation(self) -> None:
        memory = WorkingMemory(
            objective="到了后帮我约客户会议",
            tool_catalog=[],
            conversation=[
                {"role": "user", "text": "帮我查查周三的高铁票"},
                {"role": "assistant", "text": "已查到 G7315……"},
                {"role": "bogus", "text": "smuggled"},
            ],
        )
        context = memory.build(1, {})
        self.assertEqual(len(context["conversation"]), 2)
        self.assertEqual(context["conversation"][0]["text"], "帮我查查周三的高铁票")
        self.assertFalse(any(item["role"] == "bogus" for item in context["conversation"]))

    def test_without_conversation_the_key_never_appears(self) -> None:
        """The finance v10 prompt bytes must be untouched by this feature."""
        memory = WorkingMemory(objective="t", tool_catalog=[])
        context = memory.build(1, {})
        self.assertNotIn("conversation", context)

    def test_restore_reads_origin_from_the_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")

            def handler(_: dict) -> dict:
                return {"ok": True}

            spec = ToolSpec(
                name="probe", summary="p", effect=EFFECT_READ,
                params=(), handler=handler, outputs=(),
            )
            harness = Harness(
                store=store,
                registry=ToolRegistry([spec]),
                provider=MockProvider(),
                world=None,
                limits=BudgetLimits(10, 10, 10_000, 10_000),
                receipts=ReceiptStore(store),
                verifier=lambda obs: {"verified": True, "verification": "t", "result": obs.result},
            )
            task = harness.create(
                task_id="bridge-1",
                objective="到了后帮我约客户会议",
                principal="test",
                binding={"scenario": "general-research", "spec": {
                    "mode": "script", "origin_thread": make_origin(),
                }},
                allowed_tools=["probe"],
                grant={},
            )
            loop = Loop(harness, planner=None, max_rounds=5, result_tools=())
            memory = loop.restore(task)
            self.assertEqual(len(memory.conversation or []), 3)
            context = memory.build(1, {})
            self.assertIn("帮我查查周三的高铁票", json.dumps(context["conversation"], ensure_ascii=False))

    def test_restore_without_origin_yields_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")

            def handler(_: dict) -> dict:
                return {"ok": True}

            spec = ToolSpec(
                name="probe", summary="p", effect=EFFECT_READ,
                params=(), handler=handler, outputs=(),
            )
            harness = Harness(
                store=store,
                registry=ToolRegistry([spec]),
                provider=MockProvider(),
                world=None,
                limits=BudgetLimits(10, 10, 10_000, 10_000),
                receipts=ReceiptStore(store),
                verifier=lambda obs: {"verified": True, "verification": "t", "result": obs.result},
            )
            task = harness.create(
                task_id="bridge-2",
                objective="plain delegation",
                principal="test",
                binding={"scenario": "general-research", "spec": {"mode": "script"}},
                allowed_tools=["probe"],
                grant={},
            )
            loop = Loop(harness, planner=None, max_rounds=5, result_tools=())
            memory = loop.restore(task)
            self.assertIsNone(memory.conversation)
            self.assertNotIn("conversation", memory.build(1, {}))


if __name__ == "__main__":
    unittest.main()

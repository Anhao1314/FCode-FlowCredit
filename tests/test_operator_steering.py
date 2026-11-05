"""Operator steering: a mid-run note reaches the model, not just the chain.

The console's composer promises "the message is queued and handed to the agent
next round". Before this, the text was only written to the hash chain — the
provider never saw it. These tests pin the channel end to end, and pin the
byte-safety of the runs that have no notes (the finance v10 prompt gate).
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import verify_observation  # noqa: E402
from flowagent.memory.context import WorkingMemory  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.control import RoundControl, ToolControl  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


class RecordingProvider:
    """Proposes the same read-only action every round and keeps every context.

    Mirroring MockProvider's shape exactly (a fixed step, no todo binding) is
    the point: the run keeps asking until the round limit, so there are as many
    contexts to inspect as rounds were paid for.
    """

    name = "recording-test"
    model = "recording-test-v1"

    def __init__(self, tool: str = "list_documents") -> None:
        self.tool = tool
        self.contexts: list[dict] = []

    def propose(self, context):
        self.contexts.append(context)
        return Proposal(
            plan={
                "version": PLAN_VERSION,
                "steps": [{"id": "s1", "tool": self.tool, "arguments": {}}],
            },
            provider=self.name,
            model=self.model,
            tokens=10,
            cost_microusd=0,
            todo_id=None,
        )


class QueueingControl:
    """Stands in for the console's RunControl: records a note at one boundary.

    Only the queueing behaviour matters here — the pre-execution gate says yes
    to everything, so the run reaches the round that should carry the note.
    """

    def __init__(self, note: str, *, at_round: int = 1) -> None:
        self.note = note
        self.at_round = at_round
        self.applied = 0
        self.harness = None

    def before_round(self, *, round_no: int, task, plan):
        if round_no == self.at_round and self.applied == 0:
            self.applied += 1
            task = self.harness.record(task, "operator_message", {
                "round": round_no, "text": self.note, "by": "operator",
            })
        return RoundControl(task=task, plan=plan)

    def before_tool(self, *, round_no: int, task, step, decision):
        return ToolControl(task=task, allowed=True)


class OperatorNoteTests(unittest.TestCase):
    """The loop reads notes from the chain; the context carries them."""

    def build_run(self, *, note: str | None, at_round: int = 1):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scenario = get_scenario("full")
        store = TaskStore(pathlib.Path(tmp.name) / "task.sqlite")
        world = FinanceWorld()
        provider = RecordingProvider()
        harness = Harness(
            store=store,
            registry=ToolRegistry(build_finance_tools(world)),
            provider=provider,
            world=world,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
            verifier=lambda observation: verify_observation(world, observation),
        )
        task = harness.create(
            task_id="task-notes",
            objective=scenario.objective,
            principal="test",
            binding={"module": "finance-research"},
            allowed_tools=list(scenario.allowed_tools),
            grant=dict(scenario.grant),
        )
        control = None
        if note is not None:
            control = QueueingControl(note, at_round=at_round)
            control.harness = harness
        loop = Loop(
            harness,
            planner=FinancePlanner(),
            max_rounds=4,
            result_tools=(),
            control=control,
        )
        return loop, provider, store, task

    def test_a_note_queued_before_the_first_round_is_in_the_first_prompt(self) -> None:
        loop, provider, store, task = self.build_run(note="只查 FY2025，不要碰 FY2024", at_round=1)
        loop.run(task.id)
        self.assertGreaterEqual(len(provider.contexts), 1)
        self.assertEqual(provider.contexts[0]["operator_messages"], [
            {"round": 1, "text": "只查 FY2025，不要碰 FY2024"},
        ])
        # And it is chain truth: the note is an event like any other.
        kinds = [event["type"] for event in store.events(task.id)]
        self.assertEqual(kinds.count("operator_message"), 1)

    def test_a_note_queued_mid_run_reaches_the_next_prompt(self) -> None:
        """The composer's promise, precisely: a note that arrives while round 1
        is running is applied at that round's boundary and is in round 2's
        prompt — not in the prompt that was already sent."""
        loop, provider, store, task = self.build_run(note="顺便看一下 FY2024 的重述", at_round=2)
        loop.run(task.id)
        self.assertGreaterEqual(len(provider.contexts), 2)
        self.assertNotIn("operator_messages", provider.contexts[0])
        self.assertEqual(provider.contexts[1]["operator_messages"], [
            {"round": 2, "text": "顺便看一下 FY2024 的重述"},
        ])

    def test_the_note_keeps_riding_later_rounds(self) -> None:
        loop, provider, store, task = self.build_run(note="改用 FY2025 口径", at_round=2)
        loop.run(task.id)
        self.assertGreaterEqual(len(provider.contexts), 3)
        for context in provider.contexts[1:]:
            self.assertEqual(context["operator_messages"][0]["text"], "改用 FY2025 口径")

    def test_a_steered_note_is_labelled_as_such(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scenario = get_scenario("full")
        store = TaskStore(pathlib.Path(tmp.name) / "task.sqlite")
        world = FinanceWorld()
        harness = Harness(
            store=store,
            registry=ToolRegistry(build_finance_tools(world)),
            provider=RecordingProvider(),
            world=world,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
            verifier=lambda observation: verify_observation(world, observation),
        )
        task = harness.create(
            task_id="task-steered",
            objective=scenario.objective,
            principal="test",
            binding={"module": "finance-research"},
            allowed_tools=list(scenario.allowed_tools),
            grant=dict(scenario.grant),
        )
        task = harness.record(task, "operator_message", {
            "round": 2, "text": "先跳过重述检查", "by": "operator", "steered": True,
        })
        loop = Loop(harness, planner=FinancePlanner(), max_rounds=2, result_tools=())
        notes = loop.operator_notes(task)
        self.assertEqual(notes, [{"round": 2, "text": "先跳过重述检查", "steered": True}])

    def test_notes_are_bounded(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scenario = get_scenario("full")
        store = TaskStore(pathlib.Path(tmp.name) / "task.sqlite")
        world = FinanceWorld()
        harness = Harness(
            store=store,
            registry=ToolRegistry(build_finance_tools(world)),
            provider=RecordingProvider(),
            world=world,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
            verifier=lambda observation: verify_observation(world, observation),
        )
        task = harness.create(
            task_id="task-many",
            objective=scenario.objective,
            principal="test",
            binding={"module": "finance-research"},
            allowed_tools=list(scenario.allowed_tools),
            grant=dict(scenario.grant),
        )
        for index in range(12):
            task = harness.record(task, "operator_message", {
                "round": index + 1, "text": f"第{index}条" + "长" * 900,
            })
        loop = Loop(harness, planner=FinancePlanner(), max_rounds=2, result_tools=())
        notes = loop.operator_notes(task)
        self.assertEqual(len(notes), Loop.OPERATOR_NOTES)
        self.assertEqual(notes[-1]["round"], 12)
        self.assertEqual(len(notes[-1]["text"]), Loop.OPERATOR_NOTE_CHARS)
        # Blank notes are not steering: they never reach the prompt.
        self.assertTrue(all(note["text"].strip() for note in notes))


class DomainInstructionTests(unittest.TestCase):
    """A domain that writes must say so: a real model obeys the instruction it
    is given, and the read-only default made one refuse its own plan and try to
    declare itself blocked (observed on a real Kimi run)."""

    def test_the_general_domain_states_what_it_can_actually_do(self) -> None:
        from flowagent.domains.general import GENERAL_DOMAIN, GENERAL_INSTRUCTION

        self.assertEqual(GENERAL_DOMAIN.instruction, GENERAL_INSTRUCTION)
        for word in ("write_file", "edit_file", "run_command", "fetch_url", "tools"):
            self.assertIn(word, GENERAL_INSTRUCTION)
        # It must not repeat the finance-only claim that made the model refuse.
        self.assertNotIn("read-only", GENERAL_INSTRUCTION)
        self.assertNotIn("financial value", GENERAL_INSTRUCTION)

    def test_the_finance_default_is_untouched(self) -> None:
        from flowagent.domains.finance import FINANCE_DOMAIN
        from flowagent.memory.context import DEFAULT_INSTRUCTION, WorkingMemory

        self.assertIsNone(FINANCE_DOMAIN.instruction)
        default = WorkingMemory(objective="t", tool_catalog=[]).build(1, {})["instruction"]
        self.assertEqual(default, DEFAULT_INSTRUCTION)
        self.assertIn("read-only", default)   # the frozen v10 wording, verbatim

    def test_a_harness_carries_the_instruction_into_the_prompt(self) -> None:
        from flowagent.memory.context import DEFAULT_INSTRUCTION

        with tempfile.TemporaryDirectory() as tmp:
            scenario = get_scenario("full")
            store = TaskStore(pathlib.Path(tmp) / "task.sqlite")
            world = FinanceWorld()
            harness = Harness(
                store=store,
                registry=ToolRegistry(build_finance_tools(world)),
                provider=RecordingProvider(),
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: verify_observation(world, observation),
                instruction="只提议一个动作；可以写工作区文件。",
            )
            task = harness.create(
                task_id="task-instruction",
                objective=scenario.objective,
                principal="test",
                binding={"module": "finance-research"},
                allowed_tools=list(scenario.allowed_tools),
                grant=dict(scenario.grant),
            )
            loop = Loop(harness, planner=FinancePlanner(), max_rounds=1, result_tools=())
            memory = loop.restore(task)
            self.assertEqual(memory.build(1, {})["instruction"], "只提议一个动作；可以写工作区文件。")
            # …and a harness without one leaves the frozen default in place.
            # The Loop captures the instruction when it is built (same as
            # state_fn), so this asks a fresh loop.
            harness.instruction = None
            plain = Loop(harness, planner=FinancePlanner(), max_rounds=1, result_tools=())
            self.assertEqual(plain.restore(task).build(1, {})["instruction"], DEFAULT_INSTRUCTION)


class ByteSafetyTests(unittest.TestCase):
    """A run without notes must produce exactly the prompt it produced before."""

    def test_without_notes_no_key_appears(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[])
        context = memory.build(1, {})
        self.assertNotIn("operator_messages", context)
        self.assertNotIn("operator_messages", json.dumps(context))

    def test_a_blank_note_does_not_add_the_key(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[], operator_messages=[
            {"round": 1, "text": "   "},
        ])
        self.assertNotIn("operator_messages", memory.build(1, {}))

    def test_a_real_note_is_sanitised(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[], operator_messages=[
            {"round": "3", "text": "x" * 900},
            {"round": 4, "text": "短的"},
        ])
        context = memory.build(1, {})
        self.assertEqual(context["operator_messages"], [
            {"round": 3, "text": "x" * 600},
            {"round": 4, "text": "短的"},
        ])

    def test_a_run_without_notes_renders_the_same_prompt_bytes(self) -> None:
        """End to end on the render side: the provider's prompt is byte-identical
        whether the field exists as None or is simply unused."""
        from flowagent.providers.prompt import render_prompt

        context = WorkingMemory(objective="目标", tool_catalog=[]).build(1, {"calls_used": 0})
        rendered = render_prompt(context)
        self.assertNotIn("operator_messages", json.dumps(rendered, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
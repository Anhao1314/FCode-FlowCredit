"""The run asks the operator: ``ask_operator`` end to end.

A general agent that cannot ask has to guess. These tests pin the governed
form: the question is a tool call (gated, budgeted, chained), the wait is the
console's, the answer arrives as the tool's observation, and a timeout is
reported as a timeout — never as an invented answer.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
VIEWER = ROOT / "viewer"
sys.path.insert(0, str(VIEWER))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.general.ask import ANSWER_CHARS, QUESTION_CHARS, ask_spec  # noqa: E402
from flowagent.tools.general.commands import CommandRunner  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.summaries import summarize_ask_operator  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def sandbox(root: str) -> WorkspaceSandbox:
    return WorkspaceSandbox(pathlib.Path(root) / "workspace")


class AskToolTests(unittest.TestCase):
    """The tool's own contract: honest answers, bounded text, no channel = no hang."""

    def test_a_deployment_without_a_channel_says_so(self) -> None:
        spec = ask_spec(None)
        result = spec.handler({"question": "用哪个日期？"})
        self.assertFalse(result["answered"])
        self.assertIn("no operator channel", result["reason"])
        self.assertEqual(result["asked"], "用哪个日期？")

    def test_the_answer_comes_back_whole(self) -> None:
        spec = ask_spec(lambda **kwargs: {"answered": True, "answer": "用周三"})
        result = spec.handler({"question": "哪一天？", "why": "目标里只写了周三之后"})
        self.assertTrue(result["answered"])
        self.assertEqual(result["answer"], "用周三")

    def test_a_timeout_is_a_timeout(self) -> None:
        spec = ask_spec(lambda **kwargs: {"answered": False, "timed_out": True})
        result = spec.handler({"question": "哪一天？"})
        self.assertFalse(result["answered"])
        self.assertTrue(result["timed_out"])
        self.assertNotIn("answer", result)

    def test_the_question_is_bounded_and_never_empty(self) -> None:
        spec = ask_spec(lambda **kwargs: {"answered": True, "answer": "ok"})
        with self.assertRaises(Exception):
            spec.handler({"question": "   "})
        with self.assertRaises(Exception):
            spec.handler({"question": "问" * (QUESTION_CHARS + 1)})

    def test_a_long_answer_is_truncated_not_refused(self) -> None:
        spec = ask_spec(lambda **kwargs: {"answered": True, "answer": "答" * 900})
        self.assertEqual(len(spec.handler({"question": "q"})["answer"]), ANSWER_CHARS)

    def test_the_summary_keeps_the_answer_verbatim(self) -> None:
        summary = summarize_ask_operator({"asked": "哪一天？", "answered": True, "answer": "用周三"})
        self.assertEqual(summary["answer"], "用周三")
        timeout = summarize_ask_operator({"asked": "q", "answered": False, "timed_out": True})
        self.assertTrue(timeout["timed_out"])
        self.assertNotIn("answer", timeout)


class AskingProvider:
    """Proposes one ``ask_operator`` call, then a write, then stops."""

    name = "asking-test"
    model = "asking-test-v1"

    def __init__(self, question: str = "用哪一天？") -> None:
        self.question = question
        self.calls = 0
        self.contexts: list[dict] = []

    def propose(self, context):
        self.contexts.append(context)
        self.calls += 1
        if self.calls == 1:
            step = {"id": "t1", "tool": "ask_operator", "arguments": {"question": self.question}}
        elif self.calls == 2:
            step = {"id": "t2", "tool": "write_file", "arguments": {"path": "note.md", "content": "记下了\n"}}
        else:
            raise AssertionError("the run asked for more rounds than the plan needs")
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": [step]},
            provider=self.name, model=self.model, tokens=10, cost_microusd=0,
        )


class RunAsksOperatorTests(unittest.TestCase):
    """The console's side: publishing, waiting, chaining both halves."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "fcode_console_runtime_ask", VIEWER / "console_runtime.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.runtime_module = module
        # RunControl is built against the real runtime surface (Plan, AgentError).
        cls.console_runtime = module.Runtime(ROOT)

    def build(self, *, timeout: float = 5.0):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        store = TaskStore(root / "task.sqlite")
        self.addCleanup(store.close)
        world = GENERAL_DOMAIN.create_world(
            spec={"mode": "script", "scriptPath": "note.md"},
            sandbox=sandbox(tmp.name),
            command_runner=CommandRunner(sandbox(tmp.name)),
        )
        scenario = get_scenario("general-research")
        provider = AskingProvider()
        harness = Harness(
            store=store,
            registry=ToolRegistry(world.build_tools()),
            provider=provider,
            world=world,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
            verifier=lambda observation: world.verify_observation(observation),
        )
        task = harness.create(
            task_id="task-ask",
            objective="记一条笔记（日期不明）",
            principal="test",
            binding={"scenario": scenario.key, "mode": "script"},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        run = self.runtime_module.Run(
            id="run-ask", template=scenario.key, objective=task.objective,
            created="2026-10-26T00:00:00+00:00", pace=0.0, status="running",
            approval="off", task_id=task.id,
        )
        run.snapshot = None
        control = self.runtime_module.RunControl(
            run, harness, self.console_runtime, ("write_file",), timeout
        )
        run.controller = control
        world.ask_operator = control.ask
        loop = Loop(
            harness,
            planner=GENERAL_DOMAIN.make_planner(world),
            max_rounds=2,
            result_tools=(),
            control=control,
        )
        return run, control, harness, loop, world, provider

    def test_the_pending_question_is_published_and_the_answer_lands_in_the_chain(self) -> None:
        run, control, harness, loop, world, provider = self.build()
        answered = []
        thread = threading.Thread(target=lambda: harness.store and loop.run("task-ask"), daemon=True)
        thread.start()
        # Wait for the question to be published, then answer it like the API does.
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = control.pending()
            if pending and pending.get("kind") == "question":
                answered.append(pending)
                self.assertTrue(control.answer(pending["id"], "用周三"))
                break
            time.sleep(0.02)
        self.assertTrue(answered, "the question was never published")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "the loop did not resume after the answer")
        pending = answered[0]
        self.assertEqual(pending["question"], "用哪一天？")
        self.assertEqual(pending["kind"], "question")

        events = harness.store.events("task-ask")
        started = next(e for e in events if e["type"] == "tool_started")
        observation = next(e for e in events if e["type"] == "tool_succeeded")
        # Both halves are chain facts: the question is the call's argument, the
        # answer is its result. One tool call is the unit an auditor reads.
        self.assertEqual(started["payload"]["detail"]["arguments"]["question"], "用哪一天？")
        self.assertEqual(observation["payload"]["detail"]["tool"], "ask_operator")
        self.assertEqual(observation["payload"]["detail"]["result"]["answer"], "用周三")
        # …and it is in the very next prompt, not merely in the log.
        self.assertEqual(len(provider.contexts), 2)
        self.assertNotIn("用周三", json.dumps(provider.contexts[0], ensure_ascii=False))
        self.assertIn("用周三", json.dumps(provider.contexts[1], ensure_ascii=False))

    def test_answering_the_wrong_question_is_refused(self) -> None:
        run, control, harness, loop, world, provider = self.build()
        thread = threading.Thread(target=lambda: loop.run("task-ask"), daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = control.pending()
            if pending and pending.get("kind") == "question":
                break
            time.sleep(0.02)
        self.assertFalse(control.answer("ask-nope", "无关"))
        self.assertFalse(control.decide(pending["id"], allow=True))
        self.assertTrue(control.answer(pending["id"], "用周三"))
        thread.join(timeout=10)

    def test_a_skipped_question_leaves_the_model_on_its_own(self) -> None:
        run, control, harness, loop, world, provider = self.build()
        thread = threading.Thread(target=lambda: loop.run("task-ask"), daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = control.pending()
            if pending and pending.get("kind") == "question":
                self.assertTrue(control.skip_question(pending["id"]))
                break
            time.sleep(0.02)
        thread.join(timeout=10)
        events = harness.store.events("task-ask")
        observation = next(e for e in events if e["type"] == "tool_succeeded")
        self.assertFalse(observation["payload"]["detail"]["result"]["answered"])
        self.assertIn("reason", observation["payload"]["detail"]["result"])

    def test_a_timeout_is_reported_as_one(self) -> None:
        run, control, harness, loop, world, provider = self.build(timeout=0.4)
        loop.run("task-ask")   # nobody answers: the wait expires
        events = harness.store.events("task-ask")
        observation = next(e for e in events if e["type"] == "tool_succeeded")
        self.assertTrue(observation["payload"]["detail"]["result"]["timed_out"])
        self.assertNotIn("answer", observation["payload"]["detail"]["result"])

    def test_the_question_costs_a_read_and_is_not_approval_gated(self) -> None:
        """It is a tool: budgeted and chained. But asking is not consequential —
        the gate must not stop the run to ask permission to ask."""
        run, control, harness, loop, world, provider = self.build(timeout=0.3)
        task = loop.run("task-ask")
        kinds = [event["type"] for event in harness.store.events(task.id)]
        self.assertIn("tool_started", kinds)
        self.assertNotIn("approval_requested", kinds)
        budget = harness.budget(harness.store.load(task.id)).snapshot()
        self.assertGreaterEqual(budget["reads_used"], 1)

    def test_the_question_round_is_a_plan_step_like_any_other(self) -> None:
        """The model proposes it like any other tool, so the plan carries it."""
        run, control, harness, loop, world, provider = self.build(timeout=0.2)
        task = loop.run("task-ask")
        plans = [
            event["payload"]["detail"]["plan"]["steps"][0]["tool"]
            for event in harness.store.events(task.id)
            if event["type"] == "plan_validated"
        ]
        self.assertEqual(plans[0], "ask_operator")
        self.assertIn("write_file", plans)


if __name__ == "__main__":
    unittest.main()
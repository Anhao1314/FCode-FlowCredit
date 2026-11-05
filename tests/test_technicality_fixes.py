"""Two ways a run used to die on a technicality, both now bounded.

* A model that writes a **better** artifact than the plan composed failed the
  run as RESULT_INCOMPLETE: the result todo was matched by every argument it
  named, body included. Binding is by identity now — the file is verified by
  hash, and the body is not what makes an observation *the* result.
* A proposal that did not fit the tool's schema (an empty ``new_text``, say)
  ended the whole run as TOOL_ARGUMENTS_INVALID. Nothing had been dispatched
  and nothing was unknown, so the error is handed back: one visible
  ``proposal_rejected`` event, one more model call, bounded, and the run still
  ends denied when the model cannot fix it.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION, AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import MAX_PROPOSAL_REPAIRS, REPAIRABLE_DENIALS, Loop  # noqa: E402
from flowagent.tools.general.commands import CommandRunner  # noqa: E402
from flowagent.tools.general.planner import GeneralPlanner  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402

DRAFT = "print('first draft')\n"


class ScriptedProvider:
    """Proposes a fixed sequence of steps; keeps every context it was given."""

    name = "technicality-test"
    model = "technicality-test-v1"

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.contexts: list[dict] = []

    def propose(self, context):
        self.contexts.append(context)
        step = self.steps.pop(0) if self.steps else None
        if step is None:
            raise AgentError("MODEL_FAILED", "the scripted provider ran out of steps")
        return Proposal(
            plan={"version": PLAN_VERSION, "steps": [step]},
            provider=self.name, model=self.model, tokens=10, cost_microusd=0,
        )


class HarnessFixture(unittest.TestCase):
    def run_script(self, steps: list[dict], *, script_path: str = "script.py", max_rounds: int = 8):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sandbox = WorkspaceSandbox(pathlib.Path(tmp.name) / "workspace")
        world = GENERAL_DOMAIN.create_world(
            spec={"mode": "script", "scriptPath": script_path},
            sandbox=sandbox,
            command_runner=CommandRunner(sandbox),
        )
        scenario = get_scenario("general-research")
        store = TaskStore(pathlib.Path(tmp.name) / "task.sqlite")
        self.addCleanup(store.close)
        provider = ScriptedProvider(steps)
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
            task_id="task-technicality",
            objective="写一个脚本并归档结果",
            principal="test",
            binding={"scenario": scenario.key, "mode": "script"},
            allowed_tools=list(scenario.allowed_tools),
            grant=scenario.grant,
        )
        loop = Loop(
            harness,
            planner=GeneralPlanner([], mode="script", script_path=script_path, report_path="results.md"),
            max_rounds=max_rounds,
            result_tools=GENERAL_DOMAIN.result_tools,
        )
        task = loop.run(task.id)
        return task, harness, sandbox, provider

    @staticmethod
    def blueprint(*, script_path: str = "script.py", results_body: str | None = None,
                  results_path: str = "results.md") -> list[dict]:
        """The script blueprint's five steps, with the result body chosen by the case."""
        return [
            {"id": "t1", "tool": "write_file",
             "arguments": {"path": script_path, "content": DRAFT}},
            {"id": "t2", "tool": "run_command",
             "arguments": {"command": "python3", "args": [script_path]}},
            {"id": "t3", "tool": "edit_file",
             "arguments": {"path": script_path, "old_text": "print('first draft')",
                           "new_text": "print('fixed')"}},
            {"id": "t4", "tool": "run_command",
             "arguments": {"command": "python3", "args": [script_path]}},
            {"id": "t5", "tool": "write_file",
             "arguments": {"path": results_path,
                           "content": results_body if results_body is not None else "# 结果\n\n计划里的正文。\n"}},
        ]


class BetterArtifactTests(HarnessFixture):
    """A model's own, better body must not fail the run."""

    def test_a_different_body_for_the_result_still_binds(self) -> None:
        # The plan composed its own results.md body; the model writes a better
        # one. The run must accept it: the file is what the todo names, and its
        # hash is what the verifier re-derives.
        task, harness, sandbox, provider = self.run_script(
            self.blueprint(results_body="# 更好的结论\n\n模型自己写的，比计划里的更完整。\n")
        )
        self.assertEqual(task.status, "succeeded", task.failure)
        results = sandbox.read("results.md")["content"]
        self.assertIn("更好的结论", results)

    def test_a_write_to_another_path_is_not_the_result(self) -> None:
        """Identity is still enforced: the *file* is what binds, not the tool."""
        steps = self.blueprint(results_path="somewhere-else.md", results_body="错的产物\n")
        task, harness, sandbox, provider = self.run_script(steps, max_rounds=8)
        self.assertNotEqual(task.status, "succeeded")
        # The wrong path satisfied no result todo, so the run could not finish
        # with evidence — and it did not pretend to.
        self.assertFalse((sandbox.root / "results.md").exists())

    def test_the_identity_rule_survives_a_replayed_plan(self) -> None:
        """The matcher reads the tool's declared params, so a domain that
        declares none keeps the strict comparison (nothing silently loosens)."""
        from flowagent.tools.base import Param, ToolSpec, EFFECT_WRITE

        strict = ToolSpec(
            name="strict_write", summary="s", effect=EFFECT_WRITE,
            params=(Param(name="path", kind="string"), Param(name="content", kind="string")),
            handler=lambda arguments: arguments, outputs=(),
        )
        loose = ToolSpec(
            name="loose_write", summary="s", effect=EFFECT_WRITE,
            params=(Param(name="path", kind="string"), Param(name="content", kind="string", identity=False)),
            handler=lambda arguments: arguments, outputs=(),
        )
        scenario = get_scenario("general-research")
        store = TaskStore(pathlib.Path(tempfile.mkdtemp()) / "t.sqlite")
        self.addCleanup(store.close)
        harness = Harness(
            store=store,
            registry=ToolRegistry([strict, loose]),
            provider=ScriptedProvider([]),
            world=None,
            limits=scenario.limits,
            receipts=ReceiptStore(store),
        )
        loop = Loop(harness, planner=None, max_rounds=1, result_tools=("strict_write", "loose_write"))
        from flowagent.tools.base import Observation

        todo = type("T", (), {"tool": "loose_write", "arguments": {"path": "a.md", "content": "planned"}})()
        observation = Observation(tool="loose_write", arguments={"path": "a.md", "content": "better"}, result={}, result_hash="x")
        self.assertTrue(loop._same_result_step(todo, observation))
        strict_todo = type("T", (), {"tool": "strict_write", "arguments": {"path": "a.md", "content": "planned"}})()
        strict_observation = Observation(tool="strict_write", arguments={"path": "a.md", "content": "better"}, result={}, result_hash="x")
        self.assertFalse(loop._same_result_step(strict_todo, strict_observation))


class RepairRoundTests(HarnessFixture):
    """A schema-invalid proposal gets one visible, bounded chance to be fixed."""

    def test_a_bad_proposal_is_rejected_visibly_and_repaired(self) -> None:
        steps = self.blueprint(results_body="# 结果\n\n修好了。\n")
        # Splice the invalid attempt in where the valid edit belongs: new_text
        # is empty, so the gate refuses it before anything is dispatched.
        steps.insert(2, {"id": "t3", "tool": "edit_file",
                         "arguments": {"path": "script.py", "old_text": "print('first draft')",
                                       "new_text": ""}})
        task, harness, sandbox, provider = self.run_script(steps, max_rounds=8)
        self.assertEqual(task.status, "succeeded", task.failure)
        events = harness.store.events(task.id)
        rejected = [event for event in events if event["type"] == "proposal_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["payload"]["detail"]["code"], "TOOL_ARGUMENTS_INVALID")
        self.assertIn("new_text", rejected[0]["payload"]["detail"]["message"])
        # The error reached the next proposal's context — that is what makes it
        # a repair rather than a retry.
        after = provider.contexts[-1]
        self.assertTrue(
            any("rejected_proposals" in context for context in provider.contexts),
            "the rejection never reached a later context",
        )
        self.assertIn("fixed", sandbox.read("script.py")["content"])
        # The rejected attempt was not charged a read and left no tool event.
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds.count("tool_started"), 5)   # the blueprint's five steps
        self.assertNotIn("tool_denied", kinds)

    def test_repairs_are_bounded_and_the_run_still_ends_denied(self) -> None:
        invalid = {"id": "t3", "tool": "edit_file",
                   "arguments": {"path": "script.py", "old_text": "print('first draft')",
                                 "new_text": ""}}
        steps = [self.blueprint()[0], invalid, invalid, invalid]
        task, harness, sandbox, provider = self.run_script(steps, max_rounds=8)
        self.assertEqual(task.status, "denied")
        self.assertEqual(task.failure["code"], "TOOL_ARGUMENTS_INVALID")
        rejected = [e for e in harness.store.events(task.id) if e["type"] == "proposal_rejected"]
        self.assertEqual(len(rejected), MAX_PROPOSAL_REPAIRS)
        # One proposal that ran, then the two repairs, then the one that denied.
        self.assertEqual(len(provider.contexts), MAX_PROPOSAL_REPAIRS + 2)

    def test_a_governance_denial_is_not_repaired(self) -> None:
        """A tool the run never registered keeps its meaning: the run stops on
        the first one, exactly as the failure-injection scenarios demonstrate.
        The repair round exists for typos in a schema, not for scope."""
        task, harness, sandbox, provider = self.run_script([
            self.blueprint()[0],
            # Not a registered tool at all — the model invented a name.
            # (This case used to be spelled `report_blocked`, which counted as
            # "invented" only because the general domain failed to register it.
            # The tool IS registered now and stops the run honestly, so the
            # never-registered example has to be a name nothing registers.)
            {"id": "t9", "tool": "write_document", "arguments": {"doc_id": "doc_fy2024"}},
        ])
        self.assertEqual(task.status, "denied")
        self.assertEqual(task.failure["code"], "TOOL_NOT_REGISTERED")
        kinds = [event["type"] for event in harness.store.events(task.id)]
        self.assertNotIn("proposal_rejected", kinds)
        self.assertIn("tool_denied", kinds)
        self.assertEqual(len(provider.contexts), 2)

    def test_the_repairable_set_is_deliberately_narrow(self) -> None:
        self.assertEqual(REPAIRABLE_DENIALS, ("TOOL_ARGUMENTS_INVALID",))


if __name__ == "__main__":
    unittest.main()
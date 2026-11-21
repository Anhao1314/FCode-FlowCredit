"""Pelican capability driven through the real Work Harness execution boundary."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_run_driver import RunDriver  # noqa: E402

from flowagent.capabilities.artifacts import Action  # noqa: E402
from flowagent.capabilities.motion import build_motion_capability  # noqa: E402
from flowagent.core.contracts import AgentError, PLAN_VERSION  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.runtime.budget import BudgetLimits  # noqa: E402
from flowagent.runtime.capability import HarnessCapabilityBridge  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402

from tests.test_motion_capability import (  # noqa: E402
    fake_fail_probe,
    fake_pass_probe,
    motion_check_action,
    result_write,
    scene_fill,
    styles_fill,
    GOOD_SCENE,
)


class StageProvider:
    """Return model proposals while recording the exact stage packets."""

    def __init__(self, actions: list[Action]) -> None:
        self.actions = list(actions)
        self.contexts: list[dict] = []

    def propose(self, context: dict) -> Proposal:
        self.contexts.append(context)
        if not self.actions:
            raise AgentError("NO_TOOL_CALL", "scripted provider exhausted")
        action = self.actions.pop(0)
        return Proposal(
            provider="stage-script",
            model="offline-pelican",
            plan={
                "version": PLAN_VERSION,
                "steps": [{"id": context["state"]["stage"]["name"],
                           "tool": action.tool, "arguments": action.arguments}],
            },
            tokens=action.tokens,
            cost_microusd=action.cost_microusd,
        )


class HostProbe:
    def __init__(self, verdict) -> None:
        self.verdict = verdict
        self.calls: list[dict] = []

    def probe(self, path: str, **kwargs):
        self.calls.append({"path": path, **kwargs})
        return self.verdict(path)


class HarnessPelicanStack:
    def __init__(self, tmp: str, actions: list[Action], *, verify_script: str | None = None,
                 host_probe=None) -> None:
        self.root = pathlib.Path(tmp)
        self.sandbox = WorkspaceSandbox(self.root / "workspace")
        spec = {"fastProfile": "motion-render"}
        if verify_script is not None:
            self.sandbox.write("verify.py", verify_script)
            spec["verification"] = {"command": "python3", "args": ["verify.py"]}
        self.provider = StageProvider(actions)
        self.world = GENERAL_DOMAIN.create_world(
            spec=spec, sandbox=self.sandbox, motion_probe=HostProbe(fake_pass_probe),
        )
        registry = ToolRegistry(GENERAL_DOMAIN.build_tools(self.world))
        allowed = ["edit_file", "write_file", "motion_check", "run_command"]
        task_store = TaskStore(self.root / "tasks.db")
        self.harness = Harness(
            store=task_store,
            receipts=ReceiptStore(task_store),
            registry=registry,
            verifier=lambda observation: GENERAL_DOMAIN.verify(self.world, observation),
            state_fn=GENERAL_DOMAIN.summarize_state,
            instruction=GENERAL_DOMAIN.instruction,
            provider=self.provider,
            world=self.world,
            limits=BudgetLimits(40, 60, 2_000_000, 1_000_000),
            prompt_profile="capability-motion",
            plan_extension=False,
            verification=GENERAL_DOMAIN.make_verification(self.world),
        )
        task = self.harness.create(
            task_id="pelican-work",
            objective="draw the pelican on a bicycle",
            principal="test",
            binding={"objective": "draw the pelican on a bicycle", "instruction": "single HTML"},
            allowed_tools=allowed,
            grant=get_scenario("general-research").grant,
        )
        loop = Loop(self.harness, planner=None, max_rounds=20, result_tools=("write_file",))
        self.bridge = HarnessCapabilityBridge(
            self.harness,
            task,
            loop=loop,
            motion_probe=host_probe,
            host_verify_times_ms=(150, 450),
        )
        self.runtime = build_motion_capability(
            self.sandbox,
            self.bridge,
            probe=fake_pass_probe,
            verify=self.bridge.verify,
            executor=self.bridge.execute,
            child_run_id=f"cap-{task.id}",
        )
        self.bridge.bind(self.runtime)

    def run(self):
        result = self.runtime.run("draw the pelican on a bicycle")
        task = self.bridge.finish()
        return result, task


def successful_actions() -> list[Action]:
    actions = [scene_fill(GOOD_SCENE), styles_fill(), motion_check_action(), result_write()]
    for action in actions:
        if action.tool == "edit_file":
            action.arguments["path"] = "index.html"
    return actions


class WorkHarnessCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pelican_success_uses_harness_events_and_bounded_stage_packets(self) -> None:
        stack = HarnessPelicanStack(self._tmp.name, successful_actions())
        result, task = stack.run()
        self.assertTrue(result.succeeded, result.to_dict())
        self.assertEqual(task.model.attempts, 4)
        self.assertTrue((stack.root / "workspace" / "FCODE_RESULT.md").is_file())
        self.assertEqual(
            [context["prompt_profile"] for context in stack.provider.contexts],
            ["capability-motion"] * 4,
        )
        self.assertIn("SECTION: scene", stack.provider.contexts[0]["state"]["artifact"]["content"])
        self.assertEqual(
            [context["state"]["stage"]["name"] for context in stack.provider.contexts],
            ["draft", "structure", "motion", "delivery"],
        )
        tool_events = [event for event in stack.harness.store.events(task.id)
                       if event["type"] == "tool_succeeded"]
        self.assertEqual(len(tool_events), 4)
        self.assertTrue(all(event["payload"]["detail"]["round"] for event in tool_events))
        self.assertEqual(stack.harness.budget(task).snapshot()["tokens_used"], 2400)

    def test_all_denied_actions_can_settle_without_any_tool_execution(self) -> None:
        oversized = Action("edit_file", {
            "path": "index.html", "old_text": "<!-- SECTION: scene -->",
            "new_text": "x" * 3_001,
        })
        with tempfile.TemporaryDirectory() as tmp:
            stack = HarnessPelicanStack(tmp, [oversized, oversized])
            result, task = stack.run()
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "NO_TOOL_CALL")
            self.assertIn(task.status, ("running", "recovery_required"))
            task = stack.harness.finalize(
                task, "failed", failure={"code": result.reason}, event="task_failed",
            )
            self.assertEqual(task.status, "failed")
            self.assertFalse(any(event["type"] == "tool_started"
                                 for event in stack.harness.store.events(task.id)))

    def test_oversized_edit_is_a_retryable_denial_not_a_tool_side_effect(self) -> None:
        oversized = Action("edit_file", {
            "path": "index.html",
            "old_text": "<!-- SECTION: scene -->",
            "new_text": "x" * 3_001,
        })
        with tempfile.TemporaryDirectory() as tmp:
            stack = HarnessPelicanStack(tmp, [oversized, *successful_actions()])
            result, task = stack.run()
            self.assertEqual(result.status, "succeeded")
            self.assertEqual(task.model.attempts, 5)
            retry_packet = stack.provider.contexts[1]["state"]
            issues = retry_packet["last_check"]["checks"][0]["issues"]
            self.assertIn("3000-character", issues[0])
            started = [event for event in stack.harness.store.events(task.id)
                       if event["type"] == "tool_started"]
            self.assertEqual(len(started), 4, "the denied oversized edit never reaches the tool")

    def test_two_oversized_edits_are_repaired_before_the_draft_advances(self) -> None:
        oversized = Action("edit_file", {
            "path": "index.html",
            "old_text": "<!-- SECTION: scene -->",
            "new_text": "x" * 3_001,
        })
        actions = [oversized, oversized, *successful_actions()]
        with tempfile.TemporaryDirectory() as tmp:
            stack = HarnessPelicanStack(tmp, actions)
            result, task = stack.run()
            self.assertTrue(result.succeeded, result.to_dict())
            self.assertEqual(result.to_dict()["budget"]["repairs"], 2)
            self.assertGreaterEqual(result.to_dict()["budget"]["valid_actions"], 2)
            denied = [item for item in result.journal if item["event"] == "action_denied"]
            self.assertEqual(len(denied), 2)
            self.assertTrue(all(item["failure_code"] == "ACTION_REJECTED" for item in denied))
            self.assertEqual(stack.provider.contexts[2]["state"]["repair"]["last_ticket"]["code"],
                             "EDIT_TOO_LARGE")
            self.assertEqual(
                [context["state"]["stage"]["name"] for context in stack.provider.contexts][:4],
                ["draft", "draft", "draft", "structure"],
            )

    def test_missing_anchor_after_scene_fill_is_a_repair_not_a_stage_failure(self) -> None:
        scene = scene_fill(GOOD_SCENE)
        scene.arguments["path"] = "index.html"
        missing_anchor = Action("edit_file", {
            "path": "index.html",
            "old_text": "<!-- SECTION: scene -->",
            "new_text": "",
        })
        rest = successful_actions()[1:]
        with tempfile.TemporaryDirectory() as tmp:
            stack = HarnessPelicanStack(tmp, [scene, missing_anchor, *rest])
            result, task = stack.run()
            self.assertTrue(result.succeeded, result.to_dict())
            self.assertEqual(result.to_dict()["budget"]["repairs"], 1)
            failed = [item for item in result.journal if item["event"] == "action_failed"]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["failure_code"], "INVALID_TOOL_CALL")
            self.assertEqual(failed[0]["repair_ticket"]["code"], "TOOL_ARGUMENTS_INVALID")
            self.assertEqual(
                stack.provider.contexts[2]["state"]["repair"]["last_ticket"]["code"],
                "TOOL_ARGUMENTS_INVALID",
            )
            self.assertEqual(
                [context["state"]["stage"]["name"] for context in stack.provider.contexts][:3],
                ["draft", "structure", "structure"],
            )

    def test_real_tool_failure_becomes_a_typed_stage_failure(self) -> None:
        actions = successful_actions()
        actions[1].arguments["old_text"] = "this anchor does not exist"
        with tempfile.TemporaryDirectory() as tmp:
            stack = HarnessPelicanStack(tmp, actions)
            result, _task = stack.run()
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.stage, "structure")
            self.assertNotEqual(result.reason, "PROPOSER_EXHAUSTED")
            events = stack.harness.store.events(stack.bridge.task.id)
            self.assertTrue(any(event["type"] == "tool_failed" for event in events))

    def test_final_host_probe_failure_cannot_be_a_typed_success(self) -> None:
        stack = HarnessPelicanStack(
            self._tmp.name,
            successful_actions(),
            host_probe=HostProbe(fake_fail_probe),
        )
        result, task = stack.run()
        self.assertFalse(result.succeeded)
        self.assertEqual(result.stage, "delivery")
        self.assertEqual(result.reason, "ARTIFACT_CHECK_FAILED")
        self.assertEqual(result.legacy_reason, "DELIVERY_BLOCKED")
        self.assertEqual(len(stack.bridge.motion_probe.calls), 1)
        self.assertEqual(stack.bridge.motion_probe.calls[0]["times"], (150, 450))
        self.assertEqual(result.quality.get("status"), "unverified")
        self.assertNotEqual(task.status, "succeeded")

    def test_declared_verifier_runs_inside_the_event_chain_before_success(self) -> None:
        stack = HarnessPelicanStack(
            self._tmp.name,
            successful_actions(),
            verify_script="from pathlib import Path\nraise SystemExit(0 if Path('FCODE_RESULT.md').exists() else 2)\n",
        )
        result, task = stack.run()
        self.assertTrue(result.succeeded, result.to_dict())
        events = stack.harness.store.events(task.id)
        obligations = [event for event in events if event["type"] == "verification_obligation"]
        verdicts = [event for event in events if event["type"] == "verification_result"]
        self.assertEqual(len(obligations), 1)
        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0]["payload"]["detail"]["verified"])
        self.assertTrue(any(event["type"] == "tool_succeeded" and
                            event["payload"]["detail"]["tool"] == "run_command"
                            for event in events))

    def test_declared_verifier_failure_is_not_capability_success(self) -> None:
        stack = HarnessPelicanStack(
            self._tmp.name,
            successful_actions(),
            verify_script="raise SystemExit(2)\n",
        )
        result, task = stack.run()
        self.assertFalse(result.succeeded)
        self.assertEqual(result.stage, "delivery")
        self.assertEqual(result.reason, "ARTIFACT_CHECK_FAILED")
        self.assertEqual(result.legacy_reason, "DELIVERY_BLOCKED")
        verdicts = [event for event in stack.harness.store.events(task.id)
                    if event["type"] == "verification_result"]
        self.assertEqual(len(verdicts), 1)
        self.assertFalse(verdicts[0]["payload"]["detail"]["verified"])
        self.assertNotEqual(task.status, "succeeded")


class NoOpObserver:
    def round_start(self, *_args):
        return None

    def proposal(self, *_args):
        return None

    def gate(self, *_args):
        return None

    def observation(self, *_args):
        return None

    def round_end(self, *_args):
        return None

    def terminal(self, *_args):
        return None


class ConsoleRunDriverCapabilityTests(unittest.TestCase):
    def test_real_driver_entry_returns_a_succeeded_task_with_capability_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stack = HarnessPelicanStack(tmp, successful_actions())
            driver = RunDriver.__new__(RunDriver)
            driver.rt = SimpleNamespace(Loop=Loop)
            driver.run = SimpleNamespace(
                id="console-pelican",
                objective="draw the pelican on a bicycle",
                spec={"fastProfile": "motion-render", "fast": True,
                      "capabilityRig": False},
            )
            driver.general = True
            driver.fast = True
            driver.motion_probe = HostProbe(fake_pass_probe)
            driver.planner = None
            driver.max_rounds = 20
            driver.observer = NoOpObserver()
            driver.control = None
            driver.harness = stack.harness
            driver.sandbox = stack.sandbox
            driver.module = SimpleNamespace(result_tools=("write_file",), instruction="build")
            driver.manager = SimpleNamespace(chat=None)
            driver.run_dir = stack.root
            driver.capability_result = None
            driver.capability_quality = None
            final = driver._execute_capability(stack.bridge.task)
            self.assertEqual(final.status, "succeeded")
            self.assertIsNotNone(driver.capability_result)
            self.assertEqual(driver.capability_result.stage, "delivery")
            self.assertEqual(driver.capability_quality["status"], "pass")
            events = stack.harness.store.events(final.id)
            self.assertTrue(any(event["type"] == "capability_succeeded" for event in events))
            self.assertEqual(events[-1]["type"], "task_succeeded")


class CapabilityBridgeStateTests(unittest.TestCase):
    def test_finish_reloads_the_durable_task_after_a_late_sequence_write(self) -> None:
        class _Store:
            def load(self, task_id: str):
                return SimpleNamespace(id=task_id, sequence=28, latest=True)

        bridge = HarnessCapabilityBridge(
            SimpleNamespace(store=_Store()),
            SimpleNamespace(id="pelican-work", sequence=27, latest=False, allowed_tools=()),
        )
        finished = bridge.finish()
        self.assertEqual(finished.sequence, 28)
        self.assertTrue(finished.latest)


if __name__ == "__main__":
    unittest.main()

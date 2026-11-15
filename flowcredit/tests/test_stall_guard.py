"""Motion runs stop a read-only stall instead of burning the whole budget."""

from __future__ import annotations

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
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.base import Proposal  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _read_proposal(path: str = "note.txt") -> Proposal:
    return Proposal(
        plan={
            "version": PLAN_VERSION,
            "steps": [{
                "id": "read-again",
                "tool": "read_file_raw",
                "arguments": {"path": path},
            }],
        },
        provider="scripted",
        model="scripted-v1",
        tokens=10,
        cost_microusd=1,
    )


class ReadOnlyProvider:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self) -> None:
        self.calls = 0
        self.contexts = []

    def propose(self, context):
        self.calls += 1
        self.contexts.append(context)
        return _read_proposal()


class WriteThenReadProvider(ReadOnlyProvider):
    """Complete one write, then leave a different write todo pending."""

    def propose(self, context):
        self.calls += 1
        self.contexts.append(context)
        if self.calls > 1:
            return _read_proposal()
        return Proposal(
            plan={
                "version": PLAN_VERSION,
                "steps": [{
                    "id": "first-write",
                    "tool": "write_file",
                    "arguments": {"path": "note.txt", "content": "first\n"},
                }],
                "additions": [{
                    "id": "second-write",
                    "text": "write the second artifact",
                    "tool": "write_file",
                    "done_when": "observed:second-write",
                    "arguments": {"path": "second.txt", "content": "second\n"},
                }],
            },
            provider="scripted",
            model="scripted-v1",
            tokens=10,
            cost_microusd=1,
        )


class StallGuardTests(unittest.TestCase):
    def test_a_second_write_todo_cannot_hide_behind_the_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            scenario = get_scenario("general-research")
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"}, sandbox=WorkspaceSandbox(root / "ws"),
            )
            world.sandbox.ensure()
            store = TaskStore(root / "task.sqlite")
            provider = WriteThenReadProvider()
            harness = Harness(
                store=store,
                registry=ToolRegistry(world.build_tools()),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: world.verify_observation(observation),
                prompt_profile="motion-render",
                plan_extension=True,
            )
            task = harness.create(
                task_id="task-second-write-stalled",
                objective="write the artifact",
                principal="test",
                binding={"scenario": scenario.key},
                allowed_tools=list(scenario.allowed_tools),
                grant=dict(scenario.grant),
            )
            final = Loop(
                harness,
                planner=GENERAL_DOMAIN.make_planner(world),
                max_rounds=8,
                result_tools=GENERAL_DOMAIN.result_tools,
            ).run(task.id)
            self.assertEqual(final.status, "failed")
            self.assertEqual(final.failure["code"], "NO_PROGRESS")
            self.assertEqual(provider.calls, 4, "one write plus three read-only rounds")
            self.assertFalse((root / "ws" / "second.txt").exists())
            writes = [event for event in store.events(final.id)
                      if event["type"] == "tool_succeeded" and
                      event["payload"]["detail"]["tool"] == "write_file"]
            self.assertEqual(len(writes), 1)
            store.close()

    def test_motion_profile_stops_after_repeated_read_only_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            scenario = get_scenario("general-research")
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"}, sandbox=WorkspaceSandbox(root / "ws"),
            )
            world.sandbox.ensure()
            world.sandbox.write("note.txt", "hello\n")
            store = TaskStore(root / "task.sqlite")
            provider = ReadOnlyProvider()
            harness = Harness(
                store=store,
                registry=ToolRegistry(world.build_tools()),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: world.verify_observation(observation),
                prompt_profile="motion-render",
                plan_extension=True,
            )
            task = harness.create(
                task_id="task-stalled",
                objective="write the artifact",
                principal="test",
                binding={"scenario": scenario.key},
                allowed_tools=list(scenario.allowed_tools),
                grant=dict(scenario.grant),
            )
            final = Loop(
                harness,
                planner=GENERAL_DOMAIN.make_planner(world),
                max_rounds=8,
                result_tools=GENERAL_DOMAIN.result_tools,
            ).run(task.id)
            self.assertEqual(final.status, "failed")
            self.assertEqual(final.failure["code"], "NO_PROGRESS")
            self.assertEqual(provider.calls, 3, "the guard stops before a fourth paid read")
            self.assertTrue(any("progress_guard" in context for context in provider.contexts))
            events = store.events(final.id)
            self.assertTrue(any(event["type"] == "task_failed" for event in events))
            self.assertFalse(any(event["type"] == "tool_succeeded" and
                                 event["payload"]["detail"]["tool"] == "write_file"
                                 for event in events))
            store.close()

    def test_a_profile_without_the_guard_keeps_the_old_behavior(self) -> None:
        # The guard is profile data, not a kernel default: a general run with
        # the same read-only provider must not gain a surprise terminal.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            scenario = get_scenario("general-research")
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"}, sandbox=WorkspaceSandbox(root / "ws"),
            )
            world.sandbox.ensure()
            world.sandbox.write("note.txt", "hello\n")
            store = TaskStore(root / "task.sqlite")
            provider = ReadOnlyProvider()
            harness = Harness(
                store=store,
                registry=ToolRegistry(world.build_tools()),
                provider=provider,
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: world.verify_observation(observation),
                prompt_profile="general-fast",
                plan_extension=True,
            )
            task = harness.create(
                task_id="task-not-stalled",
                objective="write the artifact",
                principal="test",
                binding={"scenario": scenario.key},
                allowed_tools=list(scenario.allowed_tools),
                grant=dict(scenario.grant),
            )
            final = Loop(
                harness,
                planner=GENERAL_DOMAIN.make_planner(world),
                max_rounds=6,
                result_tools=GENERAL_DOMAIN.result_tools,
            ).run(task.id)
            self.assertNotEqual((final.failure or {}).get("code"), "NO_PROGRESS")
            self.assertEqual(provider.calls, 6, "without the profile flag the loop is unchanged")
            store.close()


if __name__ == "__main__":
    unittest.main()

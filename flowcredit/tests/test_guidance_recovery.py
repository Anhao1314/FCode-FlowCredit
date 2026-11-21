"""Host-guidance tool errors are recoverable, bounded, and never silent."""

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
from flowagent.runtime.loop import Loop, MAX_PROPOSAL_REPAIRS, RECOVERABLE_TOOL_ERRORS  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def _proposal(tool: str, arguments: dict) -> Proposal:
    return Proposal(
        plan={"version": PLAN_VERSION, "steps": [{
            "id": "step", "tool": tool, "arguments": arguments,
        }]},
        provider="scripted", model="scripted-v1", tokens=10, cost_microusd=1,
    )


class _GuidanceProvider:
    name = "scripted"
    model = "scripted-v1"

    def __init__(self) -> None:
        self.calls = 0

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal("write_file", {"path": "index.html", "content": "bad"})
        if self.calls == 2:
            return _proposal("edit_file", {
                "path": "index.html",
                "old_text": "/* SECTION: styles */",
                "new_text": ".robot { fill: #fff; }",
            })
        return _proposal("write_file", {
            "path": "FCODE_RESULT.md", "content": "# done\n",
        })


class _AlwaysBadProvider(_GuidanceProvider):
    def propose(self, context):
        self.calls += 1
        return _proposal("write_file", {"path": "index.html", "content": "bad"})


class _SandboxPathRecoveryProvider(_GuidanceProvider):
    """Round 1 probes a repo-root-mindset path (sandbox root IS the project
    root, so ``viewer/...`` does not exist); the run must hand the denial
    back to the model instead of dying on it."""

    def propose(self, context):
        self.calls += 1
        if self.calls == 1:
            return _proposal("repo_map", {"max_files": 10, "path": "viewer"})
        return _proposal("write_file", {"path": "FCODE_RESULT.md", "content": "# done\n"})


def _run(provider, tmp: pathlib.Path):
    scenario = get_scenario("general-research")
    world = GENERAL_DOMAIN.create_world(
        spec={"mode": "open", "fastProfile": "motion-render"},
        sandbox=WorkspaceSandbox(tmp / "ws"),
    )
    store = TaskStore(tmp / "task.sqlite")
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
        task_id="task-guidance",
        objective="fill the motion scaffold",
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
    return final, store


class GuidanceRecoveryTests(unittest.TestCase):
    def test_guidance_codes_are_recoverable(self) -> None:
        for code in (
            "WRITE_SCAFFOLDED_USE_EDIT",
            "EDIT_TOO_LARGE",
            "FILE_EXISTS_USE_EDIT",
            "WRITE_TOO_LARGE_USE_APPEND",
            "WRITE_MODE_CONFLICT",
        ):
            self.assertIn(code, RECOVERABLE_TOOL_ERRORS)

    def test_motion_guidance_error_does_not_kill_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            final, store = _run(_GuidanceProvider(), pathlib.Path(tmp))
            events = store.events(final.id)
            failed = [event for event in events if event["type"] == "tool_failed"]
            self.assertTrue(failed)
            self.assertEqual(failed[0]["payload"]["detail"]["code"], "WRITE_SCAFFOLDED_USE_EDIT")
            self.assertNotEqual(
                (final.failure or {}).get("code"), "WRITE_SCAFFOLDED_USE_EDIT",
            )
            self.assertGreaterEqual(sum(1 for event in events if event["type"] == "model_started"), 2)
            store.close()

    def test_wrong_root_path_is_host_guidance_not_fatal(self) -> None:
        """真模型 Work run 实测：repo_map(path="viewer") 在项目沙箱里被拒。
        良性路径错位（沙箱根已是项目根）是 host-guidance——喂回模型重规划；
        而 SANDBOX_PATH_DENIED（越权/逃逸）仍保持致命，不进恢复集。"""
        with tempfile.TemporaryDirectory() as tmp:
            final, store = _run(_SandboxPathRecoveryProvider(), pathlib.Path(tmp))
            events = store.events(final.id)
            failed = [event for event in events if event["type"] == "tool_failed"]
            self.assertTrue(failed)
            self.assertEqual(failed[0]["payload"]["detail"]["code"], "TOOL_ARGUMENTS_INVALID")
            self.assertNotIn("SANDBOX_PATH_DENIED", RECOVERABLE_TOOL_ERRORS)
            self.assertGreaterEqual(sum(1 for event in events if event["type"] == "model_started"), 2)
            store.close()

    def test_guidance_repair_budget_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _AlwaysBadProvider()
            final, store = _run(provider, pathlib.Path(tmp))
            self.assertEqual(final.status, "failed")
            self.assertEqual(provider.calls, MAX_PROPOSAL_REPAIRS + 1)
            store.close()


if __name__ == "__main__":
    unittest.main()

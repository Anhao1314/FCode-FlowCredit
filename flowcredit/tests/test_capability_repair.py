"""Regression replay for the pelican control-plane failure.

The real run produced two oversized scene edits (4,210 and 3,481 characters)
and lost the stage because rejected actions consumed the only two calls. These
tests use the same shape with a scripted proposer and no browser/model calls:
the first two actions are rejected with a repair ticket, the third is a valid
chunk, and the stage must advance instead of failing with NO_DRAFT.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.capabilities.artifacts import Action, ArtifactStore  # noqa: E402
from flowagent.capabilities.checks import (  # noqa: E402
    CheckContext, CheckRegistry, CheckResult,
)
from flowagent.capabilities.manifest import (  # noqa: E402
    BudgetSpec, CapabilityManifest, CheckSpec, StageSpec,
)
from flowagent.capabilities.runtime import (  # noqa: E402
    ACTION_REPAIR_EXHAUSTED_NO_VALID,
    BUDGET_EXHAUSTED_REASONING,
    NO_TOOL_CALL,
    INVALID_TOOL_CALL,
    ActionGate,
    CapabilityRuntime,
)
from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402

SCAFFOLD = (
    '<!doctype html><html><body><svg id="scene" viewBox="0 0 900 520">'
    "<!-- SECTION: scene --></svg></body></html>"
)
VALID_SCENE = '<g class="wheel"></g>'


def scene_check(context: CheckContext, _registry: CheckRegistry) -> CheckResult:
    ok = context.store.scene_nonempty()
    return CheckResult(
        name="scene.nonempty",
        passed=ok,
        status="pass" if ok else "fail",
        score=100 if ok else 0,
        issues=() if ok else ("scene is empty",),
        required_fixes=() if ok else ("fill the scene",),
        artifact_sha256=context.artifact.sha256,
    )


def manifest(*, calls: int = 2, repairs: int = 3) -> CapabilityManifest:
    return CapabilityManifest(
        kind="motion.render",
        when="unit replay",
        input_schema={},
        output_schema={},
        stages=(
            StageSpec(
                "draft",
                ("edit_file",),
                ("scene.nonempty",),
                BudgetSpec(calls=calls, tokens=100_000, repairs=repairs),
                action_rules=("scene_edit_only",),
            ),
        ),
        checks=(CheckSpec("scene.nonempty"),),
        artifact_path="index.html",
        result_path="RESULT.md",
    )


class Scripted:
    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.packets: list[dict[str, Any]] = []

    def propose(self, packet: dict[str, Any], *, tools: tuple[str, ...]) -> Any:
        self.packets.append(packet)
        if not self.items:
            return None
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def edit(new_text: str) -> Action:
    return Action(
        "edit_file",
        {
            "path": "index.html",
            "old_text": "<!-- SECTION: scene -->",
            "new_text": new_text,
        },
    )


class RepairReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self._tmp.name) / "ws")
        self.sandbox.write("index.html", SCAFFOLD)
        self.store = ArtifactStore(self.sandbox, path="index.html", result_path="RESULT.md")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def runtime(self, proposer: Scripted, **budget: int) -> CapabilityRuntime:
        checks = CheckRegistry().register("scene.nonempty", scene_check)
        return CapabilityRuntime(
            manifest=manifest(**budget),
            store=self.store,
            checks=checks,
            proposer=proposer,
            gate=ActionGate(),
            child_run_id="cap-repair-replay",
        )

    def test_two_oversized_edits_do_not_kill_the_draft(self) -> None:
        proposer = Scripted(
            edit("x" * 4_210),
            edit("x" * 3_481),
            edit(VALID_SCENE),
        )
        result = self.runtime(proposer).run("draw the pelican on a bicycle")

        self.assertTrue(result.succeeded, result.to_dict())
        self.assertEqual(result.to_dict()["budget"]["model_calls"], 3)
        self.assertEqual(result.to_dict()["budget"]["valid_actions"], 1)
        self.assertEqual(result.to_dict()["budget"]["repairs"], 2)

        denied = [item for item in result.journal if item["event"] == "action_denied"]
        self.assertEqual([item["code"] for item in denied], ["EDIT_TOO_LARGE", "EDIT_TOO_LARGE"])
        self.assertTrue(all(item["failure_code"] == "ACTION_REJECTED" for item in denied))
        self.assertEqual(denied[0]["repair_ticket"]["remaining_repairs"], 2)
        self.assertEqual(denied[1]["repair_ticket"]["remaining_repairs"], 1)
        self.assertEqual(proposer.packets[1]["repair"]["last_ticket"]["code"], "EDIT_TOO_LARGE")
        self.assertEqual(proposer.packets[1]["repair"]["repairs_left"], 2)
        self.assertEqual(proposer.packets[2]["repair"]["repairs_left"], 1)
        self.assertEqual(
            proposer.packets[2]["repair"]["last_ticket"]["suggested_action"],
            "split_into_chunks",
        )

    def test_repair_budget_exhaustion_is_not_no_draft(self) -> None:
        proposer = Scripted(edit("x" * 4_210), edit("x" * 3_481), edit("x" * 4_000))
        result = self.runtime(proposer).run("draw the pelican on a bicycle")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.stage, "draft")
        self.assertEqual(result.reason, ACTION_REPAIR_EXHAUSTED_NO_VALID)
        self.assertEqual(result.legacy_reason, "NO_DRAFT")
        self.assertEqual(result.to_dict()["budget"]["model_calls"], 3)
        self.assertEqual(result.to_dict()["budget"]["valid_actions"], 0)
        self.assertEqual(result.to_dict()["budget"]["repairs"], 3)
        self.assertEqual(result.repair_ticket["remaining_repairs"], 0)
        self.assertFalse(self.store.scene_nonempty())

    def test_reasoning_exhaustion_is_classified_as_control_failure(self) -> None:
        proposer = Scripted(AgentError(
            "MODEL_CALL_FAILED",
            "deepseek 的补全预算被推理占满（reasoning 98788 字，content 为空）",
        ))
        result = self.runtime(proposer).run("draw the pelican")
        self.assertEqual(result.reason, BUDGET_EXHAUSTED_REASONING)
        self.assertEqual(result.legacy_reason, "")
        self.assertEqual(result.to_dict()["budget"]["valid_actions"], 0)
        self.assertEqual(result.to_dict()["budget"]["repairs"], 0)

    def test_no_tool_call_and_invalid_tool_call_are_distinct(self) -> None:
        none = self.runtime(Scripted()).run("draw")
        self.assertEqual(none.reason, NO_TOOL_CALL)

        invalid = self.runtime(Scripted(
            AgentError("PROPOSAL_MALFORMED", "native reply carried bad arguments"),
        )).run("draw")
        self.assertEqual(invalid.reason, INVALID_TOOL_CALL)


if __name__ == "__main__":
    unittest.main()

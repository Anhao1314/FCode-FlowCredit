"""Capability manifests are data: bounded, validated and JSON-safe."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.capabilities.manifest import (  # noqa: E402
    BudgetSpec,
    CapabilityManifest,
    CheckSpec,
    StageSpec,
)
from flowagent.capabilities.motion import motion_manifest  # noqa: E402


def tiny_manifest() -> CapabilityManifest:
    return CapabilityManifest(
        kind="test.tiny",
        when="unit tests",
        input_schema={"goal": "string"},
        stages=(
            StageSpec("draft", ("edit_file",), ("check.a",), BudgetSpec(calls=2, tokens=100)),
            StageSpec("delivery", ("write_file",), ("check.b",), BudgetSpec(calls=1, tokens=50), "stop"),
        ),
        checks=(CheckSpec("check.a"), CheckSpec("check.b")),
        output_schema={"ok": "boolean"},
        artifact_path="index.html",
        result_path="RESULT.md",
        initial_stage_name="draft",
    )


class _Artifact:
    def __init__(self, filled: bool) -> None:
        self.filled = filled

    def scene_nonempty(self) -> bool:
        return self.filled


class BudgetTests(unittest.TestCase):
    def test_budget_is_integer_only_and_frozen(self) -> None:
        budget = BudgetSpec(calls=2, tokens=60_000, cost_microusd=1_000)
        self.assertEqual(budget.remaining(calls=1, tokens=10, cost_microusd=0),
                         {"calls": 1, "valid_actions": 1, "repairs": 2,
                          "tokens": 59_990, "cost_microusd": 1_000})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            budget.calls = 9  # type: ignore[misc]
        for bad in (-1, 1.5, True, "2"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    BudgetSpec(calls=bad)


class ManifestShapeTests(unittest.TestCase):
    def test_stage_and_check_specs_are_frozen(self) -> None:
        stage = StageSpec("s", ("edit_file",), ("c",), BudgetSpec(1))
        check = CheckSpec("c")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            stage.name = "other"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            check.name = "other"  # type: ignore[misc]

    def test_manifest_rejects_bad_shapes(self) -> None:
        def build(**overrides):
            fields = dict(
                kind="test.bad", when="x", input_schema={}, output_schema={},
                stages=(StageSpec("s", ("edit_file",), ("c",), BudgetSpec(1)),),
                checks=(CheckSpec("c"),),
            )
            fields.update(overrides)
            return CapabilityManifest(**fields)

        with self.assertRaises(ValueError):
            build(kind="")
        with self.assertRaises(ValueError):
            build(stages=())
        with self.assertRaises(ValueError):
            build(stages=(StageSpec("s", ("edit_file",), ("c",), BudgetSpec(1)),
                          StageSpec("s", ("edit_file",), ("c",), BudgetSpec(1))))
        with self.assertRaises(ValueError):
            build(stages=(StageSpec("s", ("edit_file",), ("missing",), BudgetSpec(1)),))
        with self.assertRaises(ValueError):
            build(stages=(StageSpec("s", ("edit_file",), ("c",), BudgetSpec(1), "explode"),))
        with self.assertRaises(ValueError):
            build(stages=(StageSpec("s", ("edit_file",), ("c",), BudgetSpec(1),
                                    action_rules=("not_a_rule",)),))
        with self.assertRaises(ValueError):
            build(stages=(StageSpec("s", (), ("c",), BudgetSpec(1)),))
        with self.assertRaises(ValueError):
            build(initial_stage_name="nope")

    def test_initial_next_and_to_dict(self) -> None:
        manifest = tiny_manifest()
        self.assertEqual(manifest.initial_stage().name, "draft")
        self.assertEqual(manifest.next(manifest.stage("draft")).name, "delivery")
        self.assertIsNone(manifest.next(manifest.stage("delivery")))
        self.assertEqual(manifest.check("check.a").name, "check.a")
        with self.assertRaises(KeyError):
            manifest.stage("unknown")
        payload = manifest.to_dict()
        self.assertEqual([s["name"] for s in payload["stages"]], ["draft", "delivery"])
        self.assertEqual(payload["stages"][0]["budget"]["calls"], 2)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_prefilled_artifact_starts_at_the_later_stage(self) -> None:
        manifest = CapabilityManifest(
            kind="test.hint", when="x", input_schema={}, output_schema={},
            stages=(
                StageSpec("draft", ("edit_file",), ("c",), BudgetSpec(1)),
                StageSpec("structure", ("edit_file",), ("c",), BudgetSpec(1)),
            ),
            checks=(CheckSpec("c"),),
            prefilled_stage_name="structure",
        )
        self.assertEqual(manifest.initial_stage().name, "draft")
        self.assertEqual(manifest.initial_stage(_Artifact(False)).name, "draft")
        self.assertEqual(manifest.initial_stage(_Artifact(True)).name, "structure")


class MotionManifestTests(unittest.TestCase):
    def test_four_stages_and_their_gates(self) -> None:
        manifest = motion_manifest()
        self.assertEqual(manifest.kind, "motion.render")
        self.assertEqual([s.name for s in manifest.stages],
                         ["draft", "structure", "motion", "delivery"])
        by_name = {s.name: s for s in manifest.stages}
        self.assertIn("scene_edit_only", by_name["draft"].action_rules)
        self.assertEqual(by_name["draft"].exit_checks, ("scene.nonempty",))
        self.assertEqual(by_name["structure"].exit_checks, ("hooks.all", "static.pass"))
        self.assertEqual(by_name["motion"].exit_checks, ("probe.pass", "coverage.complete"))
        self.assertEqual(
            by_name["delivery"].exit_checks,
            ("motion_check", "verify.pass", "result.written"),
        )
        self.assertIn("motion_check_required", by_name["delivery"].action_rules)
        self.assertEqual(by_name["delivery"].on_fail, "retry")
        self.assertIn("edit_file", by_name["delivery"].allowed_tools)
        self.assertIn("motion_check", by_name["delivery"].allowed_tools)
        self.assertEqual([s.budget.calls for s in manifest.stages], [2, 4, 4, 3])
        self.assertEqual(manifest.result_path, "FCODE_RESULT.md")
        self.assertTrue(all(isinstance(s.budget.tokens, int) for s in manifest.stages))


if __name__ == "__main__":
    unittest.main()

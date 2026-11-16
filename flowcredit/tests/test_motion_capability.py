"""motion.render driven by a scripted proposer and inline fixtures.

No browser, no real model, no viewer/runs evidence. A fake probe supplies the
rendered geometry verdict; the static analyzer and the sandbox are the real
ones. These fixtures are the regression proof for the 2026-11-14 pelican run:
CSS-only edits cannot advance the draft stage, a failing static check cannot
advance structure, and no result is delivered without a motion_check pass for
the current artifact hash.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.capabilities.artifacts import Action  # noqa: E402
from flowagent.capabilities.motion import build_motion_capability  # noqa: E402
from flowagent.capabilities.runtime import STAGE_ACTION_DENIED  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402

SCENE_ANCHOR = "<!-- SECTION: scene -->"
STYLE_ANCHOR = "/* SECTION: styles */"

#: A complete rig: wheel radius 100px over a 1000ms turn is 628 px/s, the
#: scenery scrolls exactly -628px in the same period, crank/leg share 1000ms.
#: Existing ``analyze_motion`` scores this ``pass / 100``.
STYLES_FILL = """@keyframes wheel-spin { to { transform: rotate(360deg); } }
@keyframes pedal { to { transform: rotate(4deg); } }
@keyframes scenery-scroll { to { transform: translateX(-628px); } }
.wheel { animation: wheel-spin 1000ms linear infinite; }
.scenery { animation: scenery-scroll 1000ms linear infinite; }
.crank { animation: pedal 1000ms linear infinite; }
.leg { animation: pedal 1000ms linear infinite; }
""" + STYLE_ANCHOR

GOOD_SCENE = """<g class="scenery"><rect x="0" y="0" width="10" height="10"></rect></g>
<circle class="wheel" cx="350" cy="250" r="100"></circle>
<circle class="hip" cx="100" cy="100" r="6"></circle>
<line class="leg" x1="100" y1="100" x2="100" y2="200" stroke="black"></line>
<line class="leg" x1="100" y1="100" x2="120" y2="200" stroke="black"></line>
<circle class="foot" cx="100" cy="200" r="6"></circle>
<circle class="foot" cx="120" cy="200" r="6"></circle>
<circle class="pedal" cx="100" cy="200" r="6"></circle>
<circle class="pedal" cx="120" cy="200" r="6"></circle>"""

#: Same wheel/hip/leg but no .foot and no support hook: static and hook checks
#: must both fail and keep their required_fixes.
BAD_SCENE = """<g class="scenery"><rect x="0" y="0" width="10" height="10"></rect></g>
<circle class="wheel" cx="350" cy="250" r="100"></circle>
<circle class="hip" cx="100" cy="100" r="6"></circle>
<line class="leg" x1="100" y1="100" x2="100" y2="200" stroke="black"></line>"""


def scene_fill(body: str) -> Action:
    return Action("edit_file", {
        "old_text": SCENE_ANCHOR,
        "new_text": body + "\n" + SCENE_ANCHOR,
    }, tokens=1_000, cost_microusd=300, note="fill scene")


def styles_fill() -> Action:
    return Action("edit_file", {
        "old_text": STYLE_ANCHOR, "new_text": STYLES_FILL,
    }, tokens=1_000, cost_microusd=300, note="fill styles")


def css_only() -> Action:
    return Action("edit_file", {
        "old_text": "</style>", "new_text": ".wheel { color: red; }\n</style>",
    }, tokens=500, cost_microusd=100, note="pure css")


def no_op_style() -> Action:
    return Action("edit_file", {"old_text": STYLE_ANCHOR, "new_text": STYLE_ANCHOR})


def motion_check_action() -> Action:
    return Action("motion_check", {}, tokens=200, cost_microusd=50)


def result_write() -> Action:
    return Action("write_file", {
        "path": "FCODE_RESULT.md", "content": "# motion delivered\n",
    }, tokens=200, cost_microusd=50)


def identity_label_edit() -> Action:
    """Add the evaluator's identity hooks without touching motion CSS."""
    return Action("edit_file", {
        "old_text": '<g class="scenery">',
        "new_text": '<g class="pelican bicycle"></g>\n<g class="scenery">',
    }, tokens=300, cost_microusd=80)


def fake_pass_probe(_path: str = "") -> dict[str, Any]:
    """Raw frame-probe payload; merged by the existing probe_quality helper."""
    return {
        "ok": True,
        "contact": "",
        "frames": [{"time_ms": 0, "name": "f0", "sha256": "x", "width": 900, "height": 520}],
        "geometry": [{
            "time_ms": 0, "feet": 2, "pedals": 2, "legs": 2,
            "scene_box": {"x": 0, "y": 0, "w": 900, "h": 520},
            "wheel_boxes": [
                {"x": 100, "y": 100, "w": 100, "h": 100},
                {"x": 600, "y": 100, "w": 100, "h": 100},
            ],
            "hip_centers": [{"cx": 100.0, "cy": 100.0}],
            "foot_centers": [{"cx": 100.0, "cy": 200.0}, {"cx": 120.0, "cy": 200.0}],
            "animation_names": {
                "leg": ["pedal"], "wheel": ["wheel-spin"],
                "background": ["scenery-scroll"],
            },
            "foot_pedal_px": [0, 0], "foot_pedal_details": [],
            "leg_wheel_overlap_px2": 0, "leg_leg_overlap_px2": 0,
            "leg_details": [
                {"has_path": True, "length_px": 100.0,
                 "start": {"x": 100.0, "y": 100.0}, "end": {"x": 100.0, "y": 200.0}},
                {"has_path": True, "length_px": 100.0,
                 "start": {"x": 100.0, "y": 100.0}, "end": {"x": 120.0, "y": 200.0}},
            ],
        }],
    }


def fake_fail_probe(_path: str = "") -> dict[str, Any]:
    return {"ok": False, "error": "frame probe unavailable in offline test"}


class ScriptedProposer:
    def __init__(self, script: dict[str, list[Action]]) -> None:
        self.script = {name: list(actions) for name, actions in script.items()}
        self.packets: list[dict[str, Any]] = []

    def propose(self, packet: dict[str, Any], *, tools: tuple[str, ...]) -> Action | None:
        self.packets.append(packet)
        queue = self.script.get(packet["stage"]["name"])
        return queue.pop(0) if queue else None


class MotionStack:
    def __init__(self, tmp: str) -> None:
        self.root = pathlib.Path(tmp) / "ws"
        self.sandbox = WorkspaceSandbox(self.root)

    def runtime(self, script, *, probe=None, verify=None, start_stage=None):
        proposer = ScriptedProposer(script)
        runtime = build_motion_capability(
            self.sandbox, proposer, probe=probe, verify=verify,
        )
        return runtime, proposer, start_stage

    def run(self, script, *, probe=None, verify=None, start_stage=None):
        runtime, proposer, start = self.runtime(script, probe=probe, verify=verify,
                                               start_stage=start_stage)
        result = runtime.run("draw the pelican on a bicycle", start_stage=start)
        return result, runtime, proposer


def denials(result) -> list[dict[str, Any]]:
    return [item for item in result.journal if item["event"] == "action_denied"]


class DraftStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = MotionStack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_1_css_only_edit_is_denied_and_the_artifact_is_unchanged(self) -> None:
        before, _, _ = self.stack.runtime({})
        initial = before.store.snapshot()
        result, runtime, _ = self.stack.run({"draft": [css_only()]}, probe=fake_pass_probe)
        self.assertEqual(len(denials(result)), 1)
        self.assertEqual(denials(result)[0]["code"], STAGE_ACTION_DENIED)
        self.assertIn("scene edits only", denials(result)[0]["message"])
        self.assertEqual(result.stage, "draft")
        self.assertEqual(runtime.store.snapshot().sha256, initial.sha256)
        self.assertFalse(runtime.store.scene_nonempty())
        self.assertFalse(runtime.store.result_exists())

    def test_2_scene_anchor_fill_advances_to_structure(self) -> None:
        result, runtime, _ = self.stack.run({"draft": [scene_fill(GOOD_SCENE)]},
                                           probe=fake_pass_probe)
        self.assertEqual(result.stage, "structure")
        self.assertTrue(runtime.store.scene_nonempty())
        transitions = [item for item in result.journal if item["event"] == "stage_transition"]
        self.assertEqual([(t["from"], t["to"]) for t in transitions], [("draft", "structure")])
        applied = [item for item in result.journal if item["event"] == "action_applied"]
        self.assertTrue(applied[0]["check"]["passed"])

    def test_7_css_only_until_repair_budget_exhaustion_is_typed(self) -> None:
        initial_runtime, _, _ = self.stack.runtime({})
        initial = initial_runtime.store.snapshot()
        result, runtime, _ = self.stack.run(
            {"draft": [css_only(), css_only(), css_only()]}, probe=fake_pass_probe,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.stage, "draft")
        self.assertEqual(result.reason, "ACTION_REPAIR_EXHAUSTED_NO_VALID")
        self.assertEqual(result.legacy_reason, "NO_DRAFT")
        self.assertEqual(len(denials(result)), 1, "wrong-anchor repairs are capped at one")
        self.assertTrue(all(d["code"] == STAGE_ACTION_DENIED for d in denials(result)))
        self.assertEqual(runtime.store.snapshot().sha256, initial.sha256)
        self.assertFalse(runtime.store.result_exists())
        self.assertEqual(result.to_dict()["budget"]["calls_used"], 1)
        self.assertEqual(result.to_dict()["budget"]["valid_actions"], 0)
        self.assertEqual(result.to_dict()["budget"]["repairs"], 1)


class StructureStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = MotionStack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_3_missing_hooks_keep_static_failed_and_required_fixes(self) -> None:
        result, runtime, _ = self.stack.run({
            "draft": [scene_fill(BAD_SCENE)],
            "structure": [styles_fill(), no_op_style(), no_op_style()],
        }, probe=fake_pass_probe)
        self.assertEqual(result.stage, "structure")
        self.assertEqual(result.reason, "ARTIFACT_CHECK_FAILED")
        self.assertEqual(result.legacy_reason, "STATIC_CHECK_FAILED")
        self.assertTrue(result.required_fixes, "the failing checks must keep their fixes")
        structure_checks = [
            item["check"] for item in result.journal
            if item["event"] == "action_applied" and item["stage"] == "structure"
        ]
        self.assertTrue(structure_checks)
        self.assertFalse(structure_checks[0]["passed"])
        names = [entry["name"] for entry in structure_checks[0]["checks"]]
        self.assertIn("hooks.all", names)
        self.assertIn("static.pass", names)
        self.assertFalse(runtime.store.result_exists())

    def test_4_hooks_and_static_pass_advance_to_motion(self) -> None:
        result, runtime, _ = self.stack.run({
            "draft": [scene_fill(GOOD_SCENE)],
            "structure": [styles_fill()],
        }, probe=fake_pass_probe)
        self.assertEqual(result.stage, "motion")
        transitions = [t["to"] for t in result.journal if t["event"] == "stage_transition"]
        self.assertEqual(transitions, ["structure", "motion"])
        structure_checks = [
            item["check"] for item in result.journal
            if item["event"] == "action_applied" and item["stage"] == "structure"
        ]
        self.assertTrue(structure_checks[-1]["passed"])
        self.assertTrue(runtime.store.scene_nonempty())

    def test_5_fake_probe_pass_and_complete_coverage_advance_to_delivery(self) -> None:
        result, runtime, _ = self.stack.run({
            "draft": [scene_fill(GOOD_SCENE)],
            "structure": [styles_fill()],
            "motion": [motion_check_action()],
        }, probe=fake_pass_probe)
        self.assertEqual(result.stage, "delivery")
        transitions = [t["to"] for t in result.journal if t["event"] == "stage_transition"]
        self.assertEqual(transitions, ["structure", "motion", "delivery"])
        motion_checks = [
            item["check"] for item in result.journal
            if item["event"] == "action_applied" and item["stage"] == "motion"
        ]
        self.assertTrue(motion_checks[-1]["passed"])
        self.assertTrue(runtime.checks.motion_check_pass(runtime.store.snapshot().sha256))


class DeliveryStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = MotionStack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_6a_no_motion_check_pass_refuses_the_result_write(self) -> None:
        result, runtime, _ = self.stack.run(
            {"delivery": [result_write()]}, probe=fake_fail_probe,
            start_stage="delivery",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.stage, "delivery")
        refused = denials(result)
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["code"], "MOTION_CHECK_REQUIRED")
        self.assertIn("motion_check", refused[0]["message"])
        self.assertFalse(runtime.store.result_exists())

    def test_6b_full_pipeline_with_a_pass_is_a_typed_success(self) -> None:
        result, runtime, proposer = self.stack.run({
            "draft": [scene_fill(GOOD_SCENE)],
            "structure": [styles_fill()],
            "motion": [motion_check_action()],
            "delivery": [result_write()],
        }, probe=fake_pass_probe)
        self.assertTrue(result.succeeded, result.reason)
        payload = result.to_dict()
        self.assertEqual(payload["kind"], "motion.render")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["stage"], "delivery")
        self.assertEqual(payload["artifact"]["sha256"], runtime.store.snapshot().sha256)
        self.assertEqual(payload["quality"]["status"], "pass")
        self.assertEqual(payload["quality"]["score"], 100)
        self.assertTrue(payload["quality"]["coverage"]["complete"])
        self.assertEqual(payload["required_fixes"], [])
        self.assertEqual(payload["reason"], "")
        self.assertTrue(payload["child_run_id"].startswith("cap-motion.render-"))
        self.assertTrue(all(isinstance(value, int) for value in payload["budget"].values()))
        self.assertTrue(runtime.store.result_exists())
        self.assertEqual(len(proposer.packets), 4)
        delivered = [
            item for item in result.journal
            if item["event"] == "action_applied" and item["stage"] == "delivery"
        ]
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["tool"], "write_file")
        self.assertTrue(delivered[0]["check"]["passed"])

    def test_6c_an_edit_after_the_pass_invalidates_that_pass(self) -> None:
        result, runtime, _ = self.stack.run({
            "draft": [scene_fill(GOOD_SCENE)],
            "structure": [styles_fill()],
            "motion": [motion_check_action()],
            "delivery": [result_write()],
        }, probe=fake_pass_probe)
        self.assertTrue(result.succeeded)
        passed_sha = runtime.store.snapshot().sha256
        self.assertTrue(runtime.checks.motion_check_pass(passed_sha))
        runtime.store.apply(Action("edit_file", {
            "old_text": STYLE_ANCHOR, "new_text": ".extra { opacity: 1; }\n" + STYLE_ANCHOR,
        }))
        self.assertNotEqual(runtime.store.snapshot().sha256, passed_sha)
        self.assertFalse(runtime.checks.motion_check_pass(runtime.store.snapshot().sha256))

    def test_6d_a_failed_final_verifier_repairs_index_html_then_rechecks(self) -> None:
        verify_calls: list[str] = []

        def verifier(path: str) -> dict[str, Any]:
            verify_calls.append(path)
            target = pathlib.Path(path)
            if not target.is_absolute():
                target = self.stack.sandbox.root / target
            ok = 'class="pelican bicycle"' in target.read_text(encoding="utf-8")
            return {"ok": ok, "detail": "" if ok else "missing identity hooks"}

        result, runtime, proposer = self.stack.run({
            "draft": [scene_fill(GOOD_SCENE)],
            "structure": [styles_fill()],
            "motion": [motion_check_action()],
            "delivery": [result_write(), identity_label_edit(), motion_check_action()],
        }, probe=fake_pass_probe, verify=verifier)
        self.assertTrue(result.succeeded, result.reason)
        self.assertEqual(result.stage, "delivery")
        self.assertTrue(runtime.store.result_exists())
        self.assertIn('class="pelican bicycle"', runtime.store.read("index.html")["content"])
        delivered = [
            item for item in result.journal
            if item["event"] == "action_applied" and item["stage"] == "delivery"
        ]
        self.assertEqual([item["tool"] for item in delivered],
                         ["write_file", "edit_file"])
        failed = [item["check"] for item in delivered if not item["check"]["passed"]]
        self.assertTrue(failed)
        verify_check = next(
            entry
            for item in failed
            for entry in item["checks"]
            if entry["name"] == "verify.pass"
        )
        self.assertEqual(verify_check["required_fixes"][0]["code"], "VERIFICATION_FAILED")
        self.assertEqual(len(verify_calls), 2)
        self.assertEqual(len(proposer.packets), 5)


class ConstraintsPacketTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = MotionStack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_raw_prompt_descriptor_and_verifier_contract_reach_the_packet(self) -> None:
        runtime, proposer, _ = self.stack.runtime({}, probe=fake_pass_probe)
        constraints = {
            "raw_user_prompt": "编写单文件HTML，内含SVG动画：卡通鹈鹕骑带辐条自行车",
            "scene_descriptor": {"id": "pelican-bicycle", "verify_terms": ["pelican", "bicycle"]},
            "verifier_contract": {"identity_terms": ["pelican", "bicycle"]},
        }
        result = runtime.run(
            "编写单文件HTML，内含SVG动画：卡通鹈鹕骑带辐条自行车",
            constraints=constraints,
            start_stage="draft",
        )
        self.assertTrue(proposer.packets)
        packet_constraints = proposer.packets[0]["constraints"]
        self.assertEqual(packet_constraints["raw_user_prompt"], constraints["raw_user_prompt"])
        self.assertEqual(packet_constraints["scene_descriptor"], constraints["scene_descriptor"])
        self.assertEqual(packet_constraints["verifier_contract"], constraints["verifier_contract"])


class CompositionRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stack = MotionStack(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _probe(*, offscreen: bool = False, foot: int = 0) -> dict[str, Any]:
        import copy
        data = copy.deepcopy(fake_pass_probe())
        for item in data["geometry"]:
            item["foot_pedal_px"] = [foot, foot]
            item["max_foot_pedal_px"] = foot
            if offscreen:
                item["wheel_boxes"][0]["x"] = -120
        return data

    def test_an_edit_that_moves_a_wheel_outside_is_rolled_back_to_best(self) -> None:
        class SequenceProbe:
            def __init__(self, *verdicts: dict[str, Any]) -> None:
                self.verdicts = list(verdicts)
                self.calls = 0

            def __call__(self, _path: str = "") -> dict[str, Any]:
                verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
                self.calls += 1
                return verdict

        bad_edit = Action("edit_file", {
            "old_text": '<g class="scenery">',
            "new_text": '<g class="scenery" data-bad="1">',
        })
        probe = SequenceProbe(
            self._probe(foot=40),       # motion_check: mechanical fail, composition safe
            self._probe(offscreen=True, foot=0),  # this edit: composition regression
        )
        result, runtime, _ = self.stack.run({
            "draft": [scene_fill(GOOD_SCENE)],
            "structure": [styles_fill()],
            "motion": [motion_check_action(), bad_edit],
        }, probe=probe)

        self.assertEqual(result.status, "failed")
        rolled = [item for item in result.journal if item["event"] == "artifact_rolled_back"]
        self.assertEqual(len(rolled), 1)
        self.assertEqual(rolled[0]["reason"], "composition_regression")
        self.assertEqual(result.repair_ticket["code"], "COMPOSITION_REGRESSION")
        self.assertNotIn('data-bad="1"', runtime.store.snapshot().content)
        self.assertEqual(runtime.store.snapshot().sha256, rolled[0]["to_sha256"])
        self.assertTrue(result.quality.get("composition_ok"), "the delivered artifact is the restored best")


if __name__ == "__main__":
    unittest.main()

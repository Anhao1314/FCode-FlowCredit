"""Rig scaffold: host-owned geometry, read-only gate, real probe acceptance."""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.capabilities.artifacts import Action, ArtifactStore  # noqa: E402
from flowagent.capabilities.checks import CheckContext, CheckRegistry  # noqa: E402
from flowagent.capabilities.motion import build_motion_capability, motion_manifest  # noqa: E402
from flowagent.capabilities.runtime import ActionGate, StageActionDenied  # noqa: E402
from flowagent.tools.general.motion_quality import analyze_motion, probe_quality  # noqa: E402
from flowagent.tools.general.motion_scaffold import (  # noqa: E402
    HOST_OWNED_START,
    MOTION_RIG_SCAFFOLD_HTML,
    edit_touches_host_owned,
    ensure_motion_scaffold,
    host_owned_blocks,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402


class ScriptedProposer:
    def propose(self, packet, *, tools):
        return None


class _NodeProbe:
    """Real Playwright probe; skipped when the browser sidecar is absent."""

    @staticmethod
    def available() -> bool:
        node = shutil.which("node")
        modules = ROOT / "viewer" / "node_modules" / "playwright"
        return bool(node and modules.is_dir())


class RigSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.sandbox = WorkspaceSandbox(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rig_has_host_owned_blocks_and_static_motion(self) -> None:
        blocks = host_owned_blocks(MOTION_RIG_SCAFFOLD_HTML)
        self.assertGreaterEqual(len(blocks), 2, "CSS + SVG + placeholder")
        self.assertIn("wheel-spin", MOTION_RIG_SCAFFOLD_HTML)
        self.assertIn("scenery-scroll", MOTION_RIG_SCAFFOLD_HTML)
        self.assertIn("<!-- SECTION: scene -->", MOTION_RIG_SCAFFOLD_HTML)
        static = analyze_motion(MOTION_RIG_SCAFFOLD_HTML)
        self.assertEqual(static["status"], "pass", static["issues"])
        self.assertEqual(static["score"], 100)
        self.assertGreater(static["wheel_speed_px_s"], 0)
        self.assertEqual(static["slip_bp"], 0)

    def test_rig_seed_does_not_count_as_model_scene_work(self) -> None:
        gated = ensure_motion_scaffold(self.sandbox, rig=True)
        self.assertEqual(gated, ("index.html",))
        store = ArtifactStore(self.sandbox, path="index.html")
        self.assertFalse(store.scene_nonempty())
        content = self.sandbox.read("index.html")["content"]
        self.assertIn("<g class=\"wheel wheel-rear\">", content)
        self.assertIn("<!-- FCODE MOTION SCAFFOLD v2 RIG -->", content)
        store.apply(Action("edit_file", {
            "old_text": "<!-- SECTION: scene -->",
            "new_text": ('<ellipse class="pelican-body" cx="450" cy="190" '
                         'rx="60" ry="40"/>\n<!-- SECTION: scene -->'),
        }))
        self.assertTrue(store.scene_nonempty())

    def test_base_scaffold_upgrades_but_real_work_is_never_overwritten(self) -> None:
        ensure_motion_scaffold(self.sandbox)
        ensure_motion_scaffold(self.sandbox, rig=True)
        content = self.sandbox.read("index.html")["content"]
        self.assertIn("v2 RIG", content)

        real = ("<!doctype html><html><style>@keyframes wheel-spin {}</style>"
                "<svg id=\"scene\" viewBox=\"0 0 10 10\"></svg></html>")
        self.sandbox.write("index.html", real)
        self.assertEqual(ensure_motion_scaffold(self.sandbox, rig=True), ())
        self.assertEqual(self.sandbox.read("index.html")["content"], real)

    def test_capability_can_assemble_with_the_rig(self) -> None:
        runtime = build_motion_capability(
            self.sandbox, ScriptedProposer(), seed_rig=True)
        self.assertIn("v2 RIG", runtime.store.content())
        self.assertFalse(runtime.store.scene_nonempty())


class RigGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.sandbox = WorkspaceSandbox(pathlib.Path(self.tmp.name))
        ensure_motion_scaffold(self.sandbox, rig=True)
        self.store = ArtifactStore(self.sandbox, path="index.html")
        self.manifest = motion_manifest()
        self.stage = self.manifest.stages[0]
        self.gate = ActionGate()
        self.checks = CheckRegistry()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def context(self) -> CheckContext:
        return CheckContext(
            store=self.store,
            artifact=self.store.snapshot(),
            manifest=self.manifest,
            stage=self.stage,
            result_written=False,
        )

    def check(self, action: Action) -> None:
        self.gate.require(
            action, stage=self.stage, context=self.context(),
            checks=self.checks, manifest=self.manifest,
        )

    def test_rig_edit_is_refused(self) -> None:
        for old_text in (
            '<g class="wheel wheel-rear">',
            'transform-origin: 450px 250px;',
        ):
            with self.assertRaises(StageActionDenied) as raised:
                self.check(Action("edit_file", {
                    "path": "index.html",
                    "old_text": old_text,
                    "new_text": "/* replaced */",
                }))
            self.assertEqual(raised.exception.code, "RIG_READ_ONLY")
            self.assertEqual(raised.exception.suggested_action, "edit_scene_visuals")

    def test_replacement_cannot_smuggle_a_rig_marker(self) -> None:
        with self.assertRaises(StageActionDenied) as raised:
            self.check(Action("edit_file", {
                "path": "index.html",
                "old_text": "<!-- SECTION: scene -->",
                "new_text": f"<!-- {HOST_OWNED_START} -->\n<!-- SECTION: scene -->",
            }))
        self.assertEqual(raised.exception.code, "RIG_READ_ONLY")

    def test_scene_visuals_are_allowed(self) -> None:
        self.check(Action("edit_file", {
            "path": "index.html",
            "old_text": "<!-- SECTION: scene -->",
            "new_text": ('<ellipse class="pelican-body" cx="450" cy="190" '
                         'rx="60" ry="40"/>\n<!-- SECTION: scene -->'),
        }))
        self.assertFalse(edit_touches_host_owned(
            self.store.content(), "<!-- SECTION: scene -->"))


@unittest.skipUnless(_NodeProbe.available(),
                     "Playwright/Node sidecar is not installed")
class RigProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        from motion_frames import MotionProbe

        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.sandbox = WorkspaceSandbox(self.root)
        self.probe = MotionProbe(self.root, self.root / "frames")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seeded_rig_passes_geometry_with_zero_slop(self) -> None:
        ensure_motion_scaffold(self.sandbox, rig=True)
        raw = self.probe.probe("index.html")
        self.assertTrue(raw.get("ok"), raw.get("error"))
        quality = probe_quality(raw)
        self.assertEqual(quality["status"], "pass", quality["issues"])
        self.assertTrue(quality["coverage"]["complete"])
        self.assertEqual(quality["max_foot_pedal_px"], 0)
        self.assertEqual(quality["leg_wheel_overlap_px2"], 0)
        self.assertEqual(quality["max_leg_start_to_hip_px"], 0)
        self.assertEqual(quality["max_leg_end_to_foot_px"], 0)
        self.assertGreaterEqual(quality["min_leg_length_px"], 60)

    def test_filling_the_rider_does_not_break_the_rig(self) -> None:
        ensure_motion_scaffold(self.sandbox, rig=True)
        content = self.sandbox.read("index.html")["content"]
        filled = content.replace(
            "<!-- SECTION: scene -->",
            '<ellipse class="pelican-body" cx="450" cy="190" rx="70" ry="45" '
            'fill="#fff4d6"/>\n<path class="beak" d="M505 185 l45 12 l-45 12 z" '
            'fill="#e8a54b"/>\n<!-- SECTION: scene -->',
        )
        self.sandbox.write("index.html", filled)
        raw = self.probe.probe("index.html")
        self.assertTrue(raw.get("ok"), raw.get("error"))
        quality = probe_quality(raw)
        self.assertEqual(quality["status"], "pass", quality["issues"])
        self.assertEqual(quality["max_foot_pedal_px"], 0)


if __name__ == "__main__":
    unittest.main()

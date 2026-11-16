"""Motion quality is a host-computable gate, not a prose opinion."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.tools.general.motion_quality import (  # noqa: E402
    analyze_motion,
    merge_quality,
    probe_quality,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402


def probe(*distances, leg_wheel=0, ok=True, frames=4, hips=True,
          hip_distance=0, leg_length=100, foot_end=0, wheel_boxes=None):
    if wheel_boxes is None:
        wheel_boxes = [
            {"x": 100, "y": 100, "w": 100, "h": 100, "cx": 150, "cy": 150},
            {"x": 700, "y": 100, "w": 100, "h": 100, "cx": 750, "cy": 150},
        ]
    geometry = []
    for distance in distances:
        geometry.append({
            "scene_box": {"x": 0, "y": 0, "w": 1000, "h": 600},
            "wheel_boxes": wheel_boxes,
            "feet": 2, "pedals": 2, "legs": 2, "wheels": len(wheel_boxes),
            "hips": 1 if hips else 0,
            "hip_centers": [{"cx": 10, "cy": 10}] if hips else [],
            "leg_details": [
                {
                    "has_path": True,
                    "length_px": leg_length,
                    "start": {"x": 10 + hip_distance, "y": 10},
                    "end": {"x": 10 + foot_end, "y": 10},
                }
                for _ in range(2)
            ],
            "foot_centers": [{"cx": 10, "cy": 10}, {"cx": 20, "cy": 20}],
            "animation_names": {
                "wheel": ["wheel-spin", "wheel-spin"],
                "leg": ["pedal", "pedal"],
                "background": ["scenery-scroll"],
            },
            "foot_pedal_px": [distance, distance + 2],
            "max_foot_pedal_px": distance + 2,
            "leg_wheel_overlap_px2": leg_wheel,
            "leg_leg_overlap_px2": 0,
        })
    return {
        "ok": ok,
        "error": "" if ok else "chromium unavailable",
        "frames": [
            {"time_ms": index * 300, "name": f"frame-{index}.png",
             "sha256": "a" * 64, "width": 1000, "height": 600}
            for index in range(frames)
        ],
        "geometry": geometry,
        "contact": "contact.png",
    }


def page(*, wheel_ms=1000, bg_ms=1000, bg_px=628, crank_ms=1000, leg_ms=1000,
         with_feet=True, wheel_rotate=True, bg_translate=True, wheel_deg=360,
         extra_circle=False) -> str:
    feet = '<g class="foot"></g><g class="pedal"></g>' if with_feet else ""
    rotate = (
        f"transform: rotate({wheel_deg}deg);" if wheel_rotate
        else "transform: translateX(0);"
    )
    translate = "transform: translateX(-%dpx);" % bg_px if bg_translate else "transform: rotate(0deg);"
    return f"""<!doctype html><html><head><style>
      .wheel {{ animation: wheel-spin {wheel_ms}ms linear infinite; }}
      .scenery {{ animation: scenery-scroll {bg_ms}ms linear infinite; }}
      .crank {{ animation: crank-spin {crank_ms}ms linear infinite; }}
      .leg {{ animation: pedal {leg_ms}ms ease-in-out infinite; }}
      @keyframes wheel-spin {{ to {{ {rotate} }} }}
      @keyframes scenery-scroll {{ to {{ {translate} }} }}
      @keyframes crank-spin {{ to {{ transform: rotate(360deg); }} }}
      @keyframes pedal {{ to {{ transform: rotate(20deg); }} }}
    </style></head><body><svg>
      <g class="wheel"><circle cx="100" cy="100" r="100"></circle></g>
      {('<circle cx="500" cy="500" r="300"></circle>' if extra_circle else '')}
      <g class="scenery"></g><g class="crank"></g><g class="leg"></g>
      <g class="leg"></g>{feet}
    </svg></body></html>"""


class MotionQualityTests(unittest.TestCase):
    def test_synced_wheel_and_ground_pass(self) -> None:
        result = analyze_motion(page())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["wheel_speed_px_s"], 628)
        self.assertEqual(result["background_speed_px_s"], 628)
        self.assertEqual(result["slip_bp"], 0)
        self.assertEqual(result["phase_mismatch_bp"], 0)
        self.assertTrue(result["foot_pedal_hooks"])

    def test_half_turn_with_half_circumference_background_passes(self) -> None:
        # A 180-degree turn is half a wheel circumference, not a full one.
        # The old analyzer assumed 360 degrees and reported a false 50% slip.
        result = analyze_motion(page(wheel_deg=180, bg_px=314))
        self.assertEqual(result["wheel_rotation_deg"], 180)
        self.assertEqual(result["wheel_speed_px_s"], 314)
        self.assertEqual(result["background_speed_px_s"], 314)
        self.assertEqual(result["slip_bp"], 0)
        self.assertEqual(result["status"], "pass")

    def test_full_turn_with_half_circumference_background_fails(self) -> None:
        result = analyze_motion(page(wheel_deg=360, bg_px=314))
        self.assertEqual(result["wheel_rotation_deg"], 360)
        self.assertEqual(result["wheel_speed_px_s"], 628)
        self.assertEqual(result["background_speed_px_s"], 314)
        self.assertGreater(result["slip_bp"], 2000)
        self.assertEqual(result["status"], "fail")

    def test_wheel_radius_ignores_larger_non_wheel_scene_circles(self) -> None:
        result = analyze_motion(page(extra_circle=True))
        self.assertEqual(result["wheel_radius_px"], 100)
        self.assertEqual(result["wheel_speed_px_s"], 628)
        self.assertEqual(result["status"], "pass")

    def test_rotate_property_and_explicit_from_to_are_measured(self) -> None:
        html = page(bg_px=314).replace(
            "to { transform: rotate(360deg); }",
            "from { rotate: 0deg; } to { rotate: 180deg; }",
        )
        result = analyze_motion(html)
        self.assertEqual(result["wheel_rotation_deg"], 180)
        self.assertEqual(result["wheel_speed_px_s"], 314)
        self.assertEqual(result["status"], "pass")

    def test_wheel_ground_slip_and_phase_mismatch_fail(self) -> None:
        result = analyze_motion(page(wheel_ms=1000, bg_px=450, bg_ms=4000,
                                     crank_ms=650, leg_ms=900))
        self.assertEqual(result["status"], "fail")
        self.assertGreater(result["slip_bp"], 2000)
        self.assertGreater(result["phase_mismatch_bp"], 2000)
        self.assertTrue(any("slip" in issue for issue in result["issues"]))
        self.assertTrue(any("period mismatch" in issue for issue in result["issues"]))

    def test_deck_is_a_support_role_not_a_bicycle_only_hook(self) -> None:
        result = analyze_motion(page().replace('class="pedal"', 'class="deck"'))
        self.assertTrue(result["foot_pedal_hooks"])
        self.assertEqual(result["status"], "pass")

    def test_missing_foot_hooks_are_a_warning_not_a_fake_pass(self) -> None:
        result = analyze_motion(page(with_feet=False))
        self.assertEqual(result["status"], "warn")
        self.assertLess(result["score"], 100)
        self.assertFalse(result["foot_pedal_hooks"])
        self.assertTrue(any("foot" in issue for issue in result["issues"]))

    def test_missing_motion_keyframes_fail_closed(self) -> None:
        result = analyze_motion(page(wheel_rotate=False, bg_translate=False))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("wheel" in issue for issue in result["issues"]))
        self.assertTrue(any("background" in issue for issue in result["issues"]))

    def test_geometry_catches_a_foot_off_the_pedal(self) -> None:
        result = probe_quality(probe(8, 61))
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["max_foot_pedal_px"], 63)
        self.assertEqual(result["frame_count"], 4)
        self.assertTrue(any("foot leaves pedal" in issue for issue in result["issues"]))
        self.assertTrue(any(fix["code"] == "FOOT_PEDAL_DISTANCE"
                            for fix in result["required_fixes"]))
        self.assertEqual(result["contact"], "contact.png")

    def test_geometry_catches_a_leg_through_the_wheel(self) -> None:
        result = probe_quality(probe(4, 5, leg_wheel=120))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("leg/wheel overlap" in issue for issue in result["issues"]))

    def test_clean_geometry_passes_and_stays_integer_safe(self) -> None:
        result = probe_quality(probe(4, 6, frames=4))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["unpaired_feet"], 0)
        for frame in result["frames"]:
            self.assertIsInstance(frame["time_ms"], int)
            self.assertEqual(len(frame["sha256"]), 64)

    def test_off_screen_wheel_fails_composition(self) -> None:
        result = probe_quality(probe(4, 6, wheel_boxes=[
            {"x": 100, "y": 100, "w": 100, "h": 100},
            {"x": 950, "y": 100, "w": 100, "h": 100},
        ]))
        self.assertEqual(result["status"], "fail")
        self.assertFalse(result["composition_ok"])
        self.assertIn("composition", " ".join(result["issues"]))
        self.assertTrue(any(fix["code"] == "WHEEL_OFFSCREEN"
                            for fix in result["required_fixes"]))

    def test_missing_composition_measurement_is_unverified(self) -> None:
        raw = probe(4, 6)
        for item in raw["geometry"]:
            item.pop("scene_box")
            item.pop("wheel_boxes")
        result = probe_quality(raw)
        self.assertEqual(result["status"], "unverified")
        self.assertIn("composition", result["coverage"]["uncovered"])

    def test_one_wheel_is_not_a_bicycle(self) -> None:
        result = probe_quality(probe(4, 6, wheel_boxes=[
            {"x": 100, "y": 100, "w": 100, "h": 100},
        ]))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any(fix["code"] == "WHEEL_COUNT"
                            for fix in result["required_fixes"]))

    def test_geometry_unavailable_is_unverified_not_a_pass(self) -> None:
        result = probe_quality({"ok": False, "error": "playwright missing"})
        self.assertEqual(result["status"], "unverified")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["frame_count"], 0)
        self.assertTrue(any("geometry unavailable" in issue for issue in result["issues"]))

    def test_missing_hip_anchor_is_unverified_not_pass(self) -> None:
        result = probe_quality(probe(4, 6, hips=False))
        self.assertEqual(result["status"], "unverified")
        self.assertIn("hip_anchor", result["coverage"]["uncovered"])
        self.assertLess(result["score"], 100)
        self.assertTrue(any(fix["code"] == "COVERAGE_MISSING"
                            for fix in result["required_fixes"]))

    def test_wrong_leg_animation_name_fails_the_contract(self) -> None:
        bad = probe(4, 6)
        for item in bad["geometry"]:
            item["animation_names"]["leg"] = ["pedal", "pedal-alt"]
        result = probe_quality(bad)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("animation contract" in issue for issue in result["issues"]))

    def test_leg_start_far_from_hip_and_short_leg_fail(self) -> None:
        far = probe_quality(probe(4, 6, hip_distance=50))
        self.assertEqual(far["status"], "fail")
        self.assertIn("leg does not start at the hip", " ".join(far["issues"]))
        short = probe_quality(probe(4, 6, leg_length=30))
        self.assertEqual(short["status"], "fail")
        self.assertIn("leg is too short", " ".join(short["issues"]))

    def test_merge_never_hides_a_geometry_failure_behind_a_text_pass(self) -> None:
        merged = merge_quality(analyze_motion(page()), probe(8, 63))
        self.assertEqual(merged["status"], "fail")
        self.assertEqual(merged["score"], 65)
        self.assertEqual(merged["max_foot_pedal_px"], 65)
        self.assertTrue(any("foot leaves pedal" in issue for issue in merged["issues"]))

    def test_text_pass_without_geometry_is_unverified(self) -> None:
        merged = merge_quality(analyze_motion(page()), None)
        self.assertEqual(merged["status"], "unverified")
        self.assertFalse(merged["coverage"]["complete"])
        self.assertIn("frame_capture", merged["coverage"]["uncovered"])

    def test_the_motion_check_tool_reads_the_workspace_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "workspace")
            sandbox.write("index.html", page())
            world = GENERAL_DOMAIN.create_world(spec={"mode": "open"}, sandbox=sandbox)
            tools = {tool.name: tool for tool in GENERAL_DOMAIN.build_tools(world)}
            self.assertIn("motion_check", tools)
            result = tools["motion_check"].handler({"path": "index.html"})
            self.assertEqual(result["status"], "unverified")
            self.assertFalse(result["coverage"]["complete"])
            self.assertTrue(any("geometry unavailable" in issue for issue in result["issues"]))

    def test_unchanged_artifact_reuses_the_quality_result(self) -> None:
        class CountingProbe:
            def __init__(self):
                self.calls = 0

            def probe(self, path):
                self.calls += 1
                return probe(4, 6)

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "workspace")
            sandbox.write("index.html", page())
            counting = CountingProbe()
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"}, sandbox=sandbox, motion_probe=counting,
            )
            tools = {tool.name: tool for tool in GENERAL_DOMAIN.build_tools(world)}
            first = tools["motion_check"].handler({"path": "index.html"})
            second = tools["motion_check"].handler({"path": "index.html"})
            self.assertEqual(counting.calls, 1)
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertIn("do not recheck", second["cache_note"])
            sandbox.write("index.html", page() + "\n<!-- changed -->\n")
            third = tools["motion_check"].handler({"path": "index.html"})
            self.assertEqual(counting.calls, 2)
            self.assertFalse(third["cached"])

    def test_the_motion_check_tool_uses_the_injected_geometry_probe(self) -> None:
        class FakeProbe:
            def probe(self, path):
                self.path = path
                return probe(8, 61)

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "workspace")
            sandbox.write("index.html", page())
            fake = FakeProbe()
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "open"}, sandbox=sandbox, motion_probe=fake,
            )
            tools = {tool.name: tool for tool in GENERAL_DOMAIN.build_tools(world)}
            result = tools["motion_check"].handler({"path": "index.html"})
            self.assertEqual(fake.path, "index.html")
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["max_foot_pedal_px"], 63)
            self.assertEqual(result["frame_count"], 4)


if __name__ == "__main__":
    unittest.main()

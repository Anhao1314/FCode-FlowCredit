"""Jev visual criticism is scene-driven, advisory, and fail-closed."""

from __future__ import annotations

import base64
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.decisions.jev_visual import (  # noqa: E402
    build_request,
    classify_visual,
    load_env_file,
    load_scene,
    prepare_image,
)

PELICAN = {
    "id": "test-pelican-bicycle",
    "subject": {"label": "cartoon pelican", "description": "bird with a throat pouch"},
    "vehicle": {"label": "bicycle", "description": "two-wheeled bicycle",
                "support_label": "pedal"},
    "features": {"two_legs": True, "legs_attach": True, "feet_on_support": True},
    "composition": "pelican riding a bicycle",
}
ROBOT = {
    "id": "test-robot-scooter",
    "subject": {"label": "cartoon robot", "description": "robot with two legs"},
    "vehicle": {"label": "kick scooter", "description": "scooter with a deck",
                "support_label": "deck"},
    "features": {"two_legs": True, "legs_attach": True, "feet_on_support": True},
    "composition": "robot riding a kick scooter",
}
BALL = {
    "id": "test-abstract-bounce",
    "subject": {"label": "bouncing ball", "description": "simple ball"},
    "vehicle": {"label": "ground", "description": "ground line", "support_label": "ground"},
    "features": {"two_legs": False, "legs_attach": False, "feet_on_support": False},
    "composition": "bouncing ball on the ground",
}


def noul(value: float) -> dict:
    return {"type": "noul", "noul": value}


def good_answers() -> dict:
    return {
        "subject_visible": noul(0.97),
        "vehicle_visible": noul(0.96),
        "two_legs_visible": noul(0.9),
        "legs_attach_to_body": noul(0.88),
        "near_foot_on_support": noul(0.93),
        "far_foot_on_support": noul(0.81),
        "occlusion": {"type": "choice", "choice": "none",
                      "confidence": 0.8, "probabilities": {"none": 0.8}},
        "composition": {"type": "score", "score": 3.0, "confidence": 0.7,
                        "legend": ["broken", "weak", "acceptable", "good", "strong"]},
    }


class JevVisualTests(unittest.TestCase):
    def test_image_is_cropped_resized_and_encoded(self) -> None:
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is optional; image cropping is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "frame.png"
            Image.new("RGB", (400, 300), "white").save(path)
            result = prepare_image(path, crop=(10, 10, 390, 290), scale=2.0)
            self.assertEqual(result["width"], 760)
            self.assertEqual(result["height"], 560)
            self.assertEqual(result["format"], "png")
            self.assertTrue(base64.b64decode(result["data_base64"]).startswith(b"\x89PNG"))
            self.assertEqual(result["crop"]["left"], 10)

    def test_request_contains_all_typed_primitives(self) -> None:
        request = build_request("jev-test", {
            "format": "png", "data_base64": "abc",
        }, PELICAN)
        questions = request["questions"]
        self.assertEqual(questions["two_legs_visible"]["type"], "noul")
        self.assertEqual(questions["occlusion"]["type"], "choice")
        self.assertEqual(questions["composition"]["type"], "score")
        self.assertEqual(questions["hip_box"]["type"], "box")
        self.assertIn("frame-main", request["assets"])

    def test_robot_scene_is_descriptor_driven_not_pelican_specific(self) -> None:
        request = build_request("jev-test", {
            "format": "png", "data_base64": "abc",
        }, ROBOT)
        serialized = json.dumps(request, ensure_ascii=False).lower()
        self.assertIn("cartoon robot", serialized)
        self.assertIn("kick scooter", serialized)
        self.assertNotIn("pelican", serialized)
        self.assertNotIn("bicycle", serialized)
        self.assertIn("near_foot_on_support", request["questions"])

    def test_abstract_scene_omits_leg_and_support_questions(self) -> None:
        request = build_request("jev-test", {
            "format": "png", "data_base64": "abc",
        }, BALL)
        questions = request["questions"]
        self.assertNotIn("two_legs_visible", questions)
        self.assertNotIn("legs_attach_to_body", questions)
        self.assertNotIn("near_foot_on_support", questions)
        self.assertNotIn("near_foot_box", questions)
        answers = {
            "subject_visible": noul(0.95),
            "vehicle_visible": noul(0.9),
            "occlusion": {"type": "choice", "choice": "none",
                          "confidence": 0.8, "probabilities": {"none": 0.8}},
            "composition": {"type": "score", "score": 3.0, "confidence": 0.7,
                            "legend": []},
        }
        result = classify_visual(answers, BALL)
        self.assertEqual(result["status"], "pass")

    def test_core_source_contains_no_pelican_specific_words(self) -> None:
        source = (ROOT / "src" / "flowagent" / "decisions" / "jev_visual.py").read_text(
            encoding="utf-8"
        ).lower()
        self.assertNotIn("pelican", source)
        self.assertNotIn("bicycle", source)

    def test_good_visual_signals_pass_but_stay_advisory(self) -> None:
        result = classify_visual(good_answers(), PELICAN)
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["advisory_only"])
        self.assertIn("does not change", result["note"])

    def test_leg_through_vehicle_fails(self) -> None:
        answers = good_answers()
        answers["occlusion"] = {
            "type": "choice", "choice": "legs_through_vehicle",
            "confidence": 0.9, "probabilities": {"legs_through_vehicle": 0.9},
        }
        result = classify_visual(answers, PELICAN)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("legs_through_vehicle" in issue for issue in result["issues"]))

    def test_low_confidence_occlusion_warns_instead_of_hard_failing(self) -> None:
        answers = good_answers()
        answers["occlusion"] = {
            "type": "choice", "choice": "legs_through_vehicle",
            "confidence": 0.54, "probabilities": {"legs_through_vehicle": 0.63},
        }
        result = classify_visual(answers, PELICAN)
        self.assertEqual(result["status"], "warn")
        self.assertTrue(any("low confidence" in issue for issue in result["issues"]))

    def test_missing_answer_is_unverified_not_pass(self) -> None:
        answers = good_answers()
        del answers["two_legs_visible"]
        result = classify_visual(answers, PELICAN)
        self.assertEqual(result["status"], "unverified")
        self.assertTrue(any("two_legs_visible" in issue for issue in result["issues"]))

    def test_scene_config_files_load(self) -> None:
        for name in ("pelican_bicycle", "robot_scooter", "abstract_bounce"):
            scene = load_scene(ROOT / "config" / "scenes" / f"{name}.json")
            self.assertEqual(scene["id"], scene["id"])
            self.assertIn("subject", scene)

    def test_scene_descriptors_are_chain_safe(self) -> None:
        from flowagent.core.hashing import canonical_json

        for name in ("pelican_bicycle", "robot_scooter", "abstract_bounce"):
            scene = load_scene(ROOT / "config" / "scenes" / f"{name}.json")
            canonical_json(scene)  # floats would fail the hash chain here

    def test_env_file_parser_keeps_names_and_does_not_require_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "jev.env"
            path.write_text(
                "export TYPESAFE_API_KEY='secret-value'\nTYPESAFE_MODEL=jev-test\n",
                encoding="utf-8",
            )
            values = load_env_file(path)
            self.assertEqual(values["TYPESAFE_API_KEY"], "secret-value")
            self.assertEqual(values["TYPESAFE_MODEL"], "jev-test")


if __name__ == "__main__":
    unittest.main()

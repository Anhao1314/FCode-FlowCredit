"""The host's deterministic motion probe: frame capture and geometry."""

from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from motion_frames import HOST_VERIFY_TIMES_MS, MotionProbe  # noqa: E402
from flowagent.tools.general.motion_quality import probe_quality  # noqa: E402

PLAYWRIGHT = ROOT / "viewer" / "node_modules" / "playwright"


def page() -> str:
    return """<!doctype html><html><head><style>
      #scene { background: #f4efe5; }
      .wheel { animation: wheel-spin 1000ms linear infinite; transform-origin: 350px 250px; }
      .leg { animation: pedal 1000ms linear infinite; transform-origin: 50px 50px; }
      .scenery { animation: scenery-scroll 1000ms linear infinite; }
      @keyframes wheel-spin { to { transform: rotate(360deg); } }
      @keyframes pedal { to { transform: rotate(4deg); } }
      @keyframes scenery-scroll { to { transform: translateX(-20px); } }
    </style></head><body>
    <svg id="scene" width="400" height="300" viewBox="0 0 400 300">
      <g class="scenery"><rect x="0" y="0" width="10" height="10"></rect></g>
      <g class="wheel"><circle cx="350" cy="250" r="25"></circle></g>
      <g class="wheel" style="transform-origin:100px 250px"><circle cx="100" cy="250" r="25"></circle></g>
      <circle class="hip" cx="100" cy="100" r="6"></circle>
      <g class="leg"><path d="M100 100 L80 200" fill="none" stroke="black" stroke-width="4"></path></g>
      <line class="leg" x1="100" y1="100" x2="120" y2="200" stroke="black" stroke-width="4"></line>
      <g class="foot"><circle cx="80" cy="200" r="6"></circle></g>
      <g class="foot"><circle cx="120" cy="200" r="6"></circle></g>
      <g class="pedal"><circle cx="80" cy="200" r="6"></circle></g>
      <g class="pedal"><circle cx="120" cy="200" r="6"></circle></g>
    </svg></body></html>"""


class MotionProbeTests(unittest.TestCase):
    def test_the_host_gate_uses_offset_samples(self) -> None:
        self.assertEqual(HOST_VERIFY_TIMES_MS, (150, 450, 750, 1050))
        self.assertNotEqual(HOST_VERIFY_TIMES_MS, (0, 300, 600, 900))

    def test_path_outside_the_workspace_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = MotionProbe(pathlib.Path(tmp) / "ws", pathlib.Path(tmp) / "frames")
            result = probe.probe("../secret.html")
            self.assertFalse(result["ok"])
            self.assertIn("outside", result["error"])

    def test_missing_html_is_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = MotionProbe(pathlib.Path(tmp) / "ws", pathlib.Path(tmp) / "frames")
            result = probe.probe("index.html")
            self.assertFalse(result["ok"])
            self.assertIn("not found", result["error"])

    @unittest.skipUnless(shutil.which("node") and PLAYWRIGHT.exists(), "Playwright unavailable")
    def test_probe_captures_frames_and_contact_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "ws"
            root.mkdir()
            (root / "index.html").write_text(page(), encoding="utf-8")
            probe = MotionProbe(root, pathlib.Path(tmp) / "frames")
            result = probe.probe("index.html", times=(0, 300))
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(result["frames"]), 2)
            self.assertEqual(result["contact"], "contact.png")
            self.assertTrue((pathlib.Path(tmp) / "frames" / "contact.png").is_file())
            geometry = result["geometry"]
            self.assertEqual(geometry[0]["feet"], 2)
            self.assertEqual(geometry[0]["pedals"], 2)
            self.assertEqual(geometry[0]["hips"], 1)
            self.assertEqual(geometry[0]["max_foot_pedal_px"], 0)
            self.assertEqual(geometry[0]["leg_wheel_overlap_px2"], 0)
            for leg in geometry[0]["leg_details"]:
                self.assertTrue(leg["has_path"])
                self.assertGreater(leg["length_px"], 60)
                self.assertIsNotNone(leg["start"])
                self.assertIsNotNone(leg["end"])
            quality = probe_quality(result)
            self.assertEqual(quality["status"], "pass", quality)
            self.assertTrue(quality["coverage"]["complete"])


if __name__ == "__main__":
    unittest.main()

"""Model-free checks for the real pelican evaluation's artifact verifier."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_work_pelican import VERIFY


VALID_HTML = """<!doctype html>
<html><head><style>
@keyframes wheel-spin { to { rotate: 360deg; } }
@keyframes pedal { to { transform: rotate(360deg); } }
@keyframes scenery-scroll { to { transform: translateX(-100px); } }
.wheel { animation-name: wheel-spin; }
.leg { animation-name: pedal; }
.scenery { animation-name: scenery-scroll; }
</style></head><body>
<h1>Pelican bicycle ride</h1>
<svg viewBox = '0 0 900 520' xmlns="http://www.w3.org/2000/svg">
  <g class="wheel leg foot hip pedal scenery"></g>
</svg></body></html>
"""


class PelicanEvalVerifyTests(unittest.TestCase):
    def run_verify(self, html: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "verify.py").write_text(VERIFY, encoding="utf-8")
            (workspace / "index.html").write_text(html, encoding="utf-8")
            return subprocess.run(
                [sys.executable, "verify.py"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_capability_compliant_html_passes_sandbox_verify(self) -> None:
        result = self.run_verify(VALID_HTML)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_javascript_is_rejected(self) -> None:
        result = self.run_verify(VALID_HTML.replace("</body>", "<script>requestAnimationFrame(() => {});</script></body>"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("<script> is forbidden", result.stderr)

    def test_missing_wheel_leg_and_foot_hooks_are_rejected(self) -> None:
        missing_hooks = VALID_HTML.replace("wheel leg foot", "").replace(".wheel { animation-name: wheel-spin; }", "")
        result = self.run_verify(missing_hooks)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("class .wheel", result.stderr)
        self.assertIn("class .leg", result.stderr)
        self.assertIn("class .foot", result.stderr)


if __name__ == "__main__":
    unittest.main()

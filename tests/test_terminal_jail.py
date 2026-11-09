"""Seatbelt-jailed persistent terminal: workspace writes only."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.tools.general.terminal import PersistentShell  # noqa: E402


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS-only")
class TerminalJailTest(unittest.TestCase):
    def test_workspace_writes_allowed_outside_writes_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workspace = base / "ws"
            shell = PersistentShell(workspace, jail=True)
            try:
                inside = shell.execute("echo inside > ok.txt && cat ok.txt")
                self.assertEqual(inside["exit_code"], 0, inside)
                self.assertIn("inside", inside["output"])
                self.assertTrue((workspace / "ok.txt").is_file())

                escaped = base / "escaped.txt"
                denied = shell.execute(f"echo escaped > {escaped}")
                self.assertNotEqual(denied["exit_code"], 0, "jail 必须拒绝工作区外写入")
                self.assertFalse(escaped.exists())

                python = shell.execute("python3 -c \"print('jail-ok')\"")
                self.assertEqual(python["exit_code"], 0, python)
                self.assertIn("jail-ok", python["output"])
            finally:
                shell.close()

    def test_unjailed_shell_keeps_old_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            shell = PersistentShell(base / "ws")
            try:
                self.assertFalse(shell.jail)
                outside = base / "plain.txt"
                result = shell.execute(f"echo plain > {outside}")
                self.assertEqual(result["exit_code"], 0, result)
                self.assertTrue(outside.is_file())
            finally:
                shell.close()


if __name__ == "__main__":
    unittest.main()
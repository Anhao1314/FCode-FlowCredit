"""Seatbelt-jailed persistent terminal: workspace writes only."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.tools.general.terminal import PersistentShell  # noqa: E402


def _nested_seatbelt_available() -> bool:
    """Whether this host lets a process apply a deny-default Seatbelt profile.

    A hardened/sandboxed parent (CI runners, hardened app helpers) can refuse
    ``sandbox_apply`` with EPERM even on macOS. That is a host capability, not a
    defect in the terminal: the jailed-shell assertions cannot run there, while
    the unjailed path still can. Detect it once instead of reporting two false
    ``TERMINAL_DEAD`` errors.
    """
    if sys.platform != "darwin":
        return False
    try:
        done = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p",
             "(version 1)(deny default)(allow file-read*)", "/bin/true"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


SEATBELT_AVAILABLE = _nested_seatbelt_available()


@unittest.skipUnless(sys.platform == "darwin", "Seatbelt is macOS-only")
class TerminalJailTest(unittest.TestCase):
    @unittest.skipUnless(SEATBELT_AVAILABLE, "host refuses nested sandbox-exec profiles (EPERM)")
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

    @unittest.skipUnless(SEATBELT_AVAILABLE, "host refuses nested sandbox-exec profiles (EPERM)")
    def test_user_site_packages_remain_importable_inside_jail(self) -> None:
        """Redirecting HOME must not hide the operator's user site.

        The terminal and run_command share the host's Python, but the jailed
        shell used to lose ``~/Library/Python/.../site-packages`` because HOME
        moved into the workspace. A package that imports under run_command
        (Pillow here) must import under terminal too.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            probe = base / "user-site"
            probe.mkdir()
            (probe / "fcode_user_site_probe.py").write_text("VALUE = 42\n", encoding="utf-8")
            shell = PersistentShell(base / "ws", jail=True)
            try:
                with mock.patch(
                    "flowagent.tools.general.terminal.site.getusersitepackages",
                    return_value=str(probe),
                ):
                    result = shell.execute("python3 -c 'import fcode_user_site_probe; print(fcode_user_site_probe.VALUE)'")
                self.assertEqual(result["exit_code"], 0, result)
                self.assertIn("42", result["output"])
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
"""External browser sidecar: allowlist and URL policy (no page is opened)."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "viewer" / "browser_external.mjs"
PLAYWRIGHT = ROOT / "viewer" / "node_modules" / "playwright"


def _ask(process: subprocess.Popen, payload: dict, *, timeout: float = 20.0) -> dict:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise AssertionError("sidecar closed without a reply")
    return json.loads(line)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
@unittest.skipUnless(PLAYWRIGHT.is_dir(), "Playwright is not installed in viewer/node_modules")
class BrowserExternalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process = subprocess.Popen(
            [shutil.which("node"), str(SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(SCRIPT.parent),
        )
        self.addCleanup(self._close)

    def _close(self) -> None:
        try:
            _ask(self.process, {"id": 999, "command": "release"}, timeout=5)
        except Exception:  # noqa: BLE001 — best effort
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.process.kill()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def test_ping_reports_the_external_browser_profile(self) -> None:
        reply = _ask(self.process, {"id": 1, "command": "ping"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["platform"], "external-browser")

    def test_private_and_non_allowlisted_hosts_are_denied_before_launch(self) -> None:
        cases = (
            ("https://127.0.0.1/", ["example.com"]),
            ("http://localhost:8080/", ["example.com"]),
            ("https://10.0.0.8/", ["example.com"]),
            ("https://evil.example.org/", ["example.com"]),
            ("file:///etc/passwd", ["example.com"]),
            ("https://user:secret@example.com/", ["example.com"]),
        )
        for index, (url, domains) in enumerate(cases, start=2):
            with self.subTest(url=url):
                reply = _ask(self.process, {
                    "id": index, "command": "look", "url": url, "domains": domains,
                })
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"]["code"], "URL_DENIED")

    def test_unknown_command_is_refused(self) -> None:
        reply = _ask(self.process, {"id": 2, "command": "nope"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "COMPUTER_COMMAND_INVALID")


if __name__ == "__main__":
    unittest.main()

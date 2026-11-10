"""Loopback browser inspection: URL policy, graceful absence, real sidecar."""

from __future__ import annotations

import functools
import http.server
import pathlib
import socketserver
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.browser import BrowserRunner, validate_browser_url  # noqa: E402
from flowagent.tools.general.tools import GENERAL_TOOLS, build_general_tools  # noqa: E402


class BrowserPolicyTests(unittest.TestCase):
    def test_loopback_urls_are_allowed(self) -> None:
        for url in (
            "http://127.0.0.1:5173/",
            "http://localhost:8642/index.html",
            "http://[::1]:3000/",
        ):
            self.assertEqual(validate_browser_url(url), url)

    def test_external_credentials_and_file_urls_are_refused(self) -> None:
        for url, code in (
            ("https://example.com/", "BROWSER_URL_DENIED"),
            ("http://user:pass@127.0.0.1/", "BROWSER_URL_DENIED"),
            ("file:///etc/passwd", "BROWSER_URL_DENIED"),
            ("http://10.0.0.1/", "BROWSER_URL_DENIED"),
            ("", "BROWSER_URL_INVALID"),
        ):
            with self.assertRaises(AgentError) as error:
                validate_browser_url(url)
            self.assertEqual(error.exception.code, code, url)

    def test_missing_node_degrades_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = BrowserRunner(pathlib.Path(tmp), node="/definitely/missing/node")
            result = runner.inspect("http://127.0.0.1:9/")
            self.assertFalse(result["available"])
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "BROWSER_UNAVAILABLE")

    def test_tool_is_registered_as_read_and_present_in_general_catalogue(self) -> None:
        self.assertIn("browser_inspect", GENERAL_TOOLS)
        with tempfile.TemporaryDirectory() as tmp:
            from flowagent.tools.general.fetch import UrlFetcher
            from flowagent.tools.general.sandbox import WorkspaceSandbox

            specs = build_general_tools(WorkspaceSandbox(pathlib.Path(tmp)), UrlFetcher())
            spec = next(item for item in specs if item.name == "browser_inspect")
            self.assertEqual(spec.effect, "read")
            self.assertEqual(spec.params[0].name, "url")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: D401 - silence test server
        return


class BrowserSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "index.html").write_text(
            "<!doctype html><html><head><meta charset='utf-8'><title>Preview OK</title></head>"
            "<body><h1>Hello FCode</h1><button>提交</button>"
            "<a href='/next'>下一节</a><input name='q'></body></html>",
            encoding="utf-8",
        )
        handler = functools.partial(_QuietHandler, directory=str(self.workspace))
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.runner = BrowserRunner(
            self.workspace, artifact_root=self.root / "artifacts",
        )
        available, reason = self.runner.availability()
        if not available:
            self.skipTest(reason)

    def test_inspect_returns_dom_text_elements_and_screenshot(self) -> None:
        port = self.server.server_address[1]
        result = self.runner.inspect(f"http://127.0.0.1:{port}/", full_page=True)
        self.assertTrue(result["available"], result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["title"], "Preview OK")
        self.assertIn("Hello FCode", result["text"])
        self.assertIn("提交", [item["text"] for item in result["elements"]])
        shot = result["screenshot"]
        self.assertIsNotNone(shot)
        self.assertTrue(shot["path"].endswith(".png"))
        self.assertGreater(shot["bytes"], 0)
        self.assertTrue((self.root / "artifacts" / shot["path"]).is_file())
        self.assertIn("/api/browser/artifact?path=", shot["url"])
        dom = result["dom"]
        self.assertIsNotNone(dom)
        self.assertGreater(dom["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
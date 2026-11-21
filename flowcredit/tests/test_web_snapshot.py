"""P1 网站靶子治理化快照器：robots 合规、响应头事实、溯源入链。"""

from __future__ import annotations

import http.server
import json
import pathlib
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from web_snapshot import _browser_headers, detect_challenge, snapshot_url  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /admin\n"
            self.send_response(200)
        elif self.path.startswith("/admin"):
            body = b"forbidden"
            self.send_response(403)
        elif self.path in ("/", "/zh"):
            body = ('<html><head><script src="/app.js"></script>'
                    '<link href="/style.css" rel="stylesheet"></head>'
                    "<body>hello</body></html>").encode()
            self.send_response(200)
        elif self.path == "/app.js":
            body = b"console.log(1);"
            self.send_response(200)
        elif self.path == "/style.css":
            body = b"body{}"
            self.send_response(200)
        else:
            body = b""
            self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.send_header("X-Custom", "probe")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: N802 — 静默测试日志
        pass


class SnapshotUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.srv_dir = pathlib.Path(self._tmp.name) / "site"
        self.srv_dir.mkdir(parents=True)
        (self.srv_dir / "app.js").write_text("console.log(1);")
        (self.srv_dir / "style.css").write_text("body{}")
        handler = _Handler
        self._srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.addCleanup(self._srv.server_close)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_snapshot_fetches_assets_and_records_headers(self) -> None:
        out = pathlib.Path(self._tmp.name) / "snap"
        summary = snapshot_url(self._url("/zh"), out, runtime=False)
        self.assertEqual(summary["landing"]["status"], 200)
        self.assertEqual(summary["assets"]["fetched"], 2)
        self.assertTrue((out / "index.html").exists())
        self.assertTrue((out / "app.js").exists())
        headers = json.loads((out / "headers.json").read_text())
        self.assertEqual(headers["landing_headers"].get("x-custom"), "probe")
        self.assertIn("content-security-policy", summary["security_headers"]["missing"])
        prov = (out / "PROVENANCE.md").read_text()
        self.assertIn("robots", prov)
        self.assertIn(str(self._url("/zh")), prov)

    def test_robots_disallowed_path_refused(self) -> None:
        out = pathlib.Path(self._tmp.name) / "snap2"
        with self.assertRaises(ValueError) as caught:
            snapshot_url(self._url("/admin"), out)
        self.assertIn("robots", str(caught.exception))
        # 不产出半成品目录
        self.assertFalse(out.exists() and any(out.iterdir()))

    def test_challenge_detection_is_evidence_not_defeat(self) -> None:
        self.assertEqual(detect_challenge(403, b""), "http-403")
        self.assertEqual(detect_challenge(429, b""), "http-429")
        self.assertEqual(detect_challenge(200, b"<title>Just a moment...</title>"), "just a moment")
        self.assertEqual(detect_challenge(200, b"<html>normal</html>"), "")
        # 挑战在快照层是 fail-closed 的事实（覆盖受限），不是要破解的东西
        with mock.patch("web_snapshot._fetch",
                        return_value=(403, {}, b"challenge")):
            with self.assertRaises(ValueError) as blocked:
                snapshot_url("http://127.0.0.1:1/x", pathlib.Path(self._tmp.name) / "s6", runtime=False)
        self.assertIn("反自动化挑战", str(blocked.exception))

    def test_browser_grade_headers_present(self) -> None:
        headers = _browser_headers("document")
        self.assertIn("sec-ch-ua", headers)
        self.assertIn("Sec-Fetch-Mode", headers)
        self.assertEqual(headers["Sec-Fetch-Dest"], "document")
        asset = _browser_headers("script")
        self.assertEqual(asset["Sec-Fetch-Dest"], "script")

    def test_non_http_url_refused(self) -> None:
        with self.assertRaises(ValueError):
            snapshot_url("ftp://example.com/x", pathlib.Path(self._tmp.name) / "s3")
        with self.assertRaises(ValueError):
            snapshot_url("not-a-url", pathlib.Path(self._tmp.name) / "s4")


if __name__ == "__main__":
    unittest.main()

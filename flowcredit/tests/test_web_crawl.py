"""P3 子页面爬取策略 + 部署锚点：受控覆盖与两级变化视图。"""

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

from web_snapshot import diff_anchors, read_anchor, snapshot_url  # noqa: E402

PAGES = {
    "/": '<html><a href="/about">A</a><a href="/pricing?x=1">P</a>'
         '<a href="/admin">X</a><a href="https://evil.example/x">E</a>'
         '<script src="/app.js"></script></html>',
    "/about": "<html>about page</html>",
    "/pricing": "<html>pricing page</html>",
    "/admin": "<html>admin</html>",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    overrides: dict[str, bytes] = {}

    def do_GET(self):  # noqa: N802
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /admin\n"
            self.send_response(200)
        elif self.path in ("/app.js",):
            body = b"console.log(1)"
            self.send_response(200)
        elif self.path.split("?")[0] in PAGES or self.path.split("?")[0] in self.overrides:
            body = self.overrides.get(self.path.split("?")[0],
                                      PAGES.get(self.path.split("?")[0], "")).encode() \
                if isinstance(self.overrides.get(self.path.split("?")[0]), str) else PAGES.get(self.path.split("?")[0], "").encode()
            self.send_response(200)
        else:
            body = b""
            self.send_response(404)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: N802
        pass


class CrawlPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self.addCleanup(self._srv.server_close)
        self.url = f"http://127.0.0.1:{self.port}/"

    def _snapshot(self, name: str, **kwargs):
        out = pathlib.Path(self._tmp.name) / name
        return snapshot_url(self.url, out, runtime=False, **kwargs), out

    def test_crawl_depth1_robots_and_same_origin(self) -> None:
        with mock.patch("time.sleep"):
            summary, out = self._snapshot("snap", max_pages=3)
        pages = summary["pages"]
        crawled = set(pages["crawled"])
        self.assertIn("index.html", crawled)
        self.assertIn("about.html", crawled)
        # robots 拒绝 /admin；外链不追；查询串路由也抓（/pricing?x=1 → pricing.html）
        skipped = {row["path"]: row["reason"] for row in pages["skipped"]}
        self.assertEqual(skipped.get("/admin"), "robots")
        self.assertIn("pricing.html", crawled)
        self.assertFalse(any("evil" in p for p in crawled))

    def test_anchor_and_diff_on_redeploy(self) -> None:
        with mock.patch("time.sleep"):
            _, out = self._snapshot("site", max_pages=2)
        anchor = read_anchor(out)
        self.assertIsNotNone(anchor)
        self.assertIn("index.html", anchor["pages"])
        self.assertGreater(anchor["assets"]["count"], 0)

        # 模拟部署：改动 about 页内容后重爬
        _Handler.overrides = {"/about": "<html>about page v2</html>"}
        try:
            with mock.patch("time.sleep"):
                summary2, _ = self._snapshot("site", max_pages=2)
        finally:
            _Handler.overrides = {}
        change = summary2.get("change_since_last") or {}
        self.assertTrue(change.get("changed"), change)
        self.assertIn("about.html", change.get("pages_changed") or [])
        diff_file = out.with_name("site.snapshot-diff.json")
        self.assertTrue(diff_file.is_file())
        payload = json.loads(diff_file.read_text())
        self.assertEqual(payload["pages_changed"], ["about.html"])

    def test_anchor_survives_encryption_for_diffing(self) -> None:
        from audit_privacy import encrypt_dir

        with mock.patch("time.sleep"):
            _, out = self._snapshot("enc", max_pages=1)
        with mock.patch.dict("os.environ", {"FCODE_AUDIT_KEY_FILE": str(pathlib.Path(self._tmp.name) / "k")}):
            archive = encrypt_dir(out, archive_name="enc-audit1.tar.enc")
        self.assertFalse(out.exists())
        # 锚点在密文外——加密后仍可读、可 diff
        anchor = read_anchor(out)
        self.assertIsNotNone(anchor)
        self.assertTrue(archive.is_file())
        again = {"fetched_at": "later",
                 "pages": {k: "different" for k in anchor["pages"]},
                 "assets": {"count": 0, "bytes": 0}}
        change = diff_anchors(anchor, again)
        self.assertTrue(change["changed"])


if __name__ == "__main__":
    unittest.main()

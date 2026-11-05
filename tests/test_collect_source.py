"""Host-configured collection: fetch text, anchor it, cite it.

A real loopback server exercises the transport path. The connector is the only
place that knows the URL; the tool receives a source name and returns a doc id.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.connectors import Connector, ConnectorRegistry, load_registry  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402
from flowagent.tools.workspace import build_workspace_tools  # noqa: E402

POSTS: list[dict] = []

PAGE = """<!doctype html>
<html><head><title>恒星智算补充材料</title>
<script>window.secret = "not evidence";</script></head>
<body><h1>FY2025 桥接说明</h1>
<p>调整后毛利 196 百万元，桥接后为 168 百万元。</p>
<p>毛利率为 15.00%。</p></body></html>"""


class _Source(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - silence test output
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        POSTS.append({"path": path, "body": body, "auth": self.headers.get("Authorization", "")})
        if path == "/crawl":
            target = (body.get("urls") or [""])[0]
            payload = {
                "success": True,
                "results": [{
                    "url": target,
                    "success": True,
                    "markdown": {"fit_markdown": "# Crawl 结果\n\n毛利率为 15.00%。"},
                    "metadata": {"title": "Crawl4AI 标题"},
                }],
            }
            return self._send(200, json.dumps(payload).encode(), "application/json")
        if path == "/crawl-fail":
            return self._send(200, json.dumps({
                "success": True,
                "results": [{"success": False, "error_message": "render blocked"}],
            }).encode(), "application/json")
        if path == "/crawl-bad":
            return self._send(200, json.dumps({"success": True}).encode(), "application/json")
        return self._send(404, b"{}", "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/page":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/data":
            return self._send(200, json.dumps({"margin_bp": 1500}).encode(), "application/json")
        if path == "/binary":
            return self._send(200, b"\x00\x01\x02", "application/octet-stream")
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if path == "/redirect-out":
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.end_headers()
            return
        if path == "/big":
            return self._send(200, b"x" * 5000, "text/plain")
        return self._send(404, b"{}", "application/json")


class CollectSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Source)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        POSTS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.base = f"http://127.0.0.1:{self.port}"
        self.registry = ConnectorRegistry([
            Connector(name="page", base_url=self.base + "/page", enabled=True, allow_private=True),
            Connector(name="data", base_url=self.base + "/data", enabled=True, allow_private=True),
            Connector(name="binary", base_url=self.base + "/binary", enabled=True, allow_private=True),
            Connector(name="redirect", base_url=self.base + "/redirect", enabled=True, allow_private=True),
            Connector(name="redirect-out", base_url=self.base + "/redirect-out", enabled=True, allow_private=True),
            Connector(name="big", base_url=self.base + "/big", enabled=True, allow_private=True, max_bytes=100),
            Connector(name="closed", base_url=self.base + "/page", enabled=False, allow_private=True),
            Connector(
                name="crawl",
                base_url=self.base + "/crawl",
                adapter="crawl4ai",
                target_url=self.base + "/page",
                enabled=True,
                allow_private=True,
                timeout=2,
                credentials=lambda: {"Authorization": "Bearer sidecar-secret"},
                crawler_config={"cache_mode": "bypass"},
            ),
            Connector(name="crawl-fail", base_url=self.base + "/crawl-fail", adapter="crawl4ai", target_url=self.base + "/page", enabled=True, allow_private=True),
            Connector(name="crawl-bad", base_url=self.base + "/crawl-bad", adapter="crawl4ai", target_url=self.base + "/page", enabled=True, allow_private=True),
            Connector(name="crawl-big", base_url=self.base + "/crawl", adapter="crawl4ai", target_url=self.base + "/page", enabled=True, allow_private=True, max_bytes=10),
        ])
        self.evidence = EvidenceStore(self.root)
        self.tools = ToolRegistry(build_workspace_tools(
            self.evidence,
            ArtifactStore(self.root / "artifacts"),
            self.registry,
        ))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_collects_html_into_searchable_evidence(self) -> None:
        observation = self.tools.execute("collect_source", {"source": "page"})
        result = observation.result
        self.assertEqual(observation.tool_version, "1.0")
        self.assertEqual(result["title"], "恒星智算补充材料")
        self.assertIn("15.00%", result["preview"])
        self.assertNotIn("not evidence", result["preview"])
        self.assertTrue(result["doc_id"].startswith("evidence:"))
        self.assertEqual(result["kind"], "html")
        self.assertGreater(result["lines"], 2)

        hits = self.tools.execute("search_corpus", {"query": "15.00"}).result
        self.assertEqual(hits["hits"][0]["doc_id"], result["doc_id"])
        self.assertEqual(hits["hits"][0]["source"], "page")

        window = self.tools.execute("read_slice", {"doc_id": result["doc_id"], "start": 1}).result
        self.assertEqual(window["metadata"]["source"], "page")
        self.assertIn("http://127.0.0.1", window["metadata"]["url"])
        self.assertTrue(any("15.00%" in line["text"] for line in window["lines"]))

    def test_disabled_source_is_refused(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "closed"})
        self.assertEqual(caught.exception.code, "CONNECTOR_DISABLED")

    def test_binary_payload_is_refused(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "binary"})
        self.assertEqual(caught.exception.code, "CONNECTOR_CONTENT_TYPE")

    def test_same_host_redirect_is_followed(self) -> None:
        result = self.tools.execute("collect_source", {"source": "redirect"}).result
        self.assertEqual(result["title"], "恒星智算补充材料")

    def test_cross_host_redirect_is_refused(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "redirect-out"})
        self.assertEqual(caught.exception.code, "CONNECTOR_REDIRECT_DENIED")

    def test_private_host_requires_explicit_opt_in(self) -> None:
        registry = ConnectorRegistry([
            Connector(name="local", base_url=self.base.replace("http://", "https://") + "/page", enabled=True, allow_private=False),
        ])
        with self.assertRaises(AgentError) as caught:
            registry.collect("local")
        self.assertEqual(caught.exception.code, "CONNECTOR_HOST_DENIED")

    def test_size_cap_is_enforced(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "big"})
        self.assertEqual(caught.exception.code, "CONNECTOR_TOO_LARGE")

    def test_json_is_pretty_printed(self) -> None:
        result = self.tools.execute("collect_source", {"source": "data"}).result
        self.assertEqual(result["kind"], "json")
        window = self.tools.execute("read_slice", {"doc_id": result["doc_id"]}).result
        self.assertIn("margin_bp", "\n".join(line["text"] for line in window["lines"]))

    def test_crawl4ai_sidecar_becomes_evidence(self) -> None:
        result = self.tools.execute("collect_source", {"source": "crawl"}).result
        self.assertEqual(result["adapter"], "crawl4ai")
        self.assertEqual(result["title"], "Crawl4AI 标题")
        self.assertEqual(result["kind"], "markdown")
        self.assertIn("15.00%", result["preview"])
        self.assertTrue(result["doc_id"].startswith("evidence:"))
        self.assertEqual(POSTS[-1]["auth"], "Bearer sidecar-secret")
        self.assertEqual(POSTS[-1]["body"]["crawler_config"]["params"]["cache_mode"], "bypass")
        self.assertNotIn("sidecar-secret", json.dumps(result, ensure_ascii=False))
        hits = self.tools.execute("search_corpus", {"query": "15.00"}).result
        self.assertEqual(hits["hits"][0]["source"], "crawl")
        window = self.tools.execute("read_slice", {"doc_id": result["doc_id"]}).result
        self.assertEqual(window["metadata"]["adapter"], "crawl4ai")
        self.assertEqual(window["metadata"]["url"], self.base + "/page")

    def test_crawl4ai_failure_is_a_clean_error(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "crawl-fail"})
        self.assertEqual(caught.exception.code, "CONNECTOR_UPSTREAM_ERROR")

    def test_crawl4ai_text_cap_is_enforced(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "crawl-big"})
        self.assertEqual(caught.exception.code, "CONNECTOR_TOO_LARGE")

    def test_crawl4ai_bad_response_is_reported(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.tools.execute("collect_source", {"source": "crawl-bad"})
        self.assertEqual(caught.exception.code, "CONNECTOR_BAD_RESPONSE")

    def test_load_registry_reads_host_config(self) -> None:
        config = self.root / "sources.json"
        config.write_text(json.dumps({"sources": [{
            "name": "configured",
            "base_url": self.base + "/page",
            "enabled": True,
            "allow_private": True,
            "max_bytes": 4096,
            "token_env": "FCODE_TEST_TOKEN",
            "description": "fixture",
            "adapter": "crawl4ai",
            "target_url": self.base + "/page",
            "crawler_config": {"cache_mode": "bypass"},
        }]}), encoding="utf-8")
        registry = load_registry(config)
        self.assertEqual(registry.names(), ("configured",))
        source = registry.get("configured")
        self.assertTrue(source.enabled)
        self.assertEqual(source.adapter, "crawl4ai")
        self.assertEqual(source.target_url, self.base + "/page")
        self.assertEqual(source.crawler_config, {"cache_mode": "bypass"})


if __name__ == "__main__":
    unittest.main()

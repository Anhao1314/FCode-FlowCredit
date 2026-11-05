"""Network effects: host-owned endpoints, lazy credentials, no hidden retries.

A real loopback HTTP server is used on purpose: the point is to exercise the
actual transport path (timeouts, HTTP errors, size caps), not a stub that
returns whatever the test expects.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.artifacts import ArtifactStore  # noqa: E402
from flowagent.tools.connectors import Connector, ConnectorRegistry, default_registry  # noqa: E402
from flowagent.tools.evidence import EvidenceStore  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402

HITS: collections.Counter = collections.Counter()


class _Feed(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - silence test output
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        HITS[parsed.path] += 1
        if parsed.path == "/feed":
            key = urllib.parse.parse_qs(parsed.query).get("key", [None])[0]
            body = json.dumps({
                "ok": True,
                "key": key,
                # echo only presence: the connector must never forward
                # credential material into the observation itself
                "auth_present": bool(self.headers.get("Authorization")),
            }).encode()
            return self._send(200, body)
        if parsed.path == "/slow":
            time.sleep(1.2)
            return self._send(200, b'{"ok": true}')
        if parsed.path == "/big":
            return self._send(200, b"x" * (300 * 1024))
        if parsed.path == "/fail":
            return self._send(500, b'{"error": "boom"}')
        if parsed.path == "/html":
            return self._send(200, b"<html>hello</html>", "text/html")
        return self._send(404, b"{}")


class ConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Feed)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        HITS.clear()
        base = f"http://127.0.0.1:{self.port}"
        self.registry = ConnectorRegistry([
            Connector(
                name="feed",
                base_url=base + "/feed",
                enabled=True,
                timeout=0.5,
                credentials=lambda: {"Authorization": "Bearer secret-token"},
            ),
            Connector(name="slow", base_url=base + "/slow", enabled=True, timeout=0.15),
            Connector(name="big", base_url=base + "/big", enabled=True),
            Connector(name="fail", base_url=base + "/fail", enabled=True),
            Connector(name="html", base_url=base + "/html", enabled=True),
            Connector(name="closed", base_url=base + "/feed", enabled=False),
        ])
        self.registry_tools = ToolRegistry(
            build_finance_tools(
                FinanceWorld(),
                EvidenceStore(pathlib.Path(self.server.server_address[0])),
                ArtifactStore(pathlib.Path("./.test-artifacts")),
                self.registry,
            )
        )

    def test_fetch_records_provenance_and_hides_credentials(self) -> None:
        observation = self.registry_tools.execute("fetch_source", {"source": "feed", "key": "FY2025"})
        result = observation.result
        self.assertEqual(result["status"], 200)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(result["payload"]["json"]["key"], "FY2025")
        # the server really received the credential...
        self.assertTrue(result["payload"]["json"]["auth_present"])
        # ...but it must not appear in the observation that goes into the chain
        self.assertNotIn("secret-token", json.dumps(result, ensure_ascii=False))
        self.assertEqual(observation.tool_version, "1.0")

    def test_disabled_connector_is_refused(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.fetch("closed")
        self.assertEqual(ctx.exception.code, "CONNECTOR_DISABLED")

    def test_timeout_is_unknown_and_not_retried(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.fetch("slow")
        self.assertEqual(ctx.exception.code, "CONNECTOR_TIMEOUT")
        self.assertEqual(HITS["/slow"], 1)  # exactly one attempt

    def test_http_error_is_reported_without_retry(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.fetch("fail")
        self.assertEqual(ctx.exception.code, "CONNECTOR_HTTP_ERROR")
        self.assertEqual(HITS["/fail"], 1)

    def test_oversized_response_is_refused(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry.fetch("big")
        self.assertEqual(ctx.exception.code, "CONNECTOR_TOO_LARGE")

    def test_non_json_payload_still_has_a_shape(self) -> None:
        result = self.registry.fetch("html")
        self.assertIn("text", result["payload"])
        self.assertIn("hello", result["payload"]["text"])

    def test_model_can_only_name_configured_sources(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.registry_tools.execute("fetch_source", {"source": "https://evil.example", "key": "x"})
        self.assertEqual(ctx.exception.code, "TOOL_ARGUMENTS_INVALID")

    def test_default_catalog_is_deterministic_and_network_is_closed(self) -> None:
        registry = default_registry()
        self.assertEqual(registry.names(), ("demo-feed",))
        with self.assertRaises(AgentError) as ctx:
            registry.fetch("demo-feed")
        self.assertEqual(ctx.exception.code, "CONNECTOR_DISABLED")
        tools = ToolRegistry(build_finance_tools(FinanceWorld()))
        spec = tools.get("fetch_source")
        self.assertEqual(spec.effect, "network")
        self.assertEqual(spec.params[0].enum, ("demo-feed",))

    def test_demo_policy_allows_the_tool(self) -> None:
        from flowagent.demo.fixtures import get_scenario
        scenario = get_scenario("full")
        self.assertIn("collect_source", scenario.allowed_tools)
        self.assertIn("fetch_source", scenario.allowed_tools)
        self.assertIn("collect_source", scenario.grant["allowed_tools"])


if __name__ == "__main__":
    unittest.main()

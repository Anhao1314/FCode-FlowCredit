"""The MCP bridge: import, classify, call — and fail honestly.

Every test launches the deterministic fake server (``fixtures/mcp_fake_server.py``)
as a real subprocess, so the JSON-RPC framing, the reader thread and the
process-death paths are exercised exactly as they run in production. No
network is touched; stdio is the whole transport.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.base import validate_arguments  # noqa: E402
from flowagent.tools.mcp.bridge import McpBridge, params_from_schema, tool_full_name  # noqa: E402
from flowagent.tools.mcp.client import McpStdioClient  # noqa: E402
from flowagent.tools.mcp.config import McpServerConfig, load_mcp_config  # noqa: E402

FAKE_SERVER = pathlib.Path(__file__).resolve().parent / "fixtures" / "mcp_fake_server.py"


def _server_config(name: str = "fake", **overrides) -> McpServerConfig:
    defaults = dict(
        name=name,
        command=sys.executable,
        args=(str(FAKE_SERVER),),
        timeout=5.0,
    )
    defaults.update(overrides)
    return McpServerConfig(**defaults)


class McpConfigTests(unittest.TestCase):
    def test_missing_file_means_no_servers(self) -> None:
        self.assertEqual(load_mcp_config("/nonexistent/mcp.json"), {})

    def test_servers_are_loaded_with_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mcp.json"
            path.write_text(json.dumps({
                "servers": {
                    "weather": {
                        "command": "python3",
                        "args": ["-m", "weather"],
                        "readonlyTools": ["get_forecast"],
                        "env": {"TOKEN": "env:WEATHER_TOKEN"},
                    }
                }
            }), encoding="utf-8")
            config = load_mcp_config(path)["weather"]
        self.assertEqual(config.readonly_tools, ("get_forecast",))
        self.assertEqual(config.env["TOKEN"], "env:WEATHER_TOKEN")

    def test_bad_names_and_shapes_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mcp.json"
            path.write_text(json.dumps({"servers": {"Bad Name": {"command": "x"}}}), encoding="utf-8")
            with self.assertRaises(AgentError) as ctx:
                load_mcp_config(path)
            self.assertEqual(ctx.exception.code, "MCP_CONFIG_INVALID")


class McpClientTests(unittest.TestCase):
    def test_handshake_and_tool_roundtrip(self) -> None:
        client = McpStdioClient(_server_config())
        try:
            client.start()
            self.assertTrue(client.alive())
            self.assertEqual(client.server_info.get("name"), "fake")
            tools = client.list_tools()
            self.assertEqual({tool["name"] for tool in tools},
                             {"echo", "add", "blob", "slow", "fail", "die"})
            result = client.call_tool("echo", {"text": "hi"})
            self.assertEqual(result["content"][0]["text"], "echo: hi")
        finally:
            client.stop()

    def test_unlaunchable_command_is_a_confirmed_connect_failure(self) -> None:
        client = McpStdioClient(_server_config(command="/nonexistent/binary"))
        with self.assertRaises(AgentError) as ctx:
            client.start()
        self.assertEqual(ctx.exception.code, "MCP_CONNECT_FAILED")


class McpBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config_path = pathlib.Path(self._tmp.name) / "mcp.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bridge(self, **entry_overrides) -> McpBridge:
        entry = {
            "command": sys.executable,
            "args": [str(FAKE_SERVER)],
            "timeout": 5.0,
            **entry_overrides,
        }
        self.config_path.write_text(json.dumps({"servers": {"fake": entry}}), encoding="utf-8")
        bridge = McpBridge(self.config_path)
        bridge.connect()
        return bridge

    def test_tools_import_under_namespaced_names_with_host_classification(self) -> None:
        bridge = self._bridge(readonlyTools=["echo"])
        try:
            statuses = bridge.statuses()
            self.assertTrue(statuses[0]["connected"])
            imported = set(statuses[0]["tools"])
            # blob's object parameter cannot be validated by the gate: refused,
            # visibly, instead of silently widened.
            self.assertEqual(imported, {"mcp__fake__echo", "mcp__fake__add", "mcp__fake__slow",
                                        "mcp__fake__fail", "mcp__fake__die"})
            refused = {item["tool"]: item["reason"] for item in statuses[0]["refused"]}
            self.assertIn("blob", refused)

            echo = bridge.specs_map()["mcp__fake__echo"]
            self.assertEqual(echo.effect, "read")  # host said readonly
            add = bridge.specs_map()["mcp__fake__add"]
            self.assertEqual(add.effect, "write")  # default consequential
            self.assertEqual(bridge.approval_names(),
                             ("mcp__fake__add", "mcp__fake__slow", "mcp__fake__fail", "mcp__fake__die"))
        finally:
            bridge.disconnect()

    def test_imported_schema_is_enforced_by_the_same_gate_validator(self) -> None:
        bridge = self._bridge()
        try:
            add = bridge.specs_map()["mcp__fake__add"]
            self.assertEqual(validate_arguments(add, {"a": 1, "b": 2}), {"a": 1, "b": 2})
            with self.assertRaises(AgentError) as ctx:
                validate_arguments(add, {"a": 1, "b": 2, "extra": True})
            self.assertEqual(ctx.exception.code, "TOOL_ARGUMENTS_INVALID")
            with self.assertRaises(AgentError):
                validate_arguments(add, {"a": "one", "b": 2})
        finally:
            bridge.disconnect()

    def test_call_returns_receipt_text_and_latency(self) -> None:
        bridge = self._bridge()
        try:
            result = bridge.call("fake", "add", {"a": 20, "b": 22})
            self.assertEqual(result["text"], "42")
            self.assertFalse(result["is_error"])
            self.assertGreaterEqual(result["latency_ms"], 0)
        finally:
            bridge.disconnect()

    def test_server_side_iserror_is_a_confirmed_tool_failure(self) -> None:
        bridge = self._bridge()
        try:
            with self.assertRaises(AgentError) as ctx:
                bridge.call("fake", "fail", {"message": "nope"})
            self.assertEqual(ctx.exception.code, "MCP_TOOL_ERROR")
            self.assertIn("nope", ctx.exception.message)
        finally:
            bridge.disconnect()

    def test_call_timeout_is_an_unknown_outcome(self) -> None:
        bridge = self._bridge(timeout=0.6)
        try:
            with self.assertRaises(AgentError) as ctx:
                bridge.call("fake", "slow", {"seconds": 5})
            self.assertEqual(ctx.exception.code, "MCP_TIMEOUT")
            self.assertEqual(ctx.exception.details.get("outcome"), "unknown")
        finally:
            bridge.disconnect()

    def test_process_death_marks_the_server_unavailable_and_further_calls_refuse(self) -> None:
        bridge = self._bridge(timeout=0.8)
        try:
            with self.assertRaises(AgentError) as ctx:
                bridge.call("fake", "die", {})
            self.assertIn(ctx.exception.code, ("MCP_TIMEOUT", "MCP_SERVER_LOST"))
            self.assertEqual(ctx.exception.details.get("outcome"), "unknown")
            # Honest availability: the server is now reported lost, and a
            # later call fails pre-dispatch (confirmed, not unknown).
            status = next(s for s in bridge.statuses() if s["server"] == "fake")
            self.assertFalse(status["connected"])
            with self.assertRaises(AgentError) as ctx2:
                bridge.call("fake", "echo", {"text": "late"})
            self.assertEqual(ctx2.exception.code, "MCP_SERVER_UNAVAILABLE")
        finally:
            bridge.disconnect()

    def test_one_bad_server_does_not_hide_a_good_one(self) -> None:
        self.config_path.write_text(json.dumps({"servers": {
            "broken": {"command": "/nonexistent/binary"},
            "fake": {"command": sys.executable, "args": [str(FAKE_SERVER)], "timeout": 5.0},
        }}), encoding="utf-8")
        bridge = McpBridge(self.config_path)
        bridge.connect()
        try:
            statuses = {s["server"]: s for s in bridge.statuses()}
            self.assertFalse(statuses["broken"]["connected"])
            self.assertTrue(statuses["fake"]["connected"])
        finally:
            bridge.disconnect()


class SchemaMappingTests(unittest.TestCase):
    def test_supported_types_map_and_unsupported_refuse(self) -> None:
        params = params_from_schema({
            "type": "object",
            "required": ["q"],
            "properties": {
                "q": {"type": "string"},
                "n": {"type": "integer"},
                "f": {"type": "number"},
                "b": {"type": "boolean"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "mode": {"type": "string", "enum": ["fast", "slow"]},
            },
        })
        assert params is not None
        kinds = {param.name: (param.kind, param.required) for param in params}
        self.assertEqual(kinds["q"], ("string", True))
        self.assertEqual(kinds["n"], ("integer", False))
        self.assertEqual(kinds["b"], ("boolean", False))
        self.assertEqual(params[-1].enum, ("fast", "slow"))
        self.assertIsNone(params_from_schema({"type": "object", "properties": {"o": {"type": "object"}}}))
        self.assertIsNone(params_from_schema({"oneOf": []}))
        self.assertIsNone(params_from_schema({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "number"}}}}))


if __name__ == "__main__":
    unittest.main()

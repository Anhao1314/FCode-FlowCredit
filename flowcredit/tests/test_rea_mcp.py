"""The flat-schema REA MCP wrapper, tested through FCode's own client.

No REA installation is needed: a fake CLI emits the same Evidence shape.
The bridge import is the load-bearing assertion — FCode refuses nested
schemas, so all four wrapper tools must survive `params_from_schema`.
"""

from __future__ import annotations

import json
import pathlib
import stat
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.tools.mcp.bridge import McpBridge  # noqa: E402
from flowagent.tools.mcp.client import McpStdioClient  # noqa: E402
from flowagent.tools.mcp.config import McpServerConfig  # noqa: E402

EVIDENCE_GO = {
    "evidence_id": "ev_" + "a" * 64,
    "operation": "inspect_go_binary",
    "subject": {"name": "demo", "digest": {"sha256": "a" * 64}, "local_path": "/tmp/demo"},
    "provider": {"id": "go-build-info", "name": "REA Go reader", "version": "1"},
    "confidence": "observed",
    "authority": "shipped-artifact",
    "normalized_result": {
        "format": "macho",
        "architecture": "arm64",
        "artifact": {"sha256": "a" * 64, "bytes": 12, "format": "macho", "architecture": "arm64"},
        "build_info": {
            "go_version": "go1.26.10",
            "encoding": "inline",
            "module": {
                "path": "example/app",
                "main": {"path": "example/app", "version": "(devel)"},
                "dependencies": [{"path": "example/rc", "version": "v1.0.0-rc1"}],
                "settings": [{"key": "-tags", "value": "production"}],
                "complete": True,
            },
        },
    },
    "limitations": [],
}


def _fake_rea(path: pathlib.Path) -> pathlib.Path:
    script = path / "rea-fake"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('9.9.9')\n"
        "    raise SystemExit(0)\n"
        "print(%r)\n" % json.dumps(EVIDENCE_GO),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


class ReaMcpTests(unittest.TestCase):
    def test_stdio_client_lists_flat_tools_and_calls_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = _fake_rea(root)
            target = root / "demo.bin"
            target.write_bytes(b"demo")
            config = McpServerConfig(
                name="rea",
                command=sys.executable,
                args=("-m", "flowaudit.rea.mcp"),
                env={"PYTHONPATH": str(ROOT / "src"), "REA_BIN": str(binary)},
                cwd=str(ROOT),
                timeout=30,
            )
            client = McpStdioClient(config, cwd=ROOT)
            client.start()
            try:
                tools = client.list_tools()
                names = {tool["name"] for tool in tools}
                self.assertEqual(names, {
                    "inspect_go_binary", "inspect_desktop_application",
                    "analyze_javascript_application", "inspect_library_validation",
                })
                for tool in tools:
                    schema = tool["inputSchema"]
                    self.assertEqual(schema["type"], "object")
                    self.assertEqual(list(schema["properties"]), ["path"])
                result = client.call_tool("inspect_go_binary", {"path": str(target)})
                text = result["content"][0]["text"]
                self.assertIn(EVIDENCE_GO["evidence_id"], text)
                self.assertIn("go1.26.10", text)
            finally:
                client.stop()

    def test_bridge_imports_wrapper_tools_and_calls_through_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = _fake_rea(root)
            target = root / "demo.bin"
            target.write_bytes(b"demo")
            config_path = root / "mcp.json"
            config_path.write_text(json.dumps({
                "servers": {
                    "rea": {
                        "command": sys.executable,
                        "args": ["-m", "flowaudit.rea.mcp"],
                        "cwd": str(ROOT),
                        "env": {"PYTHONPATH": str(ROOT / "src"), "REA_BIN": str(binary)},
                        "readonlyTools": [
                            "inspect_go_binary", "inspect_desktop_application",
                            "analyze_javascript_application", "inspect_library_validation",
                        ],
                        "timeout": 30,
                    },
                },
            }), encoding="utf-8")
            bridge = McpBridge(config_path, cwd=ROOT)
            bridge.connect()
            try:
                specs = {spec.name: spec for spec in bridge.specs()}
                self.assertIn("mcp__rea__inspect_go_binary", specs)
                self.assertIn("mcp__rea__inspect_library_validation", specs)
                self.assertEqual(bridge.approval_names(), ())
                result = bridge.call("rea", "inspect_go_binary", {"path": str(target)})
                self.assertIn(EVIDENCE_GO["evidence_id"], result["text"])
                self.assertFalse(result["is_error"])
            finally:
                bridge.disconnect()


if __name__ == "__main__":
    unittest.main()
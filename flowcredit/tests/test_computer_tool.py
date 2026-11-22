"""Computer-use policy, session and tool contract (no real GUI)."""

from __future__ import annotations

import json
import pathlib
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.computer import (  # noqa: E402
    MAX_SCREENSHOTS,
    AppTarget,
    ComputerPolicy,
    ComputerRuntime,
    ComputerSessionManager,
    build_computer_tools,
)
from flowagent.tools.registry import ToolRegistry  # noqa: E402


def png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xff" * 30 for _ in range(10))  # 10x10 white RGB
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 10, 10, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


class FakeSidecar:
    def __init__(self, name: str, secure_regions: list[dict] | None = None) -> None:
        self.name = name
        self.calls: list[dict] = []
        self.secure_regions = list(secure_regions or [])

    def availability(self):
        return True, ""

    def call(self, payload):
        self.calls.append(dict(payload))
        command = payload.get("command")
        if command == "release":
            return {"ok": True, "released": True}, None
        if command == "ping":
            return {"ok": True, "accessibility": True, "screen_recording": True}, None
        if command == "look":
            for key in ("screenshot_path", "ax_path", "dom_path"):
                value = payload.get(key)
                if value:
                    pathlib.Path(value).parent.mkdir(parents=True, exist_ok=True)
                    pathlib.Path(value).write_bytes(
                        png_bytes() if value.endswith(".png") else b'{"elements": []}')
            return {
                "ok": True, "app": {"bundle_id": "com.apple.TextEdit"},
                "window": {"title": "demo", "x": 0, "y": 0, "width": 800, "height": 600},
                "elements": [{"id": "0/1", "role": "AXButton", "title": "Save",
                              "x": 10, "y": 20, "width": 40, "height": 20}],
                "text": "demo", "secure_regions": self.secure_regions,
                "width": 800, "height": 600,
                "url": "https://example.com/", "title": "Example",
                "dom_diff": "",
            }, None
        if command == "act":
            for key in ("before_path", "after_path"):
                value = payload.get(key)
                if value:
                    pathlib.Path(value).parent.mkdir(parents=True, exist_ok=True)
                    pathlib.Path(value).write_bytes(png_bytes())
            return {"ok": True, "target": "Save", "width": 800, "height": 600,
                    "text": "demo", "dom_diff": "", "duration_ms": 12}, None
        return None, {"code": "COMPUTER_COMMAND_INVALID", "message": "bad"}

    def close(self) -> None:
        self.calls.append({"command": "close"})


class PolicyTests(unittest.TestCase):
    def test_config_loads_allowlists_and_denies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "computer.json"
            path.write_text(json.dumps({
                "allowed_apps": [{"bundle_id": "com.apple.TextEdit", "name": "TextEdit"}],
                "allowed_domains": ["example.com"],
                "denied_apps": ["com.apple.Terminal"],
            }), encoding="utf-8")
            policy = ComputerPolicy.load(path)
        self.assertEqual(policy.app("TextEdit").bundle_id, "com.apple.TextEdit")
        self.assertIsNone(policy.app("com.apple.Terminal"))
        self.assertTrue(policy.domain_allowed("docs.example.com"))
        self.assertFalse(policy.domain_allowed("example.org"))

    def test_scope_validates_mode_apps_domains_and_bounds(self) -> None:
        policy = ComputerPolicy(
            allowed_apps=(AppTarget("com.apple.TextEdit", "TextEdit"),),
            allowed_domains=("example.com",),
        )
        manager = ComputerSessionManager(policy, clock=lambda: 1000)
        session = manager.request({
            "mode": "both", "apps": ["TextEdit"], "domains": ["example.com"],
            "duration_seconds": 60, "max_actions": 2, "allow_destructive": False,
        })
        self.assertEqual(session["actions"], 0)
        self.assertEqual(manager.session.mode, "both")
        manager.count_action()
        manager.count_action()
        with self.assertRaises(AgentError) as ctx:
            manager.count_action()
        self.assertEqual(ctx.exception.code, "COMPUTER_ACTION_LIMIT")
        manager.stop()
        self.assertIsNone(manager.session)

    def test_unknown_app_or_domain_is_refused(self) -> None:
        policy = ComputerPolicy(
            allowed_apps=(AppTarget("com.apple.TextEdit", "TextEdit"),),
            allowed_domains=("example.com",),
        )
        manager = ComputerSessionManager(policy)
        with self.assertRaises(AgentError) as app_error:
            manager.request({"mode": "desktop", "apps": ["com.apple.Terminal"]})
        self.assertEqual(app_error.exception.code, "COMPUTER_APP_DENIED")
        with self.assertRaises(AgentError) as domain_error:
            manager.request({"mode": "browser", "domains": ["evil.com"]})
        self.assertEqual(domain_error.exception.code, "COMPUTER_DOMAIN_DENIED")

    def test_expired_session_is_gone(self) -> None:
        now = [1000]
        policy = ComputerPolicy(
            allowed_apps=(AppTarget("com.apple.TextEdit", "TextEdit"),),
            allowed_domains=(),
        )
        manager = ComputerSessionManager(policy, clock=lambda: now[0])
        manager.request({"mode": "desktop", "apps": ["TextEdit"], "duration_seconds": 30})
        now[0] = 5000
        self.assertIsNone(manager.session)


class ToolTests(unittest.TestCase):
    def _runtime(self, tmp: str, secure_regions: list[dict] | None = None) -> ComputerRuntime:
        app = AppTarget("com.apple.TextEdit", "TextEdit")
        policy = ComputerPolicy(
            allowed_apps=(app,), allowed_domains=("example.com",),
        )
        runtime = ComputerRuntime(
            artifact_root=pathlib.Path(tmp) / "artifacts",
            run_id="run-test",
            policy=policy,
            viewer_dir=pathlib.Path(tmp),
        )
        runtime.desktop = FakeSidecar("desktop", secure_regions)
        runtime.browser = FakeSidecar("browser")
        return runtime

    def test_tools_require_a_session_then_act_and_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_look", {})
            self.assertEqual(ctx.exception.code, "COMPUTER_SESSION_REQUIRED")
            requested = registry.execute("computer_request", {
                "mode": "desktop", "apps": ["TextEdit"], "duration_seconds": 60,
                "max_actions": 3,
            }).result
            self.assertTrue(requested["ok"])
            looked = registry.execute("computer_look", {"mode": "desktop"}).result
            self.assertTrue(looked["ok"])
            self.assertEqual(looked["elements"][0]["role"], "AXButton")
            self.assertEqual(looked["screenshot"]["bytes"] > 0, True)
            acted = registry.execute("computer_act", {
                "action": "click_element", "element_id": "0/1",
            }).result
            self.assertTrue(acted["ok"])
            self.assertEqual(acted["action"], "click_element")
            stopped = registry.execute("computer_stop", {}).result
            self.assertTrue(stopped["stopped"])

    def test_coordinate_click_needs_destructive_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "desktop", "apps": ["TextEdit"], "duration_seconds": 60,
            })
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_act", {
                    "action": "click_xy", "x": 10, "y": 20,
                })
            self.assertEqual(ctx.exception.code, "COMPUTER_DESTRUCTIVE_BLOCKED")
            # "click" without an element id is the same coordinate click.
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_act", {"action": "click", "x": 10, "y": 20})
            self.assertEqual(ctx.exception.code, "COMPUTER_DESTRUCTIVE_BLOCKED")

    def test_coordinate_click_needs_both_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "desktop", "apps": ["TextEdit"], "duration_seconds": 60,
                "allow_destructive": True,
            })
            for arguments in ({"action": "click", "x": 10},
                              {"action": "click_xy", "y": 20},
                              {"action": "click_xy"}):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(AgentError) as ctx:
                        registry.execute("computer_act", arguments)
                    self.assertEqual(ctx.exception.code, "COMPUTER_ACTION_INVALID")

    def test_actions_validate_their_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "both", "apps": ["TextEdit"], "domains": ["example.com"],
            })
            cases = (
                ({"action": "click_element"}, "COMPUTER_ACTION_INVALID"),
                ({"action": "type", "element_id": "0/1"}, "COMPUTER_ACTION_INVALID"),
                ({"action": "press", "element_id": "0/1"}, "COMPUTER_ACTION_INVALID"),
                ({"action": "scroll"}, "COMPUTER_ACTION_INVALID"),
                ({"action": "click", "selector": "#go"}, None),
            )
            for arguments, code in cases:
                with self.subTest(arguments=arguments):
                    if code is None:
                        result = registry.execute("computer_act", arguments).result
                        self.assertTrue(result["ok"])
                        self.assertEqual(result["mode"], "browser")
                        continue
                    with self.assertRaises(AgentError) as ctx:
                        registry.execute("computer_act", arguments)
                    self.assertEqual(ctx.exception.code, code)

    def test_browser_action_needs_a_selector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "browser", "domains": ["example.com"],
            })
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_act", {"action": "click"})
            self.assertEqual(ctx.exception.code, "COMPUTER_ACTION_INVALID")
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_act", {
                    "action": "scroll", "selector": "#list",
                })
            self.assertEqual(ctx.exception.code, "COMPUTER_ACTION_INVALID")

    def test_desktop_session_rejects_browser_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "desktop", "apps": ["TextEdit"], "allow_destructive": True,
            })
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_act", {"action": "click", "selector": "#go"})
            self.assertEqual(ctx.exception.code, "COMPUTER_ACTION_INVALID")

    def test_look_limit_stops_runaway_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "desktop", "apps": ["TextEdit"], "duration_seconds": 60,
            })
            runtime.sessions.session.looks = MAX_SCREENSHOTS
            with self.assertRaises(AgentError) as ctx:
                registry.execute("computer_look", {"mode": "desktop"})
            self.assertEqual(ctx.exception.code, "COMPUTER_LOOK_LIMIT")

    def test_secure_field_regions_are_blacked_out(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(
                tmp, secure_regions=[{"x": 0, "y": 0, "width": 400, "height": 300}])
            registry = ToolRegistry(build_computer_tools(runtime))
            registry.execute("computer_request", {
                "mode": "desktop", "apps": ["TextEdit"], "duration_seconds": 60,
            })
            looked = registry.execute("computer_look", {"mode": "desktop"}).result
            self.assertEqual(looked["redacted_regions"], 1)
            path = runtime.artifact_root / looked["screenshot"]["path"]
            with Image.open(path) as image:
                self.assertEqual(image.getpixel((1, 1))[:3], (0, 0, 0))
                self.assertEqual(image.getpixel((9, 9))[:3], (255, 255, 255))

    def test_redaction_failure_drops_the_screenshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(
                tmp, secure_regions=[{"x": 0, "y": 0, "width": 400, "height": 300}])
            path = runtime.artifact_root / "bad.png"
            runtime.artifact_root.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not a png")
            self.assertEqual(runtime._redact(path, [{"width": 10, "height": 10}], {}), -1)
            self.assertFalse(path.exists())

    def test_general_tools_only_gain_computer_when_enabled(self) -> None:
        from flowagent.tools.general.fetch import UrlFetcher
        from flowagent.tools.general.sandbox import WorkspaceSandbox
        from flowagent.tools.general.tools import build_general_tools

        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp))
            default = build_general_tools(sandbox, UrlFetcher())
            self.assertNotIn("computer_request", [spec.name for spec in default])
            runtime = self._runtime(tmp)
            extended = build_general_tools(sandbox, UrlFetcher(), computer=runtime)
            names = [spec.name for spec in extended]
            for name in ("computer_request", "computer_look", "computer_act", "computer_stop"):
                self.assertIn(name, names)

    def test_results_are_chain_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(tmp)
            registry = ToolRegistry(build_computer_tools(runtime))
            result = registry.execute("computer_request", {
                "mode": "both", "apps": ["TextEdit"], "domains": ["example.com"],
            }).result

            def walk(value):
                if isinstance(value, float):
                    self.fail(f"float in tool result: {value}")
                if isinstance(value, dict):
                    for item in value.values():
                        walk(item)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        walk(item)

            walk(result)


if __name__ == "__main__":
    unittest.main()

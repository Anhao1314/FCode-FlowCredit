"""agent-browser backend: the same browser contract on Vercel's agent CLI.

The Playwright sidecar stays the default; ``FCODE_BROWSER_BACKEND=agent-browser``
swaps in a runner that shells out to ``agent-browser`` — pages are read as
compact accessibility-tree snapshots with element refs, and actions take a ref.
What must not change: the result contract, the host-owned loopback URL gate,
and honest failures (a covered element refuses the click instead of faking it).

CLI-backed tests auto-skip when the binary is absent.
"""

from __future__ import annotations

import http.server
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.agent_browser import (  # noqa: E402
    AgentBrowserRunner,
    parse_snapshot,
)
from flowagent.tools.general.browser import browser_artifact_dir  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402
from flowagent.tools.general.fetch import UrlFetcher  # noqa: E402

PAGE = """<!doctype html><html><head><title>ABSmoke</title></head><body>
<h1>代理浏览器冒烟</h1>
<input id="name" placeholder="名字">
<button id="go" onclick="document.getElementById('out').textContent='点了'">点我</button>
<div id="out"></div>
</body></html>"""


class _LoopbackPage(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: N802 — quiet
        pass


class ParseSnapshotTests(unittest.TestCase):
    def test_parses_roles_names_refs_and_state(self) -> None:
        elements = parse_snapshot(
            '- checkbox "美观输出" [checked=false, ref=e1]\n'
            '- button "发送" [ref=e12]\n'
            '- heading "任务详情" [level=1, ref=e7]\n'
            "  - nested text without ref\n"
            '- textbox "名字" [ref=e3]\n'
        )
        self.assertEqual(
            elements,
            [
                {"role": "checkbox", "name": "美观输出", "ref": "e1", "checked": False},
                {"role": "button", "name": "发送", "ref": "e12"},
                {"role": "heading", "name": "任务详情", "ref": "e7", "level": 1},
                {"role": "textbox", "name": "名字", "ref": "e3"},
            ],
        )
        # 无 ref 的行（纯展示节点）如实跳过，不造 ref。
        self.assertEqual(len(elements), 4)

    def test_caps_elements_and_ignores_noise(self) -> None:
        text = "\n".join(f'- button "b{i}" [ref=e{i}]' for i in range(30))
        self.assertEqual(len(parse_snapshot(text, max_elements=5)), 5)
        self.assertEqual(parse_snapshot("random log line"), [])


class BackendSwitchTests(unittest.TestCase):
    def test_env_switch_selects_agent_browser_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
            sandbox.ensure()
            with mock.patch.dict(
                "os.environ", {"FCODE_BROWSER_BACKEND": "agent-browser"}
            ):
                specs = build_general_tools(sandbox, UrlFetcher())
            # runner 不可直接从闭包读取；走 availability 文案分辨两种后端。
            inspect = {spec.name: spec for spec in specs}["browser_inspect"]
            with mock.patch("shutil.which", return_value=None):
                result = inspect.handler({"url": "http://127.0.0.1:1/x"})
            self.assertFalse(result["available"])
            self.assertIn("agent-browser", result["reason"])

    def test_default_backend_stays_playwright(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
            sandbox.ensure()
            with mock.patch.dict("os.environ", {"FCODE_BROWSER_BACKEND": ""}):
                specs = build_general_tools(sandbox, UrlFetcher())
            inspect = {spec.name: spec for spec in specs}["browser_inspect"]
            with mock.patch("shutil.which", return_value=None):
                result = inspect.handler({"url": "http://127.0.0.1:1/x"})
            self.assertFalse(result["available"])
            self.assertIn("node", result["reason"])


@unittest.skipUnless(shutil.which("agent-browser"), "agent-browser CLI not installed")
class AgentBrowserLiveTests(unittest.TestCase):
    """Real CLI against a real loopback page. Honest failures stay failures."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackPage)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        # 这组测试跑在 agent-browser 后端上；不设开关就会打到 Playwright。
        cls._env_patch = mock.patch.dict(
            "os.environ", {
                "FCODE_BROWSER_BACKEND": "agent-browser",
                "FCODE_BROWSER_DIR": tempfile.mkdtemp(prefix="ab-test-"),
            }
        )
        cls._env_patch.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls._env_patch.stop()

    @staticmethod
    def _swap_browser_dir():
        import os

        old = os.environ.get("FCODE_BROWSER_DIR")
        os.environ["FCODE_BROWSER_DIR"] = tempfile.mkdtemp(prefix="ab-test-")
        return {"FCODE_BROWSER_DIR": old}

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.sandbox = WorkspaceSandbox(pathlib.Path(tmp) / "ws")
        self.sandbox.ensure()
        specs = {
            spec.name: spec
            for spec in build_general_tools(self.sandbox, UrlFetcher())
        }
        self.inspect = specs["browser_inspect"].handler
        self.act = specs["browser_act"].handler

    def test_inspect_returns_refs_text_and_screenshot(self) -> None:
        result = self.inspect({"url": self.url})
        self.assertTrue(result["available"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "ABSmoke")
        self.assertTrue(result["url"].startswith("http://127.0.0.1:"))
        roles = {element["role"] for element in result["elements"]}
        self.assertIn("button", roles)
        self.assertIn("textbox", roles)
        self.assertTrue(result["screenshot"] and result["screenshot"]["bytes"] > 0)
        self.assertTrue(result["text"], "snapshot text must not be empty")

    def test_click_fill_press_on_refs(self) -> None:
        seen = self.inspect({"url": self.url})
        ref_button = next(
            element["ref"] for element in seen["elements"] if element["role"] == "button"
        )
        ref_input = next(
            element["ref"] for element in seen["elements"] if element["role"] == "textbox"
        )
        filled = self.act({
            "url": self.url, "action": "fill", "selector": f"@{ref_input}",
            "text": "FCode",
        })
        self.assertTrue(filled["ok"], filled.get("reason"))
        clicked = self.act({"url": self.url, "action": "click", "selector": f"@{ref_button}"})
        self.assertTrue(clicked["ok"], clicked.get("reason"))
        # 诚实证据：点击前后截图都在；fill 的值进了无障碍树。
        # （点击改的是纯 div 文本，不进 a11y 树，前后快照相同是合理事实。）
        self.assertTrue(clicked["before"] and clicked["after"])
        self.assertIn("FCode", clicked["text"])

    def test_covered_element_fails_honestly(self) -> None:
        # API/health 页的 checkbox 被格式化层覆盖：点击必须失败并说明，不假成功。
        result = self.act({
            "url": "http://127.0.0.1:8642/api/health",
            "action": "click", "selector": "@e1",
        })
        self.assertFalse(result["ok"])
        self.assertIn("covered", result.get("reason", ""))

    def test_egress_is_denied_before_any_browser_call(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            self.act({"url": "https://example.com", "action": "click", "selector": "@e1"})
        self.assertEqual(ctx.exception.code, "BROWSER_URL_DENIED")


class SessionIsolationTests(unittest.TestCase):
    def test_per_workspace_session_names_differ(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one = AgentBrowserRunner(pathlib.Path(tmp) / "a")
            two = AgentBrowserRunner(pathlib.Path(tmp) / "b")
            self.assertNotEqual(one._session, two._session)
            self.assertTrue(one._session.startswith("fcode-"))

    def test_artifact_dir_is_per_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            one = AgentBrowserRunner(pathlib.Path(tmp) / "a")
            # artifact dir 是懒创建的：断言它落在宿主约定的位置，而不是碰运气断言已存在。
            self.assertTrue(str(one.artifact_root).endswith("fcode-browser"))
            one._artifact_dir()
            self.assertTrue(one.artifact_root.is_dir())


if __name__ == "__main__":
    unittest.main()

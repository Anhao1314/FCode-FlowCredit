"""Agentic chat: tool-calling transport, governed loop, projection.

Phase 1 scope: read-only tools (ls_workspace / read_file) inside a chat turn.
The tests cover the transport's tool_calls accumulation, the multi-round loop
with a scripted mock provider, the host gate (write tools refused), the chain
projection (blocks survive a reload) and the non-tool fallback.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import ChatService  # noqa: E402
from flowagent.chat_config import ChatProviderConfig  # noqa: E402
from flowagent.chat_provider import _RemoteProvider  # noqa: E402


def _service(tmp: pathlib.Path, workspace: pathlib.Path) -> ChatService:
    (workspace / "README.md").write_text("# Demo\nhello agent\n", encoding="utf-8")
    return ChatService(tmp / "chat.sqlite", None, workspace_root=workspace)


class ToolTransportTests(unittest.TestCase):
    """Fragment accumulation: id/name once, JSON arguments in pieces."""

    def test_stream_accumulates_tool_call_fragments(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                Handler.last_body = body
                frames = [
                    {"choices": [{"delta": {"content": "先看一下。"}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "id": "call_1",
                        "function": {"name": "read_file", "arguments": "{\"path\": \"RE"}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{
                        "index": 0, "function": {"arguments": "ADME.md\"}"}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                ]
                payload = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(payload.encode("utf-8"))

            def log_message(self, *args):  # silence the test server
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ChatProviderConfig(
                key="kimi", label="Kimi", model="test-model",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                supports_tools=True,
            )
            provider = _RemoteProvider(config)
            items = list(provider.stream([{"role": "user", "content": "hi"}],
                                         tools=[{"type": "function", "function": {"name": "read_file"}}]))
            deltas = [item["delta"] for item in items if "delta" in item]
            self.assertEqual(deltas, ["先看一下。"])
            calls = next(item["tool_calls"] for item in items if "tool_calls" in item)
            self.assertEqual(calls, [{"id": "call_1", "name": "read_file",
                                      "arguments": "{\"path\": \"README.md\"}"}])
            self.assertEqual(Handler.last_body.get("tools", [{}])[0]["function"]["name"], "read_file")
        finally:
            server.shutdown()
            server.server_close()


class AgentLoopTests(unittest.TestCase):
    def test_read_tool_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
            os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
                [{"name": "read_file", "arguments": {"path": "README.md"}}])
            try:
                events = list(service.send(thread_id=None, text="README 里有什么？", model="mock"))
            finally:
                if old is None:
                    os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                else:
                    os.environ["FCODE_CHAT_MOCK_TOOLS"] = old

            kinds = [event["type"] for event in events]
            self.assertIn("tool_call", kinds)
            self.assertIn("tool_result", kinds)
            call = next(e for e in events if e["type"] == "tool_call")
            self.assertEqual(call["name"], "read_file")
            result = next(e for e in events if e["type"] == "tool_result")
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["result_hash"])
            self.assertIn("hello agent", json.dumps(result["result"], ensure_ascii=False))

            done = next(e for e in events if e["type"] == "done")
            message = done["message"]
            self.assertTrue(any(b["type"] == "tool" for b in message["blocks"]))
            self.assertTrue(any(b["type"] == "text" for b in message["blocks"]))
            self.assertEqual([c["name"] for c in message["tool_calls"]], ["read_file"])

            # Replay (reload) keeps the tool blocks.
            snapshot = service.get_thread(done["thread_id"])
            last = snapshot["messages"][-1]
            self.assertEqual(len([b for b in last["blocks"] if b["type"] == "tool"]), 1)
            # Budget: one read per tool call, two model rounds.
            self.assertEqual(snapshot["budget"]["reads_used"], 1)
            self.assertEqual(snapshot["budget"]["calls_used"], 2)

            # Chain carries the tool evidence (append-only ledger).
            chain = service.store.events(done["thread_id"])
            types = [event["type"] for event in chain]
            self.assertIn("chat_tool_started", types)
            self.assertIn("chat_tool_result", types)

def _wait_pending(service, deadline=5.0):
    """等待一个待批调用出现，返回 (approval_id, thread_id)。模块级供各测试类共用。"""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        with service._approvals_cv:
            for approval_id, record in service._approvals.items():
                if record["decision"] is None:
                    return approval_id, record["thread_id"]
        time.sleep(0.05)
    raise AssertionError("no pending approval appeared")


    def test_write_tool_executes_after_approval(self) -> None:
        """写工具：consequential 档默认审批；允许后执行并落 git。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
            os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
                [{"name": "write_file",
                  "arguments": {"path": "x.txt", "content": "written by agent"}}])
            collected: list[dict] = []
            worker = threading.Thread(
                target=lambda: collected.extend(
                    service.send(thread_id=None, text="写个文件", model="mock")))
            try:
                worker.start()
                approval_id, thread_id = _wait_pending(service)
                decision = service.decide_tool(thread_id, approval_id, allow=True)
                self.assertEqual(decision["allow"], True)
                worker.join(10)
                self.assertFalse(worker.is_alive(), "允许后流应结束")

                kinds = [e["type"] for e in collected]
                self.assertIn("tool_approval", kinds)
                self.assertIn("tool_result", kinds)
                result = next(e for e in collected if e["type"] == "tool_result")
                self.assertEqual(result["status"], "ok")
                self.assertTrue((root / "ws" / "x.txt").exists(), "允许后写工具必须执行")
                # 链上有审批证据
                chain = [e["type"] for e in service.store.events(thread_id)]
                self.assertIn("chat_tool_approval", chain)
                self.assertIn("chat_tool_decision", chain)
            finally:
                if old is None:
                    os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                else:
                    os.environ["FCODE_CHAT_MOCK_TOOLS"] = old
                service.cancel_thread_approvals(thread_id if "thread_id" in dir() else "")

    def test_write_tool_denial_blocks_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
            os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
                [{"name": "write_file",
                  "arguments": {"path": "y.txt", "content": "should not land"}}])
            collected: list[dict] = []
            thread_box: dict[str, str] = {}
            worker = threading.Thread(target=lambda: [
                (thread_box.setdefault("thread_id", e.get("thread_id") or ""),
                 collected.append(e))
                for e in service.send(thread_id=None, text="写个文件", model="mock")
            ])
            try:
                worker.start()
                approval_id, thread_id = _wait_pending(service)
                service.decide_tool(thread_id, approval_id, allow=False)
                worker.join(10)
                self.assertFalse(worker.is_alive())

                result = next(e for e in collected if e["type"] == "tool_result")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "OPERATOR_DENIED")
                self.assertFalse((root / "ws" / "y.txt").exists(),
                                 "拒绝后不得写入")
                chain = [e["type"] for e in service.store.events(thread_id)]
                self.assertIn("chat_tool_decision", chain)
            finally:
                if old is None:
                    os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                else:
                    os.environ["FCODE_CHAT_MOCK_TOOLS"] = old
                service.cancel_thread_approvals(thread_box.get("thread_id", ""))

    def test_prompt_replays_tool_history_on_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
            os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
                [{"name": "read_file", "arguments": {"path": "README.md"}}])
            try:
                events = list(service.send(thread_id=None, text="看看 README", model="mock"))
            finally:
                if old is None:
                    os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                else:
                    os.environ["FCODE_CHAT_MOCK_TOOLS"] = old
            thread_id = events[0]["thread_id"]
            messages = service._prompt_messages(thread_id)
            roles = [message["role"] for message in messages]
            self.assertIn("tool", roles)
            tool_message = next(m for m in messages if m["role"] == "tool")
            self.assertIn("hello agent", tool_message["content"])
            assistant_with_calls = next(
                m for m in messages if m["role"] == "assistant" and m.get("tool_calls"))
            self.assertEqual(assistant_with_calls["tool_calls"][0]["function"]["name"], "read_file")

    def test_provider_without_tool_support_stays_text_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            events = list(service.send(thread_id=None, text="你好", model="mock"))
            # Simulate the legacy path by checking the gate directly: a config
            # without supports_tools must not enter the tool loop.
            config = ChatProviderConfig(key="local", label="Local", model="m")
            self.assertFalse(config.supports_tools)
            self.assertTrue(all(e["type"] != "tool_call" for e in events))


if __name__ == "__main__":
    unittest.main()
class RunCommandGranularityTests(unittest.TestCase):
    """Phase 3b: run_command 按精确 argv 审批，可证只读免审。"""

    def _send_scripted(self, service, command, args):
        old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
        os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
            [{"name": "run_command", "arguments": {"command": command, "args": args}}])
        collected: list[dict] = []
        thread_box: dict[str, str] = {}
        worker = threading.Thread(target=lambda: [
            (thread_box.setdefault("thread_id", e.get("thread_id") or ""),
             collected.append(e))
            for e in service.send(thread_id=None, text="跑个命令", model="mock")
        ])
        return old, collected, thread_box, worker

    def _restore(self, old):
        if old is None:
            os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
        else:
            os.environ["FCODE_CHAT_MOCK_TOOLS"] = old

    def test_readonly_command_auto_executes_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            old, collected, box, worker = self._send_scripted(
                service, "ls", ["-la"])
            try:
                worker.start()
                worker.join(10)
                self.assertFalse(worker.is_alive())
                kinds = [e["type"] for e in collected]
                self.assertNotIn("tool_approval", kinds, "可证只读命令不该进审批")
                self.assertIn("tool_result", kinds)
                result = next(e for e in collected if e["type"] == "tool_result")
                self.assertEqual(result["status"], "ok")
            finally:
                self._restore(old)
                service.cancel_thread_approvals(box.get("thread_id", ""))

    def test_consequential_command_requires_approval_and_denial_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws" / "scripts").mkdir(parents=True)
            (root / "ws" / "scripts" / "x.py").write_text("print('x')\n", encoding="utf-8")
            service = _service(root, root / "ws")
            old, collected, box, worker = self._send_scripted(
                service, "python3", ["scripts/x.py"])
            try:
                worker.start()
                approval_id, thread_id = _wait_pending(service)
                service.decide_tool(thread_id, approval_id, allow=False)
                worker.join(10)
                self.assertFalse(worker.is_alive())
                result = next(e for e in collected if e["type"] == "tool_result")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "OPERATOR_DENIED")
                chain = [e["type"] for e in service.store.events(thread_id)]
                self.assertIn("chat_tool_approval", chain)
                # 审批卡数据带完整 argv 与 cwd
                approval_event = next(e for e in collected if e["type"] == "tool_approval")
                self.assertIn("python3", approval_event["preview"]["argv"])
                self.assertTrue(approval_event["preview"]["cwd"])
            finally:
                self._restore(old)
                service.cancel_thread_approvals(box.get("thread_id", ""))

    def test_remember_is_per_argv_not_per_tool(self) -> None:
        """run_command 的「不再询问」按精确 argv 记：批准 `python3 x.py`
        不等于放行 `python3 other.py`——不同 argv 仍要走审批。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            (root / "ws" / "scripts").mkdir(parents=True)
            (root / "ws" / "scripts" / "x.py").write_text("print('x')\n", encoding="utf-8")
            (root / "ws" / "scripts" / "other.py").write_text("print('other')\n", encoding="utf-8")
            service = _service(root, root / "ws")
            old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
            thread_box: dict[str, str] = {}
            collected: list[dict] = []

            def _scripted(argv_args):
                os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
                    [{"name": "run_command",
                      "arguments": {"command": "python3", "args": argv_args}}])
                worker = threading.Thread(target=lambda: [
                    (thread_box.setdefault("thread_id", e.get("thread_id") or ""),
                     collected.append(e))
                    for e in service.send(thread_id=thread_box.get("thread_id"),
                                          text="跑个命令", model="mock",
                                          approval="consequential")
                ])
                return worker

            try:
                # 第一条：python3 scripts/x.py → 审批 → 允许并记住
                worker = _scripted(["scripts/x.py"])
                worker.start()
                approval_id, thread_id = _wait_pending(service)
                service.decide_tool(thread_id, approval_id, allow=True, remember=True)
                worker.join(10)
                self.assertFalse(worker.is_alive())
                first_result = next(e for e in collected if e["type"] == "tool_result")
                self.assertEqual(first_result["status"], "ok")

                # 第二条：python3 scripts/other.py（不同 argv）→ 仍需审批 → 拒绝
                worker2 = _scripted(["scripts/other.py"])
                worker2.start()
                approval_id2, _ = _wait_pending(service)
                service.decide_tool(thread_id, approval_id2, allow=False)
                worker2.join(10)
                self.assertFalse(worker2.is_alive())
                second_result = next(e for e in collected[::-1]
                                     if e["type"] == "tool_result")
                self.assertEqual(second_result["status"], "error")
                self.assertEqual(second_result["error"]["code"], "OPERATOR_DENIED")

                # 链上两次审批都有记录
                chain = [e["type"] for e in service.store.events(thread_id)]
                self.assertEqual(chain.count("chat_tool_approval"), 2)
                self.assertEqual(chain.count("chat_tool_decision"), 2)
            finally:
                if old is None:
                    os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                else:
                    os.environ["FCODE_CHAT_MOCK_TOOLS"] = old
                service.cancel_thread_approvals(thread_box.get("thread_id", ""))



class FetchUrlTests(unittest.TestCase):
    def test_fetch_url_requires_approval_and_allow_executes(self) -> None:
        """fetch_url：consequential 档逐调用审批；允许后执行（公网 URL 沙箱放行，
        拒绝私网）。离线环境没有外网——用本地 HTTP 服务器当抓取目标。"""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                payload = b"hello from web"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        # fetch 的 SSRF 策略拒绝私网——本机 127.0.0.1 属私网，会被拒。
        # 这正是要测的：审批放行 ≠ 绕过 SSRF。
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                (root / "ws").mkdir()
                service = _service(root, root / "ws")
                old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
                os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps(
                    [{"name": "fetch_url",
                      "arguments": {"url": f"http://127.0.0.1:{server.server_port}/x"}}])
                collected: list[dict] = []
                thread_box: dict[str, str] = {}
                worker = threading.Thread(target=lambda: [
                    (thread_box.setdefault("thread_id", e.get("thread_id") or ""),
                     collected.append(e))
                    for e in service.send(thread_id=None, text="抓取网页", model="mock")
                ])
                try:
                    worker.start()
                    approval_id, thread_id = _wait_pending(service)
                    # 审批卡的域名信息
                    with service._approvals_cv:
                        payload = service._approvals[approval_id]["payload"]
                    self.assertTrue(payload.get("preview", {}).get("host"))
                    service.decide_tool(thread_id, approval_id, allow=True)
                    worker.join(15)
                    self.assertFalse(worker.is_alive())
                    result = next(e for e in collected if e["type"] == "tool_result")
                    # SSRF 策略在执行时拒绝私网——审批放行不等于绕过沙箱
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], "FETCH_HOST_DENIED")
                finally:
                    if old is None:
                        os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                    else:
                        os.environ["FCODE_CHAT_MOCK_TOOLS"] = old
                    service.cancel_thread_approvals(thread_box.get("thread_id", ""))
        finally:
            server.shutdown()
            server.server_close()

    def test_fetch_url_remember_is_per_host(self) -> None:
        from flowagent.chat_tools import remember_key
        key_a = remember_key("fetch_url", {"url": "https://example.com/a"})
        key_b = remember_key("fetch_url", {"url": "https://example.com/b"})
        key_c = remember_key("fetch_url", {"url": "https://other.org/a"})
        self.assertEqual(key_a, key_b, "同域免审")
        self.assertNotEqual(key_a, key_c, "跨域要重新审批")


class EditPreviewBugTests(unittest.TestCase):
    def test_edit_approval_preview_contains_real_diff(self) -> None:
        """回归：_tool_context 曾把裸 Path 当 sandbox，edit 审批卡只有 preview_error。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "ws").mkdir()
            service = _service(root, root / "ws")
            old = os.environ.get("FCODE_CHAT_MOCK_TOOLS")
            os.environ["FCODE_CHAT_MOCK_TOOLS"] = json.dumps([
                {"name": "edit_file", "arguments": {
                    "path": "README.md", "old_text": "hello agent", "new_text": "hi agent"}}])
            collected: list[dict] = []
            thread_box: dict[str, str] = {}
            worker = threading.Thread(target=lambda: [
                (thread_box.setdefault("thread_id", e.get("thread_id") or ""),
                 collected.append(e))
                for e in service.send(thread_id=None, text="改一下 README", model="mock")
            ])
            try:
                worker.start()
                approval_id, thread_id = _wait_pending(service)
                with service._approvals_cv:
                    preview = service._approvals[approval_id]["payload"].get("preview") or {}
                self.assertIn("diff", preview, "edit 审批卡必须带真实 diff")
                self.assertIn("hi agent", preview["diff"])
                service.decide_tool(thread_id, approval_id, allow=True)
                worker.join(10)
                self.assertFalse(worker.is_alive())
                content = (root / "ws" / "README.md").read_text(encoding="utf-8")
                self.assertIn("hi agent", content)
            finally:
                if old is None:
                    os.environ.pop("FCODE_CHAT_MOCK_TOOLS", None)
                else:
                    os.environ["FCODE_CHAT_MOCK_TOOLS"] = old
                service.cancel_thread_approvals(thread_box.get("thread_id", ""))

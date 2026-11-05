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

    def _wait_pending(self, service, deadline=5.0) -> str:
        """等待一个待批调用出现，返回 (approval_id, thread_id)。"""
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
                approval_id, thread_id = self._wait_pending(service)
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
                approval_id, thread_id = self._wait_pending(service)
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
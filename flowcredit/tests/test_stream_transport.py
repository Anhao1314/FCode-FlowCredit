"""流式传输纪律：本地假 SSE server，全离线、零真实 provider 调用。

钉死的边界：
* 多 delta 拼成完整 tool call 之前，没有任何东西可以执行（结构保证：
  只有组装完的 message 离开 _post_stream）；
* 同一 index 的 fragment 只累积、不重复；
* 收到 ≥1 个 data delta 后断连 / 无 finish_reason 的干净 EOF = 未知结果，
  绝不重发；
* 0 delta 断连 = 与 0 字节同语义，最多重发一次并记账；
* usage chunk 缺失时只有注解、没有 provider usage → usage_missing；
* finish_reason=length 与 reasoning 占满预算的错误路径与缓冲模式一致。
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.providers.openai_compat import (  # noqa: E402
    CompatClient,
    OpenAICompatProvider,
    PriceSnapshot,
    _has_usage,
)

PRICE = PriceSnapshot(
    input_microusd_per_mtok=100,
    output_microusd_per_mtok=200,
    as_of="2026-11-14",
    billing="input-price-v1",
)


def _sse_lines(events: list[dict]) -> bytes:
    out = bytearray()
    for event in events:
        out += f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    out += b"data: [DONE]\n\n"
    return bytes(out)


def _delta_event(content=None, reasoning=None, tool_calls=None, finish=None, index=0):
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    choice: dict = {"index": index, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return {"id": "chatcmpl-stream", "choices": [choice]}


_USAGE = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


def _write_sse(handler, body: bytes) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()
    handler.close_connection = True


def _stream_tool_call(script_fragments: list[str]):
    """One write_file call whose arguments arrive in pieces."""

    def handler_fn(handler):
        events = [
            _delta_event(tool_calls=[{
                "index": 0, "id": "call_abc",
                "function": {"name": "write_file", "arguments": ""},
            }]),
        ]
        for fragment in script_fragments:
            events.append(_delta_event(tool_calls=[{
                "index": 0, "function": {"arguments": fragment},
            }]))
        events.append(_delta_event(finish="tool_calls"))
        events.append({"id": "chatcmpl-stream", "choices": [], "usage": dict(_USAGE)})
        _write_sse(handler, _sse_lines(events))

    return handler_fn


def _stream_ok(handler) -> None:
    events = [
        _delta_event(content="hel"),
        _delta_event(content="lo"),
        _delta_event(finish="stop"),
        {"id": "chatcmpl-stream", "choices": [], "usage": dict(_USAGE)},
    ]
    _write_sse(handler, _sse_lines(events))


def _stream_no_usage(handler) -> None:
    events = [_delta_event(content="hi"), _delta_event(finish="stop")]
    _write_sse(handler, _sse_lines(events))


def _stream_truncated(handler) -> None:
    # ≥1 delta, then a clean EOF without finish_reason: indistinguishable
    # from "done" unless the contract demands the terminator.
    events = [
        _delta_event(tool_calls=[{
            "index": 0, "id": "call_abc",
            "function": {"name": "write_file", "arguments": '{"path":'},
        }]),
    ]
    _write_sse(handler, _sse_lines(events))


def _stream_length_finish(handler) -> None:
    events = [
        _delta_event(reasoning="thinking " * 40),
        _delta_event(finish="length"),
        {"id": "chatcmpl-stream", "choices": [], "usage": dict(_USAGE)},
    ]
    _write_sse(handler, _sse_lines(events))


def _zero_byte(handler) -> None:
    handler.close_connection = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # noqa: ARG002
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        server = self.server
        try:
            server.bodies.append(json.loads(body.decode("utf-8")))
        except ValueError:
            pass
        index = server.calls
        server.calls += 1
        step = server.script[min(index, len(server.script) - 1)]
        step(self)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, script):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script
        self.calls = 0
        self.bodies: list[dict] = []


class StreamTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _client(self, script, *, timeout: float = 5.0) -> CompatClient:
        self.server = _Server(script)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-sse-provider", daemon=True,
        )
        self.thread.start()
        return CompatClient(
            label="fake",
            api_key="",
            base_url=f"http://127.0.0.1:{self.server.server_address[1]}",
            model="fake-model",
            timeout=timeout,
            max_completion_tokens=64,
            require_https=False,
            stream=True,
        )

    def test_request_body_enables_stream_and_usage(self) -> None:
        client = self._client([_stream_ok])
        client.chat("system", "user")
        body = self.server.bodies[0]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_multi_delta_tool_call_assembles_before_return(self) -> None:
        arguments = json.dumps({"path": "index.html", "content": "x" * 5000})
        fragments = [arguments[i:i + 97] for i in range(0, len(arguments), 97)]
        client = self._client([_stream_tool_call(fragments)])
        content, calls, usage, request_id = client.chat_with_tools(
            "system", "user", tools=[{"type": "function", "function": {"name": "write_file"}}],
        )
        self.assertEqual(content, "")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_abc")
        self.assertEqual(calls[0]["function"]["name"], "write_file")
        parsed = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(parsed["content"], "x" * 5000)
        self.assertEqual(request_id, "chatcmpl-stream")
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["finish_reason"], "tool_calls")
        self.assertTrue(usage["stream"])
        self.assertGreater(usage["stream_chunks"], len(fragments))
        self.assertEqual(len(usage["stream_digest"]), 64)

    def test_same_index_fragments_never_duplicate_calls(self) -> None:
        arguments = json.dumps({"path": "a.txt", "content": "y"})
        client = self._client([_stream_tool_call([arguments[:10], arguments[10:]])])
        _, calls, _, _ = client.chat_with_tools(
            "system", "user", tools=[{"type": "function", "function": {"name": "write_file"}}],
        )
        self.assertEqual(len(calls), 1, "同一 index 的 fragment 只累积，不重复")

    def test_truncated_stream_is_unknown_and_never_resent(self) -> None:
        client = self._client([_stream_truncated, _stream_ok])
        with self.assertRaises(AgentError) as caught:
            client.chat_with_tools(
                "system", "user", tools=[{"type": "function", "function": {"name": "write_file"}}],
            )
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertTrue(caught.exception.details["stream_partial"])
        self.assertGreaterEqual(caught.exception.details["stream_chunks"], 1)
        self.assertGreater(caught.exception.details["stream_argument_chars"], 0)
        self.assertEqual(self.server.calls, 1, "收到 delta 后断连绝不重发")

    def test_zero_delta_disconnect_retries_once(self) -> None:
        client = self._client([_zero_byte, _stream_ok])
        content, usage, _ = client.chat("system", "user")
        self.assertEqual(content, "hello")
        self.assertEqual(usage["transport_retries"], 1)
        self.assertEqual(self.server.calls, 2)

    def test_two_zero_delta_disconnects_retry_at_most_once(self) -> None:
        client = self._client([_zero_byte, _zero_byte, _stream_ok])
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(self.server.calls, 2, "同一请求最多一次重试")

    def test_missing_usage_chunk_stays_usage_missing(self) -> None:
        client = self._client([_stream_no_usage])
        content, usage, _ = client.chat("system", "user")
        self.assertEqual(content, "hi")
        self.assertFalse(_has_usage(usage), "注解不是 provider usage")

    def test_length_finish_reason_is_recorded(self) -> None:
        client = self._client([_stream_length_finish])
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "BUDGET_EXHAUSTED_REASONING")
        self.assertIn("推理", caught.exception.message)
        self.assertEqual(caught.exception.details["failure_kind"], "BUDGET_EXHAUSTED_REASONING")

    def test_buffered_path_keeps_stream_false(self) -> None:
        self.server = _Server([_stream_ok])
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-sse-provider", daemon=True,
        )
        self.thread.start()
        client = CompatClient(
            label="fake", api_key="",
            base_url=f"http://127.0.0.1:{self.server.server_address[1]}",
            model="fake-model", timeout=5.0, max_completion_tokens=64,
            require_https=False,
        )
        with self.assertRaises(AgentError):
            # SSE 正文不是单个 JSON 文档：缓冲路径诚实失败，而不是偷偷解析。
            client.chat("system", "user")
        self.assertIs(self.server.bodies[0]["stream"], False)
        self.assertNotIn("stream_options", self.server.bodies[0])


class StreamProviderMetadataTests(unittest.TestCase):
    """provider 层的 receipt metadata：stream 字段必须可审计地进入收据。"""

    def setUp(self) -> None:
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def test_proposal_metadata_carries_stream_fields(self) -> None:
        self.server = _Server([_stream_ok])
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-sse-provider", daemon=True,
        )
        self.thread.start()
        provider = OpenAICompatProvider(
            api_key="",
            price=PRICE,
            model="fake-model",
            base_url=f"http://127.0.0.1:{self.server.server_address[1]}",
            timeout=5.0,
            max_completion_tokens=64,
            require_https=False,
            stream=True,
            receipt_detail="compact",
        )
        context = {
            "round": 1,
            "intent": {"id": "t1"},
            "task": "general-research",
            "mode": "open",
            "plan": [{"id": "t1", "text": "读文件", "tool": "ls_workspace",
                      "arguments": {}, "status": "pending"}],
            "profile": "general-fast",
        }
        with mock.patch(
            "flowagent.providers.openai_compat.render_prompt",
            return_value=("system", "user"),
        ), mock.patch(
            "flowagent.providers.openai_compat.parse_proposal_text",
            return_value={"version": "flowagent.plan/v1",
                          "steps": [{"id": "t1", "tool": "ls_workspace", "arguments": {}}]},
        ):
            proposal = provider.propose(context)
        self.assertTrue(proposal.metadata["stream"])
        self.assertGreater(proposal.metadata["stream_chunks"], 0)
        self.assertEqual(len(proposal.metadata["stream_digest"]), 64)
        self.assertEqual(proposal.metadata["finish_reason"], "stop")
        self.assertEqual(proposal.cost_microusd, PRICE.cost(dict(_USAGE)))


if __name__ == "__main__":
    unittest.main()

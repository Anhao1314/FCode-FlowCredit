"""0 字节断连的唯一自动重试：本地假 HTTP server，全离线、零真实 provider 调用。

钉死允许与不允许的边界：
* 允许：RemoteDisconnected（一个响应字节都没收到）、IncompleteRead(partial=b"")；
* 不允许：HTTP 4xx/5xx、超时、partial 非空的半包、非 JSON 正文；
* 同一请求最多重试 1 次；transport_retries 作为整数进 usage / metadata / 签名回执。
"""

from __future__ import annotations

import http.client
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.demo.provider_factory import build_provider  # noqa: E402
from flowagent.providers.openai_compat import CompatClient  # noqa: E402

PLAN_TEXT = json.dumps({
    "version": "flowagent.plan/v1",
    "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}],
    "todo_id": "t1",
}, ensure_ascii=False)

_OK_BODY = json.dumps({
    "id": "chatcmpl-ok",
    "choices": [{"message": {"content": "hello"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}).encode("utf-8")


def _zero_byte(handler):
    """Close with no status line and no body at all."""
    handler.close_connection = True


def _declared_no_bytes(handler):
    """Headers say a body is coming, then nothing: IncompleteRead(partial=b'')."""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", "32")
    handler.end_headers()
    handler.close_connection = True


def _half_body(handler):
    """Half the declared body arrives: bytes already crossed the wire."""
    chunk = b'{"id":"half","choices":['
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(chunk) * 2))
    handler.end_headers()
    handler.wfile.write(chunk)
    handler.close_connection = True


def _ok(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(_OK_BODY)))
    handler.end_headers()
    handler.wfile.write(_OK_BODY)


def _http_500(handler):
    body = b'{"error":"upstream"}'
    handler.send_response(500)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _bad_json(handler):
    body = b"not-json"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _slow(handler):
    time.sleep(0.8)
    _ok(handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # noqa: ARG002 - keep test output clean
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        server = self.server
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


class CompatClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = None
        self.thread = None

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _client(self, script, *, timeout: float = 5.0):
        self.server = _Server(script)
        self.thread = threading.Thread(
            target=self.server.serve_forever, name="fake-provider", daemon=True,
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
        )

    def test_zero_byte_disconnect_retries_once_and_succeeds(self) -> None:
        client = self._client([_zero_byte, _ok])
        content, usage, request_id = client.chat("system", "user")
        self.assertEqual(content, "hello")
        self.assertEqual(request_id, "chatcmpl-ok")
        self.assertEqual(usage["transport_retries"], 1)
        self.assertEqual(self.server.calls, 2)

    def test_empty_partial_incomplete_read_retries_once(self) -> None:
        client = self._client([_declared_no_bytes, _ok])
        _, usage, _ = client.chat("system", "user")
        self.assertEqual(usage["transport_retries"], 1)
        self.assertEqual(self.server.calls, 2)

    def test_two_zero_byte_disconnects_retry_at_most_once(self) -> None:
        client = self._client([_zero_byte, _zero_byte, _ok])
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(self.server.calls, 2, "同一请求最多一次重试")

    def test_partial_body_is_not_retried(self) -> None:
        client = self._client([_half_body, _ok])
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(self.server.calls, 1)

    def test_http_500_is_not_retried(self) -> None:
        client = self._client([_http_500, _ok])
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(self.server.calls, 1)

    def test_timeout_is_not_retried(self) -> None:
        client = self._client([_slow, _ok], timeout=0.2)
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(self.server.calls, 1)

    def test_non_json_response_is_not_retried(self) -> None:
        client = self._client([_bad_json, _ok])
        with self.assertRaises(AgentError) as caught:
            client.chat("system", "user")
        self.assertEqual(caught.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(self.server.calls, 1)

    def test_no_retry_leaves_usage_block_untouched(self) -> None:
        client = self._client([_ok])
        _, usage, _ = client.chat("system", "user")
        self.assertNotIn("transport_retries", usage)
        self.assertEqual(usage["total_tokens"], 12)
        self.assertEqual(self.server.calls, 1)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class ReceiptVisibilityTests(unittest.TestCase):
    def test_transport_retries_reaches_metadata_and_signed_receipt(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = TaskStore(Path(tmp.name) / "task.sqlite")
        receipts = ReceiptStore(store)
        try:
            with mock.patch.dict("os.environ", {
                "FCODE_OPENAI_COMPAT_PROVIDERS": "OLLAMA",
                "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
                "OLLAMA_MODEL": "fake-model",
                "OLLAMA_INPUT_PRICE_MICROUSD_PER_MTOK": "0",
                "OLLAMA_OUTPUT_PRICE_MICROUSD_PER_MTOK": "0",
                "OLLAMA_PRICE_AS_OF": "2026-10-25",
            }, clear=False):
                provider = build_provider(
                    "ollama", scenario=get_scenario("full"), receipts=receipts,
                )
                seen = {"calls": 0}

                def fake_urlopen(request, timeout):  # noqa: ARG001
                    seen["calls"] += 1
                    if seen["calls"] == 1:
                        raise http.client.RemoteDisconnected(
                            "Remote end closed connection without response"
                        )
                    return _Response({
                        "id": "chatcmpl-ok",
                        "choices": [{"message": {"content": PLAN_TEXT}}],
                        "usage": {
                            "prompt_tokens": 50,
                            "completion_tokens": 5,
                            "total_tokens": 55,
                        },
                    })

                with mock.patch("urllib.request.urlopen", fake_urlopen):
                    proposal = provider.propose({
                        "objective": "t", "round": 1,
                        "intent": {"task_id": "t", "round": 1},
                    })
            self.assertEqual(seen["calls"], 2)
            self.assertEqual(proposal.metadata["transport_retries"], 1)
            self.assertEqual(proposal.receipt()["metadata"]["transport_retries"], 1)
            ref = proposal.metadata["hmac_receipt_ref"]
            self.assertEqual(receipts.load(ref)["usage"]["transport_retries"], 1)
            self.assertIsInstance(receipts.load(ref)["usage"]["transport_retries"], int)
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()

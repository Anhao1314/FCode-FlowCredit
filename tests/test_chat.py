"""Chat: streaming, branching and cancellation on the same event chain."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import (  # noqa: E402
    ChatProviderConfig,
    ChatService,
    parse_sse_data,
    resolve_chat_provider,
)
from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.pacing import reset  # noqa: E402


class ChatServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"FCODE_CHAT_MOCK_DELAY": "0"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        reset()
        self.service = ChatService(pathlib.Path(self._tmp.name) / "chat.sqlite")
        self.addCleanup(self.service.close)
        self.addCleanup(reset)

    def send(self, text: str, thread_id: str | None = None, **kwargs):
        return list(self.service.send(thread_id=thread_id, text=text, model="mock", **kwargs))

    def test_streaming_answer_is_chained_and_receipted(self) -> None:
        events = self.send("介绍一下 FCode")
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "start")
        self.assertIn("delta", kinds)
        self.assertEqual(kinds[-1], "done")
        thread_id = events[0]["thread_id"]
        thread = self.service.get_thread(thread_id)
        self.assertEqual([message["role"] for message in thread["messages"]], ["user", "assistant"])
        self.assertEqual(thread["messages"][1]["status"], "ok")
        self.assertEqual(thread["budget"]["calls_used"], 1)
        self.assertGreater(thread["budget"]["tokens_used"], 0)
        self.assertGreaterEqual(self.service.receipts.count(), 1)
        # The chain is append-only and independently verifiable.
        self.service.store.verify_chain(thread_id)
        self.assertEqual(thread["events"], 3)

    def test_follow_up_keeps_the_thread_history(self) -> None:
        first = self.send("第一问")
        thread_id = first[0]["thread_id"]
        self.send("第二问", thread_id=thread_id)
        thread = self.service.get_thread(thread_id)
        self.assertEqual(
            [message["role"] for message in thread["messages"]],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(thread["messages"][2]["text"], "第二问")
        self.assertEqual(thread["budget"]["calls_used"], 2)
        self.service.store.verify_chain(thread_id)

    def test_retention_archives_old_threads_without_deleting_events(self) -> None:
        old = self.send("旧对话")[0]["thread_id"]
        new = self.send("新对话")[0]["thread_id"]
        with self.service.store.transaction():
            self.service.store._conn.execute(
                "UPDATE task SET updated_at=? WHERE id=?",
                ("2020-01-01T00:00:00.000Z", old),
            )
        self.assertEqual(self.service.archive_stale(30), 1)
        self.assertEqual(self.service.archive_stale(30), 0)
        self.assertNotIn(old, [thread["id"] for thread in self.service.list_threads()])
        self.assertIn(new, [thread["id"] for thread in self.service.list_threads()])
        # The tombstone hides the thread; it does not remove evidence.
        self.assertGreater(len(self.service.store.events(old)), 0)
        self.service.store.verify_chain(old)

    def test_regenerate_supersedes_only_the_answer(self) -> None:
        events = self.send("问题")
        thread_id = events[0]["thread_id"]
        thread = self.service.get_thread(thread_id)
        answer = thread["messages"][-1]
        list(self.service.regenerate(
            thread_id=thread_id, message_id=answer["id"], model="mock",
        ))
        thread = self.service.get_thread(thread_id)
        self.assertEqual([message["role"] for message in thread["messages"]], ["user", "assistant"])
        self.assertNotEqual(thread["messages"][-1]["id"], answer["id"])
        self.service.store.verify_chain(thread_id)

    def test_edit_supersedes_the_later_branch(self) -> None:
        first = self.send("第一问")
        thread_id = first[0]["thread_id"]
        self.send("第二问", thread_id=thread_id)
        thread = self.service.get_thread(thread_id)
        target = next(message for message in thread["messages"] if message["role"] == "user")
        list(self.service.edit(
            thread_id=thread_id,
            message_id=target["id"],
            text="改问",
            model="mock",
        ))
        thread = self.service.get_thread(thread_id)
        self.assertEqual([message["role"] for message in thread["messages"]], ["user", "assistant"])
        self.assertEqual(thread["messages"][0]["text"], "改问")
        self.assertEqual(thread["messages"][0]["edited"], True)
        # The old branch is still in the chain, just not in the active projection.
        self.assertGreater(thread["events"], 6)
        self.service.store.verify_chain(thread_id)

    def test_stop_records_a_partial_cancelled_answer(self) -> None:
        events = self.send("会中途停止")
        thread_id = events[0]["thread_id"]
        generator = self.service.send(
            thread_id=thread_id, text="再写一段", model="mock",
        )
        seen = 0
        for event in generator:
            if event["type"] == "delta":
                seen += 1
                if seen >= 3:
                    generator.close()
                    break
        thread = self.service.get_thread(thread_id)
        self.assertEqual(thread["messages"][-1]["status"], "cancelled")
        self.assertTrue(thread["messages"][-1]["text"])
        self.service.store.verify_chain(thread_id)

    def test_attachments_are_stored_but_not_echoed_in_public_payloads(self) -> None:
        events = self.send("看看这个", attachments=[
            {"name": "notes.md", "text": "# 标题\n正文"},
        ])
        thread_id = events[0]["thread_id"]
        public = self.service.get_thread(thread_id)
        self.assertEqual(public["messages"][0]["attachments"], [{"name": "notes.md", "bytes": len("# 标题\n正文".encode())}])
        raw = [
            event for event in self.service.store.events(thread_id)
            if event["type"] == "chat_user"
        ][0]
        stored = raw["payload"]["detail"]["attachments"][0]
        self.assertEqual(stored["text"], "# 标题\n正文")

    def test_attachment_limits_fail_closed(self) -> None:
        with self.assertRaises(AgentError) as too_many:
            self.send("x", attachments=[{"name": f"{i}.txt", "text": "x"} for i in range(4)])
        self.assertEqual(too_many.exception.code, "CHAT_ATTACHMENT_INVALID")
        with self.assertRaises(AgentError) as too_large:
            self.send("x", attachments=[{"name": "big.txt", "text": "x" * (64 * 1024 + 1)}])
        self.assertEqual(too_large.exception.code, "CHAT_ATTACHMENT_INVALID")

    def test_budget_exhaustion_is_an_error_event_not_a_retry(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_CHAT_MAX_CALLS": "1"}, clear=False):
            first = self.send("第一问")
            thread_id = first[0]["thread_id"]
            second = self.send("第二问", thread_id=thread_id)
        self.assertEqual(second[0]["type"], "error")
        self.assertEqual(second[0]["thread_id"], thread_id)
        self.assertEqual(second[0]["code"], "BUDGET_EXCEEDED")
        thread = self.service.get_thread(thread_id)
        self.assertEqual(thread["messages"][-1]["status"], "error")
        self.service.store.verify_chain(thread_id)

    def test_thread_list_is_ordered_and_titled(self) -> None:
        first = self.send("标题来自第一条消息 " + "x" * 40)
        thread_id = first[0]["thread_id"]
        threads = self.service.list_threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["id"], thread_id)
        self.assertTrue(threads[0]["title"].startswith("标题来自第一条消息"))
        self.assertLessEqual(len(threads[0]["title"]), 31)

    def test_archive_hides_thread_but_keeps_the_chain(self) -> None:
        first = self.send("归档我")
        thread_id = first[0]["thread_id"]
        self.assertEqual([thread["id"] for thread in self.service.list_threads()], [thread_id])
        self.service.archive_thread(thread_id)
        self.assertEqual(self.service.list_threads(), [])
        self.assertEqual(self.service.thread_count(), 0)
        # The evidence is still auditable, just not listed.
        thread = self.service.get_thread(thread_id)
        self.assertTrue(thread["messages"])
        self.service.store.verify_chain(thread_id)
        with self.assertRaises(AgentError) as error:
            self.service.archive_thread("chat-missing")
        self.assertEqual(error.exception.code, "CHAT_THREAD_NOT_FOUND")

    def test_list_threads_paginates(self) -> None:
        ids = []
        for index in range(3):
            ids.append(self.send(f"问题 {index}")[0]["thread_id"])
            time.sleep(0.003)
        first = self.service.list_threads(limit=2, offset=0)
        second = self.service.list_threads(limit=2, offset=2)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertEqual({thread["id"] for thread in first + second}, set(ids))
        self.assertEqual(self.service.thread_count(), 3)

    def test_unknown_thread_is_not_silently_created(self) -> None:
        with self.assertRaises(AgentError) as error:
            list(self.service.send(thread_id="chat-missing", text="hi", model="mock"))
        self.assertEqual(error.exception.code, "CHAT_THREAD_NOT_FOUND")


class ProviderConfigTests(unittest.TestCase):
    def test_mock_needs_nothing(self) -> None:
        config = resolve_chat_provider("mock")
        self.assertEqual(config.key, "mock")
        self.assertFalse(config.remote)

    def test_kimi_without_key_or_prices_fails_closed(self) -> None:
        cleared = {
            "KIMI_API_KEY": "",
            "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "",
            "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "",
        }
        with mock.patch.dict(os.environ, cleared, clear=False):
            with self.assertRaises(AgentError) as error:
                resolve_chat_provider("kimi")
        self.assertEqual(error.exception.code, "CHAT_PROVIDER_UNAVAILABLE")
        self.assertIn("KIMI_API_KEY", error.exception.message)

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(AgentError) as error:
            resolve_chat_provider("not-a-provider")
        self.assertEqual(error.exception.code, "CHAT_PROVIDER_UNAVAILABLE")


class SSEParserTests(unittest.TestCase):
    def test_data_lines(self) -> None:
        self.assertEqual(
            parse_sse_data('data: {"choices":[{"delta":{"content":"你"}}]}')["choices"][0]["delta"]["content"],
            "你",
        )
        self.assertIsNone(parse_sse_data("data: [DONE]"))
        self.assertIsNone(parse_sse_data(": keepalive"))
        self.assertIsNone(parse_sse_data("event: message"))
        self.assertIsNone(parse_sse_data("data: not-json"))
        self.assertIsNone(parse_sse_data(b""))

    def test_bytes_lines(self) -> None:
        parsed = parse_sse_data(b'data: {"usage":{"total_tokens":3}}')
        self.assertEqual(parsed["usage"]["total_tokens"], 3)


class ChatFailureAndPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"FCODE_CHAT_MOCK_DELAY": "0"}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = ChatService(pathlib.Path(self._tmp.name) / "chat.sqlite")
        self.addCleanup(self.service.close)

    def test_unexpected_provider_error_is_error_not_cancelled(self) -> None:
        first = list(self.service.send(thread_id=None, text="第一问", model="mock"))
        thread_id = first[0]["thread_id"]
        config = ChatProviderConfig(key="mock", label="Fake", model="fake")

        class BrokenProvider:
            def stream(self, messages):
                yield {"delta": "partial "}
                raise ValueError("malformed chunk")

        with mock.patch.object(self.service, "_provider", return_value=(config, BrokenProvider())):
            events = list(self.service.send(thread_id=thread_id, text="再试一次", model="mock"))
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "CHAT_STREAM_FAILED")
        thread = self.service.get_thread(thread_id)
        self.assertEqual(thread["messages"][-1]["status"], "error")
        self.assertIn("malformed chunk", thread["messages"][-1]["error"]["message"])
        kinds = [event["type"] for event in self.service.store.events(thread_id)]
        self.assertIn("chat_error", kinds)
        self.assertNotIn("chat_cancelled", kinds)
        self.service.store.verify_chain(thread_id)

    def test_start_event_does_not_resend_the_whole_thread(self) -> None:
        first = list(self.service.send(thread_id=None, text="第一问", model="mock"))
        thread_id = first[0]["thread_id"]
        second = list(self.service.send(thread_id=thread_id, text="第二问", model="mock"))
        start = second[0]
        self.assertNotIn("thread", start)
        self.assertEqual(start["thread_title"], "第一问")
        self.assertEqual(start["thread_id"], thread_id)


class RemoteStreamTests(unittest.TestCase):
    """The OpenAI-compatible SSE client, against a local fake provider."""

    def setUp(self) -> None:
        reset()

    def tearDown(self) -> None:
        reset()

    def test_kimi_stream_uses_sse_and_kimi_completion_field(self) -> None:
        captured: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                captured["body"] = json.loads(self.rfile.read(length))
                captured["auth"] = self.headers.get("Authorization")
                captured["path"] = self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for payload in (
                    {"choices": [{"delta": {"content": "你"}}]},
                    {"choices": [{"delta": {"content": "好"}}]},
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                    },
                ):
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

            def log_message(self, *args) -> None:  # noqa: D401
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        environment = {
            "KIMI_API_KEY": "test-key",
            "KIMI_BASE_URL": f"http://127.0.0.1:{port}/v1",
            "KIMI_MODEL": "test-model",
            "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "1000",
            "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "2000",
            "KIMI_MIN_CALL_INTERVAL_SECONDS": "0",
            "KIMI_PRICE_AS_OF": "2026-10-25",
            "CHAT_MAX_COMPLETION_TOKENS": "64",
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                service = ChatService(pathlib.Path(tmp) / "chat.sqlite")
                self.addCleanup(service.close)
                events = list(service.send(thread_id=None, text="你好", model="kimi"))
                done = next(event for event in events if event["type"] == "done")
                thread_id = events[0]["thread_id"]
                self.assertEqual(done["message"]["text"], "你好")
                self.assertEqual(captured["auth"], "Bearer test-key")
                self.assertTrue(captured["path"].endswith("/v1/chat/completions"))
                body = captured["body"]
                self.assertTrue(body["stream"])
                self.assertIn("max_completion_tokens", body)
                self.assertNotIn("max_tokens", body)
                self.assertNotIn("temperature", body)
                self.assertEqual(body["messages"][0]["role"], "system")
                self.assertEqual(body["messages"][-1]["content"], "你好")
                self.assertGreater(done["message"]["usage"]["cost_microusd"], 0)
                self.assertGreaterEqual(service.receipts.count(), 1)
                service.store.verify_chain(thread_id)


class ChatSettingsTests(unittest.TestCase):
    """Settings overrides: stored locally, redacted in API responses."""

    KIMI_ENV_CLEARED = {
        "KIMI_API_KEY": "",
        "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "",
        "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "",
        "KIMI_MODEL": "",
        "KIMI_BASE_URL": "",
        "KIMI_MIN_CALL_INTERVAL_SECONDS": "",
        "KIMI_PRICE_AS_OF": "",
        "KIMI_PRICE_SOURCE": "",
    }

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        patcher = mock.patch.dict(os.environ, self.KIMI_ENV_CLEARED, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = ChatService(
            self.root / "chat.sqlite", self.root / "chat-config.json",
        )
        self.addCleanup(self.service.close)

    def test_settings_override_enables_kimi_without_env(self) -> None:
        models = {item["key"]: item for item in self.service.chat_models()}
        self.assertFalse(models["kimi"]["available"])
        snapshot = self.service.update_config(provider="kimi", values={
            "api_key": "sk-test-1234567890",
            "model": "kimi-k2.6",
            "input_price_microusd_per_mtok": "950000",
            "output_price_microusd_per_mtok": "4000000",
            "min_interval_seconds": "22",
            "price_as_of": "2026-10-25",
            "price_source": "moonshot-cn-published",
        })
        self.assertTrue(snapshot["config"]["providers"]["kimi"]["api_key_set"])
        self.assertNotIn("sk-test-1234567890", json.dumps(snapshot))
        models = {item["key"]: item for item in snapshot["models"]}
        self.assertTrue(models["kimi"]["available"])
        self.assertEqual(models["kimi"]["model"], "kimi-k2.6")
        # The key lives only in the local file, never in the API payload.
        stored = (self.root / "chat-config.json").read_text(encoding="utf-8")
        self.assertIn("sk-test-1234567890", stored)

    def test_prices_without_a_date_are_not_a_configured_provider(self) -> None:
        """The date is what makes a price auditable. Keeping it required on
        both surfaces is deliberate: the delegation presets never guessed it,
        and chat feeding the same budget ceiling must not either."""
        snapshot = self.service.update_config(provider="kimi", values={
            "api_key": "sk-test-1234567890",
            "input_price_microusd_per_mtok": "950000",
            "output_price_microusd_per_mtok": "4000000",
        })
        models = {item["key"]: item for item in snapshot["models"]}
        self.assertFalse(models["kimi"]["available"])
        self.assertIn("KIMI_PRICE_AS_OF", models["kimi"]["reason"])
        self.assertIn("价格日期", models["kimi"]["reason"])
        # …and filling it in is enough to make the same provider available.
        snapshot = self.service.update_config(provider="kimi", values={"price_as_of": "2026-10-25"})
        models = {item["key"]: item for item in snapshot["models"]}
        self.assertTrue(models["kimi"]["available"])

    def test_default_model_roundtrip_and_validation(self) -> None:
        snapshot = self.service.update_config(default_model="kimi")
        self.assertEqual(snapshot["default"], "kimi")
        self.assertEqual(self.service.default_model(), "kimi")
        with self.assertRaises(AgentError) as error:
            self.service.update_config(default_model="not-a-model")
        self.assertEqual(error.exception.code, "CHAT_CONFIG_INVALID")

    def test_environment_beats_stored_settings(self) -> None:
        with mock.patch.dict(os.environ, {
            "KIMI_API_KEY": "env-key",
            "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "1",
            "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "2",
            "KIMI_MODEL": "kimi-env",
            "KIMI_PRICE_AS_OF": "2026-01-01",
        }, clear=False):
            self.service.update_config(provider="kimi", values={
                "api_key": "settings-key",
                "model": "kimi-settings",
                "input_price_microusd_per_mtok": "950000",
                "output_price_microusd_per_mtok": "4000000",
                "price_as_of": "2026-10-25",
            })
            config = resolve_chat_provider(
                "kimi", self.service._overrides_for("kimi"),
            )
        self.assertEqual(config.api_key, "env-key")
        self.assertEqual(config.model, "kimi-env")

    def test_empty_value_clears_back_to_environment(self) -> None:
        with mock.patch.dict(os.environ, {
            "KIMI_API_KEY": "env-key",
            "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "950000",
            "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "4000000",
            "KIMI_PRICE_AS_OF": "2026-10-25",
        }, clear=False):
            self.service.update_config(provider="kimi", values={"api_key": "settings-key"})
            self.service.update_config(provider="kimi", values={"api_key": ""})
            config = resolve_chat_provider(
                "kimi", self.service._overrides_for("kimi"),
            )
        self.assertEqual(config.api_key, "env-key")

    def test_remote_base_url_override_is_rejected(self) -> None:
        with self.assertRaises(AgentError) as error:
            self.service.update_config(
                provider="kimi",
                values={"base_url": "https://evil.example", "api_key": "k"},
            )
        self.assertEqual(error.exception.code, "CHAT_CONFIG_INVALID")
        self.assertIn("unknown field", error.exception.message)
        # local endpoints remain editable
        snapshot = self.service.update_config(
            provider="local", values={"base_url": "http://127.0.0.1:11434/v1"},
        )
        self.assertEqual(
            snapshot["config"]["providers"]["local"]["base_url"],
            "http://127.0.0.1:11434/v1",
        )

    def test_validation_fails_closed(self) -> None:
        cases = [
            ({"input_price_microusd_per_mtok": "0"}, "positive"),
            ({"input_price_microusd_per_mtok": "abc"}, "integer"),
            ({"min_interval_seconds": "-1"}, "0..3600"),
            ({"max_completion_tokens": "999999"}, "1..32768"),
            ({"nope": "1"}, "unknown field"),
        ]
        for values, needle in cases:
            with self.subTest(field=list(values)[0]):
                with self.assertRaises(AgentError) as error:
                    self.service.update_config(provider="kimi", values={**values, "api_key": "k"})
                self.assertEqual(error.exception.code, "CHAT_CONFIG_INVALID")
                self.assertIn(needle, error.exception.message)
        with self.assertRaises(AgentError) as error:
            self.service.update_config(provider="mock", values={})
        self.assertEqual(error.exception.code, "CHAT_CONFIG_INVALID")
        with self.assertRaises(AgentError) as error:
            self.service.update_config(provider="local", values={"base_url": "ftp://x"})
        self.assertIn("http(s)", error.exception.message)

    def test_corrupt_config_file_reads_as_empty(self) -> None:
        (self.root / "chat-config.json").write_text("{not json", encoding="utf-8")
        fresh = ChatService(self.root / "chat2.sqlite", self.root / "chat-config.json")
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.default_model(), "mock")
        models = {item["key"]: item for item in fresh.chat_models()}
        self.assertTrue(models["mock"]["available"])


if __name__ == "__main__":
    unittest.main()

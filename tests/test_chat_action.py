"""Chat v2: the in-conversation execution proposal and its confirmation.

A chat answer may carry a ````fcode_action```` block. Parsing is fail-closed,
the anchor from proposal to run is one-shot, and the HTTP confirmation goes
through the same creation path as every other delegation.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "viewer" / "server.py"
sys.path.insert(0, str(ROOT / "src"))

from flowagent.chat import (  # noqa: E402
    ChatService,
    merge_action,
    parse_action_block,
)
from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.pacing import reset  # noqa: E402


def action_block(payload) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return "```fcode_action\n" + body + "\n```"


class ActionBlockTests(unittest.TestCase):
    """Anything that is not a whole, valid proposal leaves the text untouched."""

    def test_block_is_split_from_the_prose(self) -> None:
        text, action = parse_action_block(
            "我来安排。\n\n"
            + action_block({
                "goal": "查周三上海到杭州的高铁",
                "mode": "research",
                "sources": ["https://example.com/a"],
            })
            + "\n"
        )
        self.assertEqual(text, "我来安排。")
        self.assertEqual(action["mode"], "research")
        self.assertEqual(action["goal"], "查周三上海到杭州的高铁")
        self.assertEqual(action["sources"], ["https://example.com/a"])

    def test_block_without_mode_is_a_script_proposal(self) -> None:
        _, action = parse_action_block(action_block({"goal": "写一个脚本整理文件"}))
        self.assertEqual(action, {"goal": "写一个脚本整理文件", "mode": "script"})

    def test_last_valid_block_wins(self) -> None:
        _, action = parse_action_block(
            action_block({"goal": "第一个提案目标"})
            + "\n正文\n"
            + action_block({"goal": "第二个提案目标", "mode": "research"})
        )
        self.assertEqual(action["goal"], "第二个提案目标")

    def test_a_later_invalid_block_keeps_the_earlier_valid_one(self) -> None:
        _, action = parse_action_block(
            action_block({"goal": "有效的提案目标"})
            + "\n"
            + action_block({"mode": "deploy"})
        )
        self.assertEqual(action["goal"], "有效的提案目标")

    def test_malformed_payloads_leave_the_text_untouched(self) -> None:
        cases = {
            "no block": "只是一段普通回答。",
            "unterminated": '前面\n```fcode_action\n{"goal": "没有收尾"}\n',
            "bad json": action_block("{goal: 不是 JSON}"),
            "not an object": action_block('["goal"]'),
            "no goal": action_block({"mode": "script"}),
            "goal too short": action_block({"goal": "短"}),
            "goal not a string": action_block({"goal": 42}),
            "goal too long": action_block({"goal": "长" * 601}),
            "unknown mode": action_block({"goal": "足够长的提案目标", "mode": "deploy"}),
            "oversized": action_block({"goal": "x" * 4000}),
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                text, action = parse_action_block(raw)
                self.assertIsNone(action)
                self.assertEqual(text, raw)

    def test_sources_are_capped_and_non_strings_dropped(self) -> None:
        _, action = parse_action_block(action_block({
            "goal": "抓取这几个来源",
            "mode": "research",
            "sources": [
                "https://a.example", 7, "  ", "https://b.example", "https://c.example",
                "https://d.example", "https://e.example", "https://f.example",
            ],
        }))
        self.assertEqual(action["sources"], [
            "https://a.example", "https://b.example", "https://c.example",
            "https://d.example", "https://e.example",
        ])

    def test_reason_is_kept_but_bounded(self) -> None:
        _, action = parse_action_block(
            action_block({"goal": "读一下这几个文件", "reason": "因为" * 200})
        )
        self.assertEqual(len(action["reason"]), 200)


class PromptContractTests(unittest.TestCase):
    """The system prompt and the parser are one contract, one owner."""

    def test_the_prompt_describes_exactly_the_block_the_parser_accepts(self) -> None:
        from flowagent import chat, chat_config

        prompt = chat.CHAT_SYSTEM_PROMPT
        self.assertIn("fcode_action", prompt)
        for field in ('"goal"', '"mode"', '"sources"'):
            self.assertIn(field, prompt)
        # A second copy during the module split would drift silently.
        self.assertFalse(hasattr(chat_config, "CHAT_SYSTEM_PROMPT"))

    def test_the_prompted_example_is_a_parsable_proposal(self) -> None:
        """The example the model is told to copy must parse: prompt drift would
        otherwise show up as answers the card silently ignores."""
        from flowagent.chat import CHAT_SYSTEM_PROMPT

        start = CHAT_SYSTEM_PROMPT.index("```fcode_action")
        end = CHAT_SYSTEM_PROMPT.index("```", start + 3) + 3
        example = CHAT_SYSTEM_PROMPT[start:end].replace("（5–600 字）", "")
        text, action = parse_action_block(example)
        self.assertIsNotNone(action)
        self.assertEqual(action["mode"], "script")
        self.assertEqual(text, "")


class MergeActionTests(unittest.TestCase):
    """The confirmation card edits a proposal under the parser's own rules."""

    def test_goal_and_mode_edits_are_applied(self) -> None:
        merged = merge_action(
            {"goal": "模型写的目标", "mode": "script", "sources": ["https://a.example"]},
            goal="用户改写后的目标",
            mode="research",
        )
        self.assertEqual(merged["goal"], "用户改写后的目标")
        self.assertEqual(merged["mode"], "research")
        self.assertEqual(merged["sources"], ["https://a.example"])

    def test_edited_sources_replace_the_models_and_drop_duplicates(self) -> None:
        merged = merge_action(
            {"goal": "模型写的目标", "mode": "research"},
            sources=["https://a.example", " https://a.example ", "", "https://b.example"],
        )
        self.assertEqual(merged["sources"], ["https://a.example", "https://b.example"])

    def test_clearing_sources_removes_the_key(self) -> None:
        merged = merge_action(
            {"goal": "模型写的目标", "mode": "research", "sources": ["https://a.example"]},
            sources=[],
        )
        self.assertNotIn("sources", merged)

    def test_edits_fail_closed(self) -> None:
        cases = {
            "goal too short": {"goal": "短"},
            "goal too long": {"goal": "长" * 601},
            "unknown mode": {"mode": "deploy"},
            "too many sources": {"sources": [f"https://{i}.example" for i in range(6)]},
        }
        for name, edits in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    merge_action({"goal": "原本的目标是合法的"}, **edits)

    def test_a_proposal_without_a_goal_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            merge_action({"mode": "script"})


class ChatActionAnchorTests(unittest.TestCase):
    """The proposal rides the answer; the anchor to the run is one-shot."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "FCODE_CHAT_MOCK_DELAY": "0",
            "FCODE_CHAT_MOCK_ACTION": json.dumps({
                "goal": "查周三上海到杭州的高铁",
                "mode": "research",
                "sources": ["https://example.com/tickets"],
                "reason": "需要实时车次",
            }, ensure_ascii=False),
        }, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        reset()
        self.addCleanup(reset)
        self.service = ChatService(pathlib.Path(self._tmp.name) / "chat.sqlite")
        self.addCleanup(self.service.close)

    def answer(self):
        events = list(self.service.send(thread_id=None, text="帮我查查周三的高铁票", model="mock"))
        thread_id = events[0]["thread_id"]
        thread = self.service.get_thread(thread_id)
        return thread_id, thread["messages"][-1]

    def test_the_proposal_rides_the_answer_and_the_block_is_hidden(self) -> None:
        _, message = self.answer()
        self.assertNotIn("fcode_action", message["text"])
        self.assertIn("离线 Mock 回复", message["text"])
        self.assertEqual(message["action"]["goal"], "查周三上海到杭州的高铁")
        self.assertEqual(message["action"]["mode"], "research")
        self.assertEqual(message["action"]["sources"], ["https://example.com/tickets"])
        self.assertNotIn("run", message)

    def test_confirming_anchors_the_run_once(self) -> None:
        thread_id, message = self.answer()
        self.service.link_run(thread_id, message["id"], "run-20260101-000000-abcd")
        thread = self.service.get_thread(thread_id)
        anchored = thread["messages"][-1]
        self.assertEqual(anchored["run"]["id"], "run-20260101-000000-abcd")
        self.assertEqual(anchored["run"]["goal"], "查周三上海到杭州的高铁")
        self.assertEqual(anchored["run"]["mode"], "research")
        self.service.store.verify_chain(thread_id)
        with self.assertRaises(AgentError) as caught:
            self.service.link_run(thread_id, message["id"], "run-20260101-000000-eeee")
        self.assertEqual(caught.exception.code, "CHAT_ACTION_ALREADY_STARTED")
        self.assertEqual(caught.exception.details["run_id"], "run-20260101-000000-abcd")
        # The refused second anchor left no event behind.
        self.assertEqual(self.service.get_thread(thread_id)["events"], 4)

    def test_the_anchor_records_what_was_approved(self) -> None:
        thread_id, message = self.answer()
        self.service.link_run(
            thread_id, message["id"], "run-20260101-000000-abcd",
            goal="用户确认后的目标", mode="script",
        )
        anchored = self.service.get_thread(thread_id)["messages"][-1]
        self.assertEqual(anchored["run"]["goal"], "用户确认后的目标")
        self.assertEqual(anchored["run"]["mode"], "script")

    def test_anchoring_a_non_proposal_fails_closed(self) -> None:
        thread_id, message = self.answer()
        question = self.service.get_thread(thread_id)["messages"][0]
        with self.assertRaises(AgentError) as caught:
            self.service.link_run(thread_id, question["id"], "run-20260101-000000-abcd")
        self.assertEqual(caught.exception.code, "CHAT_MESSAGE_INVALID")
        with self.assertRaises(AgentError) as caught:
            self.service.link_run(thread_id, "a-does-not-exist", "run-20260101-000000-abcd")
        self.assertEqual(caught.exception.code, "CHAT_MESSAGE_INVALID")

    def test_an_answer_without_a_proposal_cannot_be_anchored(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_CHAT_MOCK_ACTION": ""}, clear=False):
            service = ChatService(pathlib.Path(self._tmp.name) / "plain.sqlite")
            self.addCleanup(service.close)
            events = list(service.send(thread_id=None, text="随便聊聊", model="mock"))
            thread_id = events[0]["thread_id"]
            message = service.get_thread(thread_id)["messages"][-1]
            # The old projection emitted {"action": {}} here; an empty object is
            # truthy in the viewer and painted a blank proposal card. The field
            # is now absent, and anchoring still fails closed.
            self.assertNotIn("action", message)
            with self.assertRaises(AgentError) as caught:
                service.link_run(thread_id, message["id"], "run-20260101-000000-abcd")
            self.assertEqual(caught.exception.code, "CHAT_ACTION_MISSING")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(url: str, *, method: str = "GET", payload: dict | None = None, headers: dict | None = None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    merged = {"Content-Type": "application/json"} if body is not None else {}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


class ConfirmEndpointTests(unittest.TestCase):
    """POST /api/chat/threads/{id}/confirm — one proposal, one run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = pathlib.Path(cls.tmp.name)
        cls.port = free_port()
        env = dict(os.environ)
        env.update({
            "FCODE_RUNS_DIR": str(root / "runs"),
            "FCODE_CONFIG_DIR": str(root / "config"),
            "FCODE_SKIP_LEGACY_CONFIG_MIGRATION": "1",
            "FCODE_CHAT_MOCK_DELAY": "0",
            "FCODE_CHAT_MOCK_ACTION": json.dumps({
                "goal": "查周三上海到杭州的高铁",
                "mode": "research",
                "sources": ["https://example.com/tickets"],
            }, ensure_ascii=False),
        })
        cls.process = subprocess.Popen(
            [sys.executable, str(SERVER), "--repo", str(ROOT), "--port", str(cls.port)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls._stop)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if request(f"http://127.0.0.1:{cls.port}/api/health")[0] == 200:
                    return
            except Exception:  # noqa: BLE001 — the server is still coming up
                time.sleep(0.2)
        raise RuntimeError("server did not become healthy")

    @classmethod
    def _stop(cls) -> None:
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover — best effort
                cls.process.kill()

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def propose(self) -> tuple[str, str]:
        """One chat turn that ends in a proposal; returns (thread, message)."""
        req = urllib.request.Request(
            f"{self.base()}/api/chat/send",
            data=json.dumps({"text": "帮我查查周三的高铁票", "model": "mock"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            stream = response.read().decode("utf-8", "replace")
        frames = [
            json.loads(line[5:].strip())
            for line in stream.splitlines()
            if line.startswith("data:") and line[5:].strip()
        ]
        done = [frame for frame in frames if frame.get("type") == "done"]
        self.assertTrue(done, f"no done frame in {stream[:400]}")
        thread_id = done[-1]["thread_id"]
        status, body = request(f"{self.base()}/api/chat/threads/{thread_id}")
        self.assertEqual(status, 200, body)
        thread = json.loads(body)
        assistant = thread["messages"][-1]
        self.assertEqual(assistant["action"]["mode"], "research")
        return thread_id, assistant["id"]

    def test_confirm_creates_one_governed_run_anchored_to_the_thread(self) -> None:
        thread_id, message_id = self.propose()
        url = f"{self.base()}/api/chat/threads/{thread_id}/confirm"
        status, body = request(url, method="POST", payload={
            "messageId": message_id, "model": "mock", "effort": "",
        })
        self.assertEqual(status, 202, body)
        run = json.loads(body)["run"]
        self.assertEqual(run["template"], "general-research")
        self.assertEqual(run["objective"], "查周三上海到杭州的高铁")
        self.assertEqual(run["spec"]["source"], "chat-action")
        self.assertEqual(run["spec"]["mode"], "research")
        self.assertEqual(run["spec"]["sources"], ["https://example.com/tickets"])
        origin = run["spec"]["origin_thread"]
        self.assertEqual(origin["threadId"], thread_id)
        self.assertEqual([m["role"] for m in origin["messages"]], ["user", "assistant"])
        self.assertIn("周三", origin["messages"][0]["text"])
        # The thread now points at the run, and only at this one.
        _, thread_body = request(f"{self.base()}/api/chat/threads/{thread_id}")
        anchored = json.loads(thread_body)["messages"][-1]
        self.assertEqual(anchored["run"]["id"], run["id"])
        # A second confirmation is refused with the run that already exists.
        status, body = request(url, method="POST", payload={"messageId": message_id})
        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body)["runId"], run["id"])

    def test_confirm_applies_operator_edits(self) -> None:
        thread_id, message_id = self.propose()
        url = f"{self.base()}/api/chat/threads/{thread_id}/confirm"
        status, body = request(url, method="POST", payload={
            "messageId": message_id,
            "goal": "查周三高铁并整理成表格",
            "mode": "script",
            "sources": [],
            "model": "mock",
        })
        self.assertEqual(status, 202, body)
        run = json.loads(body)["run"]
        self.assertEqual(run["objective"], "查周三高铁并整理成表格")
        self.assertEqual(run["spec"]["mode"], "script")
        self.assertNotIn("sources", run["spec"])
        _, thread_body = request(f"{self.base()}/api/chat/threads/{thread_id}")
        anchored = json.loads(thread_body)["messages"][-1]
        self.assertEqual(anchored["run"]["goal"], "查周三高铁并整理成表格")
        self.assertEqual(anchored["run"]["mode"], "script")

    def test_confirm_refuses_bad_input(self) -> None:
        thread_id, message_id = self.propose()
        url = f"{self.base()}/api/chat/threads/{thread_id}/confirm"
        cases = {
            "no message id": ({"messageId": ""}, 400),
            "unknown message": ({"messageId": "a-nope"}, 404),
            "unknown thread": ({"messageId": message_id}, 404),
            "short goal": ({"messageId": message_id, "goal": "短"}, 400),
            "unknown mode": ({"messageId": message_id, "mode": "deploy"}, 400),
            "bad approval": ({"messageId": message_id, "approval": "always"}, 400),
            "too many sources": ({"messageId": message_id, "sources": [f"https://{i}.example" for i in range(6)]}, 400),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(case=name):
                target = url
                if name == "unknown thread":
                    target = f"{self.base()}/api/chat/threads/chat-nope/confirm"
                elif name == "unknown message":
                    payload = {"messageId": "a-nope"}
                status, body = request(target, method="POST", payload=payload)
                self.assertEqual(status, expected, f"{name}: {body}")

    def test_confirm_refuses_a_message_that_carries_no_proposal(self) -> None:
        # The proposal-free answer has to be produced with the mock's scripted
        # block switched off, which only the in-process service can do; the
        # endpoint's own refusal is therefore asserted on an id that exists in
        # no thread at all, and the CHAT_ACTION_MISSING branch is unit-tested.
        thread_id, message_id = self.propose()
        status, body = request(
            f"{self.base()}/api/chat/threads/{thread_id}/confirm",
            method="POST",
            payload={"messageId": "a-nonexistent"},
        )
        self.assertEqual(status, 404, body)
        status, _ = request(
            f"{self.base()}/api/chat/threads/{thread_id}/confirm",
            method="POST",
            payload={"messageId": message_id, "goal": ""},
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
"""DeepSeek transport, pricing and choice parsing (no live network)."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.providers.deepseek import (  # noqa: E402
    DeepSeekPolicy,
    DeepSeekProvider,
    PriceSnapshot,
)
from flowagent.runtime.policy import (  # noqa: E402
    ActionCandidate,
    DecisionPacket,
    PacketProvider,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _price() -> PriceSnapshot:
    return PriceSnapshot(
        input_microusd_per_mtok=1_000_000,
        output_microusd_per_mtok=2_000_000,
        cache_hit_microusd_per_mtok=500_000,
        as_of="2026-10-21",
    )


class DeepSeekTests(unittest.TestCase):
    def _fake(self, payload: dict):
        seen: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return _Response(payload)

        return seen, fake_urlopen

    def test_provider_request_and_cost(self) -> None:
        seen, fake = self._fake({
            "id": "req-1",
            "choices": [{"message": {"content": json.dumps({
                "version": "flowagent.plan/v1",
                "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}],
                "todo_id": "t1",
            })}}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "total_tokens": 1100,
                "prompt_cache_hit_tokens": 200,
            },
        })
        provider = DeepSeekProvider(api_key="sk-test", price=_price(), max_prompt_tokens=4096, max_tokens=512)
        with mock.patch("flowagent.providers.deepseek.urllib.request.urlopen", fake):
            proposal = provider.propose({"objective": "test", "round": 1})
        self.assertEqual(seen["url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-test")
        self.assertEqual(seen["body"]["model"], "deepseek-chat")
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(proposal.tokens, 1100)
        self.assertEqual(proposal.cost_microusd, 1100)
        self.assertEqual(proposal.metadata["provider_request_id"], "req-1")
        self.assertEqual(proposal.plan["steps"][0]["tool"], "list_documents")
        self.assertEqual(provider.reserve_tokens, 4608)
        self.assertEqual(provider.reserve_cost_microusd, 5120)

    def test_policy_choice_and_cost(self) -> None:
        seen, fake = self._fake({
            "id": "req-2",
            "choices": [{"message": {"content": '{"choice":"a2"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        })
        packet = DecisionPacket(
            objective="test",
            round_no=1,
            state={},
            last_evidence=None,
            memory_cards=[],
            budget={},
            candidates=[
                ActionCandidate("a1", "t1", "list_documents", {}, "list"),
                ActionCandidate("a2", "t2", "read_document", {"doc_id": "doc_fy2024"}, "read"),
            ],
        )
        policy = DeepSeekPolicy(api_key="sk-test", price=_price(), max_packet_tokens=4096, max_tokens=64)
        with mock.patch("flowagent.providers.deepseek.urllib.request.urlopen", fake):
            choice = policy.choose(packet)
        self.assertEqual(choice.candidate_id, "a2")
        self.assertEqual(choice.tokens, 110)
        self.assertEqual(choice.cost_microusd, 120)
        self.assertEqual(choice.request_id, "req-2")
        self.assertEqual(choice.repaired, False)
        self.assertEqual(choice.metadata["billing"], "external")
        self.assertEqual(choice.metadata["price_as_of"], "2026-10-21")
        self.assertEqual(choice.metadata["price_source"], "host")
        self.assertNotIn("usage_missing", choice.metadata)
        self.assertEqual(policy.reserve_tokens, 4160)
        self.assertEqual(seen["body"]["max_tokens"], 64)

        class FrozenChoicePolicy:
            name = policy.name
            model = policy.model

            def choose(self, packet):  # noqa: ARG002
                return choice

        proposal = PacketProvider(packet, FrozenChoicePolicy()).propose({})
        receipt = proposal.receipt()
        self.assertEqual(receipt["metadata"]["provider_request_id"], "req-2")
        self.assertEqual(receipt["metadata"]["price_as_of"], "2026-10-21")
        self.assertEqual(receipt["metadata"]["price_source"], "host")
        self.assertEqual(receipt["metadata"]["billing"], "external")

    def test_invalid_choice_fails_without_repair(self) -> None:
        calls = {"n": 0}
        _, fake = self._fake({"choices": [{"message": {"content": '{"choice":"a9"}'}}], "usage": {}})

        def counting(request, timeout):  # noqa: ARG001
            calls["n"] += 1
            return fake(request, timeout)

        packet = DecisionPacket(
            objective="test",
            round_no=1,
            state={},
            last_evidence=None,
            memory_cards=[],
            budget={},
            candidates=[ActionCandidate("a1", "t1", "list_documents", {}, "list")],
        )
        with mock.patch("flowagent.providers.deepseek.urllib.request.urlopen", counting):
            with self.assertRaises(AgentError) as raised:
                DeepSeekPolicy(api_key="sk-test", price=_price()).choose(packet)
        self.assertEqual(raised.exception.code, "POLICY_INVALID_CHOICE")
        self.assertEqual(calls["n"], 1)

    def test_http_auth_error_is_not_a_silent_fallback(self) -> None:
        def fail(request, timeout):  # noqa: ARG001
            raise urllib.error.HTTPError(
                request.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
            )

        provider = DeepSeekProvider(api_key="sk-test", price=_price())
        with mock.patch("flowagent.providers.deepseek.urllib.request.urlopen", fail):
            with self.assertRaises(AgentError) as raised:
                provider.propose({"objective": "test", "round": 1})
        self.assertEqual(raised.exception.code, "MODEL_AUTH_FAILED")

    def test_price_snapshot_validation(self) -> None:
        with self.assertRaises(ValueError):
            PriceSnapshot(input_microusd_per_mtok=-1, output_microusd_per_mtok=1, as_of="x")
        with self.assertRaises(ValueError):
            PriceSnapshot(input_microusd_per_mtok=1, output_microusd_per_mtok=1, as_of="")

    def test_reserve_uses_rendered_prompt(self) -> None:
        provider = DeepSeekProvider(
            api_key="sk-test", price=_price(), max_prompt_tokens=4096, max_tokens=256
        )
        tokens, cost = provider.reserve({"objective": "test", "round": 1})
        self.assertGreater(tokens, 256)
        self.assertLess(tokens, 4096 + 256)
        self.assertGreater(cost, 0)

    def test_http_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DeepSeekProvider(api_key="sk-test", price=_price(), base_url="http://api.deepseek.com/v1")


if __name__ == "__main__":
    unittest.main()

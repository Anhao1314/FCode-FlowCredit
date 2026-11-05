"""Kimi transport, pricing, calibration and receipts (no live network)."""

from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.pacing import reset  # noqa: E402
from flowagent.providers.kimi import (  # noqa: E402
    KimiPriceSnapshot,
    KimiProvider,
    load_kimi_settings,
)

PLAN_TEXT = json.dumps(
    {
        "version": "flowagent.plan/v1",
        "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}],
        "todo_id": "t1",
    },
    ensure_ascii=False,
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


def _price() -> KimiPriceSnapshot:
    return KimiPriceSnapshot(
        input_microusd_per_mtok=1_000_000,
        output_microusd_per_mtok=2_000_000,
        as_of="2026-10-24",
    )


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "KIMI_API_KEY": "sk-kimi-test-not-real",
        "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "1000000",
        "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "2000000",
        "KIMI_PRICE_AS_OF": "2026-10-24",
    }
    env.update(overrides)
    return env


class KimiSettingsTests(unittest.TestCase):
    def test_settings_resolve_environment_and_redact_key(self) -> None:
        settings = load_kimi_settings(
            env=_env(
                KIMI_MODEL="kimi-k3.1-pro-highspeed",
                KIMI_BASE_URL="https://api.moonshot.cn/v1/",
                KIMI_TIMEOUT_SECONDS="12.5",
                KIMI_MAX_COMPLETION_TOKENS="77",
            )
        )
        self.assertEqual(settings.model, "kimi-k3.1-pro-highspeed")
        self.assertEqual(settings.base_url, "https://api.moonshot.cn/v1")
        self.assertEqual(settings.timeout, 12.5)
        self.assertEqual(settings.max_completion_tokens, 77)
        redacted = settings.redacted()
        self.assertEqual(redacted["apiKey"], "env:KIMI_API_KEY")
        self.assertNotIn("sk-kimi-test-not-real", repr(redacted))

    def test_default_model_is_the_account_accessible_e2e_model(self) -> None:
        settings = load_kimi_settings(env=_env())
        self.assertEqual(settings.model, "kimi-k2.6")
        self.assertEqual(settings.redacted()["model"], "kimi-k2.6")

    def test_prices_and_date_are_required(self) -> None:
        env = _env()
        del env["KIMI_INPUT_PRICE_MICROUSD_PER_MTOK"]
        del env["KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK"]
        with self.assertRaisesRegex(ValueError, "KIMI_INPUT_PRICE"):
            load_kimi_settings(env=env)
        with self.assertRaisesRegex(ValueError, "KIMI_PRICE_AS_OF"):
            load_kimi_settings(env=_env(KIMI_PRICE_AS_OF=""))

    def test_http_base_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "https"):
            load_kimi_settings(env=_env(), base_url="http://api.moonshot.cn/v1")


class KimiProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        reset()

    def tearDown(self) -> None:
        reset()

    def _fake(self, payload: dict):
        seen: dict[str, object] = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return _Response(payload)

        return seen, fake_urlopen

    def test_temperature_env_is_optional_and_parsed(self) -> None:
        unset = load_kimi_settings(env=_env())
        self.assertIsNone(unset.temperature)
        self.assertIsNone(unset.redacted()["temperature"])
        settings = load_kimi_settings(env=_env(KIMI_TEMPERATURE="1"))
        self.assertEqual(settings.temperature, 1.0)
        with self.assertRaisesRegex(ValueError, "KIMI_TEMPERATURE"):
            load_kimi_settings(env=_env(KIMI_TEMPERATURE="hot"))

    def test_min_call_interval_env_is_optional(self) -> None:
        settings = load_kimi_settings(env=_env())
        self.assertEqual(settings.min_call_interval_seconds, 0.0)
        settings = load_kimi_settings(env=_env(KIMI_MIN_CALL_INTERVAL_SECONDS="22"))
        self.assertEqual(settings.min_call_interval_seconds, 22.0)
        with self.assertRaisesRegex(ValueError, "KIMI_MIN_CALL_INTERVAL"):
            load_kimi_settings(env=_env(KIMI_MIN_CALL_INTERVAL_SECONDS="-1"))

    def test_request_cost_calibration_and_hmac_receipt(self) -> None:
        seen, fake = self._fake(
            {
                "id": "kimi-req-1",
                "choices": [{"message": {"content": PLAN_TEXT}}],
                "usage": {
                    "prompt_tokens": 10000,
                    "completion_tokens": 100,
                    "total_tokens": 10100,
                    "prompt_tokens_details": {"cached_tokens": 900},
                },
            }
        )
        issued: list[dict] = []
        raw: list[dict] = []

        def issue(**kwargs):
            issued.append(kwargs)
            return "receipt-ref-1"

        with tempfile.TemporaryDirectory() as tmp:
            provider = KimiProvider(
                api_key="sk-kimi-test-not-real",
                price=_price(),
                calibration_path=pathlib.Path(tmp) / "kimi-calibration.json",
                issue_receipt=issue,
                raw_sink=raw.append,
            )
            with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", fake):
                proposal = provider.propose(
                    {"objective": "test", "round": 1, "intent": {"task_id": "t", "round": 1}}
                )

        self.assertEqual(seen["url"], "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-kimi-test-not-real")
        body = seen["body"]
        self.assertEqual(body["model"], "kimi-k2.6")
        # 4096, not 512: the reasoning channel can consume a small budget
        # entirely, leaving an empty content (observed on a real delegation run).
        self.assertEqual(body["max_completion_tokens"], 8192)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("max_tokens", body)
        self.assertNotIn("temperature", body)

        # Cache hits are metadata only in v1: all 10000 input tokens use input price.
        self.assertEqual(proposal.cost_microusd, 10200)
        self.assertEqual(proposal.tokens, 10100)
        self.assertEqual(proposal.metadata["cached_tokens"], 900)
        self.assertEqual(proposal.metadata["cache_billing"], "input-price-v1")
        self.assertEqual(proposal.metadata["reasoning_effort"], "low")
        self.assertEqual(proposal.metadata["raw_content"], PLAN_TEXT)
        self.assertEqual(proposal.metadata["hmac_receipt_ref"], "receipt-ref-1")
        self.assertEqual(issued[0]["usage"]["cost_microusd"], 10200)
        self.assertEqual(raw[0]["content"], PLAN_TEXT)

        report = proposal.metadata["calibration"]
        self.assertEqual(report["usage_prompt_tokens"], 10000)
        self.assertEqual(report["reserved_prompt_tokens"], provider.calibration.reports[0]["reserved_prompt_tokens"])
        self.assertTrue(report["adjusted"])
        self.assertIn("max(ceil(chars/2)", report["new_formula"])

        # The calibration report travels inside receipt metadata into committed
        # event bodies; canonical_json rejects floats, so it must be int-only.
        def _float_free(node: object) -> bool:
            if isinstance(node, float):
                return False
            if isinstance(node, dict):
                return all(_float_free(v) for v in node.values())
            if isinstance(node, (list, tuple)):
                return all(_float_free(v) for v in node)
            return True

        self.assertTrue(_float_free(report))
        self.assertIsInstance(report["ratio_actual_to_reserve_bp"], int)

    def test_calibrated_reserve_uses_measured_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "kimi-calibration.json"
            provider = KimiProvider(
                api_key="sk-kimi-test-not-real",
                price=_price(),
                calibration_path=path,
            )
            tokens_before, _ = provider.reserve({"objective": "test", "round": 1})
            provider.calibration.measured_ratio = 1.0
            provider.calibration.save()
            tokens_after, _ = provider.reserve({"objective": "test", "round": 1})
        self.assertGreater(tokens_after, tokens_before)

    def test_temperature_sent_only_when_host_pins_it(self) -> None:
        # K2/K3 chat models reject every temperature except 1; without a host
        # override the field is omitted so the server default applies.
        seen, fake = self._fake(
            {
                "id": "kimi-temp-1",
                "choices": [{"message": {"content": PLAN_TEXT}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        provider = KimiProvider(
            api_key="sk-kimi-test-not-real",
            price=_price(),
            temperature=1,
        )
        with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", fake):
            proposal = provider.propose(
                {"objective": "test", "round": 1, "intent": {"task_id": "t", "round": 1}}
            )
        self.assertEqual(seen["body"]["temperature"], 1)
        self.assertEqual(proposal.metadata["temperature"], 1)

    def test_pacing_waits_full_interval_on_first_call(self) -> None:
        # A fresh process conservatively assumes a sibling or resumed worker
        # may have just hit the API (org RPM ceilings are shared).
        _, fake = self._fake(
            {"id": "kimi-pace-1", "choices": [{"message": {"content": PLAN_TEXT}}], "usage": {}}
        )
        provider = KimiProvider(
            api_key="sk-kimi-test-not-real", price=_price(), min_call_interval_seconds=30
        )
        with mock.patch("flowagent.providers.kimi.time.sleep") as fake_sleep:
            with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", fake):
                provider.propose(
                    {"objective": "test", "round": 1, "intent": {"task_id": "t", "round": 1}}
                )
        # 首次调用等待**完整间隔**：断言契约而不是字面量。
        # `reserve_slot` 返回 `(moment + interval) - moment`，IEEE-754 下这个减法
        # 不保证恒等于 interval——实测出现过 30.000000000000014 与 29.999999999999993
        # 两侧的尾差（分别在 ubuntu/3.11 与 ubuntu/3.13 上各红一次）。
        # 所以这里比对「≈ 间隔」，误差限 1 毫秒，比时钟分辨率还紧，却不再是平台抽奖。
        first_waits = fake_sleep.call_args_list[0].args[0]
        self.assertAlmostEqual(first_waits, 30, delta=0.001)

    def test_pacing_waits_remainder_between_calls(self) -> None:
        _, fake = self._fake(
            {"id": "kimi-pace-2", "choices": [{"message": {"content": PLAN_TEXT}}], "usage": {}}
        )
        provider = KimiProvider(
            api_key="sk-kimi-test-not-real", price=_price(), min_call_interval_seconds=30
        )
        with mock.patch("flowagent.providers.kimi.time.sleep") as fake_sleep:
            with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", fake):
                provider.propose(
                    {"objective": "test", "round": 1, "intent": {"task_id": "t", "round": 1}}
                )
                provider._last_call_at = time.monotonic()
                provider.propose(
                    {"objective": "test", "round": 2, "intent": {"task_id": "t", "round": 2}}
                )
        remainder = fake_sleep.call_args_list[1].args[0]
        self.assertGreater(remainder, 29.0)

    def test_auth_failure_is_not_retried_or_fallback(self) -> None:
        calls = {"n": 0}

        def fail(request, timeout):  # noqa: ARG001
            calls["n"] += 1
            raise urllib.error.HTTPError(
                request.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"error":"bad key"}')
            )

        provider = KimiProvider(api_key="sk-kimi-test-not-real", price=_price())
        with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", fail):
            with self.assertRaises(AgentError) as raised:
                provider.propose({"objective": "test", "round": 1})
        self.assertEqual(raised.exception.code, "MODEL_AUTH_FAILED")
        self.assertEqual(calls["n"], 1)

    def test_malformed_plan_fails_once_and_keeps_raw_details(self) -> None:
        calls = {"n": 0}
        raw_content = "不是 JSON"
        _, fake = self._fake(
            {"id": "kimi-req-2", "choices": [{"message": {"content": raw_content}}], "usage": {}}
        )

        def counting(request, timeout):  # noqa: ARG001
            calls["n"] += 1
            return fake(request, timeout)

        provider = KimiProvider(api_key="sk-kimi-test-not-real", price=_price())
        with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", counting):
            with self.assertRaises(AgentError) as raised:
                provider.propose({"objective": "test", "round": 1})
        self.assertEqual(raised.exception.code, "PROPOSAL_MALFORMED")
        self.assertEqual(raised.exception.details["raw_content"], raw_content)
        self.assertEqual(calls["n"], 1)



    def test_a_reasoning_only_answer_names_the_budget_it_needs(self) -> None:
        """Empty content with a populated reasoning channel is a budget problem,
        not an absent answer: the operator can fix exactly one number."""
        seen, fake = self._fake({
            "id": "kimi-req-empty",
            "choices": [{
                "message": {"content": "", "reasoning_content": "让我想想……" * 40},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
        })
        with mock.patch("flowagent.providers.kimi.urllib.request.urlopen", fake):
            with self.assertRaises(AgentError) as error:
                KimiProvider(api_key="sk-kimi-test-not-real", price=_price()).propose(
                    {"objective": "t", "round": 1, "intent": {"task_id": "t", "round": 1}}
                )
        self.assertEqual(error.exception.code, "MODEL_CALL_FAILED")
        self.assertIn("补全预算", error.exception.message)
        self.assertIn("KIMI_MAX_COMPLETION_TOKENS", error.exception.message)
        self.assertTrue(seen["body"]["max_completion_tokens"])

if __name__ == "__main__":
    unittest.main()

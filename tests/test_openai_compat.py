"""The OpenAI-compatible engine: generic endpoints, presets, enumeration.

All offline — HTTP is intercepted at urllib like the preset suites. What is
locked here: the generic loader's env matrix, the generic wire shape
(temperature sent, keyless local, max_completion_tokens field), both billing
contracts, the server-side catalog that enumerates whatever the host
configured, and the factory's declared-prefix branch.
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.demo.provider_factory import build_provider  # noqa: E402
from flowagent.providers.deepseek import DeepSeekProvider  # noqa: E402
from flowagent.providers.kimi import KimiProvider  # noqa: E402
from flowagent.providers.openai_compat import (  # noqa: E402
    GENERIC_COMPAT_DEFAULTS,
    OpenAICompatProvider,
    PriceSnapshot,
    declared_compat_prefixes,
    load_compat_settings,
)
from flowagent.providers.catalog import compat_settings_for, provider_catalog  # noqa: E402

PLAN_TEXT = json.dumps({
    "version": "flowagent.plan/v1",
    "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}],
    "todo_id": "t1",
}, ensure_ascii=False)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _generic_env(**overrides: str) -> dict[str, str]:
    env = {
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
        "OLLAMA_MODEL": "fcode-qwen3-4b:latest",
        "OLLAMA_INPUT_PRICE_MICROUSD_PER_MTOK": "0",
        "OLLAMA_OUTPUT_PRICE_MICROUSD_PER_MTOK": "0",
        "OLLAMA_PRICE_AS_OF": "2026-10-25",
    }
    env.update(overrides)
    return env


class CompatLoaderTests(unittest.TestCase):
    def test_generic_env_resolves_with_keyless_local_http(self) -> None:
        settings = load_compat_settings(
            "OLLAMA", env=_generic_env(), defaults=GENERIC_COMPAT_DEFAULTS
        )
        self.assertEqual(settings.label, "ollama")
        self.assertEqual(settings.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.price.input_microusd_per_mtok, 0)
        self.assertFalse(settings.supports_effort)

    def test_prices_and_date_have_no_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "OLLAMA_INPUT_PRICE"):
            load_compat_settings("OLLAMA", env={"OLLAMA_BASE_URL": "http://x", "OLLAMA_MODEL": "m"}, defaults=GENERIC_COMPAT_DEFAULTS)
        with self.assertRaisesRegex(ValueError, "OLLAMA_PRICE_AS_OF"):
            load_compat_settings("OLLAMA", env={
                "OLLAMA_BASE_URL": "http://x", "OLLAMA_MODEL": "m",
                "OLLAMA_INPUT_PRICE_MICROUSD_PER_MTOK": "0",
                "OLLAMA_OUTPUT_PRICE_MICROUSD_PER_MTOK": "0",
            }, defaults=GENERIC_COMPAT_DEFAULTS)

    def test_base_url_and_model_are_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "OLLAMA_BASE_URL"):
            load_compat_settings("OLLAMA", env={}, defaults=GENERIC_COMPAT_DEFAULTS)
        with self.assertRaisesRegex(ValueError, "OLLAMA_MODEL"):
            load_compat_settings("OLLAMA", env={"OLLAMA_BASE_URL": "http://x"}, defaults=GENERIC_COMPAT_DEFAULTS)

    def test_https_still_required_when_preset_says_so(self) -> None:
        with self.assertRaisesRegex(ValueError, "OLLAMA_BASE_URL must use https"):
            load_compat_settings("OLLAMA", env={
                "OLLAMA_BASE_URL": "http://cloud.example/v1", "OLLAMA_MODEL": "m",
                "OLLAMA_INPUT_PRICE_MICROUSD_PER_MTOK": "0",
                "OLLAMA_OUTPUT_PRICE_MICROUSD_PER_MTOK": "0",
                "OLLAMA_PRICE_AS_OF": "d",
            }, defaults={"require_https": True})

    def test_optional_knobs_parse_and_validate(self) -> None:
        settings = load_compat_settings("OLLAMA", env=_generic_env(
            OLLAMA_TEMPERATURE="0.3",
            OLLAMA_MIN_CALL_INTERVAL_SECONDS="5",
            OLLAMA_MAX_COMPLETION_TOKENS="256",
            OLLAMA_REASONING_EFFORT="high",
            OLLAMA_SUPPORTS_EFFORT="1",
        ), defaults=GENERIC_COMPAT_DEFAULTS)
        self.assertEqual(settings.temperature, 0.3)
        self.assertEqual(settings.min_call_interval_seconds, 5.0)
        self.assertEqual(settings.max_completion_tokens, 256)
        self.assertEqual(settings.reasoning_effort, "high")
        self.assertTrue(settings.supports_effort)
        with self.assertRaisesRegex(ValueError, "OLLAMA_TEMPERATURE"):
            load_compat_settings("OLLAMA", env=_generic_env(OLLAMA_TEMPERATURE="abc"), defaults=GENERIC_COMPAT_DEFAULTS)
        with self.assertRaisesRegex(ValueError, "OLLAMA_REASONING_EFFORT"):
            load_compat_settings("OLLAMA", env=_generic_env(OLLAMA_REASONING_EFFORT="turbo"), defaults=GENERIC_COMPAT_DEFAULTS)

    def test_declared_prefixes_parse_and_dedupe(self) -> None:
        self.assertEqual(
            declared_compat_prefixes({"FCODE_OPENAI_COMPAT_PROVIDERS": " lmstudio, LMSTUDIO, ollama ,x"}),
            ("LMSTUDIO", "OLLAMA", "X"),
        )
        self.assertEqual(declared_compat_prefixes({}), ())


class BillingModeTests(unittest.TestCase):
    def test_input_price_v1_charges_every_input_token(self) -> None:
        price = PriceSnapshot(input_microusd_per_mtok=1_000_000, output_microusd_per_mtok=2_000_000, as_of="d", billing="input-price-v1")
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 900}}
        self.assertEqual(price.cost(usage), 1000 + 200)

    def test_cache_split_uses_the_cheaper_hit_price(self) -> None:
        price = PriceSnapshot(
            input_microusd_per_mtok=1_000_000, output_microusd_per_mtok=2_000_000,
            cache_hit_microusd_per_mtok=500_000, as_of="d", billing="cache-split",
        )
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_cache_hit_tokens": 400}
        self.assertEqual(price.cost(usage), 200 + 600 + 200)


class GenericWireTests(unittest.TestCase):
    def _provider(self, **env_overrides) -> OpenAICompatProvider:
        settings = load_compat_settings("OLLAMA", env=_generic_env(**env_overrides), defaults=GENERIC_COMPAT_DEFAULTS)
        return OpenAICompatProvider.from_settings(settings)

    def test_generic_body_shape_temperature_sent_and_keyless(self) -> None:
        provider = self._provider()
        seen: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return _Response({
                "id": "chatcmpl-x",
                "choices": [{"message": {"content": PLAN_TEXT}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            })

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            proposal = provider.propose({"objective": "t", "round": 1, "intent": {"task_id": "t", "round": 1}})
        self.assertEqual(seen["url"], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertIsNone(seen["auth"], "keyless local endpoints send no Authorization header")
        self.assertEqual(seen["body"]["temperature"], 0, "send-configured defaults to deterministic 0")
        self.assertEqual(seen["body"]["max_completion_tokens"], 512)
        self.assertNotIn("max_tokens", seen["body"])
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})
        self.assertNotIn("reasoning_effort", seen["body"])
        # Full receipts for declared endpoints: prices, calibration, cache label.
        self.assertEqual(proposal.metadata["input_microusd_per_mtok"], 0)
        self.assertIn("calibration", proposal.metadata)
        self.assertEqual(proposal.cost_microusd, 0, "free endpoint books a zero-cost receipt")
        self.assertEqual(proposal.metadata["billing"], "external")

    def test_transport_failure_is_unknown_and_unretried(self) -> None:
        provider = self._provider()
        calls = {"n": 0}

        def failing(request, timeout):  # noqa: ARG001
            calls["n"] += 1
            raise TimeoutError("late")

        with mock.patch("urllib.request.urlopen", failing):
            with self.assertRaises(AgentError) as raised:
                provider.propose({"objective": "t", "round": 1})
        self.assertEqual(raised.exception.code, "MODEL_CALL_FAILED")
        self.assertEqual(calls["n"], 1)


class PresetsAreTheEngineTests(unittest.TestCase):
    def test_preset_providers_are_engine_instances(self) -> None:
        price = PriceSnapshot(input_microusd_per_mtok=1, output_microusd_per_mtok=2, as_of="d", billing="input-price-v1")
        kimi = KimiProvider(api_key="k", price=price)
        deep = DeepSeekProvider(api_key="k", price=PriceSnapshot(input_microusd_per_mtok=1, output_microusd_per_mtok=2, as_of="d"))
        self.assertIsInstance(kimi, OpenAICompatProvider)
        self.assertIsInstance(deep, OpenAICompatProvider)
        self.assertEqual(kimi.client.completion_field, "max_completion_tokens")
        self.assertEqual(deep.client.completion_field, "max_tokens")
        self.assertEqual(kimi.client.temperature_mode, "omit-unless-pinned")
        self.assertEqual(deep.client.temperature_mode, "pin-zero")


class CatalogTests(unittest.TestCase):
    def test_catalog_enumerates_what_the_host_configured(self) -> None:
        env = {
            "KIMI_API_KEY": "sk-x", "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK": "1",
            "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK": "2", "KIMI_PRICE_AS_OF": "d",
            "DEEPSEEK_API_KEY": "sk-y", "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK": "1",
            "DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK": "2", "DEEPSEEK_PRICE_AS_OF": "d",
            "FCODE_OPENAI_COMPAT_PROVIDERS": "OLLAMA,VOLCANO",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1", "OLLAMA_MODEL": "qwen",
            "OLLAMA_INPUT_PRICE_MICROUSD_PER_MTOK": "0", "OLLAMA_OUTPUT_PRICE_MICROUSD_PER_MTOK": "0",
            "OLLAMA_PRICE_AS_OF": "d",
        }
        entries = {e["key"]: e for e in provider_catalog(env=env)}
        self.assertEqual(entries["mock"]["available"], True)
        self.assertFalse(entries["local"]["available"])
        self.assertTrue(entries["kimi"]["available"])
        self.assertTrue(entries["kimi"]["supportsEffort"])
        self.assertTrue(entries["deepseek"]["available"])
        self.assertTrue(entries["ollama"]["available"])
        self.assertEqual(entries["ollama"]["price"]["inputMicrousdPerMtok"], 0)
        # incomplete declared endpoint stays visible with its reason
        volcano = entries["volcano"]
        self.assertFalse(volcano["available"])
        self.assertIn("VOLCANO", volcano["reason"])

    def test_incomplete_presets_report_the_missing_variable(self) -> None:
        entries = {e["key"]: e for e in provider_catalog(env={})}
        self.assertFalse(entries["kimi"]["available"])
        # The reason names the exact missing KIMI_* variable (key or prices).
        self.assertRegex(entries["kimi"]["reason"], r"KIMI_[A-Z_]+")

    def test_compat_settings_for_resolves_keys(self) -> None:
        env = _generic_env(FCODE_OPENAI_COMPAT_PROVIDERS="OLLAMA")
        settings = compat_settings_for("ollama", env=env)
        self.assertIsNotNone(settings)
        self.assertEqual(settings.model, "fcode-qwen3-4b:latest")
        self.assertIsNone(compat_settings_for("nonexistent", env=env))


class FactoryGenericBranchTests(unittest.TestCase):
    def test_factory_builds_a_declared_provider_with_receipts(self) -> None:
        from flowagent.core.receipts import ReceiptStore
        from flowagent.core.store import TaskStore

        import tempfile
        tmp = tempfile.mkdtemp()
        store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
        receipts = ReceiptStore(store)
        scenario = get_scenario("full")

        with mock.patch.dict("os.environ", {
            "FCODE_OPENAI_COMPAT_PROVIDERS": "OLLAMA", **_generic_env(),
        }, clear=False):
            provider = build_provider("ollama", scenario=scenario, receipts=receipts)
            self.assertEqual(provider.name, "ollama")

            def fake_urlopen(request, timeout):  # noqa: ARG001
                return _Response({
                    "id": "chatcmpl-o",
                    "choices": [{"message": {"content": PLAN_TEXT}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
                })

            with mock.patch("urllib.request.urlopen", fake_urlopen):
                proposal = provider.propose({"objective": "t", "round": 1, "intent": {"task_id": "t", "round": 1}})
        self.assertEqual(proposal.provider, "ollama")
        # The factory wires the store's HMAC receipts for declared providers
        # exactly as it does for the presets; a ref came back and it verifies.
        ref = proposal.metadata["hmac_receipt_ref"]
        self.assertTrue(ref)
        self.assertEqual(receipts.load(ref)["usage"]["cost_microusd"], 0)
        store.close()

    def test_factory_rejects_undeclared_names_with_the_full_known_list(self) -> None:
        with mock.patch.dict("os.environ", {"FCODE_OPENAI_COMPAT_PROVIDERS": "OLLAMA"}, clear=False):
            with self.assertRaisesRegex(ValueError, "ollama"):
                build_provider("volcano", scenario=get_scenario("full"), receipts=None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

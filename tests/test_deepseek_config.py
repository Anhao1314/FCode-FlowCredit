"""DeepSeek host configuration: env resolution, validation and redaction."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.providers.deepseek_config import (  # noqa: E402
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_INPUT_PRICE_ENV,
    DEEPSEEK_MAX_TOKENS_ENV,
    DEEPSEEK_OUTPUT_PRICE_ENV,
    DEEPSEEK_PRICE_AS_OF_ENV,
    DEEPSEEK_TIMEOUT_ENV,
    load_deepseek_settings,
)


def _env(**overrides: str) -> dict[str, str]:
    env = {
        DEEPSEEK_API_KEY_ENV: "sk-test-not-real",
        DEEPSEEK_INPUT_PRICE_ENV: "270000",
        DEEPSEEK_OUTPUT_PRICE_ENV: "1100000",
        DEEPSEEK_PRICE_AS_OF_ENV: "2026-10-24",
    }
    env.update(overrides)
    return env


class DeepSeekConfigTests(unittest.TestCase):
    def test_env_resolution_and_redaction(self) -> None:
        settings = load_deepseek_settings(
            env=_env(
                DEEPSEEK_BASE_URL="https://api.example.com/v1/",
                DEEPSEEK_MODEL="deepseek-reasoner",
                DEEPSEEK_TIMEOUT_SECONDS="33.5",
                DEEPSEEK_MAX_TOKENS="17",
            )
        )
        self.assertEqual(settings.base_url, "https://api.example.com/v1")
        self.assertEqual(settings.model, "deepseek-reasoner")
        self.assertEqual(settings.timeout, 33.5)
        self.assertEqual(settings.max_tokens, 17)
        redacted = settings.redacted()
        self.assertEqual(redacted["apiKey"], f"env:{DEEPSEEK_API_KEY_ENV}")
        self.assertNotIn("sk-test-not-real", repr(redacted))
        self.assertEqual(redacted["price"]["asOf"], "2026-10-24")

    def test_cli_overrides_win_over_environment(self) -> None:
        settings = load_deepseek_settings(
            env=_env(DEEPSEEK_MODEL="deepseek-reasoner"),
            model="deepseek-chat",
            input_price=1,
            output_price=2,
            price_as_of="2026-01-01",
        )
        self.assertEqual(settings.model, "deepseek-chat")
        self.assertEqual(settings.price.input_microusd_per_mtok, 1)
        self.assertEqual(settings.price.output_microusd_per_mtok, 2)

    def test_missing_key_fails_before_request(self) -> None:
        env = _env()
        del env[DEEPSEEK_API_KEY_ENV]
        with self.assertRaisesRegex(ValueError, DEEPSEEK_API_KEY_ENV):
            load_deepseek_settings(env=env)

    def test_prices_and_date_are_required(self) -> None:
        env = _env()
        del env[DEEPSEEK_INPUT_PRICE_ENV]
        del env[DEEPSEEK_OUTPUT_PRICE_ENV]
        with self.assertRaisesRegex(ValueError, DEEPSEEK_INPUT_PRICE_ENV):
            load_deepseek_settings(env=env)

        with self.assertRaisesRegex(ValueError, DEEPSEEK_PRICE_AS_OF_ENV):
            load_deepseek_settings(env=_env(**{DEEPSEEK_PRICE_AS_OF_ENV: ""}))

    def test_http_base_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "https"):
            load_deepseek_settings(env=_env(), base_url="http://api.deepseek.com/v1")

    def test_zero_price_is_valid_and_reservation_is_an_upper_bound(self) -> None:
        settings = load_deepseek_settings(
            env=_env(
                **{
                    DEEPSEEK_INPUT_PRICE_ENV: "0",
                    DEEPSEEK_OUTPUT_PRICE_ENV: "0",
                }
            )
        )
        tokens, cost = settings.reservation("packet")
        self.assertEqual(tokens, settings.max_packet_tokens + settings.max_tokens)
        self.assertEqual(cost, 0)

    def test_invalid_numbers_are_named_not_echoed(self) -> None:
        with self.assertRaisesRegex(ValueError, DEEPSEEK_MAX_TOKENS_ENV):
            load_deepseek_settings(
                env=_env(**{DEEPSEEK_MAX_TOKENS_ENV: "not-a-number"}),
            )
        with self.assertRaisesRegex(ValueError, DEEPSEEK_TIMEOUT_ENV):
            load_deepseek_settings(
                env=_env(**{DEEPSEEK_TIMEOUT_ENV: "not-a-number"}),
            )

    def test_factories_build_the_two_api_modes(self) -> None:
        settings = load_deepseek_settings(env=_env())
        self.assertEqual(settings.policy().name, "deepseek-policy")
        provider = settings.provider()
        self.assertEqual(provider.name, "deepseek")
        self.assertEqual(provider.model, settings.model)


if __name__ == "__main__":
    unittest.main()

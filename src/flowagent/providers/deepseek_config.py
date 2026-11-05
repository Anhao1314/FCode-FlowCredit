"""Host-owned DeepSeek configuration for the API-first LLM layer.

The transport lives in :mod:`flowagent.providers.deepseek`; this module is the
thin host boundary that turns environment variables and explicit CLI overrides
into one validated settings object. It never prints or hashes an API key: the
redacted view names the environment variable, not its value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from .deepseek import DeepSeekPolicy, DeepSeekProvider, PriceSnapshot

__all__ = [
    "DEEPSEEK_API_KEY_ENV",
    "DEEPSEEK_BASE_URL_ENV",
    "DEEPSEEK_MODEL_ENV",
    "DEEPSEEK_INPUT_PRICE_ENV",
    "DEEPSEEK_OUTPUT_PRICE_ENV",
    "DEEPSEEK_CACHE_HIT_PRICE_ENV",
    "DEEPSEEK_PRICE_AS_OF_ENV",
    "DEEPSEEK_PRICE_SOURCE_ENV",
    "DEEPSEEK_TIMEOUT_ENV",
    "DEEPSEEK_MAX_TOKENS_ENV",
    "DeepSeekSettings",
    "load_deepseek_settings",
]

DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
DEEPSEEK_MODEL_ENV = "DEEPSEEK_MODEL"
DEEPSEEK_INPUT_PRICE_ENV = "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK"
DEEPSEEK_OUTPUT_PRICE_ENV = "DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK"
DEEPSEEK_CACHE_HIT_PRICE_ENV = "DEEPSEEK_CACHE_HIT_PRICE_MICROUSD_PER_MTOK"
DEEPSEEK_PRICE_AS_OF_ENV = "DEEPSEEK_PRICE_AS_OF"
DEEPSEEK_PRICE_SOURCE_ENV = "DEEPSEEK_PRICE_SOURCE"
DEEPSEEK_TIMEOUT_ENV = "DEEPSEEK_TIMEOUT_SECONDS"
DEEPSEEK_MAX_TOKENS_ENV = "DEEPSEEK_MAX_TOKENS"

_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 64
_DEFAULT_PROMPT_TOKENS = 4_096


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    return default if value is None or not value.strip() else value.strip()


def _required_text(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _optional_int(env: Mapping[str, str], name: str) -> int | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _positive_float(value: float, name: str) -> float:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_int(value: int, name: str) -> int:
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_int(value: int, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class DeepSeekSettings:
    """Validated DeepSeek runtime settings; ``api_key`` never leaves the host."""

    api_key: str
    api_key_env: str
    base_url: str
    model: str
    timeout: float
    max_tokens: int
    max_prompt_tokens: int
    max_packet_tokens: int
    price: PriceSnapshot

    def policy(self) -> DeepSeekPolicy:
        """Build the default packet-choice policy head."""
        return DeepSeekPolicy(
            api_key=self.api_key,
            price=self.price,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
            max_tokens=self.max_tokens,
            max_packet_tokens=self.max_packet_tokens,
            json_mode=True,
        )

    def provider(self, *, json_mode: bool = True) -> DeepSeekProvider:
        """Build the full-proposal comparison provider."""
        return DeepSeekProvider(
            api_key=self.api_key,
            price=self.price,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout,
            max_tokens=self.max_tokens,
            max_prompt_tokens=self.max_prompt_tokens,
            json_mode=json_mode,
        )

    def reservation(self, mode: str) -> tuple[int, int]:
        """Return the no-network upper-bound reservation for *mode*."""
        if mode not in ("packet", "proposal"):
            raise ValueError(f"unknown DeepSeek mode: {mode!r}")
        prompt_cap = self.max_packet_tokens if mode == "packet" else self.max_prompt_tokens
        tokens = prompt_cap + self.max_tokens
        cost = self.price.cost(
            {"prompt_tokens": prompt_cap, "completion_tokens": self.max_tokens}
        )
        return tokens, cost

    def redacted(self) -> dict[str, Any]:
        """The audit/display view: name the key's env var, never the key."""
        return {
            "provider": "deepseek",
            "apiKey": f"env:{self.api_key_env}",
            "baseURL": self.base_url,
            "model": self.model,
            "timeoutSeconds": self.timeout,
            "maxTokens": self.max_tokens,
            "maxPromptTokens": self.max_prompt_tokens,
            "maxPacketTokens": self.max_packet_tokens,
            "price": {
                "inputMicrousdPerMtok": self.price.input_microusd_per_mtok,
                "outputMicrousdPerMtok": self.price.output_microusd_per_mtok,
                "cacheHitMicrousdPerMtok": self.price.cache_hit_microusd_per_mtok,
                "asOf": self.price.as_of,
                "source": self.price.source,
            },
        }


def load_deepseek_settings(
    *,
    env: Mapping[str, str] | None = None,
    api_key_env: str = DEEPSEEK_API_KEY_ENV,
    base_url: str | None = None,
    model: str | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
    max_prompt_tokens: int | None = None,
    max_packet_tokens: int | None = None,
    input_price: int | None = None,
    output_price: int | None = None,
    cache_hit_price: int | None = None,
    price_as_of: str | None = None,
    price_source: str | None = None,
) -> DeepSeekSettings:
    """Resolve explicit overrides first, then environment, then safe defaults.

    Prices and their date have no default: guessing an old price would turn a
    budget ceiling into a false promise.
    """
    source = os.environ if env is None else env
    if not api_key_env.strip():
        raise ValueError("api key environment variable name is required")

    resolved_input = input_price
    if resolved_input is None:
        resolved_input = _optional_int(source, DEEPSEEK_INPUT_PRICE_ENV)
    resolved_output = output_price
    if resolved_output is None:
        resolved_output = _optional_int(source, DEEPSEEK_OUTPUT_PRICE_ENV)
    resolved_cache = cache_hit_price
    if resolved_cache is None:
        resolved_cache = _optional_int(source, DEEPSEEK_CACHE_HIT_PRICE_ENV)
    if resolved_input is None or resolved_output is None:
        raise ValueError(
            f"{DEEPSEEK_INPUT_PRICE_ENV} and {DEEPSEEK_OUTPUT_PRICE_ENV} are required"
        )
    resolved_as_of = price_as_of or _required_text(source, DEEPSEEK_PRICE_AS_OF_ENV)

    timeout_env = _text(source, DEEPSEEK_TIMEOUT_ENV, "")
    timeout_value = timeout
    if timeout_value is None and timeout_env:
        try:
            timeout_value = float(timeout_env)
        except ValueError as error:
            raise ValueError(f"{DEEPSEEK_TIMEOUT_ENV} must be a number") from error
    if timeout_value is None:
        timeout_value = _DEFAULT_TIMEOUT
    max_tokens_env = _optional_int(source, DEEPSEEK_MAX_TOKENS_ENV)
    max_tokens_value = max_tokens if max_tokens is not None else (
        max_tokens_env if max_tokens_env is not None else _DEFAULT_MAX_TOKENS
    )

    settings = DeepSeekSettings(
        api_key=_required_text(source, api_key_env),
        api_key_env=api_key_env,
        base_url=(
            base_url
            or _text(source, DEEPSEEK_BASE_URL_ENV, _DEFAULT_BASE_URL)
        ).rstrip("/"),
        model=model or _text(source, DEEPSEEK_MODEL_ENV, _DEFAULT_MODEL),
        timeout=_positive_float(timeout_value, "timeout"),
        max_tokens=_positive_int(max_tokens_value, "max_tokens"),
        max_prompt_tokens=_positive_int(
            max_prompt_tokens or _DEFAULT_PROMPT_TOKENS, "max_prompt_tokens"
        ),
        max_packet_tokens=_positive_int(
            max_packet_tokens or _DEFAULT_PROMPT_TOKENS, "max_packet_tokens"
        ),
        price=PriceSnapshot(
            input_microusd_per_mtok=_non_negative_int(
                resolved_input, DEEPSEEK_INPUT_PRICE_ENV
            ),
            output_microusd_per_mtok=_non_negative_int(
                resolved_output, DEEPSEEK_OUTPUT_PRICE_ENV
            ),
            cache_hit_microusd_per_mtok=(
                _non_negative_int(resolved_cache, DEEPSEEK_CACHE_HIT_PRICE_ENV)
                if resolved_cache is not None
                else None
            ),
            as_of=resolved_as_of,
            source=price_source
            or _text(source, DEEPSEEK_PRICE_SOURCE_ENV, "host"),
        ),
    )
    if not settings.base_url.startswith("https://"):
        raise ValueError("DeepSeek base_url must use https")
    if not settings.model.strip():
        raise ValueError("DeepSeek model is required")
    return settings

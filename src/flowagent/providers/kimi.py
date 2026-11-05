"""Kimi preset: the OpenAI-compatible engine pinned to Moonshot's contract.

Kimi (Moonshot) is the reference *externally billed* preset of
:mod:`flowagent.providers.openai_compat`: the key comes only from the host
environment, prices are host-reviewed and mandatory, budget is reserved from
a conservative upper bound before dispatch, transport failure is an unknown
outcome, and there is no retry and no mock fallback.

Cache billing is deliberately simple in v1: every input token is charged at
the input price even when the API reports a cache hit. The cached-token count
is kept in receipt metadata for a later, explicit v2 pricing implementation.

Everything below is pinned preset configuration — the transport, receipts,
calibration and pacing live in the shared engine, and this module's tests
lock the observable behaviour so the preset cannot drift.
"""

from __future__ import annotations

import time  # noqa: F401 — patch anchor: tests intercept flowagent.providers.kimi.time.sleep
import urllib.request  # noqa: F401 — patch anchor: tests intercept flowagent.providers.kimi.urllib.request.urlopen
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .openai_compat import (
    CompatSettings,
    IssueReceipt,  # noqa: F401 - re-exported for the preset's callers
    OpenAICompatProvider,
    PriceSnapshot as _EnginePriceSnapshot,
    PromptCalibration,
    RawSink,  # noqa: F401 - re-exported for the preset's callers
    load_compat_settings,
)

__all__ = [
    "KIMI_API_KEY_ENV",
    "KIMI_MODEL_ENV",
    "KIMI_BASE_URL_ENV",
    "KIMI_INPUT_PRICE_ENV",
    "KIMI_OUTPUT_PRICE_ENV",
    "KIMI_PRICE_AS_OF_ENV",
    "KIMI_PRICE_SOURCE_ENV",
    "KIMI_TIMEOUT_ENV",
    "KIMI_MAX_COMPLETION_TOKENS_ENV",
    "KIMI_TEMPERATURE_ENV",
    "KIMI_MIN_CALL_INTERVAL_ENV",
    "KimiPriceSnapshot",
    "KimiCalibration",
    "KimiSettings",
    "load_kimi_settings",
    "KimiProvider",
]

KIMI_API_KEY_ENV = "KIMI_API_KEY"
KIMI_MODEL_ENV = "KIMI_MODEL"
KIMI_BASE_URL_ENV = "KIMI_BASE_URL"
KIMI_INPUT_PRICE_ENV = "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK"
KIMI_OUTPUT_PRICE_ENV = "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK"
KIMI_PRICE_AS_OF_ENV = "KIMI_PRICE_AS_OF"
KIMI_PRICE_SOURCE_ENV = "KIMI_PRICE_SOURCE"
KIMI_TIMEOUT_ENV = "KIMI_TIMEOUT_SECONDS"
KIMI_MAX_COMPLETION_TOKENS_ENV = "KIMI_MAX_COMPLETION_TOKENS"
KIMI_TEMPERATURE_ENV = "KIMI_TEMPERATURE"
KIMI_MIN_CALL_INTERVAL_ENV = "KIMI_MIN_CALL_INTERVAL_SECONDS"

_DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
#: K2.6 is the executable default because this host's key can access it and it
#: completed the real E2E gate. K3.1 Pro remains an explicit KIMI_MODEL override
#: for accounts with access.
_DEFAULT_MODEL = "kimi-k2.6"
_DEFAULT_TIMEOUT = 180.0
#: 512 is enough for a chat-style answer and *not* enough for a reasoning
#: model. Measured on real delegation runs: 512 produced an empty content (the
#: reasoning channel ate the whole budget), so did 4096 on a longer round
#: (9653 characters of reasoning). 8192 is the working headroom; the settings
#: page's ``max_completion_tokens`` overrides it, and the failure message names
#: the knob when it is still too small.
_DEFAULT_MAX_COMPLETION_TOKENS = 8192
_DEFAULT_REASONING_EFFORT = "low"

#: The calibration machinery is engine-owned; the historical preset name stays
#: importable so callers and tests keep a stable surface.
KimiCalibration = PromptCalibration


@dataclass(frozen=True)
class KimiPriceSnapshot(_EnginePriceSnapshot):
    """Host-reviewed Kimi prices; cache hits are v1 receipt metadata only."""

    billing: str = "input-price-v1"


_PRESET = {
    "require_https": True,
    "require_api_key": True,
    "timeout": _DEFAULT_TIMEOUT,
    "max_completion_tokens": _DEFAULT_MAX_COMPLETION_TOKENS,
    "reasoning_effort": _DEFAULT_REASONING_EFFORT,
    "completion_field": "max_completion_tokens",
    "temperature_mode": "omit-unless-pinned",
    "json_mode": True,
    "receipt_detail": "full",
    "billing": "input-price-v1",
    "base_url": _DEFAULT_BASE_URL,
    "model": _DEFAULT_MODEL,
}


@dataclass(frozen=True)
class KimiSettings(CompatSettings):
    """Validated host settings; the API key never appears in ``redacted``.

    A frozen-dataclass subclass of the engine settings with the Kimi preset
    defaults baked in, so ``load_kimi_settings`` keeps resolving CLI
    overrides → ``KIMI_*`` environment → preset defaults exactly as before.
    """


def load_kimi_settings(
    *,
    env: Mapping[str, str] | None = None,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    max_completion_tokens: int | None = None,
    reasoning_effort: str | None = None,
    temperature: float | None = None,
    min_call_interval_seconds: float | None = None,
    input_price: int | None = None,
    output_price: int | None = None,
    price_as_of: str | None = None,
    price_source: str | None = None,
) -> KimiSettings:
    """Resolve CLI overrides, then environment values, then preset defaults.

    Prices and their date never receive a default: a stale guess would turn
    the budget ceiling into a false promise.
    """
    overrides: dict[str, Any] = {
        key: value
        for key, value in {
            "model": model,
            "base_url": base_url,
            "timeout": timeout,
            "max_completion_tokens": max_completion_tokens,
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
            "min_call_interval_seconds": min_call_interval_seconds,
            "input_price": input_price,
            "output_price": output_price,
            "price_as_of": price_as_of,
            "price_source": price_source,
        }.items()
        if value is not None
    }
    settings = load_compat_settings(
        "KIMI",
        env=env,
        label="kimi",
        defaults=_PRESET,
        overrides=overrides,
    )
    return KimiSettings(**{f: getattr(settings, f) for f in settings.__dataclass_fields__})


class KimiProvider(OpenAICompatProvider):
    """Full-proposal provider; Kimi never executes a tool or writes a number."""

    name = "kimi"

    def __init__(
        self,
        *,
        api_key: str,
        price: KimiPriceSnapshot,
        model: str = _DEFAULT_MODEL,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        max_completion_tokens: int = _DEFAULT_MAX_COMPLETION_TOKENS,
        reasoning_effort: str = _DEFAULT_REASONING_EFFORT,
        temperature: float | None = None,
        min_call_interval_seconds: float = 0.0,
        calibration_path: Path | None = None,
        issue_receipt: IssueReceipt | None = None,
        raw_sink: RawSink | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            price=price,
            model=model,
            base_url=base_url,
            label="kimi",
            timeout=timeout,
            max_completion_tokens=max_completion_tokens,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            min_call_interval_seconds=min_call_interval_seconds,
            completion_field="max_completion_tokens",
            temperature_mode="omit-unless-pinned",
            json_mode=True,
            require_https=True,
            receipt_detail="full",
            calibration_path=calibration_path,
            issue_receipt=issue_receipt,
            raw_sink=raw_sink,
        )

    @classmethod
    def from_settings(
        cls,
        settings: KimiSettings,
        *,
        calibration_path: Path | None = None,
        issue_receipt: IssueReceipt | None = None,
        raw_sink: RawSink | None = None,
    ) -> "KimiProvider":
        return cls(
            api_key=settings.api_key,
            price=settings.price,
            model=settings.model,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_completion_tokens=settings.max_completion_tokens,
            reasoning_effort=settings.reasoning_effort or _DEFAULT_REASONING_EFFORT,
            temperature=settings.temperature,
            min_call_interval_seconds=settings.min_call_interval_seconds,
            calibration_path=calibration_path,
            issue_receipt=issue_receipt,
            raw_sink=raw_sink,
        )

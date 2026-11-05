"""The OpenAI-compatible provider engine: one transport, preset front-ends.

Every chat-style API that speaks ``/v1/chat/completions`` with
``Authorization: Bearer`` — Kimi, DeepSeek, LM Studio, Ollama's OpenAI shim,
vLLM, any of a dozen gateways — is the *same wire discipline* under FCode's
rules:

* the key comes only from the host environment and never enters a trace;
* prices are host-reviewed and mandatory (zeros for a free local endpoint —
  the budget machinery stays honest even when money is not involved);
* budget is reserved from a conservative upper bound before dispatch;
* transport failure is an unknown outcome — no retry, no mock fallback;
* calls are paced apart when the host sets an interval.

This module is the general engine. ``kimi`` and ``deepseek`` are preset
front-ends (their env names, defaults and receipts are pinned by their own
tests); brand-new endpoints need no code at all — the host declares a prefix
in ``FCODE_OPENAI_COMPAT_PROVIDERS`` and configures it with that prefix's
environment variables (see docs/openai-compat.md, the four-step checklist).

Knobs the presets and generic entries differ on:

* ``completion_field`` — ``max_completion_tokens`` (modern) vs the older
  ``max_tokens`` some servers still require;
* ``temperature_mode`` — ``omit-unless-pinned`` (Kimi: K-series models reject
  every temperature but the server default), ``pin-zero`` (DeepSeek preset:
  deterministic, always sent), ``send-configured`` (generic: send the
  configured value, default 0);
* ``json_mode`` — request ``response_format: json_object``;
* ``billing`` — ``input-price-v1`` (every input token at the input price,
  cache reported as metadata only) vs ``cache-split`` (cache hits at a
  cheaper price when the server reports them);
* ``receipt_detail`` — ``full`` (prices, effort, calibration, cached tokens,
  raw content; the externally-billed preset contract) vs ``compact``;
* ``require_https`` — presets with cloud defaults refuse plain http; a
  generic entry may point at ``http://127.0.0.1`` for a local server.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..core.contracts import PLAN_VERSION, AgentError
from ..pacing import pace_key, reserve_slot
from .base import Proposal
from .prompt import (
    build_proposal_plan,
    parse_proposal_text,
    prompt_profile_for,
    render_prompt,
)

__all__ = [
    "PriceSnapshot",
    "PromptCalibration",
    "GENERIC_COMPAT_DEFAULTS",
    "CompatSettings",
    "load_compat_settings",
    "CompatClient",
    "OpenAICompatProvider",
    "declared_compat_prefixes",
]

RawSink = Callable[[dict[str, Any]], None]
IssueReceipt = Callable[..., str]


def _micro(tokens: int, microusd_per_mtok: int) -> int:
    return (tokens * microusd_per_mtok + 500_000) // 1_000_000


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


#: Host-declared generic prefixes, comma-separated in this environment
#: variable. Each prefix names its own ``{PREFIX}_*`` configuration set.
COMPAT_PROVIDERS_ENV = "FCODE_OPENAI_COMPAT_PROVIDERS"

#: Defaults for host-declared generic entries. ``require_https`` is False so
#: a local server (LM Studio, Ollama's OpenAI shim on 127.0.0.1) can be
#: declared; a cloud endpoint simply configures an https base URL. The
#: declaring host is the trust boundary — the same authority that edits
#: mcp.json.
GENERIC_COMPAT_DEFAULTS = {
    "require_https": False,
    "require_api_key": False,
    "temperature_mode": "send-configured",
    "completion_field": "max_completion_tokens",
    "json_mode": True,
    "receipt_detail": "full",
}


def declared_compat_prefixes(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The host-declared generic prefixes (uppercase, deduplicated, in order)."""
    source = os.environ if env is None else env
    raw = source.get(COMPAT_PROVIDERS_ENV, "")
    seen: list[str] = []
    for item in raw.split(","):
        prefix = item.strip().upper()
        if prefix and prefix.isalnum() and prefix not in seen:
            seen.append(prefix)
    return tuple(seen)


# ------------------------------------------------------------------- pricing


@dataclass(frozen=True)
class PriceSnapshot:
    """Host-reviewed prices, µUSD per million tokens.

    ``billing`` picks the v1 contract: ``input-price-v1`` charges every input
    token at the input price (cache hits are receipt metadata only — never
    cheaper than the honest worst case); ``cache-split`` charges reported
    cache hits at ``cache_hit_microusd_per_mtok``.
    """

    input_microusd_per_mtok: int
    output_microusd_per_mtok: int
    as_of: str
    source: str = "host"
    cache_hit_microusd_per_mtok: int | None = None
    billing: str = "cache-split"

    def __post_init__(self) -> None:
        for name in ("input_microusd_per_mtok", "output_microusd_per_mtok"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.cache_hit_microusd_per_mtok is not None:
            if (
                not isinstance(self.cache_hit_microusd_per_mtok, int)
                or self.cache_hit_microusd_per_mtok < 0
            ):
                raise ValueError("cache_hit_microusd_per_mtok must be a non-negative integer")
        if not isinstance(self.as_of, str) or not self.as_of.strip():
            raise ValueError("as_of is required")
        if self.billing not in ("cache-split", "input-price-v1"):
            raise ValueError("billing must be cache-split or input-price-v1")

    def cost(self, usage: dict[str, Any]) -> int:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        if self.billing == "input-price-v1":
            return _micro(prompt, self.input_microusd_per_mtok) + _micro(
                completion, self.output_microusd_per_mtok
            )
        hit = max(0, min(int(usage.get("prompt_cache_hit_tokens") or 0), prompt))
        miss = max(0, prompt - hit)
        cache_price = (
            self.cache_hit_microusd_per_mtok
            if self.cache_hit_microusd_per_mtok is not None
            else self.input_microusd_per_mtok
        )
        return (
            _micro(hit, cache_price)
            + _micro(miss, self.input_microusd_per_mtok)
            + _micro(completion, self.output_microusd_per_mtok)
        )


# --------------------------------------------------------------- calibration


@dataclass
class PromptCalibration:
    """Prompt-token estimate and its one-time correction from real usage.

    The initial char-based estimate is intentionally conservative for
    English, but Chinese text can tokenize above one token per three
    characters. If the first real response reports more prompt tokens than
    reserved, the measured chars-to-token ratio replaces the guess for all
    later rounds and scenarios.
    """

    path: Path | None = None
    measured_ratio: float | None = None
    reports: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None) -> "PromptCalibration":
        if path is None or not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                path=path,
                measured_ratio=float(data.get("measured_ratio"))
                if data.get("measured_ratio") is not None
                else None,
                reports=list(data.get("reports") or []),
            )
        except (OSError, ValueError, TypeError):
            # A damaged calibration file must not become a reason to stop
            # calling the provider. Fall back to the conservative formula.
            return cls(path=path)

    def estimate(self, system: str, user: str) -> int:
        chars = len(system) + len(user)
        if self.measured_ratio is None:
            return max(1, _ceil_div(chars, 3)) + 64
        measured = math.ceil(chars * self.measured_ratio)
        return max(1, _ceil_div(chars, 2), measured)

    @property
    def formula(self) -> str:
        if self.measured_ratio is None:
            return "ceil(chars/3)+64"
        ratio = format(self.measured_ratio, ".6f").rstrip("0").rstrip(".")
        return f"max(ceil(chars/2), ceil(chars*{ratio}))"

    def record(self, *, chars: int, reserved: int, actual: int) -> dict[str, Any]:
        report = {
            "chars": int(chars),
            "reserved_prompt_tokens": int(reserved),
            "usage_prompt_tokens": int(actual),
            # Ratio in integer basis points: this report travels inside the
            # receipt metadata into committed event bodies, and canonical_json
            # rejects floats. The float measured_ratio stays in memory only.
            "ratio_actual_to_reserve_bp": int(round(actual / reserved * 10000))
            if reserved
            else None,
            "old_formula": self.formula,
            "adjusted": False,
        }
        if self.measured_ratio is None and actual > reserved and chars > 0:
            # One correction, based on the first real response. Later rounds
            # may legitimately differ in content mix; repeatedly refitting the
            # ratio would turn a budget guard into an experiment loop.
            self.measured_ratio = actual / chars
            report["adjusted"] = True
            report["new_formula"] = self.formula
        self.reports.append(report)
        self.save()
        return report

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "measured_ratio": self.measured_ratio,
            "formula": self.formula,
            "reports": self.reports,
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


# ------------------------------------------------------------------ settings


@dataclass(frozen=True)
class CompatSettings:
    """Validated settings for one OpenAI-compatible endpoint."""

    label: str
    api_key_env: str
    api_key: str
    base_url: str
    model: str
    price: PriceSnapshot
    timeout: float = 120.0
    max_completion_tokens: int = 512
    reasoning_effort: str | None = None
    temperature: float | None = None
    min_call_interval_seconds: float = 0.0
    completion_field: str = "max_completion_tokens"
    temperature_mode: str = "send-configured"
    json_mode: bool = True
    require_https: bool = True
    receipt_detail: str = "full"
    supports_effort: bool = False

    def redacted(self) -> dict[str, Any]:
        return {
            "provider": self.label,
            "apiKey": f"env:{self.api_key_env}" if self.api_key else "",
            "baseURL": self.base_url,
            "model": self.model,
            "timeoutSeconds": self.timeout,
            "maxCompletionTokens": self.max_completion_tokens,
            "reasoningEffort": self.reasoning_effort,
            "temperature": self.temperature,
            "minCallIntervalSeconds": self.min_call_interval_seconds,
            "price": {
                "inputMicrousdPerMtok": self.price.input_microusd_per_mtok,
                "outputMicrousdPerMtok": self.price.output_microusd_per_mtok,
                "asOf": self.price.as_of,
                "source": self.price.source,
            },
        }


def _required(env: Mapping[str, str], name: str) -> str:
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


def _optional_float(env: Mapping[str, str], name: str) -> float | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error


def load_compat_settings(
    prefix: str,
    *,
    env: Mapping[str, str] | None = None,
    label: str | None = None,
    defaults: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> CompatSettings:
    """Resolve one endpoint's settings: overrides → environment → defaults.

    Environment names are derived from the prefix:
    ``{PREFIX}_API_KEY`` (optional for keyless local servers — omitted from
    the request when empty), ``{PREFIX}_BASE_URL``, ``{PREFIX}_MODEL``,
    ``{PREFIX}_INPUT/OUTPUT_PRICE_MICROUSD_PER_MTOK`` (mandatory; zeros are
    honest for a free local endpoint), ``{PREFIX}_PRICE_AS_OF`` (mandatory),
    ``{PREFIX}_PRICE_SOURCE``, ``{PREFIX}_TIMEOUT_SECONDS``,
    ``{PREFIX}_MAX_COMPLETION_TOKENS``, ``{PREFIX}_TEMPERATURE``,
    ``{PREFIX}_REASONING_EFFORT``, ``{PREFIX}_MIN_CALL_INTERVAL_SECONDS``,
    ``{PREFIX}_SUPPORTS_EFFORT``.

    Prices and their date never receive a default: a stale guess would turn
    the budget ceiling into a false promise.
    """
    source = os.environ if env is None else env
    p = prefix.upper()
    preset = dict(defaults or {})
    override = dict(overrides or {})

    def resolve(key: str, env_name: str, fallback: Any, parser=lambda v: v) -> Any:
        if key in override and override[key] is not None:
            return override[key]
        raw = source.get(env_name, "")
        if raw.strip():
            try:
                return parser(raw.strip())
            except (TypeError, ValueError) as error:
                raise ValueError(f"{env_name} must be a valid {parser.__name__}") from error
        if key in preset and preset[key] is not None:
            return preset[key]
        return fallback

    label = label or p.lower()
    api_key = str(resolve("api_key", f"{p}_API_KEY", "", str) or "").strip()

    base_url = str(resolve("base_url", f"{p}_BASE_URL", "", str)).rstrip("/")
    if not base_url:
        raise ValueError(f"{p}_BASE_URL is required")
    require_https = bool(preset.get("require_https", True))
    if require_https and not base_url.startswith("https://"):
        raise ValueError(f"{p}_BASE_URL must use https")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"{p}_BASE_URL must use http(s)")

    model = str(resolve("model", f"{p}_MODEL", "", str))
    if not model:
        raise ValueError(f"{p}_MODEL is required")

    require_key = bool(preset.get("require_api_key", False))
    if require_key and not api_key:
        raise ValueError(f"{p}_API_KEY is required")

    input_price = resolve("input_price", f"{p}_INPUT_PRICE_MICROUSD_PER_MTOK", None, int)
    output_price = resolve("output_price", f"{p}_OUTPUT_PRICE_MICROUSD_PER_MTOK", None, int)
    if input_price is None or output_price is None:
        raise ValueError(
            f"{p}_INPUT_PRICE_MICROUSD_PER_MTOK and {p}_OUTPUT_PRICE_MICROUSD_PER_MTOK are required"
        )
    price_as_of = str(resolve("price_as_of", f"{p}_PRICE_AS_OF", "", str))
    if not price_as_of:
        raise ValueError(f"{p}_PRICE_AS_OF is required")

    timeout = resolve("timeout", f"{p}_TIMEOUT_SECONDS", None, float)
    if timeout is None:
        timeout = float(preset.get("timeout", 120.0))
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    max_completion = resolve("max_completion_tokens", f"{p}_MAX_COMPLETION_TOKENS", None, int)
    if max_completion is None:
        max_completion = int(preset.get("max_completion_tokens", 512))
    if max_completion <= 0:
        raise ValueError("max_completion_tokens must be positive")

    temperature = resolve("temperature", f"{p}_TEMPERATURE", None, float)
    effort_raw = str(resolve("reasoning_effort", f"{p}_REASONING_EFFORT", "", str) or "")
    preset_effort = preset.get("reasoning_effort")
    reasoning_effort = effort_raw or (preset_effort if preset_effort is not None else "")
    if reasoning_effort and reasoning_effort not in ("low", "medium", "high"):
        raise ValueError(f"{p}_REASONING_EFFORT must be low, medium or high")

    interval = resolve("min_call_interval_seconds", f"{p}_MIN_CALL_INTERVAL_SECONDS", None, float)
    if interval is None:
        interval = 0.0
    if interval < 0:
        raise ValueError(f"{p}_MIN_CALL_INTERVAL_SECONDS must be non-negative")

    supports_effort = str(
        resolve("supports_effort", f"{p}_SUPPORTS_EFFORT", "1" if reasoning_effort else "0", str)
    ).strip().lower() in ("1", "true", "yes", "on")

    return CompatSettings(
        label=label,
        api_key_env=f"{p}_API_KEY",
        api_key=api_key,
        base_url=base_url,
        model=model,
        price=PriceSnapshot(
            input_microusd_per_mtok=input_price,
            output_microusd_per_mtok=output_price,
            as_of=price_as_of,
            source=str(resolve("price_source", f"{p}_PRICE_SOURCE", "host", str)),
            billing=str(preset.get("billing", "cache-split")),
        ),
        timeout=float(timeout),
        max_completion_tokens=int(max_completion),
        reasoning_effort=reasoning_effort or None,
        temperature=temperature,
        min_call_interval_seconds=float(interval),
        completion_field=str(preset.get("completion_field", "max_completion_tokens")),
        temperature_mode=str(preset.get("temperature_mode", "send-configured")),
        json_mode=bool(preset.get("json_mode", True)),
        require_https=require_https,
        receipt_detail=str(preset.get("receipt_detail", "full")),
        supports_effort=supports_effort,
    )


# ------------------------------------------------------------------- client


class CompatClient:
    """One HTTP transport for one endpoint; nothing here retries or falls back."""

    def __init__(
        self,
        *,
        label: str,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float,
        max_completion_tokens: int,
        completion_field: str = "max_completion_tokens",
        temperature_mode: str = "send-configured",
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        json_mode: bool = True,
        require_https: bool = True,
    ) -> None:
        if not isinstance(api_key, str):
            raise ValueError(f"{label} API key is missing")
        if require_https and not base_url.startswith("https://"):
            raise ValueError(f"{label} base_url must use https")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"{label} base_url must use http(s)")
        if max_completion_tokens <= 0:
            raise ValueError("max_completion_tokens must be positive")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be within [0, 2] when provided")
        self.label = label
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_completion_tokens = max_completion_tokens
        self.completion_field = completion_field
        self.temperature_mode = temperature_mode
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort or ""
        self.json_mode = json_mode

    def chat(self, system: str, user: str) -> tuple[str, dict[str, Any], str]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            self.completion_field: self.max_completion_tokens,
            "stream": False,
        }
        if self.temperature_mode == "pin-zero":
            body["temperature"] = 0
        elif self.temperature_mode == "omit-unless-pinned":
            # K-series chat models reject every temperature except the server
            # default; omit the field unless the host pins one explicitly.
            if self.temperature is not None:
                body["temperature"] = self.temperature
        else:  # send-configured: deterministic by default
            body["temperature"] = 0 if self.temperature is None else self.temperature
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "fcode-runtime/1",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:200]
            code = "MODEL_AUTH_FAILED" if error.code in (401, 403) else "MODEL_CALL_FAILED"
            raise AgentError(code, f"{self.label} HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AgentError("MODEL_CALL_FAILED", f"{self.label} transport: {error}") from error
        except ValueError as error:
            raise AgentError("MODEL_CALL_FAILED", f"{self.label} returned non-JSON") from error

        choices = payload.get("choices") or []
        if not choices:
            raise AgentError("MODEL_CALL_FAILED", f"{self.label} returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            # A reasoning model whose completion budget ran out during the
            # reasoning channel returns reasoning_content and no content. That
            # is not "no answer": it is a budget that was too small, and the
            # operator can fix exactly one number.
            reasoning = str(message.get("reasoning_content") or "")
            if reasoning.strip():
                raise AgentError(
                    "MODEL_CALL_FAILED",
                    f"{self.label} 的补全预算被推理占满"
                    f"（reasoning {len(reasoning)} 字，content 为空）；"
                    f"把 {self.label.upper()}_MAX_COMPLETION_TOKENS 调大后重试",
                )
            raise AgentError("MODEL_CALL_FAILED", f"{self.label} returned empty content")
        return content, dict(payload.get("usage") or {}), str(payload.get("id") or "")


# ------------------------------------------------------------------ provider


def _cached_tokens(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or {}
    try:
        return max(0, int(details.get("cached_tokens") or 0))
    except (TypeError, ValueError):
        return 0


def _usage_tokens(usage: dict[str, Any]) -> tuple[int, int, int]:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    return prompt, completion, total


class OpenAICompatProvider:
    """Full-proposal provider over any OpenAI-compatible chat endpoint.

    The model proposes; the host still gates, executes and verifies. Presets
    (kimi, deepseek) subclass this with pinned knobs so their receipts and
    request shapes stay byte-compatible with their locked tests.
    """

    name = "openai-compat"

    def __init__(
        self,
        *,
        api_key: str,
        price: PriceSnapshot,
        model: str,
        base_url: str,
        label: str | None = None,
        timeout: float = 120.0,
        max_completion_tokens: int = 512,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
        min_call_interval_seconds: float = 0.0,
        completion_field: str = "max_completion_tokens",
        temperature_mode: str = "send-configured",
        json_mode: bool = True,
        require_https: bool = True,
        receipt_detail: str = "full",
        max_prompt_tokens: int | None = None,
        calibration_path: Path | None = None,
        issue_receipt: IssueReceipt | None = None,
        raw_sink: RawSink | None = None,
    ) -> None:
        self.label = label or self.name
        self.name = self.label  # the provider identity receipts carry
        self.base_url = base_url
        self.client = CompatClient(
            label=self.label,
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            max_completion_tokens=max_completion_tokens,
            completion_field=completion_field,
            temperature_mode=temperature_mode,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            json_mode=json_mode,
            require_https=require_https,
        )
        self.model = model
        self.price = price
        self.max_completion_tokens = max_completion_tokens
        self.reasoning_effort = reasoning_effort
        self.temperature = temperature
        if min_call_interval_seconds < 0:
            raise ValueError("min_call_interval_seconds must be non-negative")
        self.min_call_interval_seconds = min_call_interval_seconds
        self.calibration = PromptCalibration.load(calibration_path)
        self.issue_receipt = issue_receipt
        self.raw_sink = raw_sink
        self.calibration_reports: list[dict[str, Any]] = []
        self.receipt_detail = receipt_detail
        # Fixed pre-dispatch reservation (the deepseek preset contract):
        # reserved once from the configured prompt ceiling, not per prompt.
        self.max_prompt_tokens = max_prompt_tokens
        if max_prompt_tokens is not None:
            self.reserve_tokens = max_prompt_tokens + max_completion_tokens
            self.reserve_cost_microusd = _micro(
                max_prompt_tokens, price.input_microusd_per_mtok
            ) + _micro(max_completion_tokens, price.output_microusd_per_mtok)

    @classmethod
    def from_settings(
        cls,
        settings: CompatSettings,
        *,
        calibration_path: Path | None = None,
        issue_receipt: IssueReceipt | None = None,
        raw_sink: RawSink | None = None,
    ) -> "OpenAICompatProvider":
        return cls(
            api_key=settings.api_key,
            price=settings.price,
            model=settings.model,
            base_url=settings.base_url,
            label=settings.label,
            timeout=settings.timeout,
            max_completion_tokens=settings.max_completion_tokens,
            reasoning_effort=settings.reasoning_effort,
            temperature=settings.temperature,
            min_call_interval_seconds=settings.min_call_interval_seconds,
            completion_field=settings.completion_field,
            temperature_mode=settings.temperature_mode,
            json_mode=settings.json_mode,
            require_https=settings.require_https,
            receipt_detail=settings.receipt_detail,
            calibration_path=calibration_path,
            issue_receipt=issue_receipt,
            raw_sink=raw_sink,
        )

    @property
    def price_snapshot(self) -> PriceSnapshot:
        return self.price

    def reserve(self, context: dict[str, Any]) -> tuple[int, int]:
        system, user = render_prompt(context)
        prompt_tokens = self.calibration.estimate(system, user)
        return (
            prompt_tokens + self.max_completion_tokens,
            _micro(prompt_tokens, self.price.input_microusd_per_mtok)
            + _micro(self.max_completion_tokens, self.price.output_microusd_per_mtok),
        )

    def _pace(self) -> None:
        """Reserve a dispatch slot on the endpoint's shared rate limiter.

        This is transport rate shaping before the send, not a retry. The slot
        table is process-wide, so a chat answer that just used the same
        endpoint also counts; the first agent dispatch keeps its conservative
        full-interval wait when nobody has called yet.
        """
        wait = reserve_slot(
            pace_key(self.label, self.base_url),
            self.min_call_interval_seconds,
            first_call_waits=True,
        )
        if wait > 0:
            time.sleep(wait)

    def propose(self, context: dict[str, Any]) -> Proposal:
        profile = prompt_profile_for(context)
        system, user = render_prompt(context)
        chars = len(system) + len(user)
        reserved_prompt_tokens = self.calibration.estimate(system, user)
        self._pace()
        started = time.monotonic()
        content, usage, request_id = self.client.chat(system, user)
        latency_ms = int((time.monotonic() - started) * 1000)

        if self.raw_sink is not None:
            self.raw_sink(
                {
                    "round": context.get("round"),
                    "provider": self.label,
                    "model": self.model,
                    "request_id": request_id,
                    "content": content,
                    "usage": dict(usage),
                }
            )

        try:
            data = parse_proposal_text(content)
        except ValueError as error:
            raise AgentError(
                "PROPOSAL_MALFORMED",
                f"{error}: {content[:120]!r}",
                details={"raw_content": content, "request_id": request_id},
            ) from error

        prompt_tokens, _, total_tokens = _usage_tokens(usage)
        plan = build_proposal_plan(data)

        metadata: dict[str, Any] = {
            "prompt_version": profile.version,
            "billing": "external",
            "provider_request_id": request_id,
            "price_as_of": self.price.as_of,
            "price_source": self.price.source,
        }
        if self.receipt_detail == "full":
            metadata.update({
                "input_microusd_per_mtok": self.price.input_microusd_per_mtok,
                "output_microusd_per_mtok": self.price.output_microusd_per_mtok,
                "reasoning_effort": self.reasoning_effort,
                "temperature": self.temperature,
                "reserved_prompt_tokens": reserved_prompt_tokens,
                "usage_prompt_tokens": prompt_tokens,
                "cached_tokens": _cached_tokens(usage),
                "cache_billing": self.price.billing,
                "raw_content": content,
            })
            calibration_report = self.calibration.record(
                chars=chars,
                reserved=reserved_prompt_tokens,
                actual=prompt_tokens,
            )
            self.calibration_reports.append(calibration_report)
            metadata["calibration"] = calibration_report
        if not usage:
            metadata["usage_missing"] = True

        receipt_ref = ""
        if self.issue_receipt is not None:
            intent = context.get("intent")
            if isinstance(intent, dict):
                receipt_ref = self.issue_receipt(
                    intent=intent,
                    plan=plan,
                    usage={
                        "provider": self.label,
                        "model": self.model,
                        "tokens": total_tokens,
                        "cost_microusd": self.price.cost(usage),
                        "request_id": request_id,
                    },
                )
                metadata["hmac_receipt_ref"] = receipt_ref

        return Proposal(
            plan=plan,
            provider=self.label,
            model=self.model,
            tokens=total_tokens,
            cost_microusd=self.price.cost(usage),
            latency_ms=latency_ms,
            todo_id=data.get("todo_id"),
            metadata=metadata,
        )

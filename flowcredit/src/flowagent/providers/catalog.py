"""Server-side provider enumeration: the menu lists what the host configured.

The console's model selector used to hardcode three entries. Now it asks the
host what exists: the offline mock, the local endpoint probe, the built-in
presets (kimi, deepseek) and every prefix the host declared in
``FCODE_OPENAI_COMPAT_PROVIDERS``. An entry appears the moment its
configuration is complete, with honest availability — an incomplete entry
appears too, marked unavailable with the exact environment variable that is
missing. Nothing here can invent a provider that was not configured, and
nothing hides one that failed to load — except a provider the host explicitly
disabled in ``FCODE_DISABLED_PROVIDERS``.
"""

from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from ..chat_config import default_chat_config_path, is_custom_provider_key, load_chat_config
from .deepseek_config import load_deepseek_settings
from .kimi import load_kimi_settings
from .openai_compat import (
    COMPAT_PROVIDERS_ENV,
    CompatSettings,
    GENERIC_COMPAT_DEFAULTS,
    PriceSnapshot,
    declared_compat_prefixes,
    load_compat_settings,
)

__all__ = [
    "DISABLED_PROVIDERS_ENV",
    "PROVIDER_PRESETS",
    "compat_settings_for",
    "disabled_providers",
    "merged_env",
    "provider_catalog",
    "resolved_provider_env",
    "settings_as_env",
]

#: Comma-separated provider keys the host wants removed from the menu and from
#: run creation. A deployment that standardises on one vendor uses this instead
#: of deleting presets: the preset code and its tests stay, the operator chooses
#: what is offered. Disabled is stronger than unavailable — an unavailable
#: provider is listed with a reason, a disabled one does not exist.
DISABLED_PROVIDERS_ENV = "FCODE_DISABLED_PROVIDERS"


def disabled_providers(env: Mapping[str, str] | None = None) -> set[str]:
    """Provider keys the host explicitly disabled (lowercase)."""
    source = os.environ if env is None else env
    raw = source.get(DISABLED_PROVIDERS_ENV, "")
    return {
        item.strip().lower()
        for item in str(raw).split(",")
        if item.strip()
    }

_LOCAL_PROBE_CACHE: dict[str, tuple[float, bool]] = {}


def probe_local_endpoint(endpoint: str) -> bool:
    """Shared local availability probe, cached for 60 seconds."""
    now = time.monotonic()
    cached = _LOCAL_PROBE_CACHE.get(endpoint)
    if cached is not None and now - cached[0] < 60.0:
        return cached[1]
    root = endpoint.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    ok = False
    for path in ("/api/tags", "/v1/models"):
        try:
            request = urllib.request.Request(root + path, method="GET")
            with urllib.request.urlopen(request, timeout=0.5) as response:
                ok = response.status == 200
                break
        except Exception:  # noqa: BLE001 — an unreachable endpoint is unavailable
            ok = False
    _LOCAL_PROBE_CACHE[endpoint] = (now, ok)
    return ok


#: Settings-page field names → the preset environment names the engine reads.
#: One settings page configures both surfaces: a key typed in the app must be
#: able to start a *delegation*, not just a conversation.
_SETTINGS_ENV_FIELDS: dict[str, dict[str, str]] = {
    "kimi": {
        "api_key": "KIMI_API_KEY",
        "model": "KIMI_MODEL",
        "input_price_microusd_per_mtok": "KIMI_INPUT_PRICE_MICROUSD_PER_MTOK",
        "output_price_microusd_per_mtok": "KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK",
        "price_as_of": "KIMI_PRICE_AS_OF",
        "price_source": "KIMI_PRICE_SOURCE",
        "min_interval_seconds": "KIMI_MIN_CALL_INTERVAL_SECONDS",
        "max_completion_tokens": "KIMI_MAX_COMPLETION_TOKENS",
    },
    "deepseek": {
        "api_key": "DEEPSEEK_API_KEY",
        "model": "DEEPSEEK_MODEL",
        "input_price_microusd_per_mtok": "DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK",
        "output_price_microusd_per_mtok": "DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK",
        "price_as_of": "DEEPSEEK_PRICE_AS_OF",
        "price_source": "DEEPSEEK_PRICE_SOURCE",
    },
}


def settings_as_env(providers: Mapping[str, Any] | None) -> dict[str, str]:
    """Translate the single settings document into provider engine values.

    Built-in cloud endpoints still cannot be redirected from settings. Custom
    endpoints are an explicit operator trust decision and carry their own
    prefix, base URL and optional key; local uses the existing FCODE_LOCAL names.
    """
    out: dict[str, str] = {}
    declared: list[str] = []
    for raw_key, values in (providers or {}).items():
        key = str(raw_key).strip().lower()
        if not isinstance(values, Mapping):
            continue
        fields = _SETTINGS_ENV_FIELDS.get(key)
        if key == "local":
            fields = {
                "api_key": "FCODE_LOCAL_API_KEY",
                "model": "FCODE_LOCAL_MODEL",
                "base_url": "FCODE_LOCAL_ENDPOINT",
                "max_completion_tokens": "FCODE_LOCAL_MAX_COMPLETION_TOKENS",
            }
        elif fields is None and is_custom_provider_key(key):
            prefix = key.upper()
            fields = {
                "label": f"{prefix}_LABEL",
                "api_key": f"{prefix}_API_KEY",
                "model": f"{prefix}_MODEL",
                "base_url": f"{prefix}_BASE_URL",
                "input_price_microusd_per_mtok": f"{prefix}_INPUT_PRICE_MICROUSD_PER_MTOK",
                "output_price_microusd_per_mtok": f"{prefix}_OUTPUT_PRICE_MICROUSD_PER_MTOK",
                "price_as_of": f"{prefix}_PRICE_AS_OF",
                "price_source": f"{prefix}_PRICE_SOURCE",
                "min_interval_seconds": f"{prefix}_MIN_CALL_INTERVAL_SECONDS",
                "max_completion_tokens": f"{prefix}_MAX_COMPLETION_TOKENS",
                "supports_effort": f"{prefix}_SUPPORTS_EFFORT",
            }
            declared.append(prefix)
        if not fields:
            continue
        for field, env_name in fields.items():
            value = values.get(field)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                out[env_name] = text
    if declared:
        out[COMPAT_PROVIDERS_ENV] = ",".join(dict.fromkeys(declared))
    return out


def merged_env(
    settings_env: Mapping[str, str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve environment → user settings → preset defaults.

    Explicit host environment values are authoritative. Blank environment
    values fall through to the user config instead of erasing it.
    """
    source = os.environ if env is None else env
    base = {
        name: str(value).strip()
        for name, value in (settings_env or {}).items()
        if str(value).strip()
    }
    for name, value in source.items():
        text = str(value).strip()
        if text:
            base[name] = text
    # Endpoint declarations are additive: an environment-declared gateway must
    # not hide a different gateway saved in the settings page.
    declarations = [
        item.strip().upper()
        for owner in ((settings_env or {}).get(COMPAT_PROVIDERS_ENV, ""), source.get(COMPAT_PROVIDERS_ENV, ""))
        for item in str(owner).split(",")
        if item.strip()
    ]
    if declarations:
        base[COMPAT_PROVIDERS_ENV] = ",".join(dict.fromkeys(declarations))
    return base


def resolved_provider_env(
    *,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """CLI's default resolution path: the same user config as the console.

    Tests and embedders that pass an explicit ``env`` get an isolated view and
    do not implicitly read the developer's real credentials.
    """
    source = os.environ if env is None else env
    path = config_path or default_chat_config_path()
    config = load_chat_config(path)
    return merged_env(settings_as_env(config.get("providers")), env=source)


def _entry_from_settings(key: str, label: str, settings: CompatSettings) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "model": settings.model,
        "supportsEffort": settings.supports_effort,
        "available": True,
        "reason": "",
        "price": {
            "inputMicrousdPerMtok": settings.price.input_microusd_per_mtok,
            "outputMicrousdPerMtok": settings.price.output_microusd_per_mtok,
        },
        "priceAsOf": settings.price.as_of,
        "priceSource": settings.price.source,
        "maxCompletionTokens": settings.max_completion_tokens,
    }


def _incomplete_reason(key: str, error: Exception, from_settings: set[str]) -> str:
    """Say which surface is incomplete, because that is where it gets fixed.

    The engine names the exact missing variable. When the settings page already
    contributed some of this provider's fields, the operator should look there
    — not in a shell they may never have opened.
    """
    names = set(_SETTINGS_ENV_FIELDS.get(key, {}).values())
    if names & from_settings:
        return f"设置页配置不完整：{error}"
    return f"Host 配置不完整：{error}"


def _unavailable(key: str, label: str, model: str, reason: str, supports_effort: bool = False) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "model": model,
        "supportsEffort": supports_effort,
        "available": False,
        "reason": reason,
        "price": None,
        "maxCompletionTokens": None,
    }


#: The built-in presets every checkout knows about. Their behaviour is pinned
#: by their own tests; the catalog only reports what the environment says.
PROVIDER_PRESETS = ("kimi", "deepseek")


def provider_catalog(
    *,
    env: Mapping[str, str] | None = None,
    settings_env: Mapping[str, str] | None = None,
    local_endpoint: str | None = None,
    local_probe=None,
) -> list[dict[str, Any]]:
    """Enumerate every provider the host has (or tried to) configure.

    ``settings_env`` carries the values the operator saved in the app (see
    ``settings_as_env``); explicit environment values win, and otherwise a key
    typed in the settings page is enough to run a delegation. ``local_probe`` is an optional
    callable the console supplies for the Ollama-style endpoint health check;
    offline callers pass ``None`` and the local entry reports its reason
    without touching the network.
    """
    source = merged_env(settings_env, env=env)
    from_settings = set((settings_env or {}).keys())
    entries: list[dict[str, Any]] = [
        {
            "key": "mock",
            "label": "offline-mock",
            "model": "offline-deterministic-mock-v1",
            "supportsEffort": False,
            "available": True,
            "reason": "",
            "price": None,
            "maxCompletionTokens": None,
        }
    ]

    if local_endpoint:
        probed = bool(local_probe()) if local_probe is not None else False
        entries.append(_unavailable(
            "local", "本地 Qwen", "fcode-qwen3-4b",
            "" if probed else "本地推理端点探活失败",
        ) | {"available": probed, "supportsEffort": False})
    else:
        entries.append(_unavailable("local", "本地 Qwen", "fcode-qwen3-4b", "未配置本地推理端点"))

    try:
        settings = load_kimi_settings(env=source)
        entries.append(_entry_from_settings("kimi", "Kimi 云端", settings) | {"supportsEffort": True})
    except ValueError as error:
        entries.append(_unavailable(
            "kimi", "Kimi 云端", source.get("KIMI_MODEL", "").strip() or "kimi-k2.6",
            _incomplete_reason("kimi", error, from_settings), supports_effort=True,
        ))

    try:
        settings = compat_settings_for("deepseek", env=source)
        if settings is not None:
            entries.append(_entry_from_settings(
                "deepseek", "DeepSeek 云端", settings))
    except ValueError as error:
        entries.append(_unavailable(
            "deepseek", "DeepSeek 云端", source.get("DEEPSEEK_MODEL", "").strip() or "deepseek-chat",
            _incomplete_reason("deepseek", error, from_settings),
        ))

    for prefix in declared_compat_prefixes(source):
        key = prefix.lower()
        label = source.get(f"{prefix}_LABEL", "").strip() or key
        try:
            settings = load_compat_settings(
                prefix, env=source, label=key, defaults=GENERIC_COMPAT_DEFAULTS
            )
        except ValueError as error:
            entries.append(_unavailable(
                key, label, source.get(f"{prefix}_MODEL", "").strip() or "unset",
                f"{prefix}_* 未配置完整：{error}",
            ))
            continue
        entries.append(_entry_from_settings(key, label, settings))

    disabled = disabled_providers(source)
    return [entry for entry in entries if entry["key"] not in disabled]


def compat_settings_for(key: str, *, env: Mapping[str, str] | None = None) -> CompatSettings | None:
    """Load the settings a configured provider key names, or ``None``.

    The console and the factory use this to construct exactly the provider
    the operator selected — never a fallback. A host-disabled key resolves to
    ``None`` here too, so a stale run spec cannot resurrect it after the menu
    stopped offering it.
    """
    source = os.environ if env is None else env
    if str(key).strip().lower() in disabled_providers(source):
        return None
    if key == "local":
        endpoint = str(source.get("FCODE_LOCAL_ENDPOINT", "")).strip().rstrip("/")
        if not endpoint:
            return None
        return CompatSettings(
            label="local",
            api_key_env="FCODE_LOCAL_API_KEY",
            api_key=str(source.get("FCODE_LOCAL_API_KEY", "")).strip(),
            base_url=endpoint,
            model=str(source.get("FCODE_LOCAL_MODEL", "")).strip() or "fcode-qwen3-4b",
            price=PriceSnapshot(0, 0, "not-applicable", "local-not-billed"),
            timeout=float(source.get("FCODE_LOCAL_TIMEOUT_SECONDS", "120")),
            max_completion_tokens=int(source.get("FCODE_LOCAL_MAX_COMPLETION_TOKENS", "2048")),
            completion_field="max_tokens",
            require_https=False,
        )
    if key == "kimi":
        return load_kimi_settings(env=source)
    if key == "deepseek":
        settings = load_deepseek_settings(env=source)
        return CompatSettings(
            label="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            api_key=settings.api_key,
            base_url=settings.base_url,
            model=settings.model,
            price=settings.price,
            timeout=settings.timeout,
            max_completion_tokens=settings.max_tokens,
            temperature_mode="pin-zero",
            completion_field="max_tokens",
            receipt_detail="compact",
        )
    for prefix in declared_compat_prefixes(source):
        if prefix.lower() == key:
            return load_compat_settings(
                prefix, env=source, label=source.get(f"{prefix}_LABEL", "").strip() or key,
                defaults=GENERIC_COMPAT_DEFAULTS,
            )
    return None

"""Chat provider configuration: environment > user settings > defaults.

This module owns the resolution rules and the local settings file. It never
imports the service or the transport, so it is safe to use from the console,
the service and tests without a cycle.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.contracts import AgentError

CHAT_CONFIG_VERSION = "fcode.chat-config/v1"


def default_chat_config_path() -> Path:
    """The shared user-scoped provider config used by CLI and console."""
    override = os.environ.get("FCODE_CONFIG_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "FCode"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "FCode"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "fcode"
    return base / "chat-config.json"
#: Providers a settings page may configure. ``mock`` needs nothing.
CONFIGURABLE_PROVIDERS = ("kimi", "deepseek", "local")
CHAT_PROVIDER_KEYS = ("mock",) + CONFIGURABLE_PROVIDERS

# The system prompt is not here: it describes the `fcode_action` block format,
# so it lives next to the parser that enforces it (flowagent.chat). A second
# copy in this module would drift out of step with that contract silently.

MAX_TEXT = 8_000
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_BYTES = 64 * 1024
MAX_ATTACHMENTS_BYTES = 128 * 1024
MAX_HISTORY_MESSAGES = 24
DEFAULT_MAX_COMPLETION = 2_048


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be an integer")
    if value <= 0:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be positive")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be a number")


def _require_price(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise AgentError("CHAT_PROVIDER_UNAVAILABLE", f"missing {name}")
    try:
        value = int(raw)
    except ValueError:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be an integer")
    if value < 0:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class ChatProviderConfig:
    key: str
    label: str
    model: str
    base_url: str = ""
    api_key: str = ""
    input_price_microusd_per_mtok: int = 0
    output_price_microusd_per_mtok: int = 0
    min_interval_seconds: float = 0.0
    timeout_seconds: float = 120.0
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION
    completion_field: str = "max_tokens"
    #: Whether this transport accepts OpenAI-style ``tools`` and returns
    #: ``tool_calls`` deltas. False means the chat loop falls back to the
    #: historical text-only turn (no tools offered, no execution).
    supports_tools: bool = False
    #: When the prices above were read, and from where. Mandatory for the same
    #: reason the delegation presets demand it: an undated price turns the
    #: budget ceiling into a guess. One settings page, one contract.
    price_as_of: str = ""
    price_source: str = ""

    @property
    def remote(self) -> bool:
        return bool(self.base_url)


def _ov_str(overrides: dict[str, Any], name: str) -> str:
    value = overrides.get(name)
    if value is None:
        return ""
    text = str(value).strip()
    return text


def _ov_int(overrides: dict[str, Any], name: str) -> int | None:
    raw = overrides.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be an integer")


def _ov_float(overrides: dict[str, Any], name: str) -> float | None:
    raw = overrides.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be a number")


def _remote_config(
    key: str,
    label: str,
    overrides: dict[str, Any],
    *,
    api_env: str,
    model_env: str,
    base_env: str,
    base_default: str,
    model_default: str,
    in_price_env: str,
    out_price_env: str,
    interval_env: str,
    price_as_of_env: str = "",
    price_source_env: str = "",
) -> ChatProviderConfig:
    """Resolve one remote provider: environment > user settings > default.

    Nothing is ever silently free: if neither settings nor env provide the key
    or both prices, the provider stays unavailable with the exact missing list.
    """
    api_key = os.environ.get(api_env, "").strip() or _ov_str(overrides, "api_key")
    model = os.environ.get(model_env, "").strip() or _ov_str(overrides, "model") or model_default
    base_url = os.environ.get(base_env, "").strip() or _ov_str(overrides, "base_url") or base_default

    missing: list[str] = []
    if not api_key:
        missing.append(api_env)

    raw_input = os.environ.get(in_price_env, "").strip()
    if raw_input:
        try:
            input_price = int(raw_input)
        except ValueError:
            raise AgentError("CHAT_CONFIG_INVALID", f"{in_price_env} must be an integer")
    else:
        input_price = _ov_int(overrides, "input_price_microusd_per_mtok")
        if input_price is None:
            missing.append(in_price_env)
    if input_price is not None and input_price <= 0:
        raise AgentError("CHAT_CONFIG_INVALID", "input price must be positive")

    raw_output = os.environ.get(out_price_env, "").strip()
    if raw_output:
        try:
            output_price = int(raw_output)
        except ValueError:
            raise AgentError("CHAT_CONFIG_INVALID", f"{out_price_env} must be an integer")
    else:
        output_price = _ov_int(overrides, "output_price_microusd_per_mtok")
        if output_price is None:
            missing.append(out_price_env)
    if output_price is not None and output_price <= 0:
        raise AgentError("CHAT_CONFIG_INVALID", "output price must be positive")
    # Prices are only half the fact: the date they were read makes them
    # auditable, and the delegation side already refuses to guess it.
    price_as_of = (
        os.environ.get(price_as_of_env, "").strip() if price_as_of_env else ""
    ) or _ov_str(overrides, "price_as_of")
    if not price_as_of and price_as_of_env:
        missing.append(f"{price_as_of_env}（价格日期）")
    if missing:
        raise AgentError(
            "CHAT_PROVIDER_UNAVAILABLE",
            f"{label} 未配置：缺少 " + "、".join(missing),
        )

    raw_interval = os.environ.get(interval_env, "").strip()
    interval = _env_float(interval_env, 0.0) if raw_interval else _ov_float(overrides, "min_interval_seconds")
    if interval is None:
        interval = 0.0
    max_completion = _ov_int(overrides, "max_completion_tokens")
    if max_completion is None:
        max_completion = _env_int("CHAT_MAX_COMPLETION_TOKENS", DEFAULT_MAX_COMPLETION)
    if not 1 <= max_completion <= 32768:
        raise AgentError("CHAT_CONFIG_INVALID", "max completion tokens must be 1..32768")
    return ChatProviderConfig(
        key=key,
        label=label,
        model=model,
        base_url=base_url,
        api_key=api_key,
        input_price_microusd_per_mtok=input_price,
        output_price_microusd_per_mtok=output_price,
        min_interval_seconds=max(0.0, interval),
        timeout_seconds=_env_float("CHAT_STREAM_TIMEOUT_SECONDS", 120.0),
        max_completion_tokens=max_completion,
        completion_field="max_completion_tokens" if key == "kimi" else "max_tokens",
        supports_tools=True,  # Moonshot / DeepSeek expose OpenAI-compatible tool calling
        price_as_of=price_as_of,
        price_source=(os.environ.get(price_source_env, "").strip() if price_source_env else "")
        or _ov_str(overrides, "price_source"),
    )


def _kimi_config(overrides: dict[str, Any]) -> ChatProviderConfig:
    return _remote_config(
        "kimi",
        "Kimi",
        overrides,
        api_env="KIMI_API_KEY",
        model_env="KIMI_MODEL",
        base_env="KIMI_BASE_URL",
        base_default="https://api.moonshot.cn/v1",
        model_default="kimi-k2.6",
        in_price_env="KIMI_INPUT_PRICE_MICROUSD_PER_MTOK",
        out_price_env="KIMI_OUTPUT_PRICE_MICROUSD_PER_MTOK",
        interval_env="KIMI_MIN_CALL_INTERVAL_SECONDS",
        price_as_of_env="KIMI_PRICE_AS_OF",
        price_source_env="KIMI_PRICE_SOURCE",
    )


def _deepseek_config(overrides: dict[str, Any]) -> ChatProviderConfig:
    return _remote_config(
        "deepseek",
        "DeepSeek",
        overrides,
        api_env="DEEPSEEK_API_KEY",
        model_env="DEEPSEEK_MODEL",
        base_env="DEEPSEEK_BASE_URL",
        base_default="https://api.deepseek.com/v1",
        model_default="deepseek-chat",
        in_price_env="DEEPSEEK_INPUT_PRICE_MICROUSD_PER_MTOK",
        out_price_env="DEEPSEEK_OUTPUT_PRICE_MICROUSD_PER_MTOK",
        interval_env="DEEPSEEK_MIN_CALL_INTERVAL_SECONDS",
        price_as_of_env="DEEPSEEK_PRICE_AS_OF",
        price_source_env="DEEPSEEK_PRICE_SOURCE",
    )


def _local_config(overrides: dict[str, Any]) -> ChatProviderConfig:
    endpoint = os.environ.get("FCODE_LOCAL_ENDPOINT", "").strip() or _ov_str(overrides, "base_url")
    if not endpoint:
        raise AgentError("CHAT_PROVIDER_UNAVAILABLE", "本地模型未配置：缺少 FCODE_LOCAL_ENDPOINT")
    max_completion = _ov_int(overrides, "max_completion_tokens")
    if max_completion is None:
        max_completion = _env_int("CHAT_MAX_COMPLETION_TOKENS", DEFAULT_MAX_COMPLETION)
    if not 1 <= max_completion <= 32768:
        raise AgentError("CHAT_CONFIG_INVALID", "max completion tokens must be 1..32768")
    return ChatProviderConfig(
        key="local",
        label="本地模型",
        model=os.environ.get("FCODE_LOCAL_MODEL", "").strip() or _ov_str(overrides, "model") or "fcode-qwen3-4b",
        base_url=endpoint,
        api_key=os.environ.get("FCODE_LOCAL_API_KEY", "").strip() or _ov_str(overrides, "api_key"),
        timeout_seconds=_env_float("CHAT_STREAM_TIMEOUT_SECONDS", 120.0),
        max_completion_tokens=max_completion,
    )


def _peek_model(key: str, overrides: dict[str, Any]) -> str:
    """The model name to display even when the provider is unavailable."""
    if key == "kimi":
        return os.environ.get("KIMI_MODEL", "").strip() or _ov_str(overrides, "model") or "kimi-k2.6"
    if key == "deepseek":
        return os.environ.get("DEEPSEEK_MODEL", "").strip() or _ov_str(overrides, "model") or "deepseek-chat"
    if key == "local":
        return os.environ.get("FCODE_LOCAL_MODEL", "").strip() or _ov_str(overrides, "model") or "fcode-qwen3-4b"
    return "offline-chat-mock-v1"


def resolve_chat_provider(
    key: str | None,
    overrides: dict[str, Any] | None = None,
) -> ChatProviderConfig:
    normalized = (key or "mock").strip().lower()
    override = overrides if isinstance(overrides, dict) else {}
    if normalized == "mock":
        return ChatProviderConfig(
            key="mock",
            label="离线 Mock",
            model="offline-chat-mock-v1",
            max_completion_tokens=_env_int("CHAT_MAX_COMPLETION_TOKENS", DEFAULT_MAX_COMPLETION),
            # 脚本化工具循环（FCODE_CHAT_MOCK_TOOLS）用于端到端测试；
            # 没有该环境变量时 mock 仍然只回文本。
            supports_tools=True,
        )
    if normalized == "kimi":
        return _kimi_config(override)
    if normalized == "deepseek":
        return _deepseek_config(override)
    if normalized == "local":
        return _local_config(override)
    raise AgentError(
        "CHAT_PROVIDER_UNAVAILABLE",
        f"对话模型 {normalized!r} 未接入；可用：mock / kimi / deepseek / local",
    )


# ------------------------------------------------------------ settings store


def load_chat_config(path: Path | None) -> dict[str, Any]:
    """Read the local settings file; anything unexpected reads as empty."""
    if path is None:
        return {"version": CHAT_CONFIG_VERSION, "providers": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": CHAT_CONFIG_VERSION, "providers": {}}
    if not isinstance(raw, dict) or raw.get("version") != CHAT_CONFIG_VERSION:
        return {"version": CHAT_CONFIG_VERSION, "providers": {}}
    providers = raw.get("providers")
    config: dict[str, Any] = {"version": CHAT_CONFIG_VERSION, "providers": {}}
    if isinstance(providers, dict):
        for key in CONFIGURABLE_PROVIDERS:
            block = providers.get(key)
            if not isinstance(block, dict):
                continue
            clean = {
                name: value for name, value in block.items()
                if isinstance(value, (str, int, float)) and str(value).strip() != ""
            }
            if key in ("kimi", "deepseek"):
                clean.pop("base_url", None)
            if clean:
                config["providers"][key] = clean
    default = raw.get("default_model")
    if default in CHAT_PROVIDER_KEYS:
        config["default_model"] = str(default)
    return config


def save_chat_config(path: Path | None, config: dict[str, Any]) -> None:
    """Atomic write with owner-only permissions; the key never goes anywhere else."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def redact_chat_config(config: dict[str, Any]) -> dict[str, Any]:
    """The API-facing copy: never the key itself, only set/masked flags."""
    providers: dict[str, Any] = {}
    for key, block in (config.get("providers") or {}).items():
        shown = {name: value for name, value in block.items() if name != "api_key"}
        api_key = str(block.get("api_key") or "")
        shown["api_key_set"] = bool(api_key)
        shown["api_key_masked"] = (api_key[:3] + "…" + api_key[-4:]) if len(api_key) > 8 else ("已设置" if api_key else "")
        providers[key] = shown
    return {
        "version": CHAT_CONFIG_VERSION,
        "providers": providers,
        "default_model": config.get("default_model", "mock"),
    }

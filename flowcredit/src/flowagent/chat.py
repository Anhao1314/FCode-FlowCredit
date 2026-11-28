"""Chat: the conversational surface, still budgeted and chained.

Since Phase 1 of agentic chat, a turn may run a bounded **read-only tool
loop** (ls_workspace / read_file): the model proposes tool calls, the host
gates them with the same Permissions class a run uses, charges one read per
call and may loop for up to FCODE_CHAT_MAX_TOOL_ROUNDS rounds. Write tools
stay out of chat — those still require the fcode_action proposal handoff to
a governed run. The governance line is unchanged:

* a chat turn is **one or more model calls**, each charged before dispatch and
  recorded as events in the same SHA-256 chain as a run;
* tools only run through the registry + gate; there is no path from the model
  to a filesystem write or a command;
* attachments are text folded into the user message, not files read by a tool;
* "regenerate" and "edit" append events and supersede old messages — the chain
  stays append-only, the active branch is a projection.

Provider resolution lives in :mod:`flowagent.chat_config`, the streaming
transport in :mod:`flowagent.chat_provider`; this module owns threads, budgets
and the append-only projection. The names are re-exported here so callers see
one chat surface.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .chat_config import (
    CHAT_CONFIG_VERSION,
    CHAT_PROVIDER_KEYS,
    CONFIGURABLE_PROVIDERS,
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_ATTACHMENTS_BYTES,
    MAX_HISTORY_MESSAGES,
    MAX_TEXT,
    ChatProviderConfig,
    _env_int,
    is_custom_provider_key,
    load_chat_config,
    redact_chat_config,
    resolve_chat_provider,
    save_chat_config,
)
from .chat_provider import (  # noqa: F401 - re-exported transport
    _MockProvider,
    _RemoteProvider,
    _cost,
    _estimate_tokens,
    parse_sse_data,
)
from .core.contracts import AgentError, Task
from .core.receipts import ReceiptStore
from .core.store import TaskStore, utc_now
from .pacing import pace_key, reserve_slot
from .runtime.budget import Budget, BudgetLimits


from .chat_protocol import (  # noqa: F401 - re-exported protocol helpers
    CHAT_VERSION,
    _ACTION_BLOCK,
    ACTION_MODES,
    _ACTION_MAX_BYTES,
    _ACTION_MAX_SOURCES,
    _read_action,
    parse_action_block,
)

__all__ = [
    "CHAT_VERSION",
    "CHAT_CONFIG_VERSION",
    "CONFIGURABLE_PROVIDERS",
    "CHAT_PROVIDER_KEYS",
    "ChatService",
    "ChatProviderConfig",
    "resolve_chat_provider",
    "load_chat_config",
    "save_chat_config",
    "redact_chat_config",
    "parse_sse_data",
]

#: Workspace references and attachments share one deny-list: a path picker
#: must not advertise credentials the text reader would refuse.
_SENSITIVE_WORKSPACE_NAMES = {
    "credentials", "credentials.json", "secrets", "secrets.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_SENSITIVE_WORKSPACE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".pyc"}
_BINARY_WORKSPACE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".tar",
    ".exe", ".dll", ".so", ".dylib", ".class", ".woff", ".woff2", ".ttf",
    ".ico", ".mp4", ".mov", ".mp3",
}
_WORKSPACE_LIST_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "work", "runs",
}


CHAT_SYSTEM_PROMPT = (
    "你是 FCode 里的对话助手。用中文、清楚、简洁地回答。"
    "问候、寒暄、探活这类消息直接回答，不调用任何工具；"
    "工具只在回答当前问题确实需要工作区事实时才用，一次够用就不重复调用。"
    "你可以调用只读工具查看工作区（ls_workspace 列目录、search_codebase 按语义"
    "检索代码与文档并返回路径/行号/片段、read_file 读文件、"
    "grep_workspace 正则搜索内容、find_files 按模式找文件、git_status 查看工作区"
    "git 状态与提交历史），基于真实文件内容回答。"
    "写文件/修改文件/运行命令/抓取网页也可以调用，但这些工具每次都要经过操作员审批，"
    "批准后才会执行；不要在未拿到工具结果时声称已经执行。"
    "如果任务需要多步执行、预算或独立验证，在回答末尾附一个执行提案块，让界面展示确认卡：\n"
    "```fcode_action\n"
    '{"goal": "把任务写成一句明确的目标（5–600 字）", "mode": "script", '
    '"sources": [], "reason": "为什么需要执行（可选）"}\n'
    "```\n"
    "mode 只能是 script（在工作区写文件并运行）或 research（抓取公开来源）；"
    "research 必须在 sources 里给 1–5 个真实的 http(s) URL。"
    "提案只是确认卡，用户点“开始执行”后才会创建受治理的委托运行。"
    "不需要执行时不要输出这个块。不知道就直说，不要编造。"
)

DEFAULT_CHAT_LIMITS = BudgetLimits(60, 0, 300_000, 200_000)


def merge_action(
    action: dict[str, Any] | None,
    *,
    goal: str | None = None,
    mode: str | None = None,
    sources: list[Any] | None = None,
) -> dict[str, Any]:
    """Apply the operator's edits to one proposal, under the parser's own rules.

    The confirmation card lets the user rewrite the goal, switch the mode and
    fix the sources. Those edits pass the same fail-closed checks the model's
    block does, so a hand-edited proposal can never become something
    ``parse_action_block`` would have refused.
    """
    merged = dict(action or {})
    if goal is not None:
        text = str(goal).strip()
        if not 5 <= len(text) <= 600:
            raise ValueError("goal must be 5 to 600 characters")
        merged["goal"] = text
    if mode is not None:
        key = str(mode).strip().lower()
        if key not in ACTION_MODES:
            raise ValueError("mode must be script or research")
        merged["mode"] = key
    if sources is not None:
        clean: list[str] = []
        for item in sources:
            text = str(item or "").strip()
            if not text or text in clean:
                continue
            clean.append(text)
        if len(clean) > _ACTION_MAX_SOURCES:
            raise ValueError(f"sources lists at most {_ACTION_MAX_SOURCES} URLs")
        if clean:
            merged["sources"] = clean
        else:
            merged.pop("sources", None)
    if not str(merged.get("goal") or "").strip():
        raise ValueError("the proposal carries no goal")
    merged.setdefault("mode", "script")
    return merged


# ------------------------------------------------------------------ service


class ChatService:
    """Threads, messages and streaming answers, backed by the task chain."""

    def __init__(self, db_path: Path, config_path: Path | None = None,
                 workspace_root: Path | str | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = TaskStore(db_path)
        self.receipts = ReceiptStore(self.store)
        #: 操作员长期记忆：与 chat.sqlite 并排的独立库，零 schema 耦合。
        from .chat_memory import MemoryStore

        self.memory = MemoryStore(db_path.with_name(f"{db_path.stem}-memory.sqlite"))
        #: 工具沙箱的根（对话可读写的目录）。None 表示不提供工具。
        self._workspace_root = Path(workspace_root) if workspace_root else None
        self._tool_ctx = None
        #: 写工具的逐调用审批注册表：approval_id → {thread_id, payload, decision}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._approvals_cv = threading.Condition()
        self._lock = threading.RLock()
        self._db_path = db_path
        # User settings live next to the chat database (Application Support in
        # the desktop bundle), yield to explicit environment values, and never
        # enter the event chain, traces or logs.
        self._config_path = config_path
        self._config = load_chat_config(config_path)
        #: The mtime guard makes external saves visible to every surface.
        self._config_loaded_at = time.time()
        self._config_mtime_ns = self._config_mtime_ns_on_disk()
        # Archiving is a tombstone, not a delete: the event chain and receipts
        # stay verifiable; only the sidebar hides the thread.
        self.store._conn.executescript("""
CREATE TABLE IF NOT EXISTS chat_thread_meta (
    thread_id   TEXT PRIMARY KEY,
    archived    INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT NOT NULL DEFAULT ''
);
""")

    # -------------------------------------------------------------- helpers

    def _tool_context(self):
        """{registry, permissions, tool_defs, sandbox} 或 None（未配置/构建失败）。

        只在第一次需要时构建；构建失败按"无工具"降级，不阻断对话。
        """
        if self._tool_ctx is not None:
            return self._tool_ctx or None
        if self._workspace_root is None:
            self._tool_ctx = False
            return None
        try:
            from .chat_tools import (
                build_chat_registry, build_chat_permissions, to_openai_tools,
            )
            from .tools.general.sandbox import WorkspaceSandbox
            # build_chat_registry 内部自建沙箱；这里传根路径，实例另建一份
            # 给 ctx（审批预览用 edit_preview/diff_preview）。
            sandbox = WorkspaceSandbox(self._workspace_root)
            registry = build_chat_registry(self._workspace_root)
            permissions = build_chat_permissions(registry)
            self._tool_ctx = {
                "registry": registry,
                "permissions": permissions,
                "tool_defs": to_openai_tools(registry),
                "sandbox": sandbox,
                "sandbox_root": str(self._workspace_root),
            }
        except Exception:  # noqa: BLE001 — 工具不可用不应阻断纯文本对话
            self._tool_ctx = False
        return self._tool_ctx or None

    # --------------------------------------------------- 写工具审批通道

    def request_tool_approval(self, thread_id: str, approval_id: str,
                              payload: dict[str, Any],
                              timeout: float = 300.0) -> dict[str, Any]:
        """注册一个待批工具调用并阻塞等待操作员决定（或超时）。

        approval_id 由调用方（工具循环）生成，SSE 事件与注册表共用同一个
        id——操作员的 POST 靠它找到待批项。返回 {"allow", "remember",
        "reason"}。线程 id 不匹配或被取消（流断开）按拒绝处理。
        """
        with self._approvals_cv:
            self._approvals[approval_id] = {
                "thread_id": thread_id, "payload": payload, "decision": None,
            }
        deadline = time.monotonic() + max(1.0, float(timeout))
        decision = None
        while decision is None:
            with self._approvals_cv:
                record = self._approvals.get(approval_id)
                if record is None:
                    break  # 已被取消
                if record["decision"] is not None:
                    decision = record["decision"]
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._approvals_cv.wait(remaining)
        with self._approvals_cv:
            self._approvals.pop(approval_id, None)
        return decision or {"allow": False, "remember": False, "reason": "timeout"}

    def decide_tool(self, thread_id: str, approval_id: str,
                    allow: bool, remember: bool = False) -> dict[str, Any] | None:
        """操作员对某个待批调用的决定；返回被决定的记录（供 404 判别）。"""
        with self._approvals_cv:
            record = self._approvals.get(approval_id)
            if record is None or record["thread_id"] != thread_id:
                return None
            record["decision"] = {"allow": bool(allow), "remember": bool(remember)}
            self._approvals_cv.notify_all()
        return dict(record["decision"])

    def cancel_thread_approvals(self, thread_id: str) -> None:
        """流断开/取消时，撤销该线程的所有待批调用（按拒绝处理）。"""
        with self._approvals_cv:
            for record in self._approvals.values():
                if record["thread_id"] == thread_id and record["decision"] is None:
                    record["decision"] = {"allow": False, "remember": False,
                                          "reason": "stream cancelled"}
            self._approvals_cv.notify_all()

    def limits(self) -> BudgetLimits:
        return BudgetLimits(
            max_calls=_env_int("FCODE_CHAT_MAX_CALLS", DEFAULT_CHAT_LIMITS.max_calls),
            # Agentic chat: each tool call charges one read. 0 used to freeze
            # tools out entirely; the default now allows a bounded read loop.
            max_reads=_env_int("FCODE_CHAT_MAX_READS", 30),
            max_cost_microusd=_env_int(
                "FCODE_CHAT_MAX_COST_MICROUSD", DEFAULT_CHAT_LIMITS.max_cost_microusd
            ),
            max_tokens=_env_int("FCODE_CHAT_MAX_TOKENS", DEFAULT_CHAT_LIMITS.max_tokens),
        )

    def _config_mtime_ns_on_disk(self) -> int | None:
        if not self._config_path:
            return None
        try:
            return self._config_path.stat().st_mtime_ns
        except OSError:
            return None

    def _refresh_config(self) -> None:
        """Reload an externally changed document before any model resolution."""
        with self._lock:
            mtime_ns = self._config_mtime_ns_on_disk()
            if mtime_ns == self._config_mtime_ns:
                return
            self._config = load_chat_config(self._config_path)
            self._config_mtime_ns = mtime_ns
            self._config_loaded_at = time.time()

    def _overrides_for(self, model: str | None) -> dict[str, Any]:
        self._refresh_config()
        key = (model or "mock").strip().lower()
        if key not in CONFIGURABLE_PROVIDERS and not is_custom_provider_key(key):
            return {}
        return (self._config.get("providers") or {}).get(key) or {}

    def check_model(self, model: str | None) -> dict[str, Any]:
        config = resolve_chat_provider(model, self._overrides_for(model))
        return {
            "key": config.key,
            "label": config.label,
            "model": config.model,
            "remote": config.remote,
            "maxCompletionTokens": config.max_completion_tokens,
            "completionField": config.completion_field,
            "supportsTools": config.supports_tools,
            "supportsParallelTools": config.supports_parallel_tools,
            "supportsReasoning": config.supports_reasoning,
            "maxContextTokens": config.max_context_tokens,
            "maxOutputTokens": config.max_output_tokens,
            "price": {
                "inputMicrousdPerMtok": config.input_price_microusd_per_mtok,
                "outputMicrousdPerMtok": config.output_price_microusd_per_mtok,
            },
        }

    def test_provider(self, provider: str | None) -> dict[str, Any]:
        """One tiny live completion; proves URL/key/model/request shape work.

        This is an operator-initiated connectivity check, not a chat turn: it
        is not charged to a thread and its output never enters the chain. The
        completion is capped so a misconfigured endpoint cannot generate a
        large bill.
        """
        key = (provider or "mock").strip().lower()
        if key == "mock":
            return {"ok": True, "provider": "mock", "model": "offline-mock",
                    "reply": "mock", "note": "offline provider needs no network"}
        try:
            config = resolve_chat_provider(key, self._overrides_for(key))
            if not config.remote:
                raise AgentError(
                    "CHAT_PROVIDER_UNAVAILABLE",
                    f"{key} has no remote endpoint",
                )
            probe = replace(
                config,
                max_completion_tokens=max(1, min(16, config.max_completion_tokens)),
                timeout_seconds=max(1.0, min(20.0, config.timeout_seconds)),
            )
            reply_parts: list[str] = []
            usage: dict[str, Any] = {}
            for event in _RemoteProvider(probe).stream([
                {"role": "user", "content": "Reply with the single word OK."},
            ]):
                if not isinstance(event, dict):
                    continue
                if isinstance(event.get("delta"), str):
                    reply_parts.append(event["delta"])
                if isinstance(event.get("usage"), dict):
                    usage = dict(event["usage"])
            return {
                "ok": True,
                "provider": key,
                "model": config.model,
                "reply": "".join(reply_parts).strip()[:120],
                "usage": usage,
                "completion_field": config.completion_field,
            }
        except AgentError as error:
            return {"ok": False, "provider": key, "code": error.code,
                    "message": error.message}
        except Exception as error:  # noqa: BLE001 — connectivity errors are data
            return {
                "ok": False, "provider": key, "code": "CHAT_TEST_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }

    def default_model(self) -> str:
        self._refresh_config()
        return (
            os.environ.get("FCODE_DEFAULT_RUN_MODEL", "").strip()
            or str(self._config.get("default_model") or "mock")
        )

    def config_snapshot(self) -> dict[str, Any]:
        """Redacted settings + the shared model menu, for every product surface."""
        self._refresh_config()
        return {
            "config": redact_chat_config(self._config),
            "models": self.chat_models(),
            "default": self.default_model(),
            "source": self.config_source(),
        }

    def config_source(self) -> dict[str, Any]:
        """Where the shared provider configuration lives and when it was read."""
        self._refresh_config()
        loaded_at = float(getattr(self, "_config_loaded_at", 0.0) or 0.0)
        info: dict[str, Any] = {
            "path": str(self._config_path) if self._config_path else "",
            "loadedAt": loaded_at,
            "fileMtime": 0.0,
            "stale": False,
        }
        if not self._config_path:
            return info
        try:
            stat = self._config_path.stat()
            info["fileMtime"] = stat.st_mtime
        except OSError:
            return info
        info["stale"] = bool(stat.st_mtime_ns != self._config_mtime_ns)
        return info

    def chat_models(self) -> list[dict[str, Any]]:
        """The exact provider catalog used by Work, governance and schedules."""
        self._refresh_config()
        from .providers.catalog import (
            merged_env,
            probe_local_endpoint,
            provider_catalog,
            settings_as_env,
        )

        env = merged_env(settings_as_env(self._config.get("providers") or {}))
        endpoint = env.get("FCODE_LOCAL_ENDPOINT", "").strip() or None
        entries = provider_catalog(
            env=env,
            local_endpoint=endpoint,
            local_probe=(lambda: probe_local_endpoint(endpoint)) if endpoint else None,
        )
        models: list[dict[str, Any]] = []
        for entry in entries:
            item = dict(entry, remote=entry["key"] != "mock")
            if not item["available"]:
                try:
                    resolve_chat_provider(item["key"], self._overrides_for(item["key"]))
                except AgentError as error:
                    item["reason"] = error.message
            models.append(item)
        return models

    def update_config(
        self,
        *,
        provider: str | None = None,
        values: dict[str, Any] | None = None,
        default_model: str | None = None,
        delete_provider: bool = False,
    ) -> dict[str, Any]:
        """Validate and persist settings overrides; empty string clears a field.

        Returns the redacted config plus the refreshed model menu. The stored
        file is the only place the API key is written — never the chain, traces
        or logs — and it lives outside the repository.
        """
        provider = provider.strip().lower() if provider is not None else None
        custom_provider = is_custom_provider_key(provider)
        if provider is not None and provider not in CONFIGURABLE_PROVIDERS and not custom_provider:
            raise AgentError(
                "CHAT_CONFIG_INVALID",
                "provider must be a built-in or an ASCII alphanumeric custom key",
            )
        if delete_provider and provider is None:
            raise AgentError("CHAT_CONFIG_INVALID", "delete_provider requires provider")
        clean: dict[str, str] = {}
        if provider is not None and not delete_provider:
            # A delete carries no fields to validate; requiring ``values``
            # here would make the settings page's delete call impossible
            # without a payload the operation never reads.
            allowed = {
                "api_key", "model",
                "input_price_microusd_per_mtok", "output_price_microusd_per_mtok",
                "min_interval_seconds", "max_completion_tokens",
                # Prices without the date they were read are a guess; the
                # settings page collects both, for chat and for delegations.
                "price_as_of", "price_source",
                # Provider request-shape capabilities. These are what make
                # "OpenAI-compatible" actually compatible across vendors.
                "completion_field", "temperature_mode", "json_mode", "stream",
                "supports_tools", "supports_parallel_tools",
                "supports_reasoning", "supports_effort", "reasoning_field",
                "max_context_tokens", "max_output_tokens",
            }
            # Endpoint overrides for the cloud providers are an exfiltration
            # path (a stolen settings write could redirect the stored key).
            # Their base URL stays host/env-configured; only local endpoints
            # are editable from the UI.
            if provider == "local" or custom_provider:
                allowed.add("base_url")
            if custom_provider:
                allowed.update({"label", "supports_effort"})
            if not isinstance(values, dict):
                raise AgentError("CHAT_CONFIG_INVALID", "values must be an object")
            for name, raw in values.items():
                if name not in allowed:
                    raise AgentError("CHAT_CONFIG_INVALID", f"unknown field {name!r}")
                text = "" if raw is None else str(raw).strip()
                if text == "":
                    clean[name] = ""  # explicit clear: fall back to env/default
                    continue
                if name == "api_key" and len(text) > 300:
                    raise AgentError("CHAT_CONFIG_INVALID", "api key is too long")
                if name == "model" and len(text) > 120:
                    raise AgentError("CHAT_CONFIG_INVALID", "model name is too long")
                if name == "label" and len(text) > 80:
                    raise AgentError("CHAT_CONFIG_INVALID", "provider label is too long")
                if name in ("json_mode", "stream", "supports_tools",
                            "supports_parallel_tools", "supports_reasoning",
                            "supports_effort") and text not in ("0", "1"):
                    raise AgentError(
                        "CHAT_CONFIG_INVALID", f"{name} must be 0 or 1")
                if name == "completion_field" and text not in (
                        "max_tokens", "max_completion_tokens"):
                    raise AgentError(
                        "CHAT_CONFIG_INVALID",
                        "completion_field must be max_tokens or max_completion_tokens",
                    )
                if name == "temperature_mode" and text not in (
                        "send-configured", "omit-unless-pinned", "pin-zero"):
                    raise AgentError(
                        "CHAT_CONFIG_INVALID",
                        "temperature_mode must be send-configured, "
                        "omit-unless-pinned or pin-zero",
                    )
                if name == "reasoning_field" and (
                        len(text) > 40 or not text.replace("_", "").isalnum()):
                    raise AgentError(
                        "CHAT_CONFIG_INVALID",
                        "reasoning_field must be an identifier of at most 40 characters",
                    )
                if name in ("max_context_tokens", "max_output_tokens"):
                    try:
                        tokens = int(text)
                    except ValueError:
                        raise AgentError(
                            "CHAT_CONFIG_INVALID", f"{name} must be an integer")
                    if name == "max_context_tokens" and not 1_024 <= tokens <= 10_000_000:
                        raise AgentError(
                            "CHAT_CONFIG_INVALID",
                            "max_context_tokens must be 1024..10000000",
                        )
                    if name == "max_output_tokens" and not 0 <= tokens <= 1_000_000:
                        raise AgentError(
                            "CHAT_CONFIG_INVALID",
                            "max_output_tokens must be 0..1000000",
                        )
                    clean[name] = str(tokens)
                    continue
                if name == "base_url" and not text.startswith(("http://", "https://")):
                    raise AgentError("CHAT_CONFIG_INVALID", "base url must start with http(s)://")
                if name in ("input_price_microusd_per_mtok", "output_price_microusd_per_mtok"):
                    try:
                        price = int(text)
                    except ValueError:
                        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be an integer")
                    if price < 0 or (price == 0 and not custom_provider):
                        rule = "non-negative" if custom_provider else "positive"
                        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be {rule}")
                    clean[name] = str(price)
                    continue
                if name == "min_interval_seconds":
                    try:
                        interval = float(text)
                    except ValueError:
                        raise AgentError("CHAT_CONFIG_INVALID", "min_interval_seconds must be a number")
                    if interval < 0 or interval > 3600:
                        raise AgentError("CHAT_CONFIG_INVALID", "min_interval_seconds must be 0..3600")
                    clean[name] = text
                    continue
                if name == "max_completion_tokens":
                    try:
                        tokens = int(text)
                    except ValueError:
                        raise AgentError("CHAT_CONFIG_INVALID", "max_completion_tokens must be an integer")
                    if not 1 <= tokens <= 32768:
                        raise AgentError("CHAT_CONFIG_INVALID", "max_completion_tokens must be 1..32768")
                    clean[name] = str(tokens)
                    continue
                clean[name] = text
        with self._lock:
            self._refresh_config()
            providers = dict(self._config.get("providers") or {})
            if provider is not None:
                if delete_provider:
                    providers.pop(provider, None)
                else:
                    block = dict(providers.get(provider) or {})
                    for name, value in clean.items():
                        if value == "":
                            block.pop(name, None)
                        else:
                            block[name] = value
                    if block:
                        providers[provider] = block
                    else:
                        providers.pop(provider, None)
            config = {"version": CHAT_CONFIG_VERSION, "providers": providers}
            if default_model is not None:
                normalized_default = default_model.strip().lower()
                known = set(CHAT_PROVIDER_KEYS) | set(providers) | {
                    item["key"] for item in self.chat_models()
                }
                if delete_provider:
                    known.discard(provider)
                if normalized_default not in known:
                    raise AgentError("CHAT_CONFIG_INVALID", "default model must be in the shared catalog")
                config["default_model"] = normalized_default
            elif delete_provider and self._config.get("default_model") == provider:
                config["default_model"] = "mock"
            elif self._config.get("default_model"):
                config["default_model"] = self._config["default_model"]
            self._config = config
            save_chat_config(self._config_path, config)
            self._config_loaded_at = time.time()
            self._config_mtime_ns = self._config_mtime_ns_on_disk()
        return self.config_snapshot()

    def _thread_exists(self, thread_id: str) -> bool:
        row = self.store._conn.execute(
            "SELECT 1 FROM task WHERE id=?", (thread_id,)
        ).fetchone()
        return row is not None

    def _create_thread(self, title: str) -> Task:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        thread_id = f"chat-{stamp}-{secrets.token_hex(2)}"
        task = Task(
            id=thread_id,
            principal="console-operator",
            objective=(title or "新对话")[:80],
            binding={"kind": "chat"},
            allowed_tools=[],
            model_grant={},
        )
        return self.store.create(task)

    def _load_task(self, thread_id: str) -> Task:
        if not thread_id.startswith("chat-"):
            raise AgentError("CHAT_THREAD_NOT_FOUND", f"no chat thread {thread_id!r}")
        try:
            return self.store.load(thread_id)
        except AgentError as error:
            if error.code == "TASK_NOT_FOUND":
                raise AgentError("CHAT_THREAD_NOT_FOUND", f"no chat thread {thread_id!r}")
            raise

    def post_explanation(
        self,
        thread_id: str,
        *,
        text: str,
        model: str = "",
        tokens: int = 0,
        cost_microusd: int = 0,
        title: str = "",
        finding_id: str = "",
        audit_id: str = "",
        kind: str = "explain",
        candidate_count: int = 0,
    ) -> dict[str, Any]:
        """把一条宿主生成的解读写进对话线程（输出在对话里，不伪造轮次）。

        与 ``send`` 的差别：没有用户输入、没有模型轮次记账——解读的预算
        与收据由调用方（解释层）负责，这里只把已成文的事实追加进线程链，
        让操作员在对话流里读到它。thread_id 为空时新建线程（title 作标题）。
        ``finding_id``/``audit_id``/``kind``/``candidate_count`` 进 ``explain``
        元数据：前端据此渲染结构化卡片（解读/探员候选），文本本身保持干净。
        """
        clean_text = str(text or "").strip()
        if not clean_text:
            raise AgentError("CHAT_EXPLAIN_EMPTY", "解读文本为空，拒绝写入对话")
        if thread_id:
            task = self._load_task(thread_id)
        else:
            task = self._create_thread(title)
        message_id = f"a-{secrets.token_hex(6)}"
        explain = {
            "finding_id": str(finding_id or ""),
            "audit_id": str(audit_id or ""),
            "kind": str(kind or "explain"),
            "candidate_count": max(0, int(candidate_count or 0)),
        }
        detail = {
            "id": message_id,
            "user_id": "",
            "text": clean_text,
            "reasoning": "",
            "status": "ok",
            "usage": {
                "total_tokens": max(0, int(tokens or 0)),
                "cost_microusd": max(0, int(cost_microusd or 0)),
            },
            "receipt_ref": "",
            "provider": "",
            "model": str(model or ""),
            "error": {},
            "explain": explain,
        }
        task = self._append(task, "chat_assistant", detail)
        return {"thread_id": task.id, "message_id": message_id}

    def _append(self, task: Task, event: str, detail: dict[str, Any]) -> Task:
        with self._lock:
            return self.store.save(task, task.sequence, event, detail)

    @staticmethod
    def _title(text: str) -> str:
        compact = " ".join(str(text or "").split())
        return (compact[:30] + "…") if len(compact) > 30 else (compact or "新对话")

    def _replay(self, thread_id: str) -> list[dict[str, Any]]:
        """Project the append-only events onto the active message branch."""
        messages: list[dict[str, Any]] = []
        superseded: set[str] = set()
        by_id: dict[str, dict[str, Any]] = {}
        for event in self.store.events(thread_id):
            detail = (event.get("payload") or {}).get("detail") or {}
            kind = event["type"]
            seq = int(event["sequence"])
            if kind == "chat_user":
                message = {
                    "id": str(detail.get("id") or f"m-{seq}"),
                    "role": "user",
                    "text": str(detail.get("text") or ""),
                    "attachments": list(detail.get("attachments") or []),
                    "status": "ok",
                    "seq": seq,
                    "at": event["at"],
                }
                messages.append(message)
                by_id[message["id"]] = message
            elif kind == "chat_assistant":
                message = {
                    "id": str(detail.get("id") or f"a-{seq}"),
                    "role": "assistant",
                    "text": str(detail.get("text") or ""),
                    "reasoning": str(detail.get("reasoning") or ""),
                    "status": str(detail.get("status") or "ok"),
                    "usage": dict(detail.get("usage") or {}),
                    "receipt_ref": str(detail.get("receipt_ref") or ""),
                    "provider": str(detail.get("provider") or ""),
                    "model": str(detail.get("model") or ""),
                    "error": dict(detail.get("error") or {}),
                    "seq": seq,
                    "at": event["at"],
                }
                # Only a real proposal earns an action field: an empty projection
                # ({}) is truthy in JavaScript and used to paint a blank card.
                action = detail.get("action")
                if isinstance(action, dict) and action:
                    message["action"] = dict(action)
                # Agentic chat: ordered blocks (text/tool) ride on the
                # assistant detail; older threads simply do not have them.
                if isinstance(detail.get("blocks"), list) and detail["blocks"]:
                    message["blocks"] = list(detail["blocks"])
                if isinstance(detail.get("tool_calls"), list) and detail["tool_calls"]:
                    message["tool_calls"] = list(detail["tool_calls"])
                # 解读卡元数据：前端据此渲染结构化卡片，正文保持干净。
                if isinstance(detail.get("explain"), dict) and detail["explain"]:
                    message["explain"] = dict(detail["explain"])
                messages.append(message)
                by_id[message["id"]] = message
            elif kind == "chat_run_started":
                target = by_id.get(str(detail.get("message_id") or ""))
                if target is not None:
                    target["run"] = {
                        "id": str(detail.get("run_id") or ""),
                        "goal": str(detail.get("goal") or ""),
                        "mode": str(detail.get("mode") or ""),
                        "at": event["at"],
                    }
            elif kind == "chat_edit":
                target = str(detail.get("target_id") or "")
                target_seq = int((by_id.get(target) or {}).get("seq", seq))
                superseded.add(target)
                for message in messages:
                    if int(message.get("seq", 0)) > target_seq:
                        superseded.add(message["id"])
                new = detail.get("message") or {}
                message = {
                    "id": str(new.get("id") or f"m-{seq}"),
                    "role": "user",
                    "text": str(new.get("text") or ""),
                    "attachments": list(new.get("attachments") or []),
                    "status": "ok",
                    "edited": True,
                    "seq": seq,
                    "at": event["at"],
                }
                messages.append(message)
                by_id[message["id"]] = message
            elif kind == "chat_regenerate":
                target = str(detail.get("target_id") or "")
                target_seq = int((by_id.get(target) or {}).get("seq", seq))
                superseded.add(target)
                for message in messages:
                    if int(message.get("seq", 0)) > target_seq:
                        superseded.add(message["id"])
            elif kind == "chat_cancelled":
                target = str(detail.get("assistant_id") or "")
                text = str(detail.get("text") or "")
                reasoning = str(detail.get("reasoning") or "")
                existing = by_id.get(target)
                if existing is not None:
                    existing["status"] = "cancelled"
                    existing["text"] = text or existing["text"]
                    existing["reasoning"] = reasoning or existing.get("reasoning", "")
                else:
                    message = {
                        "id": target or f"a-{seq}",
                        "role": "assistant",
                        "text": text,
                        "reasoning": reasoning,
                        "status": "cancelled",
                        "usage": dict(detail.get("usage") or {}),
                        "receipt_ref": "",
                        "provider": str(detail.get("provider") or ""),
                        "model": str(detail.get("model") or ""),
                        "error": {},
                        "seq": seq,
                        "at": event["at"],
                    }
                    messages.append(message)
                    by_id[message["id"]] = message
            elif kind == "chat_error":
                target = str(detail.get("assistant_id") or "")
                existing = by_id.get(target)
                if existing is not None:
                    existing["status"] = "error"
                    existing["error"] = {
                        "code": str(detail.get("code") or "CHAT_FAILED"),
                        "message": str(detail.get("message") or ""),
                    }
                else:
                    message = {
                        "id": target or f"a-{seq}",
                        "role": "assistant",
                        "text": str(detail.get("text") or ""),
                        "reasoning": "",
                        "status": "error",
                        "usage": dict(detail.get("usage") or {}),
                        "receipt_ref": "",
                        "provider": "",
                        "model": "",
                        "error": {
                            "code": str(detail.get("code") or "CHAT_FAILED"),
                            "message": str(detail.get("message") or ""),
                        },
                        "seq": seq,
                        "at": event["at"],
                    }
                    messages.append(message)
                    by_id[message["id"]] = message
        return [message for message in messages if message["id"] not in superseded]

    @staticmethod
    def _public_message(message: dict[str, Any]) -> dict[str, Any]:
        """Strip attachment bodies before a message leaves the server."""
        public = {key: value for key, value in message.items() if key != "attachments"}
        public["attachments"] = [
            {"name": item.get("name", ""), "bytes": int(item.get("bytes") or 0)}
            for item in message.get("attachments") or []
            if isinstance(item, dict)
        ]
        return public

    def list_threads(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        rows = self.store._conn.execute(
            "SELECT t.id, t.body, t.updated_at FROM task t"
            " WHERE t.id LIKE 'chat-%' AND NOT EXISTS ("
            "   SELECT 1 FROM chat_thread_meta m"
            "   WHERE m.thread_id = t.id AND m.archived = 1)"
            " ORDER BY t.updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        threads = []
        for row in rows:
            try:
                body = json.loads(row["body"])
            except ValueError:
                continue
            threads.append({
                "id": row["id"],
                "title": str(body.get("objective") or "新对话"),
                "updated_at": row["updated_at"],
            })
        return threads

    def thread_count(self) -> int:
        row = self.store._conn.execute(
            "SELECT COUNT(*) AS n FROM task t"
            " WHERE t.id LIKE 'chat-%' AND NOT EXISTS ("
            "   SELECT 1 FROM chat_thread_meta m"
            "   WHERE m.thread_id = t.id AND m.archived = 1)",
        ).fetchone()
        return int(row["n"] or 0)

    def archive_thread(self, thread_id: str) -> dict[str, Any]:
        """Hide a thread from the sidebar without destroying its evidence."""
        with self._lock:
            self._load_task(thread_id)
            with self.store.transaction():
                self.store._conn.execute(
                    "INSERT INTO chat_thread_meta (thread_id, archived, archived_at)"
                    " VALUES (?, 1, ?)"
                    " ON CONFLICT(thread_id) DO UPDATE SET"
                    " archived = 1, archived_at = excluded.archived_at",
                    (thread_id, utc_now()),
                )
        return {"id": thread_id, "archived": True}

    def archive_stale(self, days: int, *, now: datetime | None = None) -> int:
        """Archive threads untouched for ``days`` days; never delete events.

        Retention is a visibility policy, not a shredder: the tombstone hides
        the thread from the sidebar while the chain and receipts stay auditable.
        ``days <= 0`` disables it.
        """
        if days <= 0:
            return 0
        moment = now or datetime.now(timezone.utc)
        cutoff = (moment - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with self._lock, self.store.transaction():
            rows = self.store._conn.execute(
                "SELECT t.id FROM task t WHERE t.id LIKE 'chat-%'"
                " AND t.updated_at < ? AND NOT EXISTS ("
                "   SELECT 1 FROM chat_thread_meta m"
                "   WHERE m.thread_id = t.id AND m.archived = 1)",
                (cutoff,),
            ).fetchall()
            for row in rows:
                self.store._conn.execute(
                    "INSERT INTO chat_thread_meta (thread_id, archived, archived_at)"
                    " VALUES (?, 1, ?)"
                    " ON CONFLICT(thread_id) DO UPDATE SET"
                    " archived = 1, archived_at = excluded.archived_at",
                    (row["id"], utc_now()),
                )
        return len(rows)

    def link_run(
        self,
        thread_id: str,
        message_id: str,
        run_id: str,
        *,
        goal: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Anchor a confirmed proposal to the run it started.

        The thread keeps one ``chat_run_started`` event; the run itself lives
        in the run pipeline and stays the only source of its progress. A
        second confirmation of the same message is refused rather than
        silently re-anchored — one proposal, one run.
        """
        with self._lock:
            thread = self._load_task(thread_id)
            messages = self._replay(thread_id)
            message = next((item for item in messages if item["id"] == message_id), None)
            if message is None or message.get("role") != "assistant":
                raise AgentError("CHAT_MESSAGE_INVALID", "run anchor needs an assistant message")
            action = message.get("action") or {}
            if not action:
                raise AgentError("CHAT_ACTION_MISSING", "this message carries no execution proposal")
            if message.get("run"):
                existing = (message["run"] or {}).get("id")
                raise AgentError(
                    "CHAT_ACTION_ALREADY_STARTED",
                    f"proposal already started run {existing}",
                    details={"run_id": existing},
                )
            # The anchor records what the operator approved (edited goal and
            # mode), not the model's original wording.
            self._append(thread, "chat_run_started", {
                "message_id": message_id,
                "run_id": run_id,
                "goal": str(goal if goal is not None else action.get("goal") or ""),
                "mode": str(mode if mode is not None else action.get("mode") or ""),
            })
        return {"thread_id": thread_id, "message_id": message_id, "run_id": run_id}

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        task = self._load_task(thread_id)
        events = self.store.events(thread_id)
        messages = self._replay(thread_id)
        budget = Budget(self.store, f"chat:{thread_id}", self.limits()).snapshot()
        return {
            "id": thread_id,
            "title": task.objective or "新对话",
            "created_at": events[0]["at"] if events else "",
            "updated_at": events[-1]["at"] if events else "",
            "model": task.binding.get("model", ""),
            "messages": [self._public_message(message) for message in messages],
            "budget": budget,
            "events": len(events),
            "chain_head": events[-1]["hash"] if events else "",
        }

    def read_workspace_file(self, path: str) -> dict[str, Any]:
        """Read one text file from the chat workspace for an attachment.

        This is the same read-only jail the chat tools use, with the attachment
        limits applied before the text can enter a message. Credentials and
        binary files are refused here rather than becoming prompt content.
        """
        if self._workspace_root is None:
            raise AgentError(
                "CHAT_WORKSPACE_UNAVAILABLE",
                "this chat has no workspace; start the console with --repo to enable it",
            )
        from .tools.general.sandbox import WorkspaceSandbox

        result = WorkspaceSandbox(self._workspace_root).read(path)
        relative = str(result["path"])
        name = Path(relative).name
        parts = [part.lower() for part in Path(relative).parts]
        suffix = Path(relative).suffix.lower()
        if any(part == ".env" or part.startswith(".env.") for part in parts) \
                or any(part in _SENSITIVE_WORKSPACE_NAMES for part in parts) \
                or suffix in _SENSITIVE_WORKSPACE_SUFFIXES:
            raise AgentError(
                "CHAT_ATTACHMENT_DENIED",
                "credential-like files are not added to chat context",
            )
        if suffix in _BINARY_WORKSPACE_SUFFIXES:
            raise AgentError(
                "CHAT_ATTACHMENT_UNSUPPORTED",
                "only text files can be attached to chat",
            )
        if int(result["bytes"]) > MAX_ATTACHMENT_BYTES:
            raise AgentError(
                "CHAT_ATTACHMENT_INVALID",
                f"{name} is larger than {MAX_ATTACHMENT_BYTES // 1024} KiB",
            )
        text = str(result["content"])
        if "\x00" in text:
            raise AgentError(
                "CHAT_ATTACHMENT_UNSUPPORTED",
                f"{name} is not a text file",
            )
        return {
            "name": name,
            "path": relative,
            "text": text,
            "bytes": int(result["bytes"]),
            "sha256": str(result["sha256"]),
        }

    def list_workspace_files(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        """Search relative paths for the composer's ``@`` reference.

        The list is intentionally metadata-only: it never reads file content,
        never follows symlinks, and hides the same credential/binary names the
        attachment reader refuses. Ranking prefers a basename match, then a
        path match, then shallow paths, so typing ``@app`` finds ``src/app.js``
        before unrelated files.
        """
        if self._workspace_root is None:
            raise AgentError(
                "CHAT_WORKSPACE_UNAVAILABLE",
                "this chat has no workspace; start the console with --repo to enable it",
            )
        root = self._workspace_root
        needle = str(query or "").strip().lower()
        try:
            wanted = int(limit)
        except (TypeError, ValueError):
            wanted = 20
        wanted = max(1, min(wanted, 50))

        candidates: list[str] = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in _WORKSPACE_LIST_SKIP_DIRS and not name.startswith(".")
            )
            for name in sorted(filenames):
                lowered = name.lower()
                suffix = Path(name).suffix.lower()
                if lowered == ".env" or lowered.startswith(".env.") \
                        or lowered in _SENSITIVE_WORKSPACE_NAMES \
                        or suffix in _SENSITIVE_WORKSPACE_SUFFIXES \
                        or suffix in _BINARY_WORKSPACE_SUFFIXES:
                    continue
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                candidates.append(path.relative_to(root).as_posix())
                if len(candidates) >= 5_000:
                    truncated = True
                    break
            if truncated:
                break

        def score(relative: str) -> tuple[int, int, int, str]:
            name = Path(relative).name.lower()
            low = relative.lower()
            if not needle:
                rank = 0
            elif name == needle:
                rank = 0
            elif name.startswith(needle):
                rank = 1
            elif needle in name:
                rank = 2
            elif low.startswith(needle):
                rank = 3
            elif needle in low:
                rank = 4
            else:
                rank = 99
            return (rank, relative.count("/"), len(relative), relative)

        matched = [item for item in candidates if score(item)[0] < 99]
        matched.sort(key=score)
        return {
            "files": [
                {"path": relative, "name": Path(relative).name}
                for relative in matched[:wanted]
            ],
            "total": len(matched),
            "truncated": truncated,
        }

    # ------------------------------------------------------------- validation

    @staticmethod
    def _normalise_attachments(attachments: Any) -> list[dict[str, Any]]:
        if attachments is None:
            return []
        if not isinstance(attachments, list):
            raise AgentError("CHAT_ATTACHMENT_INVALID", "attachments must be a list")
        if len(attachments) > MAX_ATTACHMENTS:
            raise AgentError("CHAT_ATTACHMENT_INVALID", f"at most {MAX_ATTACHMENTS} attachments")
        clean: list[dict[str, Any]] = []
        total = 0
        for item in attachments:
            if not isinstance(item, dict):
                raise AgentError("CHAT_ATTACHMENT_INVALID", "each attachment must be an object")
            name = str(item.get("name") or "").strip()[:120]
            text = item.get("text")
            if not name or not isinstance(text, str):
                raise AgentError("CHAT_ATTACHMENT_INVALID", "attachment needs name and text")
            size = len(text.encode("utf-8"))
            if size > MAX_ATTACHMENT_BYTES:
                raise AgentError(
                    "CHAT_ATTACHMENT_INVALID",
                    f"{name} is larger than {MAX_ATTACHMENT_BYTES // 1024} KiB",
                )
            total += size
            clean.append({"name": name, "text": text, "bytes": size})
        if total > MAX_ATTACHMENTS_BYTES:
            raise AgentError("CHAT_ATTACHMENT_INVALID", "attachments exceed the total size limit")
        return clean

    # ---------------------------------------------------------------- prompts

    @staticmethod
    def _attachment_block(attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return ""
        parts = []
        for item in attachments:
            parts.append(
                f"\n\n[附件：{item['name']}]\n```text\n{item['text']}\n```"
            )
        return "".join(parts)

    def _ingest_memory(self, text: str, thread: Any) -> None:
        """Promote durable operator preferences after the user event lands.

        Failure is silent: memory is augmentation, never a reason to fail a
        turn. Extraction only sees this operator-authored text.
        """
        try:
            self.memory.ingest(text, source=f"chat:{getattr(thread, 'id', '')}")
        except Exception:  # noqa: BLE001 — memory must not break chat
            pass

    def _prompt_messages(self, thread_id: str) -> list[dict[str, Any]]:
        active = self._replay(thread_id)[-MAX_HISTORY_MESSAGES:]
        query = next(
            (message["text"] for message in reversed(active) if message["role"] == "user"),
            "",
        )
        system = CHAT_SYSTEM_PROMPT + self.memory.context_block(query=query)
        messages = [{"role": "system", "content": system}]
        # 只给最近一条带工具历史的 assistant 回填 tool_calls/tool 消息：
        # 更早的轮次只留最终文本，避免上下文被历史工具输出撑爆。
        last_tool_assistant = next(
            (m for m in reversed(active)
             if m["role"] == "assistant" and isinstance(m.get("tool_calls"), list)
             and m["tool_calls"]),
            None,
        )
        for message in active:
            if message["role"] == "user":
                content = message["text"] + self._attachment_block(message.get("attachments") or [])
                messages.append({"role": "user", "content": content})
                continue
            # 带工具历史的 assistant：先回放 tool_calls + tool 结果，再回放
            # 最终文本——与真实时序一致（tool 结果必须紧跟 tool_calls）。
            if message is last_tool_assistant:
                calls = list(message.get("tool_calls") or [])[:8]
                messages.append({
                    "role": "assistant", "content": "",
                    "tool_calls": [
                        {"id": str(call.get("call_id") or ""), "type": "function",
                         "function": {"name": str(call.get("name") or ""),
                                      "arguments": str(call.get("arguments") or "{}")}}
                        for call in calls
                    ],
                })
                for call in calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": str(call.get("call_id") or ""),
                        "content": str(call.get("content") or "")[:2000],
                    })
            if message["status"] == "ok" and message["text"]:
                messages.append({"role": "assistant", "content": message["text"]})
        return messages

    # --------------------------------------------------------------- streaming

    def _provider(self, model: str | None):
        config = resolve_chat_provider(model, self._overrides_for(model))
        if config.key == "mock":
            return config, _MockProvider(config.model)
        return config, _RemoteProvider(config)

    def router_provider(self, model: str | None = None):
        """Provider pair for one-shot text helpers such as intent routing.

        The mock is excluded: a scripted reply must never fabricate a
        classification. Callers treat ``(None, None)`` as "classifier
        unavailable" and fall back to deterministic rules.
        """
        config, provider = self._provider(model)
        if config.key == "mock":
            return None, None
        return config, provider

    def _answer(
        self,
        thread: Task,
        user_message: dict[str, Any],
        model: str | None,
        approval: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        from .chat_answer import AnswerStream

        yield from AnswerStream(self, thread, user_message, model, approval).events()

    def send(
        self,
        *,
        thread_id: str | None,
        text: str,
        attachments: Any = None,
        model: str | None = None,
        approval: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        clean_text = str(text or "").strip()
        clean_attachments = self._normalise_attachments(attachments)
        if not clean_text and not clean_attachments:
            raise AgentError("CHAT_MESSAGE_INVALID", "message is empty")
        if len(clean_text) > MAX_TEXT:
            raise AgentError("CHAT_MESSAGE_INVALID", f"message exceeds {MAX_TEXT} characters")

        with self._lock:
            thread = self._load_task(thread_id) if thread_id else self._create_thread(self._title(clean_text))
        message_id = f"m-{secrets.token_hex(6)}"
        detail = {
            "id": message_id,
            "text": clean_text,
            "attachments": clean_attachments,
            "model": str(model or "mock"),
        }
        thread = self._append(thread, "chat_user", detail)
        self._ingest_memory(clean_text, thread)
        user_message = {
            "id": message_id,
            "role": "user",
            "text": clean_text,
            "attachments": clean_attachments,
            "status": "ok",
            "seq": thread.sequence,
            "at": "",
        }
        yield from self._answer(thread, user_message, model, approval)

    def edit(
        self,
        *,
        thread_id: str,
        message_id: str,
        text: str,
        attachments: Any = None,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        clean_text = str(text or "").strip()
        clean_attachments = self._normalise_attachments(attachments)
        if not clean_text and not clean_attachments:
            raise AgentError("CHAT_MESSAGE_INVALID", "message is empty")
        if len(clean_text) > MAX_TEXT:
            raise AgentError("CHAT_MESSAGE_INVALID", f"message exceeds {MAX_TEXT} characters")
        with self._lock:
            thread = self._load_task(thread_id)
            target = next(
                (message for message in self._replay(thread_id)
                 if message["id"] == message_id and message["role"] == "user"),
                None,
            )
            if target is None:
                raise AgentError("CHAT_MESSAGE_NOT_FOUND", f"no user message {message_id!r}")
            new_user = {
                "id": f"m-{secrets.token_hex(6)}",
                "text": clean_text,
                "attachments": clean_attachments,
                "model": str(model or "mock"),
            }
            thread = self._append(thread, "chat_edit", {
                "target_id": message_id,
                "message": new_user,
            })
        self._ingest_memory(clean_text, thread)
        user_message = {
            "id": new_user["id"],
            "role": "user",
            "text": clean_text,
            "attachments": clean_attachments,
            "status": "ok",
            "seq": thread.sequence,
            "at": "",
        }
        yield from self._answer(thread, user_message, model)

    def regenerate(
        self,
        *,
        thread_id: str,
        message_id: str,
        model: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        with self._lock:
            thread = self._load_task(thread_id)
            active = self._replay(thread_id)
            index = next(
                (position for position, message in enumerate(active) if message["id"] == message_id),
                None,
            )
            if index is None or active[index]["role"] != "assistant":
                raise AgentError("CHAT_MESSAGE_NOT_FOUND", f"no assistant message {message_id!r}")
            user_index = index
            while user_index >= 0 and active[user_index]["role"] != "user":
                user_index -= 1
            if user_index < 0:
                raise AgentError("CHAT_MESSAGE_NOT_FOUND", "no user message precedes this answer")
            user_message = active[user_index]
            thread = self._append(thread, "chat_regenerate", {"target_id": message_id})
        yield from self._answer(thread, user_message, model)

    def close(self) -> None:
        self.store.close()

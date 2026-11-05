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
import re
import secrets
import threading
import time
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
    _peek_model,
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

CHAT_VERSION = "flowagent.chat/v1"

CHAT_SYSTEM_PROMPT = (
    "你是 FCode 里的对话助手。用中文、清楚、简洁地回答。"
    "你可以调用只读工具查看工作区（ls_workspace 列目录、read_file 读文件），"
    "基于真实文件内容回答；但你**不能写文件、运行命令或抓取网页**，"
    "也绝不声称你执行了未通过工具返回的动作。"
    "如果用户的请求需要写文件、运行命令或抓取网页，"
    "在回答末尾附一个执行提案块，让界面展示确认卡：\n"
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

#: The model's execution proposal travels as a fenced block. Parsing is
#: deliberately strict and fail-closed: a malformed proposal yields no card
#: (and the visible text keeps the raw block), because silently repairing it
#: would put an execution button under something the model did not propose.
_ACTION_BLOCK = re.compile(r"```fcode_action\s*\n(.*?)\n```", re.DOTALL)
ACTION_MODES = ("script", "research")
_ACTION_MAX_BYTES = 2_000
_ACTION_MAX_SOURCES = 5


def _read_action(payload_text: str) -> dict[str, Any] | None:
    """One candidate block to a proposal, or None if it is not one."""
    if len(payload_text.encode("utf-8")) > _ACTION_MAX_BYTES:
        return None
    try:
        payload = json.loads(payload_text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    goal = payload.get("goal")
    if not isinstance(goal, str) or not 5 <= len(goal.strip()) <= 600:
        return None
    mode = str(payload.get("mode") or "script").strip().lower()
    if mode not in ACTION_MODES:
        return None
    action: dict[str, Any] = {"goal": goal.strip(), "mode": mode}
    reason = payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        action["reason"] = reason.strip()[:200]
    sources = payload.get("sources")
    if isinstance(sources, list):
        clean: list[str] = []
        for item in sources:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in clean:
                clean.append(text)
        if clean:
            action["sources"] = clean[:_ACTION_MAX_SOURCES]
    return action


def parse_action_block(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split one finished answer into (visible_text, action | None).

    The last block that reads as a whole proposal wins: a model may quote the
    format while explaining it, and a malformed tail (or an earlier draft) must
    not void a proposal it really made. Every candidate is validated by the
    same rules — invalid JSON, an out-of-range goal, an unknown mode or an
    oversized payload simply are not proposals. With no valid block the answer
    is returned exactly as written, a malformed one included: no silent repair.
    """
    raw = str(text or "")
    action: dict[str, Any] | None = None
    found = False
    for candidate in _ACTION_BLOCK.finditer(raw):
        found = True
        valid = _read_action(candidate.group(1).strip())
        if valid is not None:
            action = valid
    if not found:
        return raw, None
    if action is None:
        return raw, None
    # The machinery leaves the bubble: every block goes, whichever one won.
    visible = _ACTION_BLOCK.sub("", raw).strip()
    return visible, action


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
        #: When this process read the file. The settings page shows it next to
        #: the file's mtime, because a key saved by *another* process (or
        #: before this one started) is invisible here until a restart — the
        #: confusion that made a configured Kimi look unconfigured.
        self._config_loaded_at = time.time()
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

    def _overrides_for(self, model: str | None) -> dict[str, Any]:
        key = (model or "mock").strip().lower()
        if key not in CONFIGURABLE_PROVIDERS:
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
            "price": {
                "inputMicrousdPerMtok": config.input_price_microusd_per_mtok,
                "outputMicrousdPerMtok": config.output_price_microusd_per_mtok,
            },
        }

    def default_model(self) -> str:
        return str(self._config.get("default_model") or "mock")

    def config_snapshot(self) -> dict[str, Any]:
        """Redacted settings + model menu, the shape the settings page renders."""
        return {
            "config": redact_chat_config(self._config),
            "models": self.chat_models(),
            "default": self.default_model(),
            "source": self.config_source(),
        }

    def config_source(self) -> dict[str, Any]:
        """Where the running process got its provider configuration.

        ``stale`` is the honest part: the file on disk is newer than what this
        process loaded, so an edit made elsewhere is not in effect yet. Without
        this the settings page happily showed a key the running server had
        never read.
        """
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
            info["fileMtime"] = self._config_path.stat().st_mtime
        except OSError:
            return info
        info["stale"] = bool(info["fileMtime"] > loaded_at)
        return info

    def chat_models(self) -> list[dict[str, Any]]:
        """The chat model menu: availability judged from overrides + env."""
        models: list[dict[str, Any]] = []
        labels = {"mock": "离线 Mock", "kimi": "Kimi 云端", "deepseek": "DeepSeek 云端", "local": "本地模型"}
        for key in CHAT_PROVIDER_KEYS:
            overrides = self._overrides_for(key)
            try:
                config = resolve_chat_provider(key, overrides)
                models.append({
                    "key": key,
                    "label": labels[key],
                    "model": config.model,
                    "available": True,
                    "reason": "",
                    "remote": config.remote,
                    "price": {
                        "inputMicrousdPerMtok": config.input_price_microusd_per_mtok,
                        "outputMicrousdPerMtok": config.output_price_microusd_per_mtok,
                    } if config.remote else None,
                })
            except AgentError as error:
                models.append({
                    "key": key,
                    "label": labels[key],
                    "model": _peek_model(key, overrides),
                    "available": False,
                    "reason": error.message,
                    "remote": key != "mock",
                    "price": None,
                })
        return models

    def update_config(
        self,
        *,
        provider: str | None = None,
        values: dict[str, Any] | None = None,
        default_model: str | None = None,
    ) -> dict[str, Any]:
        """Validate and persist settings overrides; empty string clears a field.

        Returns the redacted config plus the refreshed model menu. The stored
        file is the only place the API key is written — never the chain, traces
        or logs — and it lives outside the repository.
        """
        if provider is not None and provider not in CONFIGURABLE_PROVIDERS:
            raise AgentError(
                "CHAT_CONFIG_INVALID",
                f"provider must be one of {', '.join(CONFIGURABLE_PROVIDERS)}",
            )
        clean: dict[str, str] = {}
        if provider is not None:
            allowed = {
                "api_key", "model",
                "input_price_microusd_per_mtok", "output_price_microusd_per_mtok",
                "min_interval_seconds", "max_completion_tokens",
                # Prices without the date they were read are a guess; the
                # settings page collects both, for chat and for delegations.
                "price_as_of", "price_source",
            }
            # Endpoint overrides for the cloud providers are an exfiltration
            # path (a stolen settings write could redirect the stored key).
            # Their base URL stays host/env-configured; only local endpoints
            # are editable from the UI.
            if provider == "local":
                allowed.add("base_url")
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
                if name == "base_url" and not text.startswith(("http://", "https://")):
                    raise AgentError("CHAT_CONFIG_INVALID", "base url must start with http(s)://")
                if name in ("input_price_microusd_per_mtok", "output_price_microusd_per_mtok"):
                    try:
                        price = int(text)
                    except ValueError:
                        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be an integer")
                    if price <= 0:
                        raise AgentError("CHAT_CONFIG_INVALID", f"{name} must be positive")
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
            providers = dict(self._config.get("providers") or {})
            if provider is not None:
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
                if default_model not in CHAT_PROVIDER_KEYS:
                    raise AgentError("CHAT_CONFIG_INVALID", f"default model must be one of {', '.join(CHAT_PROVIDER_KEYS)}")
                config["default_model"] = default_model
            elif self._config.get("default_model"):
                config["default_model"] = self._config["default_model"]
            self._config = config
            save_chat_config(self._config_path, config)
            # Saving is reading: this process now holds the newest copy, so the
            # freshness line the settings page shows goes quiet by itself.
            self._config_loaded_at = time.time()
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

    def _prompt_messages(self, thread_id: str) -> list[dict[str, Any]]:
        messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
        active = self._replay(thread_id)[-MAX_HISTORY_MESSAGES:]
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

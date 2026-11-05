"""``mcp.json``: the host-reviewed list of MCP servers.

The file lives at the repository root and is a host decision in the same
sense as the provider environment: nothing here is model-writable, and its
contents are never echoed into traces (an ``env`` value of the form
``"env:VAR_NAME"`` resolves from the spawning process at launch, so a secret
never needs to be written down at all).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError

__all__ = ["McpServerConfig", "load_mcp_config", "SERVER_NAME_PATTERN"]

#: Server names must survive the tool-name contract: ``mcp__{name}__{tool}``.
SERVER_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class McpServerConfig:
    """One stdio MCP server the host chose to attach."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    #: Literal values, or ``"env:VAR"`` to resolve from the environment at
    #: launch (values are never logged or traced).
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    #: Tools the host classifies as read-only. Everything else imports as
    #: consequential (approval-gated) regardless of what the server claims.
    readonly_tools: tuple[str, ...] = ()
    timeout: float = 30.0
    #: v1 reserves a seat for external billing disclosure; no pricing is
    # trusted or applied yet.
    billing: str = "not-configured"


def load_mcp_config(path: str | Path | None) -> dict[str, McpServerConfig]:
    """Load the host's MCP servers. Missing file → no servers, no error.

    A malformed file is a host configuration error and fails loudly: a
    silently skipped server would make "which tools exist" depend on whether
    anyone read the log.
    """
    if path is None:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AgentError("MCP_CONFIG_INVALID", f"cannot read {target}: {error}") from error
    servers = data.get("servers") if isinstance(data, dict) else data
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise AgentError("MCP_CONFIG_INVALID", "mcp.json must contain a servers object")

    out: dict[str, McpServerConfig] = {}
    for name, entry in servers.items():
        if not isinstance(name, str) or not SERVER_NAME_PATTERN.match(name):
            raise AgentError(
                "MCP_CONFIG_INVALID",
                f"server name {name!r} must match {SERVER_NAME_PATTERN.pattern}",
            )
        if not isinstance(entry, dict) or not entry.get("command"):
            raise AgentError("MCP_CONFIG_INVALID", f"server {name!r} needs a command")
        readonly = entry.get("readonlyTools") or entry.get("readonly_tools") or []
        if not isinstance(readonly, list) or not all(isinstance(item, str) for item in readonly):
            raise AgentError("MCP_CONFIG_INVALID", f"server {name!r}: readonlyTools must be a list of tool names")
        out[name] = McpServerConfig(
            name=name,
            command=str(entry["command"]),
            args=tuple(str(arg) for arg in entry.get("args") or []),
            env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
            cwd=str(entry.get("cwd") or ""),
            readonly_tools=tuple(readonly),
            timeout=float(entry.get("timeout", 30.0)),
            billing=str(entry.get("billing", "not-configured")),
        )
    return out


def resolve_env_value(value: Any) -> str:
    """Resolve one configured env value; ``env:NAME`` reads the process env."""
    text = str(value)
    if text.startswith("env:"):
        import os

        return os.environ.get(text[4:], "")
    return text

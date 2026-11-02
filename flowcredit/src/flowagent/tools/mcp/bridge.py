"""The bridge itself: import MCP tools into the closed registry.

Every imported tool is a first-class :class:`ToolSpec`: the gate validates its
arguments against the schema the host imported, the budget charges the call,
the observation carries the result and its hash, and a timeout or a dead
process is an unknown outcome that is never retried. The server classifies
nothing: unless the host marked a tool readonly in ``mcp.json``, it imports
with the write effect and lands in the approval set.
"""

from __future__ import annotations

import re
import time
from typing import Any

from ...core.contracts import AgentError
from ..base import EFFECT_READ, EFFECT_WRITE, Param, ToolSpec
from .client import McpStdioClient
from .config import McpServerConfig, load_mcp_config

__all__ = ["McpBridge", "tool_full_name", "params_from_schema"]

#: Tool names must satisfy the runtime's tool-name contract once namespaced.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
#: Imported string parameters get an explicit ceiling so a proposal through
#: an imported tool is bounded exactly like a native one.
IMPORTED_STRING_MAX = 8_192

_KIND_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def tool_full_name(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


def params_from_schema(schema: Any) -> tuple[Param, ...] | None:
    """Map a JSON-Schema object to declared params, or refuse the import.

    ``None`` means "this tool cannot be imported honestly": a parameter type
    the gate cannot validate (objects, unions, arrays of non-strings) would
    either widen the schema or fake it, and neither is acceptable. The tool
    is reported unavailable with the reason.
    """
    if schema is None:
        return ()
    if not isinstance(schema, dict):
        return None
    declared_type = schema.get("type")
    if declared_type not in (None, "object"):
        return None
    properties = schema.get("properties")
    if properties is None:
        properties = {}
    if not isinstance(properties, dict):
        return None
    if any(keyword in schema for keyword in ("oneOf", "anyOf", "allOf")):
        return None
    required = schema.get("required") or []
    if not isinstance(required, list):
        return None

    params: list[Param] = []
    for name, raw in properties.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            return None
        raw_type = str(raw.get("type", ""))
        if raw_type == "array":
            items = raw.get("items")
            if not isinstance(items, dict) or items.get("type") != "string":
                return None
            kind = "array"
        else:
            kind = _KIND_MAP.get(raw_type)
            if kind is None:
                return None
        enum: tuple[str, ...] | None = None
        if raw.get("enum") is not None:
            values = raw["enum"]
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                return None
            enum = tuple(values)
        params.append(Param(
            name=name,
            kind=kind,
            required=name in required,
            enum=enum,
            description=str(raw.get("description", ""))[:300] or "",
            max_length=IMPORTED_STRING_MAX if kind == "string" else None,
        ))
    return tuple(params)


class _ImportedTool:
    """Book-keeping for one imported tool (spec + classification + client)."""

    def __init__(self, server: str, tool: str, spec: ToolSpec, readonly: bool) -> None:
        self.server = server
        self.tool = tool
        self.spec = spec
        self.readonly = readonly


class McpBridge:
    """Connects the configured servers and imports their tools once."""

    def __init__(self, config_path: str | Any, *, cwd: Any = None) -> None:
        self._configs: dict[str, McpServerConfig] = load_mcp_config(config_path)
        self._clients: dict[str, McpStdioClient] = {}
        self._imported: dict[str, _ImportedTool] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._refused: dict[str, list[dict[str, str]]] = {}
        self._cwd = cwd

    # ----------------------------------------------------------------- connect

    def connect(self) -> None:
        """Start every configured server and import what it offers.

        Per-server best effort with honest bookkeeping: a server that fails
        to start or negotiate is recorded unavailable with its reason, exactly
        like an unavailable model in the catalog. One bad server never hides
        another's tools and never blocks the console.
        """
        for name, config in self._configs.items():
            status = {"server": name, "connected": False, "tools": [], "refused": [], "reason": ""}
            try:
                client = McpStdioClient(config, cwd=self._cwd)
                client.start()
                tools = client.list_tools()
                imported, refused = self._import_server(config, client, tools)
                self._clients[name] = client
                status.update(connected=True, tools=sorted(imported), refused=refused)
            except AgentError as error:
                status.update(reason=f"{error.code}: {error.message}")
            except Exception as error:  # noqa: BLE001 — one server must not kill the rest
                status.update(reason=f"{type(error).__name__}: {error}")
            self._status[name] = status

    def disconnect(self) -> None:
        for client in self._clients.values():
            client.stop()
        self._clients.clear()

    def _import_server(
        self,
        config: McpServerConfig,
        client: McpStdioClient,
        tools: list[dict[str, Any]],
    ) -> tuple[list[str], list[dict[str, str]]]:
        imported: list[str] = []
        refused: list[dict[str, str]] = []
        readonly = set(config.readonly_tools)
        for tool in tools:
            raw_name = str(tool.get("name") or "")
            lowered = raw_name.lower()
            full = tool_full_name(config.name, lowered)
            reason = ""
            if not _TOOL_NAME.match(full):
                reason = f"imported name {full!r} violates the tool-name contract"
            elif full in self._imported:
                reason = f"name {full!r} collides with an earlier import"
            else:
                params = params_from_schema(tool.get("inputSchema"))
                if params is None:
                    reason = "input schema uses types the gate cannot validate"
                if not reason:
                    self._imported[full] = _ImportedTool(
                        server=config.name,
                        tool=raw_name,
                        spec=ToolSpec(
                            name=full,
                            summary_policy="detailed",
                            summary=str(tool.get("description") or f"{config.name} tool {raw_name}")[:200],
                            effect=EFFECT_READ if lowered in readonly else EFFECT_WRITE,
                            params=params,
                            handler=self._make_handler(config.name, raw_name),
                            outputs=(
                                Param.output("text", "string"),
                                Param.output("is_error", "boolean"),
                                Param.output("latency_ms", "integer"),
                                Param.output("server", "string"),
                                Param.output("tool", "string"),
                            ),
                            version="mcp",
                        ),
                        readonly=lowered in readonly,
                    )
                    imported.append(full)
                    continue
            refused.append({"tool": raw_name, "reason": reason})
        return imported, refused

    def _make_handler(self, server: str, tool: str):
        def handler(arguments: dict[str, Any]) -> dict[str, Any]:
            return self.call(server, tool, arguments)
        return handler

    # -------------------------------------------------------------------- read

    def specs(self) -> list[ToolSpec]:
        return [item.spec for item in self._imported.values()]

    def specs_map(self) -> dict[str, ToolSpec]:
        return {name: item.spec for name, item in self._imported.items()}

    def approval_names(self) -> tuple[str, ...]:
        """Imported tools the console treats as consequential.

        Everything imports consequential unless the host marked it readonly —
        the classification is the host's, never the server's claim.
        """
        return tuple(
            name for name, item in self._imported.items()
            if not item.readonly
        )

    def statuses(self) -> list[dict[str, Any]]:
        return [dict(status) for status in self._status.values()]

    def connected(self) -> bool:
        return bool(self._clients)

    # ------------------------------------------------------------------- call

    def call(self, server: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._clients.get(server)
        started = time.monotonic()
        if client is None or not client.alive():
            # Pre-dispatch unavailability is confirmed: nothing was sent.
            self._mark_lost(server, "server is not running")
            raise AgentError(
                "MCP_SERVER_UNAVAILABLE",
                f"server {server!r} is unavailable; its tools stay listed but cannot run",
            )
        try:
            result = client.call_tool(tool, arguments)
        except AgentError as error:
            if error.details.get("outcome") == "unknown":
                self._mark_lost(server, error.code)
            raise
        latency = int((time.monotonic() - started) * 1000)
        is_error = bool(result.get("isError"))
        text = self._content_text(result)
        if is_error:
            raise AgentError("MCP_TOOL_ERROR", f"{server}.{tool} answered isError: {text[:300]}")
        return {
            "text": text,
            "is_error": False,
            "latency_ms": latency,
            "server": server,
            "tool": tool,
        }

    @staticmethod
    def _content_text(result: dict[str, Any]) -> str:
        blocks = result.get("content")
        if not isinstance(blocks, list):
            return ""
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)

    def _mark_lost(self, server: str, reason: str) -> None:
        status = self._status.get(server)
        if not isinstance(status, dict):
            status = {"server": server, "connected": False, "tools": [], "refused": [], "reason": ""}
            self._status[server] = status
        status["connected"] = False
        status["reason"] = reason

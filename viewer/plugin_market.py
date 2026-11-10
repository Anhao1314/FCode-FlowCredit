"""Plugin marketplace: a curated, host-reviewed catalog over MCP servers.

The market is honest about what it can do:

* **builtin** plugins are capabilities the runtime already ships. They are
  listed for discovery; there is nothing to install.
* **mcp** plugins are stdio MCP servers from this catalog. Installing one
  appends a reviewed entry to ``mcp.json`` — the same host decision the
  bridge already reads at boot. Uninstalling removes it. The bridge reloads
  off-thread; a failed command shows up as a failed server, never as a fake
  install.

The catalog is code, not user input. The UI can only pick an id; it cannot
invent a command. That keeps ``mcp.json`` host-reviewed even when the action
comes from a browser.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["PluginMarket", "MarketError"]

BUILTIN_PLUGINS: list[dict[str, Any]] = [
    {
        "id": "code-search",
        "name": "代码检索",
        "category": "检索",
        "kind": "builtin",
        "summary": "repo_map / search_codebase / grep_workspace，先看结构再定位。",
        "tools": ["repo_map", "search_codebase", "grep_workspace", "find_files"],
    },
    {
        "id": "files",
        "name": "文件读写",
        "category": "文件",
        "kind": "builtin",
        "summary": "工作区内列目录、读文件、最小改动写入与锚定替换。",
        "tools": ["ls_workspace", "read_file", "read_file_raw", "write_file", "edit_file"],
    },
    {
        "id": "browser",
        "name": "浏览器执行面",
        "category": "执行",
        "kind": "builtin",
        "summary": "本机浏览器的 inspect / act，截图与 DOM 证据进运行链。",
        "tools": ["browser_inspect", "browser_act"],
    },
    {
        "id": "terminal",
        "name": "受限终端",
        "category": "执行",
        "kind": "builtin",
        "summary": "白名单命令在沙箱里执行，退出码与输出进收据。",
        "tools": ["run_command", "terminal"],
    },
    {
        "id": "web-fetch",
        "name": "网页抓取",
        "category": "检索",
        "kind": "builtin",
        "summary": "受限域名抓取公开网页，作为研究来源。",
        "tools": ["fetch_url"],
    },
    {
        "id": "operator-ask",
        "name": "操作员提问",
        "category": "协作",
        "kind": "builtin",
        "summary": "卡在关键决策时向操作员提问，答案进运行事件链。",
        "tools": ["ask_operator"],
    },
]

MCP_PLUGINS: list[dict[str, Any]] = [
    {
        "id": "filesystem",
        "name": "文件系统 MCP",
        "category": "文件",
        "kind": "mcp",
        "summary": "把一个目录挂给 MCP 文件工具；写操作仍走审批。",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{root}"],
        "readonly_tools": [],
    },
    {
        "id": "fetch",
        "name": "网页抓取 MCP",
        "category": "检索",
        "kind": "mcp",
        "summary": "标准 fetch MCP server；只读工具，主机仍按收据审计。",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "readonly_tools": ["fetch"],
    },
    {
        "id": "git",
        "name": "Git MCP",
        "category": "开发",
        "kind": "mcp",
        "summary": "对当前仓库执行只读 Git 查询；变更仍由内置工具与门控负责。",
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "{repo}"],
        "readonly_tools": ["git_status", "git_log", "git_diff"],
    },
    {
        "id": "time",
        "name": "时间与时区 MCP",
        "category": "工具",
        "kind": "mcp",
        "summary": "取当前时间与时区换算；只读，无副作用。",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "readonly_tools": ["get_current_time", "convert_time"],
    },
]

_CATALOG = {entry["id"]: entry for entry in [*BUILTIN_PLUGINS, *MCP_PLUGINS]}


class MarketError(Exception):
    """Raised for unknown plugins, duplicates or malformed mcp.json."""


class PluginMarket:
    """Read the catalog, install/uninstall reviewed MCP entries."""

    def __init__(self, *, config_path: Path | str, repo: Path | str | None) -> None:
        self.config_path = Path(config_path)
        self.repo = Path(repo).expanduser().resolve() if repo else None
        self.workspace = self.repo / "workspace-general" if self.repo else Path.cwd()

    # ------------------------------------------------------------------ reads

    def entries(self, *, tool_names: set[str] | None = None,
                mcp_servers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        configured = self.configured()
        statuses = {str(item.get("server")): item for item in (mcp_servers or [])}
        tool_names = tool_names or set()
        out = []
        for entry in [*BUILTIN_PLUGINS, *MCP_PLUGINS]:
            item = dict(entry)
            if entry["kind"] == "builtin":
                available = [name for name in entry.get("tools", []) if name in tool_names]
                item.update(
                    installed=bool(available),
                    status="connected" if available else "unavailable",
                    tools=available,
                    missing_tools=[name for name in entry.get("tools", []) if name not in tool_names],
                )
            else:
                installed = entry["id"] in configured
                status = statuses.get(entry["id"]) or {}
                if installed and status.get("connected"):
                    state = "connected"
                elif installed and status:
                    state = "failed"
                else:
                    state = "installed" if installed else "not_installed"
                item.update(
                    installed=installed,
                    status=state,
                    tools=list(status.get("tools") or []),
                    reason=str(status.get("reason") or ""),
                    command=self.render_command(entry),
                )
            out.append(item)
        return out

    def get(self, plugin_id: str) -> dict[str, Any]:
        entry = _CATALOG.get(str(plugin_id or ""))
        if entry is None:
            raise MarketError(f"unknown plugin: {plugin_id!r}")
        return dict(entry)

    def configured(self) -> dict[str, dict[str, Any]]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise MarketError(f"cannot read {self.config_path}: {error}") from error
        servers = data.get("servers") if isinstance(data, dict) else data
        if servers is None:
            return {}
        if not isinstance(servers, dict):
            raise MarketError("mcp.json must contain a servers object")
        return servers

    def render_command(self, entry: dict[str, Any]) -> list[str]:
        args = [str(arg).replace("{repo}", str(self.repo)).replace("{root}", str(self.workspace))
                for arg in entry.get("args", [])]
        return [str(entry.get("command", "")), *args]

    # ----------------------------------------------------------------- writes

    def install(self, plugin_id: str) -> dict[str, Any]:
        entry = self.get(plugin_id)
        if entry["kind"] != "mcp":
            raise MarketError(f"{entry['name']} 是内置能力，无需安装")
        servers = self.configured()
        if entry["id"] in servers:
            raise MarketError(f"{entry['name']} 已安装")
        servers[entry["id"]] = {
            "command": entry["command"],
            "args": self.render_command(entry)[1:],
            "readonlyTools": list(entry.get("readonly_tools") or []),
        }
        self._write(servers)
        return {**entry, "installed": True, "command": self.render_command(entry)}

    def uninstall(self, plugin_id: str) -> dict[str, Any]:
        entry = self.get(plugin_id)
        if entry["kind"] != "mcp":
            raise MarketError(f"{entry['name']} 是内置能力，不能卸载")
        servers = self.configured()
        if entry["id"] not in servers:
            raise MarketError(f"{entry['name']} 尚未安装")
        servers.pop(entry["id"], None)
        self._write(servers)
        return {**entry, "installed": False}

    def _write(self, servers: dict[str, dict[str, Any]]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"servers": servers}
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.config_path.parent),
            prefix=f".{self.config_path.name}.", suffix=".tmp", delete=False,
        )
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.close()
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, self.config_path)
        except Exception:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
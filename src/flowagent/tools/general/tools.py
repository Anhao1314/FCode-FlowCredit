"""The general toolset: workspace files, edits, a restricted terminal, and
governed web fetches.

Registration order is the contract the tests pin. ``ls_workspace`` and
``read_file`` are reads; ``write_file`` and ``edit_file`` declare the write
effect (the grant decides whether that class ever runs) and are named
consequential by the domain, so the console asks a human before executing;
``run_command`` declares the write effect too — even a whitelisted readonly
program is dispatched through the same consequential-capable tool, and the
console's per-command readonly classification decides whether an approval is
needed. ``fetch_url`` declares the network effect. All of them flow through
the same gate → budget → receipt path as every finance tool.

``report_blocked`` is the one control action in the set: a read-effect
declaration that the work cannot proceed, whose consequence is the runtime's
to decide (``Loop._reported_blocked`` turns it into a terminal
``INSUFFICIENT_EVIDENCE`` failure, never a success). It is registered here
because the shared system prompt tells every model to propose it when the
evidence runs out: without it in the general registry, a model that follows
that instruction was refused as ``TOOL_NOT_REGISTERED`` and the run ended
``denied`` — a governance event for an honest stop the host itself asked for.
"""

from __future__ import annotations

from typing import Any, Callable

from ...core.contracts import AgentError
from ..base import EFFECT_NETWORK, EFFECT_READ, EFFECT_WRITE, Param, ToolSpec
from .commands import CommandRunner, MAX_TIMEOUT_SECONDS
from .fetch import UrlFetcher
from .sandbox import MAX_WRITE_CHARS, WorkspaceSandbox
from . import summaries

__all__ = ["GENERAL_TOOLS", "MAX_BLOCK_REASON_CHARS", "build_general_tools"]

#: A stop declaration is a reason, not a report: bounded so the prompt and the
#: chain entry stay small. The model's explanation survives whole up to this.
MAX_BLOCK_REASON_CHARS = 600


def _out(name: str, kind: str, required: bool = True, nullable: bool = False) -> Param:
    return Param(name=name, kind=kind, required=required, nullable=nullable)


def build_general_tools(
    sandbox: WorkspaceSandbox,
    fetcher: UrlFetcher,
    *,
    fetch_guard: Callable[[str], None] | None = None,
    command_runner: CommandRunner | None = None,
) -> list[ToolSpec]:
    """Bind the sandbox, fetcher and command runner to their tool specs.

    ``fetch_guard`` is an optional host hook that sees every URL before the
    network is touched (an allowlist the operator set for a run, say). It
    raises to refuse; it never rewrites the URL.
    """
    runner = command_runner or CommandRunner(sandbox)

    def ls_workspace(_: dict[str, Any]) -> dict[str, Any]:
        entries = sandbox.ls()
        return {"entries": entries, "count": len(entries)}

    def read_file(arguments: dict[str, Any]) -> dict[str, Any]:
        """Read one file, optionally a 1-based line range.

        分段读是为大文件准备的：返回的 content/sha256/bytes 都是**这个切片**
        自身（读到的就是被哈希的），另附 file_sha256 与行号范围——模型不用
        再写内联脚本去数行（真实踩过：模型为看一段代码用 python3 -c，被
        命令策略反复拒绝）。
        """
        record = sandbox.read(arguments["path"])
        offset = arguments.get("offset")
        limit = arguments.get("limit")
        if offset is None and limit is None:
            return record
        try:
            start = int(offset) if offset is not None else 1
            count = int(limit) if limit is not None else 200
        except (TypeError, ValueError):
            raise AgentError("READ_RANGE_INVALID", "offset/limit must be integers")
        if start < 1:
            raise AgentError("READ_RANGE_INVALID", "offset is 1-based and must be >= 1")
        if count < 1 or count > 400:
            raise AgentError("READ_RANGE_INVALID", "limit must be between 1 and 400")
        lines = str(record.get("content") or "").splitlines()
        total = len(lines)
        sl = lines[start - 1:start - 1 + count]
        from ...core.hashing import digest_bytes  # noqa: PLC0415 — 局部导入避免环

        slice_text = "\n".join(sl)
        return {
            "path": record["path"],
            "content": slice_text,
            "sha256": digest_bytes(slice_text.encode("utf-8")),
            "bytes": len(slice_text.encode("utf-8")),
            "file_sha256": record.get("sha256"),
            "start_line": start,
            "end_line": start - 1 + len(sl),
            "total_lines": total,
            "range": True,
        }

    def grep_workspace(arguments: dict[str, Any]) -> dict[str, Any]:
        """Regex content search across the sandbox, jailed and capped.

        Reads every file through ``sandbox.read`` (the same jail as
        ``read_file``), skips binaries and oversized files, and caps matches
        so one hot pattern cannot flood the context or the receipts.
        """
        import fnmatch
        import re as _re

        raw_pattern = str(arguments["pattern"])
        if len(raw_pattern) > 200:
            raise AgentError("GREP_PATTERN_TOO_LONG", "pattern must be at most 200 chars")
        try:
            matcher = _re.compile(raw_pattern)
        except _re.error as error:
            raise AgentError("GREP_PATTERN_INVALID", f"invalid regex: {error}") from error
        glob_filter = str(arguments.get("glob") or "").strip()
        max_results = min(int(arguments.get("max_results") or 80), 200)

        matches: list[dict[str, Any]] = []
        searched = 0
        skipped_binary = 0
        truncated = False
        for entry in sandbox.ls():
            relative = entry["path"]
            if entry["bytes"] > 512 * 1024:
                skipped_binary += 1
                continue
            if glob_filter and not fnmatch.fnmatch(relative, glob_filter):
                continue
            record = sandbox.read(relative)
            text = record["content"]
            if "\x00" in text[:8192]:
                skipped_binary += 1
                continue
            searched += 1
            file_matches = 0
            for line_number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    matches.append({
                        "path": relative,
                        "line": line_number,
                        "text": line.strip()[:240],
                    })
                    file_matches += 1
                    if len(matches) >= max_results:
                        truncated = True
                        break
                if file_matches >= 20:
                    break  # per-file cap: one hot file cannot eat the budget
            if truncated:
                break
        return {
            "matches": matches,
            "count": len(matches),
            "searched_files": searched,
            "skipped_binary": skipped_binary,
            "truncated": truncated,
        }

    def find_files(arguments: dict[str, Any]) -> dict[str, Any]:
        """fnmatch file listing over the jailed inventory."""
        import fnmatch

        pattern = str(arguments["pattern"] or "").strip()
        if not pattern:
            raise AgentError("FIND_PATTERN_REQUIRED", "pattern is required (e.g. *.md or src/*.py)")
        matches = []
        for entry in sandbox.ls():
            relative = entry["path"]
            if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(relative.split("/")[-1], pattern):
                matches.append({"path": relative, "bytes": entry["bytes"]})
            if len(matches) >= 200:
                break
        return {"paths": matches, "count": len(matches), "truncated": len(matches) >= 200}

    def git_status(_: dict[str, Any]) -> dict[str, Any]:
        return sandbox.status()

    def write_file(arguments: dict[str, Any]) -> dict[str, Any]:
        record = sandbox.write(arguments["path"], arguments["content"])
        record["computed"] = True
        return record

    def edit_file(arguments: dict[str, Any]) -> dict[str, Any]:
        record = sandbox.edit(
            arguments["path"],
            arguments["old_text"],
            arguments.get("new_text") or "",
        )
        record["computed"] = True
        return record

    def run_command(arguments: dict[str, Any]) -> dict[str, Any]:
        return runner.run(
            arguments["command"],
            list(arguments.get("args") or []),
            timeout_seconds=arguments.get("timeout_seconds"),
        )

    _terminal_shell: list = []  # 惰性创建,闭包持有

    def terminal(arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a command in a persistent bash session.

        First call spawns the shell; subsequent calls reuse it —
        cd, export, venv activation persist across calls. The session
        dies with the run (daemon child of the server process).
        """
        from .terminal import PersistentShell

        if not _terminal_shell:
            _terminal_shell.append(PersistentShell(sandbox.root))
        return _terminal_shell[0].execute(
            str(arguments["command"]),
            timeout=float(arguments.get("timeout_seconds") or 60),
        )

    def fetch_url(arguments: dict[str, Any]) -> dict[str, Any]:
        url = arguments["url"].strip()
        if fetch_guard is not None:
            fetch_guard(url)
        result = fetcher.fetch(url)
        if not result["text"].strip():
            raise AgentError("FETCH_EMPTY", f"{result['host']!r} returned no readable text")
        return result

    def report_blocked(arguments: dict[str, Any]) -> dict[str, Any]:
        """Declare that the work cannot proceed. No world effect, no authority.

        The handler acknowledges and returns the reason; whether the run stops
        is decided by the loop, which reads this observation and finalizes the
        task as failed. A handler that could stop the run would be a tool the
        model points at a task, not a declaration to the host.
        """
        return {"acknowledged": True, "reason": str(arguments["reason"]).strip()}

    tools = [
        ToolSpec(
            name="ls_workspace",
            summarize=summaries.summarize_ls_workspace,
            summary="List files in this run's workspace sandbox with content hashes",
            effect=EFFECT_READ,
            params=(),
            handler=ls_workspace,
            outputs=(_out("entries", "array"),),
            version="1.0",
        ),
        ToolSpec(
            name="read_file",
            summarize=summaries.summarize_read_file,
            summary="Read one file from the workspace sandbox; paths are jailed and escapes refused",
            effect=EFFECT_READ,
            params=(
                Param(
                    name="path",
                    kind="string",
                    required=True,
                    description="path relative to the workspace root (absolute and .. are refused)",
                ),
                Param(
                    name="offset",
                    kind="integer",
                    required=False,
                    minimum=1,
                    description="optional 1-based start line for a ranged read of a large file",
                ),
                Param(
                    name="limit",
                    kind="integer",
                    required=False,
                    minimum=1, maximum=400,
                    description="optional max lines for a ranged read (1-400)",
                ),
            ),
            handler=read_file,
            outputs=(
                _out("path", "string"),
                _out("content", "string"),
                _out("sha256", "string"),
                _out("bytes", "integer"),
                _out("file_sha256", "string", required=False, nullable=True),
                _out("start_line", "integer", required=False, nullable=True),
                _out("end_line", "integer", required=False, nullable=True),
                _out("total_lines", "integer", required=False, nullable=True),
            ),
            version="1.1",
        ),
        ToolSpec(
            name="grep_workspace",
            summarize=summaries.summarize_grep_workspace,
            summary="Regex content search across the workspace sandbox; jailed, binaries skipped, results capped",
            effect=EFFECT_READ,
            params=(
                Param(
                    name="pattern",
                    kind="string",
                    required=True,
                    description="Python regular expression to search for",
                ),
                Param(
                    name="glob",
                    kind="string",
                    required=False,
                    description="optional file filter, e.g. *.py or src/*.md",
                ),
                Param(
                    name="max_results",
                    kind="integer",
                    required=False,
                    description="cap on returned matches (default 80, hard max 200)",
                ),
            ),
            handler=grep_workspace,
            outputs=(
                _out("matches", "array"),
                _out("count", "integer"),
                _out("searched_files", "integer"),
                _out("truncated", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="find_files",
            summarize=summaries.summarize_find_files,
            summary="List workspace files matching a glob pattern (e.g. *.md, src/*.py)",
            effect=EFFECT_READ,
            params=(
                Param(
                    name="pattern",
                    kind="string",
                    required=True,
                    description="glob pattern matched against workspace-relative paths and basenames",
                ),
            ),
            handler=find_files,
            outputs=(
                _out("paths", "array"),
                _out("count", "integer"),
                _out("truncated", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="git_status",
            summarize=summaries.summarize_git_status,
            summary="Read-only git query of the workspace sandbox: branch, pending changes, recent commits",
            effect=EFFECT_READ,
            params=(),
            handler=git_status,
            outputs=(
                _out("branch", "string"),
                _out("change_count", "integer"),
                _out("commit_count", "integer"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="write_file",
            summarize=summaries.summarize_write_file,
            summary="Write one file into the workspace sandbox; approval-gated, hashed and git-committed",
            effect=EFFECT_WRITE,
            params=(
                Param(
                    name="path",
                    kind="string",
                    required=True,
                    description="path relative to the workspace root (absolute and .. are refused)",
                ),
                Param(
                    name="content",
                    identity=False,
                    kind="string",
                    required=True,
                    max_length=MAX_WRITE_CHARS,
                    description=f"full file content, at most {MAX_WRITE_CHARS} characters",
                ),
            ),
            handler=write_file,
            outputs=(
                _out("path", "string"),
                _out("bytes", "integer"),
                _out("sha256_before", "string", required=False, nullable=True),
                _out("sha256_after", "string"),
                _out("commit", "string", required=False, nullable=True),
                _out("changed", "boolean"),
                _out("diff", "string"),
                _out("truncated", "boolean"),
                _out("computed", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="edit_file",
            summarize=summaries.summarize_edit_file,
            summary="Edit one file by replacing a unique anchored block; approval-gated with a real diff",
            effect=EFFECT_WRITE,
            params=(
                Param(
                    name="path",
                    kind="string",
                    required=True,
                    description="existing file inside the workspace sandbox (edits never create files)",
                ),
                Param(
                    name="old_text",
                    identity=False,
                    kind="string",
                    required=True,
                    max_length=MAX_WRITE_CHARS,
                    description="the exact block to replace; it must match exactly once",
                ),
                Param(
                    name="new_text",
                    identity=False,
                    kind="string",
                    required=False,
                    max_length=MAX_WRITE_CHARS,
                    description="the replacement block (omit or empty to delete the anchor)",
                ),
            ),
            handler=edit_file,
            outputs=(
                _out("path", "string"),
                _out("bytes", "integer"),
                _out("sha256_before", "string", required=False, nullable=True),
                _out("sha256_after", "string"),
                _out("commit", "string", required=False, nullable=True),
                _out("changed", "boolean"),
                _out("lines_added", "integer"),
                _out("lines_removed", "integer"),
                _out("diff", "string"),
                _out("truncated", "boolean"),
                _out("computed", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="run_command",
            summarize=summaries.summarize_run_command,
            summary="Run one whitelisted command in the workspace (no shell); timeouts are unknown outcomes",
            effect=EFFECT_WRITE,
            params=(
                Param(
                    name="command",
                    kind="string",
                    required=True,
                    description="program name from the host whitelist (python/python3/node/git/ls/cat/grep/find/wc/diff/pytest/unittest)",
                ),
                Param(
                    name="args",
                    kind="array",
                    required=False,
                    maximum=32,
                    description="arguments as a plain list; shell syntax (; | && > etc.) is refused",
                ),
                Param(
                    name="timeout_seconds",
                    kind="integer",
                    required=False,
                    minimum=1,
                    maximum=MAX_TIMEOUT_SECONDS,
                    description=f"deadline in seconds (default 30, at most {MAX_TIMEOUT_SECONDS})",
                ),
            ),
            handler=run_command,
            outputs=(
                _out("command", "string"),
                _out("exit_code", "integer"),
                _out("duration_ms", "integer"),
                _out("output", "string"),
                _out("bytes", "integer"),
                _out("truncated", "boolean"),
                _out("readonly", "boolean"),
                _out("computed", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="terminal",
            summarize=summaries.summarize_terminal,
            summary="Execute a command in a persistent bash session; cd/export/pipes persist across calls",
            effect=EFFECT_WRITE,
            params=(
                Param(
                    name="command",
                    kind="string",
                    required=True,
                    description="shell command to execute in the persistent session",
                ),
                Param(
                    name="timeout_seconds",
                    kind="integer",
                    required=False,
                    description="per-command timeout in seconds (default 60)",
                ),
            ),
            handler=terminal,
            outputs=(
                _out("output", "string"),
                _out("exit_code", "integer"),
                _out("call_count", "integer"),
                _out("truncated", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="fetch_url",
            summarize=summaries.summarize_fetch_url,
            summary="Fetch one http(s) URL as text; SSRF-guarded, size-capped, content marked not-verified",
            effect=EFFECT_NETWORK,
            params=(
                Param(
                    name="url",
                    kind="string",
                    required=True,
                    description="absolute http/https URL; private and loopback destinations are refused",
                ),
            ),
            handler=fetch_url,
            outputs=(
                _out("url", "string"),
                _out("host", "string"),
                _out("status", "integer"),
                _out("content_type", "string"),
                _out("sha256", "string"),
                _out("bytes", "integer"),
                _out("title", "string", required=False),
                _out("text", "string"),
                _out("content_truth", "string"),
                _out("retrieved_at", "string"),
                _out("latency_ms", "integer"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="report_blocked",
            summarize=summaries.summarize_report_blocked,
            summary="Declare why the work cannot proceed; the runtime decides whether to stop",
            effect=EFFECT_READ,
            params=(
                Param(
                    name="reason",
                    kind="string",
                    required=True,
                    max_length=MAX_BLOCK_REASON_CHARS,
                    description=(
                        "what is missing or impossible, in one bounded paragraph; "
                        "the runtime records it verbatim as the stop reason"
                    ),
                ),
            ),
            handler=report_blocked,
            outputs=(
                _out("acknowledged", "boolean"),
                _out("reason", "string"),
            ),
            version="1.0",
        ),
    ]
    return tools


#: Registration order, pinned by tests the way finance pins its catalogue.
#: 2026-11 工具广度批次：新增 grep_workspace / find_files / git_status
#: 三个只读工具（搜索与状态查询），注册顺序随之扩展。
GENERAL_TOOLS: tuple[str, ...] = (
    "ls_workspace", "read_file", "grep_workspace", "find_files", "git_status",
    "write_file", "edit_file", "run_command", "terminal", "fetch_url",
    "report_blocked",
)

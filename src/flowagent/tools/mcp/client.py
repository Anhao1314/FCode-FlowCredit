"""A stdlib MCP stdio client: JSON-RPC 2.0 over the child's stdin/stdout.

The MCP wire format is line-delimited JSON-RPC, which is small enough that a
dependency-free implementation is *more* auditable than a client library:
every byte that crosses the process boundary is visible in this file. A
reader thread moves server output into a queue; each request waits for its
own id, so server-initiated notifications can never be mistaken for replies.

Failure semantics follow the runtime's discipline:

* a server that cannot start or complete the handshake is a *confirmed*
  unavailability (nothing was dispatched on its behalf);
* a call that times out or loses the process mid-flight was dispatched and
  never answered — ``outcome: unknown``, never retried here;
* a call that returns ``isError`` is a *confirmed* tool failure: the server
  answered and said no.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError
from .config import McpServerConfig, resolve_env_value

__all__ = ["McpStdioClient", "PROTOCOL_VERSION"]

PROTOCOL_VERSION = "2024-11-05"


class McpStdioClient:
    """One launched MCP server process and its request/response queue."""

    def __init__(self, config: McpServerConfig, *, cwd: Path | None = None) -> None:
        self.config = config
        self._cwd = cwd
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._drain: threading.Thread | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- lifetime

    def start(self, *, timeout: float | None = None) -> None:
        """Launch the process and complete the MCP handshake."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            environment = {**os.environ, **{
                key: resolve_env_value(value) for key, value in self.config.env.items()
            }}
            launch_cwd = str(self.config.cwd or self._cwd or Path.cwd())
            try:
                self._process = subprocess.Popen(
                    [self.config.command, *self.config.args],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    cwd=launch_cwd,
                    env=environment,
                )
            except OSError as error:
                raise AgentError(
                    "MCP_CONNECT_FAILED",
                    f"cannot launch server {self.config.name!r}: {error}",
                ) from error
            self._messages = queue.Queue()
            self._reader = threading.Thread(
                target=self._read_stdout, name=f"mcp:{self.config.name}:out", daemon=True,
            )
            self._drain = threading.Thread(
                target=self._read_stderr, name=f"mcp:{self.config.name}:err", daemon=True,
            )
            self._reader.start()
            self._drain.start()

        deadline = timeout if timeout is not None else self.config.timeout
        try:
            reply = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "fcode-runtime", "version": "1"},
            }, timeout=deadline)
            self._notify("notifications/initialized", {})
        except Exception:
            # The bridge only retains clients after start() succeeds. A failed
            # handshake must therefore clean up its own partially launched
            # process rather than relying on a later disconnect().
            self.stop()
            raise
        self._server_info = reply.get("serverInfo") or {}

    def stop(self) -> None:
        """Stop and reap the child, close all pipes and join both readers.

        Detach the references first so concurrent liveness checks immediately
        report unavailable and repeated stop() calls are harmless. Even an
        already-dead child still owns three parent-side pipe handles and must
        be waited for; process death is not a reason to skip cleanup.
        """
        with self._lock:
            process = self._process
            reader = self._reader
            drain = self._drain
            self._process = None
            self._reader = None
            self._drain = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:  # pragma: no cover - exited between poll and kill
                pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
                pass
        except OSError:  # pragma: no cover - already reaped by a racing owner
            pass
        finally:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:  # pragma: no cover - shutdown is best effort
                        pass
        current = threading.current_thread()
        for thread in (reader, drain):
            if thread is not None and thread is not current:
                thread.join(timeout=5)

    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    # ------------------------------------------------------------------ tools

    def list_tools(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        reply = self._request("tools/list", {}, timeout=timeout or self.config.timeout)
        tools = reply.get("tools")
        if not isinstance(tools, list):
            raise AgentError("MCP_BAD_RESPONSE", f"server {self.config.name!r} returned no tool list")
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            timeout=self.config.timeout,
        )
        return result if isinstance(result, dict) else {"content": []}

    # ---------------------------------------------------------------- private

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # a malformed line is dropped, never fatal to the queue
                if isinstance(message, dict):
                    self._messages.put(message)
        except (OSError, ValueError):  # the pipe closed: the reader exits
            return

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for _line in process.stderr:
                pass  # drained to avoid a full pipe blocking the server
        except (OSError, ValueError):  # pragma: no cover
            return

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise AgentError(
                    "MCP_SERVER_UNAVAILABLE",
                    f"server {self.config.name!r} is not running",
                )
            self._next_id += 1
            request_id = self._next_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise AgentError(
                    "MCP_SERVER_LOST",
                    f"server {self.config.name!r} died while sending {method!r}; "
                    "the call was dispatched, outcome unknown",
                    details={"outcome": "unknown"},
                ) from error

        # The wait happens outside the lock: a slow call must not block
        # liveness checks or shutdown.
        try:
            message = self._messages.get(timeout=max(0.5, timeout))
        except queue.Empty as error:
            raise AgentError(
                "MCP_TIMEOUT",
                f"server {self.config.name!r} did not answer {method!r} within "
                f"{timeout:.0f}s; the call was dispatched, outcome unknown, not retried",
                details={"outcome": "unknown"},
            ) from error
        if message.get("id") != request_id:
            # A stale or reordered reply; wait for the matching one.
            return self._wait_for(request_id, timeout, first=message)
        return self._unwrap(method, message)

    def _wait_for(self, request_id: int, timeout: float, first: dict[str, Any]) -> dict[str, Any]:
        deadline = timeout
        pending = [first]
        while pending:
            message = pending.pop(0)
            if message.get("id") == request_id:
                return self._unwrap("request", message)
        try:
            remaining = max(0.5, deadline)
            while True:
                message = self._messages.get(timeout=remaining)
                if message.get("id") == request_id:
                    return self._unwrap("request", message)
        except queue.Empty as error:
            raise AgentError(
                "MCP_TIMEOUT",
                f"server {self.config.name!r} never answered request {request_id}; "
                "outcome unknown, not retried",
                details={"outcome": "unknown"},
            ) from error

    def _unwrap(self, method: str, message: dict[str, Any]) -> dict[str, Any]:
        if "error" in message and message["error"] is not None:
            error = message["error"]
            raise AgentError(
                "MCP_PROTOCOL_ERROR",
                f"server {self.config.name!r} refused {method!r}: "
                f"{(error or {}).get('message', error)}",
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise AgentError(
                "MCP_BAD_RESPONSE",
                f"server {self.config.name!r} returned a non-object result for {method!r}",
            )
        return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):  # pragma: no cover - death races the notify
            pass

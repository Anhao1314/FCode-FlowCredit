"""A flat-schema MCP wrapper around the REA reader.

FCode's MCP bridge deliberately refuses input schemas it cannot validate:
nested objects, unions and arrays of objects are rejected rather than
faked. REA's native tool schemas use those shapes, so most of its catalog
cannot be imported directly.

This wrapper exposes the four artifact reads FCode actually needs with a
single ``path`` string each. It is still a real MCP server (line-delimited
JSON-RPC 2.0 over stdio), so the host's existing bridge, timeout and
approval path apply unchanged. The wrapper never executes the target.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, TextIO

from flowagent.core.contracts import AgentError

from .client import run_evidence, version
from .facts import (
    extract_desktop_application,
    extract_go_binary,
    extract_javascript_application,
)
from .macho import analyze_library_validation

__all__ = ["TOOLS", "handle_call", "main", "serve"]

_PROTOCOL_VERSION = "2024-11-05"
_DESCRIPTION = "REA artifact reader wrapper for FCode; read-only target inspection."

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "inspect_go_binary",
        "description": "Read Go build information, module graph and dependency rows from a Mach-O/ELF/PE Go binary.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "description": "Absolute path to the binary."}},
            "required": ["path"],
        },
    },
    {
        "name": "inspect_desktop_application",
        "description": "Inventory a desktop app bundle: selected files, sidecars, components and coverage.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "description": "Absolute path to the app bundle."}},
            "required": ["path"],
        },
    },
    {
        "name": "analyze_javascript_application",
        "description": "Read JavaScript application statistics, routes and graph counts without executing it.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "description": "Absolute path to a JS tree or ASAR."}},
            "required": ["path"],
        },
    },
    {
        "name": "inspect_library_validation",
        "description": "Read Mach-O load commands and Team IDs to test whether disable-library-validation has a static load-path justification.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"path": {"type": "string", "description": "Absolute path to an executable or app bundle."}},
            "required": ["path"],
        },
    },
)


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _resolve_executable(path: Path) -> tuple[Path, Path]:
    """Return (app_root, executable) for a bundle or a single executable."""
    if path.is_dir():
        info_path = path / "Contents" / "Info.plist"
        if not info_path.is_file():
            raise AgentError("REA_TARGET_INVALID", f"not an app bundle: {path}")
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
        name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
        candidate = path / "Contents" / "MacOS" / str(name or "")
        if not candidate.is_file():
            raise AgentError("REA_TARGET_INVALID", f"bundle executable not found: {candidate}")
        return path, candidate
    if path.is_file():
        return path.parent, path
    raise AgentError("REA_TARGET_INVALID", f"target does not exist: {path}")


def _entitlements(executable: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["codesign", "-d", "--entitlements", ":-", str(executable)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    payload = (completed.stdout or "").strip()
    if completed.returncode != 0 or not payload:
        return []
    try:
        values = plistlib.loads(payload.encode("utf-8"))
    except plistlib.InvalidFileException:
        return []
    if not isinstance(values, dict):
        return []
    return sorted(str(key) for key, enabled in values.items() if enabled is True)


def handle_call(name: str, arguments: dict[str, Any], *, binary: str | None = None) -> dict[str, Any]:
    """Execute one flat tool call and return an MCP tool result."""
    path_value = arguments.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        return _result("ERROR: path must be a non-empty string", is_error=True)
    path = Path(path_value).expanduser().resolve()
    try:
        if name == "inspect_go_binary":
            evidence = run_evidence("inspect_go_binary", path, binary=binary, timeout=300)
            payload = {
                "evidence_id": evidence.evidence_id,
                "facts": extract_go_binary(evidence.evidence),
            }
        elif name == "inspect_desktop_application":
            evidence = run_evidence("inspect_desktop_application", path, binary=binary, timeout=300)
            payload = {
                "evidence_id": evidence.evidence_id,
                "facts": extract_desktop_application(evidence.evidence),
            }
        elif name == "analyze_javascript_application":
            evidence = run_evidence("analyze_javascript_application", path, binary=binary, timeout=300)
            payload = {
                "evidence_id": evidence.evidence_id,
                "facts": extract_javascript_application(evidence.evidence),
            }
        elif name == "inspect_library_validation":
            app_root, executable = _resolve_executable(path)
            payload = analyze_library_validation(app_root, executable, _entitlements(executable))
        else:
            return _result(f"ERROR: unknown tool {name!r}", is_error=True)
    except AgentError as error:
        return _result(f"ERROR {error.code}: {error.message}", is_error=True)
    except (OSError, ValueError) as error:
        return _result(f"ERROR: {error}", is_error=True)
    return _result(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _write(stdout: TextIO, message: dict[str, Any]) -> None:
    stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, *,
          binary: str | None = None) -> int:
    """Line-delimited MCP JSON-RPC loop; stdout carries protocol only."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method is None and request_id is None:
            continue
        if method == "initialize":
            _write(stdout, {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "rea-wrapper", "version": version(binary)},
                    "instructions": _DESCRIPTION,
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _write(stdout, {
                "jsonrpc": "2.0", "id": request_id,
                "result": {"tools": list(TOOLS)},
            })
        elif method == "tools/call":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            name = params.get("name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if not isinstance(name, str):
                _write(stdout, {
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "tools/call requires a tool name"},
                })
                continue
            _write(stdout, {
                "jsonrpc": "2.0", "id": request_id,
                "result": handle_call(name, arguments, binary=binary),
            })
        elif method == "ping":
            _write(stdout, {"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif request_id is not None:
            _write(stdout, {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"},
            })
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flowaudit.rea.mcp")
    parser.add_argument("--rea-bin", default=None,
                        help="REA CLI path (default: REA_BIN env or 'rea')")
    args = parser.parse_args(list(argv) if argv is not None else None)
    return serve(binary=args.rea_bin)


if __name__ == "__main__":
    raise SystemExit(main())
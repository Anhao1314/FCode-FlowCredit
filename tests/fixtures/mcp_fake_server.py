#!/usr/bin/env python3
"""A deterministic MCP stdio server for offline bridge tests.

Speaks newline-delimited JSON-RPC 2.0 exactly as the bridge client expects:
``initialize``, ``tools/list`` and ``tools/call``. Everything is in-process
and offline; ``die`` exits without answering so the client observes an
unknown outcome, and ``slow`` sleeps past any test timeout.
"""

from __future__ import annotations

import json
import sys
import time


def _tools() -> list[dict]:
    return [
        {
            "name": "echo",
            "description": "Echo text back",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string", "description": "the text"}},
            },
        },
        {
            "name": "add",
            "description": "Add two integers",
            "inputSchema": {
                "type": "object",
                "required": ["a", "b"],
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            },
        },
        {
            "name": "blob",
            "description": "A tool whose schema the gate cannot validate",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
            },
        },
        {
            "name": "slow",
            "description": "Sleeps then answers",
            "inputSchema": {
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
            },
        },
        {
            "name": "fail",
            "description": "Answers with isError set",
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        },
        {
            "name": "die",
            "description": "Exits the process without answering",
            "inputSchema": {"type": "object"},
        },
    ]


def _call(name: str, arguments: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": f"echo: {arguments.get('text', '')}"}]}
    if name == "add":
        return {"content": [{"type": "text", "text": str(int(arguments.get("a", 0)) + int(arguments.get("b", 0)))}]}
    if name == "slow":
        time.sleep(float(arguments.get("seconds", 2)))
        return {"content": [{"type": "text", "text": "woke"}]}
    if name == "fail":
        return {"isError": True, "content": [{"type": "text", "text": str(arguments.get("message", "boom"))}]}
    if name == "die":
        # No reply, no flush: the caller must treat this as an unknown outcome.
        sys.exit(1)
    return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = _call(str(params.get("name") or ""), params.get("arguments") or {})
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

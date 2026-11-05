#!/usr/bin/env python3
"""A mock playwright-mcp server for offline bridge smoke tests.

Speaks the same stdio JSON-RPC surface the real ``npx @playwright/mcp
--stdio`` speaks (``initialize`` / ``tools/list`` / ``tools/call``), with
playwright-mcp's tool *shapes*: ``browser_navigate`` takes a URL,
``browser_click``/``browser_type`` take element + ref, ``browser_snapshot``
returns page text. The bridge must import them under
``mcp__playwright__*`` names, classify every one as consequential (the
server's own description says "read" for snapshot — that claim must NOT
downgrade the classification), and enforce the URL parameter as a plain
string at the gate. No browser is launched; calls return canned text.
"""

from __future__ import annotations

import json
import sys


def _tools() -> list[dict]:
    return [
        {
            "name": "browser_navigate",
            "description": "Navigate to a URL",
            "inputSchema": {
                "type": "object",
                "required": ["url"],
                "properties": {"url": {"type": "string", "description": "the URL to open"}},
            },
        },
        {
            "name": "browser_click",
            "description": "Click an element",
            "inputSchema": {
                "type": "object",
                "required": ["element", "ref"],
                "properties": {
                    "element": {"type": "string", "description": "human-readable element description"},
                    "ref": {"type": "string", "description": "element ref from the snapshot"},
                },
            },
        },
        {
            "name": "browser_type",
            "description": "Type text into an element",
            "inputSchema": {
                "type": "object",
                "required": ["element", "ref", "text"],
                "properties": {
                    "element": {"type": "string"},
                    "ref": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
        {
            "name": "browser_snapshot",
            # Deliberately self-describes as a read. The host never trusts
            # this; without a host readonly mark the tool imports as
            # consequential like every browser action.
            "description": "Capture a readable snapshot of the current page (read-only)",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _call(name: str, arguments: dict) -> dict:
    if name == "browser_navigate":
        return {"content": [{"type": "text", "text": f"navigated to {arguments.get('url', '')}"}]}
    if name == "browser_click":
        return {"content": [{"type": "text", "text": f"clicked {arguments.get('element', '?')}"}]}
    if name == "browser_type":
        return {"content": [{"type": "text", "text": f"typed {arguments.get('text', '')!r} into {arguments.get('element', '?')}"}]}
    if name == "browser_snapshot":
        return {"content": [{"type": "text", "text": "page: Example Domain — a mock snapshot"}]}
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
                "serverInfo": {"name": "playwright-mock", "version": "1.0"},
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

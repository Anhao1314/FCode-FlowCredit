#!/usr/bin/env python3
"""Bare-API transport diagnosis for the DeepSeek large-output disconnect.

This bypasses the FCode harness on purpose: one HTTP layer (``http.client``),
the same endpoint/key resolution the console uses, and no retry anywhere. It
answers exactly one question — where does the wire break when a single
completion gets large?

Experiment matrix (each cell records status/headers/TTFB/total/finish/usage):

* E1  plain text, non-streaming, sizes 1k/3k/6k/10k/20k chars
* E2  plain text, streaming, same sizes
* E3  native tool call ``emit(text)``, non-streaming, 10k/20k chars
* E4  native tool call, streaming, 10k/20k chars
* E5  forced truncation: 20k request with max_tokens=2000 (non-streaming)
* E7  JSON mode, non-streaming, 10k chars (channel comparison)

The API key is read from the resolved provider env and never printed, logged
or written into the report. The report lands in ``work/diagnose/`` as JSON.

Usage::

    python3 scripts/diagnose_deepseek_transport.py [--only E1,E2] [--sizes 1000,3000]
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.providers.catalog import resolved_provider_env  # noqa: E402

REPORT_DIR = ROOT / "work" / "diagnose"

EMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "emit",
        "description": "Emit exactly the requested text.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the exact text to emit"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
}


def _prompt_for(size: int, *, tool: bool) -> str:
    filler = "a" * size
    if tool:
        return (
            f"Call the emit function exactly once with text set to exactly "
            f"{size} lowercase 'a' characters. Do not write any prose."
        )
    return (
        f"Reply with exactly {size} lowercase 'a' characters and nothing else: "
        f"no explanation, no quotes, no markdown. Here is the pattern to "
        f"extend: {filler[:40]}..."
    )


def _summarize_error(error: BaseException) -> dict[str, Any]:
    chain: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 6:
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = getattr(current, "reason", None) or getattr(current, "__cause__", None)
    partial = getattr(error, "partial", None)
    return {
        "error_type": type(error).__name__,
        "error_chain": chain,
        "error_message": str(error)[:300],
        "partial_bytes": len(partial) if isinstance(partial, (bytes, bytearray)) else None,
    }


def run_call(
    *,
    base_url: str,
    api_key: str,
    model: str,
    body_extra: dict[str, Any],
    user: str,
    stream: bool,
    timeout: float,
) -> dict[str, Any]:
    """One raw call. No retry: a retry would hide the breakpoint we measure."""
    parsed = urlsplit(base_url)
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    context = ssl.create_default_context() if parsed.scheme == "https" else None
    if context is not None:
        connection = connection_cls(parsed.hostname, parsed.port, timeout=timeout, context=context)
    else:
        connection = connection_cls(parsed.hostname, parsed.port, timeout=timeout)
    path = (parsed.path.rstrip("/") or "") + "/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": user}],
        "temperature": 0,
        "stream": stream,
        **{key: value for key, value in body_extra.items() if value is not None},
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "Connection": "close",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    record: dict[str, Any] = {"stream": stream}
    started = time.monotonic()
    first_byte_at: float | None = None
    try:
        connection.request(
            "POST", path,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        record["http_status"] = response.status
        record["response_headers"] = {
            key.lower(): value for key, value in response.getheaders()
            if key.lower() in (
                "content-length", "transfer-encoding", "content-type",
                "via", "server", "x-request-id", "cf-ray", "connection",
            )
        }
        if response.status != 200:
            raw = response.read(2_000)
            first_byte_at = first_byte_at or time.monotonic()
            record["error_body"] = raw.decode("utf-8", errors="replace")[:500]
            return record
        if not stream:
            chunks: list[bytes] = []
            while True:
                chunk = response.read1(65_536) if hasattr(response, "read1") else response.read(65_536)
                if chunk and first_byte_at is None:
                    first_byte_at = time.monotonic()
                if not chunk:
                    break
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks).decode("utf-8"))
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            record.update({
                "finish_reason": choice.get("finish_reason"),
                "content_chars": len(message.get("content") or ""),
                "reasoning_chars": len(message.get("reasoning_content") or ""),
                "tool_calls": len(message.get("tool_calls") or []),
                "tool_argument_chars": sum(
                    len((call.get("function") or {}).get("arguments") or "")
                    for call in (message.get("tool_calls") or [])
                ),
                "usage": payload.get("usage"),
                "sse_deltas": None,
            })
            return record
        # Streaming: count deltas, accumulate arguments, watch for mid-stream death.
        deltas = 0
        content_chars = 0
        reasoning_chars = 0
        argument_chars = 0
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        for raw_line in response:
            if first_byte_at is None:
                first_byte_at = time.monotonic()
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                continue
            try:
                item = json.loads(data)
            except ValueError:
                continue
            deltas += 1
            if isinstance(item.get("usage"), dict):
                usage = item["usage"]
            for choice in item.get("choices") or []:
                delta = choice.get("delta") or {}
                content_chars += len(delta.get("content") or "")
                reasoning_chars += len(delta.get("reasoning_content") or "")
                for fragment in delta.get("tool_calls") or []:
                    argument_chars += len(((fragment.get("function") or {}).get("arguments")) or "")
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
        record.update({
            "finish_reason": finish_reason,
            "content_chars": content_chars,
            "reasoning_chars": reasoning_chars,
            "tool_argument_chars": argument_chars,
            "usage": usage,
            "sse_deltas": deltas,
        })
        return record
    except BaseException as error:  # noqa: BLE001 — the diagnosis *is* the error
        record.update(_summarize_error(error))
        return record
    finally:
        record["ttfb_seconds"] = (
            round(first_byte_at - started, 3) if first_byte_at is not None else None
        )
        record["total_seconds"] = round(time.monotonic() - started, 3)
        try:
            connection.close()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="comma-separated cell ids, e.g. E1,E2")
    parser.add_argument("--sizes", default="1000,3000,6000,10000,20000")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="client-side per-read timeout; must exceed the observed ~121s close")
    args = parser.parse_args()

    env = resolved_provider_env()
    base_url = (env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    api_key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    model = (env.get("DEEPSEEK_MODEL") or "deepseek-chat").strip()
    if not api_key:
        print("DEEPSEEK_API_KEY is not configured", file=sys.stderr)
        return 2

    sizes = [int(item) for item in args.sizes.split(",") if item.strip()]
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    # Match the production completion budget (provider_factory default 32768)
    # so the ladder measures the real failure envelope; E5 overrides to 2000.
    budget = 32768

    cells: list[tuple[str, dict[str, Any]]] = []
    for size in sizes:
        cells.append((f"E1-plain-nostream-{size}", dict(
            kwargs=dict(body_extra={"max_tokens": budget}, user=_prompt_for(size, tool=False), stream=False),
        )))
    for size in sizes:
        cells.append((f"E2-plain-stream-{size}", dict(
            kwargs=dict(body_extra={"max_tokens": budget}, user=_prompt_for(size, tool=False), stream=True),
        )))
    for size in (10_000, 20_000):
        cells.append((f"E3-tool-nostream-{size}", dict(
            kwargs=dict(body_extra={"max_tokens": budget, "tools": [EMIT_TOOL]},
                        user=_prompt_for(size, tool=True), stream=False),
        )))
        cells.append((f"E4-tool-stream-{size}", dict(
            kwargs=dict(body_extra={"max_tokens": budget, "tools": [EMIT_TOOL]},
                        user=_prompt_for(size, tool=True), stream=True),
        )))
    cells.append(("E5-truncate-nostream-20000", dict(
        kwargs=dict(body_extra={"max_tokens": 2000}, user=_prompt_for(20_000, tool=False), stream=False),
    )))
    cells.append(("E7-jsonmode-nostream-10000", dict(
        kwargs=dict(body_extra={"max_tokens": budget,
                                "response_format": {"type": "json_object"},
                                "tools": None},
                    user=(
                        "Reply with one JSON object {\"payload\": \"...\"} where payload is "
                        "exactly 10000 lowercase 'a' characters. JSON only, no prose."
                    ),
                    stream=False),
    )))

    report: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "client_timeout_seconds": args.timeout,
        "results": {},
    }
    for cell_id, cell in cells:
        if only and not any(cell_id.startswith(prefix) for prefix in only):
            continue
        print(f"[diagnose] {cell_id} ...", flush=True)
        record = run_call(
            base_url=base_url, api_key=api_key, model=model,
            timeout=args.timeout, **cell["kwargs"],
        )
        # Usage contains no secrets; strip nothing. Never include headers we
        # did not whitelist (Authorization never leaves run_call).
        report["results"][cell_id] = record
        status = record.get("http_status", "n/a")
        print(
            f"  -> status={status} ttfb={record.get('ttfb_seconds')}s "
            f"total={record.get('total_seconds')}s finish={record.get('finish_reason')} "
            f"content={record.get('content_chars')} args={record.get('tool_argument_chars')} "
            f"deltas={record.get('sse_deltas')} err={record.get('error_type')}",
            flush=True,
        )

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = REPORT_DIR / f"deepseek-transport-{stamp}.json"
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report["results"], sort_keys=True).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[diagnose] report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

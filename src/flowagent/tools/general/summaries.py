"""General memory summaries: per-tool compressors for the general toolset.

The general agent's observations are files, commands and fetches. What a
decision needs from a ``read_file`` is not the whole content but the fact that
it was read, how big it is and its hash — the content lives in the workspace
and can be re-read (a governed action) if actually needed. Long text is cut to
a head plus its true length, so the prompt never carries a file body twice.
"""

from __future__ import annotations

from typing import Any

_HEAD = 600


def _head(text: Any) -> dict[str, Any]:
    value = str(text or "")
    if len(value) <= _HEAD:
        return {"head": value}
    return {"head": value[:_HEAD], "truncated": True, "chars": len(value)}


def summarize_ls_workspace(result: dict[str, Any]) -> dict[str, Any]:
    entries = result.get("entries", [])
    return {
        "count": result.get("count", len(entries)),
        "files": [entry.get("path") for entry in entries[:20]],
        "truncated": len(entries) > 20,
    }


def summarize_read_file(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": result.get("path"),
        "bytes": result.get("bytes"),
        "sha256": result.get("sha256"),
    }
    out.update(_head(result.get("content")))
    return out


def summarize_write_file(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": result.get("path"),
        "bytes": result.get("bytes"),
        "changed": result.get("changed", True),
        "sha256_after": result.get("sha256_after"),
        "commit": result.get("commit"),
    }


def summarize_edit_file(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": result.get("path"),
        "changed": result.get("changed", True),
        "lines_added": result.get("lines_added"),
        "lines_removed": result.get("lines_removed"),
        "sha256_after": result.get("sha256_after"),
        "commit": result.get("commit"),
    }


def summarize_run_command(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "command": result.get("command"),
        "exit_code": result.get("exit_code"),
        "duration_ms": result.get("duration_ms"),
        "readonly": result.get("readonly"),
    }
    out.update(_head(result.get("output")))
    return out


def summarize_fetch_url(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": result.get("url"),
        "status": result.get("status"),
        "title": result.get("title"),
        "bytes": result.get("bytes"),
        "content_truth": result.get("content_truth"),
        "latency_ms": result.get("latency_ms"),
    }


def summarize_ask_operator(result: dict[str, Any]) -> dict[str, Any]:
    """The answer is steering input, not bulk: it survives compression whole.

    Everything else a tool returns gets trimmed to a head; an operator's answer
    is the one thing a later round must not lose — dropping it would make the
    agent ask the same question twice.
    """
    out: dict[str, Any] = {
        "asked": str(result.get("asked") or "")[:400],
        "answered": bool(result.get("answered")),
    }
    if result.get("answered"):
        out["answer"] = str(result.get("answer") or "")[:600]
    else:
        out["timed_out"] = bool(result.get("timed_out"))
        out["reason"] = str(result.get("reason") or "")[:200]
    return out


def summarize_report_blocked(result: dict[str, Any]) -> dict[str, Any]:
    """The stop reason is the whole point of the observation, so it stays.

    A later round (and the operator reading the trace) must be able to see *why*
    the run gave up without opening the raw payload. The reason is already
    bounded by the tool's argument schema.
    """
    return {
        "reason": str(result.get("reason") or ""),
        "acknowledged": bool(result.get("acknowledged")),
    }


GENERAL_SUMMARIZERS: dict[str, Any] = {
    "ask_operator": summarize_ask_operator,
    "ls_workspace": summarize_ls_workspace,
    "read_file": summarize_read_file,
    "write_file": summarize_write_file,
    "edit_file": summarize_edit_file,
    "run_command": summarize_run_command,
    "fetch_url": summarize_fetch_url,
    "report_blocked": summarize_report_blocked,
}

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


def summarize_repo_map(result: dict[str, Any]) -> dict[str, Any]:
    """The map the model sees: path + top symbols + import edges per file.

    Without this the observation keeps only file names, and the model pays a
    read round-trip for the structure it already asked for. Bounded: 40 files,
    8 symbols / 6 imports each, one line per file.
    """
    entries = result.get("files", [])
    lines: list[str] = []
    for entry in entries[:40]:
        parts = [str(entry.get("path") or "")]
        symbols = entry.get("symbols") or []
        if symbols:
            names = ", ".join(str(item).split(":")[0] for item in symbols[:8])
            parts.append("sym: " + names)
        imports = entry.get("imports") or []
        if imports:
            mods = ", ".join(str(item).split(":")[0] for item in imports[:6])
            parts.append("imp: " + mods)
        lines.append(" · ".join(parts))
    return {
        "count": result.get("count", len(entries)),
        "tree": lines,
        "truncated": bool(result.get("truncated")) or len(entries) > 40,
        "symbols": result.get("symbols", 0),
    }


def summarize_read_file(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": result.get("path"),
        "bytes": result.get("bytes"),
        "sha256": result.get("sha256"),
    }
    if result.get("unchanged"):
        out["unchanged"] = True
        out["note"] = result.get("note")
    out.update(_head(result.get("content")))
    return out


def summarize_read_file_raw(result: dict[str, Any]) -> dict[str, Any]:
    """Fast-mode code read: keep a usable slice, not just its first 600 chars.

    The default ``read_file`` summary deliberately drops the body (the file is
    re-readable), but a coding agent that only sees 600 chars per read can never
    see a function body and ends up re-reading the same slice forever. This
    variant keeps up to 3,500 chars in context; the workspace jail, hashes and
    caps are identical.
    """
    content = str(result.get("content") or "")
    cap = 3_500
    out: dict[str, Any] = {
        "path": result.get("path"),
        "bytes": result.get("bytes"),
        "sha256": result.get("sha256"),
        "chars": len(content),
        "truncated": len(content) > cap,
        "content": content[:cap],
    }
    if result.get("unchanged"):
        out["unchanged"] = True
        out["note"] = result.get("note")
    if result.get("start_line") is not None:
        out["start_line"] = result.get("start_line")
        out["end_line"] = result.get("end_line")
        out["total_lines"] = result.get("total_lines")
    return out


def summarize_search_codebase(result: dict[str, Any]) -> dict[str, Any]:
    """Retrieval hits the model keeps: path + line + a bounded snippet."""
    files = result.get("files", [])
    return {
        "count": result.get("count", len(files)),
        "indexed_files": result.get("indexed_files"),
        "indexed_chunks": result.get("indexed_chunks"),
        "truncated": result.get("truncated", False),
        "hits": [
            f"{item.get('path')}:{item.get('start_line')} · "
            + " ".join(str(item.get("snippet") or "").split())[:220]
            for item in files[:8]
        ],
    }


def summarize_browser_inspect(result: dict[str, Any]) -> dict[str, Any]:
    text = str(result.get("text") or "")
    shot = result.get("screenshot") or {}
    return {
        "available": bool(result.get("available")),
        "ok": bool(result.get("ok")),
        "url": result.get("url"),
        "title": result.get("title"),
        "text": text[:1200],
        "elements": len(result.get("elements") or []),
        "screenshot": shot.get("path") if isinstance(shot, dict) else None,
        "console_errors": len(result.get("console_errors") or []),
        "reason": result.get("reason"),
    }


def summarize_browser_act(result: dict[str, Any]) -> dict[str, Any]:
    before = result.get("before") or {}
    after = result.get("after") or {}
    return {
        "available": bool(result.get("available")),
        "ok": bool(result.get("ok")),
        "url": result.get("url"),
        "action": result.get("action"),
        "selector": result.get("selector"),
        "title": result.get("title"),
        "before": before.get("path") if isinstance(before, dict) else None,
        "after": after.get("path") if isinstance(after, dict) else None,
        "text": str(result.get("text") or "")[:800],
        "dom_diff": str(result.get("dom_diff") or "")[:600],
        "screenshot_changed": (result.get("diff") or {}).get("changed"),
        "reason": result.get("reason"),
    }


def summarize_grep_workspace(result: dict[str, Any]) -> dict[str, Any]:
    matches = result.get("matches", [])
    return {
        "count": result.get("count", len(matches)),
        "searched_files": result.get("searched_files"),
        "truncated": result.get("truncated", False),
        "head": [
            f"{m.get('path')}:{m.get('line')}: {m.get('text')}" for m in matches[:10]
        ],
    }


def summarize_find_files(result: dict[str, Any]) -> dict[str, Any]:
    paths = result.get("paths", [])
    return {
        "count": result.get("count", len(paths)),
        "truncated": result.get("truncated", False),
        "head": [p.get("path") for p in paths[:20]],
    }


def summarize_git_status(result: dict[str, Any]) -> dict[str, Any]:
    changes = result.get("changes", [])
    commits = result.get("commits", [])
    return {
        "branch": result.get("branch"),
        "change_count": result.get("change_count", len(changes)),
        "changes_head": [f"{c.get('status')} {c.get('path')}" for c in changes[:10]],
        "commit_count": result.get("commit_count", len(commits)),
        "last_commit": commits[0] if commits else None,
    }


def summarize_subagent_status(result: dict[str, Any]) -> dict[str, Any]:
    children = result.get("children", [])
    return {
        "count": result.get("count", len(children)),
        "children": [
            f"{c.get('run_id')}: {c.get('status')}" for c in children[:6]
        ],
    }


def summarize_subagent_read(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "child_run_id": result.get("child_run_id"),
        "path": result.get("path"),
        "bytes": result.get("bytes"),
        "sha256": result.get("sha256"),
    }
    out.update(_head(result.get("content")))
    return out


def summarize_spawn_subagents(result: dict[str, Any]) -> dict[str, Any]:
    children = result.get("children", [])
    return {
        "count": len(children),
        "timed_out": result.get("timed_out", False),
        "head": [
            f"{c.get('run_id')}: {c.get('status')} — {str(c.get('summary'))[:160]}"
            for c in children[:6]
        ],
    }


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


def summarize_terminal(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "exit_code": result.get("exit_code"),
        "call_count": result.get("call_count"),
        "truncated": result.get("truncated", False),
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
    "repo_map": summarize_repo_map,
    "read_file_raw": summarize_read_file_raw,
    "grep_workspace": summarize_grep_workspace,
    "find_files": summarize_find_files,
    "git_status": summarize_git_status,
    "read_file": summarize_read_file,
    "write_file": summarize_write_file,
    "edit_file": summarize_edit_file,
    "run_command": summarize_run_command,
    "fetch_url": summarize_fetch_url,
    "report_blocked": summarize_report_blocked,
    "spawn_subagents": summarize_spawn_subagents,
    "subagent_status": summarize_subagent_status,
    "subagent_read": summarize_subagent_read,
    "terminal": summarize_terminal,
}


def summarize_budget_extension(result: dict[str, Any]) -> dict[str, Any]:
    """预算延展是一笔账：追加量与生效后的上限都要留在观察里。"""
    limits = result.get("limits") or {}
    return {
        "granted": bool(result.get("granted")),
        "extra_calls": int(result.get("extra_calls") or 0),
        "extra_cost_microusd": int(result.get("extra_cost_microusd") or 0),
        "reason": str(result.get("reason") or "")[:300],
        "max_calls": limits.get("max_calls"),
        "max_cost_microusd": limits.get("max_cost_microusd"),
    }


def summarize_apply_child_changes(result: dict[str, Any]) -> dict[str, Any]:
    """应用结果按文件清单回传：落地的、冲突的、拒绝的都要在观察里。"""
    def _names(rows: Any) -> list[str]:
        out: list[str] = []
        for row in rows or []:
            if isinstance(row, str):
                out.append(row)
            elif isinstance(row, dict) and row.get("path"):
                out.append(str(row["path"]))
        return out[:20]

    return {
        "child_run_id": str(result.get("child_run_id") or ""),
        "applied": _names(result.get("applied")),
        "conflicts": _names(result.get("conflicts")),
        "refused": [row for row in (result.get("refused") or [])][:10],
        "deleted_reported": _names(result.get("deleted_reported")),
        "unchanged_count": len(result.get("unchanged") or []),
    }

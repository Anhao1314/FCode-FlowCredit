"""Operator-maintained governance policy files (the CLAUDE.md analogue).

The governance chat already carries a hard-coded charter. This module adds the
missing layer: a small, operator-editable policy that is concatenated into the
system prompt on every turn, with the same discipline as the rest of the
harness — deterministic, bounded, and hash-visible.

Load order is broadest first, nearest last:

* user    ``$FCODE_CONFIG_DIR/GOVERNANCE.md`` (or ``~/.fcode/GOVERNANCE.md``)
* project ``<repo>/GOVERNANCE.md``
* local   ``<repo>/GOVERNANCE.local.md`` (gitignored, machine-local)

``@path`` imports resolve relative to the file that names them, at most four
hops deep. Imports may only stay inside the project or user policy roots;
anything else is skipped with a visible note instead of silently reading an
arbitrary file. Block-level HTML comments are stripped before injection.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable

__all__ = ["load_policy", "MAX_POLICY_CHARS", "MAX_IMPORT_DEPTH"]

MAX_POLICY_CHARS = 40_000
MAX_IMPORT_DEPTH = 4
_IMPORT_RE = re.compile(r"(?<!`)@([^\s`]+)(?!`)")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _user_policy_dir() -> Path:
    configured = os.environ.get("FCODE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".fcode"


def _strip_comments(text: str) -> str:
    # Comments inside fenced code blocks are data, not annotations; protect
    # them before stripping and restore afterwards.
    blocks: list[str] = []

    def stash(match: re.Match[str]) -> str:
        blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(blocks) - 1}\x00"

    protected = re.sub(r"```.*?```", stash, text, flags=re.S)
    stripped = _COMMENT_RE.sub("", protected)
    for index, block in enumerate(blocks):
        stripped = stripped.replace(f"\x00CODEBLOCK{index}\x00", block)
    return stripped


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    resolved = path.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _read_file(path: Path, roots: list[Path], depth: int,
               files: list[dict[str, Any]], notes: list[str]) -> str:
    if depth > MAX_IMPORT_DEPTH:
        notes.append(f"{path}: 导入超过 {MAX_IMPORT_DEPTH} 跳，已跳过")
        return ""
    if not _inside(path, roots):
        notes.append(f"{path}: 导入越出制度目录，已跳过")
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        notes.append(f"{path}: 读取失败（{error.__class__.__name__}）")
        return ""
    files.append({
        "path": str(path),
        "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "chars": len(raw),
    })
    text = _strip_comments(raw)
    rendered: list[str] = []

    def replace_import(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        if not target or target.startswith("#"):
            return match.group(0)
        imported = (path.parent / target).expanduser()
        body = _read_file(imported, roots, depth + 1, files, notes)
        return body.strip()

    rendered.append(_IMPORT_RE.sub(replace_import, text))
    return "\n".join(rendered)


def load_policy(project_root: Path | str | None = None) -> dict[str, Any]:
    """Load and merge the governance policy hierarchy.

    Returns ``{"text", "files", "sha256", "chars", "notes", "truncated"}``.
    Missing files are normal and produce an empty text (nothing is injected).
    """
    project = Path(project_root).expanduser().resolve() if project_root else None
    user_dir = _user_policy_dir()
    roots = [user_dir]
    if project is not None:
        roots.insert(0, project)
    candidates: list[tuple[str, Path]] = [( "user", user_dir / "GOVERNANCE.md" )]
    if project is not None:
        candidates.append(("project", project / "GOVERNANCE.md"))
        candidates.append(("local", project / "GOVERNANCE.local.md"))

    files: list[dict[str, Any]] = []
    notes: list[str] = []
    parts: list[str] = []
    for scope, path in candidates:
        if not path.is_file():
            continue
        body = _read_file(path, roots, 0, files, notes)
        if body.strip():
            parts.append(f"### {scope}: {path.name}\n{body.strip()}")
    text = "\n\n".join(parts).strip()
    truncated = False
    if len(text) > MAX_POLICY_CHARS:
        text = text[:MAX_POLICY_CHARS] + "\n…（制度文件超过字符上限，已截断）"
        truncated = True
    return {
        "text": text,
        "files": files,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "chars": len(text),
        "notes": notes,
        "truncated": truncated,
    }

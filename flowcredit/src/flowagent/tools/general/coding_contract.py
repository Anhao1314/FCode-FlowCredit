"""Explicit coding contracts for Work: deliverables,禁区 and completion gates.

A coding objective is not a contract. This module turns an optional operator
declaration (``spec.codingContract``) into a small, fail-closed dict that the
runtime can enforce:

* deliverables must exist before the run may succeed;
* test edits are forbidden by default;
* dependency manifests are frozen when ``noNewDependencies`` is true;
* declared forbidden paths are checked as path patterns;
* definition-of-done text is prompt context, never a verdict.

There is deliberately no natural-language contract parsing. If the operator
did not declare it, the runtime does not pretend it was agreed.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Any

__all__ = [
    "normalize_coding_contract",
    "coding_path_violation",
    "coding_contract_instruction",
    "coding_contract_binding",
    "missing_coding_deliverables",
    "coding_contract_violations",
    "contract_path_state",
    "DEPENDENCY_MANIFEST_PATTERNS",
]

MAX_LIST_ITEMS = 40
MAX_TEXT_CHARS = 300

_TEST_MARKERS = ("tests", "test")
_TEST_PREFIXES = ("test_",)
_TEST_SUFFIXES = ("_test",)
_TEST_FILE_MARKERS = (".test.", ".spec.")
#: The default test surface as glob patterns — the same set used for the
#: prompt-facing forbidden list and the completion-time baseline hash, so a
#: name the tool layer denies is also a name the baseline watches.
_TEST_PATTERNS = (
    "tests/**", "test_*/**", "**/test_*",
    "**/*_test.*", "**/*.test.*", "**/*.spec.*",
)


def _clean_path(raw: Any, *, field: str, allow_glob: bool = True) -> str:
    if not isinstance(raw, str):
        raise ValueError(f"{field} entries must be strings")
    value = raw.strip().replace("\\", "/")
    if not value:
        raise ValueError(f"{field} entries must not be empty")
    if value.startswith("/") or value.startswith("~"):
        raise ValueError(f"{field} path must be workspace-relative: {raw[:120]!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{field} path must not be absolute or contain ..: {raw[:120]!r}")
    if "\x00" in value:
        raise ValueError(f"{field} path contains a forbidden character")
    if not allow_glob and any(ch in value for ch in "*?["):
        raise ValueError(f"{field} path must name a file, not a glob: {raw[:120]!r}")
    return "/".join(part for part in parts if part)


def _string_list(raw: Any, *, field: str, max_items: int = MAX_LIST_ITEMS,
                 max_chars: int = MAX_TEXT_CHARS, allow_empty: bool = False,
                 strict_str: bool = False) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list")
    if len(raw) > max_items:
        raise ValueError(f"{field} may contain at most {max_items} entries")
    out: list[str] = []
    for item in raw:
        if strict_str and not isinstance(item, str):
            raise ValueError(f"{field} entries must be strings")
        text = "" if item is None else str(item)
        if len(text) > max_chars:
            raise ValueError(f"{field} entry exceeds {max_chars} characters")
        if not text.strip() and not allow_empty:
            raise ValueError(f"{field} entries must not be empty")
        out.append(text)
    return out


def _bool(raw: Any, *, field: str, default: bool) -> bool:
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise ValueError(f"{field} must be a boolean")
    return raw


def normalize_coding_contract(raw: Any) -> dict[str, Any] | None:
    """Validate and normalize an optional coding contract.

    The output is chain-safe: strings, booleans and integers only. Defaults are
    explicit so a binding records the policy that was actually enforced.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("codingContract must be an object")
    unknown = set(raw) - {
        "deliverables", "forbiddenPaths", "noNewDependencies",
        "allowTestEdits", "requireVerification", "definitionOfDone",
    }
    if unknown:
        raise ValueError(f"codingContract has unknown fields: {', '.join(sorted(unknown))}")

    deliverables = [
        _clean_path(item, field="codingContract.deliverables", allow_glob=False)
        for item in _string_list(
            raw.get("deliverables"), field="codingContract.deliverables",
            max_items=MAX_LIST_ITEMS, strict_str=True,
        )
    ]
    forbidden = [
        _clean_path(item, field="codingContract.forbiddenPaths")
        for item in _string_list(
            raw.get("forbiddenPaths"), field="codingContract.forbiddenPaths",
            strict_str=True,
        )
    ]
    no_new_dependencies = _bool(
        raw.get("noNewDependencies"), field="codingContract.noNewDependencies",
        default=False,
    )
    allow_test_edits = _bool(
        raw.get("allowTestEdits"), field="codingContract.allowTestEdits",
        default=False,
    )
    require_verification = _bool(
        raw.get("requireVerification"), field="codingContract.requireVerification",
        default=True,
    )
    definition_of_done = _string_list(
        raw.get("definitionOfDone"), field="codingContract.definitionOfDone",
        max_items=10,
    )
    if require_verification and not deliverables:
        # A verification command can prove a behaviour even when the artifact
        # set is implicit, but a coding contract with neither deliverables nor
        # verification is not enforceable.
        raise ValueError(
            "codingContract needs deliverables or requireVerification=false"
        )
    effective_forbidden = list(dict.fromkeys(forbidden))
    if not allow_test_edits:
        effective_forbidden.extend(_TEST_PATTERNS)
    if no_new_dependencies:
        effective_forbidden.extend(DEPENDENCY_MANIFEST_PATTERNS)
    return {
        "deliverables": list(dict.fromkeys(deliverables)),
        "declared_forbidden_paths": list(dict.fromkeys(forbidden)),
        "forbidden_paths": list(dict.fromkeys(effective_forbidden)),
        "no_new_dependencies": no_new_dependencies,
        "allow_test_edits": allow_test_edits,
        "require_verification": require_verification,
        "definition_of_done": definition_of_done,
    }


DEPENDENCY_MANIFEST_PATTERNS = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "bun.lockb", "requirements.txt", "requirements-*.txt", "dev-requirements.txt",
    "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock", "uv.lock",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "composer.json",
    "composer.lock", "Gemfile", "Gemfile.lock", "mix.exs", "mix.lock",
)


def _matches(path: str, pattern: str) -> bool:
    path = path.strip("/")
    pattern = pattern.strip("/")
    if not path or not pattern:
        return False
    if path == pattern:
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))
    if "/**/" in pattern:
        prefix, suffix = pattern.split("/**/", 1)
        parts = path.split("/")
        for index in range(len(parts)):
            candidate = "/".join(parts[index:])
            if fnmatch.fnmatchcase(candidate, suffix) and (
                not prefix or "/".join(parts[:index]) == prefix
            ):
                return True
        return fnmatch.fnmatchcase(path, pattern)
    return fnmatch.fnmatchcase(path, pattern)


def _is_test_path(path: str) -> bool:
    lowered = path.lower().strip("/")
    parts = lowered.split("/")
    if any(part in _TEST_MARKERS for part in parts[:-1]):
        return True
    name = parts[-1]
    # The extension is not part of the convention: ``app_test.py`` is a test
    # even though the raw name ends in ".py", so prefix/suffix checks run on
    # the stem.
    stem = name.rpartition(".")[0] or name
    return (
        stem.startswith(_TEST_PREFIXES)
        or stem.endswith(_TEST_SUFFIXES)
        or any(marker in name for marker in _TEST_FILE_MARKERS)
    )


def _manifest_matches(relative: str, patterns: tuple[str, ...]) -> bool:
    """A bare manifest name freezes at any depth: ``sub/package.json`` is
    still a manifest, so suffix segments match too."""
    parts = relative.split("/")
    return any(
        fnmatch.fnmatchcase("/".join(parts[index:]), pattern)
        for pattern in patterns
        for index in range(len(parts))
    )


def coding_path_violation(contract: dict[str, Any] | None, path: str) -> str | None:
    """Return why a direct workspace path write is forbidden, else None."""
    if not contract or not isinstance(path, str) or not path.strip():
        return None
    value = path.strip().replace("\\", "/")
    if _is_test_path(value) and not bool(contract.get("allow_test_edits")):
        return "test edits are forbidden by this coding contract"
    if bool(contract.get("no_new_dependencies")):
        for pattern in DEPENDENCY_MANIFEST_PATTERNS:
            if _manifest_matches(value, (pattern,)):
                return f"dependency manifest {pattern!r} is frozen by this coding contract"
    for pattern in contract.get("forbidden_paths") or []:
        if _matches(value, str(pattern)):
            return f"path matches forbidden pattern {pattern!r}"
    return None


def contract_path_state(
    root: str | Path,
    contract: dict[str, Any] | None,
) -> dict[str, str]:
    """Hash every workspace path a contract forbids modifying.

    The baseline is taken after the workspace is seeded and before the model
    runs. At completion, a changed hash proves the forbidden surface changed —
    including through terminal commands that bypassed the file tools.
    """
    if not contract:
        return {}
    root_path = Path(root).resolve()
    patterns: list[str] = (
        list(_TEST_PATTERNS) if not bool(contract.get("allow_test_edits")) else []
    )
    patterns.extend(contract.get("forbidden_paths") or [])
    out: dict[str, str] = {}
    for pattern in patterns:
        try:
            matches = sorted(root_path.glob(pattern))
            if pattern.endswith("/**"):
                # pathlib's bare ``**`` enumerates directories only; the files
                # under the tree need the explicit leaf segment.
                matches.extend(root_path.glob(pattern + "/*"))
        except (OSError, ValueError):
            continue
        for path in matches:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root_path).as_posix()
            try:
                out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                out[relative] = "unreadable"
    if bool(contract.get("no_new_dependencies")):
        # Manifest patterns are bare names that freeze at any depth; globs
        # cannot express that, so suffix-match a bounded walk instead. Build
        # caches and VCS metadata are not manifests, whatever they contain.
        skip = {".git", ".hg", ".svn", ".venv", "venv", "node_modules",
                "vendor", "__pycache__"}
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in skip and not name.startswith(".")
            )
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root_path).as_posix()
                if _manifest_matches(relative, DEPENDENCY_MANIFEST_PATTERNS):
                    try:
                        out[relative] = hashlib.sha256(
                            path.read_bytes()).hexdigest()
                    except OSError:
                        out[relative] = "unreadable"
    return out


def missing_coding_deliverables(
    root: str | Path,
    contract: dict[str, Any] | None,
) -> list[str]:
    """Return contract deliverables absent from the workspace."""
    if not contract:
        return []
    root_path = Path(root).resolve()
    missing: list[str] = []
    for pattern in contract.get("deliverables") or []:
        try:
            matches = [path for path in root_path.glob(pattern) if path.is_file()]
        except (OSError, ValueError):
            matches = []
        if not matches:
            missing.append(str(pattern))
    return missing


def coding_contract_violations(
    root: str | Path,
    contract: dict[str, Any] | None,
    *,
    baseline: dict[str, str] | None = None,
) -> list[str]:
    """Return completion-time contract violations, with terminal bypasses covered."""
    if not contract:
        return []
    violations = [
        f"missing deliverable: {item}"
        for item in missing_coding_deliverables(root, contract)
    ]
    current = contract_path_state(root, contract)
    baseline = baseline or {}
    for relative, old_hash in sorted(baseline.items()):
        new_hash = current.get(relative)
        if new_hash != old_hash:
            violations.append(f"forbidden path changed: {relative}")
    for relative in sorted(set(current) - set(baseline)):
        violations.append(f"forbidden path created: {relative}")
    return violations


def coding_contract_instruction(contract: dict[str, Any] | None) -> str:
    if not contract:
        return ""
    lines = ["【Coding Contract】这是一个显式交付契约，runtime 会强制执行："]
    if contract.get("deliverables"):
        lines.append("- 必须交付：" + ", ".join(contract["deliverables"]))
    else:
        lines.append("- 未声明固定交付文件；行为验证是完成依据。")
    if contract.get("require_verification"):
        lines.append("- 必须运行声明的 verification 命令；runtime 还会冻结工作区独立重跑。")
    if not contract.get("allow_test_edits"):
        lines.append("- 禁止修改测试；测试失败时应修实现。")
    if contract.get("no_new_dependencies"):
        lines.append("- 禁止引入或更新依赖清单。")
    if contract.get("declared_forbidden_paths"):
        lines.append("- 禁止触碰路径：" + ", ".join(contract["declared_forbidden_paths"]))
    if contract.get("definition_of_done"):
        lines.append("- Definition of done（仍是证据判定，不是模型自述）：")
        lines.extend(f"  - {item}" for item in contract["definition_of_done"])
    lines.append("- 违反任一条会以 CODING_CONTRACT_VIOLATED / INCOMPLETE 失败。")
    return "\n".join(lines)


def coding_contract_binding(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    if not contract:
        return None
    return {
        "deliverables": contract.get("deliverables") or [],
        "declared_forbidden_paths": contract.get("declared_forbidden_paths") or [],
        "forbidden_paths": contract.get("forbidden_paths") or [],
        "no_new_dependencies": contract.get("no_new_dependencies"),
        "allow_test_edits": contract.get("allow_test_edits"),
        "require_verification": contract.get("require_verification"),
        "definition_of_done": contract.get("definition_of_done") or [],
    }

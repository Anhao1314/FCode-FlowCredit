"""The closed set of tools that may share one concurrent read batch.

Kept in its own module so dispatch (:mod:`runtime.loop`) and crash recovery
(:mod:`runtime.recovery`) agree without importing each other. Everything not
listed here keeps the sequential, one-open-call contract.
"""

from __future__ import annotations

__all__ = ["PARALLEL_READ_TOOLS"]

PARALLEL_READ_TOOLS = frozenset({
    "ls_workspace", "repo_map", "search_codebase", "read_file", "read_file_raw",
    "grep_workspace", "find_files", "git_status",
})
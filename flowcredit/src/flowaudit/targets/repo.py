"""The repo target adapter: pin a source tree, copy it, scan it.

The copy is the audit's working substrate: replays run inside the copy,
so the real target is never mutated and the replay's ``target_copy``
claim in the evidence record is a fact, not a hope. The manifest hash
pins what was audited — two runs against different content cannot share
a conclusion.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

#: Never copied into a replay substrate. Heavy, hostile or irrelevant
#: material is excluded at copy time, not filtered later.
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".github", "node_modules", ".venv", "__pycache__",
    "work", "runs", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm",
    "chat-config.json*", ".fcode-artifacts",
)


def copy_target(target_root: Path, workdir: Path) -> Path:
    """Copy the target tree into ``workdir`` and return the copy root."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    copy_root = workdir / "target"
    if copy_root.exists():
        shutil.rmtree(copy_root)
    shutil.copytree(Path(target_root), copy_root, ignore=COPY_IGNORE)
    return copy_root


def manifest_hash(copy_root: Path) -> str:
    """Content-address the copy: sha256 over sorted relative paths + bytes.

    Deterministic for identical trees, so an evidence record's target pin
    means "this exact content", not "this path on this machine".
    """
    digest = hashlib.sha256()
    for path in sorted(Path(copy_root).rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(copy_root))
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def new_workdir(parent: Path | None = None) -> Path:
    return Path(tempfile.mkdtemp(prefix="fcode-audit-", dir=parent))


def detect_profile(copy_root: Path) -> str:
    """Classify the target copy so rules can bind scenarios per profile.

    A rule generalizes across codebases; a scenario is bound to one
    target's seams. The profile is decided by marker layout, top-down:
    demo target first (it nests inside other trees), then a FCode-like
    checkout, then ``generic``.
    """
    root = Path(copy_root)
    if (root / "mini_agent" / "agent.py").exists():
        return "demo-target"
    if (root / "flowcredit" / "viewer" / "console_runtime.py").exists() \
            or (root / "src" / "flowagent" / "__init__.py").exists() \
            or (root / "viewer" / "console_runtime.py").exists():
        return "fcode"
    return "generic"

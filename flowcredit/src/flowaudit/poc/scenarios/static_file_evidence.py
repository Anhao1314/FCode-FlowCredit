"""Generic file-state probe: re-verify a static match against the copy.

Some defects need no target execution — a plaintext credential is
proven by reading the file inside the target copy and applying the
rule's pattern again. The worker reads; the host decides.
"""

from __future__ import annotations

import re
from pathlib import Path


def run(target_repo: Path, params: dict) -> dict:
    mode = params.get("mode", "probe")
    rel = params["path"]
    pattern = params["pattern"]
    path = Path(target_repo) / rel
    matches: list[dict] = []
    if path.exists():
        for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if re.search(pattern, line):
                matches.append({"line": number, "snippet": line.strip()[:160]})
                if len(matches) >= 3:
                    break
    return {
        "scenario": "static_file_evidence",
        "mode": mode,
        "path": rel,
        "matched": bool(matches),
        "matches": matches,
    }

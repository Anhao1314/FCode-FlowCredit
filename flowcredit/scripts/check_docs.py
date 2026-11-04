"""Documentation freshness checks.

This is deliberately small: it catches the failures that make a README
lie without turning documentation into a second test suite.

* relative Markdown links must resolve;
* current product docs must mention the REA artifact adapter and the audit
  console;
* stale counts/claims that survived a migration are refused.

It does not execute the test suite. The maintained test-count convention
is to update the count after `python -m unittest discover -s tests -q`;
that command remains the source of truth.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOCS = [
    ROOT.parent / "README.md",
    ROOT / "README.md",
    ROOT / "viewer" / "README.md",
    ROOT / "docs" / "integration-rea.md",
    ROOT / "docs" / "threat-model.md",
    ROOT / "docs" / "audit-methodology.md",
    ROOT / "docs" / "capability-map.md",
    ROOT / "docs" / "architecture-map.md",
]
MARKDOWN_FILES = sorted(
    {*CURRENT_DOCS, *ROOT.glob("docs/**/*.md"), ROOT / "audit-rules" / "README.md"}
)
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
STALE = (
    "765 项",
    "唯一已实现",
    "审计台设计稿",
    "审计控制台 UI",
    "targets/repo.py` | **唯一",
)
REQUIRED = {
    ROOT.parent / "README.md": ("--adapter rea", "audit.html"),
    ROOT / "README.md": ("--adapter rea", "audit.html"),
    ROOT / "viewer" / "README.md": ("/api/audit/bundles", "/api/audit/rea/", "audit.html"),
}


def _relative_links(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"{path}: cannot read: {error}")
        return []
    links: list[str] = []
    for raw in LINK.findall(text):
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if not target or target.startswith(("#", "http://", "https://", "mailto:", "tel:")):
            continue
        target = target.split("#", 1)[0]
        if not target or target.startswith("/"):
            continue
        links.append(target.replace("%20", " "))
    return links


def check_links() -> list[str]:
    errors: list[str] = []
    for path in MARKDOWN_FILES:
        for target in _relative_links(path):
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link target {target}")
    return errors


def check_stale_claims() -> list[str]:
    errors: list[str] = []
    for path in CURRENT_DOCS:
        text = path.read_text(encoding="utf-8")
        for marker in STALE:
            if marker in text:
                errors.append(f"{path.relative_to(ROOT)}: stale claim {marker!r}")
    return errors


def check_required_surface() -> list[str]:
    errors: list[str] = []
    for path, phrases in REQUIRED.items():
        text = path.read_text(encoding="utf-8")
        for phrase in phrases:
            if phrase not in text:
                errors.append(f"{path.relative_to(ROOT)}: missing current surface {phrase!r}")
    rules = json.loads((ROOT / "audit-rules" / "index.json").read_text(encoding="utf-8"))
    if int(rules.get("version", 0)) < 6:
        errors.append("audit-rules/index.json: version must be >= 6 (REA + sidecar + OSV + runtime install rules)")
    return errors


def main() -> int:
    errors = [*check_links(), *check_stale_claims(), *check_required_surface()]
    if errors:
        for error in errors:
            print(error)
        print(f"documentation check failed: {len(errors)} issue(s)")
        return 1
    print(f"documentation check passed: {len(MARKDOWN_FILES)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
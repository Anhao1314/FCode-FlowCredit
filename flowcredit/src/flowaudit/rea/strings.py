"""Bounded printable-string scan for runtime package installers.

Some applications invoke a package manager at runtime (for example
``npm install -g <package>``). That is a supply-chain surface: the command
may fetch and execute code with the user's privileges. This module reports
the *static command string* only. A string can be help text as well as an
executed command, so the rule is a candidate; dynamic observation or a
vendor statement is required to promote it to a finding.

The scan is deliberately narrow: only global npm/pnpm/yarn install forms.
General ``npx`` strings are too common in help text to be useful here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = ["scan_runtime_installers"]

_PATTERNS = (
    "npm install -g",
    "npm install --global",
    "npm i -g",
    "pnpm add -g",
    "pnpm add --global",
    "yarn global add",
)
_STRING = re.compile(rb"[\x20-\x7e]{8,}")
_MAX_BYTES = 512 * 1024 * 1024
_MAX_COMMANDS = 20
_MAX_TEXT = 300


def scan_runtime_installers(path: str | Path) -> list[dict[str, Any]]:
    """Return deduplicated runtime install command strings from a binary."""
    target = Path(path)
    try:
        size = target.stat().st_size
    except OSError:
        return []
    with target.open("rb") as handle:
        data = handle.read(min(size, _MAX_BYTES))
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _STRING.finditer(data):
        text = match.group().decode("ascii", errors="replace")
        lowered = text.lower()
        positions = [lowered.find(pattern) for pattern in _PATTERNS if pattern in lowered]
        if not positions:
            continue
        position = min(positions)
        command = text[position:position + _MAX_TEXT].strip()
        # A printable run can concatenate several adjacent Go strings. Cut at
        # the first diagnostic delimiter so the fact is the command, not the
        # whole error-message neighbourhood.
        cuts = [
            position for position in (
                command.find(delimiter)
                for delimiter in (":", "`", ",", '"', "\n", "\t", " | ")
            )
            if position > 0
        ]
        if cuts:
            command = command[:min(cuts)].strip()
        if not command or command in seen:
            continue
        seen.add(command)
        found.append({
            "command": command,
            "offset": match.start() + position,
            "source": "executable-strings",
        })
        if len(found) >= _MAX_COMMANDS:
            break
    return found
#!/usr/bin/env python3
"""Zero-configuration entry point for the audit domain.

Run this from a fresh clone with nothing installed:

    python audit.py audit fixtures/demo-target
    python audit.py audit . --out work/audit/self
    python audit.py verify-bundle work/audit/self/audit-bundle.json \
        --evidence work/audit/self

Mirrors ``demo.py``: it adds ``src/`` to ``sys.path`` and delegates to
``flowaudit.cli``. There is no install step and no third-party import anywhere
on this path.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flowaudit.cli import main  # noqa: E402  (path set up above)

if __name__ == "__main__":
    sys.exit(main())
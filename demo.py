#!/usr/bin/env python3
"""Zero-configuration entry point.

Run this from a fresh clone with nothing installed:

    python demo.py
    python demo.py full --fast

It adds ``src/`` to ``sys.path`` and delegates to ``flowagent.demo``. There is no
install step, no virtualenv requirement and no third-party import anywhere on
this path.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from flowagent.demo.__main__ import main  # noqa: E402  (path set up above)

if __name__ == "__main__":
    sys.exit(main())

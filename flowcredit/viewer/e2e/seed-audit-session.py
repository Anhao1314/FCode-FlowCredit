"""Create the audit session chain for an E2E fixture bundle.

Called by global-setup with PYTHONPATH pointing at the isolated repo's src.
The bundle itself is static JSON; this script only adds the signed session
ledger so the provenance-chain badge has something real to verify.
"""

from __future__ import annotations

import sys
from pathlib import Path

from flowaudit.session import AuditSession


def main() -> None:
    root = Path(sys.argv[1])
    audit_id = sys.argv[2]
    operator = sys.argv[3] if len(sys.argv) > 3 else "e2e"
    target_identity = sys.argv[4] if len(sys.argv) > 4 else "demo"
    AuditSession.start(
        root,
        audit_id,
        operator=operator,
        target_identity=target_identity,
    )


if __name__ == "__main__":
    main()

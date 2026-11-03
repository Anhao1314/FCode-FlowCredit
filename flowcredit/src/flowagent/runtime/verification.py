"""Compatibility re-export: the verification contract now lives in core.

The obligation is built by a domain's world (``tools.general.world``), which
must not import the runtime. Moving the contract down to ``core`` keeps the
capability layer and the runtime on the same side of the dependency line.
New code should import from :mod:`flowagent.core.verification`.
"""

from __future__ import annotations

from ..core.verification import (
    MAX_VERIFICATION_ARGS,
    MAX_VERIFICATION_NAME_CHARS,
    VERIFICATION_SOURCE_SPEC,
    VERIFICATION_TOOL,
    VerificationObligation,
    VerificationScope,
    build_obligation,
)

__all__ = [
    "MAX_VERIFICATION_ARGS",
    "MAX_VERIFICATION_NAME_CHARS",
    "VERIFICATION_SOURCE_SPEC",
    "VERIFICATION_TOOL",
    "VerificationObligation",
    "VerificationScope",
    "build_obligation",
]
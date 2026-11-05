"""Canonical JSON encoding and SHA-256 hashing.

Every hash in this runtime is computed over *canonical* JSON, never over
``repr`` or ``str`` of a Python object. Canonical means:

* object keys sorted by Unicode code point,
* no insignificant whitespace (``separators=(",", ":")``),
* non-ASCII characters kept as-is and encoded as UTF-8,
* ``None``/``True``/``False`` rendered as ``null``/``true``/``false``.

Two processes that agree on the data therefore agree on the bytes, which is
what makes an append-only hash chain verifiable after a crash or on another
machine. ``float`` is deliberately rejected: money and token counts are
integers, and float formatting is the usual way a "reproducible" hash stops
being reproducible.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "digest", "digest_bytes", "verify_digest"]


def _reject_float(value: Any) -> None:
    """Raise if *value* contains a float anywhere.

    Floats are excluded on purpose: ``0.1 + 0.2`` has no stable textual form
    across runtimes, and an audited runtime should never hash one. Quantities
    are carried as integers in the caller's smallest unit — basis points,
    micro-units or plain counts.
    """
    if isinstance(value, float):
        raise TypeError("canonical_json does not accept float; use int micro-units or basis points")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical_json requires string object keys")
            _reject_float(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_float(item)


def canonical_json(value: Any) -> str:
    """Return the canonical JSON text for *value*."""
    _reject_float(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest_bytes(payload: bytes) -> str:
    """Return the lowercase hex SHA-256 of *payload*."""
    return hashlib.sha256(payload).hexdigest()


def digest(value: Any) -> str:
    """Return the SHA-256 of the canonical JSON encoding of *value*."""
    return digest_bytes(canonical_json(value).encode("utf-8"))


def verify_digest(value: Any, expected: str) -> bool:
    """Return True when *value* still hashes to *expected*.

    Comparison uses :func:`hmac.compare_digest` so a tampering attempt cannot
    learn the expected prefix from timing.
    """
    import hmac

    return hmac.compare_digest(digest(value), expected)

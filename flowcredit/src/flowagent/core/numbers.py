"""Model-output number sanitation at the proposal boundary.

JSON has one number type; the runtime has two. Every hash in the chain is
computed over canonical JSON, and that encoder rejects floats on purpose: a
float's textual form is not stable enough to audit. A model that writes
``30.0`` for an integer field has said the integer 30, so the boundary
normalizes it; a fractional value cannot be truncated without changing intent,
so it is rejected and the caller turns that into a paid retry.

This is a *boundary* rule, not a relaxation of the hash rule: normalized output
is what gets validated and committed, and ``canonical_json`` still rejects any
float that somehow reaches it.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NumberNormalizationError", "normalize_json_numbers"]


class NumberNormalizationError(ValueError):
    """A fractional number appeared where the runtime requires an integer."""


def normalize_json_numbers(value: Any, *, path: str = "$") -> tuple[Any, int]:
    """Return ``(normalized, coerced_count)`` with integral floats as ints.

    Booleans are left alone (``bool`` is a subclass of ``int`` in Python but a
    distinct JSON type). Lists and dicts are walked; the returned structure
    shares no mutable containers with the input.
    """
    if isinstance(value, bool):
        return value, 0
    if isinstance(value, float):
        if value.is_integer():
            return int(value), 1
        raise NumberNormalizationError(
            f"non-integral number at {path}: {value!r}; use an integer"
        )
    if isinstance(value, dict):
        coerced = 0
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            child, count = normalize_json_numbers(item, path=f"{path}.{key}")
            normalized[key] = child
            coerced += count
        return normalized, coerced
    if isinstance(value, list):
        coerced = 0
        items: list[Any] = []
        for index, item in enumerate(value):
            child, count = normalize_json_numbers(item, path=f"{path}[{index}]")
            items.append(child)
            coerced += count
        return items, coerced
    if isinstance(value, tuple):
        coerced = 0
        items = []
        for index, item in enumerate(value):
            child, count = normalize_json_numbers(item, path=f"{path}[{index}]")
            items.append(child)
            coerced += count
        return tuple(items), coerced
    return value, 0
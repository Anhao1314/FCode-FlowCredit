"""Fact identity and construction: pure functions shared by memory and tools.

``FactStore`` lives in ``memory``, but building a fact row is a pure data
operation that a domain extractor performs before storage. Keeping the
constructors here lets ``tools.finance.facts`` produce rows without importing
the memory layer — the dependency points at ``core`` instead.
"""

from __future__ import annotations

import re
from typing import Any

from .hashing import digest

__all__ = ["fact_dates", "logical_key", "make_fact"]


def logical_key(subject: str, predicate: str, period: str) -> str:
    """The identity of a claim, independent of when it was recorded."""
    return "\x1f".join((subject, predicate, period))


def fact_dates(period: str) -> tuple[str, str]:
    """Valid-time interval for a fiscal period. Unknown periods stay open."""
    match = re.search(r"(?:FY)?(20\d{2})", period or "", re.IGNORECASE)
    if not match:
        return "", ""
    year = int(match.group(1))
    return f"{year}-01-01", f"{year}-12-31"


def make_fact(
    *,
    run_id: str,
    subject: str,
    predicate: str,
    period: str,
    value_int: int | None = None,
    value_text: str | None = None,
    unit: str = "",
    recorded_at: str,
    source_event: int,
    source_hash: str,
    source_result_hash: str,
    entities: list[str],
    extractor_version: str,
) -> dict[str, Any]:
    """Build one fact row with a content-addressed id. Pure function."""
    valid_from, valid_to = fact_dates(period)
    identity = {
        "run_id": run_id,
        "subject": subject,
        "predicate": predicate,
        "period": period,
        "value_int": value_int,
        "value_text": value_text,
        "unit": unit,
        "source_event": source_event,
        "source_hash": source_hash,
    }
    return {
        "fact_id": "fact_" + digest(identity)[:32],
        "subject": subject,
        "predicate": predicate,
        "period": period,
        "value_int": value_int,
        "value_text": value_text,
        "unit": unit,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "recorded_at": recorded_at,
        "source_event": source_event,
        "source_hash": source_hash,
        "source_result_hash": source_result_hash,
        "entities": sorted(set(entities)),
        "extractor_version": extractor_version,
    }
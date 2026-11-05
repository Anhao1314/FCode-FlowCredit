"""Ports the capability layer needs from memory, without importing memory.

``tools.workspace`` exposes ``search_cases`` / ``read_case`` / ``query_facts``,
so it genuinely needs a case index and a fact store. It must not import
``memory`` to get them: the two layers are siblings, and a hard import makes
the dependency circular (memory names ``Observation``, which used to live in
``tools``). A structural protocol is enough — the store only has to have the
methods, not inherit from anything.

``NullCaseIndex`` / ``NullFactStore`` are the standalone defaults. They return
empty results instead of raising, so a registry can be built for schema
inspection without opening a database. Composition roots (``domains``, the
console) inject the real stores.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CaseSearchPort",
    "FactQueryPort",
    "NullCaseIndex",
    "NullFactStore",
]


@runtime_checkable
class CaseSearchPort(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]: ...

    def get(self, run_id: str, *, include_archived: bool = True) -> dict[str, Any] | None: ...

    def related(self, run_id: str, *, limit: int = 3) -> list[dict[str, Any]]: ...


@runtime_checkable
class FactQueryPort(Protocol):
    def query(
        self,
        *,
        subject: str = "",
        predicate: str = "",
        period: str = "",
        as_of: str = "",
        known_at: str = "",
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...


class NullCaseIndex:
    """A case index that has nothing in it (standalone / schema-only use)."""

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return []

    def get(self, run_id: str, *, include_archived: bool = True) -> dict[str, Any] | None:
        return None

    def related(self, run_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
        return []


class NullFactStore:
    """A fact store with no assertions (standalone / schema-only use)."""

    def query(
        self,
        *,
        subject: str = "",
        predicate: str = "",
        period: str = "",
        as_of: str = "",
        known_at: str = "",
        include_superseded: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return []
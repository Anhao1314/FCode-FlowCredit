"""Evidence receipts (M1): every dispatch outcome is a chain-bound fact.

The moat contract, at runtime level:

* **Evidence by construction** — the loop emits one receipt per tool
  dispatch, whatever the outcome (executed, denied, invalid args, timeout,
  unknown tool). "Executed without a receipt" is not a process mistake; it
  is not representable.
* **Hash-bound** — each receipt carries the previous receipt's hash and its
  own canonical sha256. Dropping or editing any receipt breaks the chain,
  and ``verify_evidence_chain`` proves it.
* **Runner-agnostic** — receipts live in ``HarnessCore``, so every runner
  built on it (work loop migration, capability proposals, subagents)
  inherits them with zero runner code.

Discipline: canonical JSON, ints only (``duration_ms``), content never
inlined — only hashes. ``world`` carries optional per-file sha256 facts that
a tool chooses to attach via ``ToolResult.data["evidence"]``; the harness
never invents world state it did not observe.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "EvidenceChain",
    "EvidenceReceipt",
    "verify_evidence_chain",
]

ZERO_HASH = "0" * 64
_MAX_WORLD_ENTRIES = 16
_CANON = dict(sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, **_CANON)


def _args_digest(args: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dict(args or {})))


def _result_digest(content: str) -> str:
    return _sha256(str(content or ""))


def _normalize_world(raw: Any) -> tuple[dict[str, str], ...]:
    """Tool-attached file facts → bounded (path, sha256) tuples. Untrusted."""
    entries: list[dict[str, str]] = []
    items: Iterable[Any]
    if isinstance(raw, Mapping):
        items = [{"path": path, "sha256": sha}
                 for path, sha in list(raw.items())[:_MAX_WORLD_ENTRIES]]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "").strip()[:400]
        sha = str(item.get("sha256") or item.get("sha") or "").strip().lower()
        if not path or len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
            continue
        entries.append({"path": path, "sha256": sha})
        if len(entries) >= _MAX_WORLD_ENTRIES:
            break
    return tuple(entries)


@dataclass(frozen=True)
class EvidenceReceipt:
    """One dispatch outcome, chain-bound. Content is hashed, never inlined."""

    seq: int
    tool: str
    tool_use_id: str
    args_sha256: str
    result_sha256: str
    ok: bool
    code: str
    duration_ms: int
    world: tuple[dict[str, str], ...] = ()
    prev_sha256: str = ZERO_HASH
    sha256: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "seq": int(self.seq),
            "tool": self.tool,
            "tool_use_id": self.tool_use_id,
            "args_sha256": self.args_sha256,
            "result_sha256": self.result_sha256,
            "ok": bool(self.ok),
            "code": self.code,
            "duration_ms": int(self.duration_ms),
            "world": [dict(item) for item in self.world],
            "prev_sha256": self.prev_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "sha256": self.sha256}


def _receipt_sha(body: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dict(body)))


class EvidenceChain:
    """Append-only receipt chain. Every append is hash-bound to the head."""

    def __init__(self) -> None:
        self._receipts: list[EvidenceReceipt] = []

    def append(
        self,
        *,
        tool: str,
        tool_use_id: str,
        args: Mapping[str, Any],
        result_content: str,
        ok: bool,
        code: str,
        duration_ms: int,
        world: Any = None,
    ) -> EvidenceReceipt:
        prev = self._receipts[-1].sha256 if self._receipts else ZERO_HASH
        body = {
            "seq": len(self._receipts) + 1,
            "tool": str(tool or ""),
            "tool_use_id": str(tool_use_id or ""),
            "args_sha256": _args_digest(args),
            "result_sha256": _result_digest(result_content),
            "ok": bool(ok),
            "code": str(code or ""),
            "duration_ms": max(0, int(duration_ms)),
            "world": [dict(item) for item in _normalize_world(world)],
            "prev_sha256": prev,
        }
        receipt = EvidenceReceipt(sha256=_receipt_sha(body), **body)
        self._receipts.append(receipt)
        return receipt

    def head(self) -> str:
        return self._receipts[-1].sha256 if self._receipts else ZERO_HASH

    def receipts(self) -> list[EvidenceReceipt]:
        return list(self._receipts)

    def to_list(self) -> list[dict[str, Any]]:
        return [receipt.to_dict() for receipt in self._receipts]


def verify_evidence_chain(receipts: Iterable[Mapping[str, Any]]) -> tuple[bool, str]:
    """Recompute every hash and the prev-linkage. Returns (ok, reason)."""
    prev = ZERO_HASH
    for index, receipt in enumerate(receipts, start=1):
        body = {key: value for key, value in dict(receipt).items() if key != "sha256"}
        if int(body.get("seq", -1)) != index:
            return False, f"receipt {index}: seq mismatch"
        if str(body.get("prev_sha256", "")) != prev:
            return False, f"receipt {index}: prev link broken"
        digest = _receipt_sha(body)
        if str(receipt.get("sha256", "")) != digest:
            return False, f"receipt {index}: hash mismatch"
        prev = str(receipt.get("sha256"))
    return True, ""

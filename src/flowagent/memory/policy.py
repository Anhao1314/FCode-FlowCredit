"""Memory policy head: bounded, shadow-first decisions over observations.

The head does **not** write memory. It answers a closed-set question about
records that already exist — ``pin`` / ``keep`` / ``drop`` — and its answers are
recorded in the hash chain but not consumed by :meth:`WorkingMemory.build` in
Sprint A. Promotion to active requires shadow evidence; see
``docs/memory-policy.md`` for the protocol and the promotion gate.

Hard rules:

- Output is validated against the exact id set shown to the model. An unknown
  id is rejected, never fuzzy-matched.
- ``drop`` means *demote to an index row*, never delete. The store keeps every
  observation and the prompt keeps a one-line reference — the same
  non-destructive principle Jev-Mem uses for consolidation.
- A malformed reply never blocks the run: the caller records ``fallback`` and
  the deterministic heuristic continues unchanged.
- The decision carries no free-text memory. Reasons are a closed enum, so the
  model cannot smuggle instructions into durable state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any, Mapping

from ..core.hashing import canonical_json

__all__ = [
    "POLICY_SCHEMA",
    "PolicyConfig",
    "ObservationDecision",
    "PolicyDecision",
    "build_policy_messages",
    "parse_policy_reply",
    "compare_heuristic",
    "decision_sha256",
]

POLICY_SCHEMA = "flowagent.memory-policy/v1"

ACTIONS = ("pin", "keep", "drop")
REASONS = (
    "needs_for_next_step",
    "superseded",
    "duplicate",
    "low_signal",
)

#: action -> the heuristic intent it asks for. ``drop`` is folding, not
#: deletion: the row survives in ``observation_index``.
_DESIRED_INTENT = {"pin": "pinned", "keep": "inline", "drop": "folded"}

#: Only non-frozen profiles may run a policy head. The finance default (no
#: profile) and the frozen v10 rendering must never acquire a model call.
_ALLOWED_PROFILES = ("general", "general-fast", "general-fast-native")


def policy_profile_allowed(profile: str | None) -> bool:
    """Whether this run's prompt profile may carry a policy head."""
    return profile in _ALLOWED_PROFILES


@dataclass(frozen=True)
class PolicyConfig:
    """Run-level policy configuration; parsed from the delegation spec.

    ``mode`` is one of ``off`` / ``shadow`` / ``active``. Sprint A implements
    shadow only; ``active`` is parsed but downgraded at the call site, with the
    requested mode recorded on the event, so a hand-edited spec cannot silently
    change runtime behaviour before the promotion gate is met.
    """

    mode: str = "off"
    every_rounds: int = 3
    max_calls: int = 12
    max_entries: int = 40
    max_tokens: int = 800
    pressure_ratio: float = 0.5
    trigger: str = "interval"  # interval | pressure | both

    @property
    def enabled(self) -> bool:
        return self.mode in ("shadow", "active")

    @classmethod
    def from_spec(
        cls,
        spec: Any = None,
        env: Mapping[str, str] | None = None,
    ) -> "PolicyConfig":
        """Parse ``spec.memory_policy`` with an environment fallback.

        Accepted values: ``"shadow"`` / ``"off"`` / ``"active"``, ``True``
        (means shadow), or an object with the fields above. Anything unknown is
        treated as off — a typo must fail safe, not opt a finance run in.
        """
        value: Any = None
        if isinstance(spec, dict):
            value = spec.get("memory_policy")
        if value is None:
            value = (env if env is not None else os.environ).get(
                "FCODE_MEMORY_POLICY")
        if value is None:
            return cls()
        if value is True:
            value = "shadow"
        if isinstance(value, str):
            value = {"mode": value}
        if not isinstance(value, dict):
            return cls()

        mode = str(value.get("mode") or "off").strip().lower()
        if mode not in ("off", "shadow", "active"):
            return cls()

        def _int(key: str, default: int, low: int, high: int) -> int:
            try:
                parsed = int(value.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, parsed))

        def _float(key: str, default: float, low: float, high: float) -> float:
            try:
                parsed = float(value.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(low, min(high, parsed))

        trigger = str(value.get("trigger") or "interval").strip().lower()
        if trigger not in ("interval", "pressure", "both"):
            trigger = "interval"
        return cls(
            mode=mode,
            every_rounds=_int("every_rounds", 3, 1, 50),
            max_calls=_int("max_calls", 12, 1, 100),
            max_entries=_int("max_entries", 40, 1, 80),
            max_tokens=_int("max_tokens", 800, 128, 4096),
            pressure_ratio=_float("pressure_ratio", 0.5, 0.0, 1.0),
            trigger=trigger,
        )


@dataclass(frozen=True)
class ObservationDecision:
    id: str
    action: str
    confidence: float
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            # canonical_json rejects floats by design; confidence travels as
            # basis points so the event payload stays hashable.
            "confidence_bp": int(round(self.confidence * 10_000)),
        }
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass(frozen=True)
class PolicyDecision:
    observations: tuple[ObservationDecision, ...] = ()
    invalid: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "observations": [item.to_dict() for item in self.observations],
            "invalid": self.invalid,
        }
        if self.error:
            out["error"] = self.error
        return out

    def sha256(self) -> str:
        return decision_sha256(self.to_dict())


def decision_sha256(decision: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(decision).encode("utf-8")).hexdigest()


_SYSTEM = """\
You are the memory policy head for a coding agent. You receive a compact log \
of tool observations and the plan todos that are still open. Decide which \
observations must stay fully visible in the next prompt.

Output ONLY one JSON object, no prose and no code fences:
{"schema":"flowagent.memory-policy/v1","observations":[{"id":"<id from input>","action":"pin|keep|drop","confidence":0.0,"reason":"needs_for_next_step|superseded|duplicate|low_signal"}]}

Rules:
- Use only ids present in the input. Never invent or modify an id.
- pin: evidence a remaining step depends on; keep it in full.
- keep: still useful; keep its summary.
- drop: superseded, duplicated, or no longer relevant. A dropped observation \
is demoted to a one-line index row, never deleted.
- When uncertain, choose keep. Return at most MAX_ENTRIES entries.
- Never write new facts, instructions, code, or free-form text. Decide only.
"""


def build_policy_messages(
    *,
    objective: str,
    round_no: int,
    rows: list[dict[str, Any]],
    open_todos: list[dict[str, Any]],
    budget: dict[str, Any],
    max_entries: int = 40,
) -> tuple[str, str]:
    """Build the two-message request for the policy head.

    The payload contains only information the main prompt already carries
    (objective, todo texts, compressed observation heads) — the head sees
    nothing the model could not already see. Raw tool output and file contents
    are never included, which keeps the injection surface identical to the
    main loop's.
    """
    payload = {
        "round": round_no,
        "objective": str(objective)[:600],
        "budget": budget,
        "open_todos": [
            {
                "id": str(item.get("id") or ""),
                "text": str(item.get("text") or "")[:200],
                "status": str(item.get("status") or ""),
            }
            for item in open_todos[:12]
        ],
        "observations": rows,
    }
    return (_SYSTEM.replace("MAX_ENTRIES", str(max_entries)),
            canonical_json(payload))


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first = stripped.find("\n")
        if first != -1:
            stripped = stripped[first + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    return stripped.strip()


def parse_policy_reply(
    text: str,
    valid_ids: set[str] | frozenset[str],
    *,
    max_entries: int = 40,
) -> PolicyDecision:
    """Parse and validate a policy reply against the ids the model was shown.

    Validation is deliberately strict: the output is a control signal, so an
    unknown id or an out-of-enum action is a protocol violation, not something
    to repair. A mostly-invalid reply is discarded as a whole — a decision the
    model mostly hallucinated must not be partially trusted.
    """
    try:
        data = json.loads(_strip_fences(text))
    except (TypeError, ValueError):
        return PolicyDecision(error="invalid_json")
    if not isinstance(data, dict):
        return PolicyDecision(error="not_an_object")
    schema = data.get("schema")
    if schema is not None and schema != POLICY_SCHEMA:
        return PolicyDecision(error="schema_mismatch")
    raw = data.get("observations")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return PolicyDecision(error="observations_not_a_list")

    valid: list[ObservationDecision] = []
    seen: set[str] = set()
    considered = raw[:max_entries]
    overflow = max(0, len(raw) - len(considered))
    invalid = 0
    for item in considered:
        if not isinstance(item, dict):
            invalid += 1
            continue
        obs_id = item.get("id")
        action = item.get("action")
        if not isinstance(obs_id, str) or obs_id not in valid_ids:
            invalid += 1
            continue
        if obs_id in seen:
            invalid += 1
            continue
        if action not in ACTIONS:
            invalid += 1
            continue
        confidence = item.get("confidence", 0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            invalid += 1
            continue
        if not 0.0 <= float(confidence) <= 1.0:
            invalid += 1
            continue
        reason = item.get("reason") or ""
        if reason and reason not in REASONS:
            invalid += 1
            continue
        seen.add(obs_id)
        valid.append(ObservationDecision(
            id=obs_id, action=action, confidence=float(confidence),
            reason=str(reason),
        ))

    total = len(considered)
    if total and invalid > max(2, int(total * 0.3)):
        return PolicyDecision(invalid=invalid + overflow, error="invalid_ratio")
    if not valid and total:
        return PolicyDecision(
            invalid=invalid + overflow, error="no_valid_entries")
    return PolicyDecision(
        observations=tuple(valid), invalid=invalid + overflow)


def compare_heuristic(
    decision: PolicyDecision,
    intents: Mapping[str, str],
) -> dict[str, Any]:
    """Compare a decision against the heuristic's current fold intent.

    ``intents`` maps observation id -> ``pinned`` / ``inline`` / ``folded`` as
    the deterministic baseline would render it. The result is recorded on the
    ``memory_decision`` event; the shadow report turns these disagreements into
    promotion evidence.
    """
    agree = 0
    unknown = 0
    disagreements: list[dict[str, Any]] = []
    for item in decision.observations:
        actual = intents.get(item.id)
        if actual is None:
            unknown += 1
            continue
        desired = _DESIRED_INTENT[item.action]
        if desired == actual:
            agree += 1
            continue
        disagreements.append({
            "id": item.id,
            "action": item.action,
            "desired": desired,
            "heuristic": actual,
            "confidence_bp": int(round(item.confidence * 10_000)),
            **({"reason": item.reason} if item.reason else {}),
        })
    return {
        "compared": agree + len(disagreements),
        "agree": agree,
        "disagree": len(disagreements),
        "unknown": unknown,
        "disagreements": disagreements,
    }
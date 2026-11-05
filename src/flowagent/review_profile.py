"""Reviewer profile: how past runs were judged, projected from signed traces.

The profile is not a user model and not a second memory store. It is a
**deterministic, read-only projection** over the ``review_decided`` events that
are already committed to exported traces: the same traces in yield the same
counts, ordering and suggestion out. Nothing is inferred, cached or extracted by
a model, and a run with no review simply contributes nothing.

The suggestion is a reading aid, never a decision. It is labelled
``同类历史多数`` and carries the most recent reason, so the operator can see the
precedent and still sign the judgement herself.
"""

from __future__ import annotations

from typing import Any, Iterable

from .review import DECISIONS

__all__ = [
    "BASIS",
    "DECISIONS",
    "RECENT_LIMIT",
    "UNKNOWN",
    "build_profile",
    "review_records",
    "scope_of",
]

#: How many past decisions the console shows. Enough to expose a pattern,
#: short enough that a single run cannot dominate the page.
RECENT_LIMIT = 5

#: Explicit absence. A trace without a binding or terminal status is still
#: counted, but never silently promoted to a real scope value.
UNKNOWN = "unknown"

#: The suggestion names its evidence class instead of pretending to be a model.
BASIS = "同类历史多数"


def _text(value: Any) -> str:
    """Project a JSON value to text without inventing content."""
    return value if isinstance(value, str) else ""


def _scope_value(value: Any) -> str:
    text = _text(value).strip()
    return text or UNKNOWN


def scope_of(trace: dict[str, Any]) -> dict[str, str]:
    """Return ``module`` / ``template`` / ``outcome`` for one trace.

    The shape follows the exported trace: ``task.binding.module``,
    ``task.binding.scenario`` and ``terminal.status``. Any missing, empty or
    non-string value becomes ``unknown`` rather than being dropped, so a
    malformed trace cannot hide from the counts.
    """
    if not isinstance(trace, dict):
        trace = {}
    task = trace.get("task")
    binding = task.get("binding") if isinstance(task, dict) else None
    terminal = trace.get("terminal")
    if not isinstance(binding, dict):
        binding = {}
    if not isinstance(terminal, dict):
        terminal = {}
    return {
        "module": _scope_value(binding.get("module")),
        "template": _scope_value(binding.get("scenario")),
        "outcome": _scope_value(terminal.get("status")),
    }


def review_records(trace: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    """Extract one record per valid ``review_decided`` event, in chain order.

    Only the four known judgements are accepted; anything else (a missing
    payload, a malformed detail, an unknown decision imported from an older
    build) is skipped rather than counted as a decision nobody made.
    """
    if not isinstance(trace, dict):
        return []
    events = trace.get("events")
    if not isinstance(events, list):
        return []

    records: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "review_decided":
            continue
        payload = event.get("payload")
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if not isinstance(detail, dict):
            continue
        decision = detail.get("decision")
        if decision not in DECISIONS:
            continue
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            sequence = 0
        records.append(
            {
                "run_id": str(run_id),
                "decision": decision,
                "reason": _text(detail.get("reason")),
                "reviewer": _text(detail.get("reviewer")),
                "sequence": sequence,
                "at": _text(event.get("at")),
            }
        )
    return records


def _order(record: dict[str, Any]) -> tuple[str, int, str]:
    """Newest first: timestamp, then the in-run sequence, then the run id.

    The run id is the final tiebreaker so equal timestamps from different runs
    still have a stable order instead of depending on directory iteration.
    """
    return (record["at"], record["sequence"], record["run_id"])


def build_profile(
    traces: Iterable[tuple[str, dict[str, Any]]],
    *,
    module: str = "",
    template: str = "",
    outcome: str = "",
) -> dict[str, Any]:
    """Project a reviewer profile from ``(run_id, trace)`` pairs.

    ``module``, ``template`` and ``outcome`` are exact scope filters; an empty
    string means "any". Every review of every matching trace counts, because a
    run may be reviewed more than once (a later decision is an appended
    correction, not an overwrite).
    """
    module = module.strip() if isinstance(module, str) else ""
    template = template.strip() if isinstance(template, str) else ""
    outcome = outcome.strip() if isinstance(outcome, str) else ""

    records: list[dict[str, Any]] = []
    for run_id, trace in traces:
        scope = scope_of(trace)
        if module and scope["module"] != module:
            continue
        if template and scope["template"] != template:
            continue
        if outcome and scope["outcome"] != outcome:
            continue
        records.extend(review_records(trace, run_id))

    records.sort(key=_order, reverse=True)

    counts = {decision: 0 for decision in DECISIONS}
    for record in records:
        counts[record["decision"]] += 1
    total = len(records)

    suggested: dict[str, Any] | None = None
    if total >= 2:
        top = max(counts.values())
        leading = {decision for decision, count in counts.items() if count == top}
        # ``records`` is newest first, so the first leading decision is also the
        # most recently made one: that is the tie-break, with no hidden score.
        decision = next(record["decision"] for record in records if record["decision"] in leading)
        latest = next(record for record in records if record["decision"] == decision)
        suggested = {
            "decision": decision,
            "support": counts[decision],
            "total": total,
            "basis": BASIS,
            "last_reason": latest["reason"],
        }

    return {
        "scope": {"module": module, "template": template, "outcome": outcome},
        "total": total,
        "counts": counts,
        "suggested": suggested,
        "recent": records[:RECENT_LIMIT],
    }

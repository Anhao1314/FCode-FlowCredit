"""Finance memory summaries: the per-tool compressors and the state view.

Moved verbatim from ``memory/context.py`` when summarization became a tool
contract: the finance tool knows which fields a decision needs, and the
PROMPT_VERSION=v10 contract is bound to these exact key sets — a renamed key
here would silently change the prompt and invalidate the SFT dataset. The
golden dicts in ``tests/test_context_management.py`` pin this file.

``summarize_state`` is the finance answer to "what has been established so
far"; it is registered on the finance :class:`~flowagent.domains.base.DomainModule`
and called by working memory instead of living in the kernel.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "summarize_list_documents",
    "summarize_read_document",
    "summarize_normalize_units",
    "summarize_reconcile_basis",
    "summarize_check_restatement",
    "summarize_calculate_margin",
    "summarize_report_blocked",
    "summarize_state",
    "FINANCE_SUMMARIZERS",
]


def summarize_list_documents(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "documents": [
            item.get("id") or item.get("doc_id")
            for item in result.get("documents", [])
        ]
    }


def summarize_read_document(result: dict[str, Any]) -> dict[str, Any]:
    # Two result shapes exist: the general workspace shape (doc_id/fields) and
    # the reference workload shape (id/basis + the two margin inputs).
    if "fields" in result or "doc_id" in result:
        return {
            "found": result.get("found", True),
            "doc_id": result.get("doc_id"),
            "period": result.get("period"),
            "unit": result.get("unit"),
            "fields": dict(result.get("fields") or {}),
        }
    return {
        "id": result.get("id"),
        "period": result.get("period"),
        "unit": result.get("unit"),
        "basis": result.get("basis"),
        "revenue": result.get("revenue"),
        "gross_profit": result.get("gross_profit"),
    }


def summarize_normalize_units(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": result.get("id"),
        "revenue_cny": result.get("revenue_cny"),
        "gross_profit_cny": result.get("gross_profit_cny"),
    }


def summarize_reconcile_basis(result: dict[str, Any]) -> dict[str, Any]:
    reconciled = bool(result.get("reconciled"))
    summary: dict[str, Any] = {
        "period": result.get("period"),
        "reconciled": reconciled,
        "from_basis": result.get("from_basis"),
        "to_basis": result.get("to_basis"),
    }
    if reconciled:
        summary["delta"] = result.get("delta")
        summary["reconciled_gross_profit"] = result.get("reconciled_gross_profit")
    else:
        summary["reason"] = result.get("reason") or "basis bridge unavailable"
    return summary


def summarize_check_restatement(result: dict[str, Any]) -> dict[str, Any]:
    if "original_ebitda" in result:
        return {
            "period": result.get("period"),
            "restated": result.get("restated"),
            "original_ebitda": result.get("original_ebitda"),
            "restated_ebitda": result.get("restated_ebitda"),
            "delta": result.get("delta"),
            "superseded_by": result.get("superseded_by"),
        }
    return {
        "period": result.get("period"),
        "restated": result.get("restated"),
        "delta": result.get("delta"),
        "restated_gross_profit": result.get("restated_gross_profit"),
    }


def summarize_calculate_margin(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "computed": result.get("computed"),
        "blockers": result.get("blockers", []),
        "margin_bp_2024": result.get("margin_bp_2024"),
        "margin_bp_2025": result.get("margin_bp_2025"),
        "change_bp": result.get("change_bp"),
    }


def summarize_report_blocked(result: dict[str, Any]) -> dict[str, Any]:
    return {"reason": result.get("reason")}


FINANCE_SUMMARIZERS: dict[str, Any] = {
    "list_documents": summarize_list_documents,
    "read_document": summarize_read_document,
    "normalize_units": summarize_normalize_units,
    "reconcile_basis": summarize_reconcile_basis,
    "check_restatement": summarize_check_restatement,
    "calculate_margin": summarize_calculate_margin,
    "report_blocked": summarize_report_blocked,
}


def _documents_for_period(period: str) -> list[str]:
    """Map a reconciled period back to the document that owns it."""
    mapping = {"FY2024": ["doc_fy2024"], "FY2025": ["doc_fy2025"]}
    return mapping.get(period, [])


def summarize_state(records: Iterable[Any]) -> dict[str, Any]:
    """Derive what has been established so far, from observations alone.

    Verbatim port of the former ``WorkingMemory.state`` — the key set is part
    of the v10 prompt contract.
    """
    read_documents: list[str] = []
    normalised: list[str] = []
    reconciled_periods: list[str] = []
    listed = False
    restatement_checked = False
    restatement_applied = False
    computed = False
    blockers: list[str] = []
    margins: dict[str, Any] = {}

    for record in records:
        tool, result = record.tool, record.result
        if tool == "list_documents":
            listed = True
        elif tool == "read_document":
            doc_id = result.get("id") or result.get("doc_id")
            if doc_id and result.get("found", True):
                read_documents.append(doc_id)
            elif result.get("found") is False:
                blockers.append(result.get("reason") or "document missing")
        elif tool == "normalize_units" and "id" in result:
            normalised.append(result["id"])
        elif tool == "reconcile_basis" and "period" in result:
            if result.get("reconciled"):
                reconciled_periods.append(result["period"])
            else:
                blockers.append(result.get("reason") or "basis bridge did not reconcile")
        elif tool == "check_restatement":
            restatement_checked = True
            restatement_applied = bool(result.get("restated"))
        elif tool == "calculate_margin":
            computed = bool(result.get("computed"))
            blockers = list(result.get("blockers") or [])
            if computed:
                margins = {
                    "margin_bp_2024": result.get("margin_bp_2024"),
                    "margin_bp_2025": result.get("margin_bp_2025"),
                    "change_bp": result.get("change_bp"),
                }
        elif tool == "report_blocked":
            blockers.append(result.get("reason", "unspecified"))

    reconciled_documents: list[str] = []
    for period in sorted(set(reconciled_periods)):
        reconciled_documents.extend(_documents_for_period(period))

    return {
        "listed_documents": listed,
        "read_documents": sorted(set(read_documents)),
        "normalised_documents": sorted(set(normalised)),
        "reconciled_periods": sorted(set(reconciled_periods)),
        "reconciled_documents": sorted(set(reconciled_documents)),
        "restatement_checked": restatement_checked,
        "restatement_applied": restatement_applied,
        "computed": computed,
        "blockers": blockers,
        "margins": margins,
    }

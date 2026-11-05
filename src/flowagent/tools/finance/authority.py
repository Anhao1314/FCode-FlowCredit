"""Evidence Authority: who is allowed to assert what, and for which purpose.

The Node prototype separated five permissions and permanently denied three of
them. This port keeps the same shape, because the distinction is the reason the
runtime can say "this number may be used for a contextual comparison" without
ever saying "this number is true".

In Phase 1 the finance domain only ever grants ``contextual_calculation``. A
superseded figure is denied outright: using a restated document's original
number is exactly the failure mode the demo is built to expose.
"""

from __future__ import annotations

from typing import Any

from .fixtures import FinanceWorld

__all__ = ["PERMISSION_KEYS", "evaluate_fact_authority", "AUTHORITY_VERSION"]

AUTHORITY_VERSION = "flowagent.evidence-authority/v1"

PERMISSION_KEYS = (
    "contextual_calculation",
    "scenario_calculation",
    "risk_calculation",
    "independent_corroboration",
    "integrity_veto",
)


def _denied() -> dict[str, bool]:
    return {key: False for key in PERMISSION_KEYS}


def evaluate_fact_authority(world: FinanceWorld, doc_id: str) -> dict[str, Any]:
    """Decide whether one document's figures may feed a contextual calculation.

    Returns a decision object rather than a boolean so the trace can show *why*
    a figure was refused. A refusal that cannot be explained is not auditable.
    """
    permissions = _denied()
    reasons: list[dict[str, str]] = []
    assertion_class = "rejected"

    doc = world.documents.get(doc_id)
    if doc is None:
        reasons.append({"code": "UNKNOWN_DOCUMENT", "message": "the document is not in this assessment"})
    elif doc.supersedes is not None:
        reasons.append(
            {
                "code": "SUPERSEDED_DOCUMENT",
                "message": "this document replaces an earlier one and cannot itself be cited as a source figure",
            }
        )
    elif any(other.supersedes == doc.id for other in world.documents.values()):
        reasons.append(
            {
                "code": "RESTATED_DOCUMENT",
                "message": "a later note restates this document; cite the restated figures instead",
            }
        )
    elif doc.basis != "IFRS":
        reasons.append(
            {
                "code": "NON_IFRS_BASIS",
                "message": f"basis {doc.basis!r} needs a bridge before it can be compared with IFRS figures",
            }
        )
    elif doc.revenue <= 0 or doc.gross_profit < 0:
        reasons.append({"code": "INVALID_VALUE", "message": "revenue must be positive and gross profit non-negative"})
    else:
        permissions["contextual_calculation"] = True
        assertion_class = "issuer-assertion"
        reasons.append(
            {
                "code": "CONTEXTUAL_ONLY",
                "message": "the figure may support a labelled comparison; it is not an audited fact or a credit conclusion",
            }
        )

    reasons.append(
        {
            "code": "PRIVATE_AUTHORITY_NOT_IMPLEMENTED",
            "message": "no authenticated private-source policy exists; risk calculation and integrity veto remain denied",
        }
    )

    return {
        "version": AUTHORITY_VERSION,
        "doc_id": doc_id,
        "assertion_class": assertion_class,
        "permissions": permissions,
        "reasons": reasons,
        "risk_eligible": False,
        "veto_eligible": False,
    }

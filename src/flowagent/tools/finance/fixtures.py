"""The synthetic company used by the demo, and the three obstacles in it.

The story is deliberately small but contains the three things that make real
financial comparison hard, and that a pure rule engine handles badly:

1. **unit mismatch** — FY2024 is filed in ten-thousands of CNY, FY2025 in
   millions of CNY;
2. **basis mismatch** — FY2025 also publishes an "adjusted" gross profit that
   is not IFRS, so a bridge is needed before it can be compared;
3. **restatement** — FY2024 gross profit was later restated, so the figure a
   naive reader would use is stale.

Every amount is an integer in the document's own unit. Nothing here is a
float, because floats and reproducible hashes do not coexist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "UNIT_SCALE",
    "Document",
    "FinanceWorld",
    "build_world",
]

#: Multiplier that converts one unit of the document into CNY.
UNIT_SCALE: dict[str, int] = {
    "CNY": 1,
    "CNY_10K": 10_000,      # 万元
    "CNY_1M": 1_000_000,    # 百万元
}

UNIT_LABEL: dict[str, str] = {
    "CNY": "元",
    "CNY_10K": "万元",
    "CNY_1M": "百万元",
}


@dataclass(frozen=True)
class Document:
    """One synthetic filing or note."""

    id: str
    kind: str
    title: str
    period: str
    unit: str
    basis: str
    revenue: int
    gross_profit: int
    body: str
    supersedes: str | None = None
    bridge_delta: int = 0
    bridge_reason: str = ""


class FinanceWorld:
    """A tiny, deterministic finance domain with an auditable read path.

    The world remembers what has been read, normalised, reconciled and whether
    the restatement was applied, because ``calculate_margin`` refuses to run on
    a partially prepared evidence set. That refusal is the demo's point: the
    runtime can tell the difference between "the model asked" and "the evidence
    is ready".
    """

    def __init__(self, *, partial: bool = False) -> None:
        """Build the corpus.

        ``partial`` drops the reconciliation bridge, which is the honest version
        of this demo: the company published an adjusted figure and never
        explained the difference to IFRS. An agent that computes anyway is
        producing a number nobody can check.
        """
        self.partial = partial
        self.documents: dict[str, Document] = {}
        self.read_documents: set[str] = set()
        self.normalised: set[str] = set()
        self.reconciled: set[str] = set()
        self.restatement_applied = False
        self.blockers: list[dict[str, str]] = []
        self._seed()

    # ------------------------------------------------------------------ seed

    def _seed(self) -> None:
        docs = [
            Document(
                id="doc_fy2024",
                kind="annual_report",
                title="恒星智算 2024 年年度报告（合并口径）",
                period="FY2024",
                unit="CNY_10K",
                basis="IFRS",
                revenue=86_400,
                gross_profit=15_552,
                body=(
                    "营业收入 86,400 万元，营业成本 70,848 万元，毛利 15,552 万元。"
                    "本表按国际财务报告准则（IFRS）编制，合并范围包含全部子公司。"
                ),
            ),
            Document(
                id="doc_fy2024_restated",
                kind="restatement_note",
                title="恒星智算 2024 年重述说明",
                period="FY2024",
                unit="CNY_10K",
                basis="IFRS",
                revenue=86_400,
                gross_profit=15_120,
                supersedes="doc_fy2024",
                body=(
                    "因将 432 万元履约成本由销售费用重分类至营业成本，"
                    "2024 年毛利由 15,552 万元重述为 15,120 万元。"
                    "本说明替代 2024 年年度报告中的对应金额。"
                ),
            ),
            Document(
                id="doc_fy2025",
                kind="annual_report",
                title="恒星智算 2025 年年度报告（合并口径）",
                period="FY2025",
                unit="CNY_1M",
                basis="IFRS",
                revenue=1_120,
                gross_profit=168,
                body=(
                    "营业收入 1,120 百万元，营业成本 952 百万元，毛利 168 百万元。"
                    "本表按国际财务报告准则（IFRS）编制，合并范围与上年一致。"
                ),
            ),
            Document(
                id="doc_fy2025_adjusted",
                kind="supplement",
                title="恒星智算 2025 年调整后口径补充材料",
                period="FY2025",
                unit="CNY_1M",
                basis="adjusted",
                revenue=1_120,
                gross_profit=196,
                body=(
                    "调整后毛利 196 百万元，已加回股权激励费用 28 百万元。"
                    "该口径不是 IFRS 口径，仅供内部管理使用。"
                ),
            ),
            Document(
                id="doc_fy2025_bridge",
                kind="basis_bridge",
                title="恒星智算 2025 年调整后口径至 IFRS 口径桥接表",
                period="FY2025",
                unit="CNY_1M",
                basis="reconciliation",
                revenue=1_120,
                gross_profit=168,
                bridge_delta=-28,
                bridge_reason="股权激励费用 28 百万元由营业成本转回期间费用",
                body=(
                    "调整后毛利 196 百万元，减股权激励费用 28 百万元，"
                    "等于 IFRS 口径毛利 168 百万元。差异全部来自股份支付的重分类。"
                ),
            ),
        ]
        docs = [doc for doc in docs if not (self.partial and doc.kind == "basis_bridge")]
        self.documents = {doc.id: doc for doc in docs}

    # ------------------------------------------------------------------ reads

    def list_documents(self) -> dict[str, Any]:
        return {
            "documents": [
                {
                    "id": doc.id,
                    "kind": doc.kind,
                    "period": doc.period,
                    "unit": doc.unit,
                    "unit_label": UNIT_LABEL[doc.unit],
                    "basis": doc.basis,
                    "title": doc.title,
                }
                for doc in self.documents.values()
            ]
        }

    def read_document(self, doc_id: str) -> dict[str, Any]:
        doc = self._require(doc_id)
        self.read_documents.add(doc_id)
        return {
            "id": doc.id,
            "title": doc.title,
            "kind": doc.kind,
            "period": doc.period,
            "unit": doc.unit,
            "unit_label": UNIT_LABEL[doc.unit],
            "basis": doc.basis,
            "revenue": doc.revenue,
            "gross_profit": doc.gross_profit,
            "supersedes": doc.supersedes,
            "body": doc.body,
        }

    def normalize_units(self, doc_id: str) -> dict[str, Any]:
        doc = self._require(doc_id)
        if doc.unit not in UNIT_SCALE:
            raise ValueError(f"unsupported unit {doc.unit!r}")
        scale = UNIT_SCALE[doc.unit]
        self.normalised.add(doc_id)
        return {
            "id": doc.id,
            "period": doc.period,
            "source_unit": doc.unit,
            "target_unit": "CNY",
            "revenue_cny": doc.revenue * scale,
            "gross_profit_cny": doc.gross_profit * scale,
            "factor": scale,
        }

    def reconcile_basis(self, doc_id: str) -> dict[str, Any]:
        doc = self._require(doc_id)
        bridge = next(
            (item for item in self.documents.values() if item.kind == "basis_bridge" and item.period == doc.period),
            None,
        )
        if bridge is None:
            # No published bridge. Report it rather than raising: a missing
            # explanation is a fact about the world, not an error in the agent.
            return {"period": doc.period, "reconciled": False, "reason": "该公司没有公布该期间的桥接说明"}
        self.reconciled.add(doc.period)
        return {
            "period": doc.period,
            "reconciled": True,
            "from_basis": doc.basis,
            "to_basis": "IFRS",
            "bridge_id": bridge.id,
            "delta": bridge.bridge_delta,
            "reason": bridge.bridge_reason,
            "reconciled_gross_profit": doc.gross_profit + bridge.bridge_delta,
        }

    def check_restatement(self, doc_id: str) -> dict[str, Any]:
        doc = self._require(doc_id)
        note = next(
            (item for item in self.documents.values() if item.supersedes == doc_id),
            None,
        )
        if note is None:
            return {"period": doc.period, "restated": False, "superseded_by": None}
        self.restatement_applied = True
        return {
            "period": doc.period,
            "restated": True,
            "superseded_by": note.id,
            "original_gross_profit": doc.gross_profit,
            "restated_gross_profit": note.gross_profit,
            "delta": note.gross_profit - doc.gross_profit,
            "reason": note.body,
        }

    # ------------------------------------------------------------ calculation

    def calculate_margin(self) -> dict[str, Any]:
        """Compute the two-period margin, or refuse with a precise reason.

        This is the deterministic part of the runtime. The model never gets to
        produce these numbers; it can only ask for them. Every precondition is
        checked here, so an incomplete evidence set produces a denial instead of
        a plausible-looking wrong answer.
        """
        blocked: list[str] = []
        if "doc_fy2024" not in self.read_documents:
            blocked.append("FY2024 annual report has not been read")
        if "doc_fy2025" not in self.read_documents:
            blocked.append("FY2025 annual report has not been read")
        if not self.restatement_applied:
            blocked.append("FY2024 restatement has not been checked")
        if "FY2025" not in self.reconciled:
            blocked.append("FY2025 adjusted basis has not been bridged to IFRS")
        if "doc_fy2024_restated" not in self.normalised or "doc_fy2025" not in self.normalised:
            blocked.append("both periods must be normalised to CNY before comparison")
        if blocked:
            return {"computed": False, "blockers": blocked}

        fy2024 = self._fact("doc_fy2024_restated", "FY2024")
        fy2025 = self._fact("doc_fy2025", "FY2025")
        margin_2024 = fy2024["gross_profit_cny"] * 10_000 // fy2024["revenue_cny"]
        margin_2025 = fy2025["gross_profit_cny"] * 10_000 // fy2025["revenue_cny"]
        return {
            "computed": True,
            "comparable": True,
            "basis": "IFRS",
            "consolidation": "entity-total",
            "restatement_applied": True,
            "periods": [fy2024, fy2025],
            "margin_bp_2024": margin_2024,
            "margin_bp_2025": margin_2025,
            "change_bp": margin_2025 - margin_2024,
            "citations": [
                {
                    "doc_id": fy2024["doc_id"],
                    "period": fy2024["period"],
                    "value_cny": fy2024["gross_profit_cny"],
                },
                {
                    "doc_id": fy2025["doc_id"],
                    "period": fy2025["period"],
                    "value_cny": fy2025["gross_profit_cny"],
                },
            ],
        }

    def _fact(self, doc_id: str, period: str) -> dict[str, Any]:
        doc = self._require(doc_id)
        scale = UNIT_SCALE[doc.unit]
        return {
            "fact_id": f"fact:{period}:gross_profit",
            "doc_id": doc.id,
            "period": period,
            "basis": doc.basis,
            "revenue_cny": doc.revenue * scale,
            "gross_profit_cny": doc.gross_profit * scale,
        }

    # --------------------------------------------------------------- blockers

    def report_blocked(self, reason: str) -> dict[str, Any]:
        self.blockers.append({"reason": reason})
        return {"acknowledged": True, "reason": reason, "blocker_count": len(self.blockers)}

    # ----------------------------------------------------------- resumption

    def apply_state(self, state: dict[str, Any]) -> None:
        """Rebuild the prepared-evidence flags from a recorded observation state.

        The world must not be the only place that remembers what has been read:
        a new process starts with an empty world, and without this the runtime
        would re-read documents it already has. Deriving the flags from the log
        keeps "what we know" in one place — the event chain.
        """
        self.read_documents = set(state.get("read_documents", []))
        self.normalised = set(state.get("normalised_documents", []))
        self.reconciled = set(state.get("reconciled_periods", []))
        self.restatement_applied = bool(state.get("restatement_applied"))

    # ------------------------------------------------------------------ utils

    def _require(self, doc_id: str) -> Document:
        doc = self.documents.get(doc_id)
        if doc is None:
            raise ValueError(f"unknown document {doc_id!r}")
        return doc

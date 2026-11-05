"""Deterministic fact extraction for the finance domain.

Only committed ``tool_succeeded`` observations are read. No model, no NLP, no
inference: if a number is not present in a signed result, it cannot become a
fact. The extractor is versioned, and every fact carries the event hash it came
from so the fact book can be re-derived from the chain.

Subject extraction is deliberately narrow: the synthetic corpus puts the
company name at the head of a document title (``恒星智算 2024 年年度报告``).
A corpus without that convention yields ``unknown`` rather than a guess.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ...core.facts import logical_key, make_fact

__all__ = ["EXTRACTOR_VERSION", "project_facts", "main"]

EXTRACTOR_VERSION = "finance-facts/v1"

_YEAR = re.compile(r"(?:FY)?(20\d{2})", re.IGNORECASE)
_TITLE_SUBJECT = re.compile(r"^([^\s，,：:（）()0-9]+)\s+(?:19|20)\d{2}\s*年")


def _subject_from_documents(documents: dict[str, dict[str, Any]]) -> str:
    for document in documents.values():
        match = _TITLE_SUBJECT.match(str(document.get("title") or ""))
        if match:
            return match.group(1)
    return "unknown"


def _period_of(period: str) -> str:
    return period if _YEAR.search(period or "") else ""


def project_facts(trace: dict[str, Any], *, run_id: str) -> list[dict[str, Any]]:
    """Turn one terminal trace into fact assertions. Pure function."""
    events = trace.get("events") or []
    documents: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "tool_succeeded":
            continue
        detail = event["payload"].get("detail") or {}
        if detail.get("tool") != "list_documents":
            continue
        for document in (detail.get("result") or {}).get("documents") or []:
            if document.get("id"):
                documents[str(document["id"])] = document

    subject = _subject_from_documents(documents)
    facts: list[dict[str, Any]] = []

    def emit(
        event: dict[str, Any],
        detail: dict[str, Any],
        predicate: str,
        period: str,
        *,
        value_int: int | None = None,
        value_text: str | None = None,
        unit: str = "",
        doc_id: str = "",
        basis: str = "",
    ) -> None:
        period = _period_of(period)
        entities = [subject, period, doc_id, basis]
        facts.append(make_fact(
            run_id=run_id,
            subject=subject,
            predicate=predicate,
            period=period,
            value_int=value_int,
            value_text=value_text,
            unit=unit,
            recorded_at=str(event.get("at") or ""),
            source_event=int(event.get("sequence") or 0),
            source_hash=str(event.get("hash") or ""),
            source_result_hash=str(detail.get("result_hash") or ""),
            entities=[entity for entity in entities if entity],
            extractor_version=EXTRACTOR_VERSION,
        ))

    for event in events:
        if event.get("type") != "tool_succeeded":
            continue
        detail = event["payload"].get("detail") or {}
        tool = detail.get("tool")
        result = detail.get("result") or {}
        if not tool:
            continue
        if tool == "read_document":
            period = str(result.get("period") or "")
            unit = str(result.get("unit") or "")
            doc_id = str(result.get("id") or "")
            if unit:
                emit(event, detail, "document_unit", period, value_text=unit, unit="text", doc_id=doc_id)
            if result.get("basis"):
                emit(event, detail, "document_basis", period,
                     value_text=str(result["basis"]), unit="text", doc_id=doc_id)
            if isinstance(result.get("revenue"), int):
                emit(event, detail, "reported_revenue", period,
                     value_int=result["revenue"], unit=unit, doc_id=doc_id)
            if isinstance(result.get("gross_profit"), int):
                emit(event, detail, "reported_gross_profit", period,
                     value_int=result["gross_profit"], unit=unit, doc_id=doc_id)
        elif tool == "normalize_units":
            period = str(result.get("period") or "")
            doc_id = str(result.get("id") or "")
            if isinstance(result.get("revenue_cny"), int):
                emit(event, detail, "revenue_cny", period,
                     value_int=result["revenue_cny"], unit="CNY", doc_id=doc_id)
            if isinstance(result.get("gross_profit_cny"), int):
                emit(event, detail, "gross_profit_cny", period,
                     value_int=result["gross_profit_cny"], unit="CNY", doc_id=doc_id)
        elif tool == "check_restatement":
            period = str(result.get("period") or "")
            doc_id = str(result.get("superseded_by") or "")
            if isinstance(result.get("delta"), int):
                emit(event, detail, "restatement_delta_cny", period,
                     value_int=result["delta"], unit="CNY", doc_id=doc_id)
            if result.get("restated") and isinstance(result.get("restated_gross_profit"), int):
                emit(event, detail, "restated_gross_profit_cny", period,
                     value_int=result["restated_gross_profit"], unit="CNY", doc_id=doc_id)
        elif tool == "reconcile_basis":
            period = str(result.get("period") or "")
            doc_id = str(result.get("bridge_id") or "")
            if isinstance(result.get("reconciled_gross_profit"), int):
                emit(event, detail, "reconciled_gross_profit_cny", period,
                     value_int=result["reconciled_gross_profit"], unit="CNY", doc_id=doc_id)
            if isinstance(result.get("delta"), int):
                emit(event, detail, "basis_bridge_delta_cny", period,
                     value_int=result["delta"], unit="CNY", doc_id=doc_id)
        elif tool == "calculate_margin" and result.get("computed"):
            periods = [str(item.get("period") or "") for item in result.get("periods") or []]
            for item in result.get("periods") or []:
                period = str(item.get("period") or "")
                doc_id = str(item.get("doc_id") or "")
                if isinstance(item.get("revenue_cny"), int):
                    emit(event, detail, "revenue_cny", period,
                         value_int=item["revenue_cny"], unit="CNY", doc_id=doc_id)
                if isinstance(item.get("gross_profit_cny"), int):
                    emit(event, detail, "gross_profit_cny", period,
                         value_int=item["gross_profit_cny"], unit="CNY", doc_id=doc_id)
            for key, period in (("margin_bp_2024", "FY2024"), ("margin_bp_2025", "FY2025")):
                if isinstance(result.get(key), int):
                    emit(event, detail, "gross_margin_bp", period,
                         value_int=result[key], unit="bp")
            if isinstance(result.get("change_bp"), int):
                emit(event, detail, "margin_change_bp", periods[-1] if periods else "",
                     value_int=result["change_bp"], unit="bp")

    # Within one run, the same claim observed twice is one assertion; a
    # different value is kept so the store can record an intra-run correction.
    unique: dict[tuple, dict[str, Any]] = {}
    for fact in facts:
        signature = (
            logical_key(fact["subject"], fact["predicate"], fact["period"]),
            fact["value_int"], fact["value_text"], fact["unit"],
        )
        unique[signature] = fact
    return sorted(unique.values(), key=lambda item: (item["source_event"], item["predicate"]))


def _load_traces(repo: Path) -> list[tuple[str, dict[str, Any]]]:
    """Read signed traces, ordered by when each run started.

    Fact supersession is history, so rebuild order is not cosmetic: replaying a
    later correction before its predecessor would invert the chain.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for meta_path in sorted((repo / "flowcredit" / "viewer" / "runs").glob("run-*/meta.json")):
        trace_path = meta_path.parent / "trace.json"
        if not trace_path.exists():
            continue
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        events = trace.get("events") or []
        started = str(events[0].get("at") or "") if events else ""
        found.append((started, meta_path.parent.name, trace))
    for trace_path in sorted((repo / "flowcredit" / "viewer" / "traces").glob("*.trace.json")):
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        events = trace.get("events") or []
        started = str(events[0].get("at") or "") if events else ""
        run_id = str((trace.get("task") or {}).get("id") or trace_path.stem)
        found.append((started, run_id, trace))
    found.sort(key=lambda item: (item[0], item[1]))
    return [(run_id, trace) for _, run_id, trace in found]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowagent-finance-facts",
        description="Rebuild the bi-temporal fact book from signed traces.",
    )
    parser.add_argument("--repo", required=True, help="FCode checkout")
    parser.add_argument("--db", default="", help="memory database path (default: viewer/memory/cases.sqlite)")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    db_path = Path(args.db) if args.db else repo / "flowcredit" / "viewer" / "memory" / "cases.sqlite"
    # Persistence is the CLI's concern; the extraction path above is pure and
    # must not pull the memory layer into the capability package.
    from ...memory.facts import FactStore

    store = FactStore(db_path)
    total = 0
    for run_id, trace in _load_traces(repo):
        store.upsert_run(run_id=run_id, facts=project_facts(trace, run_id=run_id))
        total += 1
    print(f"replayed {total} runs -> {store.count()} facts in {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

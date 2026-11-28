"""Intent-router evaluation over the shared fixture.

The router's job is to keep greetings out of runs, keep explicit tasks on the
direct path, and route ambiguous execution-tendency input to a clarify page
instead of a run. This suite scores exactly those failures on the versioned
fixture (``fixtures/intent-router/cases.json``) that both the Python router and
the offline frontend rules are pinned against:

* ``chat_misrouted_to_run`` — chat cases that landed in ``work`` (the costly
  failure: a greeting burning a governed run);
* ``work_missed_into_chat`` — explicit tasks that fell into chat;
* ``clarify`` rates, share and safe-fallback behaviour for the low-confidence
  bucket (a fallback may land in chat/clarify, never in ``work``);
* the classifier's extra latency and call cost, which is zero for the
  deterministic router and measured only when a classifier is supplied.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..intent_router import ROUTE_CHAT, ROUTE_CLARIFY, ROUTE_WORK, intent_router_version, route_intent

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = PROJECT_ROOT / "fixtures" / "intent-router" / "cases.json"


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else FIXTURE_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    return list(payload.get("cases") or [])


def evaluate_router(
    *,
    classifier: Callable[[str], dict[str, Any] | None] | None = None,
    model_name: str | None = None,
    cases_path: str | Path | None = None,
) -> dict[str, Any]:
    cases = load_cases(cases_path)
    rows: list[dict[str, Any]] = []
    classifier_latency_ms: list[int] = []
    for case in cases:
        context = case.get("context") or {}
        started = time.monotonic()
        decision = route_intent(
            case.get("message") or "", context,
            classifier=classifier, model_name=model_name,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if decision.get("via") == "model":
            classifier_latency_ms.append(elapsed_ms)
        expected = case.get("expected_route")
        rows.append({
            "case_id": case.get("id"),
            "expected_route": expected,
            "actual_route": decision["route"],
            "correct": decision["route"] == expected,
            "via": decision.get("via"),
            "reason": decision.get("reason"),
            "certainty": decision.get("certainty"),
            "classifier_error": decision.get("classifier_error"),
            "has_context": bool(context),
        })

    def selected(route: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["expected_route"] == route]

    def routed(route: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["actual_route"] == route]

    chat_cases = selected(ROUTE_CHAT)
    work_cases = selected(ROUTE_WORK)
    clarify_cases = selected(ROUTE_CLARIFY)
    low_confidence = [row for row in rows if row["certainty"] == "low"
                      or (row["via"] == "fallback" and row["certainty"] != "model")]

    def ratio(numerator: int, denominator: int) -> dict[str, Any]:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "value": (numerator / denominator) if denominator else None,
            "status": "MEASURED" if denominator else "NOT_MEASURED",
        }

    metrics = {
        "accuracy": ratio(sum(1 for row in rows if row["correct"]), len(rows)),
        "chat_misrouted_to_run": {
            "count": sum(1 for row in chat_cases if row["actual_route"] == ROUTE_WORK),
            "case_ids": [row["case_id"] for row in chat_cases
                         if row["actual_route"] == ROUTE_WORK],
        },
        "work_missed_into_chat": {
            "count": sum(1 for row in work_cases if row["actual_route"] == ROUTE_CHAT),
            "case_ids": [row["case_id"] for row in work_cases
                         if row["actual_route"] == ROUTE_CHAT],
        },
        "clarify_expected_recall": ratio(
            sum(1 for row in clarify_cases if row["actual_route"] == ROUTE_CLARIFY),
            len(clarify_cases),
        ),
        "clarify_share": ratio(len(routed(ROUTE_CLARIFY)), len(rows)),
        "clarify_precision": ratio(
            sum(1 for row in routed(ROUTE_CLARIFY) if row["expected_route"] == ROUTE_CLARIFY),
            len(routed(ROUTE_CLARIFY)),
        ),
        "safe_fallback_rate": ratio(
            sum(1 for row in low_confidence if row["actual_route"] in (ROUTE_CHAT, ROUTE_CLARIFY)),
            len(low_confidence),
        ),
        "low_confidence_count": len(low_confidence),
        "classifier_extra": {
            "calls": len(classifier_latency_ms),
            "latency_ms_p50": (
                sorted(classifier_latency_ms)[len(classifier_latency_ms) // 2]
                if classifier_latency_ms else 0
            ),
            "latency_ms_max": max(classifier_latency_ms) if classifier_latency_ms else 0,
            "model": model_name,
        },
        "router_version": intent_router_version(),
    }
    return {"rows": rows, "metrics": metrics}


def summary_markdown(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    def ratio_cell(metric: dict[str, Any]) -> str:
        return ("NOT_MEASURED" if metric["value"] is None
                else f"{metric['numerator']}/{metric['denominator']} = {metric['value']:.3f}")

    lines = [
        "# Intent-router evaluation",
        "",
        f"- Router: `{metrics['router_version']}` · cases: {len(report['rows'])}",
        f"- Accuracy: {ratio_cell(metrics['accuracy'])}",
        f"- chat 误创建 Run: **{metrics['chat_misrouted_to_run']['count']}**"
        f" {metrics['chat_misrouted_to_run']['case_ids'] or ''}",
        f"- work 误落入 Chat: **{metrics['work_missed_into_chat']['count']}**"
        f" {metrics['work_missed_into_chat']['case_ids'] or ''}",
        f"- clarify recall: {ratio_cell(metrics['clarify_expected_recall'])} · "
        f"share: {ratio_cell(metrics['clarify_share'])} · "
        f"precision: {ratio_cell(metrics['clarify_precision'])}",
        f"- safe fallback (low-confidence never becomes a run): "
        f"{ratio_cell(metrics['safe_fallback_rate'])}",
        f"- classifier extra: {metrics['classifier_extra']['calls']} calls · "
        f"p50 {metrics['classifier_extra']['latency_ms_p50']} ms · "
        f"max {metrics['classifier_extra']['latency_ms_max']} ms · "
        f"model `{metrics['classifier_extra']['model']}`",
        "",
        "| Case | Expected | Actual | Correct | Via | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        lines.append(
            f"| `{row['case_id']}` | {row['expected_route']} | {row['actual_route']} | "
            f"{'✅' if row['correct'] else '❌'} | {row['via']} | {row['reason']} |"
        )
    lines.append("")
    return "\n".join(lines)

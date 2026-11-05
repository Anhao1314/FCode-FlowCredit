"""Long-term case memory: terminal traces become a searchable case book.

The design is deliberately different from Mem0/Letta-style memory layers: this
runtime already has structured, hash-chained evidence, so nothing needs to be
"extracted" by a model. A case is a **deterministic projection** of a signed
trace — objective, module, outcome, cost, citations, artifacts, chain head —
which means the memory cannot hallucinate and can be rebuilt byte-for-byte.

Retrieval is explainable, not a black-box similarity: relevance (keyword and
tag overlap), importance (verified outcome, human involvement) and recency
(90-day half-life). Forgetting only lowers rank; evidence is never deleted —
deleting a run tombstones its case instead.

Storage is one SQLite file, rebuildable from `runs/*/trace.json`. No vectors,
no graph database, no third-party dependency.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = ["CaseRecord", "CaseIndex", "project_case", "default_db_path"]

HUMAN_EVENTS = {
    "approval_requested", "approval_granted", "approval_denied", "approval_aborted",
    "plan_steered", "budget_adjusted", "control_rejected", "task_resumed",
    "operator_message", "review_decided",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    run_id TEXT PRIMARY KEY,
    chain_head TEXT NOT NULL,
    module TEXT NOT NULL,
    template TEXT NOT NULL,
    objective TEXT NOT NULL,
    outcome TEXT NOT NULL,
    verification TEXT NOT NULL,
    rounds INTEGER NOT NULL,
    cost_microusd INTEGER NOT NULL,
    human_decisions INTEGER NOT NULL,
    key_citations TEXT NOT NULL,
    artifacts TEXT NOT NULL,
    tags TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cases_outcome ON cases(outcome);
CREATE INDEX IF NOT EXISTS idx_cases_module ON cases(module);
"""


def default_db_path() -> Path:
    import os

    override = os.environ.get("FCODE_MEMORY_DB")
    if override:
        return Path(override)
    return Path.cwd() / ".fcode-memory" / "cases.sqlite"


# ------------------------------------------------------------------ projection

def _objective_tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_\-]*", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(run) == 1:
            tokens.add(run)
        for index in range(len(run) - 1):
            tokens.add(run[index:index + 2])
    return tokens


def _summary(trace: dict[str, Any]) -> str:
    terminal = trace.get("terminal") or {}
    result = terminal.get("result") or {}
    failure = terminal.get("failure") or {}
    if terminal.get("status") == "succeeded" and result.get("change_bp") is not None:
        change = result["change_bp"]
        direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
        amount = "" if change == 0 else f" {abs(change) / 100:.2f} 个百分点"
        return (
            f"两期毛利率可比：FY2024 {result.get('margin_bp_2024', 0) / 100:.2f}%，"
            f"FY2025 {result.get('margin_bp_2025', 0) / 100:.2f}%，{direction}{amount}。"
        )
    if terminal.get("status") == "succeeded":
        return "任务完成，结果通过 Runtime 独立复算。"
    reason = failure.get("code") or failure.get("message") or "运行未完成"
    return f"未能给出结论：{reason}。"


def project_case(trace: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Derive one case from a trace. Pure function: same input, same output."""
    task = trace.get("task") or {}
    binding = task.get("binding") or {}
    terminal = trace.get("terminal") or {}
    result = terminal.get("result") or {}
    events = trace.get("events") or []
    rounds = trace.get("rounds") or []
    chain = trace.get("chain") or {}

    citations: list[str] = []
    for citation in result.get("citations") or []:
        doc_id = citation.get("doc_id") if isinstance(citation, dict) else None
        if doc_id:
            citations.append(str(doc_id))
    if not citations:
        for round_ in rounds:
            observation = round_.get("observation") or {}
            details = observation.get("arguments") or {}
            if observation.get("tool") == "read_document" and observation.get("result", {}).get("id"):
                citations.append(str(observation["result"]["id"]))
            elif details.get("doc_id"):
                citations.append(str(details["doc_id"]))

    artifacts: list[str] = []
    used_tools: set[str] = set()
    for round_ in rounds:
        observation = round_.get("observation") or {}
        tool = observation.get("tool")
        if not tool:
            continue
        used_tools.add(str(tool))
        if tool == "working_paper_append":
            artifact = (observation.get("result") or {}).get("artifact_id")
            if artifact:
                artifacts.append(str(artifact))

    modules = str(binding.get("module") or "finance-research")
    template = str(binding.get("scenario") or "")
    outcome = str(terminal.get("status") or "unknown")
    periods = sorted({
        match.upper()
        for match in re.findall(
            r"(?i)FY\d{4}|20\d{2}",
            " ".join([task.get("objective", ""), " ".join(citations)]),
        )
    })
    tags = sorted({
        modules,
        template,
        outcome,
        *periods,
        *citations,
        *sorted(used_tools),
    } - {""})

    cost = 0
    for round_ in reversed(rounds):
        budget = round_.get("budget_after") or {}
        if budget.get("cost_used_microusd") is not None:
            cost = int(budget["cost_used_microusd"])
            break

    human_decisions = sum(1 for event in events if event.get("type") in HUMAN_EVENTS)
    created = (events[0].get("at") if events else "") or ""

    return {
        "run_id": run_id,
        "chain_head": str(chain.get("head_hash") or ""),
        "module": modules,
        "template": template,
        "objective": str(task.get("objective") or ""),
        "outcome": outcome,
        "verification": str(result.get("verification") or ""),
        "rounds": len(rounds),
        "cost_microusd": cost,
        "human_decisions": human_decisions,
        "key_citations": sorted(set(citations)),
        "artifacts": sorted(set(artifacts)),
        "tags": tags,
        "summary": _summary(trace),
        "created_at": created,
        "archived": 0,
    }


def _entity_tokens(tags: Iterable[str], citations: Iterable[str], artifacts: Iterable[str]) -> set[str]:
    """Entities, not labels: documents, artifacts and periods.

    Module, template, outcome and tool tags describe the kind of work, not the
    thing the work is about. Including them would make every case adjacent to
    every other case and turn the score into noise.
    """
    tokens = {str(item) for item in (*citations, *artifacts) if item}
    for tag in tags:
        if re.fullmatch(r"(?i)(?:FY)?20\d{2}", str(tag)):
            tokens.add(str(tag))
    return tokens


# ---------------------------------------------------------------------- index

@dataclass(frozen=True)
class CaseRecord:
    """Typed view of a search hit. Scores are integer basis points."""

    run_id: str
    objective: str
    outcome: str
    summary: str
    chain_head: str
    tags: tuple[str, ...]
    score_bp: int
    breakdown: dict[str, int]


class CaseIndex:
    """SQLite-backed case book. Schema is created lazily on first use."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self._ready = False

    # ------------------------------------------------------------------ setup

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(SCHEMA)
            self._ready = True
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ write

    def upsert(self, trace: dict[str, Any], *, run_id: str) -> dict[str, Any]:
        case = project_case(trace, run_id=run_id)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT archived FROM cases WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                case["archived"] = int(existing["archived"])
            conn.execute(
                """INSERT OR REPLACE INTO cases
                (run_id, chain_head, module, template, objective, outcome, verification,
                 rounds, cost_microusd, human_decisions, key_citations, artifacts, tags,
                 summary, created_at, archived)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case["run_id"], case["chain_head"], case["module"], case["template"],
                    case["objective"], case["outcome"], case["verification"], case["rounds"],
                    case["cost_microusd"], case["human_decisions"],
                    json.dumps(case["key_citations"], ensure_ascii=False),
                    json.dumps(case["artifacts"], ensure_ascii=False),
                    json.dumps(case["tags"], ensure_ascii=False),
                    case["summary"], case["created_at"], case["archived"],
                ),
            )
        return case

    def tombstone(self, run_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE cases SET archived=1 WHERE run_id=?", (run_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------------- read

    def _rows(self, *, include_archived: bool) -> list[sqlite3.Row]:
        query = "SELECT * FROM cases" if include_archived else "SELECT * FROM cases WHERE archived=0"
        with self._connect() as conn:
            return list(conn.execute(query).fetchall())

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        query_tokens = _objective_tokens(query)
        now = datetime.now(timezone.utc)
        results: list[tuple[int, dict[str, Any]]] = []
        for row in self._rows(include_archived=include_archived):
            tags = json.loads(row["tags"])
            case_tokens = _objective_tokens(row["objective"] + " " + row["summary"])
            case_tokens.update(str(tag).lower() for tag in tags)
            relevance = (
                len(query_tokens & case_tokens) / len(query_tokens) if query_tokens else 0.0
            )
            importance = 0.4
            if row["outcome"] == "succeeded":
                importance += 0.3
            if row["verification"]:
                importance += 0.2
            if row["human_decisions"]:
                importance += 0.1
            importance = min(1.0, importance)
            try:
                created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                age_days = max(0.0, (now - created).total_seconds() / 86400.0)
            except ValueError:
                age_days = 365.0
            recency = math.pow(0.5, age_days / 90.0)
            score = 0.5 * relevance + 0.3 * importance + 0.2 * recency
            # Scores enter hash-chained observations, where floats are forbidden.
            # Basis points keep the ranking exact and replayable.
            score_bp = round(score * 10000)
            results.append((
                score_bp,
                {
                    "run_id": row["run_id"],
                    "objective": row["objective"],
                    "outcome": row["outcome"],
                    "summary": row["summary"],
                    "chain_head": row["chain_head"],
                    "tags": tags,
                    "created_at": row["created_at"],
                    "archived": bool(row["archived"]),
                    "score_bp": score_bp,
                    "breakdown": {
                        "relevance_bp": round(relevance * 10000),
                        "importance_bp": round(importance * 10000),
                        "recency_bp": round(recency * 10000),
                    },
                },
            ))
        results.sort(key=lambda item: (-item[0], item[1]["run_id"]))
        return [item[1] for item in results[: max(0, int(limit))]]

    def get(self, run_id: str, *, include_archived: bool = True) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cases WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        if row["archived"] and not include_archived:
            return None
        return {
            "run_id": row["run_id"],
            "chain_head": row["chain_head"],
            "module": row["module"],
            "template": row["template"],
            "objective": row["objective"],
            "outcome": row["outcome"],
            "verification": row["verification"],
            "rounds": row["rounds"],
            "cost_microusd": row["cost_microusd"],
            "human_decisions": row["human_decisions"],
            "key_citations": json.loads(row["key_citations"]),
            "artifacts": json.loads(row["artifacts"]),
            "tags": json.loads(row["tags"]),
            "summary": row["summary"],
            "created_at": row["created_at"],
            "archived": bool(row["archived"]),
        }

    def related(self, run_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Cases that share entities (documents, periods, artifacts) with this one.

        This is adjacency, not a graph database: the edges are the tags already
        recorded in the case, and the score is the Jaccard overlap in basis
        points. It gives a reviewer "what else touched these documents?" without
        introducing an embedding or a probabilistic link.
        """
        base = self.get(run_id, include_archived=True)
        if base is None:
            return []
        base_tokens = _entity_tokens(
            base["tags"], base["key_citations"], base["artifacts"],
        )
        if not base_tokens:
            return []
        results: list[tuple[int, dict[str, Any]]] = []
        for row in self._rows(include_archived=False):
            if row["run_id"] == run_id:
                continue
            tokens = _entity_tokens(
                json.loads(row["tags"]),
                json.loads(row["key_citations"]),
                json.loads(row["artifacts"]),
            )
            shared = base_tokens & tokens
            if not shared:
                continue
            score_bp = round(10000 * len(shared) / len(base_tokens | tokens))
            results.append((score_bp, {
                "run_id": row["run_id"],
                "objective": row["objective"],
                "outcome": row["outcome"],
                "summary": row["summary"],
                "score_bp": score_bp,
                "shared": sorted(shared),
            }))
        results.sort(key=lambda item: (-item[0], item[1]["run_id"]))
        return [item[1] for item in results[: max(0, int(limit))]]

    def count(self, *, include_archived: bool = False) -> int:
        query = "SELECT COUNT(*) AS n FROM cases" + ("" if include_archived else " WHERE archived=0")
        with self._connect() as conn:
            return int(conn.execute(query).fetchone()["n"])


# ------------------------------------------------------------------------ cli

def _load_traces(repo: Path) -> Iterable[tuple[str, dict[str, Any]]]:
    runs = repo / "flowcredit" / "viewer" / "runs"
    for meta_path in sorted(runs.glob("run-*/meta.json")):
        trace_path = meta_path.parent / "trace.json"
        if not trace_path.exists():
            continue
        try:
            yield meta_path.parent.name, json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    demo = repo / "flowcredit" / "viewer" / "traces"
    for trace_path in sorted(demo.glob("*.trace.json")):
        try:
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = (trace.get("task") or {}).get("id") or trace_path.stem
        yield str(task_id), trace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowagent-case-index",
        description="Rebuild the case book from signed traces.",
    )
    parser.add_argument("--repo", required=True, help="FCode checkout")
    parser.add_argument("--db", default="", help="case database path (default: viewer/memory/cases.sqlite)")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    db_path = Path(args.db) if args.db else repo / "flowcredit" / "viewer" / "memory" / "cases.sqlite"
    index = CaseIndex(db_path)
    total = 0
    for run_id, trace in _load_traces(repo):
        index.upsert(trace, run_id=run_id)
        total += 1
    print(f"indexed {total} cases -> {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

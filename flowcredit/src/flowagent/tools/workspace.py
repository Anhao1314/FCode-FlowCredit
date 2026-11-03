"""Workspace tools: capabilities every digital-economy module needs.

These are not finance tools and they are not audit tools. They are the shared
surface of the workbench — read evidence, search it, produce cited artifacts,
fetch a host-configured source — and every domain module composes them next to
its own toolset.

Keeping them here is what stops a second domain (audit, quant, …) from
re-implementing ingestion, or worse, from growing its own half-compatible copy
inside the kernel.
"""

from __future__ import annotations

from typing import Any

from .artifacts import ArtifactStore
from .base import EFFECT_NETWORK, EFFECT_READ, EFFECT_WRITE, Param, ToolSpec
from .connectors import ConnectorRegistry
from .evidence import EvidenceStore
from ..core.contracts import AgentError
from ..core.hashing import canonical_json
from ..memory.cases import CaseIndex
from ..memory.facts import FactStore

__all__ = ["SEARCH_SUMMARY_MAX_CHARS", "build_workspace_tools",
           "summarize_search_corpus"]


#: search_corpus 指针型压缩的输出字符上限:候选集可见,但不挤爆上下文预算。
SEARCH_SUMMARY_MAX_CHARS = 2_000


def summarize_search_corpus(result: dict[str, Any]) -> dict[str, Any]:
    """指针型压缩(P0-1.5):保留全部命中的证据指针,剔除 snippet 正文。

    正文可经 doc_id+line 重读,链上保留全量;prompt 里需要的是"有哪些
    命中、在哪"。超过 SEARCH_SUMMARY_MAX_CHARS 时按序保留前 N 条并显式
    标注截断(truncated_after/total)——不退化为只留第一条,也不静默丢。
    """
    raw_hits = list(result.get("hits") or [])
    pointers = [
        {"doc_id": hit.get("doc_id"), "path": hit.get("path"),
         "source": hit.get("source"), "line": hit.get("line")}
        for hit in raw_hits
    ]
    base: dict[str, Any] = {
        **{key: value for key, value in result.items()
           if key != "hits" and not isinstance(value, (list, dict))},
        "count": len(pointers),
    }

    def render(kept: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {**base, "hits": kept}
        if len(kept) < len(pointers):
            out["truncated_after"] = len(kept)
            out["total"] = len(pointers)
        return out

    kept: list[dict[str, Any]] = []
    for pointer in pointers:
        trial = kept + [pointer]
        if kept and len(canonical_json(render(trial))) > SEARCH_SUMMARY_MAX_CHARS:
            break
        kept = trial
    return render(kept)


def build_workspace_tools(
    evidence: EvidenceStore,
    artifacts: ArtifactStore,
    connectors: ConnectorRegistry,
    cases: CaseIndex | None = None,
    facts: FactStore | None = None,
) -> list[ToolSpec]:
    """Compose the shared tools bound to their stores."""

    def ingest_file(arguments: dict[str, Any]) -> dict[str, Any]:
        return evidence.ingest(arguments["path"])

    def search_corpus(arguments: dict[str, Any]) -> dict[str, Any]:
        return evidence.search(arguments["query"], int(arguments.get("limit") or 10))

    def read_slice(arguments: dict[str, Any]) -> dict[str, Any]:
        return evidence.read_slice(
            arguments["doc_id"],
            int(arguments.get("start") or 1),
            arguments.get("end"),
        )

    def working_paper_append(arguments: dict[str, Any]) -> dict[str, Any]:
        return artifacts.append_section(
            title=arguments["title"],
            body=arguments["body"],
            citations=list(arguments.get("citations") or []),
        )

    def fetch_source(arguments: dict[str, Any]) -> dict[str, Any]:
        return connectors.fetch(arguments["source"], arguments.get("key"))

    def collect_source(arguments: dict[str, Any]) -> dict[str, Any]:
        collected = connectors.collect(arguments["source"], arguments.get("query"))
        metadata = {
            key: collected[key]
            for key in ("source", "adapter", "url", "status", "content_type", "retrieved_at", "sha256", "bytes", "title")
            if key in collected
        }
        ingested = evidence.ingest_text(
            collected["text"],
            name=collected["source"],
            kind=collected["kind"],
            metadata=metadata,
        )
        preview = collected["text"][:400]
        return {
            "source": collected["source"],
            "adapter": collected.get("adapter", "http"),
            "url": collected["url"],
            "status": collected["status"],
            "content_type": collected["content_type"],
            "retrieved_at": collected["retrieved_at"],
            "sha256": collected["sha256"],
            "bytes": collected["bytes"],
            "doc_id": ingested["doc_id"],
            "kind": ingested["kind"],
            "lines": ingested["lines"],
            "title": collected.get("title", ""),
            "preview": preview,
            "truncated": len(collected["text"]) > len(preview),
        }

    index = cases or CaseIndex()
    fact_index = facts or FactStore()

    def search_cases(arguments: dict[str, Any]) -> dict[str, Any]:
        hits = index.search(
            arguments["query"],
            limit=int(arguments.get("limit") or 5),
        )
        return {"query": arguments["query"], "hits": hits, "count": len(hits)}

    def read_case(arguments: dict[str, Any]) -> dict[str, Any]:
        case = index.get(arguments["run_id"], include_archived=True)
        if case is None:
            raise AgentError("CASE_NOT_FOUND", f"no case indexed for {arguments['run_id']!r}")
        case["related_cases"] = index.related(arguments["run_id"], limit=3)
        return case

    def query_facts(arguments: dict[str, Any]) -> dict[str, Any]:
        rows = fact_index.query(
            subject=str(arguments.get("subject") or ""),
            predicate=str(arguments.get("predicate") or ""),
            period=str(arguments.get("period") or ""),
            as_of=str(arguments.get("as_of") or ""),
            known_at=str(arguments.get("known_at") or ""),
            include_superseded=bool(arguments.get("include_superseded")),
            limit=int(arguments.get("limit") or 20),
        )
        return {"facts": rows, "count": len(rows)}

    return [
        ToolSpec(
            name="ingest_file",
            summary_policy="detailed",
            summary="Ingest a text/CSV/JSON file inside the evidence sandbox; returns a content hash and line anchors",
            effect=EFFECT_READ,
            params=(
                Param(
                    name="path",
                    kind="string",
                    required=True,
                    description="path relative to the evidence root (absolute paths and .. are refused)",
                ),
            ),
            handler=ingest_file,
            outputs=(
                Param.output("doc_id", "string"),
                Param.output("kind", "string"),
                Param.output("size", "integer"),
                Param.output("sha256", "string"),
                Param.output("lines", "integer"),
                Param.output("rows", "integer", required=False, nullable=True),
                Param.output("columns", "array", required=False, nullable=True),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="search_corpus",
            summary_policy="custom",
            summary="Search ingested evidence by substring; returns hits with doc_id and line anchors",
            effect=EFFECT_READ,
            params=(
                Param(name="query", kind="string", required=True, description="case-insensitive substring"),
                Param(name="limit", kind="integer", required=False, minimum=1, maximum=50, description="max hits (default 10)"),
            ),
            handler=search_corpus,
            summarize=summarize_search_corpus,
            outputs=(
                Param.output("query", "string"),
                Param.output("hits", "array"),
                Param.output("scanned", "integer"),
                Param.output("truncated", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="read_slice",
            summary_policy="detailed",
            summary="Read a bounded line range from ingested evidence; whole documents are never dumped",
            effect=EFFECT_READ,
            params=(
                Param(name="doc_id", kind="string", required=True, description="doc_id returned by ingest_file"),
                Param(name="start", kind="integer", required=False, minimum=1, description="first line (1-based, default 1)"),
                Param(name="end", kind="integer", required=False, minimum=1, description="last line (inclusive; window capped at 80 lines)"),
            ),
            handler=read_slice,
            outputs=(
                Param.output("doc_id", "string"),
                Param.output("start", "integer"),
                Param.output("end", "integer"),
                Param.output("lines", "array"),
                Param.output("truncated", "boolean"),
                Param.output("total_lines", "integer"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="working_paper_append",
            summary_policy="detailed",
            summary="Append one cited section to the working paper; content-addressed and approval-gated",
            effect=EFFECT_WRITE,
            params=(
                Param(name="title", kind="string", required=True, description="section heading"),
                Param(name="body", kind="string", required=True, identity=False,
                      description="section body, max 8000 characters"),
                Param(
                    name="citations",
                    kind="array",
                    required=False,
                    maximum=20,
                    description="doc anchors such as doc_fy2025#L12 or evidence:<hash>#L3",
                ),
            ),
            handler=working_paper_append,
            outputs=(
                Param.output("artifact_id", "string"),
                Param.output("path", "string"),
                Param.output("sha256", "string"),
                Param.output("bytes", "integer"),
                Param.output("section", "string"),
                Param.output("citations", "integer"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="search_cases",
            summary_policy="detailed",
            summary="Search past delegations for similar work; returns explainable scores",
            effect=EFFECT_READ,
            params=(
                Param(name="query", kind="string", required=True, description="keywords or an objective"),
                Param(name="limit", kind="integer", required=False, minimum=1, maximum=20, description="max cases (default 5)"),
            ),
            handler=search_cases,
            outputs=(
                Param.output("query", "string"),
                Param.output("hits", "array"),
                Param.output("count", "integer"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="read_case",
            summary_policy="detailed",
            summary="Read one indexed case: summary, citations, artifacts and chain head",
            effect=EFFECT_READ,
            params=(
                Param(name="run_id", kind="string", required=True, description="run id from search_cases"),
            ),
            handler=read_case,
            outputs=(
                Param.output("run_id", "string"),
                Param.output("chain_head", "string"),
                Param.output("module", "string"),
                Param.output("template", "string"),
                Param.output("objective", "string"),
                Param.output("outcome", "string"),
                Param.output("verification", "string"),
                Param.output("rounds", "integer"),
                Param.output("cost_microusd", "integer"),
                Param.output("human_decisions", "integer"),
                Param.output("key_citations", "array"),
                Param.output("artifacts", "array"),
                Param.output("tags", "array"),
                Param.output("summary", "string"),
                Param.output("created_at", "string"),
                Param.output("archived", "boolean"),
                Param.output("related_cases", "array"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="query_facts",
            summary_policy="detailed",
            summary="Query bi-temporal facts (as-of and known-at) with provenance",
            effect=EFFECT_READ,
            params=(
                Param(name="subject", kind="string", required=False, description="entity, e.g. 恒星智算"),
                Param(name="predicate", kind="string", required=False, description="e.g. gross_margin_bp"),
                Param(name="period", kind="string", required=False, description="fiscal period, e.g. FY2024"),
                Param(name="as_of", kind="string", required=False, description="valid-time date, YYYY-MM-DD"),
                Param(name="known_at", kind="string", required=False, description="transaction-time cutoff, ISO-8601"),
                Param(name="include_superseded", kind="boolean", required=False, description="return correction history"),
                Param(name="limit", kind="integer", required=False, minimum=1, maximum=50, description="max facts (default 20)"),
            ),
            handler=query_facts,
            outputs=(
                Param.output("facts", "array"),
                Param.output("count", "integer"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="collect_source",
            summary_policy="detailed",
            summary="Collect a host-configured source, extract text, and ingest it as cited evidence",
            effect=EFFECT_NETWORK,
            params=(
                Param(
                    name="source",
                    kind="string",
                    required=True,
                    enum=connectors.names(),
                    description="configured source name",
                ),
                Param(
                    name="query",
                    kind="string",
                    required=False,
                    description="optional lookup term appended as the source's query parameter",
                ),
            ),
            handler=collect_source,
            outputs=(
                Param.output("source", "string"),
                Param.output("adapter", "string"),
                Param.output("url", "string"),
                Param.output("status", "integer"),
                Param.output("content_type", "string"),
                Param.output("retrieved_at", "string"),
                Param.output("sha256", "string"),
                Param.output("bytes", "integer"),
                Param.output("doc_id", "string"),
                Param.output("kind", "string"),
                Param.output("lines", "integer"),
                Param.output("title", "string"),
                Param.output("preview", "string"),
                Param.output("truncated", "boolean"),
            ),
            version="1.0",
        ),
        ToolSpec(
            name="fetch_source",
            summary_policy="detailed",
            summary="Fetch a host-configured external source by name; endpoint and credentials stay with the host",
            effect=EFFECT_NETWORK,
            params=(
                Param(
                    name="source",
                    kind="string",
                    required=True,
                    enum=connectors.names(),
                    description="configured source name",
                ),
                Param(
                    name="key",
                    kind="string",
                    required=False,
                    description="optional lookup key appended as a query parameter",
                ),
            ),
            handler=fetch_source,
            outputs=(
                Param.output("source", "string"),
                Param.output("url", "string"),
                Param.output("status", "integer"),
                Param.output("content_type", "string"),
                Param.output("sha256", "string"),
                Param.output("bytes", "integer"),
                Param.output("retrieved_at", "string"),
                Param.output("payload", "object"),
                Param.output("request_id", "string"),
                Param.output("latency_ms", "integer"),
            ),
            version="1.0",
        ),
    ]

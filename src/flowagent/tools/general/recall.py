"""recall_observation: governed re-fetch of a folded observation.

The budgeted window folds old rounds into one-line index rows. When the model
actually needs one of them back, it must ask — through the same gate, budget
and chain as any other read. This tool is that ask.

Contract:

* ``round`` is optional and ``tool``/``contains`` are optional filters; the
  result is a capped list of the *compressed* summaries plus their full
  result hashes. It never returns a raw tool result — the original record
  stays in the store and stays the only source of truth.
* The tool is a pure read over a snapshot the host hands it at assembly time
  (the run's own observation log). It cannot reach other runs, the filesystem
  or the network.
* The recall lands in the chain as an ordinary ``tool_succeeded`` observation,
  so the next round's context shows it and the audit trail answers "what did
  the model pull back and why" without any new mechanism.
"""

from __future__ import annotations

from typing import Any, Callable

from ...core.contracts import AgentError
from ..base import EFFECT_READ, Param, ToolSpec

__all__ = ["build_recall_tool", "summarize_recall"]

#: Cap on returned rows: a recall is a rescue line, not a bulk dump.
MAX_HITS = 5


def summarize_recall(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "hits": [
            {
                "round": hit.get("round"),
                "tool": hit.get("tool"),
                "result_hash": hit.get("result_hash"),
            }
            for hit in result.get("hits", [])
        ],
        "count": result.get("count", 0),
        "not_found": result.get("not_found", ""),
    }


def build_recall_tool(records_provider: Callable[[], list[dict[str, Any]]]) -> ToolSpec:
    """Build the recall tool bound to this run's observation log.

    ``records_provider`` returns the run's compressed observation records as
    plain dicts (round, tool, arguments, summary, result_hash) — the same
    projection working memory holds. It is called at execution time, so the
    tool sees exactly what has been committed so far.
    """

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        records = records_provider() or []
        round_no = arguments.get("round")
        tool = (arguments.get("tool") or "").strip()
        contains = (arguments.get("contains") or "").strip().lower()

        if round_no is None and not tool and not contains:
            raise AgentError(
                "RECALL_QUERY_EMPTY",
                "give a round, a tool name, or a contains substring — an empty recall is a bulk dump",
            )

        def matches(record: dict[str, Any]) -> bool:
            if round_no is not None and int(record.get("round") or 0) != int(round_no):
                return False
            if tool and record.get("tool") != tool:
                return False
            if contains:
                haystack = " ".join(
                    str(part).lower()
                    for part in (
                        record.get("tool"),
                        record.get("arguments"),
                        record.get("summary"),
                    )
                )
                if contains not in haystack:
                    return False
            return True

        hits = [record for record in records if matches(record)]
        if not hits:
            return {
                "hits": [],
                "count": 0,
                "not_found": "no committed observation matches the query",
            }
        if len(hits) > MAX_HITS:
            hits = sorted(hits, key=lambda r: int(r.get("round") or 0))[:MAX_HITS]
        return {
            "hits": [
                {
                    "round": record.get("round"),
                    "tool": record.get("tool"),
                    "arguments": record.get("arguments") or {},
                    "summary": record.get("summary") or {},
                    "result_hash": record.get("result_hash") or "",
                }
                for record in hits
            ],
            "count": len(hits),
            "not_found": "",
        }

    return ToolSpec(
        name="recall_observation",
        summary=(
            "Fetch a folded observation back into context: give a round number, "
            "a tool name, or a substring; returns the compressed summary and "
            "its result hash (never a raw result)"
        ),
        effect=EFFECT_READ,
        params=(
            Param(
                name="round",
                kind="integer",
                required=False,
                description="the round to fetch back",
            ),
            Param(
                name="tool",
                kind="string",
                required=False,
                description="filter by tool name",
            ),
            Param(
                name="contains",
                kind="string",
                required=False,
                max_length=200,
                description="substring to match against tool, arguments or summary",
            ),
        ),
        handler=handler,
        outputs=(
            Param(name="hits", kind="array"),
            Param(name="count", kind="integer"),
            Param(name="not_found", kind="string"),
        ),
        version="1.0",
        summarize=summarize_recall,
    )

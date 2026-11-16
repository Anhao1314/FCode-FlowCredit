"""CLI for the LangGraph three-agent pilot.

Examples:

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python "langgraph>=1.0,<2"
    .venv/bin/python -m labs.langgraph_swarm.cli --target fixtures/demo-target --auto-approve

The process pauses at ``interrupt()`` when the approval gate is reached. With
``--auto-approve`` it resumes in the same process (MemorySaver checkpoint); with
``--interactive`` it asks for y/N. Without either, it prints the pending
approval and exits 2 — the in-memory checkpoint is process-local by design.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from labs.langgraph_swarm.workflow import (  # noqa: E402
    HAS_LANGGRAPH,
    Manifest,
    _config,
    build_graph,
    initial_state,
    resume_swarm,
)

__all__ = ["main"]


def _interrupt_payload(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value")
    return value if isinstance(value, dict) else {"raw": str(value)}


def _summary(result: dict[str, Any], run_id: str, run_dir: Path) -> dict[str, Any]:
    manifest = Manifest(run_dir / "manifest.jsonl")
    return {
        "run_id": run_id,
        "status": result.get("status"),
        "audit": {
            "audit_id": (result.get("audit") or {}).get("audit_id"),
            "findings": len((result.get("audit") or {}).get("findings") or []),
            "ruleset_version": (result.get("audit") or {}).get("ruleset_version"),
        },
        "plan": result.get("plan") or {},
        "approvals": result.get("approvals") or [],
        "verification": result.get("verification") or {},
        "handoffs": result.get("handoffs") or [],
        "manifest": manifest.verify(),
        "run_dir": str(run_dir),
        "thread_id": run_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fcode-langgraph-swarm")
    parser.add_argument("--target", default="fixtures/demo-target")
    parser.add_argument("--out", default="")
    parser.add_argument("--thread", default="")
    parser.add_argument("--auto-approve", action="store_true")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)

    if not HAS_LANGGRAPH:
        print(json.dumps({
            "error": "langgraph is not installed",
            "hint": "uv pip install --python .venv/bin/python 'langgraph>=1.0,<2'",
        }, ensure_ascii=False))
        return 2

    run_id = args.thread or f"lg-{uuid.uuid4().hex[:12]}"
    run_dir = Path(args.out).expanduser().resolve() if args.out \
        else ROOT / "labs" / "langgraph_swarm" / ".runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    app = build_graph()
    result = app.invoke(initial_state(run_id, args.target, run_dir), config=_config(run_id))
    pending = _interrupt_payload(result)

    if pending:
        if args.interactive and not args.auto_approve and not args.reject:
            answer = input(f"{pending.get('question', 'approve handoff?')} [y/N] ").strip().lower()
            approved = answer in ("y", "yes", "是")
        elif args.reject:
            approved = False
        elif args.auto_approve:
            approved = True
        else:
            summary = _summary(result, run_id, run_dir)
            summary["pending_approval"] = pending
            (run_dir / "result.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print("use --auto-approve, --reject or --interactive to resume in-process",
                  file=sys.stderr)
            return 2
        result = resume_swarm(app, run_id, approved)

    summary = _summary(result, run_id, run_dir)
    (run_dir / "result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "verified" and summary["manifest"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
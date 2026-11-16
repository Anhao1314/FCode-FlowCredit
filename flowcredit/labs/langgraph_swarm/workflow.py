"""A real LangGraph three-agent pilot: auditor -> planner -> verifier.

This lab is deliberately isolated from the production ``GovBotService``. It
answers one question: what does LangGraph give us that our hand-rolled run
loop does not? The graph owns orchestration, the approval interrupt and the
checkpoint; the manifest remains the audit truth and every handoff is hashed.

Flow:

    START -> auditor -> approval --(approved)--> planner -> verifier -> END
                         |
                         +--(rejected)--> END

The ``approval`` node pauses with ``interrupt()``. A CLI run can resume with
``Command(resume={"approved": True})``. ``MemorySaver`` checkpoints the graph
by thread_id; the handoff manifest is written independently so a framework
checkpoint never becomes the only evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypedDict

try:  # optional dependency: the rest of the repo stays stdlib-only
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt

    HAS_LANGGRAPH = True
except ImportError:  # pragma: no cover - exercised by the stdlib gate
    HAS_LANGGRAPH = False
    MemorySaver = None  # type: ignore[assignment]
    END = START = None  # type: ignore[assignment]
    StateGraph = None  # type: ignore[assignment]
    Command = None  # type: ignore[assignment]

    def interrupt(value: Any) -> Any:  # type: ignore[misc]
        raise RuntimeError("langgraph is not installed")

__all__ = [
    "HAS_LANGGRAPH",
    "Manifest",
    "SwarmState",
    "build_graph",
    "default_auditor",
    "run_swarm",
    "resume_swarm",
]

ROOT = Path(__file__).resolve().parents[2]
GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: Any) -> str:
    data = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class ManifestError(Exception):
    """Raised when the handoff hash chain is not intact."""


class Manifest:
    """Append-only hash chain for handoffs and agent decisions.

    Each record signs ``{id, kind, payload, prev_hash, at}``. The record hash
    is the next record's ``prev_hash``. This is deliberately smaller than the
    runtime's HMAC receipt chain: the pilot is testing handoff orchestration,
    not replacing FCode's existing evidence layer.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records: list[dict[str, Any]] = []
        self.head = GENESIS
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.records.append(json.loads(line))
            self.verify()
            if self.records:
                self.head = self.records[-1]["record_hash"]

    def append(self, kind: str, payload: dict[str, Any],
               event_id: str | None = None) -> dict[str, Any]:
        event_id = event_id or f"ev-{uuid.uuid4().hex[:12]}"
        previous = next((r for r in self.records if r.get("id") == event_id), None)
        if previous is not None:
            return previous
        body = {
            "id": event_id,
            "kind": kind,
            "payload": payload,
            "prev_hash": self.head,
            "at": _now(),
        }
        record = {**body, "record_hash": sha(body)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical(record) + "\n")
        self.records.append(record)
        self.head = record["record_hash"]
        return record

    def verify(self) -> dict[str, Any]:
        previous = GENESIS
        errors: list[str] = []
        for index, record in enumerate(self.records):
            body = {key: record.get(key) for key in ("id", "kind", "payload", "prev_hash", "at")}
            if record.get("prev_hash") != previous:
                errors.append(f"record {index}: prev_hash mismatch")
            if record.get("record_hash") != sha(body):
                errors.append(f"record {index}: record_hash mismatch")
            previous = str(record.get("record_hash") or "")
        return {
            "ok": not errors,
            "count": len(self.records),
            "head": self.head,
            "errors": errors,
        }


class SwarmState(TypedDict, total=False):
    run_id: str
    objective: str
    target: str
    run_dir: str
    audit: dict[str, Any]
    handoffs: list[dict[str, Any]]
    plan: dict[str, Any]
    approvals: list[dict[str, Any]]
    verification: dict[str, Any]
    status: str
    error: str


# --------------------------------------------------------------------- auditor

def default_auditor(target: str, run_dir: Path) -> dict[str, Any]:
    """Run the real offline audit CLI into a lab-local bundle directory."""
    repo = ROOT
    resolved = (repo / target).resolve()
    if not (resolved == repo or repo in resolved.parents):
        raise ValueError(f"target escapes the repository: {target}")
    out_dir = run_dir / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    completed = subprocess.run(
        [
            sys.executable, "-m", "flowaudit.cli", "audit", str(resolved),
            "--adapter", "repo", "--out", str(out_dir),
            "--episodes-db", str(run_dir / "episodes.sqlite"),
        ],
        cwd=str(repo), env=env, capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"audit exit {completed.returncode}")
    summary = None
    for line in reversed(completed.stdout.strip().splitlines()):
        try:
            summary = json.loads(line)
            break
        except ValueError:
            continue
    if not summary:
        raise RuntimeError("audit produced no summary JSON")
    bundle_path = Path(summary["bundle"])
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    return {
        "audit_id": summary["audit_id"],
        "bundle": str(bundle_path),
        "findings": bundle.get("findings") or [],
        "poc_evidence": bundle.get("poc_evidence_refs") or [],
        "ruleset_version": summary.get("ruleset_version"),
        "chain_verification": bundle.get("chain_verification") or {},
    }


def _findings_refs(audit: dict[str, Any]) -> list[str]:
    return [str(item.get("finding_id")) for item in audit.get("findings") or []]


def _manifest(state: SwarmState) -> Manifest:
    return Manifest(Path(state["run_dir"]) / "manifest.jsonl")


def _sign_handoff(manifest: Manifest, *, run_id: str, from_role: str,
                  to_role: str, task: str, evidence_refs: list[str],
                  previous_handoff: str = GENESIS,
                  plan: dict[str, Any] | None = None,
                  approval: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "from": from_role,
        "to": to_role,
        "task": task,
        "evidence_refs": evidence_refs,
        "prev_handoff_hash": previous_handoff,
        "plan": plan or {},
        "approval": approval or {},
    }
    record = manifest.append("handoff", payload)
    return {**payload, "record_hash": record["record_hash"]}


def auditor_node(state: SwarmState, auditor: Callable[[str, Path], dict[str, Any]] | None = None) -> SwarmState:
    if not HAS_LANGGRAPH:
        raise RuntimeError("langgraph is not installed")
    run_dir = Path(state["run_dir"])
    audit = auditor(state["target"], run_dir) if auditor else default_auditor(state["target"], run_dir)
    manifest = _manifest(state)
    previous = (state.get("handoffs") or [{}])[-1].get("record_hash", GENESIS)
    handoff = _sign_handoff(
        manifest,
        run_id=state["run_id"],
        from_role="auditor",
        to_role="planner",
        task="turn findings into a repair brief",
        evidence_refs=_findings_refs(audit),
        previous_handoff=previous,
        plan={},
    )
    manifest.append("audit_completed", {
        "audit_id": audit.get("audit_id"),
        "findings": len(audit.get("findings") or []),
        "ruleset_version": audit.get("ruleset_version"),
    }, event_id=f"{state['run_id']}:audit")
    return {
        "audit": audit,
        "handoffs": [*state.get("handoffs", []), handoff],
        "status": "awaiting_approval",
    }


# -------------------------------------------------------------------- approval

def approval_node(state: SwarmState) -> SwarmState:
    decision = interrupt({
        "type": "handoff_approval",
        "run_id": state["run_id"],
        "from": "auditor",
        "to": "planner",
        "target": state["target"],
        "findings": len((state.get("audit") or {}).get("findings") or []),
        "question": "批准把审计发现交给规划员？",
    })
    approved = bool((decision or {}).get("approved"))
    approval = {
        "approved": approved,
        "operator": str((decision or {}).get("operator") or "operator"),
        "note": str((decision or {}).get("note") or ""),
    }
    manifest = _manifest(state)
    manifest.append("approval", approval, event_id=f"{state['run_id']}:approval")
    return {
        "approvals": [*state.get("approvals", []), approval],
        "status": "planning" if approved else "rejected",
    }


# --------------------------------------------------------------------- planner

def planner_node(state: SwarmState) -> SwarmState:
    """Deterministic repair brief: no model, no tool authority."""
    audit = state.get("audit") or {}
    findings = list(audit.get("findings") or [])
    findings.sort(key=lambda item: (
        {"high": 0, "medium": 1, "low": 2}.get(str(item.get("severity")), 9),
        str(item.get("finding_id")),
    ))
    top = findings[0] if findings else None
    plan = {
        "plan_id": f"plan-{state['run_id']}",
        "finding_id": (top or {}).get("finding_id"),
        "root_cause": (top or {}).get("root_cause_group"),
        "category": (top or {}).get("category"),
        "steps": [
            "read the cited root-cause file and its call sites",
            "apply the smallest fix that removes the root cause",
            "run the existing verification command",
        ] if top else ["no findings: record a clean audit and stop"],
        "allowed_tools": ["read_file", "grep_workspace", "write_file", "run_verification"],
        "evidence_refs": [str((top or {}).get("finding_id"))] if top else [],
    }
    manifest = _manifest(state)
    manifest.append("plan_created", plan, event_id=f"{state['run_id']}:plan")
    previous = (state.get("handoffs") or [{}])[-1].get("record_hash", GENESIS)
    handoff = _sign_handoff(
        manifest,
        run_id=state["run_id"],
        from_role="planner",
        to_role="verifier",
        task="verify the repair brief against the audit evidence",
        evidence_refs=plan["evidence_refs"],
        previous_handoff=previous,
        plan=plan,
        approval=(state.get("approvals") or [{}])[-1],
    )
    return {
        "plan": plan,
        "handoffs": [*state.get("handoffs", []), handoff],
        "status": "verifying",
    }


# -------------------------------------------------------------------- verifier

def verifier_node(state: SwarmState) -> SwarmState:
    audit = state.get("audit") or {}
    plan = state.get("plan") or {}
    handoffs = state.get("handoffs") or []
    manifest = _manifest(state)
    manifest_check = manifest.verify()
    finding_ids = {str(item.get("finding_id")) for item in audit.get("findings") or []}
    checks = {
        "manifest": manifest_check["ok"],
        "handoff_count": len(handoffs) == 2,
        "handoff_chain": all(
            handoffs[index].get("prev_handoff_hash") == (
                GENESIS if index == 0 else handoffs[index - 1].get("record_hash"))
            for index in range(len(handoffs))
        ) if handoffs else False,
        "plan_evidence_known": all(ref in finding_ids for ref in plan.get("evidence_refs") or []),
        "audit_has_bundle": bool(audit.get("bundle")) and Path(str(audit["bundle"])).is_file(),
    }
    passed = all(checks.values())
    verification = {
        "passed": passed,
        "checks": checks,
        "manifest_head": manifest_check["head"],
        "audit_id": audit.get("audit_id"),
        "ruleset_version": audit.get("ruleset_version"),
    }
    manifest.append("verification", verification, event_id=f"{state['run_id']}:verification")
    return {
        "verification": verification,
        "status": "verified" if passed else "failed",
        "error": "" if passed else "handoff verification failed",
    }


# ---------------------------------------------------------------------- graph

def build_graph(auditor: Callable[[str, Path], dict[str, Any]] | None = None,
                checkpointer: Any = None):
    if not HAS_LANGGRAPH:
        raise RuntimeError("langgraph is not installed")
    workflow = StateGraph(SwarmState)
    workflow.add_node("auditor", lambda state: auditor_node(state, auditor))
    workflow.add_node("approval", approval_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("verifier", verifier_node)
    workflow.add_edge(START, "auditor")
    workflow.add_edge("auditor", "approval")
    workflow.add_conditional_edges(
        "approval",
        lambda state: "planner" if state.get("status") == "planning" else END,
        {"planner": "planner", END: END},
    )
    workflow.add_edge("planner", "verifier")
    workflow.add_edge("verifier", END)
    return workflow.compile(checkpointer=checkpointer or MemorySaver())


def _config(run_id: str) -> dict[str, Any]:
    return {
        "recursion_limit": 20,
        "configurable": {"thread_id": run_id},
    }


def initial_state(run_id: str, target: str, run_dir: Path) -> SwarmState:
    return {
        "run_id": run_id,
        "objective": f"audit and prepare repair for {target}",
        "target": target,
        "run_dir": str(run_dir),
        "handoffs": [],
        "approvals": [],
        "status": "auditing",
    }


def run_swarm(target: str, run_dir: Path, *, run_id: str | None = None,
              auditor: Callable[[str, Path], dict[str, Any]] | None = None,
              checkpointer: Any = None) -> tuple[Any, dict[str, Any], str]:
    rid = run_id or f"lg-{uuid.uuid4().hex[:12]}"
    app = build_graph(auditor, checkpointer)
    state = initial_state(rid, target, run_dir)
    result = app.invoke(state, config=_config(rid))
    return app, result, rid


def resume_swarm(app: Any, run_id: str, approved: bool,
                 operator: str = "operator") -> dict[str, Any]:
    return app.invoke(
        Command(resume={"approved": approved, "operator": operator}),
        config=_config(run_id),
    )
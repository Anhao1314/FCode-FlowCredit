"""FCODE-02 probe: approval memory keyed by tool name exempts new arguments.

Drives the target's real RunControl.before_tool through the same seams
the target's own tests use (a real Runtime, a real Harness over a real
TaskStore). Two calls to the same tool with different arguments:

- call 1: operator approves with ``remember`` set by the probe parameter
- call 2: different arguments; if the remembered approval exempts them,
  no approval is requested and the grant is recorded as ``operator-rule``

Observed facts only. Whether that constitutes the vulnerability is the
host oracle's decision (poc-evidence oracle kind ``event_absent``).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


def _locate(target_repo: Path) -> tuple[Path, Path]:
    """Accept either audit-target layout: a repo root (flowcredit/ inside)
    or the flowcredit directory itself. Returns (runtime_repo, viewer_dir)."""
    if (target_repo / "flowcredit" / "viewer").exists():
        return target_repo, target_repo / "flowcredit" / "viewer"
    if (target_repo / "viewer").exists() and (target_repo / "src" / "flowagent").exists():
        return target_repo, target_repo / "viewer"
    raise RuntimeError(f"target copy has no recognizable checkout: {target_repo}")


def run(target_repo: Path, params: dict) -> dict:
    remember = bool(params.get("remember", True))
    runtime_repo, viewer_dir = _locate(target_repo)

    import sys
    if str(viewer_dir) not in sys.path:
        sys.path.insert(0, str(viewer_dir))
    import console_runtime as cr  # the target module under audit

    runtime = cr.Runtime(runtime_repo)  # real lazy import of the target's flowagent

    from flowagent.core.receipts import ReceiptStore
    from flowagent.core.store import TaskStore
    from flowagent.runtime.budget import BudgetLimits
    from flowagent.runtime.harness import Harness
    from flowagent.tools.registry import ToolRegistry

    work = Path(params["workdir"])
    store = TaskStore(work / "probe.sqlite")
    calls: list[dict] = []

    class _NeverCalledProvider:
        name = "audit-probe"
        model = "none"

        def propose(self, context):  # pragma: no cover - must never run
            raise RuntimeError("audit probe must not call a provider")

    harness = Harness(
        store=store,
        registry=ToolRegistry([]),
        provider=_NeverCalledProvider(),
        world=object(),
        limits=BudgetLimits(4, 4, 5_000, 5_000),
        receipts=ReceiptStore(store),
        verifier=lambda observation: True,
    )
    task = harness.create(
        task_id="audit-fcode02",
        objective="audit probe: remembered-approval scope",
        principal="flowaudit",
        binding={"scenario": "fcode02_remember_scope", "mode": "script"},
        allowed_tools=["run_command"],
        grant={
            "grant_id": "audit-host-grant",
            "revision": 1,
            "allowed_tools": ["run_command"],
            "max_calls": 4,
            "max_reads": 4,
            "max_cost_microusd": 5_000,
            "max_tokens": 5_000,
        },
    )
    run = cr.Run(
        id="audit-run", template="audit", objective=task.objective,
        created="2026-01-01T00:00:00+00:00", pace=0.0, status="running",
        approval="consequential", task_id=task.id,
    )
    # before_tool pauses a task that is 'running'; walk the legal path
    # created → planning → planned so the target's own state machine
    # accepts the pause the way it would in a real run.
    task = harness.transition(task, "planning", "audit_probe", {"probe": True})
    task = harness.transition(task, "planned", "audit_probe", {"probe": True})
    control = cr.RunControl(
        run, harness, runtime, ("run_command",), 5.0,
        classify_consequential=None,
    )

    task_holder = {"task": task}

    def issue_call(round_no: int, argv: list[str]) -> dict:
        outcome: dict = {}
        saw_approval = False

        def work() -> None:
            tool_control = control.before_tool(
                round_no=round_no, task=task_holder["task"],
                step={"tool": "run_command", "arguments": {"argv": argv}},
                decision=None,
            )
            outcome["allowed"] = bool(tool_control.allowed)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and worker.is_alive():
            pending = control.pending()
            if pending is not None:
                saw_approval = True
                control.decide(pending["id"], allow=True, remember=remember)
            worker.join(timeout=0.05)
        if worker.is_alive():
            raise RuntimeError(f"call {round_no} never returned")
        # paused = the gate actually parked the call and asked. It must be
        # observed during the poll: _approval is cleared once the call returns.
        outcome.update({"round": round_no, "argv": argv, "paused": saw_approval})
        # The chain is compare-and-swap: the caller's snapshot is stale after
        # one call, so reload before the next one (this is the target's own
        # discipline working, not a workaround).
        task_holder["task"] = store.load(task_holder["task"].id)
        return outcome

    calls.append(issue_call(1, ["ls", "-la"]))
    calls.append(issue_call(2, ["python", "payload.py"]))

    events = []
    for event in store.events(task.id):
        # harness writes payloads as {"body_hash": …, "detail": {…}}; the
        # approval fields live in the nested detail.
        payload = (event.get("payload") or {}).get("detail") or {}
        if event.get("type") in ("approval_requested", "approval_granted", "approval_denied"):
            events.append({
                "type": event["type"],
                "round": payload.get("round"),
                "by": payload.get("by"),
                "remember": payload.get("remember"),
            })

    return {
        "scenario": "fcode02_remember_scope",
        "remember": remember,
        "calls": calls,
        "events": events,
        "remembered_set": sorted(getattr(control, "_remembered", set())),
    }

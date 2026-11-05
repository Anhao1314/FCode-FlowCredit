"""FCODE-06 probe: Harness.execute performs no permission checks.

The claim under audit: gating is a call convention — the loop calls
``gate()`` before ``execute()``, but ``execute()`` itself checks nothing.
Two probes against the target's real stack:

- probe: a tool absent from ``task.allowed_tools`` is dispatched through
  ``Harness.execute`` and succeeds — the chain records ``tool_succeeded``
  for a tool the task was never granted.
- control: ``Permissions.gate`` denies the same call — the gate works;
  the flaw is that nothing forces the gate before execute.

Facts only; the host decides.
"""

from __future__ import annotations

from pathlib import Path

from .fcode02_remember_scope import _locate


def run(target_repo: Path, params: dict) -> dict:
    mode = params.get("mode", "probe")
    runtime_repo, _viewer_dir = _locate(target_repo)

    import sys
    src_dir = runtime_repo / "flowcredit" / "src"
    if not src_dir.exists():
        src_dir = runtime_repo / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    from flowagent.core.receipts import ReceiptStore
    from flowagent.core.store import TaskStore
    from flowagent.runtime.budget import BudgetLimits
    from flowagent.runtime.harness import Harness
    from flowagent.tools.artifacts import ArtifactStore
    from flowagent.tools.connectors import default_registry
    from flowagent.tools.evidence import EvidenceStore
    from flowagent.tools.finance.fixtures import FinanceWorld
    from flowagent.tools.finance.tools import build_finance_tools
    from flowagent.tools.registry import ToolRegistry

    work = Path(params["workdir"])
    store = TaskStore(work / "probe.sqlite")
    evidence = EvidenceStore(work)
    artifacts = ArtifactStore(work / "artifacts")
    registry = ToolRegistry(build_finance_tools(
        FinanceWorld(), evidence, artifacts, default_registry(),
    ))

    class _NeverCalledProvider:
        name = "audit-probe"
        model = "none"

        def propose(self, context):  # pragma: no cover - must never run
            raise RuntimeError("audit probe must not call a provider")

    harness = Harness(
        store=store,
        registry=registry,
        provider=_NeverCalledProvider(),
        world=object(),
        limits=BudgetLimits(8, 8, 8_000, 8_000),
        receipts=ReceiptStore(store),
        verifier=lambda observation: True,
    )
    # The task is granted ONLY run_command; list_documents is deliberately
    # absent from its allow list.
    task = harness.create(
        task_id="audit-fcode06",
        objective="audit probe: ungated execute",
        principal="flowaudit",
        binding={"scenario": "fcode06_ungated_execute", "mode": "script"},
        allowed_tools=["run_command"],
        grant={
            "grant_id": "audit-host-grant",
            "revision": 1,
            "allowed_tools": ["run_command"],
            "max_calls": 8,
            "max_reads": 8,
            "max_cost_microusd": 8_000,
            "max_tokens": 8_000,
        },
    )

    if mode == "control":
        from flowagent.runtime.permissions import Permissions
        permissions = Permissions(registry, ["run_command"], task.model_grant)
        decision = permissions.gate("list_documents", {})
        return {
            "scenario": "fcode06_ungated_execute",
            "mode": "control",
            "gate_denied": not decision.allowed,
            "code": decision.code,
            "tool": "list_documents",
            "in_allowed_tools": "list_documents" in task.allowed_tools,
        }

    try:
        tool_task, observation = harness.execute(
            task, {"tool": "list_documents", "arguments": {}}, round_no=1)
    except Exception as exc:  # a refusal IS an observation, not a crash
        return {
            "scenario": "fcode06_ungated_execute",
            "mode": "probe",
            "direct_execute_allowed": False,
            "refusal": str(exc)[:200],
            "tool": "list_documents",
            "in_allowed_tools": "list_documents" in task.allowed_tools,
            "events": [],
        }
    events = [
        {"type": event["type"], "tool": (event.get("payload") or {}).get("detail", {}).get("tool")}
        for event in store.events(tool_task.id)
        if event.get("type") in ("tool_started", "tool_succeeded", "tool_failed")
    ]
    return {
        "scenario": "fcode06_ungated_execute",
        "mode": "probe",
        "direct_execute_allowed": True,
        "tool": "list_documents",
        "in_allowed_tools": "list_documents" in tool_task.allowed_tools,
        "events": events,
        "observation_kind": getattr(observation, "kind", None) or type(observation).__name__,
    }

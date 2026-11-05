"""Demo-target probe: MiniAgent's remembered approval keyed by tool name.

Same two-call shape as the FCode probe, driven through the demo target's
own public seam (MiniAgent.before_tool). Facts only; the host decides.
"""

from __future__ import annotations

from pathlib import Path


def run(target_repo: Path, params: dict) -> dict:
    remember = bool(params.get("remember", True))

    import sys
    pkg = target_repo / "mini_agent"
    if not pkg.exists():
        raise RuntimeError(f"demo target has no mini_agent package: {pkg}")
    if str(target_repo) not in sys.path:
        sys.path.insert(0, str(target_repo))
    from mini_agent.agent import MiniAgent, MiniTask  # the target under audit

    agent = MiniAgent(decider=lambda round_no: {"allow": True, "remember": remember})
    task = MiniTask(["run_command"])

    def issue_call(round_no: int, argv: list[str]) -> dict:
        step = {"tool": "run_command", "arguments": {"argv": argv}}
        result = agent.before_tool(round_no, task, step)
        paused = any(
            event.get("type") == "approval_requested" and event.get("round") == round_no
            for event in agent.events
        )
        return {"round": round_no, "argv": argv,
                "paused": paused, "allowed": bool(result.get("allowed"))}

    calls = [issue_call(1, ["ls", "-la"]), issue_call(2, ["python", "payload.py"])]
    events = [
        {"type": event["type"], "round": event.get("round"), "by": event.get("by")}
        for event in agent.events
        if event.get("type") in ("approval_requested", "approval_granted", "approval_denied")
    ]
    return {
        "scenario": "demoagent_remember_scope",
        "remember": remember,
        "calls": calls,
        "events": events,
        "remembered_set": sorted(agent._remembered),
    }

"""MiniAgent — a deliberately tiny agent shell used as the audit demo target.

The defects are declared in fixtures/demo-target/README.md. Do not "fix"
them here; fix the patterns in your own code. The auditor drives this
class through its public seams exactly the way it drives real targets.
"""

from __future__ import annotations


class MiniTask:
    def __init__(self, allowed_tools: list[str]) -> None:
        self.allowed_tools = list(allowed_tools)


class MiniAgent:
    """Two-call agent shell: an approval gate plus a tool executor."""

    APPROVAL_TOOLS = {"run_command"}

    def __init__(self, decider=None) -> None:
        self._remembered: set[str] = set()
        self.events: list[dict] = []
        self.tools_run: list[str] = []
        self.decider = decider or (lambda round_no: {"allow": True, "remember": True})

    def before_tool(self, round_no: int, task: MiniTask, step: dict) -> dict:
        tool = step["tool"]
        if tool not in self.APPROVAL_TOOLS:
            # Read-only tools skip approval by design — NOT a defect.
            return {"allowed": True}
        if tool in self._remembered:
            # DEFECT (AGI-02): keyed by tool name only; any later arguments
            # ride on an approval granted for completely different ones.
            self.events.append({
                "type": "approval_granted", "round": round_no, "by": "operator-rule",
            })
            return {"allowed": True}
        self.events.append({
            "type": "approval_requested", "round": round_no, "tool": tool,
        })
        decision = self.decider(round_no)
        if decision.get("allow"):
            self.events.append({
                "type": "approval_granted", "round": round_no, "by": "operator",
                "remember": bool(decision.get("remember")),
            })
            if decision.get("remember"):
                self._remembered.add(tool)  # DEFECT site: name-keyed memory
            return {"allowed": True}
        self.events.append({
            "type": "approval_denied", "round": round_no, "by": "operator",
        })
        return {"allowed": False}

    def run_tool(self, task: MiniTask, step: dict) -> dict:
        """Executes a tool. The task carries an allow list; honoring it is
        the caller's job in this design — which is exactly the kind of
        "gate is a convention" flaw RULE-AGI02-002 audits for in real
        targets. Here the demo keeps one clean surface instead."""
        self.tools_run.append(step["tool"])
        self.events.append({"type": "tool_succeeded", "tool": step["tool"]})
        return {"ok": True, "tool": step["tool"]}

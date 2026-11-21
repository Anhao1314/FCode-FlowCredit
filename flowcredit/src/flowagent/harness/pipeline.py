"""Permission pipeline (S2): PreToolUse hooks → rules → approval.

This is the harness ``checker`` the loop consumes. It is a pure decision
function: hooks may block or rewrite arguments, the policy resolves
allow/ask/deny (ask via the injected approver), and nothing here executes a
tool — execution stays behind the loop's gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .core import CheckDecision
from .hooks import HookOutcome, HookRunner
from .permissions import PermissionPolicy

__all__ = ["PermissionPipeline"]


@dataclass
class PermissionPipeline:
    policy: PermissionPolicy
    hooks: HookRunner | None = None

    def check(self, tool: str, args: Mapping[str, Any], _context: Any = None) -> CheckDecision:
        pre = self._pre_tool(tool, args)
        if pre.blocked:
            return CheckDecision(allow=False, reason=pre.reason)
        effective = pre.updated_input or args
        decision = self.policy.resolve(tool, effective)
        return CheckDecision(
            allow=decision.allowed,
            reason=decision.reason,
            updated_input=pre.updated_input,
        )

    def _pre_tool(self, tool: str, args: Mapping[str, Any]) -> HookOutcome:
        if self.hooks is None:
            return HookOutcome()
        return self.hooks.run(
            "PreToolUse",
            {"tool_name": tool, "tool_input": dict(args)},
            tool=tool,
        )

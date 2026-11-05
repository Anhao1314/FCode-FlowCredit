"""A deterministic offline model that follows the plan it is given.

This is not a stand-in for a language model; it is a stand-in for *the shape of a
model's output*. Two properties matter and are both load-bearing:

**It reads only the context.** Observations, the plan, the tool catalogue. It
never looks at the world directly. If the next action can be decided from the
context alone, then the context design is sufficient, and "would a real model do
better" becomes an empirical question rather than a structural one.

**It does not decide the sequence.** Earlier versions hardcoded the nine steps.
Now the plan decides what is left, and the policy only picks the first open todo.
That is the difference between a workflow and an agent: the order lives in a
revisable object, not in a policy function.

Policies exist so the reliability scenarios can provoke a specific failure without
touching the runtime.
"""

from __future__ import annotations

from typing import Any, Callable

from ..core.contracts import PLAN_VERSION, AgentError
from .base import Proposal

__all__ = ["MockProvider"]


class MockProvider:
    """Offline, deterministic, zero-dependency proposal source."""

    name = "offline-mock"
    model = "offline-deterministic-mock-v1"

    #: Exact synthetic accounting matches the runtime's established mock
    #: reservation. Real providers settle their reported usage; keeping these
    #: equal preserves the frozen finance prompt and case-projection contract.
    TOKENS_PER_CALL = 1_000
    COST_PER_CALL_MICROUSD = 500

    def __init__(
        self,
        *,
        policy: str = "research",
        fail_at_call: int | None = None,
        issue_receipt: Callable[..., str] | None = None,
    ) -> None:
        if policy not in ("research", "escalate", "loop"):
            raise ValueError(f"unknown mock policy {policy!r}")
        self.policy = policy
        self.fail_at_call = fail_at_call
        self.issue_receipt = issue_receipt
        self.calls = 0

    def propose(self, context: dict[str, Any]) -> Proposal:
        """Choose one action, and say which todo it is meant to advance."""
        self.calls += 1
        step, todo_id = self._next_step(context)
        plan = {"version": PLAN_VERSION, "steps": [step]}

        if self.fail_at_call is not None and self.fail_at_call == self.calls:
            # The response was produced and a receipt was persisted, but the
            # runtime never saw it. That is precisely the state an operator can
            # later reconcile — and precisely the state that must not be retried.
            if self.issue_receipt is not None:
                self.issue_receipt(
                    intent=context.get("intent", {}),
                    plan=plan,
                    usage={"provider": self.name, "model": self.model, "tokens": self.TOKENS_PER_CALL},
                )
            raise AgentError("PROVIDER_TIMEOUT", "the provider did not answer within the deadline")

        return Proposal(
            plan=plan,
            provider=self.name,
            model=self.model,
            tokens=self.TOKENS_PER_CALL,
            cost_microusd=self.COST_PER_CALL_MICROUSD,
            latency_ms=0,
            todo_id=todo_id,
            metadata={"policy": self.policy, "round": context.get("round", 0)},
        )

    # ------------------------------------------------------------------ policy

    def _next_step(self, context: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        if self.policy == "escalate":
            return self._escalate(), None
        if self.policy == "loop":
            return self._step("s1", "list_documents", {}), None
        return self._follow_plan(context)

    @staticmethod
    def _escalate() -> dict[str, Any]:
        """Propose a tool the host never registered.

        The name is syntactically valid, so the plan schema accepts it. The gate
        is what refuses it, which is the point: schema validity is not authority.
        """
        return {"id": "s9", "tool": "write_document", "arguments": {"doc_id": "doc_fy2024"}}

    #: Authored content a real model would produce. The blueprint deliberately
    #: leaves it out (otherwise every real delegation would execute this
    #: checkout's fixture — which is exactly what happened before); the mock
    #: cannot author anything, so it supplies the milestone fixture here.
    SCRIPT_BUG = "    for _ in range(n - 1):  # first draft: one iteration short"
    SCRIPT_FIX = "    for _ in range(n):  # fixed: n iterations for fib(n)"

    def _follow_plan(self, context: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Take the first open todo and turn it into an action.

        When the plan is exhausted the agent does not invent work: it reports
        that it cannot proceed, and the runtime decides whether that is true.
        """
        open_todos = [
            todo for todo in (context.get("plan") or [])
            if todo.get("status") in ("pending", "active")
        ]
        if not open_todos:
            return self._step("stop", "report_blocked", {"reason": "insufficient_evidence"}), None
        todo = open_todos[0]
        arguments = self._authored(todo["tool"], dict(todo.get("arguments") or {}))
        return self._step(todo["id"], todo["tool"], arguments), todo["id"]

    def _authored(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fill the arguments a proposer is supposed to author, and only those.

        Content the plan already carries (a report composed from real fetches,
        results filed from real runs) is left alone.
        """
        if tool == "write_file" and not str(arguments.get("content") or "").strip():
            path = str(arguments.get("path") or "")
            if path.endswith(".py"):
                arguments["content"] = self._first_draft()
            elif path.endswith(".md"):
                # Open/project delegations require a real Markdown artifact too.
                # Previously only Python received authored content, so the mock
                # repeatedly proposed an invalid write and could never succeed.
                arguments["content"] = (
                    "# Offline mock delivery\n\n"
                    f"- Artifact: `{path}`\n"
                    "- Result: completed by the deterministic offline provider.\n"
                )
        elif tool == "edit_file" and not str(arguments.get("old_text") or "").strip():
            arguments["old_text"] = self.SCRIPT_BUG
            arguments["new_text"] = self.SCRIPT_FIX
        return arguments

    @classmethod
    def _first_draft(cls) -> str:
        """The milestone's first attempt: one iteration short, on purpose."""
        return "\n".join([
            "def fib(n):",
            "    \"\"\"Return fib(n): fib(0)=0, fib(1)=1.\"\"\"",
            "    if n < 0:",
            "        raise ValueError('n must be >= 0')",
            "    a, b = 0, 1",
            cls.SCRIPT_BUG,
            "        a, b = b, a + b",
            "    return a",
            "",
            "",
            "if __name__ == \"__main__\":",
            "    for n in range(1, 11):",
            "        print(f\"fib({n}) = {fib(n)}\")",
            "",
        ])

    @staticmethod
    def _step(step_id: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"id": step_id, "tool": tool, "arguments": arguments}

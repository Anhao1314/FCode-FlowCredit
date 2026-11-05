"""The host gate: the only place that decides whether a proposal may run.

A model proposal is a *request*, never an instruction. This module turns that
request into a decision with a stable code, so the trace can show a denial and
the reason for it. Three independent scopes must all agree:

1. the tool must be registered at all,
2. the task must list it in ``allowed_tools``,
3. the current grant must list it too.

**What re-checking the third scope buys — accurately.** It buys *revalidation*,
not *revocation*: this gate is rebuilt from the task's stored authorization on
every dispatch, so nothing can be smuggled in between rounds, and if the
registry the gate reads changes, the next dispatch sees the change. What it
does **not** buy is a way to withdraw a grant mid-task. ``allowed_tools`` and
``model_grant`` are immutable in the task body (see
``TaskStore._assert_legal_change``), and no supported interface mutates them
after creation, so "a permission revoked mid-task stops the very next call" is
**not** a capability this runtime currently has. Enforcing it would mean adding
a revocation interface and relaxing a deliberately immutable task field — a
change to the Task/persistence contract, which is out of scope here and is
recorded as a separate design task rather than implied by this docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..core.contracts import AgentError
from ..tools.base import EFFECT_NETWORK, EFFECT_READ, EFFECT_WRITE, validate_arguments
from ..tools.registry import ToolRegistry

__all__ = ["GateDecision", "Permissions", "GRANTABLE_EFFECTS"]

#: The effect classes a host grant may name. Anything else in a grant is a
#: configuration error, refused loudly rather than narrowed silently.
GRANTABLE_EFFECTS = (EFFECT_READ, EFFECT_WRITE, EFFECT_NETWORK)


@dataclass(frozen=True)
class GateDecision:
    """The outcome of one gate check, with a code suitable for a trace."""

    allowed: bool
    code: str
    reason: str
    tool: str

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason, "tool": self.tool}


class Permissions:
    """Evaluates proposals against the registry, the task scope and the grant.

    The default policy is still the Phase 1 promise: only read effects run.
    Write and network effects execute solely when the *host grant* names them
    in ``allowed_effects`` — the same authority that lists allowed tools. A
    task that never asks for the wider class keeps the narrower refusal, so
    adding write tools to the registry does not widen any existing task.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        allowed_tools: list[str],
        grant: Mapping[str, Any],
        allowed_effects: Iterable[str] | None = None,
    ) -> None:
        self._registry = registry
        self._allowed_tools = list(allowed_tools)
        self._grant = dict(grant)
        # The effect classes come from the grant itself unless a caller
        # (a test, or a narrower policy) names them explicitly. A grant
        # without ``allowed_effects`` keeps the Phase 1 read-only promise.
        if allowed_effects is None:
            allowed_effects = self._grant.get("allowed_effects") or (EFFECT_READ,)
        effects = tuple(allowed_effects) or (EFFECT_READ,)
        unknown = [effect for effect in effects if effect not in GRANTABLE_EFFECTS]
        if unknown:
            raise AgentError(
                "GRANT_EFFECTS_INVALID",
                f"allowed_effects names unknown classes {unknown}; valid: {list(GRANTABLE_EFFECTS)}",
            )
        self._allowed_effects = effects

    def gate(self, tool: str, arguments: Mapping[str, Any]) -> GateDecision:
        """Return a decision; never raises for an ordinary denial."""
        if tool not in self._registry:
            return GateDecision(False, "TOOL_NOT_REGISTERED", f"{tool!r} is not a registered tool", tool)
        if tool not in self._allowed_tools:
            return GateDecision(
                False,
                "TOOL_NOT_IN_TASK_SCOPE",
                f"{tool!r} is not in this task's allowed_tools",
                tool,
            )
        grant_tools = self._grant.get("allowed_tools", [])
        if tool not in grant_tools:
            return GateDecision(
                False,
                "TOOL_NOT_IN_GRANT",
                f"{tool!r} is not covered by the current host grant",
                tool,
            )

        spec = self._registry.get(tool)
        if spec.effect not in self._allowed_effects:
            return GateDecision(
                False,
                "TOOL_EFFECT_DENIED",
                f"{tool!r} has effect {spec.effect!r}; this grant permits only {list(self._allowed_effects)}",
                tool,
            )

        try:
            validate_arguments(spec, arguments)
        except AgentError as error:
            return GateDecision(False, error.code, error.message, tool)
        return GateDecision(True, "PASS", "schema, scope and grant all agree", tool)

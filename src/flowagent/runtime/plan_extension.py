"""Governed plan extension: the host judges a model's proposed todos.

A general delegation is not a workflow. Its blueprint can be a starting point or
an artifact obligation, but the task's actual steps are not knowable in advance,
so the model has to be able to add them. Without that, a run whose actions were
all legal and useful still could not finish: every observation settled nothing,
the plan never completed, and the round limit ended the run (measured — see the
Phase A experiments).

The whole difficulty is doing that without handing the model the plan. So:

* **A proposal is not authority.** ``validate_plan`` already checked the shape
  (bounded, well-named, and carrying none of the host-owned fields). This module
  decides whether the *host* accepts it, reusing the same ``Permissions`` gate a
  step goes through — registration, task scope, host grant, effect class and the
  tool's own argument schema — so a todo can never name an action the run was
  not already allowed to take.
* **All or nothing.** One bad entry refuses the whole extension. Partial
  acceptance would leave the operator unable to tell which half the host agreed
  to, and the model unable to tell which half to fix.
* **Addition only.** Drop, replace, reorder and complete stay where they were:
  with the operator (``skip``/``promote``) and the planner. Nothing here marks a
  todo done, and every accepted todo is ``required`` — an added step the run can
  quietly abandon would be a way to look finished without being it.
* **Persist before use.** This module only returns todos. The caller records
  them through the existing ``plan_steered`` event before the plan they belong
  to can drive another round, which is also what makes replay reconstruct them.

The completion contract is untouched: todos still have to be settled by real
observations and the domain verifier still recomputes the result. An added todo
that claims to be the answer is worth exactly as much as any other todo — which
is to say, nothing.
"""

from __future__ import annotations

from typing import Any

from ..core.contracts import AgentError, validate_addition_shapes
from ..core.plan import Plan, Todo
from ..tools.registry import ToolRegistry
from .permissions import Permissions

__all__ = [
    "MAX_PLAN_TODOS",
    "PLAN_CONTROL_TOOLS",
    "PlanExtension",
    "validate_additions",
]

#: The runtime's stop action (see :meth:`Loop._reported_blocked`). It declares a
#: stop rather than a unit of work, so it is not a plan step: an added todo for
#: it could never be settled by an observation, and would strand the run at the
#: round limit instead of stopping it.
PLAN_CONTROL_TOOLS: tuple[str, ...] = ("report_blocked",)

#: A ceiling on plan size, so "add steps" cannot become "add unbounded work".
#: Generous next to the four entries one proposal may carry and next to the
#: blueprints (five to nine todos) — but finite, and enforced.
MAX_PLAN_TODOS = 24


class PlanExtension:
    """The host's verdict on one proposal's plan-extension request.

    ``accepted`` are ``Todo`` objects the caller may persist. ``rejected`` is
    why not — a list of ``{id, tool, code, message}`` so the trace and the next
    round's context can both name the offending entry.
    """

    __slots__ = ("accepted", "rejected", "reason")

    def __init__(
        self,
        *,
        accepted: list[Todo] | None = None,
        rejected: list[dict[str, str]] | None = None,
        reason: str = "",
    ) -> None:
        self.accepted = accepted or []
        self.rejected = rejected or []
        self.reason = reason

    @property
    def ok(self) -> bool:
        return bool(self.accepted) and not self.rejected

    @property
    def present(self) -> bool:
        """True when the caller actually had something to judge."""
        return bool(self.accepted or self.rejected or self.reason)

    def added_ids(self) -> list[str]:
        return [todo.id for todo in self.accepted]


def validate_additions(
    additions: Any,
    *,
    plan: Plan,
    registry: ToolRegistry,
    permissions: Permissions,
    enabled: bool,
    result_tools: tuple[str, ...] = (),
) -> PlanExtension:
    """Judge a proposal's requested plan extension. Never raises.

    A refusal is a normal outcome the run continues from: the model gets the
    reason in its next context and can propose a corrected extension, while the
    action it proposed alongside is still gated and executed on its own merits.
    """
    if not additions:
        # No request at all — the proposal did not try to extend anything, so
        # there is nothing to judge (and nothing to record).
        return PlanExtension()

    if not enabled:
        # The domain did not open its plan to the model. That is a host decision,
        # not a malformed proposal — say which, so the trace is not read as the
        # model having tried something illegal.
        return PlanExtension(
            rejected=[{
                "id": "", "tool": "", "code": "PLAN_EXTENSION_DISABLED",
                "message": "this domain's plan is authored by its planner; proposed todos are not accepted",
            }],
            reason="plan extension is not enabled for this domain",
        )

    try:
        shape = validate_addition_shapes(additions, known_ids=[todo.id for todo in plan.todos])
    except AgentError as error:
        return PlanExtension(
            rejected=[{"id": "", "tool": "", "code": error.code, "message": error.message}],
            reason=error.message,
        )

    accepted: list[Todo] = []
    rejected: list[dict[str, str]] = []
    for entry in shape:
        refusal = _refuse(entry, plan=plan, registry=registry, permissions=permissions,
                          accepted=accepted, result_tools=result_tools)
        if refusal is None:
            accepted.append(Todo(
                id=entry["id"],
                text=entry["text"],
                tool=entry["tool"],
                done_when=entry["done_when"],
                arguments=entry["arguments"],
                # Forced by the host, never proposed: a new step starts pending
                # with no evidence, and is required so it cannot be abandoned as
                # a way of appearing finished.
                status="pending",
                evidence=(),
                reason="",
                required=True,
            ))
        else:
            rejected.append(refusal)

    if rejected:
        # All or nothing: a partially applied extension is not auditable.
        return PlanExtension(
            rejected=rejected,
            reason="; ".join(sorted({item["message"] for item in rejected})),
        )
    return PlanExtension(accepted=accepted)


def _refuse(
    entry: dict[str, Any],
    *,
    plan: Plan,
    registry: ToolRegistry,
    permissions: Permissions,
    accepted: list[Todo],
    result_tools: tuple[str, ...],
) -> dict[str, str] | None:
    """Why this entry may not enter the plan — or None when it may."""
    todo_id, tool = entry["id"], entry["tool"]

    def no(code: str, message: str) -> dict[str, str]:
        return {"id": todo_id, "tool": tool, "code": code, "message": message}

    if tool in PLAN_CONTROL_TOOLS:
        return no(
            "PLAN_ADDITION_CONTROL_TOOL_REFUSED",
            f"{tool!r} declares a stop, not a step; propose it as the round's action instead",
        )
    # One gate for registration, task scope, host grant, effect class and the
    # tool's own argument schema — the same decision the step will face at
    # dispatch. Scope is checked here so a todo the run may not perform never
    # enters the plan at all.
    decision = permissions.gate(tool, entry["arguments"])
    if not decision.allowed:
        return no(decision.code, f"{tool!r} is not available to this run: {decision.reason}")

    existing = {todo.id for todo in plan.todos} | {todo.id for todo in accepted}
    if todo_id in existing:
        return no("PLAN_ADDITION_DUPLICATE_ID", f"todo id {todo_id!r} is already in this plan")
    if len(plan.todos) + len(accepted) >= MAX_PLAN_TODOS:
        return no(
            "PLAN_ADDITION_PLAN_FULL",
            f"the plan already holds {MAX_PLAN_TODOS} todos; finish or abandon before adding more",
        )
    # The domain's result todo is the answer obligation — the thing the verifier
    # recomputes "succeeded" from. Adding a second step for the same target would
    # give the run two identical obligations, only one of which the planner's
    # path match settles, so the other would sit pending and end the run at the
    # round limit. Repeating a *run* is fine (the planner settles those in
    # order); duplicating the result target is not.
    if tool in result_tools:
        target = _identity_arguments(registry, tool, entry["arguments"])
        for todo in [*plan.todos, *accepted]:
            if todo.tool != tool:
                continue
            if _identity_arguments(registry, tool, todo.arguments) == target:
                return no(
                    "PLAN_ADDITION_RESULT_DUPLICATE",
                    f"{tool!r} already has a step for {entry['arguments']} — the plan's own result "
                    "step covers it; propose the action, not another step",
                )
    return None


def _identity_arguments(
    registry: ToolRegistry,
    tool: str,
    arguments: Any,
) -> tuple[tuple[str, str], ...]:
    """The subset of *arguments* that identifies this step's target.

    Uses the tool's own ``Param.identity`` flags — the same ones the result
    verifier binds its observations with — so "same target" means the same thing
    in both places. A tool that declares no identity parameters has one target.
    """
    try:
        spec = registry.get(tool)
    except AgentError:
        return ()
    identity = {param.name for param in spec.params if param.identity}
    return tuple(sorted(
        (key, repr(value))
        for key, value in (arguments or {}).items()
        if key in identity
    ))
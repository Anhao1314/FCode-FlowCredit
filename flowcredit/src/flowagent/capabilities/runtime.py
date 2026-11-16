"""Capability runtime: a stage machine over one artifact.

This is a **library** component. It is not the conversation loop, it does not
read or write a task's plan, and it never asks a model for a checklist. Its
whole contract is:

    stage -> packed state -> one action -> gated apply -> exit checks -> stage

The parent agent calls ``run()`` and gets a ``CapabilityResult``. Everything
that happened inside — actions, denials, revisions, check verdicts — stays in
the journal, addressed by ``child_run_id``.

A stage is left only when its exit checks pass against the artifact's current
bytes. Budget exhaustion, an unchanged-artifact streak, or an exhausted
scripted proposer ends the invocation with a typed *failed* result instead of
an exception or a generic schema error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .artifacts import Action, ArtifactStore
from .checks import CheckContext, CheckOutcome, CheckRegistry, CheckResult
from .manifest import BudgetSpec, CapabilityManifest, StageSpec
from .result import CapabilityResult

__all__ = [
    "STAGE_ACTION_DENIED",
    "StageActionDenied",
    "ActionGate",
    "CapabilityRuntime",
    "Proposer",
    "RepairTicket",
    "FAILURE_CODES",
]

STAGE_ACTION_DENIED = "STAGE_ACTION_DENIED"

#: Control-plane failures. These are deliberately distinct: a rejected action
#: is not an artifact failure, and reasoning exhaustion is not a missing draft.
NO_TOOL_CALL = "NO_TOOL_CALL"
EMPTY_CONTENT = "EMPTY_CONTENT"
INVALID_TOOL_CALL = "INVALID_TOOL_CALL"
ACTION_REJECTED = "ACTION_REJECTED"
ARTIFACT_CHECK_FAILED = "ARTIFACT_CHECK_FAILED"
BUDGET_EXHAUSTED_REASONING = "BUDGET_EXHAUSTED_REASONING"
BUDGET_EXHAUSTED_ACTION = "BUDGET_EXHAUSTED_ACTION"
ACTION_REPAIR_EXHAUSTED_NO_VALID = "ACTION_REPAIR_EXHAUSTED_NO_VALID"
ACTION_REPAIR_EXHAUSTED_PARTIAL = "ACTION_REPAIR_EXHAUSTED_PARTIAL"
ACTION_EXECUTION_FAILED = "ACTION_EXECUTION_FAILED"
MODEL_CALL_FAILED = "MODEL_CALL_FAILED"
ITERATION_LIMIT = "ITERATION_LIMIT"

FAILURE_CODES = frozenset({
    NO_TOOL_CALL,
    EMPTY_CONTENT,
    INVALID_TOOL_CALL,
    ACTION_REJECTED,
    ARTIFACT_CHECK_FAILED,
    BUDGET_EXHAUSTED_REASONING,
    BUDGET_EXHAUSTED_ACTION,
    ACTION_REPAIR_EXHAUSTED_NO_VALID,
    ACTION_REPAIR_EXHAUSTED_PARTIAL,
    ACTION_EXECUTION_FAILED,
    MODEL_CALL_FAILED,
    ITERATION_LIMIT,
})

#: Compatibility aliases for the old stage-specific failure vocabulary.
STAGE_ALIASES = {
    "draft": "NO_DRAFT",
    "structure": "STATIC_CHECK_FAILED",
    "motion": "PROBE_NOT_PASS",
    "delivery": "DELIVERY_BLOCKED",
}

#: Per-code repair allowance. A stage's ``BudgetSpec.repairs`` is the total
#: cap; this table prevents a model from spending the whole repair budget on
#: one repeated formatting mistake.
REPAIR_LIMITS_BY_CODE = {
    "EDIT_TOO_LARGE": 3,
    "TOOL_ARGUMENTS_INVALID": 2,
    "COMPOSITION_REGRESSION": 2,
    "WRONG_ANCHOR": 1,
    "STAGE_ACTION_DENIED": 1,
    "MOTION_CHECK_REQUIRED": 1,
}
DEFAULT_REPAIR_LIMIT = 1

#: Host-side argument errors are control-plane rejections: nothing executed and
#: the artifact did not change. They get the same bounded repair treatment as a
#: capability-level gate denial.
EXECUTION_REPAIR_CODES = frozenset({
    "TOOL_ARGUMENTS_INVALID",
})

#: Failures that describe the artifact, not the control plane.
ARTIFACT_FAILURE_CODES = frozenset({
    ARTIFACT_CHECK_FAILED,
    BUDGET_EXHAUSTED_ACTION,
    ACTION_REPAIR_EXHAUSTED_NO_VALID,
    ACTION_REPAIR_EXHAUSTED_PARTIAL,
})

#: Two consecutive applied actions with no artifact delta mean the capability
#: is looping. Gate denials use the separate repair budget instead.
MAX_NO_DELTA_ROUNDS = 2


class Proposer(Protocol):
    """A scripted/stand-in proposer. Real routing arrives in a later slice."""

    def propose(self, packet: dict[str, Any], *, tools: tuple[str, ...]) -> Action | None: ...


class StageActionDenied(Exception):
    """The action is legal in general but not permitted in this stage."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        violation: dict[str, Any] | None = None,
        constraint: str = "",
        suggested_action: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.violation = dict(violation or {})
        self.constraint = constraint or message
        self.suggested_action = suggested_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "violation": dict(self.violation),
            "constraint": self.constraint,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class RepairTicket:
    """One bounded, machine-readable gate rejection handed to the next round."""

    repair_id: str
    stage: str
    code: str
    message: str
    violation: dict[str, Any]
    constraint: str
    suggested_action: str
    remaining_repairs: int
    remaining_by_code: dict[str, int]
    artifact_sha256: str
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "violation": dict(self.violation),
            "constraint": self.constraint,
            "suggested_action": self.suggested_action,
            "remaining_repairs": int(self.remaining_repairs),
            "remaining_by_code": dict(self.remaining_by_code),
            "artifact_sha256": self.artifact_sha256,
            "attempt": int(self.attempt),
        }


class ActionGate:
    """Stage-local authorization. A denial never touches the artifact."""

    def require(
        self,
        action: Action | None,
        *,
        stage: StageSpec,
        context: CheckContext,
        checks: CheckRegistry,
        manifest: CapabilityManifest,
    ) -> None:
        if action is None or not str(getattr(action, "tool", "") or "").strip():
            raise StageActionDenied(STAGE_ACTION_DENIED, "empty action: propose exactly one tool call")
        tool = str(action.tool)
        if tool not in stage.allowed_tools:
            raise StageActionDenied(
                STAGE_ACTION_DENIED,
                f"tool {tool!r} is not allowed in stage {stage.name!r}; "
                f"allowed: {list(stage.allowed_tools)}",
            )
        rules = set(stage.action_rules)
        if tool == "edit_file":
            if manifest.kind == "motion.render" and len(str(action.arguments.get("new_text") or "")) > 3_000:
                actual = len(str(action.arguments.get("new_text") or ""))
                raise StageActionDenied(
                    "EDIT_TOO_LARGE",
                    "new_text exceeds the 3000-character motion edit limit; "
                    "split it into smaller anchored edits and keep the SECTION anchor",
                    violation={"field": "new_text", "actual": actual, "limit": 3_000},
                    constraint=(
                        "split new_text into chunks <=3000 characters; keep the "
                        "same SECTION anchor at the end of each chunk"
                    ),
                    suggested_action="split_into_chunks",
                )
            path = str(action.arguments.get("path") or manifest.artifact_path)
            if path != manifest.artifact_path:
                raise StageActionDenied(
                    STAGE_ACTION_DENIED,
                    f"stage {stage.name!r} may only edit {manifest.artifact_path!r}",
                )
            if "scene_edit_only" in rules and not self._scene_edit(action, context.store):
                raise StageActionDenied(
                    STAGE_ACTION_DENIED,
                    "draft stage accepts scene edits only: old_text must be the "
                    "<!-- SECTION: scene --> anchor or an existing SVG element; "
                    "a pure CSS edit (e.g. </style>) cannot fill the scene",
                )
        if tool == "write_file":
            path = str(action.arguments.get("path") or "")
            if "result_write_only" in rules and path != manifest.result_path:
                raise StageActionDenied(
                    STAGE_ACTION_DENIED,
                    f"delivery may only write {manifest.result_path!r}, got {path!r}",
                )
            if "motion_check_required" in rules and not checks.motion_check_pass(context.artifact.sha256):
                raise StageActionDenied(
                    "MOTION_CHECK_REQUIRED",
                    "run motion_check on the current artifact hash until status=pass "
                    f"and coverage.complete before writing {manifest.result_path}; "
                    "an edit changes the hash and invalidates an earlier pass",
                )
        if tool not in ("edit_file", "write_file", "motion_check", "verify"):
            raise StageActionDenied(STAGE_ACTION_DENIED, f"unsupported action {tool!r}")

    @staticmethod
    def _scene_edit(action: Action, store: ArtifactStore) -> bool:
        old = str(action.arguments.get("old_text") or "")
        if not old.strip():
            return False
        if "SECTION: scene" in old or "SECTION:scene" in old:
            return True
        # A later draft edit may replace an element already inside the scene.
        if (
            old.startswith("<")
            and not old.startswith("</style")
            and not old.startswith("<style")
            and old in store.scene_body()
        ):
            return True
        return False


class CapabilityRuntime:
    """Runs one capability manifest to a typed result."""

    def __init__(
        self,
        *,
        manifest: CapabilityManifest,
        store: ArtifactStore,
        checks: CheckRegistry,
        proposer: Proposer,
        gate: ActionGate | None = None,
        child_run_id: str = "",
        executor: Any = None,
    ) -> None:
        self.manifest = manifest
        self.store = store
        self.checks = checks
        self.proposer = proposer
        self.gate = gate or ActionGate()
        self.child_run_id = child_run_id
        #: Optional host execution boundary. The default is the library's direct
        #: sandbox apply; Work injects Harness.execute here so no model write
        #: can bypass permissions, budget charging or the durable event chain.
        self.executor = executor

    def pack_state(
        self,
        *,
        goal: str,
        stage: StageSpec,
        revision: Any,
        last_check: CheckOutcome | None,
        remaining_budget: dict[str, int],
        constraints: dict[str, Any],
        repair: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The bounded packet handed to the proposer. No internal trace here."""
        return {
            "goal": goal,
            "stage": {
                "name": stage.name,
                "allowed_tools": list(stage.allowed_tools),
                "exit_checks": list(stage.exit_checks),
                "on_fail": stage.on_fail,
            },
            "artifact": {
                "path": revision.path,
                "sha256": revision.sha256,
                "bytes": revision.bytes,
                "revision": revision.revision,
                "scene_nonempty": self.store.scene_nonempty(),
                # The proposer must see the actual anchors and failing code, not
                # just a hash. Keep the packet bounded; oversized artifacts are
                # explicit rather than silently truncated.
                "content": str(revision.content)[:30_000],
                "content_truncated": len(str(revision.content)) > 30_000,
            },
            "last_check": last_check.summary() if last_check is not None else None,
            "repair": dict(repair or {}),
            "remaining_budget": dict(remaining_budget),
            "constraints": dict(constraints),
            "tools": list(stage.allowed_tools),
        }

    def run(
        self,
        goal: str,
        *,
        constraints: dict[str, Any] | None = None,
        budget: BudgetSpec | None = None,
        start_stage: str | None = None,
    ) -> CapabilityResult:
        """Execute the stage machine and return one typed result."""
        constraints = dict(constraints or {})
        initial = self.manifest.stage(start_stage) if start_stage else self.manifest.initial_stage(self.store)
        if initial is None:
            raise ValueError("this manifest has no stages")
        first = self.store.snapshot()
        child_run_id = self.child_run_id or f"cap-{self.manifest.kind}-{first.sha256[:12]}"

        stage: StageSpec | None = initial
        journal: list[dict[str, Any]] = []
        last_check: CheckOutcome | None = None
        #: True only after a gate-passed action failed its exit checks. Gate
        #: denials and repair tickets must not mask a missing proposal.
        artifact_check_failed = False
        stage_attempts = stage_actions = stage_repairs = stage_tokens = stage_cost = 0
        total_model_calls = total_attempts = total_valid_actions = total_repairs = 0
        total_tokens = total_cost = 0
        no_delta = 0
        repair_by_code: dict[str, int] = {}
        last_repair_ticket: dict[str, Any] | None = None
        best_content: str | None = None
        best_sha: str = ""
        best_quality: dict[str, Any] | None = None
        hard_limit = sum(
            item.budget.calls + item.budget.repairs for item in self.manifest.stages
        ) + 8

        def budget_view() -> dict[str, int]:
            return {
                "calls_used": total_model_calls,
                "model_calls": total_model_calls,
                "attempts": total_attempts,
                "valid_actions": total_valid_actions,
                "repairs": total_repairs,
                "tokens_used": total_tokens,
                "cost_microusd": total_cost,
            }

        def failure_quality(reason: str) -> dict[str, Any]:
            sha = self.store.snapshot().sha256
            if reason not in ARTIFACT_FAILURE_CODES:
                # Control-plane failures did not establish an artifact verdict.
                # The restored best revision can still publish its composition
                # facts without pretending the run passed.
                base = {
                    "status": "unverified",
                    "score": 0,
                    "coverage": {
                        "required": [], "covered": [], "uncovered": [],
                        "complete": False,
                    },
                }
                if best_quality is not None and sha == best_sha:
                    for key in (
                        "composition_ok", "composition_measured",
                        "wheel_count", "offscreen_parts",
                    ):
                        if key in best_quality:
                            base[key] = best_quality[key]
                return base
            sha = self.store.snapshot().sha256
            measured = self.checks.quality_for(sha)
            if not measured and best_quality is not None and sha == best_sha:
                measured = best_quality
            if measured:
                return dict(measured)
            if last_check is not None:
                return dict(last_check.quality)
            return {
                "status": "unverified",
                "score": 0,
                "coverage": {
                    "required": [], "covered": [], "uncovered": [],
                    "complete": False,
                },
            }

        def fail(
            stage_name: str,
            reason: str,
            *,
            ticket: dict[str, Any] | None = None,
        ) -> CapabilityResult:
            fixes: list[Any] = []
            if last_check is not None:
                fixes = list(last_check.required_fixes)
            return CapabilityResult.failed_result(
                kind=self.manifest.kind,
                stage=stage_name,
                artifact=self.store.identity(),
                quality=failure_quality(reason),
                required_fixes=fixes[:5],
                budget=budget_view(),
                reason=reason,
                legacy_reason=(
                    STAGE_ALIASES.get(stage_name, "")
                    if reason in ARTIFACT_FAILURE_CODES else ""
                ),
                repair_ticket=ticket or last_repair_ticket,
                child_run_id=child_run_id,
                journal=tuple(journal),
            )

        def repair_limit(code: str) -> int:
            return REPAIR_LIMITS_BY_CODE.get(code, DEFAULT_REPAIR_LIMIT)

        def make_ticket(
            denied: StageActionDenied,
            *,
            state: Any,
            attempt: int,
        ) -> dict[str, Any]:
            used = repair_by_code.get(denied.code, 0)
            cap = repair_limit(denied.code)
            remaining = max(0, stage.budget.repairs - stage_repairs)
            return RepairTicket(
                repair_id=f"{stage.name}:{state.sha256[:12]}:{attempt}",
                stage=stage.name,
                code=denied.code,
                message=denied.message[:300],
                violation=denied.violation,
                constraint=denied.constraint[:300],
                suggested_action=denied.suggested_action,
                remaining_repairs=remaining,
                remaining_by_code={denied.code: max(0, cap - used)} if denied.code else {},
                artifact_sha256=state.sha256,
                attempt=attempt,
            ).to_dict()

        def stop_reason() -> str | None:
            if stage_attempts >= stage.budget.calls:
                # A gate-passed action that fails its exit check is an artifact
                # failure, not a budget failure. The budget only explains why we
                # stopped retrying, not why the artifact was rejected.
                return ARTIFACT_CHECK_FAILED
            if stage.budget.repairs and stage_repairs >= stage.budget.repairs:
                return (
                    ACTION_REPAIR_EXHAUSTED_PARTIAL
                    if stage_actions else ACTION_REPAIR_EXHAUSTED_NO_VALID
                )
            if stage.budget.tokens and stage_tokens >= stage.budget.tokens:
                return BUDGET_EXHAUSTED_ACTION
            if stage.budget.cost_microusd and stage_cost >= stage.budget.cost_microusd:
                return BUDGET_EXHAUSTED_ACTION
            if no_delta >= MAX_NO_DELTA_ROUNDS:
                return ARTIFACT_CHECK_FAILED
            if budget is not None:
                if total_model_calls >= budget.calls:
                    return BUDGET_EXHAUSTED_ACTION
                if budget.tokens and total_tokens >= budget.tokens:
                    return BUDGET_EXHAUSTED_ACTION
                if budget.cost_microusd and total_cost >= budget.cost_microusd:
                    return BUDGET_EXHAUSTED_ACTION
            return None

        def classify_error(error: Exception) -> str:
            code = str(getattr(error, "code", "") or "")
            message = str(error).lower()
            if code == MODEL_CALL_FAILED:
                if (
                    "reasoning" in message
                    or "推理" in message
                    or "completion budget" in message
                    or "补全预算" in message
                ):
                    return BUDGET_EXHAUSTED_REASONING
                if "empty content" in message or "content 为空" in message:
                    return EMPTY_CONTENT
                if "no usable tool calls" in message or "no tool call" in message:
                    return NO_TOOL_CALL
                return MODEL_CALL_FAILED
            if code in FAILURE_CODES:
                return code
            if "no usable tool calls" in message or "no tool call" in message:
                return NO_TOOL_CALL
            if code in (
                "PROPOSAL_MALFORMED",
                "PLAN_SCHEMA_INVALID",
                "PLAN_ADDITION_INVALID",
                "CAPABILITY_ACTION_INVALID",
            ):
                return INVALID_TOOL_CALL
            return code or MODEL_CALL_FAILED

        for _ in range(hard_limit):
            if stage is None:
                break
            state = self.store.snapshot()
            remaining = stage.budget.remaining(
                calls=stage_attempts,
                tokens=stage_tokens,
                cost_microusd=stage_cost,
                repairs=stage_repairs,
            )
            repair_packet = {
                "repairs_left": max(0, stage.budget.repairs - stage_repairs),
                "remaining_by_code": {
                    code: max(0, cap - repair_by_code.get(code, 0))
                    for code, cap in REPAIR_LIMITS_BY_CODE.items()
                },
                "last_ticket": last_repair_ticket,
            }
            packet = self.pack_state(
                goal=goal, stage=stage, revision=state, last_check=last_check,
                remaining_budget=remaining, constraints=constraints,
                repair=repair_packet,
            )
            try:
                action = self.proposer.propose(packet, tools=stage.allowed_tools)
            except Exception as error:  # noqa: BLE001 — a broken proposer is a typed failure
                if getattr(error, "capability_passthrough", False):
                    raise
                total_model_calls += 1
                code = classify_error(error)
                if artifact_check_failed:
                    # The model stopped after a failing artifact check: report
                    # the artifact failure, not the missing proposal.
                    code = ARTIFACT_CHECK_FAILED
                journal.append({
                    "stage": stage.name, "event": "proposer_error",
                    "code": code, "message": str(error)[:300],
                    "artifact_sha256": state.sha256,
                    "model_calls": total_model_calls,
                    "valid_actions": total_valid_actions,
                    "repairs": total_repairs,
                })
                return fail(stage.name, code)
            if action is None:
                total_model_calls += 1
                reason = (
                    ARTIFACT_CHECK_FAILED if artifact_check_failed else NO_TOOL_CALL
                )
                journal.append({
                    "stage": stage.name, "event": "proposer_exhausted",
                    "artifact_sha256": state.sha256,
                    "model_calls": total_model_calls,
                    "valid_actions": total_valid_actions,
                    "repairs": total_repairs,
                    "reason": reason,
                })
                return fail(stage.name, reason)

            calls_for_action = max(1, int(getattr(action, "model_calls", 1) or 1))
            total_model_calls += calls_for_action
            stage_tokens += int(getattr(action, "tokens", 0) or 0)
            total_tokens += int(getattr(action, "tokens", 0) or 0)
            stage_cost += int(getattr(action, "cost_microusd", 0) or 0)
            total_cost += int(getattr(action, "cost_microusd", 0) or 0)

            context = CheckContext(
                store=self.store, artifact=state, manifest=self.manifest,
                stage=stage, result_written=self.store.result_exists(),
            )
            try:
                self.gate.require(
                    action, stage=stage, context=context,
                    checks=self.checks, manifest=self.manifest,
                )
            except StageActionDenied as denied:
                stage_repairs += 1
                total_repairs += 1
                repair_by_code[denied.code] = repair_by_code.get(denied.code, 0) + 1
                ticket = make_ticket(denied, state=state, attempt=stage_repairs)
                last_repair_ticket = ticket
                last_check = CheckOutcome(
                    passed=False,
                    results=(CheckResult(
                        name="stage_action",
                        passed=False,
                        status="fail",
                        issues=(denied.message,),
                        required_fixes=(denied.message,),
                        artifact_sha256=state.sha256,
                    ),),
                    status="fail",
                    score=0,
                    issues=(denied.message,),
                    required_fixes=(denied.message,),
                    coverage={"required": [], "covered": [], "uncovered": [], "complete": False},
                    quality={"status": "unverified", "score": 0, "issues": [denied.message]},
                )
                journal.append({
                    "stage": stage.name, "event": "action_denied",
                    "tool": action.tool, "code": denied.code,
                    "failure_code": ACTION_REJECTED,
                    "message": denied.message[:300],
                    "repair_ticket": ticket,
                    "revision": state.revision,
                    "artifact_sha256": state.sha256,
                    "model_calls": total_model_calls,
                    "valid_actions": stage_actions,
                    "repairs": stage_repairs,
                })
                cap = repair_limit(denied.code)
                if (
                    stage.budget.repairs <= 0
                    or stage_repairs >= stage.budget.repairs
                    or repair_by_code[denied.code] >= cap
                ):
                    reason = (
                        ACTION_REPAIR_EXHAUSTED_PARTIAL
                        if stage_actions else ACTION_REPAIR_EXHAUSTED_NO_VALID
                    )
                    return fail(stage.name, reason, ticket=ticket)
                reason = stop_reason()
                if reason:
                    return fail(stage.name, reason, ticket=ticket)
                continue

            try:
                revision = (
                    self.executor(action, stage=stage, context=context)
                    if self.executor is not None else self.store.apply(action)
                )
            except Exception as error:  # noqa: BLE001 — host tool failures are typed outcomes
                if getattr(error, "capability_passthrough", False):
                    raise
                reason = str(getattr(error, "code", "") or ACTION_EXECUTION_FAILED)
                if reason in EXECUTION_REPAIR_CODES:
                    stage_repairs += 1
                    total_repairs += 1
                    repair_by_code[reason] = repair_by_code.get(reason, 0) + 1
                    ticket = RepairTicket(
                        repair_id=f"{stage.name}:{state.sha256[:12]}:{stage_repairs}",
                        stage=stage.name,
                        code=reason,
                        message=str(error)[:300],
                        violation={"message": str(error)[:300]},
                        constraint=(
                            "old_text must exist verbatim in artifact.content and "
                            "new_text must be non-empty; use an existing scene "
                            "element or keep the SECTION anchor"
                        ),
                        suggested_action="choose_existing_anchor",
                        remaining_repairs=max(0, stage.budget.repairs - stage_repairs),
                        remaining_by_code={
                            reason: max(
                                0,
                                repair_limit(reason)
                                - repair_by_code.get(reason, 0),
                            ),
                        },
                        artifact_sha256=state.sha256,
                        attempt=stage_repairs,
                    ).to_dict()
                    last_repair_ticket = ticket
                    last_check = CheckOutcome(
                        passed=False,
                        results=(CheckResult(
                            name="stage_action",
                            passed=False,
                            status="fail",
                            issues=(str(error),),
                            required_fixes=(str(error),),
                            artifact_sha256=state.sha256,
                        ),),
                        status="fail",
                        score=0,
                        issues=(str(error),),
                        required_fixes=(str(error),),
                        coverage={"required": [], "covered": [], "uncovered": [], "complete": False},
                        quality={"status": "unverified", "score": 0, "issues": [str(error)]},
                    )
                    journal.append({
                        "stage": stage.name, "event": "action_failed",
                        "tool": action.tool, "code": reason,
                        "failure_code": INVALID_TOOL_CALL,
                        "message": str(error)[:300],
                        "repair_ticket": ticket,
                        "artifact_sha256": state.sha256,
                        "model_calls": total_model_calls,
                        "valid_actions": stage_actions,
                        "repairs": stage_repairs,
                        "attempts": stage_attempts,
                    })
                    if (
                        stage.budget.repairs <= 0
                        or stage_repairs >= stage.budget.repairs
                        or repair_by_code[reason] >= repair_limit(reason)
                    ):
                        exhausted = (
                            ACTION_REPAIR_EXHAUSTED_PARTIAL
                            if stage_actions else ACTION_REPAIR_EXHAUSTED_NO_VALID
                        )
                        return fail(stage.name, exhausted, ticket=ticket)
                    stop = stop_reason()
                    if stop:
                        return fail(stage.name, stop, ticket=ticket)
                    continue

                stage_attempts += 1
                total_attempts += 1
                journal.append({
                    "stage": stage.name, "event": "action_failed",
                    "tool": action.tool, "code": reason,
                    "message": str(error)[:300], "artifact_sha256": state.sha256,
                    "model_calls": total_model_calls,
                    "valid_actions": total_valid_actions,
                    "repairs": total_repairs,
                    "attempts": total_attempts,
                })
                return fail(stage.name, reason)

            changed = bool(revision.changed)
            stage_attempts += 1
            total_attempts += 1

            after = CheckContext(
                store=self.store, artifact=revision, manifest=self.manifest,
                stage=stage, result_written=self.store.result_exists(),
            )
            outcome = self.checks.run(stage.exit_checks, after)
            last_check = outcome
            artifact_check_failed = not outcome.passed
            quality = dict(outcome.quality or {})
            composition = quality.get("composition_ok")
            if composition is True:
                best_content = str(revision.content)
                best_sha = revision.sha256
                best_quality = quality

            rolled_back = False
            if (
                composition is False
                and best_content is not None
                and revision.sha256 != best_sha
            ):
                restored = self.store.restore(best_content)
                rolled_back = True
                stage_repairs += 1
                total_repairs += 1
                repair_by_code["COMPOSITION_REGRESSION"] = (
                    repair_by_code.get("COMPOSITION_REGRESSION", 0) + 1
                )
                ticket = RepairTicket(
                    repair_id=f"{stage.name}:{revision.sha256[:12]}:{stage_repairs}",
                    stage=stage.name,
                    code="COMPOSITION_REGRESSION",
                    message=(
                        "the edit moved a wheel or main part outside the scene; "
                        "the artifact was restored to the last composition-safe revision"
                    ),
                    violation={
                        "offscreen_parts": list(quality.get("offscreen_parts") or [])[:5],
                    },
                    constraint=(
                        "keep both wheels and the bicycle/pelican inside the scene "
                        "while fixing legs and feet"
                    ),
                    suggested_action="restore_composition",
                    remaining_repairs=max(0, stage.budget.repairs - stage_repairs),
                    remaining_by_code={
                        "COMPOSITION_REGRESSION": max(
                            0,
                            repair_limit("COMPOSITION_REGRESSION")
                            - repair_by_code.get("COMPOSITION_REGRESSION", 0),
                        ),
                    },
                    artifact_sha256=restored.sha256,
                    attempt=stage_repairs,
                ).to_dict()
                last_repair_ticket = ticket
                journal.append({
                    "stage": stage.name, "event": "artifact_rolled_back",
                    "from_sha256": revision.sha256, "to_sha256": restored.sha256,
                    "reason": "composition_regression",
                    "issues": list(outcome.issues)[:5],
                    "repair_ticket": ticket,
                    "model_calls": total_model_calls,
                    "valid_actions": stage_actions,
                    "repairs": stage_repairs,
                    "attempts": stage_attempts,
                })
                journal.append({
                    "stage": stage.name, "event": "action_applied",
                    "tool": action.tool, "revision": restored.revision,
                    "artifact_sha256": restored.sha256, "changed": changed,
                    "valid_action": False, "rolled_back": True,
                    "model_calls": total_model_calls,
                    "valid_actions": stage_actions,
                    "repairs": stage_repairs,
                    "attempts": stage_attempts,
                    "check": outcome.summary(),
                })
                if (
                    stage.budget.repairs <= 0
                    or stage_repairs >= stage.budget.repairs
                    or repair_by_code["COMPOSITION_REGRESSION"]
                    >= repair_limit("COMPOSITION_REGRESSION")
                ):
                    exhausted = (
                        ACTION_REPAIR_EXHAUSTED_PARTIAL
                        if stage_actions else ACTION_REPAIR_EXHAUSTED_NO_VALID
                    )
                    return fail(stage.name, exhausted, ticket=ticket)
                stop = stop_reason()
                if stop:
                    return fail(stage.name, stop, ticket=ticket)
                continue

            if changed:
                stage_actions += 1
                total_valid_actions += 1
                no_delta = 0
                last_repair_ticket = None
            else:
                no_delta += 1
            journal.append({
                "stage": stage.name, "event": "action_applied",
                "tool": action.tool, "revision": revision.revision,
                "artifact_sha256": revision.sha256, "changed": changed,
                "valid_action": changed and not rolled_back,
                "rolled_back": False,
                "model_calls": total_model_calls,
                "valid_actions": stage_actions,
                "repairs": stage_repairs,
                "attempts": stage_attempts,
                "check": outcome.summary(),
            })
            if outcome.passed:
                following = self.manifest.next(stage)
                if following is None:
                    return CapabilityResult.succeeded_result(
                        kind=self.manifest.kind,
                        stage=stage.name,
                        artifact=self.store.identity(),
                        quality=dict(outcome.quality),
                        budget=budget_view(),
                        child_run_id=child_run_id,
                        journal=tuple(journal),
                    )
                journal.append({
                    "stage": stage.name, "event": "stage_transition",
                    "from": stage.name, "to": following.name,
                    "artifact_sha256": revision.sha256,
                })
                stage = following
                stage_attempts = stage_actions = stage_repairs = stage_tokens = stage_cost = 0
                no_delta = 0
                artifact_check_failed = False
                repair_by_code = {}
                last_repair_ticket = None
                continue

            reason = stop_reason()
            if reason:
                return fail(stage.name, reason)
        return fail(stage.name if stage else initial.name, ITERATION_LIMIT)

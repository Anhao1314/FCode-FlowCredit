"""Work Harness execution boundary for typed capabilities.

The capability package owns only stage transitions and artifact-derived checks.
This adapter owns everything with a durable or external effect: model charging,
operator control, permission decisions, tool execution, independent verification
and the parent task's event chain. It deliberately lives outside ``capabilities``
so the library remains independent of plans, loops and console code.
"""

from __future__ import annotations

from typing import Any

from ..capabilities.artifacts import Action
from ..capabilities.checks import CheckContext
from ..capabilities.manifest import StageSpec
from ..core.contracts import AgentError
from ..core.plan import Plan
from ..tools.general.motion_quality import analyze_motion, merge_quality

__all__ = ["HarnessCapabilityBridge"]


class HarnessCapabilityBridge:
    """Drive one capability through an existing Harness and parent task."""

    def __init__(
        self,
        harness: Any,
        task: Any,
        *,
        control: Any = None,
        observer: Any = None,
        loop: Any = None,
        motion_probe: Any = None,
        host_verify_times_ms: tuple[int, ...] = (),
    ) -> None:
        self.harness = harness
        self.task = task
        self.control = control
        self.observer = observer
        self.loop = loop
        self.motion_probe = motion_probe
        self.host_verify_times_ms = tuple(host_verify_times_ms)
        self.original_allowed = tuple(task.allowed_tools)
        self.store: Any = None
        self.checks: Any = None
        self.final_quality: dict[str, Any] = {}
        self._step: dict[str, Any] | None = None
        self._open_round: int | None = None

    def bind(self, runtime: Any) -> None:
        self.store = runtime.store
        self.checks = runtime.checks

    def _plan(self) -> Plan:
        return Plan(goal=self.task.objective, todos=[])

    @staticmethod
    def _passthrough(error: Exception) -> None:
        """Cancellation must not become a typed PROPOSER_EXHAUSTED failure."""
        if type(error).__name__ == "StopRequested":
            setattr(error, "capability_passthrough", True)

    def _finish_round(self) -> None:
        if self._open_round is None:
            return
        if self.observer is not None:
            self.observer.round_end(
                self._open_round, self.harness.budget(self.task).snapshot(), self._plan(),
            )
        self._open_round = None

    def _reload_task(self) -> None:
        """Re-read the durable task after a proposal path may have advanced it.

        ``Harness.propose`` records ``model_started`` (and possibly an unknown
        outcome) before a provider exception escapes. The adapter's in-memory
        task can therefore lag the store by one sequence; the eval/console
        writer would then hit SEQUENCE_CONFLICT when it records the capability
        result.
        """
        loader = getattr(self.harness.store, "load", None)
        if callable(loader):
            try:
                self.task = loader(self.task.id)
            except Exception:
                pass

    def finish(self) -> Any:
        self._finish_round()
        self._reload_task()
        return self.task

    def propose(self, packet: dict[str, Any], *, tools: tuple[str, ...]) -> Action:
        self._finish_round()
        round_no = self.task.model.attempts + 1
        try:
            if self.control is not None:
                controlled = self.control.before_round(
                    round_no=round_no, task=self.task, plan=self._plan(),
                )
                self.task = controlled.task
        except Exception as error:
            self._passthrough(error)
            raise

        available = tuple(
            name for name in tools
            if name in self.original_allowed and name in self.harness.registry.names
        )
        if not available:
            raise AgentError(
                "CAPABILITY_TOOL_UNAVAILABLE",
                f"stage {packet['stage']['name']!r} has no authorized registered tools",
            )
        # Keep task.allowed_tools immutable. Harness reads this per-call field
        # only to narrow the provider schemas; execution still uses the full
        # parent grant and the ordinary permission gate.
        context = {
            "prompt_profile": "capability-motion",
            "instruction": "Follow the current capability stage packet; propose exactly one action.",
            "objective": str(packet.get("goal") or self.task.objective),
            "tools": [self.harness.registry.get(name).to_schema() for name in available],
            "plan": [],
            "observations": [],
            "state": packet,
            "budget": dict(packet.get("remaining_budget") or {}),
            "round": round_no,
            "native_tools_allowed": available,
        }
        if self.observer is not None:
            before = self.harness.budget(self.task).snapshot()
            self.observer.round_start(round_no, before, self._plan())
        else:
            before = self.harness.budget(self.task).snapshot()
        self._open_round = round_no
        try:
            self.task, proposal = self.harness.propose(self.task, context)
        except Exception as error:
            self._reload_task()
            self._passthrough(error)
            raise
        after = self.harness.budget(self.task).snapshot()
        if proposal is None:
            failure = self.task.failure or {}
            code = str(failure.get("code") or "PROPOSER_EXHAUSTED")
            cause = str(failure.get("cause") or "")
            if code == "MODEL_OUTCOME_UNKNOWN" and cause:
                # The harness deliberately records transport-shaped outcomes as
                # unknown; the capability still needs the control-plane cause
                # (reasoning budget, empty content, no tool call) for repair and
                # budget accounting.
                code = cause
            raise AgentError(
                code,
                str(failure.get("message") or cause
                    or "the model returned no usable capability action"),
            )
        if self.observer is not None:
            self.observer.proposal(round_no, proposal)
        steps = list(proposal.plan.get("steps") or [])
        if len(steps) != 1:
            raise AgentError("CAPABILITY_ACTION_INVALID", "capability proposals contain exactly one action")
        if self.task.status == "planned":
            self.task = self.harness.finalize(
                self.task,
                "running",
                result=None,
                event="execution_started",
                detail={"kind": "motion.render", "round": round_no},
            )
        self._step = dict(steps[0])
        # Charge the capability for every provider dispatch, including the
        # harness's internal retry. ``proposal.tokens`` only describes the
        # successful attempt; the budget delta is the actual cost of this round.
        calls_delta = max(1, int(after.get("calls_used", 0)) - int(before.get("calls_used", 0)))
        tokens_delta = max(
            int(proposal.tokens),
            int(after.get("tokens_used", 0)) - int(before.get("tokens_used", 0)),
        )
        cost_delta = max(
            int(proposal.cost_microusd),
            int(after.get("cost_used_microusd", 0)) - int(before.get("cost_used_microusd", 0)),
        )
        forced = int(proposal.metadata.get("completion_tokens_override") or 0) > 0
        return Action(
            str(self._step["tool"]),
            dict(self._step.get("arguments") or {}),
            tokens=tokens_delta,
            cost_microusd=cost_delta,
            model_calls=calls_delta,
            note="reasoning_budget_retry" if forced else "",
        )

    def execute(
        self,
        action: Action,
        *,
        stage: StageSpec,
        context: CheckContext,
    ) -> Any:
        if self.store is None:
            raise RuntimeError("capability bridge is not bound to a runtime")
        step = self._step or {"id": stage.name, "tool": action.tool, "arguments": action.arguments}
        if step["tool"] != action.tool or (step.get("arguments") or {}) != action.arguments:
            raise AgentError("CAPABILITY_ACTION_CHANGED", "executed action differs from the validated proposal")
        round_no = self._open_round or self.task.model.attempts
        decision = self.harness.gate(self.task, step)
        if self.observer is not None:
            self.observer.gate(round_no, decision)
        if not decision.allowed:
            error = AgentError(
                decision.code or "CAPABILITY_ACTION_DENIED",
                decision.reason or "the host refused this capability action",
                details={"terminal": "denied"},
            )
            raise error

        try:
            if self.control is not None:
                controlled = self.control.before_tool(
                    round_no=round_no, task=self.task, step=step, decision=decision,
                )
                self.task = controlled.task
                if controlled.terminated:
                    error = AgentError(
                        controlled.code or "CAPABILITY_ACTION_DENIED",
                        controlled.reason or "the operator terminated this capability action",
                        details={"terminal": self.task.status},
                    )
                    raise error
                if not controlled.allowed:
                    self.task = self.harness.finalize(
                        self.task,
                        "denied",
                        failure={
                            "code": controlled.code or "CAPABILITY_ACTION_DENIED",
                            "message": controlled.reason or "the operator denied this action",
                            "retryable": False,
                        },
                        event="tool_denied",
                        detail={"code": controlled.code or "CAPABILITY_ACTION_DENIED"},
                    )
                    raise AgentError(
                        controlled.code or "CAPABILITY_ACTION_DENIED",
                        controlled.reason or "the operator denied this action",
                        details={"terminal": "denied"},
                    )
        except Exception as error:
            self._passthrough(error)
            raise

        before = self.store.snapshot()
        self.task, observation = self.harness.execute(self.task, step, round_no=round_no)
        if self.observer is not None:
            self.observer.observation(round_no, observation)
        revision = self.store.refresh(changed=self.store.snapshot().sha256 != before.sha256)
        if action.tool == "motion_check" and self.checks is not None:
            after_context = CheckContext(
                store=self.store,
                artifact=revision,
                manifest=context.manifest,
                stage=stage,
                result_written=self.store.result_exists(),
            )
            self.checks.prime_motion(after_context, observation.result)
        self._finish_round()
        return revision

    def _mark_verifying(self, revision: Any) -> None:
        """Enter the terminal ``verifying`` state only for a passing verdict."""
        if self.task.status == "running":
            self.task = self.harness.finalize(
                self.task,
                "verifying",
                result=None,
                event="verification_started",
                detail={"kind": "motion.render", "artifact_sha256": revision.sha256},
            )

    def verify(self, path: str) -> dict[str, Any]:
        """Run final host quality and any declared command before success."""
        if self.store is None:
            raise RuntimeError("capability bridge is not bound to a runtime")
        revision = self.store.snapshot()
        # Verification may fail and the delivery stage may retry. Keep the task
        # in ``running`` until the final verdict is known; a ``verifying`` state
        # is terminal and would block the bounded repair window.
        if self.motion_probe is not None:
            raw = self.motion_probe.probe(
                path,
                **({"times": self.host_verify_times_ms} if self.host_verify_times_ms else {}),
            )
            quality = merge_quality(analyze_motion(revision.content), raw)
            self.final_quality = quality
            if self.checks is not None:
                self.checks.record_quality(revision.sha256, quality)
            coverage = quality.get("coverage") or {}
            if quality.get("status") != "pass" or not coverage.get("complete"):
                return {
                    "ok": False,
                    "error": "HOST_QUALITY_FAILED",
                    "detail": {
                        "score": int(quality.get("score") or 0),
                        "issues": list(quality.get("issues") or [])[:5],
                    },
                }
        else:
            self.final_quality = dict(
                self.checks.quality_for(revision.sha256) if self.checks is not None else {},
            )

        if self.harness.verification is not None:
            if self.loop is None:
                raise AgentError(
                    "CAPABILITY_VERIFIER_UNAVAILABLE",
                    "a declared verification obligation requires the parent loop adapter",
                )
            # Verification is host-authored, not a stage action. The parent
            # task's immutable grant still contains its declared run_command.
            verified = {"result": {"path": revision.path, "sha256": revision.sha256}}
            self.task, outcome = self.loop._verify_obligation(
                self.task,
                verified,
                self._plan(),
                self.task.model.attempts,
            )
            self._mark_verifying(revision)
            return {"ok": True, "detail": outcome}
        self._mark_verifying(revision)
        return {"ok": True, "basis": "final_host_quality"}

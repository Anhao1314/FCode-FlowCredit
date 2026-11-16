"""Typed capability manifests: stages, checks and budgets.

A manifest is data, not a control-flow object. It says what a capability is
allowed to do at each stage, which checks must be true before the stage may be
left, and how much work one stage may spend. The runtime (``runtime.py``) is the
only place that interprets it, so a new capability is a new manifest plus a set
of check adapters — never a change to the conversation loop.

Deliberately absent: any checklist of model-authored obligations. Stages are a
*state machine over the artifact*, and a stage is left only when its exit checks
pass against the artifact's current bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["BudgetSpec", "CheckSpec", "StageSpec", "CapabilityManifest"]

_ON_FAIL = ("retry", "fallback", "stop")

#: Rule names the action gate understands. They are declared per stage so a
#: capability's restrictions are reviewable data, not hidden branch conditions.
ACTION_RULES = (
    "scene_edit_only",       # edit_file must target the scene anchor or an SVG element
    "any_edit",              # edit_file may target scene or styles
    "motion_check_allowed",  # the motion_check observation is permitted
    "result_write_only",     # write_file may only create the declared result path
    "motion_check_required",  # result write needs a passing check for this exact hash
)


@dataclass(frozen=True)
class BudgetSpec:
    """Integer-only budget; micro-units so nothing float enters the hash chain.

    Stage budgets are **guardrails, not terminal conditions**: the runtime uses
    artifact quality/no-progress and the caller's global budget to decide when
    to stop. ``calls`` still counts valid artifact actions applied in a stage,
    ``repairs`` counts gate-rejected proposals, and tokens/cost are charged
    independently in the runtime result.
    """

    calls: int
    tokens: int = 0
    cost_microusd: int = 0
    repairs: int = 2

    def __post_init__(self) -> None:
        for name in ("calls", "tokens", "cost_microusd", "repairs"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"BudgetSpec.{name} must be a non-negative integer")

    def remaining(
        self, *, calls: int, tokens: int, cost_microusd: int, repairs: int = 0,
    ) -> dict[str, int]:
        return {
            "calls": max(0, self.calls - calls),
            "valid_actions": max(0, self.calls - calls),
            "repairs": max(0, self.repairs - repairs),
            "tokens": max(0, self.tokens - tokens),
            "cost_microusd": max(0, self.cost_microusd - cost_microusd),
        }


@dataclass(frozen=True)
class CheckSpec:
    """One check declaration. ``kind`` is documentation for the host/UI."""

    name: str
    kind: str = "static"


@dataclass(frozen=True)
class StageSpec:
    """One stage of a capability's stage machine."""

    name: str
    allowed_tools: tuple[str, ...]
    exit_checks: tuple[str, ...]
    budget: BudgetSpec
    on_fail: str = "retry"
    action_rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("StageSpec.name must be non-empty")
        if self.on_fail not in _ON_FAIL:
            raise ValueError(f"StageSpec.on_fail must be one of {_ON_FAIL}")
        if not self.allowed_tools:
            raise ValueError("StageSpec.allowed_tools must not be empty")
        if not self.exit_checks:
            raise ValueError("StageSpec.exit_checks must not be empty")
        unknown = set(self.action_rules) - set(ACTION_RULES)
        if unknown:
            raise ValueError(f"unknown action rules: {sorted(unknown)}")


@dataclass(frozen=True)
class CapabilityManifest:
    """The full description of one typed capability."""

    kind: str
    when: str
    input_schema: dict[str, Any]
    stages: tuple[StageSpec, ...]
    checks: tuple[CheckSpec, ...]
    output_schema: dict[str, Any]
    artifact_path: str = "index.html"
    result_path: str = "FCODE_RESULT.md"
    #: Optional hints for capabilities that can start at a later stage when the
    #: artifact already contains work (a resumed capability, say).
    initial_stage_name: str = ""
    prefilled_stage_name: str = ""

    def __post_init__(self) -> None:
        if not self.kind or not self.kind.strip():
            raise ValueError("CapabilityManifest.kind must be non-empty")
        if not self.stages:
            raise ValueError("CapabilityManifest.stages must not be empty")
        names = [stage.name for stage in self.stages]
        if len(set(names)) != len(names):
            raise ValueError("stage names must be unique")
        declared = {check.name for check in self.checks}
        for stage in self.stages:
            missing = set(stage.exit_checks) - declared
            if missing:
                raise ValueError(f"stage {stage.name!r} names undeclared checks: {sorted(missing)}")
        for hint in (self.initial_stage_name, self.prefilled_stage_name):
            if hint and hint not in names:
                raise ValueError(f"stage hint {hint!r} is not in this manifest")

    def stage(self, name: str) -> StageSpec:
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise KeyError(f"unknown stage {name!r}")

    def check(self, name: str) -> CheckSpec:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(f"unknown check {name!r}")

    def initial_stage(self, artifact: Any = None) -> StageSpec | None:
        """The first stage, or a later one when the artifact is already seeded.

        ``artifact`` is optional: callers with no artifact start at the top.
        The prefilled hint only applies when the artifact reports a non-empty
        scene through ``scene_nonempty()`` — a host decision, not the model's.
        """
        if not self.stages:
            return None
        if self.prefilled_stage_name and artifact is not None:
            probe = getattr(artifact, "scene_nonempty", None)
            if callable(probe) and probe():
                return self.stage(self.prefilled_stage_name)
        return self.stage(self.initial_stage_name or self.stages[0].name)

    def next(self, stage: StageSpec) -> StageSpec | None:
        """The following stage, or None when the capability is complete."""
        for index, current in enumerate(self.stages):
            if current.name == stage.name:
                return self.stages[index + 1] if index + 1 < len(self.stages) else None
        raise KeyError(f"stage {stage.name!r} is not in this manifest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "when": self.when,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "artifact_path": self.artifact_path,
            "result_path": self.result_path,
            "stages": [
                {
                    "name": stage.name,
                    "allowed_tools": list(stage.allowed_tools),
                    "exit_checks": list(stage.exit_checks),
                    "budget": {
                        "calls": stage.budget.calls,
                        "repairs": stage.budget.repairs,
                        "tokens": stage.budget.tokens,
                        "cost_microusd": stage.budget.cost_microusd,
                    },
                    "on_fail": stage.on_fail,
                    "action_rules": list(stage.action_rules),
                }
                for stage in self.stages
            ],
            "checks": [{"name": check.name, "kind": check.kind} for check in self.checks],
        }

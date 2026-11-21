"""The ``motion.render`` capability: manifest + assembly.

Only the manifest and the check wiring are new. The artifact edits go through
the existing sandbox, the static verdict through ``analyze_motion``, the
rendered geometry through the host's ``MotionProbe`` (injected, so offline
scripted proposers can supply a plain dict/callable instead), and the seed
through ``motion_scaffold.ensure_motion_scaffold``.
"""

from __future__ import annotations

from typing import Any, Callable

from ..tools.general.motion_scaffold import (
    MOTION_SCAFFOLD_PATH,
    ensure_motion_scaffold,
)
from .artifacts import ArtifactStore
from .checks import CheckRegistry, default_registry
from .manifest import BudgetSpec, CapabilityManifest, CheckSpec, StageSpec
from .runtime import CapabilityRuntime, Proposer

__all__ = ["MOTION_KIND", "motion_manifest", "build_motion_capability"]

MOTION_KIND = "motion.render"


def motion_manifest() -> CapabilityManifest:
    """The four-stage motion stage machine (see docs/capabilities.md)."""
    return CapabilityManifest(
        kind=MOTION_KIND,
        when="a single-file HTML/SVG/CSS animation deliverable is requested",
        input_schema={
            "goal": "string",
            "artifact_path": "string (default index.html)",
            "constraints": "object",
            "budget": "object with integer calls/tokens/cost_microusd",
        },
        output_schema={
            "kind": "motion.render",
            "status": "succeeded|failed|cancelled",
            "stage": "string",
            "artifact": "object(path, sha256, bytes)",
            "quality": "object(status, score, coverage)",
            "required_fixes": "array",
            "budget": "object(calls_used, tokens_used, cost_microusd)",
            "reason": "string",
            "child_run_id": "string",
        },
        stages=(
            StageSpec(
                name="draft",
                allowed_tools=("edit_file",),
                exit_checks=("scene.nonempty",),
                budget=BudgetSpec(calls=2, tokens=60_000, repairs=3),
                on_fail="retry",
                action_rules=("scene_edit_only", "rig_locked"),
            ),
            StageSpec(
                name="structure",
                allowed_tools=("edit_file",),
                exit_checks=("hooks.all", "static.pass"),
                budget=BudgetSpec(calls=4, tokens=120_000, repairs=2),
                on_fail="retry",
                action_rules=("any_edit", "rig_locked"),
            ),
            StageSpec(
                name="motion",
                allowed_tools=("edit_file", "motion_check"),
                exit_checks=("probe.pass", "coverage.complete"),
                budget=BudgetSpec(calls=4, tokens=150_000, repairs=2),
                on_fail="retry",
                action_rules=("any_edit", "motion_check_allowed", "rig_locked"),
            ),
            StageSpec(
                name="delivery",
                # A failed final verifier is an artifact problem, not a reason
                # to stop while global calls remain. Keep a bounded repair
                # window: edit the artifact, re-check it, then re-verify.
                allowed_tools=("edit_file", "write_file", "motion_check", "verify"),
                # ``motion_check`` is part of the exit checks so a repair edit
                # re-records the pass for the new hash before verify runs.
                exit_checks=("motion_check", "verify.pass", "result.written"),
                budget=BudgetSpec(calls=3, tokens=60_000, repairs=1),
                on_fail="retry",
                action_rules=("result_write_only", "motion_check_required",
                              "rig_locked"),
            ),
        ),
        checks=(
            CheckSpec("scene.nonempty", "scene"),
            CheckSpec("hooks.all", "structure"),
            CheckSpec("static.pass", "static"),
            CheckSpec("motion_check", "observation"),
            CheckSpec("probe.pass", "probe"),
            CheckSpec("coverage.complete", "coverage"),
            CheckSpec("verify.pass", "verify"),
            CheckSpec("result.written", "result"),
        ),
        artifact_path=MOTION_SCAFFOLD_PATH,
        result_path="FCODE_RESULT.md",
        initial_stage_name="draft",
        prefilled_stage_name="structure",
    )


def build_motion_capability(
    sandbox: Any,
    proposer: Proposer,
    *,
    probe: Any = None,
    verify: Callable[[str], Any] | None = None,
    seed_scaffold: bool = True,
    seed_rig: bool = False,
    executor: Any = None,
    child_run_id: str = "",
) -> CapabilityRuntime:
    """Assemble the capability over an existing workspace sandbox.

    ``seed_rig`` trades some model freedom for geometry that cannot drift:
    the host seeds wheels/hip/pedals/legs/keyframes and the model fills the
    rider visuals around them. Product runs enable it; offline scripted tests
    keep the minimal scaffold unless they opt in.

    ``probe`` accepts the host's ``MotionProbe`` object (``probe(path)``), a
    callable, or a pre-baked dict — the last two keep offline scripted runs free
    of any browser dependency.
    """
    manifest = motion_manifest()
    if seed_scaffold:
        ensure_motion_scaffold(sandbox, rig=seed_rig)
    store = ArtifactStore(
        sandbox, path=manifest.artifact_path, result_path=manifest.result_path,
    )
    checks: CheckRegistry = default_registry(probe=probe, verify=verify)
    return CapabilityRuntime(
        manifest=manifest, store=store, checks=checks,
        proposer=proposer, child_run_id=child_run_id, executor=executor,
    )

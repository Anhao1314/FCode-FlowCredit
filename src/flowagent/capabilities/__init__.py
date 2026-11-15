"""Capability runtime (library slice).

Typed skills run inside their own stage machine and return one typed result to
the parent agent. This package is independent of the conversation loop: it
never reads a task plan, never settles model-authored obligations and never
renders its own trace into the conversation. The parent sees only
``CapabilityResult.to_dict()``; the internal journal is addressed by
``child_run_id``.

First slice: ``motion.render`` only, driven by a scripted proposer and inline
fixtures. Routing, a ``run_capability`` tool and UI stage cards are later work.
"""

from __future__ import annotations

from .artifacts import Action, ArtifactRevision, ArtifactStore
from .checks import CheckContext, CheckOutcome, CheckRegistry, CheckResult, default_registry
from .manifest import BudgetSpec, CapabilityManifest, CheckSpec, StageSpec
from .motion import MOTION_KIND, build_motion_capability, motion_manifest
from .result import CapabilityResult
from .runtime import (
    STAGE_ACTION_DENIED,
    ActionGate,
    CapabilityRuntime,
    Proposer,
    StageActionDenied,
)

__all__ = [
    "Action", "ArtifactRevision", "ArtifactStore",
    "BudgetSpec", "CapabilityManifest", "CheckSpec", "StageSpec",
    "CheckContext", "CheckOutcome", "CheckRegistry", "CheckResult", "default_registry",
    "CapabilityResult", "CapabilityRuntime", "ActionGate",
    "StageActionDenied", "STAGE_ACTION_DENIED", "Proposer",
    "MOTION_KIND", "motion_manifest", "build_motion_capability",
]

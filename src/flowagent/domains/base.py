"""Domain modules: the pluggable side of the kernel.

A module owns everything a particular line of work needs and nothing else:

* how to build its world (fixtures, data sources, synthetic or real),
* its toolset (the shared workspace tools are composed on top by the caller),
* its planner (the blueprint a deterministic provider follows),
* its verifier (independent recomputation — the only path to "succeeded"),
* which tools are consequential enough to pause for a human.

The kernel never imports a module. Adding a line of work means adding a module
and registering it; it does not mean teaching the runtime a new noun.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..tools.base import Observation, ToolSpec

__all__ = ["DomainModule"]


@dataclass(frozen=True)
class DomainModule:
    key: str
    title: str
    #: Build a fresh world. ``partial=True`` is used by failure-injection
    #: scenarios that need a deliberately incomplete corpus.
    create_world: Callable[..., Any]
    #: Build the module's own tools bound to a world.
    build_tools: Callable[..., list[ToolSpec]]
    #: A fresh planner instance (decompose/advance). Receives the module's
    #: world when the caller built one; modules that don't need it ignore it.
    make_planner: Callable[..., Any]
    #: (world, observation) -> {"verified": True, "verification": ..., "result": ...}
    verify: Callable[[Any, Observation], dict[str, Any]]
    #: Tools whose verified observations are the domain's answer.
    result_tools: tuple[str, ...] = ()
    #: Tools that pause for a human under the "consequential" policy.
    approval_tools: tuple[str, ...] = ()
    #: Which report template the console should use for this module.
    report_template: str = "generic"
    #: The domain's "what has been established so far" view over compressed
    #: observation records. Working memory calls this instead of knowing tool
    #: names; a module without one leaves the memory's neutral fallback.
    summarize_state: Callable[[Any], dict[str, Any]] | None = None
    #: What this domain's planner is told every round. ``None`` keeps the
    #: kernel's default wording, which is read-only by construction — the
    #: finance reference workload and its frozen v10 prompt depend on it. A
    #: module whose work is to *write* must say so: a real model obeys the
    #: instruction it is given, and a read-only instruction makes it refuse the
    #: very steps the plan requires (observed on a real run, not in theory).
    instruction: str | None = None
    #: Which system-prompt profile this domain's runs are bound by (see
    #: :data:`flowagent.providers.prompt.PROMPT_PROFILES`). ``None`` means "the
    #: kernel default", which is the frozen financial contract — so a domain
    #: that declares nothing keeps rendering the v10 pair, and its contexts stay
    #: byte-identical to the ones the SFT golden hash covers. A domain whose
    #: behaviour contract differs names its profile explicitly; there is no
    #: inference from the module key, the objective text or the tool list.
    prompt_profile: str | None = None
    #: True when this domain's plan may be **extended by the model**: its
    #: planner's blueprint is a starting point or an artifact obligation, not
    #: the whole task, and the model may propose additional todos that the host
    #: validates before they are persisted. False means the plan is exactly
    #: what the domain's planner wrote — a proposal that carries additions is
    #: refused, not silently applied.
    plan_extension: bool = False
    #: Build the declared result obligation for a world, or ``None`` when this
    #: delegation declared none. The kernel dispatches it and records it; the
    #: module judges the observation. ``None`` (the field's absence) means the
    #: domain has no such concept, and the terminal result then reports only
    #: what was actually recomputed.
    make_verification: Callable[..., Any] | None = None

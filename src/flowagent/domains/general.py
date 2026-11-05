"""General delegation: the default domain module and the general-agent
prototype.

The finance module stays as the reference workload that proves the runtime on
recomputable numbers. This module proves it on file writes, command execution
and web fetches — capabilities where "independent recomputation" generalises to
file-state hashes and honest not-verified labels. Same kernel, same gate, same
budget, same receipts; only the vocabulary changed.
"""

from __future__ import annotations

from typing import Any

from ..tools.base import ToolSpec
from ..tools.general.planner import GeneralPlanner
from ..tools.general.world import GeneralWorld
from ..providers.prompt import GENERAL_DOMAIN_INSTRUCTION, GENERAL_PROMPT_PROFILE
from .base import DomainModule

__all__ = [
    "GENERAL_DOMAIN",
    "GENERAL_INSTRUCTION",
    "create_world",
    "build_tools",
    "make_planner",
    "verify_observation",
    "summarize_state",
]

#: Backwards-compatible domain export; the prompt package owns the single
#: definition so the system contract and user-context instruction cannot drift.
GENERAL_INSTRUCTION = GENERAL_DOMAIN_INSTRUCTION

APPROVAL_TOOLS = ("write_file", "edit_file", "run_command")

#: ``report_blocked`` is deliberately *not* an approval tool here. It is a
#: read-effect declaration whose only consequence is that the runtime stops;
#: asking a human to authorise "I am blocked" would make an honest stop depend
#: on someone clicking, and a silent operator would turn it into
#: APPROVAL_TIMEOUT — a different fact about the world than "the evidence was
#: insufficient". Finance gates it because its blocker changes what its
#: calculator reports; the general domain's blocker changes nothing but the
#: terminal reason, which the loop owns.


def create_world(partial: bool = False, spec: dict[str, Any] | None = None, **kwargs: Any) -> GeneralWorld:
    """``partial`` is accepted for interface parity; a general delegation has
    no corpus to withhold. ``sandbox``/``fetcher``/``command_runner``/
    ``command_config`` may be injected by the console so the world shares the
    run directory's workspace and the host's command whitelist, and
    ``ask_operator`` carries the operator channel the question tool waits on.
    Whatever this function does not forward does not exist: the channel was
    dropped here once, and a real run asked a question nobody could answer."""
    return GeneralWorld(
        spec,
        sandbox=kwargs.get("sandbox"),
        fetcher=kwargs.get("fetcher"),
        command_runner=kwargs.get("command_runner"),
        command_config=kwargs.get("command_config"),
        ask_operator=kwargs.get("ask_operator"),
    )


def build_tools(world: GeneralWorld, **kwargs: Any) -> list[ToolSpec]:
    return world.build_tools()


def make_planner(world: GeneralWorld | None = None, **kwargs: Any) -> GeneralPlanner:
    if world is None:
        raise ValueError("the general planner needs the delegation's world (it carries the mode and inputs)")
    return GeneralPlanner(
        world.sources,
        report_path=world.report_path,
        mode=world.mode,
        script_path=world.script_path,
    )


def verify_observation(world: GeneralWorld, observation) -> dict[str, Any]:
    return world.verify_observation(observation)


def make_verification(world: GeneralWorld | None = None) -> Any:
    """The declared result obligation for this delegation, or ``None``.

    Returns the scope the runtime uses to re-run it. ``None`` is a real answer,
    not a failure: a delegation that declares no engineering-result obligation
    gets exactly the old behaviour, and the terminal result then reports that
    only file state was verified.
    """
    if world is None:
        return None
    return world.verification_scope()


def summarize_state(records) -> dict[str, Any]:
    """The general agent's "what has been established so far" card.

    Files are the facts: what was written (with its content hash), what was
    edited, what was read; commands with their exit codes; fetches with their
    status; open blockers. Every value comes from signed observations — the
    card is a projection, never a model note.
    """
    written: list[dict[str, Any]] = []
    edited: list[str] = []
    read: list[str] = []
    commands: list[dict[str, Any]] = []
    fetches: list[dict[str, Any]] = []
    listed = False
    blockers: list[str] = []
    for record in records:
        tool, result = record.tool, record.result
        if tool == "write_file":
            if result.get("changed", True):
                written.append({
                    "path": result.get("path"),
                    "bytes": result.get("bytes"),
                    "sha256": result.get("sha256_after"),
                })
        elif tool == "edit_file":
            if result.get("changed", True):
                edited.append(result.get("path"))
        elif tool == "read_file":
            read.append(result.get("path"))
        elif tool == "run_command":
            commands.append({
                "command": result.get("command"),
                "exit_code": result.get("exit_code"),
            })
            if result.get("exit_code") not in (0, None):
                blockers.append(f"command failed: {result.get('command')}")
        elif tool == "fetch_url":
            fetches.append({"url": result.get("url"), "status": result.get("status")})
        elif tool == "ls_workspace":
            listed = True
        elif tool == "report_blocked":
            # The model declared the work impossible. It belongs on the state
            # card as a blocker: a resumed run must see why the previous
            # attempt stopped instead of re-deriving it from the raw log.
            reason = str(result.get("reason") or "").strip()
            if reason:
                blockers.append(reason)
        if result.get("blockers"):
            blockers.extend(result["blockers"])
    return {
        "workspace_listed": listed,
        "files_written": written,
        "files_edited": sorted(set(edited)),
        "files_read": sorted(set(read)),
        "commands": commands,
        "fetches": fetches,
        "blockers": blockers,
    }


GENERAL_DOMAIN = DomainModule(
    instruction=GENERAL_INSTRUCTION,
    # The general contract, not the frozen financial one. This is the whole
    # point of the field: the model doing engineering work must not be told
    # that numbers are the only evidence, that reads are the only permitted
    # action, or that a blueprint must be finished even when it no longer fits.
    prompt_profile=GENERAL_PROMPT_PROFILE.key,
    # A general delegation's tasks are not known in advance, so its blueprint
    # is a starting point rather than the whole job: the model may propose
    # additional todos and the host validates every one before it is persisted.
    plan_extension=True,
    # And when the operator declared an engineering-result obligation, the
    # runtime re-runs it: the artifact hash is not the only thing this domain
    # can establish.
    make_verification=make_verification,
    key="general-work",
    title="通用委托",
    create_world=create_world,
    build_tools=build_tools,
    make_planner=make_planner,
    verify=verify_observation,
    result_tools=("write_file",),
    approval_tools=APPROVAL_TOOLS,
    report_template="generic",
    summarize_state=summarize_state,
)

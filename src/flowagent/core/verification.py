"""Declared result obligations the runtime re-runs before it reports success.

The gap this closes is narrow and was measured, not theorised. Before this
module, a general delegation's success condition was ``result_tools =
("write_file",)``: the runtime independently recomputed the *artifact file
state* and nothing else. So a run whose command failed (exit code 1, faithfully
recorded on the chain) and whose summary claimed the work was done still ended
``succeeded`` — the log was truthful and the result was wrong.

The fix is not a better guess about prose. It is an **explicit, host-authored
obligation** that the runtime re-executes itself:

* **Where the criterion comes from.** The delegation ``spec`` — written by the
  operator or the host, validated when the run is created, and frozen in
  ``task.binding``, which the store treats as immutable. The model has no
  channel to write it and neither do its proposals, so "make a failing task
  look successful by naming a passing command" is not a thing it can do: the
  command that decides is the declared one.
* **What the runtime does with it.** It dispatches the declared step through the
  ordinary gate, budget and event path — the same ``Harness.execute`` every
  model-proposed action uses — so authorization, pre-charging, durable
  recording and unknown-result semantics are inherited rather than reinvented.
  A recorded exit code from normal execution is *evidence*; it is never the
  proof. Only this fresh, runtime-produced run is.
* **What the runtime then checks.** The domain judges the observation: it must
  be an observation produced *inside* the verification scope, for exactly the
  declared command, with a known exit code equal to the declared expectation.
  A timeout or an unknown outcome fails closed.

``result_tools`` is deliberately untouched: the artifact obligation keeps its
existing meaning, and this is an additional obligation rather than a wider
reading of that one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .contracts import AgentError

__all__ = [
    "MAX_VERIFICATION_ARGS",
    "MAX_VERIFICATION_NAME_CHARS",
    "VERIFICATION_SOURCE_SPEC",
    "VERIFICATION_TOOL",
    "VerificationObligation",
    "VerificationScope",
    "build_obligation",
]

#: The one tool an obligation may use. Deliberately single: this round closes
#: the gap for a *declared command/result* obligation, not for arbitrary
#: validators, and a larger vocabulary would need its own authorization story.
VERIFICATION_TOOL = "run_command"

#: Where a criterion came from. Only one source exists today, and naming it on
#: the chain is the point: a reader can see the criterion was the host's.
VERIFICATION_SOURCE_SPEC = "delegation-spec"

MAX_VERIFICATION_NAME_CHARS = 200
MAX_VERIFICATION_ARGS = 32
#: A declaration must not be able to name a program by path: the command policy
#: resolves bare names against the host's allowlist, and a path would sidestep
#: that lookup. Same rule the policy itself applies to model proposals.
_FORBIDDEN_IN_COMMAND = ("/", "\\", ".")


@dataclass(frozen=True)
class VerificationObligation:
    """One declared, host-authored result obligation."""

    name: str
    tool: str
    arguments: dict[str, Any]
    expect: dict[str, Any]
    source: str = VERIFICATION_SOURCE_SPEC

    def step(self) -> dict[str, Any]:
        """The action the runtime dispatches.

        Built from the declaration only. A proposal is never consulted here —
        that is what makes the obligation immune to the model choosing an
        easier command.
        """
        return {"tool": self.tool, "arguments": dict(self.arguments)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "expect": dict(self.expect),
            "source": self.source,
        }


class VerificationScope(Protocol):
    """Freezes the workspace for one verification run and restores it after.

    The kernel knows only these three things: what the obligation is, that
    something must be done before the run, and that it must be undone after.
    How a domain freezes its workspace — and whether it can at all — is the
    domain's business, which is what keeps the kernel domain-agnostic.
    """

    obligation: VerificationObligation

    def prepare(self, artifact: dict[str, Any] | None) -> dict[str, Any]:
        """Freeze the state under test; return the evidence describing it.

        ``artifact`` is what the artifact verifier just accepted (path plus the
        hash it recomputed), so the frozen copy can be checked to contain the
        very bytes that were accepted. ``None`` when the task declared no
        artifact obligation.
        """
        ...

    def release(self) -> None: ...


def build_obligation(
    raw: Any,
    *,
    source: str = VERIFICATION_SOURCE_SPEC,
) -> VerificationObligation | None:
    """Validate a declared obligation. ``None`` means the task declared none.

    Refused at declaration time rather than at verification time: an operator
    typo discovered after the work is done would be an unverifiable task, and
    finding that out late is worse than being told at creation.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise AgentError("VERIFICATION_DECLARATION_INVALID", "verification must be an object")
    allowed = {"name", "command", "args", "expectExitCode", "timeoutSeconds"}
    unknown = set(raw) - allowed
    if unknown:
        raise AgentError(
            "VERIFICATION_DECLARATION_INVALID",
            f"verification does not accept {sorted(unknown)}; allowed: {sorted(allowed)}",
        )
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        raise AgentError(
            "VERIFICATION_DECLARATION_INVALID", "verification.command must be a non-empty string",
        )
    command = command.strip()
    if any(marker in command for marker in _FORBIDDEN_IN_COMMAND):
        raise AgentError(
            "VERIFICATION_DECLARATION_INVALID",
            f"verification.command must be a bare program name, got {command!r}",
        )
    args = raw.get("args") or []
    if not isinstance(args, list) or any(not isinstance(item, str) or not item for item in args):
        raise AgentError(
            "VERIFICATION_DECLARATION_INVALID",
            "verification.args must be a list of non-empty strings",
        )
    if len(args) > MAX_VERIFICATION_ARGS:
        raise AgentError(
            "VERIFICATION_DECLARATION_INVALID",
            f"verification.args accepts at most {MAX_VERIFICATION_ARGS} items",
        )
    expect = raw.get("expectExitCode", 0)
    if isinstance(expect, bool) or not isinstance(expect, int):
        raise AgentError(
            "VERIFICATION_DECLARATION_INVALID", "verification.expectExitCode must be an integer",
        )
    arguments: dict[str, Any] = {"command": command, "args": list(args)}
    timeout = raw.get("timeoutSeconds")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise AgentError(
                "VERIFICATION_DECLARATION_INVALID",
                "verification.timeoutSeconds must be an integer",
            )
        # The tool schema bounds it too; refusing here keeps the failure at
        # declaration time instead of at dispatch.
        if not 1 <= timeout <= 120:
            raise AgentError(
                "VERIFICATION_DECLARATION_INVALID",
                "verification.timeoutSeconds must be 1-120 seconds",
            )
        arguments["timeout_seconds"] = timeout
    name = str(raw.get("name") or " ".join([command, *args])).strip()
    return VerificationObligation(
        name=name[:MAX_VERIFICATION_NAME_CHARS],
        tool=VERIFICATION_TOOL,
        arguments=arguments,
        expect={"exit_code": expect},
        source=source,
    )
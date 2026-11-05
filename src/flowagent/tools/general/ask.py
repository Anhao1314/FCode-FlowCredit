"""``ask_operator``: the run stops and asks a human.

A general agent that cannot ask is a general agent that guesses. This tool is
the governed form of the question: it is a real registry tool, so it passes the
gate, charges the budget, is written to the chain like any other call, and its
*answer* arrives as the observation the model reads next round.

The human channel is the console's, not the kernel's: the world carries an
``ask`` callable that the assembly wires to the run's control (the same place
approvals wait). Deployments without an operator — the CLI demo, the offline
tests — get an honest "no channel" answer instead of a hang, and the model is
expected to carry on with its own judgement.

The answer is never invented. A timeout is reported as one.
"""

from __future__ import annotations

from typing import Any, Callable

from ...core.contracts import AgentError
from ..base import EFFECT_READ, Param, ToolSpec

__all__ = ["ANSWER_CHARS", "QUESTION_CHARS", "ask_spec", "build_ask_tool"]

#: A question is a question, not a briefing; an answer is a sentence or three.
QUESTION_CHARS = 400
ANSWER_CHARS = 600


def ask_spec(ask: Callable[..., Any] | None) -> ToolSpec:
    """The tool, bound to one run's operator channel (or to none)."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments.get("question") or "").strip()
        if not question:
            raise AgentError("ASK_QUESTION_EMPTY", "question must not be empty")
        if len(question) > QUESTION_CHARS:
            raise AgentError("ASK_QUESTION_TOO_LONG", f"question must be at most {QUESTION_CHARS} characters")
        why = str(arguments.get("why") or "").strip()[:200]
        if ask is None:
            # No human on this deployment. Say so plainly: the model can still
            # decide for itself, and the operator reading the trace sees why.
            return {
                "asked": question,
                "answered": False,
                "reason": "this deployment has no operator channel",
            }
        reply = ask(question=question, why=why)
        answered = bool(reply.get("answered"))
        out: dict[str, Any] = {"asked": question, "answered": answered}
        if answered:
            out["answer"] = str(reply.get("answer") or "")[:ANSWER_CHARS]
        else:
            # Never a fabricated answer: the model is told it is on its own.
            out["timed_out"] = bool(reply.get("timed_out"))
            out["reason"] = str(reply.get("reason") or "the operator did not answer")
        return out

    return ToolSpec(
        name="ask_operator",
        summary="Ask the operator one question and wait for the answer (or a timeout)",
        effect=EFFECT_READ,
        params=(
            Param(name="question", kind="string", required=True),
            Param(name="why", kind="string", required=False),
        ),
        handler=handler,
        # Only the two facts a reader can always rely on are required; the rest
        # appear exactly when they are true (an answer, or why there is none).
        outputs=(
            Param(name="asked", kind="string", required=True),
            Param(name="answered", kind="boolean", required=True),
            Param(name="answer", kind="string", required=False, nullable=True),
            Param(name="timed_out", kind="boolean", required=False, nullable=True),
            Param(name="reason", kind="string", required=False, nullable=True),
        ),
        version="1.0",
    )


def build_ask_tool(ask: Callable[..., Any] | None) -> ToolSpec:
    return ask_spec(ask)
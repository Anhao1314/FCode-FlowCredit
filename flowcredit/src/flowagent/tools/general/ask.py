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

__all__ = [
    "ANSWER_CHARS", "OPTION_CHARS", "OPTION_COUNT", "QUESTION_CHARS",
    "ask_spec", "build_ask_tool",
]

#: A question is a question, not a briefing; an answer is a sentence or three.
QUESTION_CHARS = 400
ANSWER_CHARS = 600

#: Suggested answers are a shortcut, not a form: a handful of short labels the
#: operator can accept instead of typing the same sentence. They are only hints
#: — choosing one submits that label as the same plain-text answer, so the
#: observation contract does not grow a second shape.
OPTION_COUNT = 4
OPTION_CHARS = 60


def _options(arguments: dict[str, Any]) -> list[str]:
    """Normalise suggested answers: trim, dedupe, cap. Never invent one."""
    out: list[str] = []
    for raw in arguments.get("options") or []:
        value = str(raw or "").strip()
        if not value or value in out:
            continue
        out.append(value[:OPTION_CHARS])
        if len(out) >= OPTION_COUNT:
            break
    return out


def ask_spec(ask: Callable[..., Any] | None) -> ToolSpec:
    """The tool, bound to one run's operator channel (or to none)."""

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments.get("question") or "").strip()
        if not question:
            raise AgentError("ASK_QUESTION_EMPTY", "question must not be empty")
        if len(question) > QUESTION_CHARS:
            raise AgentError("ASK_QUESTION_TOO_LONG", f"question must be at most {QUESTION_CHARS} characters")
        why = str(arguments.get("why") or "").strip()[:200]
        options = _options(arguments)
        if ask is None:
            # No human on this deployment. Say so plainly: the model can still
            # decide for itself, and the operator reading the trace sees why.
            return {
                "asked": question,
                "answered": False,
                "reason": "this deployment has no operator channel",
            }
        reply = ask(question=question, why=why, options=options)
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
            Param(
                name="question", kind="string", required=True,
                description="One concrete question the operator must answer; at most 400 characters.",
            ),
            Param(
                name="why", kind="string", required=False,
                description="Optional short reason this answer is needed; at most 200 characters.",
            ),
            Param(
                name="options", kind="array", required=False, maximum=OPTION_COUNT,
                description=(
                    "Optional 2–4 short suggested answers shown as one-click choices; "
                    f"each at most {OPTION_CHARS} characters. The operator may still type another answer."
                ),
            ),
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
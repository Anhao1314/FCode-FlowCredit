"""Goal, plan and todo: the durable shape of "what are we trying to do".

A workflow knows its steps in advance. An agent knows its *goal* in advance and
works out the steps, which means the step list must be a first-class, mutable,
inspectable object rather than a hardcoded sequence buried in a policy.

Two decisions are worth defending.

**The plan is durable, not in memory.** It is stored in the task body next to the
observation log, so a crash does not lose the intent, and the trace can show not
just what happened but what the agent believed it was doing.

**A todo must name its evidence.** Marking something done without pointing at the
observation that proves it is how an agent talks itself into finishing. The
`evidence` tuple is what makes "done" checkable by someone else.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .contracts import AgentError

__all__ = ["TODO_STATES", "Todo", "Plan"]

TODO_STATES = ("pending", "active", "done", "blocked", "abandoned")


@dataclass(frozen=True)
class Todo:
    """One unit of intended work.

    ``done_when`` is a domain-defined predicate key, not a sentence. The domain
    decides what satisfies it; the runtime only records the verdict. Keeping the
    predicate out of here is what lets the kernel stay domain-agnostic.
    """

    id: str
    text: str
    tool: str
    done_when: str
    arguments: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    evidence: tuple[str, ...] = ()
    reason: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "tool": self.tool,
            "done_when": self.done_when,
            "arguments": dict(self.arguments),
            "status": self.status,
            "evidence": list(self.evidence),
            "reason": self.reason,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Todo":
        return cls(
            id=data["id"],
            text=data["text"],
            tool=data["tool"],
            done_when=data["done_when"],
            arguments=dict(data.get("arguments") or {}),
            status=data.get("status", "pending"),
            evidence=tuple(data.get("evidence") or ()),
            reason=data.get("reason", ""),
            # Plans written before Phase 2b have no ``required`` key; every
            # todo in them was required by construction.
            required=bool(data.get("required", True)),
        )


@dataclass
class Plan:
    """An ordered, revisable list of todos for one goal."""

    goal: str
    todos: list[Todo] = field(default_factory=list)
    revision: int = 1

    # ------------------------------------------------------------------ state

    def next_open(self) -> Todo | None:
        """Return the next todo worth working on, or None if none is left."""
        for todo in self.todos:
            if todo.status in ("pending", "active"):
                return todo
        return None

    def open_count(self) -> int:
        return sum(1 for todo in self.todos if todo.status in ("pending", "active"))

    def is_complete(self) -> bool:
        """True when no work remains and no required todo was refused.

        An optional todo may finish as ``blocked``: the plan is complete, but
        the gap must still be reported by the domain (as an explicit
        missing-optional record, for example). A required ``blocked`` todo is
        never complete; the caller must stop instead of issuing a partial
        answer.
        """
        return all(
            todo.status in ("done", "abandoned")
            or (not todo.required and todo.status == "blocked")
            for todo in self.todos
        )

    def is_blocked(self) -> bool:
        """True when nothing is left to try and a required todo was refused."""
        if self.open_count():
            return False
        return any(todo.required and todo.status == "blocked" for todo in self.todos)

    def progress(self) -> dict[str, Any]:
        counts = {state: 0 for state in TODO_STATES}
        for todo in self.todos:
            counts[todo.status] = counts.get(todo.status, 0) + 1
        return {"revision": self.revision, "total": len(self.todos), **counts}

    # ---------------------------------------------------------------- updates

    def mark(
        self,
        todo_id: str,
        status: str,
        *,
        evidence: tuple[str, ...] = (),
        reason: str = "",
    ) -> "Plan":
        """Return a copy with one todo moved, rejecting unknown ids and states."""
        if status not in TODO_STATES:
            raise AgentError("TODO_STATE_INVALID", f"{status!r} is not a todo state")
        found = False
        updated: list[Todo] = []
        for todo in self.todos:
            if todo.id != todo_id:
                updated.append(todo)
                continue
            found = True
            if todo.status == "done" and status != "done":
                raise AgentError("TODO_REGRESSION", f"todo {todo_id} is already done")
            merged = tuple(dict.fromkeys((*todo.evidence, *evidence)))
            updated.append(replace(todo, status=status, evidence=merged, reason=reason or todo.reason))
        if not found:
            raise AgentError("TODO_UNKNOWN", f"no todo {todo_id!r} in this plan")
        return Plan(goal=self.goal, todos=updated, revision=self.revision)

    def revise(self, *, drop: tuple[str, ...] = (), add: tuple[Todo, ...] = (), reason: str = "") -> "Plan":
        """Produce the next revision: abandon some todos, append others.

        Revision increments so the trace can show *when* the agent changed its
        mind, which is usually the most informative moment in a run.
        """
        updated: list[Todo] = []
        for todo in self.todos:
            if todo.id in drop:
                updated.append(replace(todo, status="abandoned", reason=reason or todo.reason))
            else:
                updated.append(todo)
        updated.extend(add)
        return Plan(goal=self.goal, todos=updated, revision=self.revision + 1)

    # ------------------------------------------------------------------- i/o

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "revision": self.revision,
            "todos": [todo.to_dict() for todo in self.todos],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        return cls(
            goal=data["goal"],
            todos=[Todo.from_dict(item) for item in data.get("todos") or []],
            revision=int(data.get("revision", 1)),
        )

    def to_context(self) -> list[dict[str, Any]]:
        """The compact view handed to the model: what is left, what is settled."""
        return [
            {
                "id": todo.id,
                "text": todo.text,
                "tool": todo.tool,
                "arguments": dict(todo.arguments),
                "status": todo.status,
            }
            for todo in self.todos
            if todo.status in ("pending", "active", "blocked")
        ]

"""Subagents (S4): typed agent definitions, depth limits, typed results.

Ported shape from the dissected harness:

* an agent definition is ``{agent_type, when_to_use, tools, model}``;
* the parent sees one orchestrator tool whose ``subagent_type`` is an enum of
  allowed definitions, and each type's ``when_to_use`` is advertised;
* a subagent runs the same ``HarnessCore`` loop with its own tool subset and
  returns a typed result to the parent as a single tool_result;
* nesting is capped by ``max_depth``: once a subagent is at the limit, the
  orchestrator tool is simply not registered for it.

Subagent events go to the runner's bus (``subagent.start`` /
``subagent.end``); the child's internal tool events stay in its own bus so the
parent transcript does not turn into a flat pile of unrelated calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .core import Checker, HarnessCore, HarnessResult
from .events import EventBus
from .hooks import HookRunner
from .provider import StreamUsage, StreamingProvider
from .tools import ToolCallContext, ToolRegistry, ToolResult, ToolSpec

__all__ = [
    "AgentDefinition",
    "AgentRegistry",
    "ORCHESTRATOR_TOOL",
    "SubagentResult",
    "SubagentRunner",
]

ORCHESTRATOR_TOOL = "task"

DEFAULT_SUBAGENT_SYSTEM = (
    "You are a {agent_type} subagent working on one bounded assignment. "
    "Use only the tools you were given. When the assignment is done, reply "
    "with a short typed result: outcome, evidence (paths/commands), and any "
    "follow-up the parent must handle."
)


@dataclass(frozen=True)
class AgentDefinition:
    agent_type: str
    when_to_use: str = ""
    tools: tuple[str, ...] = ()
    model: str = ""
    max_turns: int = 8
    system: str = ""

    def __post_init__(self) -> None:
        if not str(self.agent_type or "").strip():
            raise ValueError("agent_type must not be empty")
        object.__setattr__(self, "agent_type", str(self.agent_type))
        object.__setattr__(self, "tools", tuple(str(tool) for tool in self.tools))
        object.__setattr__(self, "max_turns", max(1, int(self.max_turns)))


class AgentRegistry:
    def __init__(
        self,
        definitions: Iterable[AgentDefinition] = (),
        *,
        allowed_agent_types: Iterable[str] | None = None,
        max_depth: int = 1,
    ) -> None:
        self._definitions = {item.agent_type: item for item in definitions}
        self.allowed = (
            None if allowed_agent_types is None
            else {str(name) for name in allowed_agent_types}
        )
        self.max_depth = max(0, int(max_depth))

    def get(self, agent_type: str) -> AgentDefinition | None:
        definition = self._definitions.get(str(agent_type))
        if definition is None:
            return None
        if self.allowed is not None and definition.agent_type not in self.allowed:
            return None
        return definition

    def names(self) -> list[str]:
        return sorted(
            name for name in self._definitions
            if self.allowed is None or name in self.allowed
        )

    def known(self, agent_type: str) -> bool:
        return str(agent_type) in self._definitions

    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in self.names():
            definition = self._definitions[name]
            rows.append({
                "agent_type": definition.agent_type,
                "when_to_use": definition.when_to_use,
                "tools": list(definition.tools),
                "model": definition.model,
            })
        return rows


@dataclass
class SubagentResult:
    agent_type: str
    depth: int
    status: str  # completed | max_turns | failed
    text: str = ""
    turns: int = 0
    usage: StreamUsage = field(default_factory=StreamUsage)
    code: str = ""
    error: str = ""
    model: str = ""
    evidence_head: str = ""

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type,
            "depth": int(self.depth),
            "status": self.status,
            "text": self.text,
            "turns": int(self.turns),
            "usage": self.usage.to_dict(),
            "code": self.code,
            "error": self.error,
            "model": self.model,
            "evidence_head": self.evidence_head,
        }


ProviderFactory = Callable[[str], StreamingProvider]


class SubagentRunner:
    def __init__(
        self,
        provider: StreamingProvider,
        tools: ToolRegistry,
        registry: AgentRegistry,
        *,
        checker: Checker | None = None,
        bus: EventBus | None = None,
        hooks: HookRunner | None = None,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.registry = registry
        self.checker = checker
        self.bus = bus or EventBus()
        self.hooks = hooks
        self.provider_factory = provider_factory

    # ------------------------------------------------------------------ tools

    def orchestrator_tool(self, *, depth: int = 0) -> ToolSpec:
        rows = self.registry.describe()
        catalogue = "\n".join(
            f"- {row['agent_type']}: {row['when_to_use'] or 'no description'}"
            for row in rows
        ) or "- no subagents are registered"

        def handler(args: Mapping[str, Any], _context: ToolCallContext) -> ToolResult:
            outcome = self.spawn(
                str(args.get("subagent_type") or ""),
                str(args.get("prompt") or ""),
                depth=depth + 1,
            )
            content = outcome.text or outcome.error or outcome.code
            return ToolResult(
                content=content,
                is_error=not outcome.completed,
                code=outcome.code,
                data=outcome.to_dict(),
            )

        return ToolSpec(
            name=ORCHESTRATOR_TOOL,
            description=(
                "Launch a typed subagent for one bounded assignment. "
                f"Allowed types:\n{catalogue}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "prompt": {"type": "string"},
                    "subagent_type": {
                        "type": "string",
                        "enum": self.registry.names(),
                    },
                },
                "required": ["description", "prompt", "subagent_type"],
            },
            handler=handler,
        )

    # ------------------------------------------------------------------- spawn

    def spawn(
        self,
        agent_type: str,
        prompt: str,
        *,
        depth: int = 1,
        max_turns: int | None = None,
    ) -> SubagentResult:
        definition = self.registry.get(agent_type)
        if definition is None:
            allowed = self.registry.known(agent_type)
            return self._failure(
                agent_type, depth,
                "AGENT_NOT_ALLOWED" if allowed else "UNKNOWN_AGENT",
                (f"agent type {agent_type!r} is not allowed"
                 if allowed else
                 f"unknown agent type {agent_type!r}"))
        if depth < 1 or depth > self.registry.max_depth:
            return self._failure(
                agent_type, depth, "SUBAGENT_DEPTH",
                f"subagent depth {depth} exceeds max_depth "
                f"{self.registry.max_depth}")
        provider = self.provider
        if definition.model and self.provider_factory is not None:
            provider = self.provider_factory(definition.model)
        child_tools = self._child_tools(depth, definition)
        system = definition.system or DEFAULT_SUBAGENT_SYSTEM.format(
            agent_type=definition.agent_type)
        self.bus.emit(
            "subagent.start", agent_type=definition.agent_type, depth=depth,
            prompt_chars=len(prompt or ""), model=definition.model,
            tools=child_tools.names(),
        )
        core = HarnessCore(
            provider, child_tools,
            checker=self.checker, hooks=self.hooks,
        )
        result = core.run(
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            system=system,
            max_turns=max_turns or definition.max_turns,
        )
        outcome = self._from_harness(definition, depth, result)
        self.bus.emit(
            "subagent.end", agent_type=definition.agent_type, depth=depth,
            status=outcome.status, turns=outcome.turns, code=outcome.code,
            usage=outcome.usage.to_dict(),
            evidence_head=outcome.evidence_head,
        )
        return outcome

    # ----------------------------------------------------------------- private

    def _child_tools(self, depth: int,
                     definition: AgentDefinition) -> ToolRegistry:
        names = list(definition.tools) if definition.tools else self.tools.names()
        specs = [
            spec for spec in (self.tools.get(name) for name in names)
            if spec is not None and spec.name != ORCHESTRATOR_TOOL
        ]
        if depth < self.registry.max_depth:
            specs.append(self.orchestrator_tool(depth=depth))
        return ToolRegistry(specs)

    def _from_harness(
        self, definition: AgentDefinition, depth: int, result: HarnessResult,
    ) -> SubagentResult:
        return SubagentResult(
            agent_type=definition.agent_type,
            depth=depth,
            status=result.status,
            text=_final_text(result.messages),
            turns=result.turns,
            usage=result.usage,
            code=result.code,
            error=result.error,
            model=definition.model,
            evidence_head=result.evidence_head,
        )

    @staticmethod
    def _failure(agent_type: str, depth: int, code: str,
                 error: str) -> SubagentResult:
        return SubagentResult(
            agent_type=str(agent_type), depth=int(depth), status="failed",
            code=code, error=error,
        )


def _final_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(block.get("text") or "")
                for block in content
                if isinstance(block, Mapping) and block.get("type") == "text"
            ]
            return "\n".join(part for part in parts if part)
    return ""

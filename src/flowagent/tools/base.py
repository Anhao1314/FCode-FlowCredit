"""Tool abstractions: a closed schema, a declared effect, and an observation.

A tool is the only way the model can touch the world, so the shape of this
module matters more than its size. Two rules are encoded here:

* a tool declares its parameters as data, not as Python signature inspection,
  so the host can show the model exactly the same schema it will enforce;
* every tool declares an ``effect``. Phase 1 registers read-only effects only,
  and the runtime refuses to execute anything else. That refusal is what lets
  the demo say "the model cannot write" and mean it literally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from ..core.contracts import AgentError
# The vocabulary lives in core so memory can name its own input without
# importing tools. Re-exported here to keep the tool-facing API stable.
from ..core.observation import Observation, VALID_SUMMARY_POLICIES

__all__ = [
    "EFFECT_READ",
    "EFFECT_WRITE",
    "EFFECT_NETWORK",
    "Param",
    "ToolSpec",
    "Observation",
    "validate_arguments",
    "validate_output",
]

EFFECT_READ = "read"
#: A tool that produces an artifact. Allowed only where the task scope and the
#: host approval policy say so; the artifact store enforces the sandbox.
EFFECT_WRITE = "write"
#: A tool that reads a host-configured external source. The endpoint and the
#: credentials belong to the host; the model only names the source.
EFFECT_NETWORK = "network"


@dataclass(frozen=True)
class Param:
    """One declared parameter of a tool."""

    name: str
    kind: str  # "string" | "integer" | "boolean" | "number" | "array"
    required: bool = True
    enum: tuple[str, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    description: str = ""
    #: Output contracts only: an explicitly nullable field may be None.
    nullable: bool = False
    #: String parameters only: the maximum length accepted at the gate, so an
    #: oversized write (a whole file in a proposal, say) is refused *before*
    #: execution rather than by the handler after approval.
    max_length: int | None = None
    #: True for an argument that *identifies* the target (a path, a doc id, a
    #: URL); False for a body the proposer authors (file content, edit anchors).
    #: Result todos bind to observations by identity: a model that writes a
    #: better artifact must not fail the run, and the body is verified by hash
    #: anyway. Not part of ``to_schema`` — the model never sees this.
    identity: bool = True

    @classmethod
    def output(cls, name: str, kind: str, required: bool = True, nullable: bool = False) -> "Param":
        """Declare an output-contract field (shorthand for tool builders)."""
        return cls(name=name, kind=kind, required=required, nullable=nullable)

    def to_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.kind, "required": self.required}
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.description:
            schema["description"] = self.description
        if self.nullable:
            schema["nullable"] = True
        if self.max_length is not None:
            schema["maxLength"] = self.max_length
        return schema


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool: schema, effect and the callable that implements it."""

    name: str
    summary: str
    effect: str
    params: tuple[Param, ...]
    handler: Callable[[Mapping[str, Any]], dict[str, Any]]
    #: The fields this tool promises to return. Validated after every call, so
    #: a renamed or mistyped field fails the run instead of silently starving
    #: the memory summary and the report of data they were promised.
    outputs: tuple[Param, ...] = ()
    #: Semver-ish contract version; recorded in every observation.
    version: str = "1.0"
    #: How to compress this tool's result for working memory ("compressed,
    #: not raw" — memory/context.py). The tool author knows which fields a
    #: decision needs; tools without one fall back to a shape-based default.
    #: This only affects the prompt; the verifier and the chain always see the
    #: full result.
    summarize: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    #: P0-1 压缩策略选择(memory/context.py::summarizer_for 按此分发):
    #:   "custom"  — 必须同时提供 summarize,注册时缺失即报错(fail-closed);
    #:   "default" — 改进版默认 summarize(头尾保留、列表首项全量、嵌套一层);
    #:   "raw"     — 不做结构化压缩,仅单条截断保护(RAW_MAX_CHARS),
    #:               降级交给上下文折叠机制。
    summary_policy: str = "default"
    #: P1-1 缓存指纹:变更 summarize 实现或压缩配置时**必须手动 bump**
    #: (不自动推断版本——不哈希函数源码、不读常量值,fail-closed 显式声明)。
    #: 不 bump 会让增量缓存返回旧产物。
    summary_version: int = 1
    # 合法策略全集;registry 注册时校验,memory/context.summarizer_for 同源引用。

    def to_schema(self) -> dict[str, Any]:
        """Model-facing schema. Deliberately excludes outputs/version: the
        context handed to the model (and therefore the SFT dataset) must not
        change when a tool gains a contract."""
        return {
            "name": self.name,
            "summary": self.summary,
            "effect": self.effect,
            "parameters": {param.name: param.to_schema() for param in self.params},
        }

    def contract(self) -> dict[str, Any]:
        """Full contract for operators, tests and audits (not for the model)."""
        return {
            "name": self.name,
            "summary": self.summary,
            "version": self.version,
            "effect": self.effect,
            "parameters": {param.name: param.to_schema() for param in self.params},
            "outputs": {param.name: param.to_schema() for param in self.outputs},
        }


def validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Validate *arguments* against *spec* and return a normalised copy.

    Unknown keys are rejected rather than ignored. A model that invents a
    parameter has escaped the schema, and silently dropping the extra key would
    hide that from the trace.
    """
    if not isinstance(arguments, Mapping):
        raise AgentError("TOOL_ARGUMENTS_INVALID", f"{spec.name} expects an object of arguments")

    declared = {param.name: param for param in spec.params}
    unknown = set(arguments) - set(declared)
    if unknown:
        raise AgentError(
            "TOOL_ARGUMENTS_INVALID",
            f"{spec.name} does not accept {sorted(unknown)}",
        )

    normalised: dict[str, Any] = {}
    for param in spec.params:
        if param.name not in arguments:
            if param.required:
                raise AgentError(
                    "TOOL_ARGUMENTS_INVALID",
                    f"{spec.name} requires {param.name!r}",
                )
            continue
        value = arguments[param.name]
        if param.kind == "string":
            if not isinstance(value, str) or not value:
                raise AgentError("TOOL_ARGUMENTS_INVALID", f"{param.name!r} must be a non-empty string")
            if param.enum is not None and value not in param.enum:
                raise AgentError(
                    "TOOL_ARGUMENTS_INVALID",
                    f"{param.name!r} must be one of {list(param.enum)}",
                )
            if param.max_length is not None and len(value) > param.max_length:
                raise AgentError(
                    "TOOL_ARGUMENTS_INVALID",
                    f"{param.name!r} exceeds {param.max_length} characters",
                )
        elif param.kind == "array":
            if not isinstance(value, (list, tuple)) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise AgentError(
                    "TOOL_ARGUMENTS_INVALID",
                    f"{param.name!r} must be an array of non-empty strings",
                )
            if param.maximum is not None and len(value) > param.maximum:
                raise AgentError(
                    "TOOL_ARGUMENTS_INVALID",
                    f"{param.name!r} accepts at most {param.maximum} items",
                )
        elif param.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise AgentError("TOOL_ARGUMENTS_INVALID", f"{param.name!r} must be an integer")
            if param.minimum is not None and value < param.minimum:
                raise AgentError("TOOL_ARGUMENTS_INVALID", f"{param.name!r} is below {param.minimum}")
            if param.maximum is not None and value > param.maximum:
                raise AgentError("TOOL_ARGUMENTS_INVALID", f"{param.name!r} is above {param.maximum}")
        elif param.kind == "boolean":
            # Same rule as the output contract: a bool is a bool; 0/1 are not.
            if not isinstance(value, bool):
                raise AgentError("TOOL_ARGUMENTS_INVALID", f"{param.name!r} must be a boolean")
        elif param.kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AgentError("TOOL_ARGUMENTS_INVALID", f"{param.name!r} must be a number")
        else:  # pragma: no cover - guards a programming error, not user input
            raise AgentError("TOOL_SCHEMA_INVALID", f"unknown parameter kind {param.kind!r}")
        normalised[param.name] = value
    return normalised


def validate_output(spec: ToolSpec, result: Mapping[str, Any]) -> None:
    """Check a tool result against its declared output contract.

    Only declared fields are checked; extra fields are allowed because a tool
    may add information without breaking readers. A required field that is
    missing, a present field with the wrong type, or an unexpected ``None`` is
    a contract violation: the runtime refuses to record the observation rather
    than persisting a shape downstream readers were never promised.
    """
    if not isinstance(result, Mapping):
        raise AgentError(
            "TOOL_OUTPUT_CONTRACT",
            f"{spec.name} v{spec.version} must return an object",
        )
    for param in spec.outputs:
        if param.name not in result:
            if param.required:
                raise AgentError(
                    "TOOL_OUTPUT_CONTRACT",
                    f"{spec.name} v{spec.version} is missing output field {param.name!r}",
                )
            continue
        value = result[param.name]
        if value is None:
            if param.nullable:
                continue
            raise AgentError(
                "TOOL_OUTPUT_CONTRACT",
                f"{spec.name} v{spec.version} output {param.name!r} is null",
            )
        if param.kind == "string":
            ok = isinstance(value, str)
        elif param.kind == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif param.kind == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif param.kind == "boolean":
            ok = isinstance(value, bool)
        elif param.kind == "object":
            ok = isinstance(value, Mapping)
        elif param.kind == "array":
            ok = isinstance(value, (list, tuple))
        else:  # pragma: no cover - programming error in a tool spec
            raise AgentError("TOOL_SCHEMA_INVALID", f"{spec.name}: unknown output kind {param.kind!r}")
        if not ok:
            raise AgentError(
                "TOOL_OUTPUT_CONTRACT",
                f"{spec.name} v{spec.version} output {param.name!r} must be {param.kind}",
            )
        if param.enum is not None and value not in param.enum:
            raise AgentError(
                "TOOL_OUTPUT_CONTRACT",
                f"{spec.name} v{spec.version} output {param.name!r} must be one of {list(param.enum)}",
            )

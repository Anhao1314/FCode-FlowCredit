"""Tool protocol: schema-validated protocol objects, not bare functions.

Ported shape from the dissected harness: a tool is
``{name, description, input_schema, handler}``. Validation happens *before*
execution, handlers get an explicit call context, and every failure is a
typed ``ToolResult`` the model can read and repair — never an exception that
kills the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "ToolCallContext",
    "ToolInputError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "validate_input",
]

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
}


class ToolInputError(ValueError):
    """Schema validation refused the arguments (a typed refusal, not a crash)."""

    code = "TOOL_INPUT_INVALID"


@dataclass(frozen=True)
class ToolCallContext:
    call_id: str
    tool_name: str
    turn: int
    events: Any = None


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    code: str = ""
    data: Mapping[str, Any] | None = None

    @classmethod
    def ok(cls, content: str = "", data: Mapping[str, Any] | None = None) -> "ToolResult":
        return cls(content=content, data=data)

    @classmethod
    def error(cls, code: str, message: str) -> "ToolResult":
        return cls(content=message, is_error=True, code=code)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any], ToolCallContext], Any] = field(repr=False)

    def describe(self) -> dict[str, Any]:
        """The wire-facing schema (what the provider advertises)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
        }


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool {spec.name!r}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def describe(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        wanted = set(names) if names is not None else set(self._specs)
        return [
            spec.describe()
            for name, spec in self._specs.items()
            if name in wanted
        ]


def validate_input(schema: Mapping[str, Any], value: Any) -> None:
    """Validate a JSON-schema subset: type object/array/string/number/...,
    ``required``, ``properties``, ``items`` and ``enum``.

    Deliberately small: the harness only needs to refuse obviously wrong
    arguments before the handler runs; full JSON Schema is out of scope.
    """
    _validate(schema, value, path="$")


def _validate(schema: Mapping[str, Any], value: Any, *, path: str) -> None:
    declared = schema.get("type")
    if isinstance(declared, list):
        if not any(_matches(item, value) for item in declared):
            raise ToolInputError(f"{path} must be one of {declared}")
    elif isinstance(declared, str) and not _matches(declared, value):
        raise ToolInputError(f"{path} must be {declared}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise ToolInputError(f"{path} must be one of {enum}")

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                raise ToolInputError(f"{path}.{key} is required")
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, Mapping):
                _validate(child, item, path=f"{path}.{key}")
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate(items, item, path=f"{path}[{index}]")


def _matches(declared: str, value: Any) -> bool:
    if declared == "null":
        return value is None
    expected = _JSON_TYPES.get(declared)
    if expected is None:
        return True
    if declared in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, expected)

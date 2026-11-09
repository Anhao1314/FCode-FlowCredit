"""ToolSpec → OpenAI function-calling schema.

One implementation shared by the chat tool loop and the fast proposal path.
Keeping it here (pure registry transform, no chat/runtime imports) lets the
provider layer reuse it without pulling the conversational stack in.
"""

from __future__ import annotations

from typing import Any

from ..tools.registry import ToolRegistry

__all__ = ["to_openai_tools"]

_KIND_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
}


def to_openai_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Render every registered tool as an OpenAI ``tools`` entry."""
    tools: list[dict[str, Any]] = []
    for name in registry.names:
        spec = registry.get(name)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in spec.params:
            schema = param.to_schema()
            schema.pop("required", None)
            schema.pop("nullable", None)
            schema["type"] = _KIND_MAP.get(str(schema.get("type")), "string")
            if param.enum is not None:
                schema["enum"] = list(param.enum)
            properties[param.name] = schema
            if param.required:
                required.append(param.name)
        tools.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.summary,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        })
    return tools
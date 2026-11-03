"""A deliberately small JSON Schema subset validator, stdlib only.

The audit contracts (audit-rules/schema/*.json) are written against
exactly the keywords this module supports: type / const / enum / pattern /
required / properties / additionalProperties / items / minItems /
minimum. No $ref, no $defs, no conditionals — if a schema starts needing
more, grow this validator deliberately, with tests.

The point is not completeness; it is that the contracts are enforceable
at runtime by flowaudit itself, without a dependency, and that CI
validates every committed example against them.
"""

from __future__ import annotations

import re
from typing import Any


def validate(instance: Any, schema: dict, path: str = "$") -> list[str]:
    """Validate one instance against the supported keyword subset.

    Returns a list of violation paths (empty means conforming).
    """
    errors: list[str] = []
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(instance, dict):
            return [f"{path}: expected object, got {type(instance).__name__}"]
    elif kind == "string":
        if not isinstance(instance, str):
            return [f"{path}: expected string"]
    elif kind == "boolean":
        if not isinstance(instance, bool):
            return [f"{path}: expected boolean"]
    elif kind == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            return [f"{path}: expected integer"]
    elif kind == "array":
        if not isinstance(instance, list):
            return [f"{path}: expected array"]

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum")
    if "pattern" in schema and isinstance(instance, str):
        if not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} fails pattern {schema['pattern']!r}")
    if "minimum" in schema and isinstance(instance, (int, float)) \
            and not isinstance(instance, bool):
        if instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in properties:
                    errors.append(f"{path}: unexpected property {key!r}")
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], f"{path}.{key}"))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return errors

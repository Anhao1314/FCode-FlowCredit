"""Tool registry: the single source of truth for what the model may call.

The registry does three things and nothing else: hold the specs, expose a
schema catalogue to the model, and dispatch a validated call. It never decides
*permission* — that is the runtime's gate. Keeping dispatch and authorisation
apart is what lets the demo show a denied call that still had a valid schema.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..core.contracts import AgentError
from ..core.hashing import digest
from .base import (EFFECT_NETWORK, EFFECT_READ, EFFECT_WRITE, Observation,
                   VALID_SUMMARY_POLICIES, ToolSpec, validate_arguments,
                   validate_output)

__all__ = ["ToolRegistry"]


class ToolRegistry:
    """An immutable, ordered collection of :class:`ToolSpec`."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        ordered = list(specs)
        names = [spec.name for spec in ordered]
        if len(names) != len(set(names)):
            raise AgentError("TOOL_REGISTRY_INVALID", "duplicate tool name")
        for spec in ordered:
            # P0-1 fail-closed:策略必须合法;"custom" 策略承诺了自定义压缩器,
            # 缺失即拒绝注册——带病注册会让该工具在 prompt 里退化为盲摘要,
            # 且无人察觉。
            if spec.summary_policy not in VALID_SUMMARY_POLICIES:
                raise AgentError(
                    "TOOL_SUMMARY_POLICY_INVALID",
                    f"{spec.name!r} has unknown summary_policy "
                    f"{spec.summary_policy!r}",
                )
            if spec.summary_policy == "custom" and spec.summarize is None:
                raise AgentError(
                    "TOOL_SUMMARY_POLICY_INVALID",
                    f"{spec.name!r} declares summary_policy='custom' "
                    "but provides no summarize implementation",
                )
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in ordered}

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise AgentError("TOOL_NOT_REGISTERED", f"{name!r} is not a registered tool")
        return spec

    def catalog(self) -> list[dict[str, Any]]:
        """Return the schema list handed to the model."""
        return [spec.to_schema() for spec in self._specs.values()]

    def summarizers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        """Per-tool memory compressors declared on their specs."""
        return {
            name: spec.summarize
            for name, spec in self._specs.items()
            if spec.summarize is not None
        }

    def execute(self, name: str, arguments: Mapping[str, Any]) -> Observation:
        """Validate, run and wrap the result in an :class:`Observation`."""
        spec = self.get(name)
        if spec.effect not in (EFFECT_READ, EFFECT_WRITE, EFFECT_NETWORK):
            raise AgentError(
                "TOOL_EFFECT_DENIED",
                f"{name!r} has unknown effect {spec.effect!r}",
            )
        # A write tool is not gated here: execution requires the tool to be in
        # the task scope and the host grant, and the console's approval policy
        # names write tools as consequential. The handler's artifact store owns
        # the sandbox (atomic, content-addressed, append-only).
        normalised = validate_arguments(spec, arguments)
        result = spec.handler(normalised)
        validate_output(spec, result)
        return Observation(
            tool=name,
            arguments=normalised,
            result=result,
            result_hash=digest(result),
            tool_version=spec.version,
        )

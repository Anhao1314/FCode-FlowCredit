"""Cross-layer vocabulary: what a tool returned, and how much to trust it.

This module exists to break a cycle, not to add a layer. ``Observation`` is
produced by the capability layer, consumed by memory and the runtime, and
recorded by trajectory. If it lived in ``tools``, then ``memory`` would have to
import ``tools`` just to name its own input — and the two layers could no
longer be tested or replaced independently.

Nothing here knows about tools, stores or providers. That is the point.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = ["Observation", "SourceTrust", "VALID_SUMMARY_POLICIES"]

#: Legal compression policies for a ToolSpec. The registry validates against
#: this tuple on registration; memory dispatches on the same source of truth.
VALID_SUMMARY_POLICIES: tuple[str, ...] = ("default", "custom", "detailed", "raw")


class SourceTrust(str, Enum):
    """P0-2 observation provenance. Default is untrusted (fail-closed).

    VERIFIED_TOOL      — in the registry, deterministically executed by the host;
    AGENT_INFERRED     — the model's own reasoning, not a tool observation;
    EXTERNAL_UNTRUSTED — MCP / external HTTP / unknown: isolate before prompt.
    """

    VERIFIED_TOOL = "verified_tool"
    AGENT_INFERRED = "agent_inferred"
    EXTERNAL_UNTRUSTED = "external_untrusted"


@dataclass(frozen=True)
class Observation:
    """What a tool returned, plus the hash the runtime will re-check later."""

    tool: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    result_hash: str = ""
    tool_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "result": self.result,
            "result_hash": self.result_hash,
            "tool_version": self.tool_version,
        }
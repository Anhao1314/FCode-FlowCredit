"""Minimal harness subset ported for governance context compaction.

Only the streaming provider contract and the nine-section summarizer are
vendored here; the rest of the harness migration lives on its own branch.
"""

from .compaction import (
    SUMMARY_SECTIONS,
    build_summary_prompt,
    ensure_earlier_asks,
    estimate_messages_tokens,
    estimate_text_tokens,
    render_conversation,
    strip_analysis,
)
from .provider import (
    StreamRequest,
    StreamUsage,
    StreamingProvider,
    TextDelta,
    collect_stream,
)

__all__ = [
    "SUMMARY_SECTIONS",
    "StreamRequest",
    "StreamUsage",
    "StreamingProvider",
    "TextDelta",
    "build_summary_prompt",
    "collect_stream",
    "ensure_earlier_asks",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "render_conversation",
    "strip_analysis",
]

"""Claude-Code-style harness core (surgery S1+S2).

This package is a *behavioral port* of the harness layer we dissected in
2.1.302: a streamed tool-use loop, a tool protocol with explicit lifecycle
events (``tool.describe`` / ``tool.check`` / ``tool.call`` / ``tool.result``),
per-call deadlines, and S2's permission + hook gates. It is deliberately
independent from the existing ``runtime/`` and ``capabilities/`` packages:
only unit-tested building blocks ship here, and the Work product path keeps
running the old harness until the migration stage.

Architecture notes (ported contracts, re-implemented in Python):

* tools are protocol objects, not bare functions: name + description +
  input schema + handler, with validation before execution;
* every call crosses a check point: PreToolUse hooks may block/rewrite, then
  three rule sets (deny > ask > allow, managed first) decide, with ``ask``
  resolved by an injected approver;
* ten lifecycle hook events exist; the loop owns SessionStart/PostToolUse/Stop
  while PreToolUse belongs to the permission pipeline;
* model calls stream and carry a wall-clock deadline; a slow stream or a
  slow tool cannot hold the loop forever;
* the loop is a thin ``propose -> dispatch -> append`` cycle, capped by turns,
  with no domain knowledge inside it.
"""

from .compaction import (
    COMPACTION_MARKER,
    SUMMARY_SECTIONS,
    Compaction,
    CompactionConfig,
    CompactionError,
    CompactionResult,
    build_summary_prompt,
    ensure_earlier_asks,
    estimate_messages_tokens,
    estimate_text_tokens,
    extract_section,
    render_conversation,
    strip_analysis,
)
from .background import (
    BackgroundLimitError,
    BackgroundTask,
    BackgroundTasks,
    background_tools,
    kill_shell_tool,
    run_background_tool,
    task_output_tool,
)
from .core import CheckDecision, HarnessCore, HarnessResult, HarnessTimeout
from .evidence import (
    EvidenceChain,
    EvidenceReceipt,
    verify_evidence_chain,
)
from .events import EventBus, HarnessEvent
from .hooks import HOOK_EVENTS, HookOutcome, HookRunner, HookSpec
from .permissions import (
    BEHAVIORS,
    Decision,
    PermissionPolicy,
    PermissionRule,
    RuleSyntaxError,
    WRITE_TOOLS,
    default_content,
)
from .pipeline import PermissionPipeline
from .provider import (
    CollectedStream,
    Done,
    ReasoningDelta,
    StreamEvent,
    StreamRequest,
    StreamTimeout,
    StreamUsage,
    StreamingProvider,
    TextDelta,
    ToolUse,
    collect_stream,
)
from .session import FileHistory, SessionStore, project_slug
from .subagents import (
    AgentDefinition,
    AgentRegistry,
    DEFAULT_SUBAGENT_SYSTEM,
    ORCHESTRATOR_TOOL,
    SubagentResult,
    SubagentRunner,
)
from .settings import (
    LoadedSettings,
    SETTINGS_SOURCES,
    load_settings,
    settings_paths,
)
from .tools import (
    ToolInputError,
    ToolResult,
    ToolSpec,
    ToolRegistry,
    validate_input,
)

__all__ = [
    "BEHAVIORS",
    "COMPACTION_MARKER",
    "CheckDecision",
    "CollectedStream",
    "Compaction",
    "CompactionConfig",
    "CompactionError",
    "CompactionResult",
    "Decision",
    "Done",
    "EventBus",
    "EvidenceChain",
    "EvidenceReceipt",
    "FileHistory",
    "HOOK_EVENTS",
    "HarnessCore",
    "HarnessEvent",
    "HarnessResult",
    "HarnessTimeout",
    "HookOutcome",
    "HookRunner",
    "HookSpec",
    "LoadedSettings",
    "ORCHESTRATOR_TOOL",
    "PermissionPipeline",
    "PermissionPolicy",
    "PermissionRule",
    "ReasoningDelta",
    "RuleSyntaxError",
    "SETTINGS_SOURCES",
    "SUMMARY_SECTIONS",
    "SessionStore",
    "StreamEvent",
    "StreamRequest",
    "StreamTimeout",
    "StreamUsage",
    "StreamingProvider",
    "SubagentResult",
    "SubagentRunner",
    "AgentDefinition",
    "AgentRegistry",
    "BackgroundLimitError",
    "BackgroundTask",
    "BackgroundTasks",
    "DEFAULT_SUBAGENT_SYSTEM",
    "TextDelta",
    "ToolInputError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolUse",
    "WRITE_TOOLS",
    "background_tools",
    "build_summary_prompt",
    "collect_stream",
    "default_content",
    "ensure_earlier_asks",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "extract_section",
    "kill_shell_tool",
    "load_settings",
    "project_slug",
    "render_conversation",
    "run_background_tool",
    "settings_paths",
    "strip_analysis",
    "task_output_tool",
    "validate_input",
    "verify_evidence_chain",
]

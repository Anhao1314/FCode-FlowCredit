"""Permission pipeline (S2): three rule sets, matchers, modes, managed layer.

Ported semantics from the dissected harness:

* rules are ``Tool(pattern)`` entries in one of three sets —
  ``allow`` / ``ask`` / ``deny`` — each with a source (managed/project/user/local);
* a tool's *rule content* comes from its arguments (Bash → command,
  edit/write → path, WebFetch → host) via a small resolver table;
* precedence is deny > ask > allow, with managed-source rules evaluated
  first inside each set; mode supplies the default when no rule matches;
* ``bypassPermissions`` is gated by availability, and allow rules can be
  shed under context pressure (they are advisory, never security-critical).

This module is deliberately pure: it never executes tools and never prompts.
An ``ask`` decision is resolved by an injected approver; without one it is a
deny with an explicit reason.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "BEHAVIORS",
    "Decision",
    "PermissionPolicy",
    "PermissionRule",
    "RuleSyntaxError",
    "WRITE_TOOLS",
    "default_content",
]

BEHAVIORS = ("allow", "ask", "deny")
WRITE_TOOLS = frozenset({
    "edit_file", "write_file", "multi_edit", "notebook_edit",
    "apply_patch", "run_command", "terminal", "bash",
})
_SOURCE_ORDER = ("managed", "project", "user", "local")


class RuleSyntaxError(ValueError):
    code = "PERMISSION_RULE_INVALID"


@dataclass(frozen=True)
class PermissionRule:
    behavior: str
    tool: str
    pattern: str = ""
    source: str = "user"

    def __post_init__(self) -> None:
        if self.behavior not in BEHAVIORS:
            raise RuleSyntaxError(f"unknown behavior {self.behavior!r}")
        if not self.tool:
            raise RuleSyntaxError("rule tool must not be empty")

    @classmethod
    def parse(cls, text: str, *, behavior: str, source: str = "user") -> "PermissionRule":
        """``Bash(git:*)`` → tool=Bash, pattern=``git:*``; ``Edit`` → tool only."""
        raw = str(text or "").strip()
        if not raw:
            raise RuleSyntaxError("empty permission rule")
        if "(" in raw:
            tool, _, rest = raw.partition("(")
            pattern = rest[:-1] if rest.endswith(")") else rest
            if not rest.endswith(")"):
                raise RuleSyntaxError(f"unbalanced rule {text!r}")
        else:
            tool, pattern = raw, ""
        return cls(behavior=behavior, tool=tool.strip(),
                   pattern=pattern.strip(), source=source)

    def matches(self, tool: str, content: str) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        pattern = self.pattern
        if pattern in ("", "*"):
            return True
        if pattern.endswith(":*"):
            prefix = pattern[:-2]
            return content == prefix or content.startswith(prefix + " ")
        # Keyed patterns: WebFetch(domain:*.github.com) matches the host part.
        key, sep, rest = pattern.partition(":")
        if sep and key in ("domain", "path", "prefix") and rest:
            pattern = rest
        return fnmatch.fnmatchcase(content, pattern)


@dataclass(frozen=True)
class Decision:
    behavior: str
    reason: str = ""
    rule: PermissionRule | None = None

    @property
    def allowed(self) -> bool:
        return self.behavior == "allow"


Approver = Callable[[str, Mapping[str, Any], str], bool]
ContentResolver = Callable[[str, Mapping[str, Any]], str]


def default_content(tool: str, args: Mapping[str, Any]) -> str:
    """Derive the rule-matching content for a call (port of per-tool matchers)."""
    lowered = tool.lower()
    if lowered in ("bash", "run_command", "terminal", "shell"):
        return str(args.get("command") or args.get("cmd") or "")
    if lowered in ("edit_file", "write_file", "multi_edit", "notebook_edit",
                   "read_file", "read_file_raw", "apply_patch"):
        return str(args.get("path") or args.get("file_path") or "")
    if lowered in ("web_fetch", "webfetch"):
        url = str(args.get("url") or "")
        return url.split("/")[2] if "://" in url else url
    return ""


class PermissionPolicy:
    def __init__(
        self,
        *,
        mode: str = "default",
        allow: Iterable[PermissionRule] = (),
        ask: Iterable[PermissionRule] = (),
        deny: Iterable[PermissionRule] = (),
        bypass_available: bool = True,
        approver: Approver | None = None,
        content_resolver: ContentResolver | None = None,
    ) -> None:
        if mode not in ("default", "acceptEdits", "plan", "bypassPermissions"):
            raise RuleSyntaxError(f"unknown permission mode {mode!r}")
        self.mode = mode
        self.bypass_available = bool(bypass_available)
        self.approver = approver
        self._content = content_resolver or default_content
        self._rules = {
            "allow": self._ordered(allow),
            "ask": self._ordered(ask),
            "deny": self._ordered(deny),
        }

    @staticmethod
    def _ordered(rules: Iterable[PermissionRule]) -> list[PermissionRule]:
        order = {source: index for index, source in enumerate(_SOURCE_ORDER)}
        return sorted(
            [rule for rule in rules],
            key=lambda rule: order.get(rule.source, len(order)),
        )

    def add(self, rule: PermissionRule) -> None:
        self._rules[rule.behavior].append(rule)

    def shed_allow_rules(self, limit: int) -> int:
        """Drop advisory allow rules under context pressure; returns shed count.

        Rules are managed-first, so keeping a prefix keeps managed policy and
        sheds the cheapest (user/local) grants last-first.
        """
        keep = max(0, int(limit))
        shed = len(self._rules["allow"]) - keep
        if shed > 0:
            self._rules["allow"] = self._rules["allow"][:keep]
            return shed
        return 0

    def evaluate(self, tool: str, args: Mapping[str, Any]) -> Decision:
        content = self._content(tool, dict(args))
        for behavior in ("deny", "ask", "allow"):
            for rule in self._rules[behavior]:
                if rule.matches(tool, content):
                    return Decision(rule.behavior, self._reason(rule), rule)
        return self._default(tool)

    def _default(self, tool: str) -> Decision:
        if self.mode == "bypassPermissions" and self.bypass_available:
            return Decision("allow", "bypassPermissions")
        if self.mode == "acceptEdits" and tool in WRITE_TOOLS:
            return Decision("allow", "acceptEdits auto-approves edits")
        if self.mode == "plan":
            return Decision("ask", "plan mode: operator approval required")
        return Decision("ask", "no matching rule")

    def resolve(self, tool: str, args: Mapping[str, Any]) -> Decision:
        """Evaluate, then resolve ``ask`` via the injected approver."""
        decision = self.evaluate(tool, args)
        if decision.behavior != "ask":
            return decision
        if self.approver is None:
            return Decision("deny", "approval required (no approver)",
                            decision.rule)
        approved = bool(self.approver(tool, dict(args), decision.reason))
        return Decision("allow" if approved else "deny",
                        decision.reason if approved else "operator denied",
                        decision.rule)

    @staticmethod
    def _reason(rule: PermissionRule) -> str:
        suffix = f"({rule.pattern})" if rule.pattern else ""
        return f"{rule.source} {rule.behavior} rule {rule.tool}{suffix}"

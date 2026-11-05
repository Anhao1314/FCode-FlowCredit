"""The rule engine: load the ruleset, keep drafts out, match static triggers.

Rules are data (audit-rules/*.json); the engine reads them and never
mutates them. Two properties are load-bearing:

- A rule with ``"status": "draft"`` does not participate in detection or
  scoring. Draft rules exist so authors can stage work without fixtures;
  the exclusion is enforced here, once, rather than remembered everywhere.
- A static trigger matches file contents inside the target copy and
  records evidence pointers (path + line). Static matching produces a
  *candidate*; only the constrained replay with a host-side oracle can
  promote it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Rule:
    id: str
    status: str
    category: str
    severity: str
    title: str
    root_cause_group: str
    trigger: dict
    poc: dict
    references: list = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass
class Candidate:
    rule: Rule
    evidence: list[dict]

    @property
    def rule_id(self) -> str:
        return self.rule.id


def load_ruleset(rules_dir: Path) -> dict:
    """Load and parse the versioned ruleset; drafts are kept but flagged."""
    index_path = Path(rules_dir) / "index.json"
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    rules: dict[str, Rule] = {}
    for item in raw.get("rules", []):
        rule = Rule(
            id=item["id"],
            status=item.get("status", "draft"),
            category=item["category"],
            severity=item["severity"],
            title=item["title"],
            root_cause_group=item.get("root_cause_group", item["id"]),
            trigger=item.get("trigger", {}),
            poc=item.get("poc", {}),
            references=item.get("references", []),
        )
        rules[rule.id] = rule
    return {"version": raw.get("version", 1), "rules": rules}


def active_rules(ruleset: dict) -> list[Rule]:
    return [rule for rule in ruleset["rules"].values() if rule.active]


def static_scan(root: Path, ruleset: dict) -> list[Candidate]:
    """Run every active static trigger over the target copy.

    Dynamic (probe) triggers are out of scope here; the scan simply skips
    them, so the same ruleset drives both static and dynamic phases.
    """
    candidates: list[Candidate] = []
    for rule in active_rules(ruleset):
        trigger = rule.trigger
        if trigger.get("type") != "static":
            continue
        patterns = [re.compile(p) for p in trigger.get("any_of", [])]
        if not patterns:
            continue
        evidence: list[dict] = []
        for path in sorted(Path(root).rglob(trigger.get("file_glob", "*"))):
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, start=1):
                if any(p.search(line) for p in patterns):
                    evidence.append({
                        "path": str(path.relative_to(root)),
                        "line": number,
                        "snippet": line.strip()[:200],
                    })
                    if len(evidence) >= 5:
                        break
            if len(evidence) >= 5:
                break
        if evidence:
            candidates.append(Candidate(rule=rule, evidence=evidence))
    return candidates



# ---------------------------------------------------------------------------
# REA facts: a second, data-only trigger type.
#
# REA is an evidence source, not a verdict. ``rea_scan`` reads a pinned
# facts document produced by ``flowaudit.targets.rea`` and turns a matching
# predicate into a *candidate*. The host oracle in ``poc.replay`` still
# decides whether the candidate is a finding; nothing in this module
# promotes anything.
# ---------------------------------------------------------------------------

_SELECTOR_PART = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve(value: Any, selector: str) -> Any:
    """Resolve a dotted selector against a facts document.

    Only object keys and non-negative list indices are supported. A missing
    path returns ``None`` so a rule can use ``absent`` deliberately.
    """
    current = value
    for part in [item for item in selector.split(".") if item]:
        if not _SELECTOR_PART.match(part):
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _apply_field(value: Any, field: str | None) -> Any:
    if not field:
        return value
    if isinstance(value, dict):
        return value.get(field)
    return None


def _predicate(value: Any, predicate: dict[str, Any]) -> bool:
    """Evaluate the deliberately small predicate vocabulary.

    Supported keys: ``equals``, ``matches``, ``exists``, ``absent``,
    ``count_gt``, ``count_lt``, ``any``, ``all``, ``not`` and ``field``.
    Rules are trusted data; patterns are capped by the loader's schema, not
    by this function. No expression language, no attribute access, no code.
    """
    if not isinstance(predicate, dict):
        return False
    if "not" in predicate:
        return not _predicate(value, predicate["not"])
    if "field" in predicate and len(predicate) > 1:
        inner = dict(predicate)
        inner.pop("field", None)
        return _predicate(_apply_field(value, str(predicate["field"])), inner)
    if "any" in predicate:
        items = value if isinstance(value, list) else []
        inner = predicate["any"]
        return any(_predicate(item, inner) for item in items)
    if "all" in predicate:
        items = value if isinstance(value, list) else []
        inner = predicate["all"]
        return bool(items) and all(_predicate(item, inner) for item in items)
    if "equals" in predicate:
        return value == predicate["equals"]
    if "matches" in predicate:
        pattern = predicate["matches"]
        return isinstance(value, str) and isinstance(pattern, str) and re.search(pattern, value) is not None
    if "exists" in predicate:
        return value is not None
    if "absent" in predicate:
        return value is None
    if "count_gt" in predicate:
        return isinstance(value, (list, dict, str)) and len(value) > int(predicate["count_gt"])
    if "count_lt" in predicate:
        return isinstance(value, (list, dict, str)) and len(value) < int(predicate["count_lt"])
    return False


def _rea_evidence(facts: dict, operation: str, selector: str,
                  value: Any) -> dict:
    operation_facts = (facts.get("operations") or {}).get(operation) or {}
    target = facts.get("target") or {}
    if isinstance(value, list):
        summary_value: Any = value[:5]
        count: int | None = len(value)
    elif isinstance(value, dict):
        summary_value = {key: value[key] for key in list(value)[:8]}
        count = len(value)
    else:
        summary_value = value if isinstance(value, (bool, int, str)) else str(value)
        count = None
    return {
        "kind": "rea-evidence" if operation != "host" else "host-fact",
        "operation": operation,
        "evidence_id": operation_facts.get("evidence_id") or "",
        "target_artifact_sha256": target.get("artifact_sha256") or "",
        "selector": selector,
        "summary": {
            "value": summary_value,
            "count": count,
            "operation_confidence": operation_facts.get("confidence"),
            "operation_authority": operation_facts.get("authority"),
        },
    }


def rea_scan(facts: dict[str, Any], ruleset: dict) -> list[Candidate]:
    """Match active ``trigger.type == "rea"`` rules against pinned facts."""
    candidates: list[Candidate] = []
    for rule in active_rules(ruleset):
        trigger = rule.trigger
        if trigger.get("type") != "rea":
            continue
        operation = str(trigger.get("operation") or "host")
        if operation == "host":
            root: Any = facts.get("host") or {}
        else:
            operation_facts = (facts.get("operations") or {}).get(operation) or {}
            root = operation_facts.get("facts") or {}
        selector = str(trigger.get("selector") or "")
        value = _resolve(root, selector)
        predicate = trigger.get("predicate")
        if not isinstance(predicate, dict) or not _predicate(value, predicate):
            continue
        candidates.append(Candidate(
            rule=rule,
            evidence=[_rea_evidence(facts, operation, selector, value)],
        ))
    return candidates

def ruleset_summary(ruleset: dict) -> dict[str, Any]:
    rules = ruleset["rules"]
    return {
        "version": ruleset["version"],
        "total": len(rules),
        "active": sum(1 for r in rules.values() if r.active),
        "draft": sorted(r.id for r in rules.values() if not r.active),
    }

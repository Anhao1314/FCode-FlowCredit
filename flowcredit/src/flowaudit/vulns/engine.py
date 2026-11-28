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


def static_scan(root: Path, ruleset: dict,
                coverage: list[dict[str, Any]] | None = None) -> list[Candidate]:
    """Run every active static trigger over the target copy.

    Dynamic (probe) triggers are out of scope here; the scan simply skips
    them, so the same ruleset drives both static and dynamic phases.

    When ``coverage`` is supplied, one record per active static rule is
    appended. The distinction that matters is ``executed`` (the glob matched
    files and they were read) versus ``not_applicable`` (no file matched):
    a rule that never saw an input must never be read as "clean".
    """
    candidates: list[Candidate] = []
    for rule in active_rules(ruleset):
        trigger = rule.trigger
        if trigger.get("type") != "static":
            continue
        record: dict[str, Any] = {
            "rule_id": rule.id,
            "category": rule.category,
            "root_cause_group": rule.root_cause_group,
            "trigger": "static",
            "file_glob": str(trigger.get("file_glob") or "*"),
            "status": "not_applicable",
            "files_seen": 0,
            "files_scanned": 0,
            "candidates": 0,
            "reason": "",
        }
        patterns = [re.compile(p) for p in trigger.get("any_of", [])]
        if not patterns:
            record.update(status="error", reason="static rule has no patterns")
            if coverage is not None:
                coverage.append(record)
            continue
        files = [path for path in sorted(Path(root).rglob(record["file_glob"]))
                 if path.is_file()]
        record["files_seen"] = len(files)
        evidence: list[dict] = []
        scanned = 0
        read_errors = 0
        truncated = False
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                read_errors += 1
                continue
            scanned += 1
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
                truncated = len(files) > scanned
                break
        record["files_scanned"] = scanned
        if not files:
            record["reason"] = f"no files matched {record['file_glob']!r}"
        elif scanned == 0:
            record.update(
                status="error",
                reason=f"all {read_errors} matched files were unreadable",
            )
        else:
            record["status"] = "executed"
            record["candidates"] = 1 if evidence else 0
            if truncated:
                record["reason"] = "evidence cap reached; remaining files not scanned"
        if coverage is not None:
            coverage.append(record)
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


def rea_scan(facts: dict[str, Any], ruleset: dict,
             coverage: list[dict[str, Any]] | None = None) -> list[Candidate]:
    """Match active ``trigger.type == "rea"`` rules against pinned facts.

    ``not_applicable`` means the selector's top-level surface is absent from
    the facts document (e.g. a repo audit with no ``web_scan``). An empty list
    under an existing surface is ``executed`` with zero candidates: the rule
    looked at real input and found nothing.
    """
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
        predicate = trigger.get("predicate")
        record: dict[str, Any] = {
            "rule_id": rule.id,
            "category": rule.category,
            "root_cause_group": rule.root_cause_group,
            "trigger": "rea",
            "operation": operation,
            "selector": selector,
            "status": "not_applicable",
            "candidates": 0,
            "reason": "",
        }
        if not selector:
            record.update(status="error", reason="rea rule has no selector")
        elif not isinstance(predicate, dict):
            record.update(status="error", reason="rea rule has no predicate")
        else:
            surface = selector.split(".", 1)[0]
            surface_present = isinstance(root, dict) and surface in root
            value = _resolve(root, selector)
            matched = surface_present and _predicate(value, predicate)
            record["status"] = "executed" if surface_present else "not_applicable"
            record["candidates"] = 1 if matched else 0
            if not surface_present:
                record["reason"] = f"selector surface {surface!r} absent from facts"
            if matched:
                candidates.append(Candidate(
                    rule=rule,
                    evidence=[_rea_evidence(facts, operation, selector, value)],
                ))
        if coverage is not None:
            coverage.append(record)
    return candidates

def ruleset_summary(ruleset: dict) -> dict[str, Any]:
    rules = ruleset["rules"]
    return {
        "version": ruleset["version"],
        "total": len(rules),
        "active": sum(1 for r in rules.values() if r.active),
        "draft": sorted(r.id for r in rules.values() if not r.active),
    }

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


def ruleset_summary(ruleset: dict) -> dict[str, Any]:
    rules = ruleset["rules"]
    return {
        "version": ruleset["version"],
        "total": len(rules),
        "active": sum(1 for r in rules.values() if r.active),
        "draft": sorted(r.id for r in rules.values() if not r.active),
    }

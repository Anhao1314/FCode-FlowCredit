"""Re-read a pinned REA facts snapshot inside the constrained worker.

This is the static-artifact analogue of ``static_file_evidence``: the
"stimulus" is the already-collected Evidence projection, and the worker
proves the selected facts are still exactly the bytes the adapter pinned.
The worker never invokes REA, never touches the shipped target and never
opens a socket; it reads one JSON file from the snapshot.

The host oracle, not this scenario, decides whether the selected fact is
a finding. For a probe run the adapter writes ``facts.json``; for the
negative control it writes ``control-facts.json`` with the condition
neutralised. Both files are part of the audit workdir, not the target.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_SELECTOR_PART = __import__("re").compile(r"^[A-Za-z0-9_-]+$")


def _resolve(value: Any, selector: str) -> Any:
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


def run(target_repo: Path, params: dict) -> dict:
    facts_name = str(params.get("facts_path") or "facts.json")
    facts_path = Path(target_repo) / facts_name
    if not facts_path.is_file():
        raise RuntimeError(f"pinned REA facts not found: {facts_path}")
    raw = facts_path.read_bytes()
    try:
        facts = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(f"pinned REA facts are not valid JSON: {error}") from error
    if not isinstance(facts, dict):
        raise RuntimeError("pinned REA facts must be a JSON object")
    operation = str(params.get("operation") or "host")
    selector = str(params.get("selector") or "")
    if operation == "host":
        root = facts.get("host") or {}
    else:
        operation_facts = (facts.get("operations") or {}).get(operation) or {}
        root = operation_facts.get("facts") or {}
    selected = _resolve(root, selector)
    return {
        "scenario": "rea_fact_evidence",
        "mode": str(params.get("mode") or "probe"),
        "facts_path": facts_name,
        "facts_sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": facts.get("schema_version"),
        "operation": operation,
        "selector": selector,
        "selected": selected,
        "scope": root if operation == "host" else None,
        "target": facts.get("target"),
        "uncovered": facts.get("uncovered"),
    }
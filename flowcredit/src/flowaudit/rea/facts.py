"""Turn REA Evidence into small, allow-listed audit facts.

The full Evidence record is kept beside the audit as evidence. What flows
into rules, the Analyst context and the report is this projection: the
fields an audit needs, bounded in depth and length, with floats normalised
because the kernel's canonical JSON deliberately rejects them.

The projection is an allow-list, not a filter. A new REA field does not
appear here until someone decides the audit needs it. That is the same
discipline as ``mcp.json``: the host classifies, the server does not.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from flowagent.core.hashing import digest

FACTS_SCHEMA_VERSION = "fcode.rea-facts/v1"

_MAX_LIST = 500
_MAX_TEXT = 800
_MAX_DEPTH = 5
_SIDECAR_MARKERS = ("bridge", "sidecar", "runtime")
_PRERELEASE = re.compile(r"(?:^|[-.])(?:rc|beta|alpha|preview|pre)(?:[.-]?\d*)?$", re.IGNORECASE)


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Normalise a JSON value into the kernel's canonical-JSON subset.

    Floats are converted to integers when integral, otherwise to their
    shortest string form. A fact is evidence, not a computation: preserving
    an exact textual value is more honest than pretending a binary float is
    reproducible across runtimes.
    """
    if depth > _MAX_DEPTH:
        return "<depth-limit>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else repr(value)
    if isinstance(value, dict):
        return {
            _text(key, 200): _json_safe(item, depth + 1)
            for key, item in list(value.items())[:_MAX_LIST]
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:_MAX_LIST]]
    return _text(value)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _provider(evidence: dict[str, Any]) -> dict[str, Any]:
    provider = _dict(evidence.get("provider"))
    return {
        "id": _text(provider.get("id"), 120),
        "name": _text(provider.get("name"), 200),
        "version": _text(provider.get("version"), 120) or None,
    }


def _limitations(evidence: dict[str, Any], limit: int = 50) -> list[str]:
    return [_text(item, 600) for item in _list(evidence.get("limitations"))[:limit]]


def _artifact(evidence: dict[str, Any]) -> dict[str, Any]:
    nr = _dict(evidence.get("normalized_result"))
    artifact = _dict(nr.get("artifact"))
    subject = _dict(evidence.get("subject"))
    subject_digest = _dict(subject.get("digest"))
    return {
        "sha256": _text(artifact.get("sha256") or subject_digest.get("sha256"), 64),
        "bytes": artifact.get("bytes") if isinstance(artifact.get("bytes"), int) else None,
        "format": _text(artifact.get("format") or nr.get("format"), 40) or None,
        "architecture": _text(artifact.get("architecture") or nr.get("architecture"), 40) or None,
        "path": _text(artifact.get("path") or subject.get("local_path"), 1000) or None,
    }


def extract_go_binary(evidence: dict[str, Any]) -> dict[str, Any]:
    """Project ``inspect_go_binary`` Evidence: build metadata and SBOM."""
    nr = _dict(evidence.get("normalized_result"))
    build_info = _dict(nr.get("build_info"))
    module = _dict(build_info.get("module"))
    dependencies: list[dict[str, Any]] = []
    for item in _list(module.get("dependencies"))[:_MAX_LIST]:
        dep = _dict(item)
        version = _text(dep.get("version"), 200)
        if not _text(dep.get("path"), 500):
            continue
        dependencies.append({
            "path": _text(dep.get("path"), 500),
            "version": version,
            "replacement": _text(dep.get("replacement"), 500) or None,
            "prerelease": bool(_PRERELEASE.search(version)),
        })
    settings: dict[str, str] = {}
    for item in _list(module.get("settings"))[:_MAX_LIST]:
        entry = _dict(item)
        key = _text(entry.get("key"), 120)
        if key:
            settings[key] = _text(entry.get("value"), 500)
    main = _dict(module.get("main"))
    return {
        "artifact": _artifact(evidence),
        "go_version": _text(build_info.get("go_version"), 80) or None,
        "encoding": _text(build_info.get("encoding"), 40) or None,
        "module": {
            "path": _text(module.get("path") or main.get("path"), 500) or None,
            "version": _text(main.get("version"), 120) or None,
            "complete": module.get("complete") if isinstance(module.get("complete"), bool) else None,
        },
        "dependencies": dependencies,
        "settings": settings,
        "build_tags": settings.get("-tags", ""),
        "limitations": _limitations(evidence),
    }


def _selected(path: str) -> bool:
    if path == "Contents/Info.plist":
        return True
    if path.startswith("Contents/MacOS/"):
        return True
    if path.startswith("Contents/Library/"):
        return True
    lowered = path.lower()
    if "/node_modules/" in lowered or "/runtime/npm/" in lowered:
        return False
    if any(marker in lowered for marker in _SIDECAR_MARKERS):
        return lowered.endswith((".json", ".plist", ".ts", ".js", ".mjs", ".cjs", ".md"))
    return False


def extract_desktop_application(evidence: dict[str, Any]) -> dict[str, Any]:
    """Project ``inspect_desktop_application`` Evidence: bundle anatomy."""
    nr = _dict(evidence.get("normalized_result"))
    files = _list(nr.get("files"))
    selected: list[dict[str, Any]] = []
    sidecars: dict[str, dict[str, Any]] = {}
    for raw in files:
        entry = _dict(raw)
        path = _text(entry.get("path"), 1000)
        if not path:
            continue
        if _selected(path):
            facts = []
            for fact in _list(entry.get("facts"))[:5]:
                item = _dict(fact)
                facts.append({
                    "kind": _text(item.get("kind"), 80),
                    "offset": item.get("offset") if isinstance(item.get("offset"), int) else None,
                    "value": _json_safe(item.get("value")),
                })
            selected.append({
                "file_id": _text(entry.get("file_id"), 120),
                "path": path,
                "kind": _text(entry.get("kind"), 40),
                "bytes": entry.get("bytes") if isinstance(entry.get("bytes"), int) else None,
                "read_status": _text(entry.get("read_status"), 40),
                "facts": facts,
                "issues": _json_safe(_list(entry.get("issues"))[:3]),
            })
            if len(selected) >= 80:
                break
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "Contents" and parts[1] == "Resources":
            top = parts[2]
            if any(marker in top.lower() for marker in _SIDECAR_MARKERS) and top not in sidecars:
                sidecars[top] = {
                    "path": f"Contents/Resources/{top}",
                    "kind": "directory",
                    "marker": next(
                        marker for marker in _SIDECAR_MARKERS if marker in top.lower()
                    ),
                }
    coverage = _dict(nr.get("coverage"))
    raw_root = _dict(nr.get("root"))
    # Device/inode identity is host-local; keep content and path facts only.
    root = {
        "path": _text(raw_root.get("path"), 1000) or None,
        "canonical_path": _text(raw_root.get("canonical_path"), 1000) or None,
        "snapshot_sha256": _text(raw_root.get("snapshot_sha256"), 64) or None,
        "identity_scope": _text(raw_root.get("identity_scope"), 120) or None,
    }
    return {
        "root": root,
        "entry_count": len(files),
        "coverage": {
            "status": _text(coverage.get("status"), 80),
            "entries_examined": coverage.get("entries_examined")
            if isinstance(coverage.get("entries_examined"), int) else None,
            "files_probed": coverage.get("files_probed")
            if isinstance(coverage.get("files_probed"), int) else None,
            "bytes_read": coverage.get("bytes_read")
            if isinstance(coverage.get("bytes_read"), int) else None,
            "symlinks_not_followed": coverage.get("symlinks_not_followed")
            if isinstance(coverage.get("symlinks_not_followed"), int) else None,
        },
        "candidates": _json_safe(_list(nr.get("candidates"))[:20]),
        "components": _json_safe(_list(nr.get("components"))[:20]),
        "next_probes": _json_safe(_list(nr.get("next_probes"))[:20]),
        "selected_files": selected,
        "sidecars": sorted(sidecars.values(), key=lambda item: item["path"]),
        "limitations": _limitations(evidence),
    }


def extract_javascript_application(evidence: dict[str, Any]) -> dict[str, Any]:
    """Project ``analyze_javascript_application`` Evidence without the graph."""
    nr = _dict(evidence.get("normalized_result"))
    graph = _dict(nr.get("graph"))
    semantic = _dict(nr.get("semantic_graph"))
    route = _dict(nr.get("route_inventory"))
    stats = _dict(nr.get("statistics"))
    route_stats = _dict(route.get("statistics"))
    return {
        "input_path": _text(nr.get("input_path"), 1000) or None,
        "format": _text(nr.get("format"), 40) or None,
        "root_artifact_sha256": _text(nr.get("root_artifact_sha256"), 64) or None,
        "statistics": {
            key: stats.get(key)
            for key in (
                "relevant_files", "parsed_javascript_files", "skipped_javascript_files",
                "visited_ast_nodes", "findings", "modules", "parse_failures",
                "text_bytes_read", "invalid_utf8_files", "nested_asar_containers",
            )
            if isinstance(stats.get(key), int)
        },
        "graph": {
            "nodes": len(_list(graph.get("nodes"))),
            "edges": len(_list(graph.get("edges"))),
            "coverage": _json_safe(_dict(graph.get("coverage"))),
            "limitations": _limitations({"limitations": _list(graph.get("limitations"))}, 20),
        },
        "semantic_graph": {
            "nodes": len(_list(semantic.get("nodes"))),
            "relations": len(_list(semantic.get("relations"))),
        },
        "route_inventory": {
            "routes": len(_list(route.get("routes"))),
            "occurrences": len(_list(route.get("occurrences"))),
            "statistics": _json_safe(route_stats),
        },
        "limitations": _limitations(evidence, 30),
    }


def _operation_facts(operation: str, evidence: dict[str, Any]) -> dict[str, Any]:
    if operation == "inspect_go_binary":
        return extract_go_binary(evidence)
    if operation == "inspect_desktop_application":
        return extract_desktop_application(evidence)
    if operation == "analyze_javascript_application":
        return extract_javascript_application(evidence)
    raise ValueError(f"unsupported REA operation: {operation}")


def build_snapshot_facts(
    *,
    target: dict[str, Any],
    evidence: Iterable[Any],
    host: dict[str, Any] | None = None,
    uncovered: Iterable[dict[str, str]] = (),
) -> dict[str, Any]:
    """Build the pinned facts document a replay scenario reads.

    ``evidence`` items are :class:`flowaudit.rea.client.ReaEvidence` values
    or plain dicts with the same shape. The raw Evidence is written beside
    this document by the target adapter; this function only projects.
    """
    operations: dict[str, Any] = {}
    for item in evidence:
        evidence_dict = item.evidence if hasattr(item, "evidence") else item
        operation = (
            item.operation if hasattr(item, "operation")
            else _text(evidence_dict.get("operation"), 80)
        )
        if not operation:
            continue
        operations[operation] = {
            "evidence_id": _text(evidence_dict.get("evidence_id"), 80),
            "operation": operation,
            "provider": _provider(evidence_dict),
            "confidence": _text(evidence_dict.get("confidence"), 40) or None,
            "authority": _text(evidence_dict.get("authority"), 60) or None,
            "facts": _operation_facts(operation, evidence_dict),
            "limitations": _limitations(evidence_dict),
        }
    host_facts = _json_safe(host or {})
    facts = {
        "schema_version": FACTS_SCHEMA_VERSION,
        "target": _json_safe(target),
        "operations": operations,
        "host": host_facts,
        "uncovered": [
            {
                "surface": _text(_dict(item).get("surface"), 200),
                "reason": _text(_dict(item).get("reason"), 600),
            }
            for item in uncovered
            if _text(_dict(item).get("surface"), 200)
        ],
    }
    return facts


def facts_digest(facts: dict[str, Any]) -> str:
    """Content-address the projected facts document."""
    return digest(_json_safe(facts))
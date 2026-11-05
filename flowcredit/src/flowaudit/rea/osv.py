"""Offline OSV matching for Go module dependencies.

The audit host decides which vulnerability feed to trust. This module does
not fetch anything: it reads an operator-supplied OSV snapshot and matches
Go package names/versions. A match is a *candidate* fact; the existing
AGI-05 host oracle still decides whether it is a finding.

Version handling is deliberately small: Go module versions are SemVer-like
``vMAJOR.MINOR.PATCH[-pre]``. Versions that do not parse still match an
explicit ``affected[].versions`` entry; they simply do not participate in
range comparison. Unknown is never treated as safe.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

__all__ = ["load_snapshot", "match_go_dependencies", "compare_versions"]

_VERSION = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
_MAX_RECORDS = 5000
_MAX_MATCHES = 100


def _parse(version: str) -> tuple[int, int, int, tuple[str, ...]] | None:
    match = _VERSION.match(str(version).strip())
    if match is None:
        return None
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease)


def compare_versions(left: str, right: str) -> int | None:
    """Return -1/0/1 for two Go versions, or None if either cannot parse."""
    a = _parse(left)
    b = _parse(right)
    if a is None or b is None:
        return None
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    # A release sorts after its pre-release; an empty pre-release is latest.
    if a[3] == b[3]:
        return 0
    if not a[3]:
        return 1
    if not b[3]:
        return -1
    return -1 if a[3] < b[3] else 1


def load_snapshot(path: str | Path) -> list[dict[str, Any]]:
    """Load an OSV snapshot (a list or an object with ``vulns``)."""
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read OSV snapshot {target}: {error}") from error
    if isinstance(data, dict):
        data = data.get("vulns")
    if not isinstance(data, list):
        raise ValueError("OSV snapshot must be a list or an object with a vulns array")
    return [item for item in data[:_MAX_RECORDS] if isinstance(item, dict)]


def _affected_package(affected: dict[str, Any], package: str) -> bool:
    pkg = affected.get("package")
    if not isinstance(pkg, dict):
        return False
    return (
        pkg.get("ecosystem") in ("Go", "Go Module")
        and str(pkg.get("name") or "") == package
    )


def _in_range(version: str, affected: dict[str, Any]) -> bool:
    if version in [str(item) for item in affected.get("versions") or []]:
        return True
    for entry in affected.get("ranges") or []:
        if not isinstance(entry, dict) or entry.get("type") != "SEMVER":
            continue
        introduced: str | None = None
        for event in entry.get("events") or []:
            if not isinstance(event, dict):
                continue
            if "introduced" in event:
                introduced = str(event["introduced"])
                if introduced in ("0", "0.0.0"):
                    introduced = "v0.0.0"
            elif "fixed" in event and introduced is not None:
                lower = compare_versions(version, introduced)
                upper = compare_versions(version, str(event["fixed"]))
                if lower is not None and upper is not None and lower >= 0 and upper < 0:
                    return True
                introduced = None
            elif "last_affected" in event and introduced is not None:
                lower = compare_versions(version, introduced)
                upper = compare_versions(version, str(event["last_affected"]))
                if lower is not None and upper is not None and lower >= 0 and upper <= 0:
                    return True
                introduced = None
    return False


def match_go_dependencies(
    dependencies: Iterable[dict[str, Any]],
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return deduplicated OSV matches for the supplied dependency rows."""
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for dep in dependencies:
        if not isinstance(dep, dict):
            continue
        package = str(dep.get("path") or "")
        version = str(dep.get("version") or "")
        if not package or not version:
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("id") or "")
            if not record_id:
                continue
            for affected in record.get("affected") or []:
                if not isinstance(affected, dict) or not _affected_package(affected, package):
                    continue
                if not _in_range(version, affected):
                    continue
                key = (record_id, package, version)
                if key in seen:
                    continue
                seen.add(key)
                fixed = []
                for entry in affected.get("ranges") or []:
                    for event in entry.get("events") or []:
                        if isinstance(event, dict) and "fixed" in event:
                            fixed.append(str(event["fixed"]))
                matches.append({
                    "osv_id": record_id,
                    "dependency": package,
                    "version": version,
                    "aliases": [str(item) for item in (record.get("aliases") or [])][:10],
                    "summary": str(record.get("summary") or "")[:500],
                    "fixed_versions": sorted(set(fixed)),
                })
                if len(matches) >= _MAX_MATCHES:
                    return matches
    return matches
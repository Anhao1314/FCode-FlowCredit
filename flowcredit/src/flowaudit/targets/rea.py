"""Artifact target adapter: pin a shipped app/binary/JS tree with REA.

The source-tree adapter copies the target and scans text. A shipped
artifact cannot be treated that way: there is no source tree to copy, the
binary is large, and static rules cannot read it. This adapter asks REA
for a bounded set of facts, writes a **facts snapshot** into the audit
workdir, and keeps the raw Evidence beside the audit for third-party
re-check. Replay scenarios read the snapshot; they never execute the
target and never invoke REA from inside the constrained worker.

Identity is explicit:

* ``artifact_sha256`` pins the executable/JS root bytes;
* ``facts_sha256`` pins the projected facts;
* each Evidence file's SHA-256 pins the raw record;
* ``manifest_sha256`` binds all of the above plus the REA version.

A conclusion cannot silently move to a different artifact or a different
evidence projection.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from flowagent.core.contracts import AgentError
from flowagent.core.hashing import digest, digest_bytes

from ..rea import (
    FACTS_SCHEMA_VERSION,
    analyze_library_validation,
    build_snapshot_facts,
    load_snapshot,
    match_go_dependencies,
    facts_digest,
    run_evidence,
    scan_runtime_installers,
    scan_sidecars,
    scan_code_quality,
    version as rea_version,
)

__all__ = ["ReaTargetSnapshot", "detect_kind", "prepare"]

_APP = "desktop-app"
_BINARY = "binary"
_JAVASCRIPT = "javascript"

_MACHO_MAGIC = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
}
_ELF_MAGIC = b"\x7fELF"
_PE_MAGIC = b"MZ"

_DECLARED_ALIASES = {
    "microphone": "com.apple.security.device.audio-input",
    "audio-input": "com.apple.security.device.audio-input",
    "calendar": "com.apple.security.personal-information.calendars",
    "calendars": "com.apple.security.personal-information.calendars",
    "apple-events": "com.apple.security.automation.apple-events",
    "automation": "com.apple.security.automation.apple-events",
    "library-validation": "com.apple.security.cs.disable-library-validation",
    "disable-library-validation": "com.apple.security.cs.disable-library-validation",
}


@dataclass(frozen=True)
class ReaTargetSnapshot:
    """The pinned artifact audit substrate and its evidence references."""

    root: Path
    snapshot_root: Path
    kind: str
    version: str
    artifact_sha256: str
    #: Stable target identity: artifact bytes + version + format/arch.
    manifest_sha256: str
    #: Analysis basis: target manifest + facts + Evidence + REA version.
    analysis_manifest_sha256: str
    facts_sha256: str
    facts: dict[str, Any]
    facts_path: Path
    control_facts_path: Path
    evidence_files: tuple[dict[str, Any], ...]
    rea_version: str
    uncovered: tuple[dict[str, str], ...]

    def target_ref(self, audit_id: str) -> dict[str, Any]:
        return {
            "audit_id": audit_id,
            "identity": str(self.root),
            "version": self.version,
            "manifest_sha256": self.manifest_sha256,
            "profile": "artifact",
            "artifact_sha256": self.artifact_sha256,
            "target_copy": False,
            "target_snapshot_sha256": self.facts_sha256,
        }


def _first_bytes(path: Path, count: int = 4) -> bytes:
    with path.open("rb") as handle:
        return handle.read(count)


def detect_kind(target: Path) -> str:
    """Classify an audit target without executing it."""
    root = Path(target)
    if root.is_dir() and (root / "Contents" / "Info.plist").is_file():
        return _APP
    if root.is_dir() and (root / "package.json").is_file():
        return _JAVASCRIPT
    if root.is_file():
        if root.suffix.lower() == ".asar":
            return _JAVASCRIPT
        magic = _first_bytes(root)
        if magic in _MACHO_MAGIC or magic == _ELF_MAGIC or magic[:2] == _PE_MAGIC:
            return _BINARY
        raise AgentError(
            "REA_TARGET_UNSUPPORTED",
            f"not a recognized shipped artifact: {root}",
            details={"magic": magic.hex()},
        )
    # A JS tree without package.json (e.g. extracted bundle) is still a tree.
    if root.is_dir() and any(root.glob("**/*.js")):
        return _JAVASCRIPT
    raise AgentError(
        "REA_TARGET_UNSUPPORTED",
        f"cannot classify target: {root}",
    )


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _app_info(app: Path) -> dict[str, Any]:
    info_path = app / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as error:
        raise AgentError(
            "REA_TARGET_INVALID",
            f"cannot read Info.plist: {error}",
            details={"path": str(info_path)},
        ) from error
    if not isinstance(info, dict):
        raise AgentError("REA_TARGET_INVALID", "Info.plist is not a dictionary")
    return info


def _app_executable(app: Path, info: dict[str, Any]) -> Path | None:
    name = info.get("CFBundleExecutable")
    if isinstance(name, str) and name:
        candidate = app / "Contents" / "MacOS" / name
        if candidate.is_file():
            return candidate
    macos = app / "Contents" / "MacOS"
    if macos.is_dir():
        files = [item for item in macos.iterdir() if item.is_file()]
        if len(files) == 1:
            return files[0]
    return None


def _codesign_entitlements(app: Path) -> tuple[list[str], dict[str, Any]]:
    """Read entitlements through the host trust tool, never through REA.

    Entitlements are an OS signature fact. REA can locate the bundle, but
    the host's ``codesign`` is the authority for what the OS will grant.
    """
    try:
        completed = subprocess.run(
            ["codesign", "-d", "--entitlements", ":-", str(app)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        return [], {"status": "unavailable", "reason": str(error)}
    payload = (completed.stdout or "").strip()
    if completed.returncode != 0 or not payload:
        detail = (completed.stderr or "").strip().splitlines()
        return [], {
            "status": "unavailable",
            "reason": " | ".join(detail[-3:]) or f"codesign exit {completed.returncode}",
        }
    try:
        values = plistlib.loads(payload.encode("utf-8"))
    except plistlib.InvalidFileException as error:
        return [], {"status": "invalid", "reason": str(error)}
    if not isinstance(values, dict):
        return [], {"status": "invalid", "reason": "entitlements is not a dictionary"}
    keys = sorted(str(key) for key, enabled in values.items() if enabled is True)
    return keys, {"status": "observed", "tool": "codesign"}


def _signature_status(app: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        return {"status": "unavailable", "reason": str(error)}
    if completed.returncode == 0:
        return {"status": "valid", "tool": "codesign"}
    detail = (completed.stderr or "").strip().splitlines()
    return {
        "status": "invalid",
        "reason": " | ".join(detail[-3:]) or f"codesign exit {completed.returncode}",
    }


def _read_declared_features(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    items: list[str] = []
    in_section = False
    for line in text:
        stripped = line.strip()
        if stripped.lower().startswith("## "):
            in_section = "declared feature" in stripped.lower()
            continue
        if in_section and stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _declared_features(target: Path, explicit: Iterable[str] | None,
                       descriptor: Path | None = None) -> list[str]:
    """Read the operator's declared feature/entitlement list.

    Explicit input wins, then an explicit descriptor (for installed apps
    whose bundle cannot hold ``target.md``), then ``target/target.md``.
    Aliases such as ``microphone`` map to the entitlement key the OS uses.
    """
    items = list(explicit or [])
    if not items:
        paths = [descriptor] if descriptor else [target / "target.md", target / "TARGET.md"]
        for path in paths:
            if path is None or not path.is_file():
                continue
            items.extend(_read_declared_features(path))
    out: list[str] = []
    for item in items:
        value = item.strip()
        if not value:
            continue
        out.append(_DECLARED_ALIASES.get(value.lower(), value))
    return sorted(set(out))


def _host_facts(target: Path, kind: str,
                descriptor: Path | None = None) -> tuple[dict[str, Any], list[dict[str, str]]]:
    uncovered: list[dict[str, str]] = []
    if kind != _APP:
        return {
            "entitlements": [],
            "declared_features": _declared_features(target, None, descriptor),
        }, uncovered
    entitlements, ent_status = _codesign_entitlements(target)
    signature = _signature_status(target)
    if ent_status.get("status") != "observed":
        uncovered.append({
            "surface": "macOS entitlements",
            "reason": f"codesign did not yield entitlements: {ent_status.get('reason', 'unknown')}",
        })
    if signature.get("status") != "valid":
        uncovered.append({
            "surface": "application signature",
            "reason": f"codesign verification: {signature.get('status')} "
                      f"{signature.get('reason', '')}".strip(),
        })
    return {
        "entitlements": entitlements,
        "declared_features": _declared_features(target, None, descriptor),
        "signature": signature,
        "entitlements_source": ent_status,
    }, uncovered


def _neutralise(facts: dict[str, Any]) -> dict[str, Any]:
    """Project a clean control from the same facts.

    The control is a harness control, not a second target: it removes the
    condition under test (pre-release dependencies; entitlements outside
    the declared set) so a host oracle that always fires fails loudly.
    Reports must keep this distinction explicit.
    """
    control = json.loads(json.dumps(facts, ensure_ascii=False))
    go = (control.get("operations") or {}).get("inspect_go_binary") or {}
    go_facts = go.get("facts") or {}
    deps = go_facts.get("dependencies")
    if isinstance(deps, list):
        go_facts["dependencies"] = [
            item for item in deps
            if isinstance(item, dict) and not item.get("prerelease")
        ]
    host = control.setdefault("host", {})
    entitlements = host.get("entitlements")
    if isinstance(entitlements, list):
        host["declared_features"] = sorted(set(entitlements))
    host["vulnerabilities"] = []
    host["runtime_installers"] = []
    # VIBE 事实：负对照把每类质量事实清零（god file / 常量分歧 / AI 残留…）
    vibe = host.get("vibe_scan")
    if isinstance(vibe, dict):
        for key in ("god_files", "todo_hotspots", "literal_sprawl",
                    "constant_conflicts", "duplicate_functions",
                    "placeholder_stubs", "ai_residue"):
            vibe[key] = []
        for issue in vibe.get("issue_files") or []:
            if isinstance(issue, dict) and isinstance(issue.get("issues"), dict):
                for sub in ("god_functions", "silent_catches"):
                    issue["issues"][sub] = []
    for sidecar in host.get("sidecars") or []:
        if not isinstance(sidecar, dict):
            continue
        sidecar["listener_without_auth"] = []
        sidecar["secret_like"] = []
        sidecar["npm_install"] = []
        if sidecar.get("listeners") and not sidecar.get("auth_markers"):
            sidecar["auth_markers"] = [{"file": "control", "line": 0, "marker": "control"}]
    return control


def _write_json(path: Path, value: Any) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(payload)
    return payload


def prepare(
    target: str | Path,
    workdir: str | Path,
    *,
    declared_features: Iterable[str] | None = None,
    target_md: str | Path | None = None,
    osv_snapshot: str | Path | None = None,
    binary: str | None = None,
    timeout: int = 300,
    evidence_dir: str | Path | None = None,
) -> ReaTargetSnapshot:
    """Pin an artifact target, collect REA facts and write the snapshot."""
    root = Path(target).expanduser().resolve()
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    snapshot = work / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    evidence_dir_path = Path(evidence_dir) if evidence_dir else snapshot / "evidence"
    evidence_dir_path.mkdir(parents=True, exist_ok=True)

    kind = detect_kind(root)
    descriptor = None
    if target_md is not None:
        descriptor = Path(target_md).expanduser().resolve()
        if not descriptor.is_file():
            raise AgentError(
                "REA_TARGET_INVALID",
                f"target descriptor not found: {descriptor}",
                details={"path": str(descriptor)},
            )
    uncovered: list[dict[str, str]] = []
    host, host_uncovered = _host_facts(root, kind, descriptor)
    uncovered.extend(host_uncovered)
    if kind == _APP:
        host["sidecars"] = scan_sidecars(root)
        host["vibe_scan"] = scan_code_quality(root)
    if kind == _APP and declared_features is not None:
        host["declared_features"] = sorted(set(
            _DECLARED_ALIASES.get(item.strip().lower(), item.strip())
            for item in declared_features if item.strip()
        ))

    evidence_items = []
    artifact_sha256 = ""
    version = ""
    fmt = ""
    arch = ""

    if kind == _APP:
        info = _app_info(root)
        version = str(info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or "")
        executable = _app_executable(root, info)
        if executable is None:
            raise AgentError(
                "REA_TARGET_INVALID",
                "app bundle has no main executable under Contents/MacOS",
                details={"path": str(root)},
            )
        artifact_sha256 = _sha256_file(executable)
        host["runtime_installers"] = scan_runtime_installers(executable)
        host["library_validation"] = analyze_library_validation(
            root, executable, host.get("entitlements", []),
        )
        if host["library_validation"].get("status") != "observed":
            uncovered.append({
                "surface": "Mach-O library validation",
                "reason": str(host["library_validation"].get("reason", "unavailable")),
            })
        desktop = run_evidence(
            "inspect_desktop_application", root,
            binary=binary, timeout=timeout,
        )
        evidence_items.append(desktop)
        fmt = "mach-o"
        arch = ""
        try:
            go = run_evidence(
                "inspect_go_binary", executable,
                binary=binary, timeout=timeout,
            )
            evidence_items.append(go)
            go_facts = go.evidence.get("normalized_result") or {}
            fmt = str(go_facts.get("format") or fmt)
            arch = str(go_facts.get("architecture") or arch)
        except AgentError as error:
            uncovered.append({
                "surface": "Go build metadata",
                "reason": f"{error.code}: {error.message}",
            })
        if host.get("sidecars"):
            uncovered.append({
                "surface": "sidecar JavaScript application graph",
                "reason": "source pattern scan only; full REA JS graph not collected",
            })
    elif kind == _BINARY:
        artifact_sha256 = _sha256_file(root)
        host["runtime_installers"] = scan_runtime_installers(root)
        evidence = run_evidence(
            "inspect_go_binary", root,
            binary=binary, timeout=timeout,
        )
        evidence_items.append(evidence)
        nr = evidence.evidence.get("normalized_result") or {}
        fmt = str(nr.get("format") or "")
        arch = str(nr.get("architecture") or "")
    else:
        evidence = run_evidence(
            "analyze_javascript_application", root,
            binary=binary, timeout=timeout,
        )
        evidence_items.append(evidence)
        nr = evidence.evidence.get("normalized_result") or {}
        artifact_sha256 = str(nr.get("root_artifact_sha256") or "")
        fmt = str(nr.get("format") or "directory")

    go_facts: dict[str, Any] = {}
    for item in evidence_items:
        if item.operation == "inspect_go_binary":
            from ..rea.facts import extract_go_binary
            go_facts = extract_go_binary(item.evidence)
            break
    if osv_snapshot:
        snapshot_path = Path(osv_snapshot).expanduser().resolve()
        if not snapshot_path.is_file():
            raise AgentError(
                "REA_OSV_INVALID",
                f"OSV snapshot not found: {snapshot_path}",
                details={"path": str(snapshot_path)},
            )
        try:
            records = load_snapshot(snapshot_path)
        except ValueError as error:
            raise AgentError("REA_OSV_INVALID", str(error)) from error
        host["vulnerabilities"] = match_go_dependencies(
            go_facts.get("dependencies", []), records,
        )
        if not host["vulnerabilities"]:
            uncovered.append({
                "surface": "dependency vulnerability intelligence",
                "reason": "OSV snapshot supplied, but no Go dependency matched",
            })
    else:
        host["vulnerabilities"] = []
        uncovered.append({
            "surface": "dependency vulnerability intelligence",
            "reason": "no OSV snapshot supplied; only pre-release versions were checked",
        })

    facts = build_snapshot_facts(
        target={
            "identity": str(root),
            "version": version,
            "artifact_sha256": artifact_sha256,
            "format": fmt or None,
            "architecture": arch or None,
        },
        evidence=evidence_items,
        host=host,
        uncovered=uncovered,
    )
    facts_sha256 = facts_digest(facts)
    facts_path = snapshot / "facts.json"
    facts_bytes = _write_json(facts_path, facts)
    control_path = snapshot / "control-facts.json"
    control_bytes = _write_json(control_path, _neutralise(facts))
    persistent_facts_path = evidence_dir_path / "facts.json"
    persistent_control_path = evidence_dir_path / "control-facts.json"
    persistent_facts_path.write_bytes(facts_bytes)
    persistent_control_path.write_bytes(control_bytes)

    evidence_files: list[dict[str, Any]] = []
    for item in evidence_items:
        filename = f"{item.operation}.json"
        path = evidence_dir_path / filename
        raw = json.dumps(item.evidence, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        evidence_files.append({
            "evidence_id": item.evidence_id,
            "operation": item.operation,
            "sha256": digest_bytes(raw),
            "filename": filename,
            "path": str(path),
        })

    target_manifest = digest({
        "artifact_sha256": artifact_sha256,
        "version": version,
        "format": fmt,
        "architecture": arch,
    })
    analysis_manifest = digest({
        "target_manifest_sha256": target_manifest,
        "facts_sha256": facts_sha256,
        "evidence": sorted(
            [{"evidence_id": item["evidence_id"], "sha256": item["sha256"]}
             for item in evidence_files],
            key=lambda item: item["evidence_id"],
        ),
        "rea_version": rea_version(binary),
        "schema_version": FACTS_SCHEMA_VERSION,
    })
    return ReaTargetSnapshot(
        root=root,
        snapshot_root=snapshot,
        kind=kind,
        version=version,
        artifact_sha256=artifact_sha256,
        manifest_sha256=target_manifest,
        analysis_manifest_sha256=analysis_manifest,
        facts_sha256=digest_bytes(facts_bytes),
        facts=facts,
        facts_path=persistent_facts_path,
        control_facts_path=control_path,
        evidence_files=tuple(evidence_files),
        rea_version=rea_version(binary),
        uncovered=tuple(uncovered),
    )
"""Run the local REA CLI and validate the Evidence it returns.

Two rules decide the shape of this module:

* **No shell.** The command is an argv list, the binary is resolved once,
  and no REA output is ever interpreted as a command. REA itself has
  commands; this adapter only invokes a fixed allow-listed set.
* **Fail closed.** A missing binary, a timeout, a non-zero exit, malformed
  JSON, an oversized record, or an Evidence identity that does not match
  the contract is an error with a stable code. It is never an empty result.

The environment is an allow-list, not the ambient one: the audit host may
hold provider credentials, and a child that does not need them must not
inherit them. REA settings that select an analysis engine remain visible
through explicit ``REA_*`` / ``GHIDRA_*`` / ``JAVA_HOME`` / ``HOPPER_*``
names.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from flowagent.core.contracts import AgentError
from flowagent.core.hashing import digest_bytes

__all__ = [
    "REA_OPERATIONS",
    "ReaEvidence",
    "run_evidence",
    "version",
]

#: Operations the audit product is allowed to request. This is a product
#: decision, not an REA capability limit: every one is read-only with
#: respect to the target. Dynamic/browser/Frida operations stay out until
#: an explicit authorization and lifecycle model lands.
REA_OPERATIONS = frozenset({
    "inspect_go_binary",
    "inspect_desktop_application",
    "analyze_javascript_application",
})

_EVIDENCE_ID = re.compile(r"^ev_[0-9a-f]{64}$")
_CLI_COMMANDS = {
    "inspect_go_binary": "inspect-go-binary",
    "inspect_desktop_application": "inspect-desktop-application",
    "analyze_javascript_application": "analyze-javascript-application",
}
_DEFAULT_TIMEOUT_SECONDS = 300
_DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_VERSION_CACHE: dict[str, str] = {}

#: Ambient variables the adapter never passes through. A name test is
#: deliberately conservative: an audit host that stores a token in an
#: unrelated variable should not leak it to an analysis child.
_SECRET_NAME = re.compile(
    r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY|"
    r"AUTH|COOKIE|SESSION)",
    re.IGNORECASE,
)

_ENV_ALLOW = {
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "WINDIR", "USERPROFILE", "PATHEXT",
}
_ENV_PREFIX_ALLOW = ("REA_", "GHIDRA_", "HOPPER_", "JAVA_")


@dataclass(frozen=True)
class ReaEvidence:
    """One validated REA Evidence record and the bytes it came from."""

    operation: str
    evidence: dict[str, Any]
    raw_sha256: str
    binary_version: str

    @property
    def evidence_id(self) -> str:
        return str(self.evidence["evidence_id"])

    @property
    def subject_sha256(self) -> str | None:
        subject = self.evidence.get("subject")
        if not isinstance(subject, dict):
            return None
        digest = subject.get("digest")
        if not isinstance(digest, dict):
            return None
        value = digest.get("sha256")
        return value if isinstance(value, str) else None

    @property
    def limitations(self) -> list[str]:
        values = self.evidence.get("limitations")
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, str)]


def _rea_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ENV_ALLOW or key.startswith(_ENV_PREFIX_ALLOW):
            if _SECRET_NAME.search(key):
                continue
            env[key] = value
    return env


def _binary_path(binary: str | None) -> str:
    return binary or os.environ.get("REA_BIN", "rea")


def _run(argv: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            shell=False,
            cwd=str(cwd) if cwd else None,
            env=_rea_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise AgentError(
            "REA_UNAVAILABLE",
            f"REA binary not found: {argv[0]}",
            details={"binary": argv[0], "hint": "install rea-agents or set REA_BIN"},
        ) from error
    except subprocess.TimeoutExpired as error:
        raise AgentError(
            "REA_TIMEOUT",
            f"REA exceeded {timeout}s: {' '.join(argv[:3])}",
            retryable=False,
            details={"binary": argv[0], "timeout_seconds": timeout},
        ) from error
    except OSError as error:
        raise AgentError(
            "REA_UNAVAILABLE",
            f"cannot start REA: {error}",
            details={"binary": argv[0], "error": str(error)},
        ) from error


def version(binary: str | None = None) -> str:
    """Return the REA CLI version, or ``"unknown"`` if it cannot be read.

    Version lookup is diagnostic, not a gate: the Evidence contract is
    validated independently, and an older CLI that still emits a valid
    record should not be refused. The version is pinned in the bundle for
    reproduction.
    """
    path = _binary_path(binary)
    if path in _VERSION_CACHE:
        return _VERSION_CACHE[path]
    try:
        completed = _run([path, "--version"], timeout=30)
    except AgentError:
        _VERSION_CACHE[path] = "unknown"
        return "unknown"
    text = (completed.stdout or "").strip().splitlines()
    value = text[-1].strip() if text else "unknown"
    if completed.returncode != 0 or not value:
        value = "unknown"
    _VERSION_CACHE[path] = value
    return value


def _validate_evidence(operation: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentError("REA_PROTOCOL_ERROR", "REA output is not a JSON object")
    evidence_id = payload.get("evidence_id")
    if not isinstance(evidence_id, str) or not _EVIDENCE_ID.match(evidence_id):
        raise AgentError(
            "REA_PROTOCOL_ERROR",
            "REA output has no valid evidence_id",
            details={"evidence_id": evidence_id},
        )
    subject = payload.get("subject")
    if not isinstance(subject, dict):
        raise AgentError("REA_PROTOCOL_ERROR", "REA output has no subject object")
    normalized = payload.get("normalized_result")
    if not isinstance(normalized, dict):
        raise AgentError("REA_PROTOCOL_ERROR", "REA output has no normalized_result object")
    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or not all(isinstance(item, str) for item in limitations):
        raise AgentError("REA_PROTOCOL_ERROR", "REA output limitations is not a string array")
    reported_operation = payload.get("operation")
    if isinstance(reported_operation, str) and reported_operation != operation:
        raise AgentError(
            "REA_PROTOCOL_ERROR",
            f"REA returned operation {reported_operation!r}, expected {operation!r}",
            details={"expected": operation, "observed": reported_operation},
        )
    return payload


def run_evidence(
    operation: str,
    target: str | Path,
    *,
    binary: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    extra_args: Iterable[str] = (),
    cwd: str | Path | None = None,
) -> ReaEvidence:
    """Run one allow-listed REA operation and return validated Evidence."""
    if operation not in REA_OPERATIONS:
        raise AgentError(
            "REA_OPERATION_REFUSED",
            f"operation {operation!r} is not allowed by the audit adapter",
            details={"allowed": sorted(REA_OPERATIONS)},
        )
    resolved = Path(target).expanduser()
    try:
        resolved = resolved.resolve(strict=True)
    except OSError as error:
        raise AgentError(
            "REA_TARGET_INVALID",
            f"target does not exist: {target}",
            details={"target": str(target), "error": str(error)},
        ) from error
    if timeout <= 0:
        raise AgentError("REA_OPTIONS_INVALID", "timeout must be positive")
    if max_output_bytes <= 0:
        raise AgentError("REA_OPTIONS_INVALID", "max_output_bytes must be positive")

    path = _binary_path(binary)
    cli_operation = _CLI_COMMANDS[operation]
    argv = [path, cli_operation, str(resolved), "--json", *extra_args]
    completed = _run(argv, timeout=timeout, cwd=Path(cwd) if cwd else None)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip().splitlines()
        tail = " | ".join(stderr[-4:])
        raise AgentError(
            "REA_COMMAND_FAILED",
            f"REA {operation} failed with exit {completed.returncode}"
            + (f": {tail}" if tail else ""),
            details={
                "operation": operation,
                "target": str(resolved),
                "exit_code": completed.returncode,
                "stderr_tail": tail,
            },
        )
    raw = (completed.stdout or "").encode("utf-8")
    if len(raw) > max_output_bytes:
        raise AgentError(
            "REA_OUTPUT_TOO_LARGE",
            f"REA {operation} output {len(raw)} bytes exceeds {max_output_bytes}",
            retryable=False,
            details={
                "operation": operation,
                "bytes": len(raw),
                "max_bytes": max_output_bytes,
                "hint": "use a summary view or a narrower operation",
            },
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise AgentError(
            "REA_PROTOCOL_ERROR",
            f"REA {operation} returned malformed JSON: {error}",
            details={"operation": operation, "bytes": len(raw)},
        ) from error
    evidence = _validate_evidence(operation, payload)
    return ReaEvidence(
        operation=operation,
        evidence=evidence,
        raw_sha256=digest_bytes(raw),
        binary_version=version(path),
    )
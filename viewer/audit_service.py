"""Governance-zone service: audit bundles, chain, dispositions and jobs.

This is the Governance counterpart of ``ChatService`` / ``RunManager``: it owns
the audit root, the path jail and the only write path into an audit bundle. The
HTTP layer only translates requests; it never imports ``flowaudit`` itself.

Why a service rather than more methods on the server:

* **One owner per store.** Audit artifacts live under ``<repo>/work/audit`` (or
  ``FCODE_AUDIT_DIR``); nothing else reads or writes them. Work has its own
  ``runs/`` tree and never gets a handle to this one.
* **One capability set.** The audit API can read bundles, append a disposition
  to the signed chain and start an offline audit job. It has no run tools, no
  model loop and no chat store. Work has none of these audit capabilities.
* **One explicit bridge.** Starting an audit is an operator action through
  ``start_run``; the Work agent cannot reach it through a tool.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

__all__ = ["AuditService"]

_AUDIT_DIR_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,63}$")
_AUDIT_POC_RE = re.compile(r"^POC-[0-9]{4,}(-c)?$")
_AUDIT_REA_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
_DISPOSITIONS = ("accepted_risk", "false_positive_disputed", "wont_fix", "repaired")


class AuditService:
    """Read, dispose and launch audits without entering the Work runtime."""

    def __init__(
        self,
        *,
        repo: Path | str | None,
        runtime: Any = None,
        root: Path | str | None = None,
    ) -> None:
        self.repo = Path(repo).expanduser().resolve() if repo else None
        override = os.environ.get("FCODE_AUDIT_DIR", "").strip()
        if root is not None:
            self.root: Path | None = Path(root).expanduser().resolve()
        elif override:
            self.root = Path(override).expanduser().resolve()
        elif self.repo is not None:
            self.root = self.repo / "work" / "audit"
        else:
            self.root = None
        #: Only used to verify audit chains with the kernel's TaskStore; the
        #: service never calls into the run loop or the Work registry.
        self.runtime = runtime
        self._disposition_lock = threading.Lock()

    # ------------------------------------------------------------------ reads

    def bundles(self) -> dict:
        root = self.root
        if root is None:
            return {"error": "live mode unavailable: no repo given", "bundles": []}
        bundles = []
        if root.exists():
            for path in sorted(root.glob("*/audit-bundle.json")):
                if not _AUDIT_DIR_RE.match(path.parent.name):
                    continue
                try:
                    bundle = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                findings = bundle.get("findings") or []
                severity = {"high": 0, "medium": 0, "low": 0}
                unhandled = 0
                for finding in findings:
                    key = str((finding or {}).get("severity") or "low")
                    severity[key] = severity.get(key, 0) + 1
                    if not (finding or {}).get("operator_disposition"):
                        unhandled += 1
                bundles.append({
                    "dir": path.parent.name,
                    "audit_id": bundle.get("audit_id"),
                    "target": (bundle.get("target") or {}).get("identity"),
                    "findings": len(findings),
                    "severity": severity,
                    "unhandled": unhandled,
                    "schema_version": bundle.get("schema_version"),
                    "ruleset_version": (bundle.get("toolchain") or {}).get("ruleset_version"),
                    "evidence": len(bundle.get("poc_evidence_refs") or []),
                    "rea_evidence": len(bundle.get("rea_evidence_refs") or []),
                    "uncovered": len(bundle.get("uncovered_surfaces") or []),
                    "artifact": (bundle.get("target") or {}).get("artifact_sha256"),
                    "recorded": (bundle.get("sessions") or [{}])[0].get("session_ref", ""),
                    "regression": bundle.get("regression"),
                })
        return {"bundles": bundles}

    def bundle(self, name: str) -> dict | tuple[dict, int]:
        path = self._within(name, "audit-bundle.json")
        if path is None:
            return {"error": "not found"}, 404
        if not path.exists():
            return {"error": "not found"}, 404
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "unreadable bundle"}, 500

    def evidence(self, name: str, poc_id: str) -> dict | tuple[dict, int]:
        if not _AUDIT_POC_RE.match(poc_id or ""):
            return {"error": "not found"}, 404
        path = self._within(name, f"{poc_id}.json")
        if path is None or not path.exists():
            return {"error": "not found"}, 404
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "unreadable evidence"}, 500

    def rea(self, name: str, filename: str) -> dict | tuple[dict, int]:
        """Read one REA evidence/facts file named by the bundle itself.

        The bundle is the allow-list: only paths it references under
        ``rea-evidence/`` are readable. A filename is a single segment, so
        traversal, absolute paths and symlinks outside the directory are
        refused before any bytes are served.
        """
        if not _AUDIT_REA_FILE_RE.match(filename or ""):
            return {"error": "not found"}, 404
        bundle = self.bundle(name)
        if isinstance(bundle, tuple):
            return bundle
        allowed: set[str] = set()
        for key in ("rea_facts_ref", "rea_control_facts_ref"):
            ref = bundle.get(key)
            if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                allowed.add(ref["path"])
        for ref in bundle.get("rea_evidence_refs") or []:
            if isinstance(ref, dict) and isinstance(ref.get("path"), str):
                allowed.add(ref["path"])
        relative = f"rea-evidence/{filename}"
        if relative not in allowed:
            return {"error": "not found"}, 404
        path = self._within(name, relative)
        if path is None or not path.exists():
            return {"error": "not found"}, 404
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "unreadable REA evidence"}, 500

    def regression(self, name: str) -> dict | tuple[dict, int]:
        path = self._within(name, "regression.json")
        if path is None:
            return {"error": "not found"}, 404
        if not path.exists():
            return {"prior_audit_id": None, "known": [], "new": [],
                    "fixed": [], "regressed": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"prior_audit_id": None, "known": [], "new": [],
                    "fixed": [], "regressed": []}

    def spec_gap(self, name: str) -> dict | tuple[dict, int]:
        path = self._within(name, "spec-gap-report.json")
        if path is None or not path.exists():
            return {"error": "no spec-gap report"}, 404
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"error": "unreadable spec-gap report"}, 500

    def chain(self, name: str) -> dict | tuple[dict, int]:
        """Read the audit session's hash-linked event chain from session.sqlite."""
        root = self.root
        if root is None or not _AUDIT_DIR_RE.match(name or ""):
            return {"error": "not found"}, 404
        audit_dir = (root / name).resolve()
        if root.resolve() not in audit_dir.parents or not audit_dir.is_dir():
            return {"error": "not found"}, 404
        db_path = audit_dir / "session.sqlite"
        if not db_path.exists():
            return {"events": [], "chain_intact": True, "count": 0,
                    "verification": "none", "note": "no session log"}
        audit_id = name
        bundle_path = audit_dir / "audit-bundle.json"
        try:
            audit_id = str(json.loads(
                bundle_path.read_text(encoding="utf-8")).get("audit_id") or name)
        except (OSError, ValueError):
            pass
        task_id = f"audit-{audit_id}"
        runtime = self.runtime
        if runtime is not None and getattr(runtime, "TaskStore", None) is not None:
            events = None
            try:
                store = runtime.TaskStore(db_path)
                events = store.events(task_id)
                store.verify_chain(task_id)
                return {"events": events, "chain_intact": True,
                        "verification": "kernel", "count": len(events)}
            except Exception as error:  # noqa: BLE001 — AgentError → tamper evidence
                if isinstance(events, list):
                    return {"events": events, "chain_intact": False,
                            "verification": "kernel", "count": len(events),
                            "tamper": str(error)}
                return {"error": f"cannot read session: {error}"}, 500
        # Kernel unavailable: link-only check, explicitly labelled weak.
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                cursor = conn.execute(
                    "SELECT sequence, type, payload, prev_hash, hash, at "
                    "FROM event ORDER BY sequence ASC"
                )
                events = []
                for row in cursor:
                    payload = json.loads(row["payload"]) if row["payload"] else {}
                    events.append({
                        "sequence": row["sequence"],
                        "type": row["type"],
                        "payload": payload,
                        "prev_hash": row["prev_hash"],
                        "hash": row["hash"],
                        "at": row["at"],
                    })
                chain_intact = True
                prev = None
                for event in events:
                    if prev is not None and event["prev_hash"] != prev:
                        chain_intact = False
                        break
                    prev = event["hash"]
                return {
                    "events": events,
                    "chain_intact": chain_intact,
                    "verification": "link-only",
                    "count": len(events),
                }
            finally:
                conn.close()
        except (sqlite3.Error, ValueError, KeyError) as error:
            return {"error": f"cannot read session: {error}"}, 500

    # ------------------------------------------------------------- disposition

    def disposition(self, name: str, finding_id: str,
                    disposition: str) -> dict | tuple[dict, int]:
        """Set ``operator_disposition`` on one finding in the pinned bundle.

        The finding's status and PoC outcome are untouched — only the
        operator's judgment field changes. The file is written atomically, then
        the same fact is appended to the audit session chain when one exists.
        """
        if self.root is None or not _AUDIT_DIR_RE.match(name or ""):
            return {"error": "not found"}, 404
        if disposition not in _DISPOSITIONS and disposition != "":
            return {
                "error": f"invalid disposition; must be one of ('', "
                         f"{', '.join(_DISPOSITIONS)})"
            }, 400
        path = self._within(name, "audit-bundle.json")
        if path is None or not path.exists():
            return {"error": "not found"}, 404
        try:
            with self._disposition_lock:
                bundle = json.loads(path.read_text(encoding="utf-8"))
                for finding in bundle.get("findings") or []:
                    if finding.get("finding_id") == finding_id:
                        finding["operator_disposition"] = disposition or None
                        break
                else:
                    return {"error": "finding not found"}, 404
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(bundle, ensure_ascii=False, indent=2),
                               encoding="utf-8")
                tmp.replace(path)
        except (OSError, ValueError):
            return {"error": "unreadable bundle"}, 500
        self._chain_disposition(name, bundle, finding_id, disposition)
        return {"finding_id": finding_id, "operator_disposition": disposition or None}

    def disposition_summary(self) -> list[dict]:
        """Operator dispositions across bundles, for the review-profile card."""
        root = self.root
        if root is None or not root.is_dir():
            return []
        rows: list[dict] = []
        for path in sorted(root.glob("*/audit-bundle.json")):
            if not _AUDIT_DIR_RE.match(path.parent.name):
                continue
            try:
                bundle = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            target = str((bundle.get("target") or {}).get("identity") or "")
            for finding in bundle.get("findings") or []:
                disposition = finding.get("operator_disposition")
                if not disposition:
                    continue
                rows.append({
                    "dir": path.parent.name,
                    "audit_id": bundle.get("audit_id"),
                    "finding_id": finding.get("finding_id"),
                    "disposition": disposition,
                    "category": finding.get("category"),
                    "severity": finding.get("severity"),
                    "target": target,
                })
        rows.sort(key=lambda row: str(row.get("audit_id") or ""))
        return rows

    # ------------------------------------------------------------------- jobs

    def start_run(self, target: str, adapter: str | None = None, *,
                  target_md: str | None = None,
                  osv_snapshot: str | None = None) -> str:
        """Start one offline audit job; the only audit-launch entry point."""
        if self.repo is None:
            raise ValueError("no repo; audit unavailable")
        from audit_jobs import start_audit_run

        return start_audit_run(self.repo, target, adapter,
                               target_md=target_md, osv_snapshot=osv_snapshot)

    def job_status(self, job_id: str) -> dict | None:
        from audit_jobs import job_status

        return job_status(job_id)

    # ----------------------------------------------------------------- private

    def _within(self, name: str, *parts: str) -> Path | None:
        """Resolve ``root/name/parts`` and refuse anything outside the root."""
        root = self.root
        if root is None or not _AUDIT_DIR_RE.match(name or ""):
            return None
        base = (root / name).resolve()
        root_resolved = root.resolve()
        if root_resolved not in base.parents:
            return None
        path = base.joinpath(*parts).resolve()
        if base not in path.parents:
            return None
        return path

    def _chain_disposition(self, name: str, bundle: dict,
                           finding_id: str, disposition: str) -> None:
        """Append the operator fact to the audit chain; failure is downgraded.

        The bundle write already succeeded, so a missing/unreachable session
        must not fail the endpoint. Bundles produced by the pure CLI batch have
        no session.sqlite and silently skip this step.
        """
        if self.root is None:
            return
        db_path = self.root / name / "session.sqlite"
        if not db_path.exists():
            return
        if self.runtime is None:
            src = Path(__file__).resolve().parents[1] / "src"
            if str(src) not in sys.path and (src / "flowaudit" / "__init__.py").exists():
                sys.path.insert(0, str(src))
        try:
            from flowagent.core.store import TaskStore
            from flowaudit.session import AuditSession
        except ImportError:
            return
        audit_id = str(bundle.get("audit_id") or name)
        try:
            session = AuditSession.attach(TaskStore(db_path), f"audit-{audit_id}")
            session.record_disposition(finding_id, disposition)
        except Exception:  # noqa: BLE001 — chain event is an enhancement
            pass
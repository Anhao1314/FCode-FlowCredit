"""Audit run jobs: the server executes the offline audit CLI on request.

治理台 /挖洞 与 `/audit <target>` 斜杠命令的落点:目标默认必须位于
--repo 之内(路径穿越在 resolve 后被包含关系拒绝);发运工件(shipped
artifact)在仓库外——操作员可用 ``FCODE_AUDIT_ROOTS`` 显式放行若干绝对
根目录(os.pathsep 分隔,如 ``/Applications``),目标 resolve 后必须落进
repo 或任一放行根。子进程跑 `python -m flowaudit.cli audit <target>`
(离线确定性管线,无模型调用,无网络),产出落在 work/audit/<audit_id>/
——发现账本的读取端已经在 /api/audit/bundles 等着它。作业状态在内存里,
进程重启即失——审计的持久证据在落盘的 bundle 里,不在作业记录里,
这是有意的。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _within(child: Path, root: Path) -> bool:
    return child == root or root in child.parents


def _audit_roots(repo: Path) -> list[Path]:
    """repo 本身 + FCODE_AUDIT_ROOTS 放行的绝对根(读环境变量,便于测试)。"""
    roots = [repo]
    raw = os.environ.get("FCODE_AUDIT_ROOTS", "")
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part).resolve())
    return roots


def resolve_adapter(resolved: Path, repo: Path, explicit: str | None = None) -> str:
    """目标在仓库内 = 源码树走 repo 适配器；放行根内的仓外目标 =
    发运工件走 REA 挖洞。显式指定时校验合法值。"""
    if explicit:
        if explicit not in ("repo", "rea"):
            raise ValueError(f"unknown adapter: {explicit}")
        return explicit
    return "repo" if _within(resolved, repo) else "rea"


def _repo_file(repo: Path, value: str | None, label: str) -> Path | None:
    """解析审计附带文件（target.md / OSV 快照）；只允许仓库内路径。"""
    if not value:
        return None
    raw = Path(value).expanduser()
    resolved = (repo / raw).resolve() if not raw.is_absolute() else raw.resolve()
    if not _within(resolved, repo):
        raise ValueError(f"{label} escapes the repository")
    if not resolved.is_file():
        raise ValueError(f"{label} not found: {value}")
    return resolved


def start_audit_run(repo: Path, target: str, adapter: str | None = None,
                    *, target_md: str | None = None,
                    osv_snapshot: str | None = None) -> str:
    repo = Path(repo).resolve()
    resolved = (repo / target).resolve()
    if not any(_within(resolved, root) for root in _audit_roots(repo)):
        raise ValueError("target escapes the repository and FCODE_AUDIT_ROOTS")
    if not resolved.exists():
        raise ValueError(f"target not found: {target}")
    descriptor = _repo_file(repo, target_md, "target descriptor")
    snapshot = _repo_file(repo, osv_snapshot, "OSV snapshot")
    resolved_adapter = resolve_adapter(resolved, repo, adapter)
    job_id = f"audit-{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _JOBS[job_id] = {"job_id": job_id, "status": "running", "target": str(resolved)}

    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    command = [sys.executable, "-m", "flowaudit.cli", "audit", str(resolved),
               "--adapter", resolved_adapter]
    if descriptor is not None:
        command.extend(["--target-md", str(descriptor)])
    if snapshot is not None:
        command.extend(["--osv-snapshot", str(snapshot)])

    def work() -> None:
        try:
            completed = subprocess.run(
                command,
                cwd=str(repo), env=env, capture_output=True, text=True, timeout=900,
            )
            if completed.returncode != 0:
                lines = (completed.stderr or "").strip().splitlines()
                detail = lines[-1] if lines else f"exit {completed.returncode}"
                with _LOCK:
                    _JOBS[job_id].update({"status": "failed", "error": detail[:300]})
                return
            summary = None
            for line in reversed(completed.stdout.strip().splitlines()):
                try:
                    summary = json.loads(line)
                    break
                except ValueError:
                    continue
            with _LOCK:
                _JOBS[job_id].update({
                    "status": "done",
                    "audit_id": (summary or {}).get("audit_id"),
                    "findings": (summary or {}).get("findings"),
                    "evidence": (summary or {}).get("evidence_records"),
                })
        except Exception as exc:  # noqa: BLE001 — 作业线程必须自己兜底
            with _LOCK:
                _JOBS[job_id].update({"status": "failed", "error": str(exc)[:300]})

    threading.Thread(target=work, daemon=True).start()
    return job_id


def job_status(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None

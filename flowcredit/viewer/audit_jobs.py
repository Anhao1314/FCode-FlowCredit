"""Audit run jobs: the server executes the offline audit CLI on request.

单一对话框的审计入口(`/audit <target>` 斜杠命令)落到这里:目标必须
位于 --repo 之内(路径穿越在 resolve 后被包含关系拒绝),子进程跑
`python -m flowaudit.cli audit <target>`(离线确定性管线,无模型调用,
无网络),产出落在 work/audit/<audit_id>/ ——发现账本的读取端已经在
/api/audit/bundles 等着它。作业状态在内存里,进程重启即失——审计的
持久证据在落盘的 bundle 里,不在作业记录里,这是有意的。
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


def start_audit_run(repo: Path, target: str) -> str:
    repo = Path(repo).resolve()
    resolved = (repo / target).resolve()
    if resolved != repo and repo not in resolved.parents:
        raise ValueError("target escapes the repository")
    if not resolved.exists():
        raise ValueError(f"target not found: {target}")
    job_id = f"audit-{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _JOBS[job_id] = {"job_id": job_id, "status": "running", "target": str(resolved)}

    env = {**os.environ, "PYTHONPATH": str(repo / "src")}

    def work() -> None:
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "flowaudit.cli", "audit", str(resolved)],
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

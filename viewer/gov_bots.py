"""Governance bot roster: standing roles that run audit/review work.

Phase A is deliberately small. A bot is not a general-purpose coworker and
does not get a shared cloud computer. It is a named role with a fixed tool
surface and a durable run log:

* **auditor** starts the offline REA pipeline and records its findings.
* **verifier** checks the signed chain and regression buckets of one bundle.

Both run in the server process; the facts they produce remain in the audit
bundle (the source of truth). ``bot_runs`` is only the work log, so losing it
never loses evidence. This is the same split as audit jobs: jobs are ephemeral,
bundles are durable.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from console_scheduler import describe_next, is_due, parse_hhmm

__all__ = ["BOT_DEFS", "GovBotService"]

BOT_DEFS = [
    {
        "id": "auditor",
        "name": "审计员",
        "role": "REA Auditor",
        "initial": "审",
        "charter": "对目标执行离线确定性审计，产出 findings 与证据；只读，不改代码。",
        "tools": ["audit.run", "bundle.read"],
        "model_policy": "deterministic",
        "mode": "on-demand",
    },
    {
        "id": "verifier",
        "name": "复核员",
        "role": "Chain Verifier",
        "initial": "核",
        "charter": "验证签名链与回归对比，确认一次审计是否完整、是否出现回归。",
        "tools": ["chain.read", "regression.read", "bundle.read"],
        "model_policy": "deterministic",
        "mode": "on-demand",
    },
]
_BY_ID = {bot["id"]: bot for bot in BOT_DEFS}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_runs (
    run_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual',
    target TEXT NOT NULL,
    status TEXT NOT NULL,
    job_id TEXT,
    audit_id TEXT,
    findings INTEGER NOT NULL DEFAULT 0,
    evidence INTEGER NOT NULL DEFAULT 0,
    chain_intact INTEGER,
    events INTEGER NOT NULL DEFAULT 0,
    regression_json TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS bot_runs_bot ON bot_runs(bot_id, started_at DESC);
CREATE TABLE IF NOT EXISTS bot_routines (
    id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    frequency TEXT NOT NULL DEFAULT '每天',
    time TEXT NOT NULL DEFAULT '09:00',
    weekday INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    last_run_id TEXT,
    last_status TEXT,
    created TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class GovBotService:
    """Durable roster + run log for the governance team surface."""

    def __init__(self, audit: Any, db_path: Path | str, *, start_scheduler: bool = True) -> None:
        self.audit = audit
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        # A run left in queued/running belongs to a previous server process.
        # Mark it failed instead of showing a phantom "working" bot forever.
        with self._lock:
            self._db.execute(
                "UPDATE bot_runs SET status='failed', error=?, finished_at=? "
                "WHERE status IN ('queued','running')",
                ("server restarted before completion", _now()),
            )
            self._db.commit()
        if start_scheduler:
            self._start_scheduler()

    # ------------------------------------------------------------------ reads

    def snapshot(self) -> dict:
        runs = self.runs(limit=200)
        by_bot: dict[str, list[dict]] = {bot["id"]: [] for bot in BOT_DEFS}
        for run in runs:
            by_bot.setdefault(str(run.get("bot_id")), []).append(run)
        bots = []
        for definition in BOT_DEFS:
            bot_runs = by_bot.get(definition["id"], [])
            current = next(
                (run for run in bot_runs if run.get("status") in ("queued", "running")),
                None,
            )
            last = bot_runs[0] if bot_runs else None
            routines = self.routines(bot_id=definition["id"])
            bots.append({
                **definition,
                "status": "running" if current else "idle",
                "current": current,
                "last": last,
                "routines": routines,
                "counts": {
                    "runs": len(bot_runs),
                    "findings": sum(int(run.get("findings") or 0) for run in bot_runs),
                    "chain_failures": sum(
                        1 for run in bot_runs
                        if run.get("chain_intact") is False
                    ),
                    "routines": len(routines),
                },
            })
        return {"bots": bots, "generated_at": _now()}

    def runs(self, bot_id: str | None = None, limit: int = 50) -> list[dict]:
        query = "SELECT * FROM bot_runs"
        params: list[Any] = []
        if bot_id:
            query += " WHERE bot_id=?"
            params.append(bot_id)
        query += " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bot_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return self._row(row) if row else None

    # -------------------------------------------------------------- routines

    def routines(self, bot_id: str | None = None, enabled_only: bool = False) -> list[dict]:
        query = "SELECT * FROM bot_routines"
        clauses: list[str] = []
        params: list[Any] = []
        if bot_id:
            clauses.append("bot_id=?")
            params.append(bot_id)
        if enabled_only:
            clauses.append("enabled=1")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created DESC, rowid DESC"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        now = datetime.now()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item.get("enabled"))
            item["bot_name"] = (_BY_ID.get(item["bot_id"]) or {}).get("name", item["bot_id"])
            item["next"] = describe_next(item, now)
            result.append(item)
        return result

    def get_routine(self, routine_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bot_routines WHERE id=?", (routine_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["bot_name"] = (_BY_ID.get(item["bot_id"]) or {}).get("name", item["bot_id"])
        item["next"] = describe_next(item, datetime.now())
        return item

    def create_routine(self, bot_id: str, payload: dict) -> dict:
        definition = _BY_ID.get(str(bot_id or ""))
        if definition is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        target = str(payload.get("target") or "").strip()
        if definition["id"] == "auditor" and not target:
            raise ValueError("auditor routine needs a target path")
        frequency = str(payload.get("frequency") or "每天").strip()
        if frequency not in ("每天", "工作日", "每周"):
            raise ValueError("frequency must be 每天 / 工作日 / 每周")
        time_text = str(payload.get("time") or "09:00").strip()
        parse_hhmm(time_text)
        weekday = payload.get("weekday")
        if frequency == "每周":
            try:
                weekday = int(weekday) if weekday is not None else datetime.now().weekday()
            except (TypeError, ValueError) as error:
                raise ValueError("weekday must be 0..6") from error
            if not 0 <= weekday <= 6:
                raise ValueError("weekday must be 0..6")
        else:
            weekday = None
        with self._lock:
            count = self._db.execute(
                "SELECT COUNT(*) FROM bot_routines WHERE bot_id=?", (definition["id"],)
            ).fetchone()[0]
        if count >= 10:
            raise ValueError("each bot can own at most 10 routines")
        routine_id = f"rt-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._db.execute(
                "INSERT INTO bot_routines (id, bot_id, target, frequency, time, weekday,"
                " enabled, created) VALUES (?,?,?,?,?,?,?,?)",
                (routine_id, definition["id"], target, frequency, time_text,
                 weekday, 1, _now()),
            )
            self._db.commit()
        return self.get_routine(routine_id) or {}

    def set_routine_enabled(self, routine_id: str, enabled: bool) -> dict:
        with self._lock:
            cur = self._db.execute(
                "UPDATE bot_routines SET enabled=? WHERE id=?",
                (1 if enabled else 0, routine_id),
            )
            self._db.commit()
        if cur.rowcount <= 0:
            raise ValueError("routine not found")
        return self.get_routine(routine_id) or {}

    def delete_routine(self, routine_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM bot_routines WHERE id=?", (routine_id,))
            self._db.commit()
        if cur.rowcount <= 0:
            raise ValueError("routine not found")
        return True

    def fire_due(self, now: datetime | None = None) -> list[dict]:
        """Start runs for due routines. Pure-ish test seam, same as scheduler."""
        now = now or datetime.now()
        fired: list[dict] = []
        for routine in self.routines(enabled_only=True):
            if not is_due(routine, now):
                continue
            if self._has_running(routine["bot_id"]):
                continue
            fired_at = now.strftime("%Y-%m-%dT%H:%M:%S")
            try:
                run = self.start_run(routine["bot_id"], routine["target"], trigger="routine")
                self._mark_routine(routine["id"], fired_at, run["run_id"], "running")
                fired.append({"routine_id": routine["id"], "run_id": run["run_id"], "status": "running"})
            except Exception as error:  # noqa: BLE001 — one bad routine must not stop others
                self._mark_routine(routine["id"], fired_at, "", f"error: {error}")
                fired.append({"routine_id": routine["id"], "status": f"error: {error}"})
        return fired

    def shutdown(self) -> None:
        self._stop.set()

    def _start_scheduler(self) -> None:
        if self._scheduler is not None and self._scheduler.is_alive():
            return
        self._stop.clear()
        self._scheduler = threading.Thread(
            target=self._schedule_loop, name="gov-bot-scheduler", daemon=True,
        )
        self._scheduler.start()

    def _schedule_loop(self) -> None:
        while not self._stop.wait(20.0):
            try:
                self.fire_due()
            except Exception:  # noqa: BLE001 — scheduler loop never exits
                continue

    def _has_running(self, bot_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM bot_runs WHERE bot_id=? AND status IN ('queued','running') LIMIT 1",
                (bot_id,),
            ).fetchone()
        return row is not None

    def _mark_routine(self, routine_id: str, fired_at: str,
                      run_id: str, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE bot_routines SET last_fired_at=?, last_run_id=?, last_status=? WHERE id=?",
                (fired_at, run_id, status, routine_id),
            )
            self._db.commit()

    # ------------------------------------------------------------------ start

    def start_run(self, bot_id: str, target: str, trigger: str = "manual") -> dict:
        definition = _BY_ID.get(str(bot_id or ""))
        if definition is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        resolved = str(target or "").strip()
        if definition["id"] == "auditor" and not resolved:
            raise ValueError("auditor needs a target path")
        if definition["id"] == "verifier" and not resolved:
            bundles = (self.audit.bundles() or {}).get("bundles") or []
            if not bundles:
                raise ValueError("no audit bundle to verify")

            def audit_day(item: dict) -> str:
                match = re.search(r"(\d{4}-\d{2}-\d{2})", str(item.get("dir") or ""))
                return match.group(1) if match else ""

            # Date first; hash suffix breaks ties. Sorting by the full dir
            # would put `flowcredit-...` ahead of a newer `demo-target-...`.
            latest = max(
                bundles,
                key=lambda item: (audit_day(item), str(item.get("dir") or "")),
            )
            resolved = str(latest.get("dir") or "")
        run_id = f"bot-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._db.execute(
                "INSERT INTO bot_runs (run_id, bot_id, trigger, target, status, started_at)"
                " VALUES (?,?,?,?,?,?)",
                (run_id, definition["id"], trigger if trigger in ("manual", "routine") else "manual",
                 resolved, "queued", _now()),
            )
            self._db.commit()
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, definition["id"], resolved),
            name=f"gov-bot-{run_id}",
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()
        return self.get_run(run_id) or {}

    # --------------------------------------------------------------- internal

    def _execute(self, run_id: str, bot_id: str, target: str) -> None:
        self._update(run_id, status="running")
        try:
            if bot_id == "auditor":
                self._run_auditor(run_id, target)
            elif bot_id == "verifier":
                self._run_verifier(run_id, target)
            else:
                self._finish(run_id, status="failed", error=f"unsupported bot {bot_id}")
        except Exception as error:  # noqa: BLE001 — one run must not kill the server
            self._finish(run_id, status="failed", error=f"{type(error).__name__}: {error}")
        finally:
            self._threads.pop(run_id, None)

    def _run_auditor(self, run_id: str, target: str) -> None:
        job_id = self.audit.start_run(target)
        self._update(run_id, job_id=job_id)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            job = self.audit.job_status(job_id) or {}
            status = str(job.get("status") or "")
            if status == "done":
                self._finish(
                    run_id,
                    status="done",
                    audit_id=job.get("audit_id"),
                    findings=int(job.get("findings") or 0),
                    evidence=int(job.get("evidence") or 0),
                )
                return
            if status == "failed":
                self._finish(run_id, status="failed", error=str(job.get("error") or "audit failed"))
                return
            time.sleep(0.5)
        self._finish(run_id, status="failed", error="audit timed out after 900s")

    def _run_verifier(self, run_id: str, audit_dir: str) -> None:
        bundle = self.audit.bundle(audit_dir)
        if isinstance(bundle, tuple):
            payload, status = bundle
            self._finish(run_id, status="failed", error=f"bundle unavailable ({status}): {payload}")
            return
        chain = self.audit.chain(audit_dir)
        if isinstance(chain, tuple):
            payload, status = chain
            self._finish(run_id, status="failed", error=f"chain unavailable ({status}): {payload}")
            return
        regression = self.audit.regression(audit_dir)
        if isinstance(regression, tuple):
            regression = {}
        counts = {
            key: len(regression.get(key) or [])
            for key in ("known", "new", "fixed", "regressed")
        }
        self._finish(
            run_id,
            status="done",
            audit_id=str(bundle.get("audit_id") or audit_dir),
            findings=len(bundle.get("findings") or []),
            evidence=len(bundle.get("poc_evidence_refs") or []),
            chain_intact=bool(chain.get("chain_intact")),
            events=int(chain.get("count") or len(chain.get("events") or [])),
            regression_json=json.dumps(counts, ensure_ascii=False),
        )

    def _update(self, run_id: str, **fields: Any) -> None:
        self._write(run_id, fields, finished=False)

    def _finish(self, run_id: str, *, status: str, **fields: Any) -> None:
        payload = {**fields, "status": status}
        self._write(run_id, payload, finished=True)

    def _write(self, run_id: str, fields: dict[str, Any], *, finished: bool) -> None:
        clean = {key: value for key, value in fields.items() if value is not None}
        if finished:
            clean["finished_at"] = _now()
        if not clean:
            return
        assignments = ", ".join(f"{key}=?" for key in clean)
        with self._lock:
            self._db.execute(
                f"UPDATE bot_runs SET {assignments} WHERE run_id=?",
                (*clean.values(), run_id),
            )
            self._db.commit()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        item = dict(row)
        if item.get("chain_intact") is not None:
            item["chain_intact"] = bool(item["chain_intact"])
        raw = item.pop("regression_json", None)
        item["regression"] = {}
        if raw:
            try:
                item["regression"] = json.loads(raw)
            except ValueError:
                item["regression"] = {}
        return item
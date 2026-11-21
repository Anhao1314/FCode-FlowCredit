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
from urllib.parse import urlsplit
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
        "explain_policy": "model-explainer",
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
        "explain_policy": "model-explainer",
        "mode": "on-demand",
    },
    {
        "id": "analyst",
        "name": "分析员",
        "role": "Triage Analyst",
        "initial": "析",
        "charter": "读取已完成的确定性审计，给出跟进优先级；只提议，不裁决、不改证据。",
        "tools": ["bundle.read", "triage.propose"],
        "model_policy": "propose-only",
        "explain_policy": "",
        "mode": "on-demand",
    },
]
_BY_ID = {bot["id"]: bot for bot in BOT_DEFS}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_runs (
    run_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual',
    source TEXT NOT NULL DEFAULT 'team',
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
    parent_run_id TEXT,
    swarm_id TEXT,
    triage_mode TEXT,
    triage_status TEXT,
    triage_json TEXT,
    triage_error TEXT,
    triage_run_id TEXT,
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
CREATE TABLE IF NOT EXISTS bot_settings (
    bot_id TEXT PRIMARY KEY,
    daily_run_limit INTEGER NOT NULL DEFAULT 12,
    routine_approval INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bot_skills (
    id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_explanations (
    run_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    model TEXT NOT NULL,
    question TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_microusd INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS bot_skills_run ON bot_skills(source_run_id);
CREATE TABLE IF NOT EXISTS bot_swarms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    bot_id TEXT NOT NULL DEFAULT 'auditor',
    status TEXT NOT NULL DEFAULT 'running',
    max_concurrency INTEGER NOT NULL DEFAULT 2,
    target_md TEXT,
    osv_snapshot TEXT,
    created TEXT NOT NULL,
    finished_at TEXT
);
"""

_DEFAULT_SETTINGS = {
    "auditor": {"daily_run_limit": 8, "routine_approval": 0, "triage_mode": "off"},
    "verifier": {"daily_run_limit": 12, "routine_approval": 0, "triage_mode": "off"},
    "analyst": {"daily_run_limit": 12, "routine_approval": 0, "triage_mode": "off"},
}

#: triage 档位：off=完整扫描（默认）；shadow=只记录模型计划；active=计划 +
#: 一条待操作员批准的复扫运行。active 绝不自动执行。
TRIAGE_MODES = ("off", "shadow", "active")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class GovBotService:
    """Durable roster + run log for the governance team surface."""

    def __init__(self, audit: Any, db_path: Path | str, *, start_scheduler: bool = True,
                 provider_factory: Any = None) -> None:
        self.audit = audit
        self._provider_factory = provider_factory
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._swarm_thread: threading.Thread | None = None
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()
        # 旧库迁移：bot_runs 的 triage 列 + source 列 + bot_settings.triage_mode。
        self._ensure_columns("bot_runs", {
            "source": "TEXT NOT NULL DEFAULT 'team'",
            "parent_run_id": "TEXT",
            "swarm_id": "TEXT",
            "triage_mode": "TEXT",
            "triage_status": "TEXT",
            "triage_json": "TEXT",
            "triage_error": "TEXT",
            "triage_run_id": "TEXT",
        })
        self._ensure_columns("bot_settings", {
            "triage_mode": "TEXT NOT NULL DEFAULT 'off'",
        })
        with self._lock:
            for bot_id, defaults in _DEFAULT_SETTINGS.items():
                self._db.execute(
                    "INSERT OR IGNORE INTO bot_settings"
                    " (bot_id, daily_run_limit, routine_approval, triage_mode)"
                    " VALUES (?,?,?,?)",
                    (bot_id, defaults["daily_run_limit"],
                     defaults["routine_approval"], defaults.get("triage_mode", "off")),
                )
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
            pending = next(
                (run for run in bot_runs if run.get("status") == "pending"),
                None,
            )
            last = bot_runs[0] if bot_runs else None
            routines = self.routines(bot_id=definition["id"])
            skills = self.skills(bot_id=definition["id"])
            settings = self.get_settings(definition["id"])
            bots.append({
                **definition,
                "explain_policy": definition.get("explain_policy", ""),
                "status": "running" if current else "pending" if pending else "idle",
                "current": current,
                "pending": pending,
                "last": last,
                "settings": settings,
                "routines": routines,
                "skills": skills,
                "runs_today": self._runs_today(definition["id"]),
                "counts": {
                    "runs": len(bot_runs),
                    "findings": sum(int(run.get("findings") or 0) for run in bot_runs),
                    "chain_failures": sum(
                        1 for run in bot_runs
                        if run.get("chain_intact") is False
                    ),
                    "routines": len(routines),
                    "skills": len(skills),
                    "pending": sum(1 for run in bot_runs if run.get("status") == "pending"),
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

    # ------------------------------------------------------------ explanations
    # 解释层产物：非确定性、非裁决、不进审计包；单独建表就是为了
    # 让这个边界在存储层可见——bundle 表和解释表永远不混。

    def save_explanation(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO bot_explanations"
                " (run_id, bot_id, model, question, text, prompt_sha256,"
                "  tokens, cost_microusd, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    str(record["run_id"]),
                    str(record.get("bot_id") or ""),
                    str(record.get("model") or ""),
                    str(record.get("question") or ""),
                    str(record.get("text") or ""),
                    str(record.get("prompt_sha256") or ""),
                    int(record.get("tokens") or 0),
                    int(record.get("cost_microusd") or 0),
                    record.get("created") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                ),
            )
            self._db.commit()
            return self.explanation(record["run_id"])  # type: ignore[return-value]

    def explanation(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bot_explanations WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def explanations_map(self, limit: int = 200) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM bot_explanations ORDER BY created DESC, rowid DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return {row["run_id"]: dict(row) for row in rows}

    def explanations_for_audit(self, audit_dir: str) -> list[dict]:
        """一个审计包的解读层：包级 `audit:<dir>` + 发现级 `audit:<dir>:<fid>`。

        目录名被 `_AUDIT_DIR_RE` 约束（小写字母数字点连字符），不含 LIKE
        通配符，前缀匹配是精确的。这些记录与 bot run 解读（`bot-*`）空间
        不重叠，团队频道的 explanations_map 不受影响。
        """
        clean = str(audit_dir or "").strip()
        if not clean:
            return []
        prefix = f"audit:{clean}"
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM bot_explanations WHERE run_id=? OR run_id LIKE ?"
                " ORDER BY created DESC, rowid DESC",
                (prefix, prefix + ":%"),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------- settings / skills

    def get_settings(self, bot_id: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bot_settings WHERE bot_id=?", (bot_id,)
            ).fetchone()
        if row is None:
            defaults = _DEFAULT_SETTINGS.get(
                bot_id, {"daily_run_limit": 12, "routine_approval": 0, "triage_mode": "off"})
            return {
                "bot_id": bot_id,
                "daily_run_limit": defaults["daily_run_limit"],
                "routine_approval": bool(defaults["routine_approval"]),
                "triage_mode": defaults.get("triage_mode", "off"),
            }
        item = dict(row)
        item["routine_approval"] = bool(item.get("routine_approval"))
        item.setdefault("triage_mode", "off")
        return item

    def set_settings(self, bot_id: str, payload: dict) -> dict:
        if bot_id not in _BY_ID:
            raise ValueError(f"unknown bot: {bot_id!r}")
        current = self.get_settings(bot_id)
        try:
            limit = int(payload.get("daily_run_limit", current["daily_run_limit"]))
        except (TypeError, ValueError) as error:
            raise ValueError("daily_run_limit must be an integer") from error
        if not 1 <= limit <= 50:
            raise ValueError("daily_run_limit must be 1..50")
        approval = bool(payload.get("routine_approval", current["routine_approval"]))
        triage_mode = str(payload.get("triage_mode", current.get("triage_mode") or "off")).strip().lower()
        if triage_mode not in TRIAGE_MODES:
            raise ValueError("triage_mode must be off, shadow or active")
        with self._lock:
            self._db.execute(
                "UPDATE bot_settings SET daily_run_limit=?, routine_approval=?, triage_mode=?"
                " WHERE bot_id=?",
                (limit, 1 if approval else 0, triage_mode, bot_id),
            )
            self._db.commit()
        return self.get_settings(bot_id)

    def skills(self, bot_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM bot_skills"
        params: list[Any] = []
        if bot_id:
            query += " WHERE bot_id=?"
            params.append(bot_id)
        query += " ORDER BY created DESC, rowid DESC"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["bot_name"] = (_BY_ID.get(item["bot_id"]) or {}).get("name", item["bot_id"])
            result.append(item)
        return result

    def create_skill(self, run_id: str, name: str = "") -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("run not found")
        if run.get("status") != "done":
            raise ValueError("only a completed run can become a skill")
        bot = _BY_ID.get(str(run.get("bot_id")))
        if bot is None:
            raise ValueError("run has an unknown bot")
        target = str(run.get("target") or "")
        leaf = target.rstrip("/").split("/")[-1] or target or "任务"
        skill_name = str(name or "").strip() or f"{bot['name']} · {leaf}"
        if len(skill_name) > 80:
            raise ValueError("skill name is too long (max 80)")
        with self._lock:
            try:
                count = self._db.execute(
                    "SELECT COUNT(*) FROM bot_skills WHERE bot_id=?", (bot["id"],)
                ).fetchone()[0]
                skill_id = f"sk-{uuid.uuid4().hex[:12]}"
                self._db.execute(
                    "INSERT INTO bot_skills (id, bot_id, name, source_run_id, target, version, created)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (skill_id, bot["id"], skill_name, run_id, target, count + 1, _now()),
                )
                self._db.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError("this run is already captured as a skill") from error
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bot_skills WHERE id=?", (skill_id,)
            ).fetchone()
        item = dict(row)
        item["bot_name"] = bot["name"]
        return item

    def delete_skill(self, skill_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM bot_skills WHERE id=?", (skill_id,))
            self._db.commit()
        if cur.rowcount <= 0:
            raise ValueError("skill not found")
        return True

    def _runs_today(self, bot_id: str) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM bot_runs WHERE bot_id=? AND substr(started_at,1,10)=?"
                " AND status != 'rejected' AND source != 'console'",
                (bot_id, today),
            ).fetchone()
        return int(row[0] if row else 0)

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
        if definition["id"] == "analyst":
            raise ValueError("analyst is manual-only; routines are for auditor/verifier")
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
                run = self.start_run(
                    routine["bot_id"], routine["target"], trigger="routine",
                    routine_id=routine["id"],
                )
                state = "pending" if run.get("status") == "pending" else "running"
                fired.append({"routine_id": routine["id"], "run_id": run["run_id"], "status": state})
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
        if self._swarm_thread is None or not self._swarm_thread.is_alive():
            self._swarm_thread = threading.Thread(
                target=self._swarm_loop, name="gov-swarm-scheduler", daemon=True,
            )
            self._swarm_thread.start()

    def _schedule_loop(self) -> None:
        while not self._stop.wait(20.0):
            try:
                self.fire_due()
            except Exception:  # noqa: BLE001 — scheduler loop never exits
                continue

    def _has_running(self, bot_id: str) -> bool:
        # A pending run is an unresolved operator decision, not an idle slot.
        # Counting only queued/running let every daily schedule enqueue another
        # pending approval while the previous one was still waiting, producing
        # an ever-growing approval queue that silently consumed the bot budget.
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM bot_runs WHERE bot_id=?"
                " AND status IN ('pending','queued','running') LIMIT 1",
                (bot_id,),
            ).fetchone()
        return row is not None

    def _mark_routine(self, routine_id: str, fired_at: str,
                      run_id: str, status: str) -> None:
        # The run thread may finish before this method runs. Never overwrite a
        # terminal state that already belongs to the same run.
        with self._lock:
            self._db.execute(
                "UPDATE bot_routines SET last_fired_at=?, last_run_id=?,"
                " last_status=CASE"
                "   WHEN last_run_id=? AND last_status IN ('done','failed','rejected')"
                "   THEN last_status ELSE ? END"
                " WHERE id=?",
                (fired_at, run_id, run_id, status, routine_id),
            )
            self._db.commit()

    # ----------------------------------------------------------------- swarms
    # 目标级 fan-out：一个蜂群把多个目标排成队列，宿主按并发上限启动
    # 审计子 run。子 run 仍写 bot_runs（source=swarm），因此预算、频道、
    # 审计台、回归都复用现有事实；蜂群只负责调度，不产生新的裁决。

    def _swarm_payload(self, row: sqlite3.Row,
                       children: list[sqlite3.Row] | None = None) -> dict:
        item = dict(row)
        if children is None:
            with self._lock:
                children = self._db.execute(
                    "SELECT * FROM bot_runs WHERE swarm_id=? ORDER BY rowid",
                    (item["id"],),
                ).fetchall()
        runs = [self._row(child) for child in children]
        counts = {key: 0 for key in
                  ("total", "queued", "running", "done", "failed", "rejected")}
        counts["total"] = len(runs)
        for run in runs:
            status = str(run.get("status") or "")
            if status in counts:
                counts[status] += 1
        counts["finished"] = counts["done"] + counts["failed"] + counts["rejected"]
        item["counts"] = counts
        item["runs"] = runs
        return item

    def get_swarm(self, swarm_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM bot_swarms WHERE id=?", (swarm_id,)
            ).fetchone()
        return self._swarm_payload(row) if row else None

    def swarms(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM bot_swarms ORDER BY created DESC, rowid DESC LIMIT ?",
                (max(1, min(int(limit), 50)),),
            ).fetchall()
        return [self._swarm_payload(row) for row in rows]

    def create_swarm(self, targets: Any, *, name: str = "",
                     max_concurrency: int = 2, target_md: str | None = None,
                     osv_snapshot: str | None = None) -> dict:
        raw = targets.splitlines() if isinstance(targets, str) else list(targets or [])
        unique: list[str] = []
        for value in raw:
            target = str(value or "").strip()
            if target and target not in unique:
                unique.append(target)
        if not unique:
            raise ValueError("swarm needs at least one target")
        if len(unique) > 20:
            raise ValueError("swarm supports at most 20 targets")
        try:
            concurrency = int(max_concurrency)
        except (TypeError, ValueError) as error:
            raise ValueError("max_concurrency must be 1..4") from error
        if not 1 <= concurrency <= 4:
            raise ValueError("max_concurrency must be 1..4")
        settings = self.get_settings("auditor")
        remaining = int(settings["daily_run_limit"]) - self._runs_today("auditor")
        if len(unique) > remaining:
            raise ValueError(
                f"审计员今日剩余预算 {remaining} 次，不足 {len(unique)} 个目标"
            )
        swarm_id = f"sw-{uuid.uuid4().hex[:12]}"
        label = str(name or "").strip()[:80] or f"蜂群 · {len(unique)} 个目标"
        with self._lock:
            self._db.execute(
                "INSERT INTO bot_swarms"
                " (id, name, bot_id, status, max_concurrency, target_md,"
                "  osv_snapshot, created) VALUES (?,?,?,?,?,?,?,?)",
                (swarm_id, label, "auditor", "running", concurrency,
                 target_md or None, osv_snapshot or None, _now()),
            )
            for target in unique:
                self._db.execute(
                    "INSERT INTO bot_runs"
                    " (run_id, bot_id, trigger, source, target, status,"
                    "  swarm_id, started_at) VALUES (?,?,?,?,?,?,?,?)",
                    (f"bot-{uuid.uuid4().hex[:12]}", "auditor", "manual",
                     "swarm", target, "queued", swarm_id, _now()),
                )
            self._db.commit()
        self._pump_swarms()
        return self.get_swarm(swarm_id) or {}

    def cancel_swarm(self, swarm_id: str) -> dict:
        swarm = self.get_swarm(swarm_id)
        if swarm is None:
            raise ValueError("swarm not found")
        if swarm.get("status") not in ("running", "queued"):
            raise ValueError("swarm is not running")
        with self._lock:
            self._db.execute(
                "UPDATE bot_swarms SET status='cancelled', finished_at=? WHERE id=?",
                (_now(), swarm_id),
            )
            self._db.execute(
                "UPDATE bot_runs SET status='rejected', error=?, finished_at=?"
                " WHERE swarm_id=? AND status='queued'",
                ("swarm cancelled", _now(), swarm_id),
            )
            self._db.commit()
        return self.get_swarm(swarm_id) or {}

    def retry_swarm(self, swarm_id: str) -> dict:
        swarm = self.get_swarm(swarm_id)
        if swarm is None:
            raise ValueError("swarm not found")
        retryable = [run for run in swarm.get("runs") or []
                     if run.get("status") in ("failed", "rejected")]
        if not retryable:
            raise ValueError("swarm has no failed targets to retry")
        with self._lock:
            self._db.execute(
                "UPDATE bot_runs SET status='queued', error=NULL, finished_at=NULL,"
                " started_at=? WHERE swarm_id=? AND status IN ('failed','rejected')",
                (_now(), swarm_id),
            )
            self._db.execute(
                "UPDATE bot_swarms SET status='running', finished_at=NULL WHERE id=?",
                (swarm_id,),
            )
            self._db.commit()
        self._pump_swarms()
        return self.get_swarm(swarm_id) or {}

    def _pump_swarms(self) -> None:
        """启动 queued 子 run；蜂群只调度，不改变子 run 的判定。"""
        with self._lock:
            swarms = self._db.execute(
                "SELECT * FROM bot_swarms WHERE status='running'"
            ).fetchall()
        for swarm in swarms:
            to_start: list[sqlite3.Row] = []
            with self._lock:
                children = self._db.execute(
                    "SELECT * FROM bot_runs WHERE swarm_id=? ORDER BY rowid",
                    (swarm["id"],),
                ).fetchall()
                running = sum(1 for row in children if row["status"] == "running")
                queued = [row for row in children if row["status"] == "queued"]
                if not queued and running == 0:
                    statuses = [str(row["status"]) for row in children]
                    done_count = sum(1 for status in statuses if status == "done")
                    if not statuses:
                        final = "failed"
                    elif done_count == len(statuses):
                        final = "done"
                    elif done_count == 0:
                        final = "failed"
                    else:
                        final = "partial"
                    self._db.execute(
                        "UPDATE bot_swarms SET status=?, finished_at=? WHERE id=?",
                        (final, _now(), swarm["id"]),
                    )
                    self._db.commit()
                    continue
                slots = max(0, int(swarm["max_concurrency"]) - running)
                for child in queued[:slots]:
                    cur = self._db.execute(
                        "UPDATE bot_runs SET status='running'"
                        " WHERE run_id=? AND status='queued'",
                        (child["run_id"],),
                    )
                    if cur.rowcount > 0:
                        to_start.append(child)
                self._db.commit()
            for child in to_start:
                try:
                    self._start_thread(
                        child["run_id"], "auditor", child["target"],
                        target_md=swarm["target_md"],
                        osv_snapshot=swarm["osv_snapshot"],
                    )
                except Exception as error:  # noqa: BLE001 — one child must not stop the swarm
                    self._finish(
                        child["run_id"], status="failed",
                        error=f"swarm start failed: {error}",
                    )

    def _swarm_loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self._pump_swarms()
            except Exception:  # noqa: BLE001 — pump loop never exits
                continue

    def _latest_audit_dir(self, purpose: str = "verify") -> str:
        """最近一次完成的审计员运行；没有运行记录时退回 bundle 列表。"""
        with self._lock:
            latest_run = self._db.execute(
                "SELECT audit_id FROM bot_runs"
                " WHERE bot_id='auditor' AND status='done'"
                " AND audit_id IS NOT NULL AND audit_id != ''"
                " ORDER BY finished_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        if latest_run is not None:
            return str(latest_run["audit_id"])
        bundles = (self.audit.bundles() or {}).get("bundles") or []
        if not bundles:
            raise ValueError(f"no completed audit to {purpose}")

        def audit_day(item: dict) -> str:
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(item.get("dir") or ""))
            return match.group(1) if match else ""

        # Date first; hash suffix breaks ties. Sorting by the full dir
        # would put `flowcredit-...` ahead of a newer `demo-target-...`.
        latest = max(
            bundles,
            key=lambda item: (audit_day(item), str(item.get("dir") or "")),
        )
        return str(latest.get("dir") or "")

    # ------------------------------------------------------------------ start

    def start_run(self, bot_id: str, target: str, trigger: str = "manual",
                  routine_id: str | None = None, *,
                  require_approval: bool = False,
                  parent_run_id: str | None = None,
                  source: str = "team",
                  target_md: str | None = None,
                  osv_snapshot: str | None = None) -> dict:
        definition = _BY_ID.get(str(bot_id or ""))
        if definition is None:
            raise ValueError(f"unknown bot: {bot_id!r}")
        resolved = str(target or "").strip()
        if definition["id"] == "auditor" and not resolved:
            raise ValueError("auditor needs a target path")
        if definition["id"] in ("verifier", "analyst") and not resolved:
            resolved = self._latest_audit_dir(
                "verify" if definition["id"] == "verifier" else "analyze")
        trigger = trigger if trigger in ("manual", "routine") else "manual"
        settings = self.get_settings(definition["id"])
        if self._runs_today(definition["id"]) >= settings["daily_run_limit"]:
            raise ValueError(
                f"{definition['name']} 今日运行预算已用完 "
                f"({settings['daily_run_limit']} 次)"
            )
        needs_approval = require_approval or (
            bool(settings["routine_approval"]) and trigger == "routine")
        status = "pending" if needs_approval else "queued"
        run_id = f"bot-{uuid.uuid4().hex[:12]}"
        source = source if source in ("team", "console", "swarm") else "team"
        with self._lock:
            self._db.execute(
                "INSERT INTO bot_runs"
                " (run_id, bot_id, trigger, source, target, status, parent_run_id, started_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, definition["id"], trigger, source, resolved, status,
                 parent_run_id, _now()),
            )
            self._db.commit()
        if routine_id:
            # Link the routine to its run before the worker starts. Otherwise
            # a fast run can finish before the routine knows its last_run_id.
            self._mark_routine(
                routine_id, _now(), run_id, status,
            )
        if not needs_approval:
            self._start_thread(run_id, definition["id"], resolved,
                               target_md=target_md, osv_snapshot=osv_snapshot)
        return self.get_run(run_id) or {}

    def start_manual_run(self, target: str, *, target_md: str | None = None,
                         osv_snapshot: str | None = None) -> dict:
        """治理对话的 `/挖洞`：写入同一条运行流，但不占 Bot 预算、不走审批。

        手动挖洞是操作员的即时调查，和常驻 Bot 的预算/审批语义分开：
        - 持久化进 bot_runs，source=console，刷新/重启后仍在频道里；
        - 不计入 `_runs_today`，也不会因为每日上限被拒绝；
        - 立即执行，不产生 pending，也不触发 triage（避免隐式模型调用）。
        """
        resolved = str(target or "").strip()
        if not resolved:
            raise ValueError("auditor needs a target path")
        # 先让审计服务做目标校验并启动作业；非法目标在 HTTP 层直接 400，
        # 不会留下一条注定失败的运行。
        job_id = self.audit.start_run(resolved, target_md=target_md,
                                      osv_snapshot=osv_snapshot)
        run_id = f"gov-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._db.execute(
                "INSERT INTO bot_runs"
                " (run_id, bot_id, trigger, source, target, status, job_id, started_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "auditor", "manual", "console", resolved,
                 "queued", job_id, _now()),
            )
            self._db.commit()
        self._start_thread(run_id, "auditor", resolved, job_id=job_id,
                           target_md=target_md, osv_snapshot=osv_snapshot)
        return self.get_run(run_id) or {}

    def approve_run(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("run not found")
        if run.get("status") != "pending":
            raise ValueError("run is not waiting for approval")
        bot_id = str(run.get("bot_id") or "")
        target = str(run.get("target") or "")
        self._update(run_id, status="queued")
        self._start_thread(run_id, bot_id, target)
        return self.get_run(run_id) or {}

    def reject_run(self, run_id: str) -> dict:
        run = self.get_run(run_id)
        if run is None:
            raise ValueError("run not found")
        if run.get("status") != "pending":
            raise ValueError("run is not waiting for approval")
        self._finish(run_id, status="rejected", error="operator rejected")
        return self.get_run(run_id) or {}

    def _start_thread(self, run_id: str, bot_id: str, target: str,
                      *, job_id: str | None = None,
                      target_md: str | None = None,
                      osv_snapshot: str | None = None) -> None:
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, bot_id, target, job_id, target_md, osv_snapshot),
            name=f"gov-bot-{run_id}",
            daemon=True,
        )
        self._threads[run_id] = thread
        thread.start()

    # --------------------------------------------------------------- internal

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """旧库就地加列；已存在则跳过（SQLite 无 IF NOT EXISTS for columns）。"""
        existing = {
            row["name"] for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name in existing:
                continue
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        self._db.commit()

    def _execute(self, run_id: str, bot_id: str, target: str,
                 job_id: str | None = None,
                 target_md: str | None = None,
                 osv_snapshot: str | None = None) -> None:
        self._update(run_id, status="running")
        try:
            if bot_id == "auditor":
                self._run_auditor(run_id, target, job_id=job_id,
                                  target_md=target_md, osv_snapshot=osv_snapshot)
            elif bot_id == "verifier":
                self._run_verifier(run_id, target)
            elif bot_id == "analyst":
                self._run_analyst(run_id, target)
            else:
                self._finish(run_id, status="failed", error=f"unsupported bot {bot_id}")
        except Exception as error:  # noqa: BLE001 — one run must not kill the server
            self._finish(run_id, status="failed", error=f"{type(error).__name__}: {error}")
        finally:
            self._threads.pop(run_id, None)

    def _run_auditor(self, run_id: str, target: str,
                     *, job_id: str | None = None,
                     target_md: str | None = None,
                     osv_snapshot: str | None = None) -> None:
        # P1：URL 靶子治理化——宿主先做快照（robots 检查/同源枚举/响应头事实/
        # 溯源入链），再审计快照目录。快照失败如实 failed，不起半成品审计。
        snapshot_dir: str | None = None
        if str(target).startswith(("http://", "https://")):
            try:
                from web_snapshot import snapshot_url

                parsed_host = urlsplit(target).netloc.replace(":", "-")
                snapshot_path = Path(self.audit.repo) / "workspace-general" / "web" / parsed_host
                provenance = snapshot_url(target, snapshot_path)
                snapshot_dir = str(snapshot_path)
                target = snapshot_dir
                # target_md 的契约是"仓库相对的描述文件路径"——PROVENANCE.md
                # 就是治理化快照的 target descriptor（随 audit job 入链）；
                # 传内联文本会撞 "target descriptor not found"（实测踩过）。
                target_md = f"workspace-general/web/{parsed_host}/PROVENANCE.md"
                self._update(run_id, target=target)
            except Exception as error:  # noqa: BLE001 — 快照失败是审计失败的事实
                self._finish(run_id, status="failed",
                             error=f"snapshot failed: {type(error).__name__}: {error}"[:200])
                return
        job_id = job_id or self.audit.start_run(
            target, target_md=target_md, osv_snapshot=osv_snapshot)
        self._update(run_id, job_id=job_id)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            job = self.audit.job_status(job_id) or {}
            status = str(job.get("status") or "")
            if status == "done":
                audit_id = str(job.get("audit_id") or "")
                self._finish(
                    run_id,
                    status="done",
                    audit_id=audit_id or None,
                    findings=int(job.get("findings") or 0),
                    evidence=int(job.get("evidence") or 0),
                )
                # 静止加密（opt-in）：审计完成后目标内容加密归档、明文删除；
                # 审计包/证据元数据保持可读——验证不需要解密全文。
                if snapshot_dir:
                    try:
                        from audit_privacy import encrypt_dir, encryption_enabled, prune_archives

                        if encryption_enabled():
                            from pathlib import Path as _SnapshotPath

                            site = _SnapshotPath(snapshot_dir).name
                            encrypt_dir(
                                snapshot_dir,
                                archive_name=f"{site}-{audit_id}.tar.enc",
                            )
                            prune_archives(snapshot_dir, keep=5)
                    except Exception as error:  # noqa: BLE001 — 加密失败要可见
                        self._update(run_id, error=f"encrypt degraded: {error}"[:160])
                # 外部刀（第二证据面）：只对快照类靶子跑，结果落审计目录旁
                # （external-scanners.json，不进 bundle 哈希）；缺刀如实降级。
                if snapshot_dir:
                    try:
                        from web_scanners import run_scanners

                        results = run_scanners(snapshot_dir)
                        results["snapshot_dir"] = snapshot_dir
                        audit_dir = Path(getattr(self.audit, "root") or "") / str(audit_id)
                        audit_dir.mkdir(parents=True, exist_ok=True)
                        (audit_dir / "external-scanners.json").write_text(
                            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
                    except Exception as error:  # noqa: BLE001 — 刀失败如实降级
                        self._update(run_id, error=f"external scanners degraded: {error}"[:160])
                # 审计已完成才做 triage：它是完成后的跟进提议，不阻塞审计终态。
                self._after_audit(run_id, audit_id)
                return
            if status == "failed":
                self._finish(run_id, status="failed", error=str(job.get("error") or "audit failed"))
                return
            time.sleep(0.5)
        self._finish(run_id, status="failed", error="audit timed out after 900s")

    def _after_audit(self, run_id: str, audit_id: str) -> None:
        """受控 triage：模型只提议优先级；active 只生成待批准复扫。"""
        run = self.get_run(run_id) or {}
        # triage 自己发起的复扫不再触发 triage（避免递归）；对话手动挖洞
        # 是即时调查，也不隐式调用模型。
        if run.get("parent_run_id") or run.get("source") == "console" or not audit_id:
            return
        mode = str(self.get_settings("auditor").get("triage_mode") or "off")
        if mode not in ("shadow", "active"):
            return
        self._update(run_id, triage_mode=mode, triage_status="pending")
        try:
            from gov_triage import GovTriage

            bundle = self.audit.bundle(audit_id)
            if isinstance(bundle, tuple):
                payload, status = bundle
                raise ValueError(f"bundle unavailable ({status}): {payload}")
            plan = GovTriage(provider_factory=self._provider_factory).execute(bundle)
            plan["mode"] = mode
            self._update(
                run_id,
                triage_status="done",
                triage_json=json.dumps(plan, ensure_ascii=False),
            )
        except Exception as error:  # noqa: BLE001 — triage 失败不影响审计事实
            self._update(
                run_id,
                triage_status="error",
                triage_error=f"{type(error).__name__}: {error}"[:300],
            )
            return
        if mode != "active" or not plan.get("priorities"):
            return
        # active 的唯一效果：按建议生成一条“待操作员批准”的完整复扫。
        try:
            child = self.start_run(
                "auditor", str(run.get("target") or ""), trigger="manual",
                require_approval=True, parent_run_id=run_id,
            )
            self._update(run_id, triage_run_id=str(child.get("run_id") or ""))
        except Exception as error:  # noqa: BLE001 — 预算/参数问题只是不提议
            self._update(run_id, triage_error=f"reaudit spawn: {error}"[:300])

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

    def _run_analyst(self, run_id: str, audit_dir: str) -> None:
        """只读分析：对已完成审计给出优先级，不产生 finding、不改 bundle。"""
        try:
            from gov_triage import GovTriage

            bundle = self.audit.bundle(audit_dir)
            if isinstance(bundle, tuple):
                payload, status = bundle
                raise ValueError(f"bundle unavailable ({status}): {payload}")
            plan = GovTriage(provider_factory=self._provider_factory).execute(bundle)
            plan["mode"] = "manual"
        except Exception as error:  # noqa: BLE001 — 分析失败要可见，但不改证据
            self._update(
                run_id,
                triage_status="error",
                triage_error=f"{type(error).__name__}: {error}"[:300],
            )
            raise
        self._finish(
            run_id,
            status="done",
            audit_id=str(bundle.get("audit_id") or audit_dir),
            triage_mode="manual",
            triage_status="done",
            triage_json=json.dumps(plan, ensure_ascii=False),
        )

    def _update(self, run_id: str, **fields: Any) -> None:
        self._write(run_id, fields, finished=False)

    def _finish(self, run_id: str, *, status: str, **fields: Any) -> None:
        # Keep the routine's last status in sync with the run it fired, so a
        # rejected/failed routine does not keep showing the old "pending".
        # Update it before the run row: a waiter that sees the run terminal
        # must also see the routine terminal.
        with self._lock:
            self._db.execute(
                "UPDATE bot_routines SET last_status=? WHERE last_run_id=?",
                (status, run_id),
            )
            self._db.commit()
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
        triage_raw = item.pop("triage_json", None)
        item["triage"] = {}
        if triage_raw:
            try:
                item["triage"] = json.loads(triage_raw)
            except ValueError:
                item["triage"] = {}
        return item
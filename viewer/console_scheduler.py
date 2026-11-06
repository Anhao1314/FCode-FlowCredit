"""定时任务调度器：把「定时任务」页的草稿变成到点自动创建的运行。

语义（与页面下拉一致）：
  每天    每日 HH:MM 触发
  工作日  周一至周五 HH:MM 触发
  每周    每周与创建日相同的星期几 HH:MM 触发

治理不变：到点创建的运行与手创运行同一条流水线（同一门控、预算、
收据、签名链）；审批档位由排程条目自带。排程存放在本机服务
（sqlite，runs 目录旁），重启后仍在。

到期判定是纯函数（next_due / is_due），可在 Node 之外的测试里直接
覆盖；fire 经 create_run_fn 回调注入，单元测试用桩替换。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

FREQUENCIES = ("每天", "工作日", "每周")
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    template TEXT NOT NULL DEFAULT 'full',
    approval TEXT NOT NULL DEFAULT 'consequential',
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


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = str(value or "").split(":")
    if len(parts) != 2:
        raise ValueError("时间必须是 HH:MM")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("时间必须是 HH:MM")
    return hour, minute


class SchedulerStore:
    """排程条目的 sqlite 存储。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def upsert(self, entry: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            entry_id = str(entry.get("id") or uuid.uuid4().hex[:16])
            existing = self.get(entry_id)
            now = _now_text()
            if existing is None:
                frequency = entry.get("frequency") or "每天"
                weekday = entry.get("weekday")
                if frequency == "每周" and weekday is None:
                    weekday = now.weekday()  # 每周默认与创建日相同的星期几
                self._db.execute(
                    "INSERT INTO schedules (id, objective, template, approval, frequency,"
                    " time, weekday, enabled, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (entry_id, entry["objective"], entry.get("template") or "full",
                     entry.get("approval") or "consequential",
                     frequency, entry.get("time") or "09:00",
                     int(weekday) if weekday is not None else None,
                     1 if entry.get("enabled") is None else int(bool(entry["enabled"])), now),
                )
            else:
                self._db.execute(
                    "UPDATE schedules SET objective=?, template=?, approval=?,"
                    " frequency=?, time=? WHERE id=?",
                    (entry["objective"], entry.get("template") or existing["template"],
                     entry.get("approval") or existing["approval"],
                     entry.get("frequency") or existing["frequency"],
                     entry.get("time") or existing["time"], entry_id),
                )
            self._db.commit()
            return self.get(entry_id)  # type: ignore[return-value]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM schedules WHERE id=?", (entry_id,)).fetchone()
            return dict(row) if row is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM schedules ORDER BY created DESC, rowid DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM schedules WHERE id=?", (entry_id,))
            self._db.commit()
            return cur.rowcount > 0

    def set_enabled(self, entry_id: str, enabled: bool) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE schedules SET enabled=? WHERE id=?", (1 if enabled else 0, entry_id)
            )
            self._db.commit()
            return cur.rowcount > 0

    def mark_fired(self, entry_id: str, *, run_id: str, status: str, fired_at: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE schedules SET last_fired_at=?, last_run_id=?, last_status=? WHERE id=?",
                (fired_at, run_id, status, entry_id),
            )
            self._db.commit()


def is_due(entry: dict[str, Any], now: datetime) -> bool:
    """到期判定（纯函数）：今天该触发的时间点已过、且今天尚未触发过。"""
    if not entry.get("enabled"):
        return False
    try:
        hour, minute = parse_hhmm(entry.get("time"))
    except ValueError:
        return False
    frequency = entry.get("frequency") or "每天"
    if frequency == "每周":
        weekday = entry.get("weekday")
        if weekday is None or int(weekday) != now.weekday():
            return False
    elif frequency == "工作日":
        if now.weekday() >= 5:
            return False
    due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < due_at:
        return False
    last = entry.get("last_fired_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(str(last))
            if last_dt >= due_at:
                return False
        except ValueError:
            pass
    return True


def describe_next(entry: dict[str, Any], now: datetime) -> str:
    """给 UI 的一句话“下次触发”描述（纯展示，不做复杂推算）。"""
    if not entry.get("enabled"):
        return "已暂停"
    if is_due(entry, now):
        return "待触发（时间已到，等待调度器下一轮）"
    frequency = entry.get("frequency") or "每天"
    try:
        hour, minute = parse_hhmm(entry.get("time"))
    except ValueError:
        return "时间格式无效"
    when = f"{hour:02d}:{minute:02d}"
    if frequency == "每周":
        weekday = entry.get("weekday")
        name = WEEKDAY_NAMES[int(weekday)] if weekday is not None else "周?"
        return f"每周{name} {when}"
    if frequency == "工作日":
        return f"工作日 {when}（周末跳过）"
    return f"每天 {when}"


class Scheduler:
    """后台循环：每 tick 检查一次到期条目并触发。

    create_run_fn(payload: dict) 由调用方注入（生产环境是 mgr.create 的
    适配，测试用桩）；触发失败只记录状态，绝不打断循环。
    """

    def __init__(self, store: SchedulerStore, create_run_fn: Callable[[dict[str, Any]], str],
                 tick_seconds: float = 20.0) -> None:
        self.store = store
        self._create_run = create_run_fn
        self._tick_seconds = max(5.0, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fire_lock = threading.Lock()

    def fire_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """单轮检查：触发所有到期条目，返回触发结果。可从测试直接调用。"""
        now = now or datetime.now()
        fired: list[dict[str, Any]] = []
        with self._fire_lock:
            for entry in self.store.list():
                if not is_due(entry, now):
                    continue
                payload = {
                    "objective": entry["objective"],
                    "template": entry["template"],
                    "approval": entry["approval"],
                }
                fired_at = now.strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    run = self._create_run(payload)
                    run_id = str(run)
                    status = "running"
                except Exception as error:  # noqa: BLE001 — 单次失败不打断调度
                    run_id = ""
                    status = f"error: {error}"
                self.store.mark_fired(entry["id"], run_id=run_id, status=status, fired_at=fired_at)
                fired.append({"id": entry["id"], "run_id": run_id, "status": status})
        return fired

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            try:
                self.fire_due()
            except Exception:  # noqa: BLE001 — 调度循环永不退出
                continue

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="fcode-scheduler")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def validate_schedule_payload(body: dict[str, Any]) -> dict[str, Any]:
    """HTTP 层的输入校验：返回干净字段或抛 ValueError。"""
    objective = str(body.get("objective") or "").strip()
    if not objective:
        raise ValueError("先描述定时任务的目标")
    if len(objective) > 600:
        raise ValueError("目标最长 600 字")
    template = str(body.get("template") or "full")
    approval = str(body.get("approval") or "consequential")
    if approval not in ("off", "consequential", "all"):
        raise ValueError("approval must be off, consequential or all")
    frequency = str(body.get("frequency") or "每天")
    if frequency not in FREQUENCIES:
        raise ValueError(f"频率必须是 {'/'.join(FREQUENCIES)}")
    time_text = str(body.get("time") or "09:00")
    parse_hhmm(time_text)
    return {
        "id": body.get("id"),
        "objective": objective,
        "template": template,
        "approval": approval,
        "frequency": frequency,
        "time": time_text,
    }

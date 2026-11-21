"""定时任务调度器：存储、到期判定（纯函数）、输入校验与触发回路。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

from console_scheduler import (  # noqa: E402
    Scheduler,
    SchedulerStore,
    is_due,
    validate_schedule_payload,
)


def entry(**overrides) -> dict:
    base = {
        "id": "s1", "objective": "每天核对口径", "template": "full",
        "approval": "consequential", "frequency": "每天", "time": "09:00",
        "weekday": None, "enabled": 1, "last_fired_at": None,
        "last_run_id": "", "last_status": "", "created": "2026-11-06T08:00:00",
    }
    base.update(overrides)
    return base


class IsDueTest(unittest.TestCase):
    def test_daily_before_and_after_time(self) -> None:
        e = entry(time="09:00")
        self.assertFalse(is_due(e, datetime(2026, 11, 6, 8, 59)), "时间未到不触发")
        self.assertTrue(is_due(e, datetime(2026, 11, 6, 9, 0)), "到点触发")
        self.assertTrue(is_due(e, datetime(2026, 11, 6, 14, 0)), "错过也补触发")

    def test_daily_not_refired_same_day(self) -> None:
        e = entry(time="09:00", last_fired_at="2026-11-06T09:00:05")
        self.assertFalse(is_due(e, datetime(2026, 11, 6, 10, 0)), "当天已触发不再触发")
        self.assertTrue(is_due(e, datetime(2026, 11, 7, 9, 0)), "次日到点再触发")

    def test_weekday_skips_weekend(self) -> None:
        e = entry(frequency="工作日", time="09:00")
        self.assertTrue(is_due(e, datetime(2026, 11, 6, 9, 0)), "周五触发")       # 周五
        self.assertFalse(is_due(e, datetime(2026, 11, 7, 9, 0)), "周六跳过")      # 周六
        self.assertFalse(is_due(e, datetime(2026, 11, 8, 9, 0)), "周日跳过")      # 周日

    def test_weekly_matches_creation_weekday_only(self) -> None:
        e = entry(frequency="每周", time="09:00", weekday=2)  # 周三(Python 周一=0)
        self.assertTrue(is_due(e, datetime(2026, 11, 4, 9, 0)), "周三触发")       # 周三
        self.assertFalse(is_due(e, datetime(2026, 11, 5, 9, 0)), "周四不触发")    # 周四

    def test_disabled_and_bad_time_never_due(self) -> None:
        self.assertFalse(is_due(entry(enabled=0), datetime(2026, 11, 6, 10, 0)))
        self.assertFalse(is_due(entry(time="25:99"), datetime(2026, 11, 6, 10, 0)))


class SchedulerLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SchedulerStore(Path(self._tmp.name) / "scheduler.sqlite")
        self.fired: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scheduler(self, fail: bool = False) -> Scheduler:
        def create_run(payload: dict) -> str:
            if fail:
                raise RuntimeError("runtime down")
            self.fired.append(payload)
            return f"run-{len(self.fired):03d}"

        return Scheduler(self.store, create_run)

    def test_tick_fires_due_and_marks(self) -> None:
        self.store.upsert(entry(time="09:00"))
        sched = self._scheduler()
        now = datetime(2026, 11, 6, 9, 30)
        fired = sched.fire_due(now)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["status"], "running")
        self.assertEqual(self.fired[0]["objective"], "每天核对口径")
        self.assertEqual((self.fired[0]["model"], self.fired[0]["effort"]), ("mock", ""))
        saved = self.store.list()[0]
        self.assertEqual(saved["last_run_id"], "run-001")
        self.assertEqual(saved["last_status"], "running")
        # 同一轮再查不再触发
        self.assertEqual(sched.fire_due(now), [])

    def test_tick_records_failure_without_stopping(self) -> None:
        self.store.upsert(entry(time="09:00"))
        self.store.upsert({**entry(id="s2", time="09:00", objective="第二条")})
        sched = self._scheduler(fail=True)
        fired = sched.fire_due(datetime(2026, 11, 6, 9, 30))
        self.assertEqual(len(fired), 2)
        statuses = [f["status"] for f in fired]
        self.assertTrue(all(s.startswith("error:") for s in statuses), "失败如实记录")
        # 修好后下一轮继续触发
        sched2 = self._scheduler()
        self.assertEqual(len(sched2.fire_due(datetime(2026, 11, 6, 9, 40))), 0,
                         "同一天已标记触发,即便失败也不重复发起")

    def test_disabled_entry_skipped(self) -> None:
        self.store.upsert(entry(time="09:00", enabled=0))
        sched = self._scheduler()
        self.assertEqual(sched.fire_due(datetime(2026, 11, 6, 9, 30)), [])

    def test_weekly_fires_only_on_weekday(self) -> None:
        self.store.upsert(entry(frequency="每周", time="09:00", weekday=2))
        sched = self._scheduler()
        self.assertEqual(sched.fire_due(datetime(2026, 11, 5, 9, 0)), [])   # 周四
        self.assertEqual(len(sched.fire_due(datetime(2026, 11, 4, 9, 0))), 1)  # 周三


class StoreAndValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SchedulerStore(Path(self._tmp.name) / "scheduler.sqlite")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_upsert_create_then_update(self) -> None:
        created = self.store.upsert(entry(model="kimi", effort="high"))
        self.assertEqual(created["objective"], "每天核对口径")
        self.assertEqual((created["model"], created["effort"]), ("kimi", "high"))
        updated = self.store.upsert({**entry(id=created["id"]), "objective": "改目标", "time": "08:00"})
        self.assertEqual(updated["objective"], "改目标")
        self.assertEqual(updated["time"], "08:00")
        self.assertEqual((updated["model"], updated["effort"]), ("kimi", "high"))
        self.assertEqual(len(self.store.list()), 1, "upsert 不新增行")

    def test_delete_and_toggle(self) -> None:
        created = self.store.upsert(entry())
        self.assertTrue(self.store.set_enabled(created["id"], False))
        self.assertEqual(self.store.list()[0]["enabled"], 0)
        self.assertTrue(self.store.delete(created["id"]))
        self.assertFalse(self.store.delete(created["id"]))

    def test_validate_payload(self) -> None:
        clean = validate_schedule_payload({"objective": "核对", "frequency": "工作日", "time": "08:30"})
        self.assertEqual(clean["time"], "08:30")
        with self.assertRaises(ValueError):
            validate_schedule_payload({"objective": "  "})
        with self.assertRaises(ValueError):
            validate_schedule_payload({"objective": "x", "approval": "sometimes"})
        with self.assertRaises(ValueError):
            validate_schedule_payload({"objective": "x", "time": "9点"})
        with self.assertRaises(ValueError):
            validate_schedule_payload({"objective": "x", "frequency": "每年"})


if __name__ == "__main__":
    unittest.main()


class RunSourceTest(unittest.TestCase):
    """运行来源字段:调度器/子代理触发的运行在账本可见性上如实标注。"""

    def test_run_default_source_is_manual(self) -> None:
        from console_runtime import Run

        run = Run(id="r", template="full", objective="x", created="t", pace=0)
        self.assertEqual(run.source, "manual")
        self.assertEqual(run.to_json()["source"], "manual")

    def test_run_source_values_round_trip(self) -> None:
        from console_runtime import Run

        for source in ("scheduled", "subagent"):
            run = Run(id="r", template="full", objective="x", created="t",
                      pace=0, source=source)
            self.assertEqual(run.to_json()["source"], source)

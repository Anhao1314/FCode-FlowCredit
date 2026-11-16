"""HTTP surface for scheduled delegations: the model snapshot contract.

Slice B acceptance: a schedule persists the model/effort chosen at save time;
a later change of the shared default must not silently rewrite it; an unknown
or mismatched model is refused before anything is stored. The seeding goes
through the real settings endpoint, so the catalog the scheduler resolves
against is the same one the settings page writes.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from tests.server_harness import ConsoleServer, request


class ScheduleModelSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = pathlib.Path(cls._tmp.name)
        cls.server = ConsoleServer(
            runs_dir=cls.root / "runs",
            config_dir=cls.root / "config",
            timeout=30,
        )
        cls.server.start()
        cls.addClassCleanup(cls.server.stop)
        cls.addClassCleanup(cls._tmp.cleanup)

    @classmethod
    def _post(cls, path: str, payload: dict) -> tuple[int, str]:
        return request(
            cls.server.url(path),
            method="POST",
            body=json.dumps(payload).encode("utf-8"),
        )

    @classmethod
    def setUp(cls) -> None:
        # Seed one custom endpoint through the real settings write, so the
        # scheduler resolves against exactly what the settings page produces.
        status, body = cls._post("/api/settings/model-config", {
            "provider": "myep",
            "values": {
                "label": "测试端点",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "test-model",
                "input_price_microusd_per_mtok": "0",
                "output_price_microusd_per_mtok": "0",
                "price_as_of": "not-applicable",
                "price_source": "operator-declared",
                "supports_effort": "1",
            },
        })
        assert status == 200, body

    def _create(self, **extra) -> tuple[int, str]:
        payload = {
            "objective": "每天核对口径",
            "template": "full",
            "approval": "off",
            "frequency": "每天",
            "time": "09:00",
        }
        payload.update(extra)
        return self._post("/api/schedules", payload)

    def test_a_saved_model_snapshot_survives_list_and_update(self) -> None:
        status, body = self._create(model="myep", effort="high")
        self.assertEqual(status, 200, body)
        entry = json.loads(body)["schedule"]
        self.assertEqual((entry["model"], entry["effort"]), ("myep", "high"))

        status, body = request(self.server.url("/api/schedules"))
        self.assertEqual(status, 200, body)
        listed = next(item for item in json.loads(body)["schedules"]
                      if item["objective"] == "每天核对口径")
        self.assertEqual((listed["model"], listed["effort"]), ("myep", "high"))

        # 更新别的字段、不带 model：已固定的快照不被清掉或改写
        status, body = self._post("/api/schedules", {
            "id": listed["id"], "objective": "每天核对口径（改名）",
            "template": "full", "approval": "off",
            "frequency": "每天", "time": "09:30",
        })
        self.assertEqual(status, 200, body)
        updated = json.loads(body)["schedule"]
        self.assertEqual((updated["model"], updated["effort"]), ("myep", "high"))

    def test_an_unknown_model_is_refused_before_anything_is_stored(self) -> None:
        _, before = request(self.server.url("/api/schedules"))
        count_before = len(json.loads(before)["schedules"])
        status, body = self._create(objective="坏模型排程", model="nope")
        self.assertEqual(status, 400, body)
        self.assertIn("未知模型", json.loads(body)["error"])
        _, after = request(self.server.url("/api/schedules"))
        self.assertEqual(len(json.loads(after)["schedules"]), count_before)

    def test_mock_with_effort_is_refused(self) -> None:
        status, body = self._create(objective="mock 带档位", model="mock", effort="high")
        self.assertEqual(status, 400, body)
        self.assertIn("mock 模型不支持思考强度", json.loads(body)["error"])

    def test_a_schedule_created_without_a_model_keeps_the_mock_semantics(self) -> None:
        """迁移/未指定的排程落 mock——旧行为被钉死，UI 显式发模型才变化。"""
        status, body = self._create(objective="未指定模型的排程")
        self.assertEqual(status, 200, body)
        entry = json.loads(body)["schedule"]
        self.assertEqual((entry["model"], entry["effort"]), ("mock", ""))


if __name__ == "__main__":
    unittest.main()

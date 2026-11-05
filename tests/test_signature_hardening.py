"""P1-2 加固验收:签名内容快照 + records_version。

T1 summarizer_config 原地改 value(不换对象)必须触发签名变化与行重建;
T2 追加记录改变 records_version,但只增量构造新行,不重建历史行;
T2b 记录列表缩短时必须整体重建,不得沿用已删除记录的行。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys_path = str(ROOT / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from flowagent.memory.context import WorkingMemory  # noqa: E402


def _observation(round_no: int) -> object:
    return type("O", (), {
        "tool": "probe",
        "arguments": {"k": round_no},
        "result": {"x": round_no},
        "result_hash": f"{round_no:064d}",
    })()


class SignatureContentTests(unittest.TestCase):
    def test_t1_config_content_change_rebuilds_rows(self) -> None:
        calls = {"n": 0}

        def counting(result):
            calls["n"] += 1
            return {"n": calls["n"], "x": result.get("x")}

        memory = WorkingMemory(
            objective="t", tool_catalog=[],
            summarizers={"probe": counting},
            summarizer_config={"probe": ("custom", 1)},
        )
        memory.record(1, _observation(1))
        config_id = id(memory.summarizer_config)

        first = memory.build(1, {})
        self.assertEqual(calls["n"], 1)
        first_signature = memory._prepared_signature
        first_full = memory._prepared[0]["full"]
        self.assertEqual(first["observations"][0]["summary"], {"n": 1, "x": 1})

        # 原地改 value,不替换 dict 对象:旧签名按 id 不会变化。
        memory.summarizer_config["probe"] = ("custom", 2)
        self.assertEqual(id(memory.summarizer_config), config_id)

        second = memory.build(2, {})
        self.assertNotEqual(memory._prepared_signature, first_signature,
                            "配置内容变化必须改变签名")
        self.assertIsNot(memory._prepared[0]["full"], first_full,
                         "配置内容变化必须重建行产物")
        self.assertEqual(calls["n"], 2, "新版本必须重新调用 summarizer")
        self.assertEqual(second["observations"][0]["summary"],
                         {"n": 2, "x": 1}, "行内容必须按新配置重建")
        self.assertEqual(first["observations"][0]["summary"],
                         {"n": 1, "x": 1}, "旧上下文快照不得被回写")

    def test_t2_records_version_changes_but_append_keeps_history(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[])
        memory.record(1, _observation(1))
        memory.build(1, {})
        first_signature = memory._prepared_signature
        first_full = memory._prepared[0]["full"]

        # 不追加:签名不变,行不重建。
        memory.build(2, {})
        self.assertEqual(memory._prepared_signature, first_signature)
        self.assertIs(memory._prepared[0]["full"], first_full)

        # 追加:版本递增、签名变化、新行被构造,但历史行不重建。
        memory.record(2, _observation(2))
        self.assertEqual(memory._records_version, 2)
        memory.build(3, {})
        self.assertNotEqual(memory._prepared_signature, first_signature)
        self.assertEqual(len(memory._prepared), 2)
        self.assertIs(memory._prepared[0]["full"], first_full,
                      "追加不得重建历史行(P1-2 增量性)")
        self.assertEqual([row["full"]["round"] for row in memory._prepared], [1, 2])

    def test_t2b_records_shrink_triggers_rebuild(self) -> None:
        memory = WorkingMemory(objective="t", tool_catalog=[])
        memory.record(1, _observation(1))
        memory.record(2, _observation(2))
        memory.build(1, {})
        self.assertEqual(len(memory._prepared), 2)

        memory.records.pop()
        context = memory.build(2, {})
        self.assertEqual(len(memory._prepared), 1,
                         "记录缩短必须重建,不得保留已删除记录的行")
        self.assertEqual([row["full"]["round"] for row in memory._prepared], [1])
        self.assertEqual(len(context["observations"]), 1)


if __name__ == "__main__":
    unittest.main()

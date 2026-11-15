#!/usr/bin/env python3
"""修洞验证：chat ↔ chat_answer 的 import 环必须消失，且两模块可导入。

在修复运行的隔离副本里由 Runtime 独立复跑（cwd = 仓库副本根）。
1) 用 flowaudit 的 VIBE 扫描器检查 src/ 的 import 环——目标环仍在则失败；
2) 导入冒烟：flowagent.chat / flowagent.chat_answer 能正常 import。
退出码 0 = 通过；1 = 环仍在；2 = 验证环境异常。
"""

from __future__ import annotations

import importlib
import pathlib
import sys


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    try:
        from flowaudit.rea.vibe import _python_import_cycles
    except Exception as error:  # noqa: BLE001 — 验证环境异常要如实报出
        print(f"verify: cannot load flowaudit scanner: {error}")
        return 2

    cycles = _python_import_cycles(root / "src")
    target = [c for c in cycles
              if "flowagent/chat.py" in c.get("cycle", [])
              and "flowagent/chat_answer.py" in c.get("cycle", [])]
    if target:
        print(f"FAIL: import cycle still present: {target[0]['cycle']}")
        return 1

    for name in ("flowagent.chat", "flowagent.chat_answer"):
        importlib.import_module(name)
    print("OK: chat<->chat_answer cycle is gone; both modules import cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())

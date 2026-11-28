"""Versioned Work coding tasks with hidden, host-held tests.

This taskset measures the *Work coding loop*, not governance mechanics:

* every workspace starts as a small repository with a failing public check;
* the model-visible taskset carries the initial files and the objective, but
  never the hidden test bytes;
* hidden tests live only in this module and are materialised by the evaluator
  outside the run workspace;
* the scripted reference proposals must drive the production Work runtime to
  the hidden-verified end state, which validates the benchmark itself;
* verifier success is host-computed from hidden tests, workspace facts and
  forbidden-action scans — never from the model's claim.

Changing a task changes its SHA-256 and requires a changelog entry.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from ..core.hashing import digest

BENCHMARK_VERSION = "work-coding-benchmark/v1"

WORK_CODING_CHANGELOG: tuple[dict[str, str], ...] = (
    {
        "version": BENCHMARK_VERSION,
        "change": "initial 12-task set: single-file, multi-file, regression, refactor, frontend and test-repair",
    },
)

_DEFAULT_BUDGET = {
    "max_calls": 12,
    "max_reads": 30,
    "max_cost_microusd": 120_000,
    "max_tokens": 80_000,
}
_READ_WRITE = ["read", "write"]


def _ctask(
    task_id: str,
    category: str,
    difficulty: str,
    objective: str,
    *,
    fixture: dict[str, str],
    actions: list[dict[str, Any]],
    hidden_test: str,
    hidden_language: str = "python",
    hidden_timeout_seconds: int = 15,
    expected: dict[str, Any] | None = None,
    forbidden: list[dict[str, str]] | None = None,
    constraints: dict[str, Any] | None = None,
    rationale: str,
    budget: dict[str, int] | None = None,
    max_rounds: int = 10,
    timeout_seconds: int = 45,
) -> dict[str, Any]:
    tools = [
        "ls_workspace", "repo_map", "search_codebase", "read_file", "read_file_raw",
        "grep_workspace", "find_files", "write_file", "edit_file", "run_command",
        "report_blocked",
    ]
    # Work's runtime treats a write as the delivery obligation. Every coding
    # task therefore ends with a tiny report write after its repair/verify
    # steps; hidden tests, not the report's prose, decide correctness.
    actions = [*deepcopy(actions), _write("REPORT.md", "completed\n", "deliver-report")]
    base_forbidden = [
        {
            "tool": "write_file",
            "path_contains": "tests/",
            "reason": "coding tasks must repair the implementation, not tests",
        },
        {
            "tool": "edit_file",
            "path_contains": "tests/",
            "reason": "coding tasks must repair the implementation, not tests",
        },
    ]
    definition: dict[str, Any] = {
        "task_id": task_id,
        "category": category,
        "difficulty": difficulty,
        "version": BENCHMARK_VERSION,
        "objective": objective,
        "fixture": {"files": dict(fixture)},
        "model_input": {"provider": "scripted-reference", "actions": deepcopy(actions)},
        "allowed_tools": tools,
        "permissions": {
            "allowed_tools": tools,
            "allowed_effects": _READ_WRITE,
        },
        "budget": dict(budget or _DEFAULT_BUDGET),
        "timeout_seconds": timeout_seconds,
        "max_rounds": max_rounds,
        "approval": {"mode": "none"},
        "expected_final_state": {
            "expected_status": "succeeded",
            **(expected or {}),
        },
        "forbidden": [*base_forbidden, *(forbidden or [])],
        "constraints": {
            "only_workspace": True,
            "no_new_dependencies": True,
            "repair_tests_forbidden": True,
            **(constraints or {}),
        },
        "hidden_test": {
            # Host-held only. Never copied into the workspace and never part of
            # the model-visible taskset. `source` is stripped by
            # public_definition(); the evaluator reads it through
            # hidden_tests_for().
            "language": hidden_language,
            "timeout_seconds": hidden_timeout_seconds,
            "content_sha256": digest(hidden_test),
            "content_bytes": len(hidden_test.encode("utf-8")),
            "source": hidden_test,
        },
        "forbidden_side_effects": {
            "outside_canary_content": "UNTOUCHED",
            "unauthorized_tool_executions": 0,
            "duplicate_side_effects": 0,
        },
        "independent_verifier": "workspace+events+hidden-tests+forbidden-scan",
        "rationale": rationale,
        "applicability": {
            "OFFLINE_SCRIPTED": "evaluator self-check: reference repairs must pass hidden tests",
            "ONLINE_MODEL": "real-model coding effectiveness with the same gates and verifier",
        },
    }
    definition["task_sha256"] = digest(definition)
    return definition


def _edit(path: str, old: str, new: str, step: str) -> dict[str, Any]:
    return {"id": step, "tool": "edit_file",
            "arguments": {"path": path, "old_text": old, "new_text": new}}


def _write(path: str, content: str, step: str) -> dict[str, Any]:
    return {"id": step, "tool": "write_file",
            "arguments": {"path": path, "content": content}}


def _run(path: str, step: str, *, program: str = "python3") -> dict[str, Any]:
    return {"id": step, "tool": "run_command", "arguments": {"command": program, "args": [path]}}


_PY_PUBLIC = """import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from {module} import {name}

assert {check}, "{label} failed"
print("public ok")
"""

_JS_PUBLIC = """const {{ {name} }} = require('../{module}');
assert({check}, "{label} failed");
console.log("public ok");
"""


def _py_public(module: str, name: str, check: str, label: str) -> str:
    return _PY_PUBLIC.format(module=module, name=name, check=check, label=label)


def _js_public(module: str, name: str, check: str, label: str) -> str:
    return _JS_PUBLIC.format(module=module, name=name, check=check, label=label)


def _py_runner(source: str) -> str:
    return (
        "import os, subprocess, sys\n"
        "workspace = os.environ['WORKSPACE']\n"
        "sys.path.insert(0, workspace)\n"
        + source
    )


def coding_taskset() -> list[dict[str, Any]]:
    """Return the model-visible task definitions.

    Hidden test bytes are deliberately omitted. The evaluator keeps them in
    ``hidden_tests_for`` so they cannot leak into the workspace, prompt or
    exported taskset.
    """
    return [public_definition(task) for task in _TASKS]


def hidden_tests_for(task_id: str) -> dict[str, str]:
    """Return the host-held hidden test source for one task."""
    task = get_coding_task(task_id, public=False)
    hidden = ((task.get("hidden_test") or {}).get("source") or "")
    if not hidden:
        raise KeyError(f"task {task_id!r} has no hidden test")
    filename = "test.py" if task["hidden_test"]["language"] == "python" else "test.cjs"
    return {filename: hidden}


def get_coding_task(task_id: str, *, public: bool = True) -> dict[str, Any]:
    for task in _TASKS:
        if task["task_id"] == task_id:
            return public_definition(task) if public else deepcopy(task)
    raise KeyError(f"unknown work-coding task: {task_id!r}")


def coding_taskset_sha256() -> str:
    """Hash the public taskset plus hidden-test identities (not their bytes)."""
    payload = {
        "version": BENCHMARK_VERSION,
        "tasks": [
            {
                "task_sha256": task["task_sha256"],
                "hidden_sha256": task["hidden_test"]["content_sha256"],
            }
            for task in _TASKS
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def public_definition(task: dict[str, Any]) -> dict[str, Any]:
    """Strip host-held hidden test bytes from a task definition."""
    out = deepcopy(task)
    hidden = dict(out.get("hidden_test") or {})
    out["hidden_test"] = {
        "language": hidden.get("language"),
        "timeout_seconds": hidden.get("timeout_seconds"),
        "content_sha256": hidden.get("content_sha256"),
        "content_bytes": hidden.get("content_bytes"),
    }
    return out


def _task(
    task_id: str,
    category: str,
    difficulty: str,
    objective: str,
    *,
    fixture: dict[str, str],
    actions: list[dict[str, Any]],
    hidden_test: str,
    hidden_language: str = "python",
    rationale: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return _ctask(
        task_id, category, difficulty, objective,
        fixture=fixture, actions=actions, hidden_test=hidden_test,
        hidden_language=hidden_language, rationale=rationale, **kwargs,
    )


# ---------------------------------------------------------------------------
# 1-3: single-file bug repair


_SUM_TO_N_BUGGY = '''def sum_to_n(n):
    """Return 1 + 2 + ... + n."""
    return sum(range(1, n))
'''

_SUM_TO_N_PUBLIC = _py_public("sum_to_n", "sum_to_n", "sum_to_n(3) == 6", "sum_to_n(3)")


_SUM_TO_N_HIDDEN = _py_runner(
    "from sum_to_n import sum_to_n\n"
    "assert sum_to_n(0) == 0\n"
    "assert sum_to_n(1) == 1\n"
    "assert sum_to_n(3) == 6\n"
    "assert sum_to_n(100) == 5050\n"
    "assert sum_to_n(-5) == 0\n"
    "print('hidden ok')\n"
)

_AVERAGE_BUGGY = '''def average(numbers):
    """Return the arithmetic mean; empty input has average 0."""
    return sum(numbers) / len(numbers)
'''

_AVERAGE_PUBLIC = _py_public(
    "average", "average", "average([2, 4]) == 3.0", "average([2, 4])")


_AVERAGE_HIDDEN = _py_runner(
    "from average import average\n"
    "assert average([]) == 0\n"
    "assert average([2, 4]) == 3.0\n"
    "assert average([-2, 2]) == 0\n"
    "assert average([7]) == 7\n"
    "print('hidden ok')\n"
)

_CLIP_BUGGY = '''def clip(value, low, high):
    """Clamp value into [low, high]."""
    return max(low, min(value, low))
'''

_CLIP_PUBLIC = _py_public(
    "clip", "clip", "clip(8, 0, 10) == 8", "clip(8, 0, 10)")


_CLIP_HIDDEN = _py_runner(
    "from clip import clip\n"
    "assert clip(-2, 0, 10) == 0\n"
    "assert clip(8, 0, 10) == 8\n"
    "assert clip(20, 0, 10) == 10\n"
    "assert clip(5, 5, 5) == 5\n"
    "print('hidden ok')\n"
)


# ---------------------------------------------------------------------------
# 4-5: multi-file feature


_PRICING_CONFIG = '''STANDARD_RATE = 0.13
'''

_INVOICE_BUGGY = '''from pricing_config import STANDARD_RATE


def invoice_total(amount, customer_type):
    """Return tax-inclusive total; students are currently charged like staff."""
    if customer_type == "student":
        return round(amount * (1 + STANDARD_RATE), 2)
    return round(amount * (1 + STANDARD_RATE), 2)
'''

_INVOICE_PUBLIC = (
    "from invoice import invoice_total\n"
    "from pricing_config import STUDENT_RATE\n"
    "assert STUDENT_RATE == 0.10\n"
    "assert invoice_total(100, 'student') == 110.0\n"
    "assert invoice_total(100, 'staff') == 113.0\n"
    "print('public ok')\n"
)

_INVOICE_HIDDEN = _py_runner(
    "from invoice import invoice_total\n"
    "from pricing_config import STUDENT_RATE, STANDARD_RATE\n"
    "assert STUDENT_RATE == 0.10\n"
    "assert STANDARD_RATE == 0.13\n"
    "assert invoice_total(0, 'student') == 0\n"
    "assert invoice_total(80, 'student') == 88.0\n"
    "assert invoice_total(80, 'staff') == 90.4\n"
    "assert invoice_total(60, 'alumni') == 67.8\n"
    "print('hidden ok')\n"
)

_STORE_V1 = '''class TaskStore:
    def __init__(self):
        self.items = []

    def add(self, title):
        self.items.append({"title": title, "done": False})
        return self.items[-1]
'''

_CLI_V1 = '''from task_store import TaskStore


def main():
    store = TaskStore()
    store.add("write report")


if __name__ == "__main__":
    main()
'''

_STORE_PUBLIC = (
    "from task_store import TaskStore\n"
    "store = TaskStore()\n"
    "item = store.add('buy milk')\n"
    "store.complete(item['id'])\n"
    "assert store.items[0]['done'] is True\n"
    "print('public ok')\n"
)

_STORE_HIDDEN = _py_runner(
    "from task_store import TaskStore\n"
    "from cli import parse_args\n"
    "store = TaskStore()\n"
    "first = store.add('a')\n"
    "second = store.add('b')\n"
    "assert first['id'] != second['id']\n"
    "store.complete(first['id'])\n"
    "assert store.items[0]['done'] is True\n"
    "assert store.items[1]['done'] is False\n"
    "try:\n"
    "    store.complete('missing')\n"
    "except KeyError:\n"
    "    pass\n"
    "else:\n"
    "    raise AssertionError('missing id must fail')\n"
    "assert parse_args(['add', 'hello']) == ('add', 'hello')\n"
    "assert parse_args(['complete', 'task-1']) == ('complete', 'task-1')\n"
    "try:\n"
    "    parse_args([])\n"
    "except ValueError:\n"
    "    pass\n"
    "else:\n"
    "    raise AssertionError('empty args must fail')\n"
    "print('hidden ok')\n"
)

_CLI_PUBLIC = (
    "from cli import parse_args\n"
    "assert parse_args(['complete', 'task-1']) == ('complete', 'task-1')\n"
    "print('public ok')\n"
)

_CLI_HIDDEN = _py_runner(
    "from cli import parse_args\n"
    "assert parse_args(['add', 'hello']) == ('add', 'hello')\n"
    "assert parse_args(['complete', 'task-1']) == ('complete', 'task-1')\n"
    "try:\n"
    "    parse_args([])\n"
    "except ValueError:\n"
    "    pass\n"
    "else:\n"
    "    raise AssertionError('empty args must fail')\n"
    "print('hidden ok')\n"
)


# ---------------------------------------------------------------------------
# 6-7: regression repair


_ORDER_BUGGY = '''def order_total(items):
    """Return sum of price * quantity. Empty carts used to be free."""
    if not items:
        return None
    return sum(item["price"] * item["quantity"] for item in items)
'''

_ORDER_PUBLIC = _py_public(
    "order_total", "order_total",
    "order_total([{'price': 2, 'quantity': 3}]) == 6",
    "order_total non-empty")


_ORDER_HIDDEN = _py_runner(
    "from order_total import order_total\n"
    "assert order_total([]) == 0\n"
    "assert order_total([{'price': 2, 'quantity': 3}]) == 6\n"
    "assert order_total([{'price': 1.5, 'quantity': 2}, {'price': 4, 'quantity': 1}]) == 7.0\n"
    "assert order_total([{'price': 0, 'quantity': 0}]) == 0\n"
    "print('hidden ok')\n"
)

_CACHE_JS_BUGGY = """const cache = new Map();

function compute(key, value) {
  const hit = cache.get(key);
  if (hit) {
    return hit;
  }
  const result = `${key}:${value}`;
  cache.set(key, result);
  return result;
}

module.exports = { compute, cache };
"""

_CACHE_PUBLIC = _js_public(
    "src/cache.js", "compute",
    "compute('a', 1) === 'a:1'", "cache first compute")

_CACHE_HIDDEN = """const assert = require('node:assert');
const path = require('node:path');
const { compute } = require(path.join(process.env.WORKSPACE, 'src', 'cache.js'));

assert.equal(compute('a', 1), 'a:1');
assert.equal(compute('a', 2), 'a:2', 'changed value must not reuse stale key');
assert.equal(compute('b', 1), 'b:1');
console.log('hidden ok');
"""


# ---------------------------------------------------------------------------
# 8-9: refactor, behaviour-preserving


_TAX_DUPLICATED = '''def receipt_net(amount):
    # Round to cents after subtracting the fixed 0.07 shipping tax.
    return round(amount / 1.07, 2)


def report_net(amounts):
    # Round to cents after subtracting the fixed 0.07 shipping tax.
    return [round(amount / 1.07, 2) for amount in amounts]
'''

_TAX_PUBLIC = _py_public(
    "tax", "receipt_net", "receipt_net(107) == 100.0", "receipt_net(107)")

_TAX_HIDDEN = _py_runner(
    "from tax import receipt_net, report_net, net_amount\n"
    "assert net_amount(107) == 100.0\n"
    "assert receipt_net(107) == 100.0\n"
    "assert report_net([107, 214]) == [100.0, 200.0]\n"
    "assert net_amount(0) == 0\n"
    "print('hidden ok')\n"
)

_LIMITS_DUPLICATED = '''def can_edit_public(user):
    if user["role"] == "admin" or user["team"] == "content" and user["active"]:
        return True
    return False


def can_edit_private(user):
    if user["role"] == "admin" or user["team"] == "content" and user["active"]:
        return True
    return False
'''

_LIMITS_PUBLIC = _py_public(
    "permissions", "can_edit_public",
    "can_edit_public({'role': 'admin', 'team': '', 'active': True}) is True",
    "admin can edit public")

_LIMITS_HIDDEN = _py_runner(
    "from permissions import can_edit_public, can_edit_private, is_editor\n"
    "admin = {'role': 'admin', 'team': '', 'active': True}\n"
    "content = {'role': 'editor', 'team': 'content', 'active': True}\n"
    "inactive = {'role': 'editor', 'team': 'content', 'active': False}\n"
    "guest = {'role': 'viewer', 'team': '', 'active': True}\n"
    "assert is_editor(admin) is True\n"
    "assert is_editor(content) is True\n"
    "assert is_editor(inactive) is False\n"
    "assert is_editor(guest) is False\n"
    "assert can_edit_public(admin) is True\n"
    "assert can_edit_private(content) is True\n"
    "assert can_edit_private(inactive) is False\n"
    "print('hidden ok')\n"
)


# ---------------------------------------------------------------------------
# 10-11: frontend behaviour


_COUNTER_JS_BUGGY = """class Counter {
  constructor({ max = 3 } = {}) {
    this.value = 0;
    this.max = max;
  }

  increment() {
    // A capped counter should stop at max, not overshoot it.
    this.value += 1;
    return this.value;
  }
}

module.exports = { Counter };
"""

_COUNTER_PUBLIC = _js_public(
    "src/counter.js", "Counter",
    "new Counter().increment() === 1", "counter first increment")

_COUNTER_HIDDEN = """const assert = require('node:assert');
const path = require('node:path');
const { Counter } = require(path.join(process.env.WORKSPACE, 'src', 'counter.js'));

const counter = new Counter({ max: 2 });
assert.equal(counter.increment(), 1);
assert.equal(counter.increment(), 2);
assert.equal(counter.increment(), 2, 'counter must stop at max');
const unlimited = new Counter({});
assert.equal(unlimited.increment(), 1);
console.log('hidden ok');
"""

_VALIDATOR_JS_BUGGY = """function isValidEmail(value) {
  // Intentionally accepts an empty local part.
  return /^[^@]+@example\\.com$/.test(value || '');
}

module.exports = { isValidEmail };
"""

_VALIDATOR_PUBLIC = _js_public(
    "src/validator.js", "isValidEmail",
    "isValidEmail('user@example.com') === true", "valid email")

_VALIDATOR_HIDDEN = """const assert = require('node:assert');
const path = require('node:path');
const { isValidEmail } = require(path.join(process.env.WORKSPACE, 'src', 'validator.js'));

assert.equal(isValidEmail('user@example.com'), true);
assert.equal(isValidEmail('user@example.com '), false);
assert.equal(isValidEmail('@example.com'), false);
assert.equal(isValidEmail('user@'), false);
assert.equal(isValidEmail(''), false);
assert.equal(isValidEmail(null), false);
console.log('hidden ok');
"""


# ---------------------------------------------------------------------------
# 12: test repair


_IMPORT_BUGGY = '''from .helpers import normalize
'''

_IMPORT_PUBLIC = (
    "import sys\n"
    "from helpers import normalize\n"
    "assert normalize(' A ') == 'A'\n"
    "print('public ok')\n"
)

_IMPORT_HIDDEN = _py_runner(
    "from normalize import normalize\n"
    "assert normalize(' A ') == 'A'\n"
    "assert normalize('A\\t') == 'A'\n"
    "assert normalize('') == ''\n"
    "print('hidden ok')\n"
)


_TASKS = [
    _task(
        "single-file-off-by-one", "bug_single_file", "easy",
        "修复 sum_to_n 的边界错误：n=3 必须返回 6，n=0 返回 0。先跑公共测试，修好后重新运行。",
        fixture={
            "sum_to_n.py": _SUM_TO_N_BUGGY,
            "tests/test_public.py": _SUM_TO_N_PUBLIC,
        },
        actions=[
            _edit("sum_to_n.py", "return sum(range(1, n))",
                  "return sum(range(1, n + 1)) if n > 0 else 0", "fix-boundary"),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_SUM_TO_N_HIDDEN,
        rationale="测最小定位：单文件 off-by-one 与非正数边界。",
    ),
    _task(
        "single-file-empty-mean", "bug_single_file", "easy",
        "average([]) 现在会除零。修复为空列表返回 0，并保持非空列表的平均值正确。",
        fixture={
            "average.py": _AVERAGE_BUGGY,
            "tests/test_public.py": _AVERAGE_PUBLIC,
        },
        actions=[
            _edit("average.py", "return sum(numbers) / len(numbers)",
                  "return sum(numbers) / len(numbers) if numbers else 0", "fix-empty"),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_AVERAGE_HIDDEN,
        rationale="测除零失败后的最小修复与负数/单元素边界。",
    ),
    _task(
        "single-file-clamp-order", "bug_single_file", "easy",
        "clip(value, low, high) 没有正确使用 high。修复 clamp 语义并通过公共测试。",
        fixture={
            "clip.py": _CLIP_BUGGY,
            "tests/test_public.py": _CLIP_PUBLIC,
        },
        actions=[
            _edit("clip.py", "return max(low, min(value, low))",
                  "return max(low, min(value, high))", "fix-high"),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_CLIP_HIDDEN,
        rationale="测参数顺序错误与相同上下界。",
    ),
    _task(
        "multi-file-student-rate", "feature_multi_file", "medium",
        "新增学生折扣：在 pricing_config.py 中定义 STUDENT_RATE = 0.10；invoice_total 对 student 使用学生税率，其他客户仍用 STANDARD_RATE。",
        fixture={
            "pricing_config.py": _PRICING_CONFIG,
            "invoice.py": _INVOICE_BUGGY,
            "tests/test_public.py": _INVOICE_PUBLIC,
        },
        actions=[
            _edit("pricing_config.py", "STANDARD_RATE = 0.13",
                  "STANDARD_RATE = 0.13\nSTUDENT_RATE = 0.10", "add-student-rate"),
            _edit("invoice.py", "from pricing_config import STANDARD_RATE",
                  "from pricing_config import STANDARD_RATE, STUDENT_RATE",
                  "import-student-rate"),
            _edit("invoice.py",
                  'if customer_type == "student":\n        return round(amount * (1 + STANDARD_RATE), 2)\n    return round(amount * (1 + STANDARD_RATE), 2)',
                  'if customer_type == "student":\n        return round(amount * (1 + STUDENT_RATE), 2)\n    return round(amount * (1 + STANDARD_RATE), 2)',
                  "apply-student-rate"),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_INVOICE_HIDDEN,
        rationale="测跨文件一致性：配置常量、导入与调用点必须同时成立。",
    ),
    _task(
        "multi-file-task-store", "feature_multi_file", "medium",
        "给 TaskStore 增加稳定 id 和 complete(id)；让 cli.parse_args 支持 add/complete，且空参数抛 ValueError。",
        fixture={
            "task_store.py": _STORE_V1,
            "cli.py": _CLI_V1,
            "tests/test_store_public.py": _STORE_PUBLIC,
            "tests/test_cli_public.py": _CLI_PUBLIC.replace(
                "from cli import parse_args", "from cli import parse_args"),
        },
        actions=[
            _write(
                "task_store.py",
                "class TaskStore:\n"
                "    def __init__(self):\n"
                "        self.items = []\n\n"
                "    def add(self, title):\n"
                "        item = {'id': f'task-{len(self.items) + 1}', 'title': title, 'done': False}\n"
                "        self.items.append(item)\n"
                "        return item\n\n"
                "    def complete(self, task_id):\n"
                "        for item in self.items:\n"
                "            if item['id'] == task_id:\n"
                "                item['done'] = True\n"
                "                return item\n"
                "        raise KeyError(task_id)\n",
                "add-store-complete",
            ),
            _write(
                "cli.py",
                "from task_store import TaskStore\n\n\n"
                "def parse_args(args):\n"
                "    if not args:\n"
                "        raise ValueError('command is required')\n"
                "    return args[0], args[1]\n\n\n"
                "def main():\n"
                "    command, value = parse_args(['add', 'write report'])\n"
                "    store = TaskStore()\n"
                "    if command == 'add':\n"
                "        store.add(value)\n\n\n"
                "if __name__ == '__main__':\n"
                "    main()\n",
                "add-cli-parser",
            ),
            _run("tests/test_store_public.py", "run-store-public"),
            _run("tests/test_cli_public.py", "run-cli-public"),
        ],
        hidden_test=_STORE_HIDDEN,
        rationale="测 API 扩展：数据结构、CLI 解析和错误行为必须一致。",
    ),
    _task(
        "regression-empty-order", "regression", "easy",
        "空购物车应返回 0，而不是 None。修复 order_total 并保持非空购物车结果不变。",
        fixture={
            "order_total.py": _ORDER_BUGGY,
            "tests/test_public.py": _ORDER_PUBLIC,
        },
        actions=[
            _edit("order_total.py", 'if not items:\n        return None',
                  "if not items:\n        return 0", "fix-empty-cart"),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_ORDER_HIDDEN,
        rationale="测回归修复：类型约定变化会破坏下游调用。",
    ),
    _task(
        "regression-stale-cache", "regression", "medium",
        "修复 cache.compute：相同 key 但 value 变化时必须重新计算，不能复用旧缓存。",
        fixture={
            "src/cache.js": _CACHE_JS_BUGGY,
            "tests/cache_public.test.js": _CACHE_PUBLIC,
        },
        actions=[
            _edit("src/cache.js", "const hit = cache.get(key);\n  if (hit) {\n    return hit;\n  }\n  const result = `${key}:${value}`;\n  cache.set(key, result);",
                  "const result = `${key}:${value}`;\n  cache.set(key, result);", "fix-stale-cache"),
            _run("tests/cache_public.test.js", "run-public", program="node"),
        ],
        hidden_test=_CACHE_HIDDEN,
        hidden_language="javascript",
        rationale="测 JS 回归：key 相同但输入变化时缓存失效。",
    ),
    _task(
        "refactor-extract-tax", "refactor", "medium",
        "把 receipt_net 和 report_net 中重复的 0.07 税计算抽成 net_amount(amount)，行为保持不变，并让两个函数复用它。",
        fixture={
            "tax.py": _TAX_DUPLICATED,
            "tests/test_public.py": _TAX_PUBLIC,
        },
        actions=[
            _write(
                "tax.py",
                "def net_amount(amount):\n"
                "    \"\"\"Round to cents after subtracting the fixed 0.07 shipping tax.\"\"\"\n"
                "    return round(amount / 1.07, 2)\n\n\n"
                "def receipt_net(amount):\n"
                "    return net_amount(amount)\n\n\n"
                "def report_net(amounts):\n"
                "    return [net_amount(amount) for amount in amounts]\n",
                "extract-net-amount",
            ),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_TAX_HIDDEN,
        expected={"file_contains": {"tax.py": ["def net_amount(amount):"]}},
        rationale="测行为保持重构：公共 API 不变，重复实现收敛。",
    ),
    _task(
        "refactor-editor-policy", "refactor", "medium",
        "把 can_edit_public 和 can_edit_private 的重复权限判断抽成 is_editor(user)，行为保持不变。",
        fixture={
            "permissions.py": _LIMITS_DUPLICATED,
            "tests/test_public.py": _LIMITS_PUBLIC,
        },
        actions=[
            _write(
                "permissions.py",
                "def is_editor(user):\n"
                "    return user[\"role\"] == \"admin\" or user[\"team\"] == \"content\" and user[\"active\"]\n\n\n"
                "def can_edit_public(user):\n"
                "    return is_editor(user)\n\n\n"
                "def can_edit_private(user):\n"
                "    return is_editor(user)\n",
                "extract-is-editor",
            ),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_LIMITS_HIDDEN,
        expected={"file_contains": {"permissions.py": ["def is_editor(user):"]}},
        rationale="测重复权限判断收敛，且保留既有 API。",
    ),
    _task(
        "frontend-counter-cap", "frontend", "easy",
        "修复 Counter.increment：到达 max 后不能再增加；未传 max 时保持递增。",
        fixture={
            "src/counter.js": _COUNTER_JS_BUGGY,
            "tests/counter_public.test.js": _COUNTER_PUBLIC,
        },
        actions=[
            _edit("src/counter.js", "this.value += 1;\n    return this.value;",
                  "if (this.value >= this.max) {\n      return this.value;\n    }\n    this.value += 1;\n    return this.value;",
                  "fix-counter-cap"),
            _run("tests/counter_public.test.js", "run-public", program="node"),
        ],
        hidden_test=_COUNTER_HIDDEN,
        hidden_language="javascript",
        rationale="测前端状态边界：上限与默认配置。",
    ),
    _task(
        "frontend-email-validator", "frontend", "medium",
        "修复 isValidEmail：只接受非空 local part 的 @example.com 邮箱，空白、缺失 local/domain 都必须返回 false。",
        fixture={
            "src/validator.js": _VALIDATOR_JS_BUGGY,
            "tests/validator_public.test.js": _VALIDATOR_PUBLIC,
        },
        actions=[
            _edit("src/validator.js",
                  "return /^[^@]+@example\\.com$/.test(value || '');",
                  "return /^[A-Za-z0-9._%+-]+@example\\.com$/.test(String(value || ''));",
                  "fix-email-local-part"),
            _run("tests/validator_public.test.js", "run-public", program="node"),
        ],
        hidden_test=_VALIDATOR_HIDDEN,
        hidden_language="javascript",
        rationale="测输入清洗与校验边界：空 local、空值、空白。",
    ),
    _task(
        "test-repair-import", "test_repair", "easy",
        "公共测试因导入路径失败。只修实现模块的组织，使 `from helpers import normalize` 可用；禁止修改 tests/ 下的文件。",
        fixture={
            "normalize.py": "def normalize(value):\n    return str(value or '').strip()\n",
            "helpers/__init__.py": "",
            "tests/test_public.py": _IMPORT_PUBLIC,
        },
        actions=[
            _write(
                "helpers/normalize.py",
                "def normalize(value):\n"
                "    return str(value or '').strip()\n",
                "move-normalize",
            ),
            _write(
                "helpers/__init__.py",
                "from .normalize import normalize\n\n__all__ = ['normalize']\n",
                "export-normalize",
            ),
            _run("tests/test_public.py", "run-public"),
        ],
        hidden_test=_IMPORT_HIDDEN.replace(
            "from normalize import normalize", "from helpers.normalize import normalize"),
        rationale="测测试修复边界：只能调整实现组织，不能改测试。",
    ),
]

_TASK_BY_ID = {task["task_id"]: task for task in _TASKS}


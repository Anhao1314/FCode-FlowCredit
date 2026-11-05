"""P1-1 增量 summary 缓存:消除每轮对全部历史观测重跑 summarizer。

设计契约(见提交说明):
- 缓存 key = (tool, args_hash, result_hash, summary_policy, summary_version);
- summarizer 是这五元组的纯函数,同一 key 永远同一产物——因此**不做 TTL、
  不做容量淘汰**(表大小受唯一组合数天然限制);
- 与 TaskStore 同库不同表(summary_cache),复用现有连接与事务约定;
- 缓存是优化不是故障点:任何读写异常都 fail-open(回退实时 summarize),
  不得让 loop 中断。缓存只在未命中时写入,命中不重写。
"""

from __future__ import annotations

import json
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS summary_cache (
    tool            TEXT NOT NULL,
    args_hash       TEXT NOT NULL,
    result_hash     TEXT NOT NULL,
    summary_policy  TEXT NOT NULL,
    summary_version INTEGER NOT NULL,
    summary_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tool, args_hash, result_hash, summary_policy, summary_version)
)
"""


class SummaryCache:
    """(tool, args, result, policy, version) → summary 的持久缓存。"""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._ready = False
        #: 诊断计数(V3 命中率报告用);不参与任何决策。
        self.hits = 0
        self.misses = 0

    def hits_probe(self) -> int:
        return self.hits

    # ------------------------------------------------------------------ setup

    def _ensure_table(self) -> None:
        if self._ready:
            return
        with self._store.transaction():
            self._store._conn.execute(_DDL)
        self._ready = True

    # ------------------------------------------------------------------ io

    def get(self, key: tuple[str, str, str, str, int]) -> dict | None:
        try:
            self._ensure_table()
            row = self._store._conn.execute(
                "SELECT summary_json FROM summary_cache WHERE tool=? AND "
                "args_hash=? AND result_hash=? AND summary_policy=? AND "
                "summary_version=?",
                key,
            ).fetchone()
            if row is None:
                self.misses += 1
                return None
            product = json.loads(row[0])
            self.hits += 1
            return product
        except Exception:  # noqa: BLE001 — fail-open:缓存坏了不影响 summarize
            self.misses += 1
            self._ready = False
            return None

    def put(self, key: tuple[str, str, str, str, int], summary: dict) -> None:
        try:
            self._ensure_table()
            with self._store.transaction():
                self._store._conn.execute(
                    "INSERT OR IGNORE INTO summary_cache "
                    "(tool, args_hash, result_hash, summary_policy, "
                    "summary_version, summary_json, created_at) "
                    "VALUES (?,?,?,?,?,?, datetime('now'))",
                    (*key, json.dumps(summary, ensure_ascii=False,
                                      sort_keys=True)),
                )
        except Exception:  # noqa: BLE001 — 写失败不阻塞本次产物使用
            self._ready = False

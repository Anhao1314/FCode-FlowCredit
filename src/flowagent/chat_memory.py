"""操作员长期记忆：跨线程、跨任务的一句话沉淀。

这是对话的记忆层（M1）：用户用 /remember 存一句话（偏好、事实、纠正），
之后每轮对话把它注入 system 上下文。只存操作员明示的内容——不自动
提炼，不做嵌入检索；列表在设置页可见、可删，记忆永远可审计。

存储与 chat.sqlite 并排（chat-memory.sqlite），独立的表与生命周期，
不碰 TaskStore 的 schema 迁移。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 单条记忆长度：一句话，不是一篇文章。
MIN_CHARS = 2
MAX_CHARS = 600
#: 注入 system 的条数上限——记忆是佐料，不是主菜。
INJECT_LIMIT = 12
LIST_LIMIT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'operator',
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MemoryStore:
    """按条记忆的 sqlite 存储。同文重复 = 刷新时间，不产生重复行。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 工具循环在 worker 线程里调 _prompt_messages → 注入记忆，
        # 与 TaskStore 同款：跨线程连接 + 写锁。
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def remember(self, text: str, *, source: str = "", kind: str = "operator") -> dict[str, Any]:
        text = str(text or "").strip()
        if not MIN_CHARS <= len(text) <= MAX_CHARS:
            raise ValueError(f"记忆长度须在 {MIN_CHARS}–{MAX_CHARS} 字之间")
        now = _now()
        row = self._db.execute(
            "SELECT * FROM memories WHERE text = ?", (text,)
        ).fetchone()
        with self._lock:
            if row is not None:
                self._db.execute(
                    "UPDATE memories SET last_seen = ?, source = ? WHERE id = ?",
                    (now, source or row["source"], row["id"]),
                )
                self._db.commit()
                return self.get(row["id"])
            entry_id = uuid.uuid4().hex[:16]
            self._db.execute(
                "INSERT INTO memories (id, kind, text, source, created, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, kind, text, source, now, now),
            )
            self._db.commit()
            return self.get(entry_id)

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (entry_id,)).fetchone()
            return dict(row) if row is not None else None

    def list(self, limit: int = LIST_LIMIT) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), LIST_LIMIT))
        with self._lock:
            rows = self._db.execute(
                # rowid 兜底：同一秒内多次写入时按插入序倒排，排序保持确定性
                "SELECT * FROM memories ORDER BY last_seen DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def forget(self, entry_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
            self._db.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def context_block(self, limit: int = INJECT_LIMIT) -> str:
        """system 注入块。空库返回空串——不注入空段落。"""
        entries = self.list(limit=limit)
        if not entries:
            return ""
        lines = "\n".join(f"- {entry['text']}" for entry in entries)
        return (
            "\n\n[操作员长期记忆——此前对话中用户让你记住的内容，"
            "与当前请求相关时才使用，没有依据的不要编造]\n" + lines
        )

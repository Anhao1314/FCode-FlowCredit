"""操作员长期记忆：跨线程、跨任务的一句话沉淀。

这是对话的记忆层（M1/M2），与 L3 案例簿分开：

- **操作员明示**：``/remember`` 或设置页写入，kind=operator；
- **保守自动提炼**：只从操作员消息里识别明确的偏好/约定（“以后都…”
  “本项目统一…”），kind=auto；绝不从模型输出、工具结果或网页内容提炼；
- **相关召回**：每轮按当前用户消息做词法 BM25 召回，只注入相关条目；
  没有相关命中时回落到最近条目，不注入空段落。

存储与 chat.sqlite 并排（chat-memory.sqlite），独立的表与生命周期，
不碰 TaskStore 的 schema 迁移。所有条目在设置页可见、可删。
"""

from __future__ import annotations

import math
import sqlite3
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core.lexical import tokens_for
from .memory.extract import extract_memories

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
    last_seen TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL DEFAULT 'global',
    confidence REAL NOT NULL DEFAULT 1.0
);
"""

#: 旧库（M1）没有后三列；启动时逐列补齐，不重建表、不丢数据。
_MIGRATIONS = (
    ("auto", "INTEGER NOT NULL DEFAULT 0"),
    ("scope", "TEXT NOT NULL DEFAULT 'global'"),
    ("confidence", "REAL NOT NULL DEFAULT 1.0"),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_days(value: str) -> float:
    try:
        then = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 365.0
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86_400.0)


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
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(memories)")}
        for name, ddl in _MIGRATIONS:
            if name not in columns:
                self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")

    # ------------------------------------------------------------------ writes

    def remember(
        self,
        text: str,
        *,
        source: str = "",
        kind: str = "operator",
        auto: bool = False,
        scope: str = "global",
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        text = str(text or "").strip()
        if not MIN_CHARS <= len(text) <= MAX_CHARS:
            raise ValueError(f"记忆长度须在 {MIN_CHARS}–{MAX_CHARS} 字之间")
        now = _now()
        auto_value = 1 if auto else 0
        confidence_value = max(0.0, min(float(confidence), 1.0))
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM memories WHERE text = ?", (text,)
            ).fetchone()
            if row is not None:
                # 明示优先于自动：用户以后手动写入同一条，升为 operator。
                final_auto = auto_value and int(row["auto"] or 0)
                final_kind = row["kind"] if final_auto and row["kind"] == "operator" else (
                    "operator" if not final_auto else kind
                )
                self._db.execute(
                    "UPDATE memories SET last_seen = ?, source = ?, kind = ?, auto = ?, "
                    "scope = ?, confidence = ? WHERE id = ?",
                    (
                        now,
                        source or row["source"],
                        final_kind,
                        final_auto,
                        scope or row["scope"] or "global",
                        max(float(row["confidence"] or 0), confidence_value),
                        row["id"],
                    ),
                )
                self._db.commit()
                return self.get(row["id"])
            entry_id = uuid.uuid4().hex[:16]
            self._db.execute(
                "INSERT INTO memories (id, kind, text, source, created, last_seen, "
                "auto, scope, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry_id, kind, text, source, now, now,
                    auto_value, scope or "global", confidence_value,
                ),
            )
            self._db.commit()
            return self.get(entry_id)

    def ingest(self, text: str, *, source: str = "") -> list[dict[str, Any]]:
        """Extract conservative operator preferences and store them as auto."""
        written = []
        for candidate in extract_memories(text):
            written.append(self.remember(
                str(candidate["text"]),
                source=source,
                kind="auto",
                auto=True,
                confidence=float(candidate.get("confidence") or 0.6),
            ))
        return written

    # ------------------------------------------------------------------- reads

    def get(self, entry_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (entry_id,)).fetchone()
            return dict(row) if row is not None else None

    def list(self, limit: int = LIST_LIMIT, scope: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), LIST_LIMIT))
        with self._lock:
            if scope:
                rows = self._db.execute(
                    "SELECT * FROM memories WHERE scope = ? "
                    "ORDER BY last_seen DESC, rowid DESC LIMIT ?",
                    (scope, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    # rowid 兜底：同一秒内多次写入时按插入序倒排，排序保持确定性
                    "SELECT * FROM memories ORDER BY last_seen DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def recall(self, query: str, *, limit: int = INJECT_LIMIT,
               scope: str | None = None) -> list[dict[str, Any]]:
        """BM25 over memory text, with a small recency/operator prior.

        An empty query is the M1 behaviour: latest entries, no invented
        relevance. A query with no lexical overlap returns [] so the caller can
        decide whether to fall back; ``context_block`` falls back to latest.
        """
        limit = max(1, min(int(limit), LIST_LIMIT))
        entries = self.list(limit=LIST_LIMIT, scope=scope)
        if not query or not str(query).strip():
            return entries[:limit]
        terms = []
        for token in tokens_for(str(query)):
            if token not in terms:
                terms.append(token)
        if not terms or not entries:
            return []

        docs = [Counter(tokens_for(entry["text"])) for entry in entries]
        dfs: Counter[str] = Counter()
        for doc in docs:
            dfs.update(doc.keys())
        total = len(entries)
        avgdl = sum(sum(doc.values()) for doc in docs) / total or 1.0
        scored = []
        for entry, doc in zip(entries, docs):
            length = sum(doc.values()) or 1
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)
                if not tf:
                    continue
                df = dfs[term]
                idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
                score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * length / avgdl))
            if not score:
                continue
            score += 0.4 if entry.get("kind") == "operator" else 0.1
            score += 0.2 * float(entry.get("confidence") or 0.0)
            score += 0.35 / (1.0 + _age_days(str(entry.get("last_seen") or "")) / 30.0)
            scored.append(dict(entry, score=round(score, 4)))
        scored.sort(key=lambda item: (-item["score"], str(item.get("last_seen") or "")))
        return scored[:limit]

    def forget(self, entry_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
            self._db.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def context_block(self, limit: int = INJECT_LIMIT, *, query: str = "",
                      scope: str | None = None) -> str:
        """system 注入块。空库返回空串——不注入空段落。"""
        entries = self.recall(query, limit=limit, scope=scope) if query else []
        if not entries:
            entries = self.list(limit=limit, scope=scope)
        if not entries:
            return ""
        lines = []
        for entry in entries:
            prefix = "[auto] " if entry.get("auto") else ""
            lines.append(f"- {prefix}{entry['text']}")
        return (
            "\n\n[操作员长期记忆——与当前请求相关时使用；标 [auto] 的是从历史消息"
            "自动提炼，可能不准确；没有依据的不要编造]\n" + "\n".join(lines)
        )
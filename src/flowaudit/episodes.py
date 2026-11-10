"""L3 episodic memory: one audit = one episode, keyed by target identity.

设计修订说明:情景层没有复用 CaseIndex——回归状态机需要以
target identity(同一路径)为键的结构化发现列表,而 CaseIndex 的投影与
检索是客观文本 token 驱动的运行时案例库。两者同属 L3,各有其store:
CaseIndex 记运行时案例,这里记审计情景。修订记录见 long-term-memory.md。

为什么不再用 manifest_sha256 做时间线键:修复必然改变清单哈希,而
"修复后重挖"正是回归判断唯一有价值的时刻。按内容哈希建索引导致
prior 永远为空,fixed/regressed 永不产生。清单哈希仍是靶子的内容
版本号,保留在 episode 里用于引用。

回归状态机(同靶时间线的灵魂):
    known      本情景发现 ∩ 上情景发现   (已知,仍未修复)
    new        本情景 − 上情景         (新缺陷)
    fixed      上情景 − 本情景         (已修复)
    regressed  曾修复(更早情景有),上情景缺席,本情景重现
               (修复被回退——最危险的信号)

匹配键分两层:
    1. 指纹 = category + root_cause_group + 证据文件路径集合。行号会
       漂移、片段会被改写,路径集合在"同一处缺陷"上稳定。
    2. 旧情景没有指纹时回退到 (category, root_cause_group) 粗键。
任何一侧缺失(首次审计/全新目标)不产生回归判断。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    audit_id     TEXT PRIMARY KEY,
    target_hash  TEXT NOT NULL,
    target_identity TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    profile      TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    candidates   INTEGER NOT NULL,
    chain_head   TEXT NOT NULL,
    bundle_dir   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_target ON episodes(target_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_episodes_identity ON episodes(target_identity, created_at);
"""


def finding_key(finding: dict) -> tuple:
    """根因身份粗键:只用于没有指纹的旧情景,或跨版本兼容回退。"""
    return (finding.get("category", ""), finding.get("root_cause_group", ""))


def fingerprint_for(category: str, root_cause_group: str,
                    evidence: list[dict] | None) -> str:
    """稳定实例指纹:根因身份 + 证据文件路径集合(不含行号/片段)。

    A finding that moves within the same file keeps its identity; a fix
    that removes the defect site changes the path set and correctly shows
    up as fixed. Aggregate findings (many rows in many files) are coarser:
    any file entering/leaving the evidence set changes the fingerprint —
    still monotone with respect to "did this defect disappear".
    """
    paths = sorted({
        str(item.get("path") or "")
        for item in (evidence or [])
        if isinstance(item, dict) and item.get("path")
    })
    raw = "\x1f".join((category, root_cause_group, "|".join(paths)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def finding_fingerprint(finding: dict) -> str:
    """Return the instance fingerprint, or "" for legacy findings.

    Legacy episodes stored no evidence and no fingerprint. Returning ""
    (rather than a hash of the coarse key) lets the matcher fall back to
    root-cause identity without falsely claiming instance precision.
    """
    explicit = finding.get("fingerprint")
    if explicit:
        return str(explicit)
    evidence = finding.get("evidence")
    if not evidence:
        return ""
    return fingerprint_for(finding.get("category", ""),
                           finding.get("root_cause_group", ""), evidence)


def _matches(left: dict, right: dict) -> bool:
    left_fp, right_fp = finding_fingerprint(left), finding_fingerprint(right)
    if left_fp and right_fp:
        return left_fp == right_fp
    return finding_key(left) == finding_key(right)


def _take_match(pool: list[dict], finding: dict) -> dict | None:
    """Consume the best match from `pool`, one-to-one.

    Exact fingerprint first. A fingerprinted finding may also match a
    *legacy* (fingerprint-less) prior entry by root identity — that is the
    one-time transition audit after this feature lands. Two fingerprinted
    entries with the same root cause but different sites never match.
    """
    fingerprint = finding_fingerprint(finding)
    if fingerprint:
        for index, item in enumerate(pool):
            if finding_fingerprint(item) == fingerprint:
                return pool.pop(index)
        for index, item in enumerate(pool):
            if not finding_fingerprint(item) \
                    and finding_key(item) == finding_key(finding):
                return pool.pop(index)
        return None
    for index, item in enumerate(pool):
        if finding_key(item) == finding_key(finding):
            return pool.pop(index)
    return None


def regression(prior: list[dict], current: list[dict],
               earlier: list[list[dict]] | None = None) -> dict[str, list[dict]]:
    """Compare two finding sets by stable identity.

    `earlier` is the episode history *before* `prior` (oldest → newest),
    used only for `regressed`: absent in prior, present now, but seen in
    an earlier episode. Missing prior (first audit) returns every bucket
    empty — no prior, no regression judgment.
    """
    pool = list(prior)
    known: list[dict] = []
    candidates: list[dict] = []
    for finding in current:
        if _take_match(pool, finding) is not None:
            known.append(finding)
        else:
            candidates.append(finding)
    fixed = pool
    regressed: list[dict] = []
    new: list[dict] = []
    for finding in candidates:
        if any(_matches(item, finding) for episode in (earlier or [])
               for item in episode):
            regressed.append(finding)
        else:
            new.append(finding)
    return {"known": known, "new": new, "fixed": fixed, "regressed": regressed}


class EpisodeStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close_episode(self, *, bundle: dict, bundle_dir: str) -> dict:
        """会话收尾的自动投影:零模型判断,"发生过"不需要批准。"""
        target = bundle.get("target") or {}
        findings = bundle.get("findings") or []
        audit_id = bundle.get("audit_id")
        episode = {
            "audit_id": audit_id,
            "target_hash": target.get("manifest_sha256", ""),
            "target_identity": target.get("identity", ""),
            "created_at": bundle.get("authorization", {}).get("granted_at", ""),
            "profile": (bundle.get("sessions") or [{}])[0].get("session_ref", ""),
            "findings_json": json.dumps(findings, ensure_ascii=False),
            "candidates": int(len(bundle.get("poc_evidence_refs") or [])),
            "chain_head": (bundle.get("chain_verification") or {}).get("head_hash", ""),
            "bundle_dir": bundle_dir,
        }
        with self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO episodes
                (audit_id, target_hash, target_identity, created_at, profile,
                 findings_json, candidates, chain_head, bundle_dir)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                tuple(episode.values()),
            )
        return episode

    def episodes_for_target(self, target_hash: str) -> list[dict]:
        """Content-version lookup: which audits saw exactly this manifest."""
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE target_hash=? "
            "ORDER BY created_at ASC, rowid ASC",
            (target_hash,),
        ).fetchall()
        return [self._episode(row) for row in rows]

    def episodes_for_identity(self, target_identity: str) -> list[dict]:
        """Timeline of one target path across content versions."""
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE target_identity=? "
            "ORDER BY created_at ASC, rowid ASC",
            (target_identity,),
        ).fetchall()
        return [self._episode(row) for row in rows]

    def prior_episode(self, target_identity: str, before_audit_id: str) -> dict | None:
        """Latest episode for the same target path, excluding this audit."""
        episodes = [e for e in self.episodes_for_identity(target_identity)
                    if e["audit_id"] != before_audit_id]
        return episodes[-1] if episodes else None

    @staticmethod
    def _episode(row: sqlite3.Row) -> dict:
        episode = dict(row)
        episode["findings"] = json.loads(episode.pop("findings_json"))
        return episode
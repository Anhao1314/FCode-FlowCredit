"""L3 episodic memory: one audit = one episode, keyed by target.

设计修订说明:情景层没有复用 CaseIndex——回归状态机需要以
target.manifest_sha256 为键的结构化发现列表,而 CaseIndex 的投影与
检索是客观文本 token 驱动的运行时案例库。两者同属 L3,各有其store:
CaseIndex 记运行时案例,这里记审计情景。修订记录见 long-term-memory.md。

回归状态机(同靶时间线的灵魂):
    known      本情景发现 ∩ 上情景发现   (已知,仍未修复)
    new        本情景 − 上情景         (新缺陷)
    fixed      上情景 − 本情景         (已修复)
    regressed  上情景 fixed,本情景重现 (修复被回退——最危险的信号)

匹配键 = (category, root_cause_group):根因身份跨版本稳定,文件行号
会漂移。任何一侧缺失(首次审计/全新目标)不产生回归判断。
"""

from __future__ import annotations

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
"""


def finding_key(finding: dict) -> tuple:
    """根因身份:跨版本稳定的匹配键(文件与行号会漂移)。"""
    return (finding.get("category", ""), finding.get("root_cause_group", ""))


def regression(prior: list[dict], current: list[dict]) -> dict[str, list[dict]]:
    """Compare two finding sets by root-cause identity.

    Missing sides (first audit) return every bucket empty — no prior, no
    regression judgment. `regressed` items carry the prior finding so the
    report can show what was fixed and came back.
    """
    prior_by_key = {finding_key(f): f for f in prior}
    current_by_key = {finding_key(f): f for f in current}
    out: dict[str, list[dict]] = {"known": [], "new": [], "fixed": [], "regressed": []}
    for key, finding in current_by_key.items():
        if key in prior_by_key:
            out["known"].append(finding)
        else:
            out["new"].append(finding)
    for key, finding in prior_by_key.items():
        if key not in current_by_key:
            out["fixed"].append(finding)
        elif key in prior_by_key and key in current_by_key:
            pass
    # regressed:上一情景标记为 fixed 的根因,本情景重新出现。
    for item in out["new"]:
        key = finding_key(item)
        if any(finding_key(f) == key for f in prior):
            out["regressed"].append(item)
    return out


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
        rows = self._conn.execute(
            "SELECT * FROM episodes WHERE target_hash=? ORDER BY created_at ASC",
            (target_hash,),
        ).fetchall()
        episodes = []
        for row in rows:
            episode = dict(row)
            episode["findings"] = json.loads(episode.pop("findings_json"))
            episodes.append(episode)
        return episodes

    def prior_episode(self, target_hash: str, before_audit_id: str) -> dict | None:
        episodes = [e for e in self.episodes_for_target(target_hash)
                    if e["audit_id"] != before_audit_id]
        return episodes[-1] if episodes else None

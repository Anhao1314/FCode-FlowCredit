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

匹配键分三层:
    1. v2 exact  = category + root_cause_group + 规范化证据路径集合 +
       语义锚点(标识符/实体名)。行号/空白/字面量细节变化不影响。
    2. v2 semantic = 同上但不含路径。文件移动时 exact 不匹配,
       semantic 仍能把"同一处缺陷"认成 known,而不是 fixed+new。
    3. v1 / legacy = category + root_cause_group + 证据路径集合;
       更早的情景没有语义锚点,靠 legacy_fingerprint 做一次性过渡。

任何一侧缺失(首次审计/全新目标)不产生回归判断。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

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
    """v1 稳定实例指纹:根因身份 + 证据文件路径集合(不含行号/片段)。

    Kept as the compatibility layer: episodes created before v2 carry this
    value, and the matcher uses it for the one-time transition.
    """
    paths = sorted({
        _normalize_path(item.get("path"))
        for item in (evidence or [])
        if isinstance(item, dict) and item.get("path")
    })
    raw = "\x1f".join((category, root_cause_group, "|".join(paths)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_path(value: Any) -> str:
    """Canonicalize one evidence path without making it location-sensitive."""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_QUOTED_RE = re.compile(r"'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"")
_IDENTIFIER_STOP = {
    "and", "as", "assert", "await", "break", "class", "const", "continue",
    "def", "elif", "else", "except", "export", "extends", "false", "finally",
    "for", "from", "function", "global", "import", "in", "is", "lambda",
    "let", "none", "not", "null", "or", "pass", "print", "raise", "return",
    "self", "true", "try", "type", "undefined", "while", "with", "yield",
}

#: REA / vibe evidence keys worth carrying into a defect's semantic identity.
#: Numeric counts, densities, line numbers and timestamps are deliberately
#: excluded — they measure severity, not identity.
_ANCHOR_KEYS = {
    "category", "dependency", "entitlement", "function", "kind", "module",
    "name", "operation", "package", "path", "phrase", "rule_id", "selector",
    "severity", "type", "value",
}


def _normalize_anchor(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:200] if text else None


def _collect_anchor_values(value: Any, out: set[str], *,
                           include_paths: bool = True) -> None:
    """Collect stable entity names from a bounded evidence summary."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ANCHOR_KEYS and (include_paths or key != "path"):
                anchor = _normalize_anchor(item)
                if anchor:
                    out.add(f"{key}:{anchor}")
            _collect_anchor_values(item, out, include_paths=include_paths)
    elif isinstance(value, list):
        for item in value:
            _collect_anchor_values(item, out, include_paths=include_paths)


def _evidence_anchors(evidence: list[dict] | None, *,
                      include_paths: bool = True) -> list[str]:
    """Extract stable semantic anchors from evidence, without location noise."""
    anchors: set[str] = set()
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        operation = str(item.get("operation") or "")
        selector = str(item.get("selector") or "")
        if operation:
            anchors.add(f"operation:{operation}")
        if selector:
            anchors.add(f"selector:{selector}")
        snippet = str(item.get("snippet") or "")
        if snippet:
            # String literals are evidence values, not structure; changing a
            # secret/password/literal must not turn the same defect into a
            # new identity.
            code = _QUOTED_RE.sub('""', snippet)
            anchors.update(
                f"ident:{token.lower()}"
                for token in _IDENTIFIER_RE.findall(code)
                if token.lower() not in _IDENTIFIER_STOP
            )
        # REA / vibe evidence keeps a bounded summary; use its entity names.
        # Paths are part of the exact identity, but excluded from semantic
        # identity so a file move does not look like fixed + new.
        summary = item.get("summary")
        if isinstance(summary, dict):
            _collect_anchor_values(summary.get("value"), anchors,
                                   include_paths=include_paths)
            _collect_anchor_values(summary, anchors, include_paths=include_paths)
    return sorted(anchors)


def finding_identity(category: str, root_cause_group: str,
                      evidence: list[dict] | None) -> dict[str, Any]:
    """Build the v2 stable identity for one finding.

    ``fingerprint`` is the site identity: same root cause, same evidence
    path set, same semantic anchors. ``semantic_fingerprint`` drops paths so
    a file move can still be recognized as the same defect. Both are 16-hex
    values and exclude line numbers, snippets, counts and timestamps.
    """
    paths = sorted({
        _normalize_path(item.get("path"))
        for item in (evidence or [])
        if isinstance(item, dict) and item.get("path")
    })
    exact_anchors = _evidence_anchors(evidence, include_paths=True)
    semantic_anchors = _evidence_anchors(evidence, include_paths=False)
    exact_raw = "\x1f".join(("2", category, root_cause_group, "|".join(paths),
                             "|".join(exact_anchors)))
    semantic_raw = "\x1f".join(("2", category, root_cause_group,
                                "|".join(semantic_anchors)))
    return {
        "fingerprint_version": 2,
        "fingerprint": hashlib.sha256(exact_raw.encode("utf-8")).hexdigest()[:16],
        "semantic_fingerprint": hashlib.sha256(
            semantic_raw.encode("utf-8")).hexdigest()[:16],
        # Stored once so the matcher can bridge v1 episodes without access to
        # the old evidence document.
        "legacy_fingerprint": fingerprint_for(
            category, root_cause_group, evidence),
    }


def finding_fingerprint(finding: dict) -> str:
    """Return the exact display/matching fingerprint, or "" for legacy findings.

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


def _identity(finding: dict) -> dict[str, Any]:
    """Normalize a stored finding into a comparable identity."""
    explicit = str(finding.get("fingerprint") or "")
    semantic = str(finding.get("semantic_fingerprint") or "")
    legacy = str(finding.get("legacy_fingerprint") or "")
    try:
        version = int(finding.get("fingerprint_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if semantic:
        version = max(version, 2)
        exact = explicit or semantic
        return {"version": version, "exact": exact, "semantic": semantic,
                "legacy": legacy}
    if explicit:
        return {"version": version or 1, "exact": explicit,
                "semantic": explicit, "legacy": legacy or explicit}
    evidence = finding.get("evidence")
    if evidence:
        legacy = fingerprint_for(finding.get("category", ""),
                                 finding.get("root_cause_group", ""), evidence)
        return {"version": 1, "exact": legacy, "semantic": legacy,
                "legacy": legacy}
    return {"version": 0, "exact": "", "semantic": "", "legacy": ""}


def _same_identity(left: dict, right: dict) -> bool:
    """Match two findings without marking a moved defect as fixed+new."""
    left_id, right_id = _identity(left), _identity(right)
    if left_id["version"] >= 2 and right_id["version"] >= 2:
        return (left_id["exact"] == right_id["exact"]
                or left_id["semantic"] == right_id["semantic"])
    # v2 current vs v1 prior: compare the stored v1 bridge fingerprint.
    if left_id["version"] >= 2 or right_id["version"] >= 2:
        newer, older = ((left_id, right_id) if left_id["version"] >= 2
                        else (right_id, left_id))
        if older["version"] == 1:
            return bool(newer["legacy"]) and newer["legacy"] == older["exact"]
        return finding_key(left) == finding_key(right)
    if left_id["exact"] and right_id["exact"]:
        return left_id["exact"] == right_id["exact"]
    return finding_key(left) == finding_key(right)


def _matches(left: dict, right: dict) -> bool:
    return _same_identity(left, right)


def _take_match(pool: list[dict], finding: dict) -> dict | None:
    """Consume the best match from `pool`, one-to-one.

    Exact v2 fingerprint first, then v2 semantic identity (file move), then
    the v1 bridge fingerprint, and finally root identity for fingerprint-less
    legacy episodes. Two v2 findings at different semantic sites never match.
    """
    identity = _identity(finding)
    if identity["version"] >= 2:
        for index, item in enumerate(pool):
            item_id = _identity(item)
            if item_id["version"] >= 2 and identity["exact"] == item_id["exact"]:
                return pool.pop(index)
        for index, item in enumerate(pool):
            item_id = _identity(item)
            if item_id["version"] >= 2 and identity["semantic"] == item_id["semantic"]:
                return pool.pop(index)
        for index, item in enumerate(pool):
            if _same_identity(item, finding):
                return pool.pop(index)
        return None
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


def _coverage_index(coverage: dict | None) -> tuple[dict[str, str], dict[tuple[str, str], list[str]]]:
    by_rule: dict[str, str] = {}
    by_pair: dict[tuple[str, str], list[str]] = {}
    for item in (coverage or {}).get("rules") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        rule_id = str(item.get("rule_id") or "")
        if rule_id:
            by_rule[rule_id] = status
        pair = (str(item.get("category") or ""), str(item.get("root_cause_group") or ""))
        if pair != ("", ""):
            by_pair.setdefault(pair, []).append(status)
    return by_rule, by_pair


def _finding_covered(finding: dict, coverage: dict | None) -> bool:
    """A prior finding may only become `fixed` if its rule actually ran now."""
    if not isinstance(coverage, dict):
        return True  # legacy callers keep the old pure-function behavior
    by_rule, by_pair = _coverage_index(coverage)
    rule_id = str(finding.get("rule_id") or "")
    if rule_id:
        return by_rule.get(rule_id) == "executed"
    pair = (str(finding.get("category") or ""), str(finding.get("root_cause_group") or ""))
    statuses = by_pair.get(pair) or []
    return any(status == "executed" for status in statuses)


def regression(prior: list[dict], current: list[dict],
               earlier: list[list[dict]] | None = None,
               coverage: dict | None = None) -> dict[str, list[dict]]:
    """Compare two finding sets by stable identity.

    `earlier` is the episode history *before* `prior` (oldest → newest),
    used only for `regressed`: absent in prior, present now, but seen in
    an earlier episode. Missing prior (first audit) returns every bucket
    empty — no prior, no regression judgment.

    `coverage` is the current audit's rule ledger. A prior finding that is
    absent now only becomes `fixed` when its rule is `executed`; otherwise it
    is `unverified` — "not scanned" is not "repaired".
    """
    pool = list(prior)
    known: list[dict] = []
    candidates: list[dict] = []
    for finding in current:
        if _take_match(pool, finding) is not None:
            known.append(finding)
        else:
            candidates.append(finding)
    fixed: list[dict] = []
    unverified: list[dict] = []
    for finding in pool:
        if _finding_covered(finding, coverage):
            fixed.append(finding)
        else:
            unverified.append(finding)
    regressed: list[dict] = []
    new: list[dict] = []
    for finding in candidates:
        if any(_matches(item, finding) for episode in (earlier or [])
               for item in episode):
            regressed.append(finding)
        else:
            new.append(finding)
    return {
        "known": known, "new": new, "fixed": fixed,
        "unverified": unverified, "regressed": regressed,
    }


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
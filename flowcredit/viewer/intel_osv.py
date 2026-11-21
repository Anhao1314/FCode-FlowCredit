"""Intel knowledge base v0: OSV snapshots with provenance, synced on demand.

网络纪律（宪法级）：**审计离线**——PoC 证据声明 network: none，审计过程永远
不碰网。情报同步是**操作员的显式治理动作**（独立端点），把世界漏洞知识
拉成带出处的快照落盘；之后审计与探员**离线读取**这份快照。

快照 = 可验证的第三种记忆：schema 版本 + 来源 + fetched_at + 查询清单 +
完整 OSV 记录。它不是自由文本记忆，错了能追溯、能复算。
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "SCHEMA_VERSION",
    "OSV_SOURCE",
    "go_dependencies_from_facts",
    "query_osv_batch",
    "fetch_osv_record",
    "sync_osv_snapshot",
]

SCHEMA_VERSION = "fcode.intel-osv-snapshot/v1"
OSV_SOURCE = "api.osv.dev"
_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_VULN_URL = "https://api.osv.dev/v1/vulns/"


def go_dependencies_from_facts(facts: dict[str, Any]) -> list[dict[str, str]]:
    """Collect Go dependency rows pinned in REA facts (path + version)."""
    operations = (facts or {}).get("operations") or {}
    op = operations.get("inspect_go_binary") or {}
    rows = (op.get("facts") or {}).get("dependencies") or []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("path") or "").strip()
        version = str(row.get("version") or "").strip()
        if not name or not version:
            continue
        if (name, version) in seen:
            continue
        seen.add((name, version))
        out.append({"name": name, "version": version})
    return out


def _http_post_json(url: str, body: bytes) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_batch(body: bytes) -> dict[str, Any]:
    return _http_post_json(_BATCH_URL, body)


def _http_get_vuln(vuln_id: str) -> dict[str, Any]:
    return _http_get_json(_VULN_URL + vuln_id)


def _query_version(version: str) -> str:
    """OSV 的 Go 生态查询吃裸 semver（v 前缀会被拒）。"""
    return version[1:] if version.startswith("v") else version


def query_osv_batch(
    deps: list[dict[str, str]],
    *,
    fetch_batch: Callable[[bytes], dict[str, Any]] | None = None,
    fetch_vuln: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Query OSV for Go dependency rows; return deduped full vuln records.

    querybatch 只回 id；命中的记录逐条取全文，快照里必须有 affected ranges
    才能被离线 matcher 复算。hits 用请求时的 id 而不是记录回填的 id——
    出处以我们的查询为准，不信返回体自称的身份。
    """
    post = fetch_batch or _http_post_batch
    get = fetch_vuln or _http_get_vuln
    queries = [
        {"package": {"ecosystem": "Go", "name": dep["name"]},
         "version": _query_version(dep["version"])}
        for dep in deps
    ]
    results = post(json.dumps({"queries": queries}).encode("utf-8"))
    ids: list[str] = []
    seen: set[str] = set()
    for item in results.get("results") or []:
        for vuln in item.get("vulns") or []:
            vuln_id = str(vuln.get("id") or "")
            if vuln_id and vuln_id not in seen:
                seen.add(vuln_id)
                ids.append(vuln_id)
    records: list[dict[str, Any]] = []
    for vuln_id in ids:
        records.append(get(vuln_id))
    return {"ids": ids, "records": records}


def sync_osv_snapshot(
    deps: list[dict[str, str]],
    *,
    out_path: Path | str,
    now: str | None = None,
    fetch_batch: Callable[[bytes], dict[str, Any]] | None = None,
    fetch_vuln: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Sync one provenance-stamped snapshot for the given dependency rows.

    每次同步整包重写（同路径替换语义，与解读缓存一致）；写入是原子的。
    返回 {path, queries, hits, snapshot}；hits 是去重后的 OSV id 列表。
    """
    fetched_at = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    queried = query_osv_batch(deps, fetch_batch=fetch_batch, fetch_vuln=fetch_vuln)
    vulns = queried["records"]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "source": OSV_SOURCE,
        "fetched_at": fetched_at,
        "queries": [
            {"ecosystem": "Go", "name": dep["name"],
             "version": _query_version(dep["version"])}
            for dep in deps
        ],
        "vulns": vulns,
    }
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return {
        "path": str(path),
        "queries": len(deps),
        "hits": queried["ids"],
        "snapshot": snapshot,
    }

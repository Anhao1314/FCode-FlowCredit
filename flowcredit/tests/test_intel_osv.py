"""情报知识库 v0：带出处的 OSV 快照同步（纯离线单测，fetch 全注入）。

边界即契约：
1. 依赖收集只认 REA facts 里钉住的行，空行/重复去重；
2. 快照必须带 schema/来源/fetched_at/查询清单——没有出处不落盘；
3. fetch 全部注入：单测永远不碰网。
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from intel_osv import (  # noqa: E402
    SCHEMA_VERSION,
    go_dependencies_from_facts,
    sync_osv_snapshot,
)


def fake_facts() -> dict:
    return {
        "operations": {
            "inspect_go_binary": {
                "facts": {
                    "dependencies": [
                        {"path": "github.com/wailsapp/wails/v2", "version": "v2.12.0"},
                        {"path": "golang.org/x/crypto", "version": "v0.58.0"},
                        {"path": "github.com/wailsapp/wails/v2", "version": "v2.12.0"},
                        {"path": "", "version": "v1.0.0"},
                        {"path": "github.com/google/uuid", "version": ""},
                    ]
                }
            }
        }
    }


def fake_batch(body: bytes) -> dict:
    queries = json.loads(body.decode("utf-8"))["queries"]
    hits = []
    for query in queries:
        if query["package"]["name"].endswith("wails/v2"):
            hits.append({"vulns": [{"id": "GHSA-47hv-j4px-h3c9"}]})
        elif query["package"]["name"] == "golang.org/x/crypto":
            hits.append({"vulns": [{"id": "GO-2026-5932"}]})
        else:
            hits.append({})
    return {"results": hits}


def fake_vuln(vuln_id: str) -> dict:
    return {
        "id": vuln_id,
        "summary": f"fake record for {vuln_id}",
        "affected": [],
    }


@contextlib.contextmanager
def tempfile_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield tmp


class IntelOsvTests(unittest.TestCase):
    def test_collect_deps_dedupes_and_skips_empty_rows(self) -> None:
        deps = go_dependencies_from_facts(fake_facts())
        self.assertEqual(deps, [
            {"name": "github.com/wailsapp/wails/v2", "version": "v2.12.0"},
            {"name": "golang.org/x/crypto", "version": "v0.58.0"},
        ])

    def test_sync_writes_provenance_snapshot(self) -> None:
        with tempfile_dir() as tmp:
            out = pathlib.Path(tmp) / "intel" / "osv" / "osv-demo.json"
            result = sync_osv_snapshot(
                go_dependencies_from_facts(fake_facts()),
                out_path=out,
                now="2026-11-19T00:00:00+00:00",
                fetch_batch=fake_batch,
                fetch_vuln=fake_vuln,
            )
            self.assertEqual(result["hits"],
                             ["GHSA-47hv-j4px-h3c9", "GO-2026-5932"])
            self.assertEqual(result["queries"], 2)
            snapshot = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(snapshot["schema_version"], SCHEMA_VERSION)
            self.assertEqual(snapshot["source"], "api.osv.dev")
            self.assertEqual(snapshot["fetched_at"], "2026-11-19T00:00:00+00:00")
            # 查询清单带出处：v 前缀按 OSV 约定剥掉，原始版本不篡改
            self.assertEqual(
                snapshot["queries"][0],
                {"ecosystem": "Go", "name": "github.com/wailsapp/wails/v2",
                 "version": "2.12.0"},
            )
            # 命中记录全文落盘：离线 matcher 要靠 affected ranges 复算
            self.assertEqual(
                [v["id"] for v in snapshot["vulns"]],
                ["GHSA-47hv-j4px-h3c9", "GO-2026-5932"],
            )

    def test_resync_replaces_snapshot_atomically(self) -> None:
        with tempfile_dir() as tmp:
            out = pathlib.Path(tmp) / "osv-demo.json"
            sync_osv_snapshot(
                [{"name": "github.com/wailsapp/wails/v2", "version": "v2.12.0"}],
                out_path=out, fetch_batch=fake_batch, fetch_vuln=fake_vuln,
            )
            result = sync_osv_snapshot(
                [{"name": "golang.org/x/crypto", "version": "v0.58.0"}],
                out_path=out, fetch_batch=fake_batch, fetch_vuln=fake_vuln,
            )
            snapshot = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(result["hits"], ["GO-2026-5932"])
            self.assertEqual(
                [v["id"] for v in snapshot["vulns"]], ["GO-2026-5932"],
                "重同步整包替换，不留旧记录")
            leftovers = list(out.parent.glob("*.tmp"))
            self.assertEqual(leftovers, [], "原子写入不留临时文件")


if __name__ == "__main__":
    unittest.main()

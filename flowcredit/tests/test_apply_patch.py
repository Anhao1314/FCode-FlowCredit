"""S5 修复落地: apply-patch 端点——冲突检测、原子性、回执与拒绝路径。

用假 handler 直接调用 console_post.post_run_apply_patch:
- 成功:补丁落地、apply.json 回执写入(patch sha256 + 文件清单);
- 冲突:原目录在运行期间改过 → 409,原目录分毫不动;
- 运行未终态 → 409;无补丁 → 404;源目录消失 → 400。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from console_post import post_run_apply_patch  # noqa: E402


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(path: Path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")


def _make_patch(base_files: dict[str, str], changed_files: dict[str, str]) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch"
        _init_repo(scratch, base_files)
        for rel, text in changed_files.items():
            (scratch / rel).write_text(text, encoding="utf-8")
        _git(scratch, "add", "-A")
        diff = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=scratch,
                              capture_output=True, text=True, check=True)
        return diff.stdout


class FakeHandler:
    manager = None

    def __init__(self, manager):
        type(self).manager = manager

    def _json(self, payload, status=200):
        return (payload, status)


def _setup(tmp: Path, *, patch: str, source_files: dict[str, str] | None = None,
           finished: bool = True):
    runs_dir = tmp / "runs"
    run_dir = runs_dir / "run-1"
    run_dir.mkdir(parents=True)
    source = tmp / "project"
    _init_repo(source, source_files if source_files is not None else {"app.py": "old\n"})
    (run_dir / "project.json").write_text(json.dumps({
        "source": str(source),
        "filesChanged": [{"status": "M", "path": "app.py"}],
    }), encoding="utf-8")
    if patch:
        (run_dir / "changes.patch").write_text(patch, encoding="utf-8")
    run = SimpleNamespace(id="run-1", finished=finished)
    manager = SimpleNamespace(runs_dir=runs_dir, get=lambda rid: run if rid == "run-1" else None)
    return manager, run_dir, source


class ApplyPatchTest(unittest.TestCase):
    def test_success_applies_and_writes_receipt(self) -> None:
        patch = _make_patch({"app.py": "old\n"}, {"app.py": "new\n"})
        with tempfile.TemporaryDirectory() as tmp:
            manager, run_dir, source = _setup(Path(tmp), patch=patch)
            payload, status = post_run_apply_patch(FakeHandler(manager), "run-1")
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["applied"])
            self.assertEqual((source / "app.py").read_text(), "new\n")
            receipt = json.loads((run_dir / "apply.json").read_text())
            self.assertEqual(receipt["source"], str(source))
            self.assertEqual(len(receipt["patch_sha256"]), 64)
            self.assertEqual(receipt["files"][0]["path"], "app.py")

    def test_conflict_refuses_and_leaves_source_untouched(self) -> None:
        patch = _make_patch({"app.py": "old\n"}, {"app.py": "new\n"})
        with tempfile.TemporaryDirectory() as tmp:
            manager, run_dir, source = _setup(Path(tmp), patch=patch)
            # 原目录在运行期间被改过 → check 必须失败
            (source / "app.py").write_text("changed meanwhile\n", encoding="utf-8")
            payload, status = post_run_apply_patch(FakeHandler(manager), "run-1")
            self.assertEqual(status, 409, payload)
            self.assertIn("does not apply", payload["error"])
            self.assertEqual((source / "app.py").read_text(), "changed meanwhile\n")
            self.assertFalse((run_dir / "apply.json").exists(), "失败不落回执")

    def test_refuse_running_run(self) -> None:
        patch = _make_patch({"app.py": "old\n"}, {"app.py": "new\n"})
        with tempfile.TemporaryDirectory() as tmp:
            manager, _, _ = _setup(Path(tmp), patch=patch, finished=False)
            payload, status = post_run_apply_patch(FakeHandler(manager), "run-1")
            self.assertEqual(status, 409)
            self.assertIn("in progress", payload["error"])

    def test_refuse_no_patch_and_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager, _, source = _setup(Path(tmp), patch="")
            payload, status = post_run_apply_patch(FakeHandler(manager), "run-1")
            self.assertEqual(status, 404)
            self.assertIn("no changes.patch", payload["error"])
        with tempfile.TemporaryDirectory() as tmp:
            patch = _make_patch({"app.py": "old\n"}, {"app.py": "new\n"})
            manager, run_dir, source = _setup(Path(tmp), patch=patch)
            (run_dir / "project.json").write_text(
                json.dumps({"source": str(Path(tmp) / "gone")}), encoding="utf-8")
            payload, status = post_run_apply_patch(FakeHandler(manager), "run-1")
            self.assertEqual(status, 400)
            self.assertIn("unavailable", payload["error"])


if __name__ == "__main__":
    unittest.main()

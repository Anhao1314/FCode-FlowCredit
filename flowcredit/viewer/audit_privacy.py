"""审计数据静止加密（opt-in）：目标内容不裸奔在磁盘上。

边界（治理口径）：

1. **只加密目标数据**（快照目录：网页全部资源/软件副本），审计包与证据
   元数据保持可读——验证不需要解密全文；
2. **密钥本地 0600**（FCODE_CONFIG_DIR/audit-key），可用 FCODE_AUDIT_KEY_FILE
   指向操作员自管密钥；密钥缺失时如实报错，绝不静默跳过；
3. 加密 = tar.gz + openssl AES-256-CBC(PBKDF2)。加密后删除明文目录，
   留 `<name>.tar.enc` 与 `.encrypted` 标记；解密工具供重审计/复核使用。
4. 开关：FCODE_AUDIT_ENCRYPT=1 时由治理管线在审计完成后调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

__all__ = ["encrypt_dir", "decrypt_dir", "prune_archives", "encryption_enabled", "key_path"]


def encryption_enabled() -> bool:
    return os.environ.get("FCODE_AUDIT_ENCRYPT", "").strip() in ("1", "true", "yes")


def key_path() -> Path:
    from console_common import app_config_dir  # viewer 侧复用现有配置目录

    override = os.environ.get("FCODE_AUDIT_KEY_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return app_config_dir() / "audit-key"


def _ensure_key() -> Path:
    path = key_path()
    if path.is_file():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(32).hex().encode())  # 64 hex chars
    os.chmod(path, 0o600)
    return path


def encrypt_dir(directory: Path | str, *, archive_name: str | None = None) -> Path:
    """把目录加密为同级的 `<archive_name>`（默认 `<name>.tar.enc`）并删除明文。

    ``archive_name`` 供治理管线按审计版本命名（`<site>-<audit_id>.tar.enc`），
    重爬多次时旧版本归档不被覆盖——历史可回溯，配合 prune_archives 做保留期。
    """
    src = Path(directory)
    if not src.is_dir():
        raise ValueError(f"not a directory: {src}")
    key = _ensure_key()
    archive = src.with_name(archive_name or (src.name + ".tar.enc"))
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        with tarfile.open(temp_path, "w:gz") as tar:
            tar.add(src, arcname=src.name)
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
             "-pass", f"file:{key}", "-in", str(temp_path), "-out", str(archive)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl enc failed: {(proc.stderr or '')[:200]}")
    finally:
        temp_path.unlink(missing_ok=True)
    shutil.rmtree(src)
    marker = archive.parent / (archive.name + ".encrypted")
    marker.write_text(
        "target content encrypted at rest; key: " + str(key) + "\n", encoding="utf-8")
    return archive


def prune_archives(directory: Path | str, *, keep: int = 5) -> list[Path]:
    """保留最近 ``keep`` 份版本化归档，其余删除（保留期策略）。返回被删列表。"""
    src = Path(directory)
    pattern = src.name + "-*.tar.enc"
    archives = sorted(src.parent.glob(pattern),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    for old in archives[keep:]:
        old.unlink(missing_ok=True)
        marker = old.parent / (old.name + ".encrypted")
        marker.unlink(missing_ok=True)
        removed.append(old)
    return removed


def decrypt_dir(archive: Path | str, dest_parent: Path | str | None = None) -> Path:
    """解密 `<name>.tar.enc` 回目录（重审计/复核用）。"""
    enc = Path(archive)
    if not enc.is_file():
        raise ValueError(f"not a file: {enc}")
    key = _ensure_key()
    parent = Path(dest_parent) if dest_parent else enc.parent
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
             "-pass", f"file:{key}", "-in", str(enc), "-out", str(temp_path)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl dec failed: {(proc.stderr or '')[:200]}")
        with tarfile.open(temp_path, "r:gz") as tar:
            tar.extractall(parent)
    finally:
        temp_path.unlink(missing_ok=True)
    name = enc.name[:-len(".tar.enc")]
    return parent / name

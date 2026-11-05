# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the same stdlib console used in development."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
VIEWER = ROOT / "viewer"
SRC = ROOT / "src"


def source_tree(source: Path, destination: str):
    files = []
    for path in source.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or "tests" in path.parts:
            continue
        if path.suffix in {".pyc", ".pyo", ".sqlite", ".sqlite-wal", ".sqlite-shm"}:
            continue
        if path.name.startswith("chat-config.json") or path.name == "mcp.json":
            continue
        relative_parent = path.parent.relative_to(source)
        files.append((str(path), str(Path(destination) / relative_parent)))
    return files


a = Analysis(
    [str(ROOT / "desktop" / "app.py")],
    pathex=[str(SRC), str(VIEWER)],
    binaries=[],
    datas=source_tree(SRC, "src") + source_tree(VIEWER, "viewer"),
    hiddenimports=collect_submodules("flowagent") + [
        "server", "console_common", "console_manager", "console_runtime",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FCode",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FCode",
)
app = BUNDLE(
    coll,
    name="FCode.app",
    icon=None,
    bundle_identifier="local.fcode.desktop",
)

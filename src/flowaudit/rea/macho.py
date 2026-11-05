"""Minimal Mach-O load-command reader for library-validation evidence.

This is not a general Mach-O parser. It answers one audit question:

* which dylibs does this executable load directly (``LC_LOAD_DYLIB`` /
  ``LC_LOAD_WEAK_DYLIB`` / ``LC_REEXPORT_DYLIB``);
* which ``LC_RPATH`` search paths does it declare;
* are those loads Apple system libraries, in-bundle files, or external
  paths;
* what Team ID signs each non-system file the host can resolve.

``dlopen``/``NSBundle`` at runtime are **not** observed here. A clean
static graph is evidence that no *declared* load requires a non-team
library; it is not proof that no runtime path exists. The report keeps
that distinction explicit.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["analyze_library_validation", "parse_macho"]

_MH_MAGIC = 0xFEEDFACE
_MH_MAGIC_64 = 0xFEEDFACF
_MH_CIGAM = 0xCEFAEDFE
_MH_CIGAM_64 = 0xCFFAEDFE
_FAT_MAGIC = 0xCAFEBABE
_FAT_CIGAM = 0xBEBAFECA

_LC_LOAD_DYLIB = 0xC
_LC_ID_DYLIB = 0xD
_LC_LOAD_WEAK_DYLIB = 0x80000018
_LC_RPATH = 0x8000001C
_LC_REEXPORT_DYLIB = 0x8000001F

_CPU_TYPES = {
    0x0100000C: "arm64",
    0x01000007: "x86_64",
    12: "arm",
    7: "x86",
    0x01000012: "arm64_32",
}

_MAX_LOAD_COMMANDS = 4096
_MAX_NAME_BYTES = 4096
_MAX_HEADER_BYTES = 4 * 1024 * 1024
_MAX_NESTED_MACHO = 200


def _read_name(data: bytes, command_offset: int, cmdsize: int, string_offset: int) -> str | None:
    if string_offset <= 0 or string_offset >= cmdsize:
        return None
    start = command_offset + string_offset
    end = min(command_offset + cmdsize, start + _MAX_NAME_BYTES)
    raw = data[start:end].split(b"\0", 1)[0]
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _parse_thin(data: bytes) -> dict[str, Any]:
    if len(data) < 28:
        raise ValueError("Mach-O header is truncated")
    magic_le = struct.unpack_from("<I", data, 0)[0]
    magic_be = struct.unpack_from(">I", data, 0)[0]
    if magic_le in (_MH_MAGIC, _MH_MAGIC_64):
        endian = "<"
        magic = magic_le
    elif magic_be in (_MH_MAGIC, _MH_MAGIC_64):
        endian = ">"
        magic = magic_be
    else:
        raise ValueError(f"not a Mach-O header: 0x{magic_le:08x}")
    bits = 64 if magic == _MH_MAGIC_64 else 32
    header_size = 32 if bits == 64 else 28
    if len(data) < header_size:
        raise ValueError("Mach-O header is truncated")
    if bits == 64:
        _, cputype, _, filetype, ncmds, sizeofcmds, _, _ = struct.unpack_from(
            endian + "IiiIIIII", data, 0)
    else:
        _, cputype, _, filetype, ncmds, sizeofcmds, _ = struct.unpack_from(
            endian + "IiiIIII", data, 0)
    if ncmds > _MAX_LOAD_COMMANDS or sizeofcmds > _MAX_HEADER_BYTES:
        raise ValueError("Mach-O load command table exceeds the audit bound")
    offset = header_size
    loaded: list[str] = []
    rpaths: list[str] = []
    identity: str | None = None
    for _ in range(ncmds):
        if offset + 8 > len(data):
            break
        cmd, cmdsize = struct.unpack_from(endian + "II", data, offset)
        if cmdsize < 8 or offset + cmdsize > len(data):
            break
        if cmd in (_LC_LOAD_DYLIB, _LC_LOAD_WEAK_DYLIB, _LC_REEXPORT_DYLIB, _LC_ID_DYLIB):
            string_offset = struct.unpack_from(endian + "I", data, offset + 8)[0]
            name = _read_name(data, offset, cmdsize, string_offset)
            if name:
                if cmd == _LC_ID_DYLIB:
                    identity = name
                else:
                    loaded.append(name)
        elif cmd == _LC_RPATH:
            string_offset = struct.unpack_from(endian + "I", data, offset + 8)[0]
            path = _read_name(data, offset, cmdsize, string_offset)
            if path:
                rpaths.append(path)
        offset += cmdsize
    return {
        "format": "macho",
        "bits": bits,
        "byte_order": "little" if endian == "<" else "big",
        "architecture": _CPU_TYPES.get(cputype, hex(cputype)),
        "filetype": filetype,
        "loaded": loaded,
        "rpaths": rpaths,
        "id": identity,
    }


def _parse_fat(data: bytes) -> dict[str, Any] | None:
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic not in (_FAT_MAGIC, _FAT_CIGAM):
        return None
    nfat = struct.unpack_from(">I", data, 4)[0]
    if nfat > 32:
        return None
    offset = 8
    slices: list[dict[str, Any]] = []
    for _ in range(nfat):
        if offset + 20 > len(data):
            break
        cputype, _, slice_offset, slice_size, _ = struct.unpack_from(">iiIII", data, offset)
        if slice_offset + slice_size > len(data):
            break
        slices.append({
            "architecture": _CPU_TYPES.get(cputype, hex(cputype)),
            "offset": slice_offset,
            "size": slice_size,
        })
        offset += 20
    if not slices:
        return None
    preferred = next(
        (item for item in slices if item["architecture"] in ("arm64", "x86_64")),
        slices[0],
    )
    parsed = _parse_thin(data[preferred["offset"]:preferred["offset"] + preferred["size"]])
    parsed["fat"] = True
    parsed["slices"] = slices
    return parsed


def parse_macho(path: str | Path) -> dict[str, Any]:
    """Read one thin/fat Mach-O load-command table."""
    target = Path(path)
    with target.open("rb") as handle:
        data = handle.read(_MAX_HEADER_BYTES)
    if len(data) < 8:
        raise ValueError("file is too small to be Mach-O")
    fat = _parse_fat(data)
    return fat if fat is not None else _parse_thin(data)


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in (
        struct.pack("<I", _MH_MAGIC), struct.pack("<I", _MH_MAGIC_64),
        struct.pack(">I", _MH_MAGIC), struct.pack(">I", _MH_MAGIC_64),
        struct.pack(">I", _FAT_MAGIC), struct.pack(">I", _FAT_CIGAM),
    )


def _codesign_identity(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["codesign", "-dv", "--verbose=4", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as error:
        return {"status": "unavailable", "reason": str(error)}
    text = (completed.stderr or "") + "\n" + (completed.stdout or "")
    if completed.returncode != 0:
        return {"status": "invalid", "reason": text.strip().splitlines()[-1:]}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return {
        "status": "observed",
        "identifier": values.get("Identifier"),
        "team_id": values.get("TeamIdentifier"),
        "authority": values.get("Authority"),
        "runtime": values.get("Runtime Version"),
    }


def _is_system(path: str) -> bool:
    return path.startswith(("/System/", "/usr/lib/", "/Library/Apple/"))


def _is_user_writable(path: Path) -> bool:
    text = str(path)
    home = str(Path.home())
    return text.startswith(("/Users/", "/tmp/", "/private/tmp/", "/var/folders/")) \
        or text.startswith(home + "/")


def _resolve(name: str, executable: Path, app_root: Path, rpaths: list[str]) -> Path | None:
    if name.startswith("@executable_path/"):
        return executable.parent / name[len("@executable_path/"):]
    if name.startswith("@loader_path/"):
        return executable.parent / name[len("@loader_path/"):]
    if name.startswith("@rpath/"):
        suffix = name[len("@rpath/"):]
        candidates = [
            Path(item.replace("@executable_path", str(executable.parent))
                 .replace("@loader_path", str(executable.parent)))
            for item in rpaths
        ]
        candidates.extend([
            app_root / "Contents" / "Frameworks",
            app_root / "Contents" / "Resources",
        ])
        for base in candidates:
            candidate = base / suffix
            if candidate.exists():
                return candidate
        return None
    return Path(name)


def _nested_macho(app_root: Path, executable: Path) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(app_root / "Contents"):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in {".git", "dist", "build", "coverage"}
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path == executable or not _is_macho(path):
                continue
            identity = _codesign_identity(path)
            found.append({
                "path": str(path.relative_to(app_root)),
                "team_id": identity.get("team_id"),
                "same_team": None,
                "codesign": identity,
            })
            if len(found) >= _MAX_NESTED_MACHO:
                return found
    return found


def analyze_library_validation(
    app_root: Path,
    executable: Path,
    entitlements: list[str],
) -> dict[str, Any]:
    """Summarize the declared load graph against the main process Team ID."""
    app_root = Path(app_root)
    executable = Path(executable)
    entitlement_present = "com.apple.security.cs.disable-library-validation" in entitlements
    identity = _codesign_identity(executable)
    main_team = identity.get("team_id")
    try:
        parsed = parse_macho(executable)
    except (OSError, ValueError) as error:
        return {
            "status": "unavailable",
            "reason": str(error),
            "entitlement_present": entitlement_present,
            "main_team_id": main_team,
        }
    loaded: list[dict[str, Any]] = []
    non_team: list[dict[str, Any]] = []
    user_writable: list[str] = []
    unresolved: list[str] = []
    for name in parsed.get("loaded", []):
        if _is_system(name):
            loaded.append({
                "path": name, "kind": "system", "team_id": "apple",
                "same_team": True, "user_writable": False,
            })
            continue
        resolved = _resolve(name, executable, app_root, parsed.get("rpaths", []))
        if resolved is None or not resolved.exists():
            unresolved.append(name)
            loaded.append({
                "path": name, "kind": "unresolved", "team_id": None,
                "same_team": None, "user_writable": False,
            })
            continue
        path_identity = _codesign_identity(resolved)
        team_id = path_identity.get("team_id")
        writable = _is_user_writable(resolved)
        record = {
            "path": name,
            "resolved": str(resolved),
            "kind": "bundle" if app_root in resolved.parents else "external",
            "team_id": team_id,
            "same_team": team_id == main_team if team_id else None,
            "user_writable": writable,
        }
        loaded.append(record)
        if team_id and team_id != main_team:
            non_team.append(record)
        if writable:
            user_writable.append(str(resolved))
    nested = _nested_macho(app_root, executable)
    for item in nested:
        item["same_team"] = item.get("team_id") == main_team if item.get("team_id") else None
    if not entitlement_present:
        verdict = "not_applicable"
    elif non_team or user_writable:
        verdict = "requires_review"
    elif unresolved:
        verdict = "unknown"
    else:
        verdict = "no_static_justification"
    return {
        "status": "observed",
        "entitlement_present": entitlement_present,
        "main_team_id": main_team,
        "main_identity": identity,
        "loaded": loaded,
        "loaded_count": len(loaded),
        "system_count": sum(1 for item in loaded if item["kind"] == "system"),
        "non_team_signed": non_team,
        "user_writable_load_paths": user_writable,
        "unresolved": unresolved,
        "rpaths": parsed.get("rpaths", []),
        "nested": nested,
        "verdict": verdict,
        "limitations": [
            "Only declared mach-o load commands are inspected; runtime dlopen/NSBundle paths are not observed.",
            "Nested Mach-O processes have their own entitlements and are listed for context, not merged into the main-process verdict.",
        ],
    }
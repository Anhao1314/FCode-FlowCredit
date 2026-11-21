"""Settings loader (S2): permissions + hooks from four disk sources.

Mirrors the dissected harness's settings layering, with the product's own
paths:

* ``managed``  — explicit path or ``FCODE_MANAGED_SETTINGS`` (org policy);
* ``project``  — ``<root>/.fcode/settings.json``;
* ``user``     — ``~/.fcode/settings.json``;
* ``local``    — ``<root>/.fcode/settings.local.json``.

Rules load in managed→local order, so policy evaluation can keep managed
first. Invalid entries never crash a run: they are skipped and reported as
warnings, because a malformed operator file must not brick the agent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .hooks import HOOK_EVENTS, HookRunner, HookSpec
from .permissions import (
    Decision,
    PermissionPolicy,
    PermissionRule,
    RuleSyntaxError,
)

__all__ = [
    "LoadedSettings",
    "SETTINGS_SOURCES",
    "load_settings",
    "settings_paths",
]

SETTINGS_SOURCES = ("managed", "project", "user", "local")


@dataclass
class LoadedSettings:
    rules: dict[str, list[PermissionRule]] = field(
        default_factory=lambda: {"allow": [], "ask": [], "deny": []})
    hooks: list[HookSpec] = field(default_factory=list)
    mode: str = "default"
    bypass_available: bool = True
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def policy(
        self,
        *,
        mode: str | None = None,
        approver: Callable[[str, Mapping[str, Any], str], bool] | None = None,
        bypass_available: bool | None = None,
    ) -> PermissionPolicy:
        return PermissionPolicy(
            mode=mode or self.mode,
            allow=self.rules["allow"],
            ask=self.rules["ask"],
            deny=self.rules["deny"],
            bypass_available=(
                self.bypass_available if bypass_available is None
                else bypass_available),
            approver=approver,
        )

    def hook_runner(
        self,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        execute: Callable[[str, bytes, float], tuple[int, str, str]] | None = None,
    ) -> HookRunner:
        return HookRunner(
            self.hooks, cwd=cwd, env=env, execute=execute)


def settings_paths(
    root: str | os.PathLike[str],
    *,
    home: str | os.PathLike[str] | None = None,
    managed: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    environment = os.environ if env is None else env
    managed_path = managed or environment.get("FCODE_MANAGED_SETTINGS")
    base = Path(root)
    home_path = Path(home) if home is not None else Path.home()
    paths: dict[str, Path] = {
        "project": base / ".fcode" / "settings.json",
        "user": home_path / ".fcode" / "settings.json",
        "local": base / ".fcode" / "settings.local.json",
    }
    if managed_path:
        paths["managed"] = Path(managed_path)
    return paths


def load_settings(
    root: str | os.PathLike[str],
    *,
    home: str | os.PathLike[str] | None = None,
    managed: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> LoadedSettings:
    loaded = LoadedSettings()
    paths = settings_paths(root, home=home, managed=managed, env=env)
    mode_set = False
    for source in SETTINGS_SOURCES:
        path = paths.get(source)
        if path is None:
            continue
        document = _read_document(path, source, loaded.warnings)
        if document is None:
            continue
        loaded.sources.append(source)
        _load_permissions(document, source, loaded)
        _load_hooks(document, source, loaded)
        permissions = document.get("permissions") or {}
        mode = str(permissions.get("defaultMode") or "")
        if mode and not mode_set:
            loaded.mode = mode
            mode_set = True
        if str(permissions.get("disableBypassPermissionsMode") or "") not in (
                "", "0", "False", "false"):
            loaded.bypass_available = False
        elif permissions.get("disableBypassPermissionsMode") is True:
            loaded.bypass_available = False
    return loaded


def _read_document(path: Path, source: str,
                   warnings: list[str]) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        warnings.append(f"{source}: cannot read {path}: {error}")
        return None
    try:
        document = json.loads(raw)
    except ValueError as error:
        warnings.append(f"{source}: invalid JSON in {path}: {error}")
        return None
    if not isinstance(document, dict):
        warnings.append(f"{source}: {path} must contain a JSON object")
        return None
    return document


def _load_permissions(document: Mapping[str, Any], source: str,
                      loaded: LoadedSettings) -> None:
    permissions = document.get("permissions") or {}
    if not isinstance(permissions, Mapping):
        loaded.warnings.append(f"{source}: permissions must be an object")
        return
    for behavior in ("allow", "ask", "deny"):
        entries: Iterable[Any] = permissions.get(behavior) or []
        if isinstance(entries, str):
            entries = [entries]
        for entry in entries:
            try:
                loaded.rules[behavior].append(
                    PermissionRule.parse(str(entry), behavior=behavior,
                                         source=source))
            except RuleSyntaxError as error:
                loaded.warnings.append(f"{source}: {error}")


def _load_hooks(document: Mapping[str, Any], source: str,
                loaded: LoadedSettings) -> None:
    hooks = document.get("hooks") or {}
    if not isinstance(hooks, Mapping):
        loaded.warnings.append(f"{source}: hooks must be an object")
        return
    for event, groups in hooks.items():
        if event not in HOOK_EVENTS:
            loaded.warnings.append(f"{source}: unknown hook event {event!r}")
            continue
        if not isinstance(groups, list):
            loaded.warnings.append(f"{source}: hooks.{event} must be a list")
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                loaded.warnings.append(f"{source}: hooks.{event} entry must be an object")
                continue
            matcher = str(group.get("matcher") or "")
            entries = group.get("hooks") or []
            for entry in entries:
                if not isinstance(entry, Mapping):
                    loaded.warnings.append(
                        f"{source}: hooks.{event} hook must be an object")
                    continue
                if str(entry.get("type") or "command") != "command":
                    loaded.warnings.append(
                        f"{source}: unsupported hook type {entry.get('type')!r}")
                    continue
                command = str(entry.get("command") or "")
                timeout_s = entry.get("timeout", 60)
                try:
                    hook = HookSpec(
                        event=event,
                        command=command,
                        matcher=matcher,
                        timeout_ms=int(float(timeout_s) * 1000),
                        source=source,
                    )
                except (TypeError, ValueError) as error:
                    loaded.warnings.append(f"{source}: {error}")
                    continue
                loaded.hooks.append(hook)

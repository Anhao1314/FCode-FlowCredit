"""Check protocol, registry and the adapters over existing motion helpers.

The heavy lifting is **reused, not rewritten**:

* ``analyze_motion`` / ``merge_quality`` from ``tools/general/motion_quality.py``
  provide the deterministic text invariants and geometry verdict;
* the host's ``viewer/motion_frames.py::MotionProbe`` can be injected as any
  object exposing ``probe(path) -> dict`` (scripted proposers inject a plain
  dict or a callable instead, so no browser is needed offline);
* ``ArtifactStore.scene_nonempty()`` is the scene-empty detector.

Results of artifact-derived checks are cached by artifact sha256: a check is a
pure function of the bytes, so re-running it for the same revision is free.
Stateful checks (result written, delivery verification) are deliberately not
cached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol

from ..tools.general.motion_quality import analyze_motion, merge_quality

__all__ = [
    "CheckResult",
    "CheckOutcome",
    "CheckContext",
    "Check",
    "CheckRegistry",
]

#: Structural hooks the static/geometry checks require in the artifact.
REQUIRED_HOOKS = ("wheel", "leg", "hip", "foot")
SUPPORT_HOOKS = ("pedal", "deck", "support")
SCENERY_HOOKS = ("scenery", "background")

_ISSUE_LIMIT = 5
_CLASS_PATTERN = r"class\s*=\s*[\"'][^\"']*(?<![\w-])({})(?![\w-])"


@dataclass(frozen=True)
class CheckResult:
    """One check's verdict against one artifact revision."""

    name: str
    passed: bool
    status: str                      # "pass" | "fail" | "unverified"
    score: int = 0
    issues: tuple[str, ...] = ()
    required_fixes: tuple[Any, ...] = ()
    coverage: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)
    artifact_sha256: str = ""
    cached: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "status": self.status,
            "score": self.score,
            "issues": list(self.issues[:_ISSUE_LIMIT]),
            "cached": self.cached,
        }


@dataclass(frozen=True)
class CheckOutcome:
    """All exit checks for one stage, aggregated."""

    passed: bool
    results: tuple[CheckResult, ...]
    status: str
    score: int
    issues: tuple[str, ...]
    required_fixes: tuple[Any, ...]
    coverage: dict[str, Any]
    quality: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status,
            "score": self.score,
            "checks": [item.summary() for item in self.results],
        }


@dataclass(frozen=True)
class CheckContext:
    """Everything a check may read. No model-authored obligations live here."""

    store: Any
    artifact: Any
    manifest: Any
    stage: Any
    result_written: bool = False


class Check(Protocol):
    """A check is a callable of ``(context, registry) -> CheckResult``."""

    def __call__(self, context: CheckContext, registry: "CheckRegistry") -> CheckResult: ...


def _has_class(html: str, name: str) -> bool:
    matcher = re.compile(_CLASS_PATTERN.format(re.escape(name)), re.IGNORECASE)
    return bool(matcher.search(html))


class CheckRegistry:
    """Named checks, cached by artifact hash, plus the motion_check journal."""

    def __init__(
        self,
        *,
        probe: Any = None,
        verify: Callable[[str], Any] | None = None,
    ) -> None:
        self._fns: dict[str, Callable[[CheckContext, "CheckRegistry"], CheckResult]] = {}
        self._cacheable: set[str] = set()
        self._cache: dict[tuple[str, str], CheckResult] = {}
        self._motion_check: dict[str, CheckResult] = {}
        self._quality: dict[str, dict[str, Any]] = {}
        self.probe = probe
        self.verify = verify

    def register(
        self,
        name: str,
        fn: Callable[[CheckContext, "CheckRegistry"], CheckResult],
        *,
        cache: bool = True,
    ) -> "CheckRegistry":
        self._fns[name] = fn
        if cache:
            self._cacheable.add(name)
        return self

    def run(self, names: tuple[str, ...], context: CheckContext) -> CheckOutcome:
        results: list[CheckResult] = []
        for name in names:
            key = (name, context.artifact.sha256)
            if name in self._cacheable and key in self._cache:
                results.append(replace(self._cache[key], cached=True))
                continue
            fn = self._fns.get(name)
            if fn is None:
                result = CheckResult(
                    name=name, passed=False, status="unverified",
                    issues=(f"check {name!r} is not registered",),
                    artifact_sha256=context.artifact.sha256,
                )
            else:
                result = fn(context, self)
            if name in self._cacheable:
                self._cache[key] = result
            results.append(result)
        return _aggregate(results, sha=context.artifact.sha256, registry=self)

    # ------------------------------------------------------------- motion gate

    def resolve_probe(self, context: CheckContext) -> dict[str, Any] | None:
        """Accept a dict, a callable, or the host's MotionProbe object."""
        probe = self.probe
        if probe is None:
            return None
        try:
            if isinstance(probe, dict):
                return dict(probe)
            if callable(probe):
                raw = probe(context.artifact.path)
            elif hasattr(probe, "probe"):
                raw = probe.probe(context.artifact.path)
            else:
                return None
        except Exception as error:  # noqa: BLE001 — an unavailable probe is unverified
            return {"ok": False, "error": str(error)[:300]}
        return raw if isinstance(raw, dict) else None

    def motion_check(self, context: CheckContext) -> CheckResult:
        """Compute (or reuse) the merged quality verdict for this artifact hash.

        This is the single source the stage machine and the delivery gate both
        consult: a pass recorded here is bound to the artifact's sha256, so any
        later edit invalidates it automatically.
        """
        sha = context.artifact.sha256
        if sha in self._motion_check:
            return replace(self._motion_check[sha], cached=True)
        base = analyze_motion(str(context.artifact.content))
        raw = self.resolve_probe(context)
        if raw is None:
            merged = merge_quality(base, None)
        elif "status" in raw and "coverage" in raw:
            # Scripted verdict: a fake probe may speak the merged shape directly.
            merged = dict(raw)
        else:
            merged = merge_quality(base, raw)
        return self.record_motion(context, merged)

    def prime_motion(self, context: CheckContext, verdict: dict[str, Any]) -> CheckResult:
        """Reuse a Harness motion_check observation for this exact artifact.

        Work has already rendered the artifact inside its ordinary tool event.
        Feeding that observation here avoids a second browser probe and makes
        the stage check and the tool observation the same fact, not two samples.
        """
        merged = dict(verdict)
        if "status" not in merged or "coverage" not in merged:
            merged = merge_quality(analyze_motion(str(context.artifact.content)), merged)
        return self.record_motion(context, merged)

    def record_motion(self, context: CheckContext, merged: dict[str, Any]) -> CheckResult:
        sha = context.artifact.sha256
        merged = dict(merged)
        merged.pop("frames", None)
        merged.pop("contact", None)
        merged.pop("foot_pedal_details", None)
        coverage = dict(merged.get("coverage") or {})
        passed = merged.get("status") == "pass" and bool(coverage.get("complete"))
        issues = tuple(str(item) for item in (merged.get("issues") or []))[:_ISSUE_LIMIT]
        fixes = tuple(merged.get("required_fixes") or [])[:_ISSUE_LIMIT]
        result = CheckResult(
            name="motion_check",
            passed=passed,
            status=str(merged.get("status") or "unverified"),
            score=int(merged.get("score") or 0),
            issues=issues,
            required_fixes=fixes,
            coverage=coverage,
            details={"quality": merged},
            artifact_sha256=sha,
        )
        self._motion_check[sha] = result
        self._quality[sha] = merged
        return result

    def motion_check_pass(self, sha256: str) -> bool:
        """True only when a recorded pass covers this exact artifact hash."""
        result = self._motion_check.get(sha256)
        if result is None:
            return False
        return bool(result.passed and (result.coverage or {}).get("complete"))

    def quality_for(self, sha256: str) -> dict[str, Any] | None:
        return self._quality.get(sha256)

    def record_quality(self, sha256: str, quality: dict[str, Any]) -> None:
        """Publish a bounded final host verdict for the same artifact hash."""
        bounded = dict(quality)
        bounded.pop("frames", None)
        bounded.pop("contact", None)
        bounded.pop("foot_pedal_details", None)
        self._quality[sha256] = bounded

    def run_verifier(self, context: CheckContext) -> dict[str, Any]:
        if self.verify is None:
            # No declared command does not mean "verification skipped = pass".
            # The current artifact still needs its own passing motion verdict.
            return {
                "ok": self.motion_check_pass(context.artifact.sha256),
                "skipped": False,
                "basis": "current_artifact_motion_check",
            }
        try:
            verdict = self.verify(context.artifact.path)
        except Exception as error:  # noqa: BLE001 — a broken verifier is a failed check
            return {"ok": False, "error": str(error)[:300]}
        if isinstance(verdict, dict):
            ok = bool(verdict.get("ok", verdict.get("exit_code", 1) == 0))
            return {"ok": ok, "detail": str(verdict)[:300]}
        return {"ok": bool(verdict)}


# ------------------------------------------------------------------- checks

def _scene_nonempty(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    ok = bool(context.store.scene_nonempty())
    return CheckResult(
        name="scene.nonempty",
        passed=ok,
        status="pass" if ok else "fail",
        score=100 if ok else 0,
        issues=() if ok else ("scene is empty: fill <!-- SECTION: scene --> with SVG elements",),
        required_fixes=() if ok else ({
            "code": "SCENE_EMPTY", "selector": "#scene",
            "message": "add at least one SVG element inside <svg id=\"scene\">",
        },),
        artifact_sha256=context.artifact.sha256,
    )


def _hooks_all(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    html = str(context.artifact.content)
    missing = [name for name in REQUIRED_HOOKS if not _has_class(html, name)]
    if not any(_has_class(html, name) for name in SUPPORT_HOOKS):
        missing.append("pedal|deck|support")
    if not any(_has_class(html, name) for name in SCENERY_HOOKS):
        missing.append("scenery|background")
    ok = not missing
    return CheckResult(
        name="hooks.all",
        passed=ok,
        status="pass" if ok else "fail",
        score=100 if ok else max(0, 100 - 20 * len(missing)),
        issues=() if ok else (f"missing hooks: {', '.join(missing)}",),
        required_fixes=tuple(
            {"code": "MISSING_HOOK", "selector": f".{name}", "message": f"add a .{name} element"}
            for name in missing[:_ISSUE_LIMIT]
        ),
        artifact_sha256=context.artifact.sha256,
    )


def _static_pass(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    static = analyze_motion(str(context.artifact.content))
    issues = tuple(str(item) for item in static["issues"])[:_ISSUE_LIMIT]
    ok = static["status"] == "pass"
    return CheckResult(
        name="static.pass",
        passed=ok,
        status=str(static["status"]),
        score=int(static["score"]),
        issues=issues,
        required_fixes=tuple(
            {"code": "TEXT_INVARIANT", "message": item} for item in issues[:_ISSUE_LIMIT]
        ),
        details={"static": static},
        artifact_sha256=context.artifact.sha256,
    )


def _probe_pass(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    verdict = registry.motion_check(context)
    return CheckResult(
        name="probe.pass",
        passed=verdict.passed,
        status=verdict.status,
        score=verdict.score,
        issues=verdict.issues,
        required_fixes=verdict.required_fixes,
        coverage=verdict.coverage,
        details={"motion_check": verdict.summary()},
        artifact_sha256=context.artifact.sha256,
        cached=verdict.cached,
    )


def _coverage_complete(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    verdict = registry.motion_check(context)
    coverage = dict(verdict.coverage or {})
    ok = bool(coverage.get("complete")) and verdict.status not in ("fail", "unverified")
    return CheckResult(
        name="coverage.complete",
        passed=ok,
        status="pass" if ok else "unverified",
        score=verdict.score,
        issues=() if ok else ("motion coverage is incomplete or unverified",),
        required_fixes=() if ok else verdict.required_fixes,
        coverage=coverage,
        artifact_sha256=context.artifact.sha256,
        cached=verdict.cached,
    )


def _verify_pass(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    passed = registry.motion_check_pass(context.artifact.sha256)
    issues: list[str] = []
    if not passed:
        issues.append("no motion_check pass recorded for the current artifact hash")
    if passed:
        verdict = registry.run_verifier(context)
        if not verdict.get("ok"):
            passed = False
            issues.append(f"verification failed: {verdict.get('error') or verdict.get('detail') or 'no details'}")
    return CheckResult(
        name="verify.pass",
        passed=passed,
        status="pass" if passed else "fail",
        score=100 if passed else 0,
        issues=tuple(issues),
        required_fixes=() if passed else ({
            "code": "MOTION_CHECK_REQUIRED",
            "message": "run motion_check on the current artifact until it passes",
        },),
        artifact_sha256=context.artifact.sha256,
    )


def _result_written(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    ok = bool(context.store.result_exists())
    return CheckResult(
        name="result.written",
        passed=ok,
        status="pass" if ok else "fail",
        score=100 if ok else 0,
        issues=() if ok else ("FCODE_RESULT.md has not been written",),
        required_fixes=() if ok else ({
            "code": "RESULT_MISSING", "selector": "FCODE_RESULT.md",
            "message": "write the delivery result",
        },),
        artifact_sha256=context.artifact.sha256,
    )


def _motion_check(context: CheckContext, registry: CheckRegistry) -> CheckResult:
    return registry.motion_check(context)


def default_registry(*, probe: Any = None, verify: Callable[[str], Any] | None = None) -> CheckRegistry:
    """The motion capability's checks, wired to the reused analyzers."""
    registry = CheckRegistry(probe=probe, verify=verify)
    registry.register("scene.nonempty", _scene_nonempty)
    registry.register("hooks.all", _hooks_all)
    registry.register("static.pass", _static_pass)
    registry.register("motion_check", _motion_check)
    registry.register("probe.pass", _probe_pass)
    registry.register("coverage.complete", _coverage_complete)
    registry.register("verify.pass", _verify_pass, cache=False)
    registry.register("result.written", _result_written, cache=False)
    return registry


def _aggregate(
    results: list[CheckResult],
    *,
    sha: str,
    registry: CheckRegistry,
) -> CheckOutcome:
    passed = all(item.passed for item in results)
    issues: list[str] = []
    fixes: list[Any] = []
    for item in results:
        for issue in item.issues:
            if issue not in issues:
                issues.append(issue)
        for fix in item.required_fixes:
            if fix not in fixes:
                fixes.append(fix)
    if any(item.status == "fail" for item in results):
        status = "fail"
    elif any(item.status == "unverified" for item in results):
        status = "unverified"
    else:
        status = "pass" if passed else "fail"
    score = min((item.score for item in results), default=0)
    coverage: dict[str, Any] = {}
    for item in results:
        if item.coverage:
            coverage = dict(item.coverage)
            break
    quality = registry.quality_for(sha) or {
        "status": status,
        "score": score,
        "issues": issues[:_ISSUE_LIMIT],
        "required_fixes": fixes[:_ISSUE_LIMIT],
        "foot_pedal_hooks": False,
        "wheel_speed_px_s": 0,
        "background_speed_px_s": 0,
        "coverage": coverage or {
            "required": [], "covered": [], "uncovered": [], "complete": False,
        },
    }
    quality = dict(quality)
    quality["issues"] = list(quality.get("issues") or [])[:_ISSUE_LIMIT]
    quality["required_fixes"] = list(quality.get("required_fixes") or [])[:_ISSUE_LIMIT]
    return CheckOutcome(
        passed=passed,
        results=tuple(results),
        status=status,
        score=score,
        issues=tuple(issues[:_ISSUE_LIMIT]),
        required_fixes=tuple(fixes[:_ISSUE_LIMIT]),
        coverage=coverage,
        quality=quality,
    )

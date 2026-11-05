"""The general world: a workspace sandbox plus the delegation's sources.

The finance world holds documents and recomputes margins. The general world
holds the file sandbox and recomputes *file state*: the verifier re-reads the
file from disk and re-derives its sha256, refusing to accept a write
observation whose recorded hash the disk no longer matches. That is the
generalisation of independent recomputation — recompute the accounts that
exist (file hashes, budgets, the chain), and label what does not (web
content truth) as not-verified instead of pretending.

File state is not the whole of it, though. A delegation may declare an
engineering-result obligation (``spec["verification"]``), and then the
runtime re-executes that declared command itself and this module judges the
result. Two things make that safe: the declaration is the *host's* and is
frozen in the task binding, and the command runs against a **snapshot** of the
workspace rather than the workspace itself — so the run that decides success
cannot disturb the artifact it is verifying. A command result recorded during
normal task execution is evidence; it is never the proof.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError
from dataclasses import replace

from ...core.verification import (
    VERIFICATION_SOURCE_SPEC,
    VERIFICATION_TOOL,
    VerificationObligation,
    build_obligation,
)
from ..base import Observation, ToolSpec
from .ask import build_ask_tool
from .commands import CommandRunner, load_command_config
from .planner import GENERAL_MODES, OPEN_REPORT_PATH
from .summaries import summarize_ask_operator
from .fetch import UrlFetcher
from .recall import build_recall_tool
from .sandbox import WorkspaceSandbox
from .tools import build_general_tools

__all__ = ["GeneralWorld", "MAX_VERIFICATION_SNAPSHOT_BYTES"]

#: A verification snapshot is a bounded copy, not an archive. The cap exists so
#: an unbounded workspace fails closed with a clear reason instead of quietly
#: spending the host's disk — an unverifiable task must never look verified.
MAX_VERIFICATION_SNAPSHOT_BYTES = 32 * 1024 * 1024

#: Where the frozen copy goes, as a sibling of the jail. Outside the jail on
#: purpose: the model's own tools must not be able to read or write the state
#: that is being used to judge it.
_SNAPSHOT_DIRNAME = "verify-snapshot"


def _allow_hosts_from_env() -> tuple[str, ...]:
    raw = os.environ.get("FCODE_FETCH_ALLOW_HOSTS", "")
    return tuple(host.strip() for host in raw.split(",") if host.strip())


class GeneralWorld:
    """Domain state for one general delegation.

    Two delegation modes, both operator-decided in the spec:

    * ``research`` (default): fetch the listed sources, write ``research.md``;
    * ``script``: the agent writes a script into the workspace, runs it
      under the restricted terminal, fixes what it got wrong with an
      anchored edit, re-runs, and files the outputs in ``results.md``.
    """

    def __init__(
        self,
        spec: dict[str, Any] | None = None,
        *,
        sandbox: WorkspaceSandbox | None = None,
        fetcher: UrlFetcher | None = None,
        command_runner: CommandRunner | None = None,
        command_config: str | None = None,
        ask_operator: Any = None,
    ) -> None:
        self.spec = dict(spec or {})
        # An unrecognised mode falls back to research, which is what a
        # delegation that named none has always got. ``open`` is the mode whose
        # plan the model may extend (see the planner), and it is never inferred:
        # a blueprint delegation must not silently become an open one.
        requested_mode = str(self.spec.get("mode") or "").strip()
        self.mode = requested_mode if requested_mode in GENERAL_MODES else "research"
        # Chat-to-work bridge: the conversation this delegation came from, so
        # temporal/place references ("周三", "当天", "到了后") in the objective
        # resolve against what the user actually said. Sanitised upstream by
        # the console (role/text pairs, capped); kept verbatim here so the
        # chain carries exactly what the planner saw.
        origin = self.spec.get("origin_thread")
        self.origin_thread: dict[str, Any] | None = origin if isinstance(origin, dict) else None
        self.sources: list[str] = [
            str(url) for url in (self.spec.get("sources") or []) if str(url).strip()
        ]
        self.script_path = str(self.spec.get("scriptPath") or "script.py")
        # Where the artifact of this mode goes. An open delegation's only
        # host-authored obligation is that this file ends up written and
        # verified, so the operator may name it (E1's SUMMARY.md, say).
        default_report = {"script": "results.md", "open": OPEN_REPORT_PATH}.get(
            self.mode, "research.md"
        )
        self.report_path = str(self.spec.get("reportPath") or default_report)
        self.sandbox = sandbox or WorkspaceSandbox(Path.cwd() / "workspace-general")
        self.fetcher = fetcher or UrlFetcher(allow_hosts=_allow_hosts_from_env())
        #: The restricted terminal's policy: the host's commands.json (same
        #: directory as mcp.json — the console injects the path) extends the
        #: code default whitelist; never replaces it.
        self.command_runner = command_runner
        self.command_config = command_config or os.environ.get("FCODE_COMMANDS_CONFIG") or None
        #: The operator channel (see the class attribute below). Wired by the
        #: console once the run's control exists; assignments after construction
        #: also work, because the tool resolves it when a question is asked.
        self.ask_operator = ask_operator
        #: The declared engineering-result obligation, or None. Validated here —
        #: the one place both the CLI and the console build a world — so a bad
        #: declaration is refused before any work happens.
        self.verification: VerificationObligation | None = build_obligation(
            self.spec.get("verification"), source=VERIFICATION_SOURCE_SPEC,
        )
        #: True only while the runtime's own verification run is in flight. The
        #: verifier requires it, which is what keeps a command result recorded
        #: during ordinary task execution from ever being mistaken for proof.
        self._verifying = False
        #: The sandbox the command runner was pointed at before verification
        #: redirected it at a snapshot.
        self._verification_restore: Any = None

    #: Optional provider of this run's compressed observation records; set by
    #: the assembly so ``recall_observation`` reads exactly what working memory
    #: holds — never another run, never the raw store.
    memory_records_provider: Any = None
    #: The loop publishes the live WorkingMemory here each run; the provider
    #: closes over it. An assembly may also leave both unset to skip the tool.
    recall_holder: Any = None
    #: The operator channel this run can ask through: ``ask(question=…, why=…)
    #: -> {"answered": bool, …}``. The console wires it to the run's control
    #: (where approvals wait); CLI and offline deployments leave it unset and
    #: the tool answers "no operator channel" instead of hanging.
    ask_operator: Any = None

    def build_tools(self) -> list[ToolSpec]:
        runner = self.command_runner
        if runner is None:
            runner = CommandRunner(self.sandbox, load_command_config(self.command_config))
            self.command_runner = runner
        if self.verification is not None:
            # Refuse a declaration the host's own command policy would refuse.
            # The policy is the authority on which programs may run and how their
            # arguments are shaped; asking it here means an operator who declares
            # an unwhitelisted check hears about it when the run is assembled,
            # not after the work is finished and cannot be verified. The runtime
            # still re-resolves at dispatch — this is early, not instead.
            policy = getattr(runner, "policy", None)
            if policy is not None:
                try:
                    policy.resolve(
                        str(self.verification.arguments["command"]),
                        list(self.verification.arguments.get("args") or []),
                        self.sandbox,
                    )
                except AgentError as error:
                    raise AgentError(
                        "VERIFICATION_DECLARATION_INVALID",
                        f"the declared verification command cannot run under this host's "
                        f"command policy: {error.code}: {error.message}",
                    ) from error
        tools = build_general_tools(self.sandbox, self.fetcher, command_runner=runner)
        # The tool is always registered; the *channel* is resolved when a
        # question is actually asked. Assemblies wire it after the world exists
        # (the console's control needs the harness, which needs the world), and
        # a deployment with no operator gets the honest answer instead of a
        # tool the gate would refuse as unregistered.
        def channel(**kwargs: Any) -> dict[str, Any]:
            current = self.ask_operator
            if current is None:
                return {"answered": False, "reason": "this deployment has no operator channel"}
            return current(**kwargs)

        tools.append(replace(build_ask_tool(channel), summarize=summarize_ask_operator))
        if self.memory_records_provider is not None:
            tools.append(build_recall_tool(self.memory_records_provider))
        return tools

    def apply_state(self, state: dict[str, Any]) -> None:
        """Resume hook. The sandbox *is* the durable state (files on disk);
        the in-memory world has nothing to re-derive from the event log."""

    # ------------------------------------------------------------ verification

    def verification_scope(self) -> "GeneralVerificationScope | None":
        """The scope the runtime uses to re-run the declared obligation."""
        if self.verification is None:
            return None
        return GeneralVerificationScope(self)

    def _snapshot_root(self) -> Path:
        return self.sandbox.root.parent / _SNAPSHOT_DIRNAME

    @staticmethod
    def _tree_measure(root: Path) -> tuple[int, int]:
        """(files, bytes) without following symlinks — a stat pass, not a read."""
        files = 0
        total = 0
        for entry in root.rglob("*"):
            if entry.is_symlink() or not entry.is_file():
                continue
            files += 1
            total += entry.stat().st_size
        return files, total

    @staticmethod
    def _tree_digest(root: Path) -> str:
        """A deterministic identity for the frozen tree: sorted (path, sha256).

        This is the answer to "which workspace state was tested?" — the copy's
        identity, derived after copying, so it describes what actually ran.
        """
        digest = hashlib.sha256()
        for entry in sorted(root.rglob("*")):
            if entry.is_symlink() or not entry.is_file():
                continue
            relative = entry.relative_to(root).as_posix()
            body = hashlib.sha256(entry.read_bytes()).hexdigest()
            digest.update(f"{relative}\0{body}\n".encode("utf-8"))
        return digest.hexdigest()

    def _freeze_workspace(self, artifact: Any) -> dict[str, Any]:
        """Copy the workspace to a snapshot and point the runner at it.

        Fail-closed throughout: if the copy cannot be made, is too large, or the
        artifact inside it does not match what the artifact verifier just
        accepted, the runtime must not report success — the honest outcome is
        "independent verification was unavailable", never a pass.
        """
        source = self.sandbox.root
        if not source.exists():
            raise AgentError("VERIFICATION_UNAVAILABLE", f"{source} does not exist")
        files, total = self._tree_measure(source)
        if total > MAX_VERIFICATION_SNAPSHOT_BYTES:
            raise AgentError(
                "VERIFICATION_UNAVAILABLE",
                f"the workspace holds {total} bytes, above the {MAX_VERIFICATION_SNAPSHOT_BYTES}-byte "
                "verification snapshot cap; independent verification was not performed",
            )
        snapshot = self._snapshot_root()
        try:
            if snapshot.exists():
                shutil.rmtree(snapshot)
            shutil.copytree(source, snapshot, symlinks=True)
        except OSError as error:
            raise AgentError(
                "VERIFICATION_UNAVAILABLE", f"cannot freeze the workspace for verification: {error}",
            ) from error

        artifact_check: dict[str, Any] = {"path": None, "sha256": None, "matched": None}
        path = str((artifact or {}).get("path") or "")
        expected = str((artifact or {}).get("sha256") or "")
        if path and expected:
            frozen = snapshot / path
            try:
                body = frozen.read_bytes()
            except OSError as error:
                raise AgentError(
                    "VERIFICATION_UNAVAILABLE",
                    f"verified artifact {path!r} is not readable inside the snapshot: {error}",
                ) from error
            actual = hashlib.sha256(body).hexdigest()
            artifact_check = {"path": path, "sha256": actual, "matched": actual == expected}
            if actual != expected:
                raise AgentError(
                    "VERIFICATION_UNAVAILABLE",
                    f"the snapshot's {path!r} hashes {actual[:12]}… but the verified artifact was "
                    f"{expected[:12]}… and the verification was refused rather than run against "
                    "a state it does not describe",
                )

        runner = self.command_runner
        if runner is None:
            raise AgentError("VERIFICATION_UNAVAILABLE", "this run has no command runner")
        if not hasattr(runner, "sandbox"):
            # A runner that cannot be pointed at the snapshot cannot give the
            # isolation the verification promise rests on, so refuse rather
            # than run the check against the live workspace.
            raise AgentError(
                "VERIFICATION_UNAVAILABLE",
                "this run's command runner cannot be redirected to a verification snapshot",
            )
        self._verification_restore = runner.sandbox
        runner.sandbox = WorkspaceSandbox(snapshot)
        self._verifying = True
        return {
            "files": files,
            "bytes": total,
            "sha256": self._tree_digest(snapshot),
            "artifact": artifact_check,
        }

    def _thaw_workspace(self) -> None:
        """Put the runner back and end the verification window."""
        self._verifying = False
        if self._verification_restore is not None and self.command_runner is not None:
            self.command_runner.sandbox = self._verification_restore
        self._verification_restore = None

    def _verify_declared_command(self, observation: Observation) -> dict[str, Any]:
        """Judge the runtime's own re-run of the declared obligation.

        Four conditions, all required, and each one rules out a way this could
        be faked: the run happened inside the verification window (not during
        ordinary task execution); the command is exactly the declared one; the
        exit code is known (a timeout is not a pass); and it equals the
        declared expectation.
        """
        obligation = self.verification
        if obligation is None:
            raise AgentError(
                "RESULT_INCOMPLETE",
                "no verification obligation was declared for this delegation",
            )
        if not self._verifying:
            # The distinction that matters: a command result recorded while the
            # task was executing is evidence, not verification.
            raise AgentError(
                "RESULT_INCOMPLETE",
                "a command result recorded during task execution is not a verification result",
            )
        declared = dict(obligation.arguments)
        actual = {key: value for key, value in dict(observation.arguments).items() if key != "timeout_seconds"}
        if actual != {key: value for key, value in declared.items() if key != "timeout_seconds"}:
            raise AgentError(
                "RESULT_INCOMPLETE",
                f"the observation is for {actual!r}, but the declared obligation is {declared!r}",
            )
        result = observation.result
        exit_code = result.get("exit_code")
        if exit_code is None:
            raise AgentError(
                "RESULT_VERIFICATION_FAILED",
                f"the verification command {obligation.name!r} produced no exit code "
                "(timeout or unknown outcome) and cannot be a pass",
            )
        expected = int(obligation.expect.get("exit_code", 0))
        if int(exit_code) != expected:
            raise AgentError(
                "RESULT_VERIFICATION_FAILED",
                f"the declared obligation {obligation.name!r} failed: exit code {exit_code}, "
                f"expected {expected}",
            )
        return {
            "verified": True,
            "verification": "command-result-recomputed",
            "result": {
                "name": obligation.name,
                "tool": observation.tool,
                "arguments": dict(observation.arguments),
                "exit_code": int(exit_code),
                "expect_exit_code": expected,
                "duration_ms": result.get("duration_ms"),
                "output_bytes": result.get("bytes"),
                "output_truncated": result.get("truncated"),
                "result_hash": observation.result_hash,
                "source": obligation.source,
                "content_truth": "runtime-observed-command-result",
            },
        }

    def verify_observation(self, observation: Observation) -> dict[str, Any]:
        """Judge one claimed result: a written file, or the runtime's own re-run.

        Two kinds of result are accepted, and they are deliberately different
        claims:

        * ``write_file`` — the *artifact* obligation. The recompute is the
          receipt's own currency: sha256 over the bytes on disk compared with
          the recorded after-hash. A file that was *legitimately edited later*
          (edit_file is a first-class capability) no longer matches its earlier
          write on disk, so the fallback re-derives the historical state from
          the git commit the receipt names. What this establishes is file state
          — never that the file's contents are true.
        * the declared command obligation, re-executed by the runtime inside a
          frozen snapshot (see :meth:`_verify_declared_command`). This one
          establishes a *result*: the declared check exits as declared.

        Anything else fails closed. In particular a command result recorded
        while the task was executing reaches neither branch with a pass — it is
        evidence on the chain, not a verification.
        """
        if observation.tool == VERIFICATION_TOOL:
            return self._verify_declared_command(observation)
        if observation.tool != "write_file":
            raise AgentError("RESULT_INCOMPLETE", f"{observation.tool!r} is not a result tool here")
        result = observation.result
        if not result.get("computed"):
            raise AgentError("RESULT_INCOMPLETE", "the write did not compute")
        relative = str(result.get("path") or observation.arguments.get("path") or "")
        record = self.sandbox.read(relative)
        if record["sha256"] == result.get("sha256_after"):
            return self._verified(record, "file-state-recomputed", result)
        commit = str(result.get("commit") or "")
        if commit:
            import hashlib
            import subprocess

            try:
                blob = subprocess.run(
                    ["git", "show", f"{commit}:{relative}"],
                    cwd=str(self.sandbox.root),
                    capture_output=True,
                    timeout=15,
                    check=True,
                ).stdout
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                blob = None
            if blob is not None and hashlib.sha256(blob).hexdigest() == result.get("sha256_after"):
                return self._verified(record, "git-history-recomputed", result)
        raise AgentError(
            "RESULT_VERIFICATION_FAILED",
            f"{relative!r} on disk hashes {record['sha256'][:12]}… but the "
            f"observation recorded {str(result.get('sha256_after'))[:12]}… "
            "and no git state matches that hash",
        )

    @staticmethod
    def _verified(record: dict[str, Any], verification: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "verified": True,
            "verification": verification,
            "result": {
                "path": record["path"],
                "sha256": record["sha256"],
                "bytes": record["bytes"],
                "commit": result.get("commit", ""),
                # Says exactly what was recomputed. It is not a claim that the
                # file's contents are accurate — see the evidence labels the
                # loop attaches to the terminal result.
                "content_truth": "runtime-verified-file-state",
            },
        }


class GeneralVerificationScope:
    """The workspace-freezing half of a declared obligation.

    A thin adapter: it owns no state of its own beyond the world it belongs to,
    so there is exactly one place that knows how this domain freezes a
    workspace, and the kernel never learns what a sandbox is.
    """

    def __init__(self, world: GeneralWorld) -> None:
        if world.verification is None:  # pragma: no cover - guarded by the caller
            raise AgentError("RESULT_INCOMPLETE", "no verification obligation was declared")
        self.world = world
        self.obligation = world.verification
        self.evidence: dict[str, Any] = {}

    def prepare(self, artifact: dict[str, Any] | None) -> dict[str, Any]:
        self.evidence = self.world._freeze_workspace(artifact)
        return dict(self.evidence)

    def release(self) -> None:
        self.world._thaw_workspace()

"""The restricted terminal: a whitelist registry, not a shell.

``run_command`` exists because a general agent must be able to *execute* what
it writes. The governance answer is not "no terminal" and not "a real shell";
it is a closed per-command policy, evaluated before dispatch:

* **Whitelist.** Only the programs the host registered (a default set in
  code, extendable via ``commands.json`` next to ``mcp.json``). Anything else
  is a confirmed refusal before a process exists.
* **Argument policy per program.** ``python``/``node`` run *workspace script
  files* only — ``-c``/``-e``/``-m`` inline or module execution is refused
  (inline code is a logic escape from the sandbox the file jail cannot see).
  ``git`` is limited to ``status``/``diff``/``log``/``add``/``commit``; no
  network or history-rewriting subcommand, and no global flags (``-C``,
  ``--git-dir`` …) that could retarget the repository. Read-only utilities
  get their path arguments jail-checked against the workspace root.
* **No shell, ever.** ``subprocess`` with an argv array and ``shell=False``;
  there is nothing to inject *into*. Shell *syntax* in an argument
  (``;`` ``&&`` ``||`` ``|`` ``<`` ``>`` backticks, ``$(``, newlines) is
  still refused outright — a proposal that looks like shell plumbing is a
  proposal the policy never promised to accept. (Consequence: grep
  alternation must use repeated ``-e`` patterns, not ``a|b``.)
* **Unknown discipline.** The command is dispatched with a deadline; a
  timeout means it *ran* and never came back — ``COMMAND_TIMEOUT`` with
  ``outcome: unknown``, the process group is killed, nothing is re-run. The
  workspace git history is the reconciliation ledger: what the command
  actually changed on disk is inspectable, commit by commit.

Honest boundary, stated in the docs: this is *command-level* confinement —
whitelist, argument policy, approval, receipt — not an OS-level sandbox. A
script the operator approves can still do anything its interpreter can; the
approval dialog and the git snapshot are what make that decision reviewable.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...core.contracts import AgentError

__all__ = [
    "CommandPolicy",
    "CommandRunner",
    "DEFAULT_COMMANDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "MAX_OUTPUT_BYTES",
    "load_command_config",
]

DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 120
#: Captured output is capped (and honestly marked truncated) so a chatty
#: command cannot blow up the observation, the event chain or the trace.
MAX_OUTPUT_BYTES = 64 * 1024
#: On timeout the process group is killed; give the OS a beat to reap it.
_KILL_GRACE_SECONDS = 3.0

#: Shell metacharacters refused in any argument. With ``shell=False`` these
#: are inert bytes — the refusal is a policy statement: the terminal has no
#: shell, and a proposal written in shell grammar is not a legal proposal.
_SHELL_META = (";", "&&", "||", "|", "<", ">", "`", "$(", "\n", "\r")

#: git subcommands the host allows. Everything else — network (push/pull/
#: fetch), history rewrite (reset/rebase/filter-branch), config — is out.
_GIT_SUBCOMMANDS = {
    "status": True, "diff": True, "log": True,          # readonly
    "add": False, "commit": False,                      # workspace writes
}
#: git global flags rejected before the subcommand is even looked at.
_GIT_GLOBAL_FLAGS = {"-C", "--git-dir", "--work-tree", "-c", "--exec-path", "--namespace"}

#: Options are allowlisted per utility. An option that consumes a value is
#: parsed with its value kind: ``path`` goes through the workspace jail,
#: ``number`` must be numeric and ``text``/``pattern`` is opaque data. Unknown
#: options are refused rather than assumed to be readonly.
_UTILITY_FLAGS = {
    "ls": {"-1", "-a", "-al", "-h", "-l", "-la", "-R"},
    "cat": {"-b", "-e", "-n", "-t", "-v"},
    "find": {"-print"},
    "wc": {"-L", "-c", "-l", "-lw", "-m", "-w", "-wc"},
    "diff": {"-N", "-c", "-q", "-r", "-u", "--brief", "--recursive", "--unified"},
    "grep": {"-E", "-F", "-R", "-c", "-i", "-l", "-n", "-r", "-v", "-w", "-x"},
}
_UTILITY_VALUE_OPTIONS = {
    "find": {"-iname": "pattern", "-maxdepth": "number", "-name": "pattern", "-type": "text"},
    "grep": {
        "-A": "number", "-B": "number", "-C": "number", "-e": "pattern",
        "-f": "path", "-m": "number",
    },
}
_GIT_OPTIONS = {
    "status": (
        {"-b", "-s", "--branch", "--porcelain", "--short"},
        {},
        {"--untracked-files="},
    ),
    "diff": (
        {"--cached", "--exit-code", "--name-only", "--name-status", "--no-color", "--quiet", "--stat", "--staged"},
        {},
        set(),
    ),
    "log": (
        {"--all", "--decorate", "--graph", "--no-color", "--oneline"},
        {"-n": "number", "--max-count": "number"},
        {"--format=", "--max-count="},
    ),
    "add": ({"-A", "-u", "--all", "--update"}, {}, set()),
    "commit": ({"--allow-empty"}, {"-m": "text", "--message": "text"}, {"--message="}),
}


@dataclass(frozen=True)
class CommandRule:
    """One whitelisted program and how its arguments are constrained."""

    program: str
    #: True = pure read (ls/cat/grep/...); False = consequential (approval).
    readonly: bool
    #: "script" → interpreter policy (first path arg must be a jailed
    # workspace script, inline flags refused). "git" → git subcommand
    # whitelist. "paths" → non-flag args are jail-checked paths.
    # "free" → no per-arg constraint beyond the metacharacter refusal.
    arg_policy: str = "free"


#: The host's default registry. Extendable (never replaceable) via
#: ``commands.json`` next to ``mcp.json``: same shape, host-reviewed.
DEFAULT_COMMANDS: tuple[CommandRule, ...] = (
    CommandRule("python", readonly=False, arg_policy="script"),
    CommandRule("python3", readonly=False, arg_policy="script"),
    CommandRule("node", readonly=False, arg_policy="script"),
    CommandRule("git", readonly=False, arg_policy="git"),
    CommandRule("ls", readonly=True, arg_policy="paths"),
    CommandRule("cat", readonly=True, arg_policy="paths"),
    CommandRule("grep", readonly=True, arg_policy="grep"),
    CommandRule("find", readonly=True, arg_policy="paths"),
    CommandRule("wc", readonly=True, arg_policy="paths"),
    CommandRule("diff", readonly=True, arg_policy="paths"),
    CommandRule("pytest", readonly=False, arg_policy="free"),
    CommandRule("unittest", readonly=False, arg_policy="free"),
)


class CommandPolicy:
    """Evaluates one proposed command against the whitelist and per-program
    argument rules. Pure: no filesystem touch, no process."""

    def __init__(self, rules: tuple[CommandRule, ...] = DEFAULT_COMMANDS) -> None:
        self._rules = {rule.program: rule for rule in rules}

    @property
    def rules(self) -> tuple[CommandRule, ...]:
        return tuple(self._rules.values())

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"program": rule.program, "readonly": rule.readonly, "argPolicy": rule.arg_policy}
            for rule in self._rules.values()
        ]

    def classify(self, command: str, args: list[str], sandbox=None) -> bool | None:
        """Classify the exact argv, using the same parser as execution.

        True = consequential, False = provably readonly under the host's
        allowlist, None = unregistered. Invalid or unrecognised arguments fail
        closed to consequential; they are never exempted from approval.
        """
        rule = self._rules.get(str(command or ""))
        if rule is None:
            return None
        try:
            self.resolve(command, list(args or []), sandbox)
        except AgentError:
            return True
        if rule.readonly:
            return False
        if rule.program == "git":
            return bool(not args or args[0] not in ("status", "diff", "log"))
        return True

    def resolve(self, program: str, args: list[str], sandbox) -> tuple[CommandRule, list[str]]:
        """Return (rule, argv) for a legal proposal, or raise a confirmed refusal.

        ``sandbox`` is only used to jail-check path arguments — the same
        :class:`WorkspaceSandbox` the file tools use.
        """
        if not isinstance(program, str) or not program.strip():
            raise AgentError("COMMAND_DENIED", "command must be a non-empty program name")
        program = program.strip()
        if "/" in program or "\\" in program or program.startswith("."):
            raise AgentError(
                "COMMAND_DENIED",
                f"{program!r} must be a program name from the whitelist, not a path",
            )
        rule = self._rules.get(program)
        if rule is None:
            raise AgentError(
                "COMMAND_NOT_WHITELISTED",
                f"{program!r} is not in the command whitelist; allowed: {sorted(self._rules)}",
            )
        if not isinstance(args, (list, tuple)):
            raise AgentError("COMMAND_DENIED", "args must be an array of strings")
        for arg in args:
            if not isinstance(arg, str) or not arg:
                raise AgentError("COMMAND_DENIED", "every argument must be a non-empty string")
            for meta in _SHELL_META:
                if meta in arg:
                    raise AgentError(
                        "COMMAND_SHELL_SYNTAX",
                        f"argument {arg[:60]!r} contains shell syntax ({meta!r}); "
                        "the terminal has no shell — pass single arguments only",
                    )

        if rule.arg_policy == "script":
            checked = self._check_interpreter(program, args, sandbox)
        elif rule.arg_policy == "git":
            checked = self._check_git(args, sandbox)
        elif rule.arg_policy == "paths":
            checked = self._check_paths(program, args, sandbox)
        elif rule.arg_policy == "grep":
            checked = self._check_grep(args, sandbox)
        else:
            checked = list(args)
        return rule, [program, *checked]

    # ---------------------------------------------------------------- policies

    @staticmethod
    def _check_interpreter(program: str, args: list[str], sandbox) -> list[str]:
        """`python foo.py [args…]`: a jailed workspace script, no inline code."""
        inline = {"-c", "-e", "-m", "--eval", "--exec"}
        path_args = [arg for arg in args if not arg.startswith("-")]
        for arg in args:
            if arg in inline:
                raise AgentError(
                    "COMMAND_INLINE_REFUSED",
                    f"{program} {arg} runs inline/module code; only workspace script files are executable",
                )
        flags = [arg for arg in args if arg.startswith("-")]
        for flag in flags:
            if flag not in ("-u", "-B", "-O", "--version"):
                raise AgentError(
                    "COMMAND_FLAG_REFUSED",
                    f"flag {flag!r} is not allowed for {program}",
                )
        if not path_args:
            raise AgentError(
                "COMMAND_DENIED",
                f"{program} needs a workspace script file to run (e.g. `{program} script.py`)",
            )
        script = path_args[0]
        if not script.endswith((".py", ".js", ".mjs")):
            raise AgentError(
                "COMMAND_DENIED",
                f"{script!r} is not a script file; {program} runs .py/.js files from the workspace",
            )
        if sandbox is not None:
            sandbox.resolve(script)  # jail check: refuses .., absolute, symlink escapes
        return list(args)

    @staticmethod
    def _jail(value: str, sandbox) -> None:
        if sandbox is not None:
            sandbox.resolve(value)

    @staticmethod
    def _check_git(args: list[str], sandbox) -> list[str]:
        """Parse an allowlisted git subcommand and its exact option grammar."""
        for arg in args:
            if arg.split("=", 1)[0] in _GIT_GLOBAL_FLAGS:
                raise AgentError(
                    "COMMAND_GIT_FLAG_REFUSED",
                    f"git {arg} could retarget the repository; it is not allowed",
                )
        if not args or args[0].startswith("-"):
            raise AgentError(
                "COMMAND_DENIED",
                "git needs a subcommand; allowed: " + ", ".join(sorted(_GIT_SUBCOMMANDS)),
            )
        subcommand = args[0]
        if subcommand not in _GIT_SUBCOMMANDS:
            raise AgentError(
                "COMMAND_GIT_SUBCOMMAND_REFUSED",
                f"git {subcommand!r} is not whitelisted; allowed: {sorted(_GIT_SUBCOMMANDS)}",
            )
        flags, value_options, attached_options = _GIT_OPTIONS[subcommand]
        checked = [subcommand]
        index = 1
        options = True
        while index < len(args):
            arg = args[index]
            if options and arg == "--":
                checked.append(arg)
                options = False
                index += 1
                continue
            if options and arg in value_options:
                if index + 1 >= len(args):
                    raise AgentError("COMMAND_GIT_FLAG_REFUSED", f"git {arg} needs a value")
                value = args[index + 1]
                if value_options[arg] == "number" and not value.isdigit():
                    raise AgentError("COMMAND_GIT_FLAG_REFUSED", f"git {arg} needs a numeric value")
                checked.extend([arg, value])
                index += 2
                continue
            if options and any(arg.startswith(prefix) for prefix in attached_options):
                value = arg.split("=", 1)[1]
                if not value or (arg.startswith("--max-count=") and not value.isdigit()):
                    raise AgentError("COMMAND_GIT_FLAG_REFUSED", f"invalid git option {arg!r}")
                checked.append(arg)
                index += 1
                continue
            if options and arg.startswith("-"):
                if arg not in flags:
                    raise AgentError(
                        "COMMAND_GIT_FLAG_REFUSED",
                        f"flag {arg!r} is not allowed for git {subcommand}",
                    )
                checked.append(arg)
                index += 1
                continue
            CommandPolicy._jail(arg, sandbox)
            checked.append(arg)
            index += 1
        return checked

    @staticmethod
    def _check_paths(program: str, args: list[str], sandbox) -> list[str]:
        return CommandPolicy._check_utility(program, args, sandbox)

    @staticmethod
    def _check_grep(args: list[str], sandbox) -> list[str]:
        return CommandPolicy._check_utility("grep", args, sandbox)

    @staticmethod
    def _check_utility(program: str, args: list[str], sandbox) -> list[str]:
        """Parse utility options, including values that are themselves paths."""
        flags = _UTILITY_FLAGS.get(program, set())
        value_options = _UTILITY_VALUE_OPTIONS.get(program, {})
        checked: list[str] = []
        index = 0
        options = True
        seen_pattern = False
        while index < len(args):
            arg = args[index]
            if options and arg == "--":
                checked.append(arg)
                options = False
                index += 1
                continue
            if options and arg in value_options:
                if index + 1 >= len(args):
                    raise AgentError("COMMAND_FLAG_REFUSED", f"{program} {arg} needs a value")
                value = args[index + 1]
                kind = value_options[arg]
                if kind == "number" and not value.isdigit():
                    raise AgentError("COMMAND_FLAG_REFUSED", f"{program} {arg} needs a numeric value")
                if kind == "path":
                    CommandPolicy._jail(value, sandbox)
                    seen_pattern = True
                elif kind == "pattern":
                    seen_pattern = True
                checked.extend([arg, value])
                index += 2
                continue
            if options and arg.startswith("-"):
                if arg not in flags:
                    raise AgentError(
                        "COMMAND_FLAG_REFUSED",
                        f"flag {arg!r} is not allowed for {program}",
                    )
                checked.append(arg)
                index += 1
                continue
            if program == "grep" and not seen_pattern:
                seen_pattern = True
            else:
                CommandPolicy._jail(arg, sandbox)
            checked.append(arg)
            index += 1
        return checked


class CommandRunner:
    """Executes one policy-approved argv in the run's workspace."""

    def __init__(self, sandbox, policy: CommandPolicy | None = None) -> None:
        self.sandbox = sandbox
        self.policy = policy or CommandPolicy()

    def run(self, command: str, args: list[str], *, timeout_seconds: int | None = None) -> dict[str, Any]:
        rule, argv = self.policy.resolve(command, list(args or []), self.sandbox)
        timeout = DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else int(timeout_seconds)
        if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise AgentError(
                "COMMAND_TIMEOUT_INVALID",
                f"timeout must be 1-{MAX_TIMEOUT_SECONDS} seconds",
            )
        self.sandbox.ensure()
        started = time.monotonic()
        deadline = started + timeout
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(self.sandbox.root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,  # from the root: there is no shell to inject into
                env=command_env(),  # provider credentials never reach a command
                start_new_session=True,  # own process group → killable on timeout
            )
        except FileNotFoundError as error:
            raise AgentError(
                "COMMAND_NOT_FOUND",
                f"{argv[0]!r} is whitelisted but not installed on this host: {error}",
            ) from error
        except OSError as error:
            raise AgentError("COMMAND_LAUNCH_FAILED", f"cannot launch {argv[0]!r}: {error}") from error

        output, truncated, finished = self._collect(process, timeout)
        exit_code: int | None = None
        if finished:
            # EOF on the merged pipe; the process may still be wrapping up.
            try:
                exit_code = process.wait(timeout=max(0.5, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                exit_code = None
        if exit_code is None:
            # The command was dispatched and never answered — either it kept
            # a pipe open past the deadline or never exited. Kill the whole
            # group so nothing keeps mutating the workspace, then record an
            # unknown outcome. Never a re-run.
            self._kill_group(process)
            exit_code = process.wait(timeout=_KILL_GRACE_SECONDS)
            raise AgentError(
                "COMMAND_TIMEOUT",
                f"{argv[0]!r} was dispatched with a {timeout}s deadline and did not return; "
                "outcome unknown — not re-run; reconcile against the workspace git history",
                details={"outcome": "unknown", "exit_code": exit_code, "seconds": timeout},
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        return {
            "command": " ".join(argv),
            "exit_code": int(exit_code),
            "duration_ms": duration_ms,
            "output": output,
            "bytes": len(output.encode("utf-8", errors="replace")),
            "truncated": truncated,
            "readonly": rule.readonly,
            "computed": True,
        }

    # ---------------------------------------------------------------- private

    def _collect(self, process: subprocess.Popen, timeout: float) -> tuple[str, bool, bool]:
        """Merge stdout+stderr with a hard cap; drain past the cap so a chatty
        command cannot wedge the pipe and turn itself into an unknown.

        Reads the raw fd (``os.read`` returns whatever is available — a
        buffered text read would block trying to fill its buffer and blow
        past the deadline). Returns ``(output, truncated, finished)``;
        ``finished`` is True when the pipe hit EOF before the deadline.
        """
        assert process.stdout is not None
        stream_fd = process.stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(stream_fd, selectors.EVENT_READ)
        chunks: list[bytes] = []
        retained = 0
        truncated = False
        deadline = time.monotonic() + timeout
        finished = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                events = selector.select(remaining)
                if not events:
                    continue  # deadline will catch it on the next loop
                chunk = os.read(stream_fd, 65536)
                if not chunk:
                    finished = True  # EOF: the command finished writing
                    break
                retained += len(chunk)
                if retained <= MAX_OUTPUT_BYTES:
                    chunks.append(chunk)
                else:
                    truncated = True  # keep draining; stop retaining
        finally:
            selector.close()
            # Popen owns this handle, but its destructor is not a lifecycle
            # strategy. Close it on EOF, timeout and read errors alike so every
            # completed command releases the fd deterministically.
            process.stdout.close()
        return b"".join(chunks).decode("utf-8", errors="replace"), truncated, finished

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:  # pragma: no cover - already gone
                pass


def load_command_config(path: str | Path | None) -> CommandPolicy:
    """Host extension of the whitelist (``commands.json`` next to ``mcp.json``).

    Adds to the default registry; never removes or downgrades a rule — a
    config that tried to mark ``python`` readonly or drop ``git`` limits is a
    configuration error, refused loudly. Example::

        {"commands": [{"program": "ruff", "readonly": true, "argPolicy": "free"}]}
    """
    if path is None:
        return CommandPolicy()
    target = Path(path)
    if not target.exists():
        return CommandPolicy()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AgentError("COMMAND_CONFIG_INVALID", f"cannot read {target}: {error}") from error
    entries = data.get("commands") if isinstance(data, dict) else None
    if entries is None:
        return CommandPolicy()
    if not isinstance(entries, list):
        raise AgentError("COMMAND_CONFIG_INVALID", "commands.json must contain a commands list")
    by_program = {rule.program: rule for rule in DEFAULT_COMMANDS}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("program"):
            raise AgentError("COMMAND_CONFIG_INVALID", "each entry needs a program")
        program = str(entry["program"])
        if "/" in program or "\\" in program:
            raise AgentError("COMMAND_CONFIG_INVALID", f"program {program!r} must be a bare name")
        arg_policy = str(entry.get("argPolicy", "free"))
        if arg_policy not in ("script", "git", "paths", "grep", "free"):
            raise AgentError("COMMAND_CONFIG_INVALID", f"unknown argPolicy {arg_policy!r}")
        rule = CommandRule(program, readonly=bool(entry.get("readonly", False)), arg_policy=arg_policy)
        existing = by_program.get(program)
        if existing is not None and (existing.readonly, existing.arg_policy) != (rule.readonly, rule.arg_policy):
            raise AgentError(
                "COMMAND_CONFIG_INVALID",
                f"config tried to change the built-in rule for {program!r}; extension is add-only",
            )
        by_program[program] = rule
    return CommandPolicy(tuple(by_program.values()))


def command_env() -> dict[str, str]:
    """The environment a command runs in: the host's, minus anything that
    would let it authenticate as the runtime, plus the same git identity the
    sandbox's own commits use — so ``git commit`` through the terminal works
    on hosts with no global git config, and lands as the same author."""
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("KIMI_", "DEEPSEEK_", "OPENAI_", "ANTHROPIC_")):
            del env[name]
    env.setdefault("GIT_AUTHOR_NAME", "fcode-runtime")
    env.setdefault("GIT_AUTHOR_EMAIL", "runtime@fcode.local")
    env.setdefault("GIT_COMMITTER_NAME", "fcode-runtime")
    env.setdefault("GIT_COMMITTER_EMAIL", "runtime@fcode.local")
    return env


def which(program: str) -> str | None:
    return shutil.which(program)

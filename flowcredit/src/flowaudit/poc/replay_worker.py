"""Constrained replay worker: the only process that runs probe scenarios.

Launched by the host replayer as a bare subprocess with a minimal
environment (no credentials, HOME/TMPDIR inside the audit workdir). The
audit hook makes the network and subprocess escape hatches fail loudly —
the constraints the evidence record claims are enforced here, in the
process that runs untrusted-influenced code.

Usage:
    python -m flowaudit.poc.replay_worker \
        --target <target-copy-root> --scenario <name> \
        --workdir <dir> --out <facts.json> [--params <json>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BLOCKED_EVENTS = {
    "socket.__new__", "socket.connect", "socket.bind", "socket.listen",
    "urllib.Request", "subprocess.Popen", "os.system", "os.exec",
    # os.putenv is allowed: the worker's env is already stripped at launch,
    # so writing env here cannot reveal anything — reads are the leak path,
    # and there is nothing left to read.
}


def _install_sandbox_hooks() -> None:
    def guard(event: str, args) -> None:  # noqa: ANN001 - audit hook signature
        if event in _BLOCKED_EVENTS:
            raise RuntimeError(f"audit sandbox: {event} is blocked in the replay worker")

    sys.addaudithook(guard)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--params", default="{}")
    args = parser.parse_args()

    _install_sandbox_hooks()

    # The worker imports the auditor's scenario modules, and the scenario
    # imports the TARGET's code from the copy. sys.path is arranged so the
    # target's own packages shadow nothing of the auditor's: the auditor's
    # src comes first, the target's src only where the scenario adds it.
    auditor_src = str(Path(__file__).resolve().parents[2])
    if auditor_src in sys.path:
        sys.path.remove(auditor_src)
    sys.path.insert(0, auditor_src)

    import importlib
    import re
    if not re.fullmatch(r"[a-z0-9_]+", args.scenario):
        raise RuntimeError(f"invalid scenario name: {args.scenario!r}")
    module = importlib.import_module(f"flowaudit.poc.scenarios.{args.scenario}")
    if not hasattr(module, "run"):
        raise RuntimeError(f"scenario {args.scenario} exposes no run()")

    params = json.loads(args.params)
    params.setdefault("workdir", args.workdir)
    facts = module.run(Path(args.target), params)

    Path(args.out).write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""File-backed oracle provider: a human (or another model) stands in for the LLM.

This is an experiment harness, not a production provider. The runtime still
renders the exact prompt that a real model would receive; instead of sending it
over HTTP, the provider writes it to an exchange directory and blocks until a
reply appears. The reply is parsed by the same ``parse_proposal_text`` used by
the local Qwen provider, so an oracle round exercises the real prompt contract,
schema validation, gating, execution and verification.

Protocol (one directory per experiment)::

    round-01.request.json   {"system", "user", "context", "prompt_version", ...}
    round-01.reply.json     {"content": "<assistant text>"}  (or a plan object)

Files are written atomically. A missing reply is an unknown outcome after the
deadline — never a silent retry and never a fallback to the mock.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..core.contracts import AgentError, PLAN_VERSION
from .base import Proposal
from .prompt import (
    build_proposal_plan,
    parse_proposal_text,
    prompt_profile_for,
    render_prompt,
)

__all__ = ["OracleFileProvider"]


class OracleFileProvider:
    """A blocking proposal source whose replies are written by an operator."""

    name = "oracle-file"
    model = "zcode-oracle-v1"

    def __init__(
        self,
        exchange_dir: str | Path,
        *,
        timeout: float = 900.0,
        poll_interval: float = 0.2,
    ) -> None:
        self.exchange_dir = Path(exchange_dir)
        self.exchange_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)

    # ------------------------------------------------------------------ files

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _read_reply(self, path: Path) -> str:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            raise ValueError("reply must be a string or an object")
        if "content" in payload:
            return str(payload["content"])
        if "steps" in payload:
            return json.dumps(payload, ensure_ascii=False)
        raise ValueError("reply needs 'content' or 'steps'")

    # ------------------------------------------------------------- interface

    def propose(self, context: dict[str, Any]) -> Proposal:
        started = time.monotonic()
        profile = prompt_profile_for(context)
        system, user = render_prompt(context)
        round_no = int(context.get("round") or 0)
        stem = f"round-{round_no:02d}"
        request_path = self.exchange_dir / f"{stem}.request.json"
        reply_path = self.exchange_dir / f"{stem}.reply.json"
        if reply_path.exists():
            reply_path.unlink()

        self._write(request_path, {
            "round": round_no,
            "prompt_version": profile.version,
            "system": system,
            "user": user,
            "context": context,
        })

        deadline = started + self.timeout
        while not reply_path.exists():
            if time.monotonic() >= deadline:
                raise AgentError(
                    "PROVIDER_TIMEOUT",
                    f"oracle reply missing for {stem} after {self.timeout:.0f}s",
                )
            time.sleep(self.poll_interval)

        try:
            content = self._read_reply(reply_path)
            data = parse_proposal_text(content)
        except (OSError, ValueError) as error:
            raise AgentError("PROPOSAL_MALFORMED", f"{error}: {stem}") from error

        latency_ms = int((time.monotonic() - started) * 1000)
        tokens = max(1, (len(system) + len(user) + len(content)) // 4)
        return Proposal(
            plan=build_proposal_plan(data),
            provider=self.name,
            model=self.model,
            tokens=tokens,
            cost_microusd=0,
            latency_ms=latency_ms,
            todo_id=data.get("todo_id"),
            metadata={
                "prompt_version": profile.version,
                "oracle": True,
                "billing": "not-billed",
                "exchange": str(request_path),
            },
        )

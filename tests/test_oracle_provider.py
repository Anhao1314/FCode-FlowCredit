"""The oracle provider must speak the same contract as a real model.

These tests pin the exchange protocol: the runtime writes the rendered prompt,
the operator writes a reply, and the runtime parses it with the same parser as
the local Qwen provider. A missing reply is an unknown outcome, not a retry.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError, PLAN_VERSION  # noqa: E402
from flowagent.providers.oracle import OracleFileProvider  # noqa: E402

CONTEXT = {
    "objective": "判断两期毛利率是否可比",
    "round": 1,
    "tools": [],
    "plan": [{"id": "t1", "tool": "list_documents", "arguments": {}, "status": "pending"}],
    "state": {},
    "observations": [],
    "budget": {},
    "instruction": "propose one action",
}


def answer(exchange: pathlib.Path, payload: dict, *, delay: float = 0.0) -> threading.Thread:
    def worker() -> None:
        request = exchange / "round-01.request.json"
        deadline = time.monotonic() + 3
        while not request.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if delay:
            time.sleep(delay)
        temporary = exchange / "round-01.reply.json.tmp"
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(exchange / "round-01.reply.json")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


class OracleProviderTests(unittest.TestCase):
    def test_renders_prompt_and_parses_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exchange = pathlib.Path(tmp)
            thread = answer(exchange, {
                "version": PLAN_VERSION,
                "steps": [{"id": "t1", "tool": "list_documents", "arguments": {}}],
                "todo_id": "t1",
            })
            provider = OracleFileProvider(exchange, timeout=3, poll_interval=0.01)
            proposal = provider.propose(CONTEXT)
            thread.join(timeout=3)
            self.assertEqual(proposal.todo_id, "t1")
            self.assertEqual(proposal.plan["steps"][0]["tool"], "list_documents")
            self.assertGreater(proposal.tokens, 0)
            request = json.loads((exchange / "round-01.request.json").read_text(encoding="utf-8"))
            self.assertIn("system", request)
            self.assertIn('"objective"', request["user"])
            self.assertEqual(request["prompt_version"], proposal.metadata["prompt_version"])

    def test_malformed_reply_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exchange = pathlib.Path(tmp)
            answer(exchange, {"content": "I think we should list documents."})
            provider = OracleFileProvider(exchange, timeout=3, poll_interval=0.01)
            with self.assertRaises(AgentError) as caught:
                provider.propose(CONTEXT)
            self.assertEqual(caught.exception.code, "PROPOSAL_MALFORMED")
            self.assertEqual(len(list(exchange.glob("round-01.request.json"))), 1)

    def test_missing_reply_times_out_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = OracleFileProvider(tmp, timeout=0.15, poll_interval=0.02)
            with self.assertRaises(AgentError) as caught:
                provider.propose(CONTEXT)
            self.assertEqual(caught.exception.code, "PROVIDER_TIMEOUT")


if __name__ == "__main__":
    unittest.main()

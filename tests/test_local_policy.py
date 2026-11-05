"""Local policy: choice-only output, one repair, no silent fallback."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN, verify_observation  # noqa: E402
from flowagent.providers.local_policy import LocalPolicy  # noqa: E402
from flowagent.providers.mock import MockProvider  # noqa: E402
from flowagent.providers.packet_prompt import render_policy_prompt  # noqa: E402
from flowagent.runtime.harness import Harness  # noqa: E402
from flowagent.runtime.loop import Loop  # noqa: E402
from flowagent.runtime.policy import ActionCandidate, DecisionPacket  # noqa: E402
from flowagent.tools.finance.fixtures import FinanceWorld  # noqa: E402
from flowagent.tools.finance.planner import FinancePlanner  # noqa: E402
from flowagent.tools.finance.tools import build_finance_tools  # noqa: E402
from flowagent.tools.registry import ToolRegistry  # noqa: E402

BODIES: list[dict] = []
REPLIES: list[str] = []
AUTO_FROM_PACKET = False


class _Model(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: A003 - silence test output
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        BODIES.append(body)
        if AUTO_FROM_PACKET:
            user = body.get("messages", [{}])[-1].get("content", "{}")
            try:
                packet = json.loads(user).get("packet") or {}
                content = json.dumps({"choice": packet["candidates"][0]["id"]})
            except (ValueError, KeyError, IndexError):
                content = '{"choice": "a1"}'
        else:
            content = REPLIES.pop(0) if REPLIES else '{"choice": "a1"}'
        payload = {
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 11},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def packet() -> DecisionPacket:
    return DecisionPacket(
        objective="test",
        round_no=1,
        state={"listed_documents": True},
        last_evidence=None,
        memory_cards=[],
        budget={"calls_remaining": 3},
        candidates=[
            ActionCandidate("a1", "t1", "list_documents", {}, "列出资料"),
            ActionCandidate("a2", "t2", "read_document", {"doc_id": "doc_fy2024"}, "读取年报"),
        ],
    )


class LocalPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Model)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        BODIES.clear()
        REPLIES.clear()

    def policy(self) -> LocalPolicy:
        return LocalPolicy(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            model="tiny",
            timeout=1,
        )

    def test_choice_is_parsed_and_validated(self) -> None:
        REPLIES.append('{"choice": "a2"}')
        choice = self.policy().choose(packet())
        self.assertEqual(choice.candidate_id, "a2")
        self.assertEqual(choice.tokens, 11)
        self.assertFalse(choice.repaired)
        self.assertEqual(len(BODIES), 1)
        self.assertTrue(any("a1" in message["content"] for message in BODIES[0]["messages"]))

    def test_invalid_output_gets_exactly_one_repair(self) -> None:
        REPLIES.extend(["not json", '{"choice": "a2"}'])
        choice = self.policy().choose(packet())
        self.assertEqual(choice.candidate_id, "a2")
        self.assertTrue(choice.repaired)
        self.assertEqual(choice.tokens, 22)
        self.assertEqual(len(BODIES), 2)
        self.assertEqual(BODIES[1]["messages"][-1]["role"], "user")

    def test_two_invalid_outputs_do_not_fall_back(self) -> None:
        REPLIES.extend(['{"choice": "a9"}', '{"choice": "a9"}'])
        with self.assertRaises(AgentError) as caught:
            self.policy().choose(packet())
        self.assertEqual(caught.exception.code, "POLICY_INVALID_CHOICE")
        self.assertEqual(len(BODIES), 2)

    def test_local_policy_drives_a_real_packet_run(self) -> None:
        REPLIES.extend([])
        # The handler needs to answer from the packet; switch it to auto mode.
        global AUTO_FROM_PACKET
        AUTO_FROM_PACKET = True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                scenario = get_scenario("full")
                store = TaskStore(pathlib.Path(tmp) / "task.sqlite")
                world = FinanceWorld(partial=scenario.partial_corpus)
                harness = Harness(
                    store=store,
                    registry=ToolRegistry(build_finance_tools(world)),
                    provider=MockProvider(policy=scenario.policy),
                    world=world,
                    limits=scenario.limits,
                    receipts=ReceiptStore(store),
                    verifier=lambda observation: verify_observation(world, observation),
                )
                task = harness.create(
                    task_id="task-local-policy",
                    objective=scenario.objective,
                    principal="test",
                    binding={"module": "finance-research"},
                    allowed_tools=list(scenario.allowed_tools),
                    grant=dict(scenario.grant),
                )
                loop = Loop(
                    harness,
                    planner=FinancePlanner(),
                    max_rounds=scenario.max_rounds,
                    policy=self.policy(),
                    result_tools=FINANCE_DOMAIN.result_tools,
                )
                task = loop.run(task.id)
                self.assertEqual(task.status, "succeeded")
                self.assertEqual(task.result["change_bp"], -250)
                types = [event["type"] for event in store.events(task.id)]
                self.assertGreaterEqual(types.count("policy_decided"), 7)
                self.assertGreaterEqual(types.count("auto_action"), 1)
                store.close()
        finally:
            AUTO_FROM_PACKET = False

    def test_prompt_is_compact_and_lists_only_candidates(self) -> None:
        system, user = render_policy_prompt(packet())
        self.assertIn("candidates", system)
        self.assertIn('<candidate id>', system)
        self.assertIn('"a1"', user)
        self.assertIn('"a2"', user)
        self.assertNotIn("normalize_units", user)
        self.assertIn("fcode.packet-prompt/v2", user)
        self.assertIn("先选读取、统一口径、核对重述或解除依赖的候选", system)


if __name__ == "__main__":
    unittest.main()

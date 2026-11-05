"""Domain-separated system prompts: routing, and the frozen finance contract.

One behaviour contract could not serve two lines of work. Every real provider
rendered the *financial* system prompt — rules about numeric evidence and about
reads being the only action — while the general domain's own instruction rode
along in a user message. The audit found the consequence in this repository's
own notes: a real run where the model proposed ``report_blocked`` because "the
current instruction says only read-only actions may be proposed".

These tests pin both halves of the fix: a general run gets the general contract,
and the financial v10 prompt stays byte-for-byte what the SFT golden hash covers.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.contracts import PLAN_VERSION  # noqa: E402
from flowagent.core.receipts import ReceiptStore  # noqa: E402
from flowagent.core.store import TaskStore  # noqa: E402
from flowagent.demo.fixtures import get_scenario  # noqa: E402
from flowagent.domains.finance import FINANCE_DOMAIN  # noqa: E402
from flowagent.domains.general import GENERAL_DOMAIN  # noqa: E402
from flowagent.providers.deepseek import DeepSeekProvider, PriceSnapshot  # noqa: E402
from flowagent.providers.kimi import KimiPriceSnapshot, KimiProvider  # noqa: E402
from flowagent.providers.local import LocalProvider  # noqa: E402
from flowagent.providers.openai_compat import (  # noqa: E402
    CompatSettings,
    OpenAICompatProvider,
)
from flowagent.providers.oracle import OracleFileProvider  # noqa: E402
from flowagent.providers.prompt import (  # noqa: E402
    DEFAULT_PROMPT_PROFILE,
    FINANCE_PROMPT_PROFILE,
    GENERAL_PROMPT_PROFILE,
    GENERAL_PROMPT_VERSION,
    GENERAL_SYSTEM_PROMPT,
    PROMPT_PROFILES,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    prompt_profile_for,
    render_prompt,
)
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402

#: The frozen v10 bytes, as captured before this phase began. Not a golden hash
#: to be regenerated: a second, independent witness that the text did not move.
FINANCE_SYSTEM_PROMPT_SHA256 = "ec0ba6d412b34fbb60682a0bd07bb8d4d233e3b0b728863a1f1cdc5b69693408"
FINANCE_SYSTEM_PROMPT_BYTES = 726

#: Substrings that make a prompt financial-only. A general run must inherit none.
FINANCIAL_ONLY_MARKERS = ("财务", "数值", "report_blocked 并说明原因")

REPLY = json.dumps({
    "version": PLAN_VERSION,
    "steps": [{"id": "s1", "tool": "read_file", "arguments": {"path": "x.py"}}],
    "todo_id": None,
})

GENERAL_CONTEXT = {
    "objective": "修好这个失败的测试",
    "round": 1,
    "tools": [],
    "plan": [],
    "state": {},
    "observations": [],
    "budget": {},
    "instruction": "每轮只提议一个动作",
    "prompt_profile": GENERAL_PROMPT_PROFILE.key,
    "intent": {"task_id": "t", "round": 1},
}
FINANCE_CONTEXT = {
    "objective": "判断两期毛利率是否可比",
    "round": 1,
    "tools": [],
    "plan": [],
    "state": {},
    "observations": [],
    "budget": {},
    "instruction": "Propose exactly one read-only action.",
    "intent": {"task_id": "t", "round": 1},
}


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _capturing_urlopen(seen: list[dict], content: str = REPLY):
    """A urlopen stand-in that records the system message it was handed."""

    def fake(request, timeout=None):  # noqa: ANN001
        body = json.loads(request.data.decode("utf-8"))
        messages = body.get("messages") or []
        seen.append({
            "url": request.full_url,
            "system": next((m.get("content") for m in messages if m.get("role") == "system"), None),
            "user": next((m.get("content") for m in messages if m.get("role") == "user"), None),
        })
        return _Response({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 3}})

    return fake


def _price() -> PriceSnapshot:
    return PriceSnapshot(
        input_microusd_per_mtok=1_000_000,
        output_microusd_per_mtok=2_000_000,
        as_of="2026-10-26",
    )


class RendererTests(unittest.TestCase):
    """The renderer is the single choke point every provider funnels through."""

    def test_general_context_selects_the_general_system_prompt(self) -> None:
        """A."""
        system, user = render_prompt(GENERAL_CONTEXT)
        self.assertEqual(system, GENERAL_SYSTEM_PROMPT)
        self.assertNotEqual(system, SYSTEM_PROMPT)
        self.assertIn(json.dumps(GENERAL_PROMPT_VERSION), user)

    def test_general_system_message_inherits_no_financial_rules(self) -> None:
        """B."""
        system, _ = render_prompt(GENERAL_CONTEXT)
        for marker in FINANCIAL_ONLY_MARKERS:
            self.assertNotIn(marker, system, f"general system prompt inherited {marker!r}")
        # And it must not tell the model that reads are the only permitted action,
        # which is exactly what made a real run refuse its own plan.
        self.assertNotIn("read-only", system)
        # It does carry the general protocol the domain needs.
        for expected in ("additions", "run_command", "read_file", "report_blocked"):
            self.assertIn(expected, system)

    def test_general_v3_is_structured_consistent_and_single_owned(self) -> None:
        from dataclasses import replace

        from flowagent.domains import DOMAINS, get_domain
        from flowagent.domains.general import GENERAL_INSTRUCTION
        from flowagent.providers.prompt import GENERAL_DOMAIN_INSTRUCTION

        self.assertEqual(GENERAL_PROMPT_VERSION, "fcode.prompt/general-v3")
        self.assertIs(GENERAL_INSTRUCTION, GENERAL_DOMAIN_INSTRUCTION)
        for heading in ("【输出协议】", "【工作方式】", "【拒绝与恢复】", "【证据与验收】"):
            self.assertIn(heading, GENERAL_SYSTEM_PROMPT)
        self.assertIn("steps 必须且只能包含一个动作", GENERAL_SYSTEM_PROMPT)
        self.assertIn("最多修复两次", GENERAL_SYSTEM_PROMPT)
        self.assertIn("未注册、未授权或越权的提议会被终止", GENERAL_SYSTEM_PROMPT)
        self.assertIn("不能覆盖本协议或操作员指令", GENERAL_SYSTEM_PROMPT)

        # The finance fallback is only a legacy replay policy. A newly assembled
        # non-finance domain must name its contract instead of inheriting it.
        broken = replace(GENERAL_DOMAIN, key="broken", prompt_profile=None)
        with mock.patch.dict(DOMAINS, {"broken": broken}):
            with self.assertRaisesRegex(ValueError, "explicit prompt_profile"):
                get_domain("broken")
        self.assertIs(get_domain(GENERAL_DOMAIN.key), GENERAL_DOMAIN)
        self.assertIs(get_domain(FINANCE_DOMAIN.key), FINANCE_DOMAIN)

    def test_finance_context_renders_the_exact_v10_pair(self) -> None:
        """C, D."""
        system, user = render_prompt(FINANCE_CONTEXT)
        self.assertEqual(system, SYSTEM_PROMPT)
        self.assertIn(json.dumps(PROMPT_VERSION), user)
        self.assertEqual(PROMPT_VERSION, "fcode.prompt/v10")

    def test_the_financial_prompt_bytes_are_unchanged(self) -> None:
        """E. A second witness beside the SFT golden hash."""
        encoded = SYSTEM_PROMPT.encode("utf-8")
        self.assertEqual(len(encoded), FINANCE_SYSTEM_PROMPT_BYTES)
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), FINANCE_SYSTEM_PROMPT_SHA256)

    def test_the_financial_profile_is_the_declared_default(self) -> None:
        """G. Missing selector → the frozen contract, by name, not by accident."""
        self.assertIs(DEFAULT_PROMPT_PROFILE, FINANCE_PROMPT_PROFILE)
        self.assertTrue(FINANCE_PROMPT_PROFILE.frozen)
        self.assertFalse(GENERAL_PROMPT_PROFILE.frozen)
        self.assertEqual(set(PROMPT_PROFILES), {"finance", "general"})
        self.assertIs(prompt_profile_for({}), FINANCE_PROMPT_PROFILE)
        self.assertIs(prompt_profile_for({"prompt_profile": "general"}), GENERAL_PROMPT_PROFILE)

    def test_an_unknown_profile_fails_loudly_instead_of_misrouting(self) -> None:
        """G. Never silently Finance, never silently General."""
        from flowagent.core.contracts import AgentError

        for bad in ("financial-research", "GENERAL", "", 7, ["general"]):
            with self.subTest(profile=bad):
                with self.assertRaises(AgentError) as ctx:
                    render_prompt({"prompt_profile": bad})
                self.assertIn(
                    ctx.exception.code, ("PROMPT_PROFILE_UNKNOWN", "PROMPT_PROFILE_INVALID"),
                )

    def test_the_memory_layer_omits_the_key_for_a_default_profile(self) -> None:
        """E, G. The byte guarantee lives in the context assembly, not in a
        convention callers have to remember: a default profile never emits the
        selector, so no finance context (and no SFT sample) gains a key."""
        from flowagent.memory.context import WorkingMemory

        common = dict(objective="o", tool_catalog=[])
        default = WorkingMemory(**common, prompt_profile=None).build(1, {})
        self.assertNotIn("prompt_profile", default)
        explicit_default = WorkingMemory(
            **common, prompt_profile=FINANCE_PROMPT_PROFILE.key,
        ).build(1, {})
        # A domain may name the default explicitly; it must not be a compiler
        # directive to change the bytes, so the assembly still emits nothing.
        # (GENERAL_DOMAIN names "general"; FINANCE_DOMAIN names nothing at all.)
        self.assertIn("prompt_profile", explicit_default)
        general = WorkingMemory(**common, prompt_profile=GENERAL_PROMPT_PROFILE.key).build(1, {})
        self.assertEqual(general["prompt_profile"], "general")

    def test_the_missing_selector_and_an_explicit_null_resolve_alike(self) -> None:
        """G. Resolution treats both as 'no selection'; only the dict bytes
        differ, and the assembly never produces the null form."""
        self.assertIs(prompt_profile_for({}), prompt_profile_for({"prompt_profile": None}))
        self.assertIs(prompt_profile_for({}), DEFAULT_PROMPT_PROFILE)


class DomainDeclarationTests(unittest.TestCase):
    """G, H. The domain decides, and the instruction keeps its message role."""

    def test_finance_declares_no_profile_so_its_contexts_never_carry_one(self) -> None:
        self.assertIsNone(FINANCE_DOMAIN.prompt_profile)
        self.assertEqual(GENERAL_DOMAIN.prompt_profile, "general")

    def test_plan_extension_is_a_general_only_capability(self) -> None:
        self.assertTrue(GENERAL_DOMAIN.plan_extension)
        self.assertFalse(FINANCE_DOMAIN.plan_extension)

    def test_a_general_run_renders_the_general_pair_end_to_end(self) -> None:
        """A, H. Real stack, real context, no mock in the routing path."""
        from flowagent.providers.mock import MockProvider
        from flowagent.runtime.harness import Harness
        from flowagent.runtime.loop import Loop
        from flowagent.tools.registry import ToolRegistry

        captured: list[dict] = []

        class Capture:
            name = "capture"
            model = "capture-v1"

            def __init__(self) -> None:
                self.inner = MockProvider(policy="research")

            def propose(self, context):
                captured.append(context)
                return self.inner.propose(context)

        with tempfile.TemporaryDirectory() as tmp:
            scenario = get_scenario("general-research")
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            world = GENERAL_DOMAIN.create_world(
                spec={"mode": "script", "scriptPath": "s.py"},
                sandbox=WorkspaceSandbox(pathlib.Path(tmp) / "ws"),
            )
            harness = Harness(
                store=store,
                registry=ToolRegistry(GENERAL_DOMAIN.build_tools(world)),
                provider=Capture(),
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: GENERAL_DOMAIN.verify(world, observation),
                state_fn=GENERAL_DOMAIN.summarize_state,
                instruction=GENERAL_DOMAIN.instruction,
                prompt_profile=GENERAL_DOMAIN.prompt_profile,
                plan_extension=GENERAL_DOMAIN.plan_extension,
            )
            task = harness.create(
                task_id="routing",
                objective="写一个脚本并归档结果",
                principal="test",
                binding={"scenario": scenario.key},
                allowed_tools=list(scenario.allowed_tools),
                grant=scenario.grant,
            )
            Loop(
                harness,
                planner=GENERAL_DOMAIN.make_planner(world),
                max_rounds=1,
                result_tools=GENERAL_DOMAIN.result_tools,
            ).run(task.id)
            store.close()

        self.assertEqual(len(captured), 1)
        context = captured[0]
        self.assertEqual(context["prompt_profile"], "general")
        system, user = render_prompt(context)
        self.assertEqual(system, GENERAL_SYSTEM_PROMPT)
        # The domain's scoped instruction still travels in the user context;
        # behavioural rules have one owner in the system contract.
        self.assertEqual(context["instruction"], GENERAL_DOMAIN.instruction)
        self.assertIn(json.dumps(GENERAL_PROMPT_VERSION), user)

    def test_a_finance_run_emits_no_selector_and_keeps_the_default(self) -> None:
        """C, E, H."""
        from flowagent.domains.finance import FinanceWorld  # noqa: F401  (import parity)
        from flowagent.providers.mock import MockProvider
        from flowagent.runtime.harness import Harness
        from flowagent.runtime.loop import Loop
        from flowagent.tools.finance.fixtures import FinanceWorld as World
        from flowagent.tools.finance.tools import build_finance_tools
        from flowagent.tools.registry import ToolRegistry

        captured: list[dict] = []

        class Capture:
            name = "capture"
            model = "capture-v1"

            def __init__(self) -> None:
                self.inner = MockProvider(policy="research")

            def propose(self, context):
                captured.append(context)
                return self.inner.propose(context)

        with tempfile.TemporaryDirectory() as tmp:
            scenario = get_scenario("full")
            store = TaskStore(pathlib.Path(tmp) / "t.sqlite")
            world = World()
            harness = Harness(
                store=store,
                registry=ToolRegistry(build_finance_tools(world)),
                provider=Capture(),
                world=world,
                limits=scenario.limits,
                receipts=ReceiptStore(store),
                verifier=lambda observation: FINANCE_DOMAIN.verify(world, observation),
                state_fn=FINANCE_DOMAIN.summarize_state,
                instruction=FINANCE_DOMAIN.instruction,
                prompt_profile=FINANCE_DOMAIN.prompt_profile,
                plan_extension=FINANCE_DOMAIN.plan_extension,
            )
            task = harness.create(
                task_id="routing-finance",
                objective="判断恒星智算两期毛利率是否可比",
                principal="test",
                binding={"scenario": scenario.key},
                allowed_tools=list(scenario.allowed_tools),
                grant=scenario.grant,
            )
            Loop(
                harness,
                planner=FINANCE_DOMAIN.make_planner(world),
                max_rounds=1,
                result_tools=FINANCE_DOMAIN.result_tools,
            ).run(task.id)
            store.close()

        self.assertEqual(len(captured), 1)
        self.assertNotIn("prompt_profile", captured[0])
        system, user = render_prompt(captured[0])
        self.assertEqual(system, SYSTEM_PROMPT)
        self.assertIn('"prompt_version": "fcode.prompt/v10"', user)


class AdapterRoutingTests(unittest.TestCase):
    """F, I. Every supported adapter routes by domain, not by the mock."""

    def _propose(self, provider, context, content: str = REPLY):
        seen: list[dict] = []
        with mock.patch("urllib.request.urlopen", _capturing_urlopen(seen, content)):
            provider.propose(dict(context))
        return seen

    def test_openai_compatible_engine(self) -> None:
        settings = CompatSettings(
            label="compat", api_key_env="X", api_key="sk-test",
            base_url="https://endpoint.example/v1", model="m", price=_price(),
        )
        provider = OpenAICompatProvider.from_settings(settings)
        general = self._propose(provider, GENERAL_CONTEXT)
        finance = self._propose(provider, FINANCE_CONTEXT)
        self.assertEqual(general[0]["system"], GENERAL_SYSTEM_PROMPT)
        self.assertEqual(finance[0]["system"], SYSTEM_PROMPT)

    def test_deepseek_preset(self) -> None:
        provider = DeepSeekProvider(api_key="sk-test", price=_price())
        general = self._propose(provider, GENERAL_CONTEXT)
        finance = self._propose(provider, FINANCE_CONTEXT)
        self.assertEqual(general[0]["system"], GENERAL_SYSTEM_PROMPT)
        self.assertEqual(finance[0]["system"], SYSTEM_PROMPT)
        self.assertEqual(general[0]["url"], "https://api.deepseek.com/v1/chat/completions")

    def test_kimi_preset(self) -> None:
        provider = KimiProvider(
            api_key="sk-kimi-test-not-real",
            price=KimiPriceSnapshot(
                input_microusd_per_mtok=1_000_000,
                output_microusd_per_mtok=2_000_000,
                as_of="2026-10-26",
            ),
        )
        general = self._propose(provider, GENERAL_CONTEXT)
        finance = self._propose(provider, FINANCE_CONTEXT)
        self.assertEqual(general[0]["system"], GENERAL_SYSTEM_PROMPT)
        self.assertEqual(finance[0]["system"], SYSTEM_PROMPT)

    def test_local_provider(self) -> None:
        provider = LocalProvider(base_url="http://127.0.0.1:11434/v1")
        general = self._propose(provider, GENERAL_CONTEXT)
        finance = self._propose(provider, FINANCE_CONTEXT)
        self.assertEqual(general[0]["system"], GENERAL_SYSTEM_PROMPT)
        self.assertEqual(finance[0]["system"], SYSTEM_PROMPT)

    def test_oracle_file_provider(self) -> None:
        """The exchange file carries the rendered pair, so the human standing in
        for the model sees the same contract a real one would."""
        with tempfile.TemporaryDirectory() as tmp:
            provider = OracleFileProvider(pathlib.Path(tmp), timeout=0.05)
            with self.assertRaises(Exception):
                provider.propose(dict(GENERAL_CONTEXT))  # no reply → unknown outcome
            request = json.loads(
                (pathlib.Path(tmp) / "round-01.request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(request["system"], GENERAL_SYSTEM_PROMPT)
            self.assertEqual(request["prompt_version"], GENERAL_PROMPT_VERSION)

    def test_the_recorded_prompt_version_follows_the_profile(self) -> None:
        """I. The receipt says which contract produced the proposal."""
        settings = CompatSettings(
            label="compat", api_key_env="X", api_key="sk-test",
            base_url="https://endpoint.example/v1", model="m", price=_price(),
        )
        provider = OpenAICompatProvider.from_settings(settings)
        seen: list[dict] = []
        with mock.patch("urllib.request.urlopen", _capturing_urlopen(seen)):
            general = provider.propose(dict(GENERAL_CONTEXT))
        seen.clear()
        with mock.patch("urllib.request.urlopen", _capturing_urlopen(seen)):
            finance = provider.propose(dict(FINANCE_CONTEXT))
        self.assertEqual(general.metadata["prompt_version"], GENERAL_PROMPT_VERSION)
        self.assertEqual(finance.metadata["prompt_version"], PROMPT_VERSION)

    def test_no_adapter_carries_its_own_system_prompt(self) -> None:
        """I. Routing lives in prompt.py; an adapter that inlined prompt text
        could drift from the contract (and from the SFT set) unnoticed.

        The two presets are pinned to the shared engine by inheritance, which is
        why they name neither renderer nor profile: they cannot render at all
        without going through :func:`render_prompt`. What must never appear is a
        copy of the prompt text itself.
        """
        import flowagent.providers.deepseek as deepseek_module
        import flowagent.providers.kimi as kimi_module
        import flowagent.providers.local as local_module
        import flowagent.providers.openai_compat as compat_module
        import flowagent.providers.oracle as oracle_module

        from flowagent.providers.openai_compat import OpenAICompatProvider

        adapters = (compat_module, deepseek_module, kimi_module, local_module, oracle_module)
        for module in adapters:
            with self.subTest(module=module.__name__):
                source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
                self.assertNotIn(SYSTEM_PROMPT, source)
                self.assertNotIn(GENERAL_SYSTEM_PROMPT, source)
        # The presets cannot render a prompt of their own: they inherit it.
        for preset in (deepseek_module.DeepSeekProvider, kimi_module.KimiProvider):
            with self.subTest(preset=preset.__name__):
                self.assertTrue(issubclass(preset, OpenAICompatProvider))
                self.assertIs(preset.propose, OpenAICompatProvider.propose)
        # Exactly one module owns each contract.
        self.assertIn("SYSTEM_PROMPT", pathlib.Path(
            __import__("flowagent.providers.prompt", fromlist=["x"]).__file__,
        ).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
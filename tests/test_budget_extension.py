"""S2a 预算延展:封底、理由、生效与工具注册。

覆盖三层:
1. 通道数值判定(单次 50% / 累计 100% / 理由必填)——纯逻辑;
2. 工具层(handler 成功/失败路径、观察形状);
3. 装配层(world 有通道才注册;general-research 授权两处都含该工具)。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "viewer"))

from flowagent.core.contracts import AgentError  # noqa: E402
from flowagent.tools.general.budget import build_budget_extension_tool  # noqa: E402
from flowagent.tools.general.sandbox import WorkspaceSandbox  # noqa: E402
from flowagent.tools.general.tools import build_general_tools  # noqa: E402
from console_budget_extension import BudgetChannel  # noqa: E402


def _driver(max_calls=20, max_reads=40, max_cost=100_000, max_tokens=50_000):
    from flowagent.runtime.budget import BudgetLimits

    limits = BudgetLimits(max_calls, max_reads, max_cost, max_tokens)
    harness = SimpleNamespace(limits=limits)
    return SimpleNamespace(limits=limits, base_limits=limits, harness=harness)


class BudgetChannelTest(unittest.TestCase):
    def test_grant_extends_limits_and_snapshots_values(self) -> None:
        driver = _driver()
        channel = BudgetChannel(driver)
        result = channel.request(reason="长任务还差几轮", extra_calls=5, extra_cost_microusd=20_000)
        self.assertTrue(result["granted"])
        self.assertEqual(result["extra_calls"], 5)
        self.assertEqual(result["limits"]["max_calls"], 25)
        self.assertEqual(result["limits"]["max_cost_microusd"], 120_000)
        self.assertEqual(driver.harness.limits.max_calls, 25)
        self.assertEqual(driver.limits.max_calls, 25)

    def test_reason_required(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            BudgetChannel(_driver()).request(reason="  ", extra_calls=5)
        self.assertEqual(ctx.exception.code, "BUDGET_EXTENSION_INVALID")

    def test_zero_delta_rejected(self) -> None:
        with self.assertRaises(AgentError) as ctx:
            BudgetChannel(_driver()).request(reason="合理理由", extra_calls=0, extra_cost_microusd=0)
        self.assertEqual(ctx.exception.code, "BUDGET_EXTENSION_INVALID")

    def test_negative_and_non_integer_rejected(self) -> None:
        for bad in (-1, "5", 1.5, True):
            with self.assertRaises(AgentError):
                BudgetChannel(_driver()).request(reason="理由", extra_calls=bad)

    def test_per_request_cap_is_half_of_original(self) -> None:
        driver = _driver(max_calls=10)
        with self.assertRaises(AgentError) as ctx:
            BudgetChannel(driver).request(reason="理由", extra_calls=6)
        self.assertEqual(ctx.exception.code, "BUDGET_EXTENSION_REFUSED")
        self.assertEqual(ctx.exception.details.get("cap"), "per_request")
        # 恰好 50% 允许
        result = BudgetChannel(_driver(max_calls=10)).request(reason="理由", extra_calls=5)
        self.assertTrue(result["granted"])

    def test_cumulative_cap_is_original_ceiling(self) -> None:
        driver = _driver(max_calls=10)
        channel = BudgetChannel(driver)
        channel.request(reason="第一次", extra_calls=5)   # 累计 5
        channel.request(reason="第二次", extra_calls=5)   # 累计 10 = 原始上限(总预算 2x)
        with self.assertRaises(AgentError) as ctx:
            channel.request(reason="第三次", extra_calls=1)  # 累计 11 > 10 → 拒
        self.assertEqual(ctx.exception.details.get("cap"), "cumulative")
        # 失败请求不污染已生效的上限
        self.assertEqual(driver.harness.limits.max_calls, 20)

    def test_base_limits_lazy_resolution(self) -> None:
        """通道先于 limits 构造:封底参照 driver.base_limits,不受已延展值漂移。"""
        driver = _driver(max_calls=10)
        driver.base_limits = None  # 模拟"limits 还没定稿"
        channel = BudgetChannel(driver)
        driver.base_limits = driver.limits
        result = channel.request(reason="理由", extra_calls=5)
        self.assertTrue(result["granted"])


class BudgetToolTest(unittest.TestCase):
    def test_tool_grants_through_channel(self) -> None:
        driver = _driver()
        tool = build_budget_extension_tool(BudgetChannel(driver).request)
        result = tool.handler({"reason": "需要更多轮次", "extra_calls": 4})
        self.assertTrue(result["granted"])
        self.assertEqual(driver.harness.limits.max_calls, 24)

    def test_tool_raises_agent_error_on_refusal(self) -> None:
        tool = build_budget_extension_tool(BudgetChannel(_driver(max_calls=4)).request)
        with self.assertRaises(AgentError):
            tool.handler({"reason": "理由", "extra_calls": 4})


def _world_with(tmp: str, **kwargs):
    from flowagent.tools.general.world import GeneralWorld
    return GeneralWorld({}, sandbox=WorkspaceSandbox(Path(tmp) / "ws"), **kwargs)


class BudgetRegistrationTest(unittest.TestCase):
    def test_tool_registered_only_with_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            without = {spec.name for spec in _world_with(tmp).build_tools()}
            self.assertNotIn("request_budget_extension", without, "无通道不注册")
            with_channel = {spec.name for spec in _world_with(
                tmp, budget_channel=BudgetChannel(_driver())).build_tools()}
            self.assertIn("request_budget_extension", with_channel)

    def test_tool_effect_is_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            specs = {spec.name: spec for spec in _world_with(
                tmp, budget_channel=BudgetChannel(_driver())).build_tools()}
            self.assertEqual(specs["request_budget_extension"].effect, "write")

    def test_general_research_grants_both_places(self) -> None:
        from flowagent.demo.fixtures import SCENARIOS
        scenario = SCENARIOS["general-research"]
        self.assertIn("request_budget_extension", scenario.allowed_tools)
        self.assertIn("request_budget_extension", scenario.grant["allowed_tools"])
        # 子代理没有预算延展:它的小预算是硬封顶,放大只能由父层做
        subagent = SCENARIOS["general-subagent"]
        self.assertNotIn("request_budget_extension", subagent.allowed_tools)


if __name__ == "__main__":
    unittest.main()

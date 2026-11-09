"""编排引擎选型：原生 Loop（默认）或 LangGraph 编排（FCODE_ENGINE=langgraph）。

仓库本体保持零第三方依赖：langgraph 是可选依赖，未安装时 langgraph 引擎
不可选（显式报错，不静默回退——回退会让"以为在跑治理编排"变成谎言）。
"""

from __future__ import annotations

import os

__all__ = ["build_loop", "ENGINE_ENV"]

ENGINE_ENV = "FCODE_ENGINE"


def build_loop(harness, **kwargs):
    """Construct the orchestration engine selected by ``FCODE_ENGINE``.

    ``native``（默认）: runtime.loop.Loop —— 零依赖 while 循环。
    ``langgraph``:      runtime.graph_loop.GraphLoop —— StateGraph 编排，
    治理层（gate/预算/回执/哈希链/计划结算）作为节点内卫兵原样保留。
    """
    engine = os.environ.get(ENGINE_ENV, "native").strip().lower()
    if engine == "langgraph":
        from .graph_loop import GraphLoop

        return GraphLoop(harness, **kwargs)
    if engine in ("", "native"):
        from .loop import Loop

        return Loop(harness, **kwargs)
    raise ValueError(f"unknown {ENGINE_ENV}: {engine!r} (native or langgraph)")

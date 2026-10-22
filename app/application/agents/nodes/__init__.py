# -*- coding: utf-8 -*-
"""nodes 包：父图各节点的工厂 / 条件边实现。

每个节点一个模块，依赖（LLM / settings / broker / task_store / throttle / tracer）
全部通过 ``make_*`` 工厂参数传入，图装配只负责 add_node / add_edge。
"""
from app.application.agents.nodes.compress import make_compress_node
from app.application.agents.nodes.main_agent import make_main_agent_node
from app.application.agents.nodes.planner import make_planner_node
from app.application.agents.nodes.preference import make_preference_node
from app.application.agents.nodes.routes import (
    make_planner_router,
    make_trade_router,
    route_after_planner,
    route_next_trade,
)
from app.application.agents.nodes.trade_chain import make_trade_chain_node

__all__ = [
    "make_compress_node",
    "make_main_agent_node",
    "make_planner_node",
    "make_preference_node",
    "make_planner_router",
    "make_trade_router",
    "make_trade_chain_node",
    "route_after_planner",
    "route_next_trade",
]

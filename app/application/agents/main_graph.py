# -*- coding: utf-8 -*-
"""兼容层：保留旧 ``app.application.agents.main_graph`` 模块的全部公开名称。

P14 重构后真正的实现拆分到：

- ``state.py``：``MainState`` / reducers；
- ``nodes/``：planner / compress / preference / main_agent / routes 等节点；
- ``subagents/``：search / trade 子图与子 Agent 节点工厂；
- ``graph.py``：图装配（``build_main_graph``）；
- ``runtime.py``：``invoke_agent`` / ``invoke_agent_stream`` 等运行时入口。

本模块只做 re-export，保证旧导入路径与签名不变。
"""
from __future__ import annotations

from app.application.agents.compression import compress_messages, estimate_tokens  # noqa: F401
from app.application.agents.graph import build_main_graph
from app.application.agents.nodes.planner import _PLANNER_PROMPT  # noqa: F401
from app.application.agents.nodes.routes import (  # noqa: F401
    _inspect_trade_result,
    _last_message_content,
    _publish_dispatch,
    _register_subtask,
    _status_text,
    _subagent_marker_in,
    route_after_planner,
    route_next_trade,
)
from app.application.agents.runtime import (  # noqa: F401
    _chunk_token,
    _final_text,
    invoke_agent,
    invoke_agent_stream,
)
from app.application.agents.state import MainState, TradeSubState  # noqa: F401
from app.application.agents.subagents import (  # noqa: F401
    SUBAGENT_UNAVAILABLE,
    PlannerDecision,
    SubTaskSpec,
    build_search_agent_graph,
    build_subagent_node,
    build_trade_agent_graph,
    subtask_task_id,
)

__all__ = [
    "SUBAGENT_UNAVAILABLE",
    "MainState",
    "TradeSubState",
    "PlannerDecision",
    "SubTaskSpec",
    "build_main_graph",
    "build_search_agent_graph",
    "build_subagent_node",
    "build_trade_agent_graph",
    "subtask_task_id",
    "route_after_planner",
    "route_next_trade",
    "invoke_agent",
    "invoke_agent_stream",
]

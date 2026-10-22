# -*- coding: utf-8 -*-
"""P14 Task 1 兼容性测试：Agents 层拆分后旧导入路径 / 签名 / 模块归属保持不变。

只验证「结构 + 导入兼容」，行为回归由既有 test_planner / test_trade_chain /
test_subagent_graph 等用例覆盖。
"""
from __future__ import annotations

import inspect

from langgraph.types import Send

from app.application.agents import graph as graph_module
from app.application.agents import state as state_module
from app.application.agents import subagents as subagents_pkg
from app.application.agents.nodes import routes as routes_module
from app.application.agents.subagents import base as base_module
from app.application.agents.subagents import search as search_module
from app.application.agents.subagents import trade as trade_module


def test_legacy_main_graph_exports_still_work():
    from app.application.agents.main_graph import (  # noqa: PLC0415
        _final_text,
        build_main_graph,
        build_subagent_node,
        invoke_agent,
        invoke_agent_stream,
        route_after_planner,
        route_next_trade,
    )

    assert callable(build_main_graph)
    assert callable(route_after_planner)
    assert callable(route_next_trade)
    assert callable(invoke_agent)
    assert callable(invoke_agent_stream)
    assert callable(_final_text)
    assert callable(build_subagent_node)


def test_legacy_subagents_exports_still_work():
    from app.application.agents.subagents import (  # noqa: PLC0415
        SUBAGENT_UNAVAILABLE,
        MainState,
        PlannerDecision,
        SubTaskSpec,
        build_search_agent_graph,
        build_subagent_node,
        build_trade_agent_graph,
        subtask_task_id,
    )

    assert SUBAGENT_UNAVAILABLE == "[subagent unavailable]"
    assert MainState is state_module.MainState
    assert callable(PlannerDecision)
    assert callable(SubTaskSpec)
    assert callable(build_search_agent_graph)
    assert callable(build_trade_agent_graph)
    assert callable(build_subagent_node)
    assert callable(subtask_task_id)


def test_legacy_single_agent_graph_imports_still_work():
    from app.application.agents.graph import (  # noqa: PLC0415
        AgentState,
        build_agent_graph,
        invoke_agent,
    )

    assert callable(build_agent_graph)
    assert callable(invoke_agent)
    # P0 图与主图的 invoke_agent 各自独立，graph.py 仍导出 P0 版本。
    assert invoke_agent.__module__ == "app.application.agents.single_agent"
    assert "messages" in AgentState.__annotations__


def test_state_module_owns_schema_and_reducers():
    reducers = state_module
    assert reducers._last_update("old", "new") == "new"
    assert reducers._last_update("old", None) == "old"
    merged = reducers._merge_task_failures(
        [{"task_id": "t1", "status": "FAILED"}],
        [{"task_id": "t1", "status": "FAILED"}, {"task_id": "t2", "status": "FAILED"}],
    )
    assert merged == [
        {"task_id": "t1", "status": "FAILED"},
        {"task_id": "t2", "status": "FAILED"},
    ]
    assert "task_failures" in state_module.MainState.__annotations__
    assert "preference_hint" not in state_module.TradeSubState.__annotations__


def test_nodes_live_in_nodes_package():
    from app.application.agents.nodes import (  # noqa: PLC0415
        make_compress_node,
        make_main_agent_node,
        make_planner_node,
        make_preference_node,
        make_trade_chain_node,
    )

    assert callable(make_planner_node)
    assert callable(make_compress_node)
    assert callable(make_preference_node)
    assert callable(make_main_agent_node)
    assert callable(make_trade_chain_node)
    assert routes_module.route_after_planner.__module__ == (
        "app.application.agents.nodes.routes"
    )
    assert routes_module.route_next_trade.__module__ == (
        "app.application.agents.nodes.routes"
    )


def test_subagents_are_split_into_modules():
    assert search_module.build_search_agent_graph.__module__ == (
        "app.application.agents.subagents.search"
    )
    assert trade_module.build_trade_agent_graph.__module__ == (
        "app.application.agents.subagents.trade"
    )
    assert base_module.subtask_task_id.__module__ == (
        "app.application.agents.subagents.base"
    )
    assert base_module.build_subagent_node.__module__ == (
        "app.application.agents.subagents.base"
    )
    assert subagents_pkg.MainState is state_module.MainState


def test_graph_module_only_assembles_and_runtime_lives_elsewhere():
    from app.application.agents import runtime  # noqa: PLC0415
    from app.application.agents.main_graph import build_main_graph  # noqa: PLC0415

    assert graph_module.build_main_graph is build_main_graph
    assert build_main_graph.__module__ == "app.application.agents.graph"
    assert runtime._final_text.__module__ == "app.application.agents.runtime"
    assert runtime.invoke_agent.__module__ == "app.application.agents.runtime"
    assert runtime.invoke_agent_stream.__module__ == "app.application.agents.runtime"


def test_build_main_graph_signature_unchanged():
    from app.application.agents.main_graph import build_main_graph  # noqa: PLC0415

    params = inspect.signature(build_main_graph).parameters
    assert list(params)[:3] == ["settings", "main_llm", "planner_llm"]
    assert params["args"].kind is inspect.Parameter.VAR_POSITIONAL
    for name in (
        "search_llm",
        "trade_llm",
        "all_tools",
        "search_tools",
        "trade_tools",
        "main_tools",
        "checkpointer",
        "compression_llm",
        "broker",
        "preference_store",
        "preference_top_k",
        "throttle",
        "tracer",
        "task_store",
    ):
        assert name in params, name
        assert params[name].default is None or name == "preference_top_k"


def test_route_after_planner_compat_behavior():
    import asyncio  # noqa: PLC0415

    from app.application.agents.main_graph import route_after_planner  # noqa: PLC0415
    from app.application.agents.subagents import PlannerDecision, SubTaskSpec  # noqa: PLC0415

    direct = {"decision": PlannerDecision(route="direct", reasoning="x", tasks=[]).model_dump()}
    assert asyncio.run(route_after_planner(direct)) == "main_agent"

    spec = SubTaskSpec(agent="search_agent", instruction="找旅行三件套")
    search = route_after_planner(
        {"decision": PlannerDecision(route="search", reasoning="x", tasks=[spec]).model_dump()}
    )
    sends = asyncio.run(search)
    assert isinstance(sends, list) and isinstance(sends[0], Send)
    assert sends[0].node == "search_agent"

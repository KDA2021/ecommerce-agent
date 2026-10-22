# -*- coding: utf-8 -*-
from langgraph.types import Send

from app.application.agents.main_graph import route_after_planner
from app.application.agents.subagents import MainState, PlannerDecision, SubTaskSpec


def _state(route: str, tasks=None) -> dict:
    decision = PlannerDecision(route=route, reasoning="test", tasks=tasks or [])
    return {"decision": decision.model_dump()}


async def test_route_direct_returns_main_agent():
    assert await route_after_planner(_state("direct")) == "main_agent"


async def test_route_search_returns_one_send_to_search_agent():
    result = await route_after_planner(
        _state("search", [SubTaskSpec(agent="search_agent", instruction="找旅行三件套")])
    )
    assert isinstance(result, list)
    assert len(result) == 1
    send = result[0]
    assert isinstance(send, Send)
    assert send.node == "search_agent"
    assert send.arg["subtask"]["instruction"] == "找旅行三件套"


async def test_route_search_parallel_returns_multiple_sends():
    tasks = [
        SubTaskSpec(agent="search_agent", instruction="找无塑料旅行三件套"),
        SubTaskSpec(agent="search_agent", instruction="找可折叠水杯"),
    ]
    result = await route_after_planner(_state("search_parallel", tasks))
    assert isinstance(result, list)
    assert [send.node for send in result] == ["search_agent", "search_agent"]
    assert [send.arg["subtask"]["instruction"] for send in result] == [
        "找无塑料旅行三件套",
        "找可折叠水杯",
    ]


async def test_route_trade_returns_trade_agent_send():
    result = await route_after_planner(
        _state("trade", [SubTaskSpec(agent="trade_agent", instruction="查询订单 O1")])
    )
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].node == "trade_agent"
    assert result[0].arg["subtask"]["instruction"] == "查询订单 O1"


async def test_route_trade_without_tasks_falls_back_to_main_agent():
    state: MainState = {"decision": {"route": "trade", "reasoning": "x", "tasks": []}}
    assert await route_after_planner(state) == "main_agent"


async def test_route_search_without_tasks_falls_back_to_main_agent():
    assert await route_after_planner(_state("search")) == "main_agent"

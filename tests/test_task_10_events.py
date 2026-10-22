# -*- coding: utf-8 -*-
from datetime import datetime
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from app.application.agents.main_graph import build_main_graph, route_after_planner
from app.application.agents.subagents import PlannerDecision, SubTaskSpec
from app.application.tools.order_tools import build_order_tools
from app.application.tools.product_search import build_product_search_tool
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot


class _Broker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, session_id: str, event_type: str, payload: dict) -> None:
        self.events.append((session_id, event_type, payload))


def _context(session_id: str = "smoke-session"):
    return ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id=session_id,
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
        )
    )


async def test_product_search_publishes_invoke_and_success_result():
    class _Usecase:
        async def execute(self, spec):
            assert spec.normalized_query == "旅行三件套"
            return {"hits": [{"product_id": "P1001"}], "recall_strategy": "keyword_2gram"}

    broker = _Broker()
    tool = build_product_search_tool(_Usecase(), broker=broker)
    token = _context()
    try:
        await tool.ainvoke({"normalized_query": "旅行三件套", "top_k": 1})
    finally:
        ShoppingContext.reset(token)

    assert [event[1] for event in broker.events] == ["tool.invoke", "tool.result"]
    assert broker.events[0][0] == "smoke-session"
    assert broker.events[0][2]["tool"] == "product_search"
    assert broker.events[0][2]["args"]["normalized_query"] == "旅行三件套"
    assert broker.events[1][2] == {
        "tool": "product_search",
        "hit_count": 1,
        "recall_strategy": "keyword_2gram",
        "hits": [{"product_id": "P1001"}],
    }


async def test_product_search_publishes_error_result_on_value_error():
    class _Usecase:
        async def execute(self, spec):
            raise ValueError("bad query")

    broker = _Broker()
    tool = build_product_search_tool(_Usecase(), broker=broker)
    token = _context()
    try:
        result = await tool.ainvoke({"normalized_query": "旅行三件套"})
    finally:
        ShoppingContext.reset(token)

    assert result == {"error": "bad query"}
    assert broker.events[0][1] == "tool.invoke"
    assert broker.events[1] == (
        "smoke-session",
        "tool.result",
        {"tool": "product_search", "error": "bad query"},
    )


async def test_order_tools_publish_invoke_and_order_snapshot():
    class _Place:
        async def execute(self, buyer_id, items, shipping_address):
            return {"order_id": "GBX-000001", "status": "CONFIRMED"}

    class _Query:
        async def execute(self, order_id):
            return {"order_id": order_id, "status": "CONFIRMED"}

    class _Cancel:
        async def execute(self, order_id, reason):
            return {"order_id": order_id, "status": "CANCELLED", "cancel_reason": reason}

    broker = _Broker()
    tools = build_order_tools(_Place(), _Query(), _Cancel(), broker=broker)
    by_name = {tool.name: tool for tool in tools}
    token = _context()
    try:
        await by_name["create_order"].ainvoke(
            {
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                "shipping_address": {
                    "recipient_name": "张三",
                    "country": "CN",
                    "city": "杭州",
                    "address_line": "某路 1 号",
                },
            }
        )
        await by_name["query_order"].ainvoke({"order_id": "GBX-000001"})
        await by_name["cancel_order"].ainvoke({"order_id": "GBX-000001", "reason": "不想要了"})
    finally:
        ShoppingContext.reset(token)

    assert [event[1] for event in broker.events] == [
        "tool.invoke",
        "tool.result",
        "tool.invoke",
        "tool.result",
        "tool.invoke",
        "tool.result",
    ]
    assert broker.events[0][2]["tool"] == "create_order"
    assert broker.events[1][2] == {
        "tool": "create_order",
        "order": {"order_id": "GBX-000001", "status": "CONFIRMED"},
    }
    assert broker.events[2][2] == {"tool": "query_order", "args": {"order_id": "GBX-000001"}}
    assert broker.events[3][2] == {
        "tool": "query_order",
        "order": {"order_id": "GBX-000001", "status": "CONFIRMED"},
    }
    assert broker.events[5][2]["tool"] == "cancel_order"
    assert broker.events[5][2]["order"]["status"] == "CANCELLED"


async def test_route_after_planner_publishes_dispatch_for_every_send():
    broker = _Broker()
    token = _context()
    try:
        sends = await route_after_planner(
            {
                "decision": PlannerDecision(
                    route="search_parallel",
                    reasoning="two searches",
                    tasks=[
                        SubTaskSpec(agent="search_agent", instruction="找无塑料旅行三件套"),
                        SubTaskSpec(agent="search_agent", instruction="找可折叠水杯"),
                    ],
                ).model_dump()
            },
            broker,
        )
    finally:
        ShoppingContext.reset(token)

    assert len(sends) == 2
    assert [event[1] for event in broker.events] == ["agent.dispatch", "agent.dispatch"]
    first = broker.events[0][2]
    assert first["agent"] == "search_agent"
    assert first["instruction"] == "找无塑料旅行三件套"
    assert datetime.fromisoformat(first["started_at"]).tzinfo is not None


class _DirectPlanner:
    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        return PlannerDecision(route="direct", reasoning="direct", tasks=[])


class _DirectMainLlm:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return AIMessage(content="直接回答")


async def test_build_main_graph_accepts_broker_and_publishes_compression_event():
    broker = _Broker()
    graph = build_main_graph(
        SimpleNamespace(context_trigger_tokens=10, context_keep_recent=1),
        _DirectMainLlm(),
        _DirectPlanner(),
        [],
        None,
        broker=broker,
        compression_llm=_DirectMainLlm(),
    )
    token = _context()
    try:
        await graph.ainvoke(
            {
                "messages": [
                    AIMessage(content="很长的历史" * 50),
                    AIMessage(content="最近一条"),
                ]
            },
            config={"configurable": {"thread_id": "compress-thread"}},
        )
    finally:
        ShoppingContext.reset(token)

    compressed = [event for event in broker.events if event[1] == "context.compressed"]
    assert len(compressed) == 1
    payload = compressed[0][2]
    assert payload["summary_length"] == len("直接回答")
    assert payload["removed_messages"] == 1

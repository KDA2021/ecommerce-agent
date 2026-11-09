"""Scripted model decisions through the real shopping graphs and guarded tool."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.application.agents.main_graph import build_main_graph, invoke_agent
from app.application.agents.subagents import PlannerDecision, SubTaskSpec
from app.application.tools.product_search import build_product_search_tool
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.resilience import build_resilient_tool

SEARCH_ARGS = {
    "normalized_query": "可充电露营灯",
    "ship_to": "CN",
    "price_max_major": 100,
    "target_currency": "CNY",
    "top_k": 3,
}
HIT = {"product_id": "P1008", "title": "LumenGo 便携露营灯", "price_major": 89}


class RecordingBroker:
    def __init__(self):
        self.events = []

    def publish(self, session_id, kind, payload):
        self.events.append((kind, payload))

    def publish_current(self, kind, payload):
        self.publish(None, kind, payload)


class ScriptedSearch:
    def __init__(self, attempts, reply):
        self.attempts = iter(attempts)
        self.reply = reply
        self.inputs = []

    def bind_tools(self, tools):
        assert "product_search" in {tool.name for tool in tools}
        return self

    async def ainvoke(self, messages):
        self.inputs.append(list(messages))
        args = next(self.attempts, None)
        if args is None:
            return AIMessage(content=self.reply)
        return AIMessage(content="", tool_calls=[
            {"name": "product_search", "args": args, "id": f"search-{len(self.inputs)}"}
        ])


class ScriptedPlanner:
    def __init__(self, query, has_search):
        self.query = query
        self.has_search = has_search

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        tasks = [SubTaskSpec(agent="search_agent", instruction=self.query)] if self.has_search else []
        return PlannerDecision(route="search" if tasks else "direct", reasoning="fixture", tasks=tasks)


@pytest.mark.parametrize("route", ["direct", "llm"])
@pytest.mark.parametrize("scenario", [
    "clarify", "match", "empty", "missing_query", "invalid_top_k", "timeout", "retry",
])
async def test_shopping_tool_flow(route, scenario):
    broker = RecordingBroker()
    specs = []
    cancelled = []

    async def execute(spec):
        specs.append(spec)
        if scenario in ("timeout", "retry") and len(specs) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(spec)
        return {"hits": [] if scenario == "empty" else [HIT]}

    tool = build_resilient_tool(
        build_product_search_tool(SimpleNamespace(execute=execute), broker),
        SimpleNamespace(tool_timeout_seconds=0.25 if scenario in ("timeout", "retry") else 5),
        broker,
    )
    query = "推荐寄中国、标价100元以内的可充电露营灯。"
    attempts = [SEARCH_ARGS]
    reply = "LumenGo 便携露营灯，标价 89 元。"
    if scenario == "clarify":
        query, attempts, reply = "给我推荐一款。", [], "你想买哪一类商品？"
    elif scenario == "empty":
        reply = "没有找到符合条件的商品。"
    elif scenario == "missing_query":
        attempts = [{k: v for k, v in SEARCH_ARGS.items() if k != "normalized_query"}]
        reply = "缺少检索词，请补充商品类型。"
    elif scenario == "invalid_top_k":
        attempts = [{**SEARCH_ARGS, "top_k": 0}]
        reply = "检索参数无效，请重试。"
    elif scenario == "timeout":
        reply = "搜索超时，暂时无法确认匹配商品。"
    elif scenario == "retry":
        attempts = [SEARCH_ARGS, SEARCH_ARGS]

    search_model = ScriptedSearch(attempts, reply)
    delegated = route == "llm" and bool(attempts)
    main_model = ScriptedSearch([], reply) if delegated else search_model
    graph = build_main_graph(
        SimpleNamespace(model_route_mode=route, context_trigger_tokens=10_000, context_keep_recent=8),
        main_model, ScriptedPlanner(query, bool(attempts)),
        search_llm=search_model, all_tools=[tool], search_tools=[tool],
        checkpointer=InMemorySaver(),
    )
    assert await invoke_agent(graph, "shopping-tool-flow", query) == reply
    if delegated:
        assert any(reply in str(message.content) for message in main_model.inputs[-1])

    results = [m for m in search_model.inputs[-1] if isinstance(m, ToolMessage)]
    expected_spec = ProductSearchSpec(**SEARCH_ARGS)
    expected_calls = 0 if scenario in ("clarify", "missing_query", "invalid_top_k") else len(attempts)
    assert specs == [expected_spec] * expected_calls
    assert len(results) == len(attempts)
    kinds = [kind for kind, _ in broker.events]
    if scenario in ("clarify", "missing_query"):
        assert not kinds
    if scenario == "missing_query":
        assert results[0].status == "error"
        assert "normalized_query" in results[0].content
    if scenario == "invalid_top_k":
        assert "top_k" in json.loads(results[0].content)["error"]
        assert kinds == ["tool.invoke", "tool.result"]
    if scenario in ("timeout", "retry"):
        assert cancelled == [expected_spec]
        assert "超时" in json.loads(results[0].content)["error"]
        assert kinds[:2] == ["tool.invoke", "tool.timeout"]
    if scenario == "timeout":
        assert kinds == ["tool.invoke", "tool.timeout"]
    if scenario in ("match", "retry", "empty"):
        assert json.loads(results[-1].content)["hits"] == ([] if scenario == "empty" else [HIT])
        assert kinds[-2:] == ["tool.invoke", "tool.result"]

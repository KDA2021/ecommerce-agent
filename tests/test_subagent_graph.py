# -*- coding: utf-8 -*-
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.application.agents.main_graph import build_main_graph, invoke_agent
from app.application.agents.subagents import (
    PlannerDecision,
    SubTaskSpec,
    build_search_agent_graph,
)


@tool
def product_search(query: str) -> str:
    """Search products by a natural-language query."""
    return f"商品检索结果：{query}"


class _FakeMainLlm:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        return AIMessage(content=self._reply)


class _FakePlanner:
    def __init__(self, decision: PlannerDecision) -> None:
        self._decision = decision

    def with_structured_output(self, schema):
        return self

    async def ainvoke(self, messages):
        return self._decision


class _FakeSearchLlm:
    def __init__(self) -> None:
        self.calls: list[list] = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        snapshot = list(messages)
        self.calls.append(snapshot)
        instruction = ""
        for message in snapshot:
            if isinstance(message, HumanMessage):
                instruction = str(message.content)
        return AIMessage(content=f"检索完成：{instruction}")


def _settings():
    return SimpleNamespace(context_trigger_tokens=10_000, context_keep_recent=8)


async def test_direct_route_uses_public_five_argument_interface_and_returns_reply():
    planner = _FakePlanner(PlannerDecision(route="direct", reasoning="简单咨询", tasks=[]))
    main_llm = _FakeMainLlm("直接回答")
    graph = build_main_graph(_settings(), main_llm, planner, [product_search], InMemorySaver())

    reply = await invoke_agent(graph, "direct-thread", "你好")

    assert reply == "直接回答"
    assert len(main_llm.calls) == 1


async def test_search_parallel_fans_two_subagent_results_back_into_main_agent():
    tasks = [
        SubTaskSpec(agent="search_agent", instruction="查找无塑料旅行三件套"),
        SubTaskSpec(agent="search_agent", instruction="查找可折叠水杯"),
    ]
    planner = _FakePlanner(
        PlannerDecision(route="search_parallel", reasoning="两个独立检索", tasks=tasks)
    )
    main_llm = _FakeMainLlm("已综合两个检索结果")
    search_llm = _FakeSearchLlm()
    graph = build_main_graph(
        _settings(),
        main_llm,
        planner,
        search_llm,
        search_llm,
        [product_search],
        [product_search],
        [product_search],
        InMemorySaver(),
    )

    reply = await invoke_agent(graph, "parallel-thread", "PARENT_HISTORY_MARKER 帮我比较两个商品")

    assert reply == "已综合两个检索结果"

    assert len(search_llm.calls) == 2
    seen_instructions = []
    for call in search_llm.calls:
        human_contents = [str(m.content) for m in call if isinstance(m, HumanMessage)]
        assert len(human_contents) == 1
        assert "PARENT_HISTORY_MARKER" not in human_contents[0]
        seen_instructions.append(human_contents[0])
    assert sorted(seen_instructions) == sorted(task.instruction for task in tasks)

    assert len(main_llm.calls) == 1
    main_context = "\n".join(str(m.content) for m in main_llm.calls[0])
    assert "检索完成：查找无塑料旅行三件套" in main_context
    assert "检索完成：查找可折叠水杯" in main_context

    snapshot = await graph.aget_state({"configurable": {"thread_id": "parallel-thread"}})
    final_contents = [str(m.content) for m in snapshot.values["messages"]]
    assert "检索完成：查找无塑料旅行三件套" in final_contents
    assert "检索完成：查找可折叠水杯" in final_contents


async def test_prepare_isolates_parent_history_and_keeps_summary():
    search_llm = _FakeSearchLlm()
    graph = build_search_agent_graph(search_llm, [product_search])

    result = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="父历史不应出现", id="parent-message"),
                HumanMessage(content="无 id 的父历史也不应出现"),
            ],
            "subtask": {"agent": "search_agent", "instruction": "只查找无塑料旅行三件套"},
            "summary": "买家偏好无塑料",
            "turn_compressed": True,
        }
    )

    assert len(search_llm.calls) == 1
    prompted_messages = search_llm.calls[0]
    assert all("父历史不应出现" not in str(m.content) for m in prompted_messages)
    assert all("无 id 的父历史也不应出现" not in str(m.content) for m in prompted_messages)
    human_contents = [str(m.content) for m in prompted_messages if isinstance(m, HumanMessage)]
    assert human_contents == ["只查找无塑料旅行三件套"]
    system_contents = [str(m.content) for m in prompted_messages if isinstance(m, SystemMessage)]
    assert any("买家偏好无塑料" in content for content in system_contents)

    final_contents = [str(m.content) for m in result["messages"]]
    assert "父历史不应出现" not in final_contents
    assert "无 id 的父历史也不应出现" not in final_contents
    assert "只查找无塑料旅行三件套" in final_contents

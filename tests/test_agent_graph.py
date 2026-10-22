# -*- coding: utf-8 -*-
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.application.agents.graph import build_agent_graph, invoke_agent


class _FakeChatModel:
    def __init__(self, reply: str = "你好") -> None:
        self._reply = reply

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        return AIMessage(content=self._reply)


async def test_agent_graph_returns_final_text():
    graph = build_agent_graph(_FakeChatModel(), [], InMemorySaver())
    reply = await invoke_agent(graph, "s1", "你好")
    assert reply == "你好"


def test_agent_graph_has_agent_and_tools_nodes():
    graph = build_agent_graph(_FakeChatModel(), [], InMemorySaver())
    assert "agent" in graph.get_graph().nodes
    assert "tools" in graph.get_graph().nodes

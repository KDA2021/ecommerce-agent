# -*- coding: utf-8 -*-
"""P0 单 Agent 图：agent 节点 + ToolNode（保留给回归测试与对比）。

P0 单 Agent 图不再是生产装配（生产走 ``graph.py`` 的 planner + 子图），但
``from app.application.agents.graph import build_agent_graph, invoke_agent`` 的旧
导入路径由 ``graph.py`` re-export 本模块保持兼容。
"""
from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from typing_extensions import TypedDict

from app.application.agents.shopping_answer_policy import SHOPPING_ANSWER_POLICY


class AgentState(TypedDict):
    """单 Agent 图 state：仅一个带 ``add_messages`` reducer 的 messages 通道。"""

    messages: Annotated[list[AnyMessage], add_messages]


_SYSTEM_PROMPT = """你是 Globex 跨境电商导购 Agent，负责商品检索与订单管理。

规则：
1. 检索商品必须调用 product_search；价格上限、ship_to 等硬约束作为工具参数传入，不要自己过滤。
2. 只有买家明确确认后才调用 create_order；买家身份由系统注入，不要编造 buyer_id。
3. 涉及金额的数字必须来自工具返回，不得自行估算。
4. 工具返回 error 时如实告知买家，不要编造结果。
5. 回答使用中文，简洁、具体。""" + SHOPPING_ANSWER_POLICY


def build_agent_graph(llm: Any, tools: list[Any], checkpointer: Any):
    """编译 ``START → agent → (tools → agent)* → END`` 的单 Agent 图。

    Args:
        llm: ChatOpenAI 实例。
        tools: 绑定给 LLM 的工具列表。
        checkpointer: 会话持久化实现（可为 None）。

    Returns:
        编译后的 LangGraph 图。
    """
    llm_with_tools = llm.bind_tools(tools)

    async def agent_node(state: AgentState) -> dict:
        """调用绑定工具的 LLM，把回复追加回 messages。

        Args:
            state: 当前图 state。

        Returns:
            ``{'messages': [AIMessage]}`` 局部更新。
        """
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *state["messages"]]
        response = await llm_with_tools.ainvoke(messages)
        return {"messages": [response]}

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


async def invoke_agent(agent: Any, session_id: str, query: str) -> str:
    """以 ``thread_id=session_id`` 执行一次，返回最后一条非工具调用的文本回复。

    Args:
        agent: 编译后的图。
        session_id: 会话 / 线程 id。
        query: 用户输入。

    Returns:
        最终回复文本。
    """
    config = {"configurable": {"thread_id": session_id}}
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=query)]},
        config=config,
    )
    for message in reversed(result["messages"]):
        content = getattr(message, "content", "") or ""
        if content and not getattr(message, "tool_calls", None):
            return str(content)
    return ""

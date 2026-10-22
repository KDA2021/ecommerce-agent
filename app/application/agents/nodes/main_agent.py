# -*- coding: utf-8 -*-
"""main_agent 节点：拼接 system / 摘要 / 偏好 hint / 历史消息后调用主 LLM。"""
from __future__ import annotations

from contextlib import nullcontext

from langchain_core.messages import SystemMessage

from app.application.agents.state import MainState
from app.infrastructure.llm import bind_tools
from app.infrastructure.tracing import traced_span


def make_main_agent_node(main_llm, main_tools=None, throttle=None, tracer=None):
    """构造主 Agent 节点；LLM / 工具 / 闸门 / tracer 全部显式传入。

    Args:
        main_llm: 主 Agent LLM。
        main_tools: 主 Agent 绑定工具列表。
        throttle: 可选 LLM 并发闸门。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        ``async def main_agent_node(state) -> dict``。
    """
    main_llm_with_tools = bind_tools(main_llm, main_tools or [])

    async def main_agent_node(state: MainState) -> dict:
        """拼接 system / 摘要 / 偏好 hint / 历史消息后调用主 LLM。

        Args:
            state: 父图 state。

        Returns:
            ``{'messages': [AIMessage]}`` 局部更新。
        """
        messages = [
            SystemMessage(
                content=(
                    "你是 Globex 导购 Agent。检索必须用 product_search，查单用 query_order；"
                    "没有下单 / 取消工具，需要下单或取消时如实说明由交易链路处理。"
                )
            ),
        ]
        if state.get("summary"):
            messages.append(SystemMessage(content=f"此前对话摘要：{state['summary']}"))
        if state.get("preference_hint"):
            messages.append(SystemMessage(content=state["preference_hint"]))
        messages.extend(state["messages"])
        slot = throttle.slot() if throttle is not None else nullcontext()
        async with traced_span(tracer, "main_agent", {"node": "main_agent"}) as span:
            async with slot:
                response = await main_llm_with_tools.ainvoke(messages)
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["message_count"] = len(messages)
        return {"messages": [response]}

    return main_agent_node

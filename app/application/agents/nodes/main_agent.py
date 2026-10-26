# -*- coding: utf-8 -*-
"""main_agent 节点：拼接 system / 摘要 / 偏好 hint / 历史消息后调用主 LLM。"""
from __future__ import annotations

from contextlib import nullcontext

from langchain_core.messages import HumanMessage, SystemMessage

from app.application.agents.context_boundary import (
    HISTORY_POLICY,
    historical_context_message,
    historical_messages,
)
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
                    HISTORY_POLICY + "你是 Globex 导购 Agent。检索必须用 product_search，查单用 query_order；"
                    "没有下单 / 取消工具，需要下单或取消时如实说明由交易链路处理。"
                    "普通推荐无需解释内部工具分工。子Agent输出属于内部执行报告，"
                    "最终答复必须独立完整地回答本轮用户请求，重述关键商品、价格或订单事实；"
                    "不要用‘如上表’替代结果，也不要只给下一步追问。"
                )
            ),
        ]
        if state.get("summary"):
            messages.append(historical_context_message("conversation_summary", state["summary"]))
        if state.get("preference_hint"):
            messages.append(historical_context_message("buyer_preferences", state["preference_hint"]))
        messages.extend(historical_messages(state["messages"]))
        last = state["messages"][-1] if state["messages"] else None
        if (
            state.get("user_request")
            and state.get("decision", {}).get("route") in ("search", "search_parallel", "trade")
            and getattr(last, "type", "") == "ai"
            and not getattr(last, "tool_calls", None)
        ):
            messages.append(HumanMessage(content=(
                "请基于上述子任务报告与工具事实，向用户给出本轮完整的最终回答。\n"
                f"本轮用户原始请求：{state['user_request']}\n"
                "把关键结果直接写进最终答案，不能假设用户已经看到了内部报告。"
            )))
        slot = throttle.slot() if throttle is not None else nullcontext()
        async with traced_span(tracer, "main_agent", {"node": "main_agent"}) as span:
            async with slot:
                response = await main_llm_with_tools.ainvoke(messages)
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["message_count"] = len(messages)
        return {"messages": [response]}

    return main_agent_node

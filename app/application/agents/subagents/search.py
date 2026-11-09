# -*- coding: utf-8 -*-
"""search_agent 子图：只做商品检索的 ``prepare → agent → (tools → agent)*``。"""
from __future__ import annotations

from app.application.agents.shopping_answer_policy import SHOPPING_ANSWER_POLICY
from app.application.agents.subagents.base import _subagent_graph


def build_search_agent_graph(llm, tools, inject_preference: bool = True, throttle=None, tracer=None):
    """构建只做商品检索的 search_agent 子图。

    Args:
        llm: 子 Agent LLM。
        tools: 检索类工具。
        inject_preference: 是否注入买家偏好。
        throttle: 可选并发闸门。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        编译后的 search_agent 图。
    """
    return _subagent_graph(
        "你是跨境商品检索专家。只基于 product_search 结果回答，不编造商品；"
        "商品卡 reasons 是检索链路算好的事实理由（预算命中 / 可发国家 / 偏好命中），"
        "只可如实转述，不得新增、合并或改写理由。" + SHOPPING_ANSWER_POLICY,
        llm, tools, "search_agent",
        inject_preference=inject_preference,
        throttle=throttle,
        tracer=tracer,
    )

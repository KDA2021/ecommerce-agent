# -*- coding: utf-8 -*-
"""trade_agent 子图：只调用订单工具的交易子图（默认不注入偏好）。"""
from __future__ import annotations

from app.application.agents.state import TradeSubState
from app.application.agents.subagents.base import _subagent_graph


def build_trade_agent_graph(llm, tools, inject_preference: bool = False, throttle=None, tracer=None):
    """构建交易类 trade_agent 子图（默认不注入偏好）。

    Args:
        llm: 子 Agent LLM。
        tools: 订单工具。
        inject_preference: 是否注入买家偏好。
        throttle: 可选并发闸门。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        编译后的 trade_agent 图。
    """
    return _subagent_graph(
        "你是下单交易专家。只调用订单工具，金额必须来自工具结果。",
        llm, tools, "trade_agent",
        inject_preference=inject_preference,
        state_schema=TradeSubState,
        throttle=throttle,
        tracer=tracer,
    )

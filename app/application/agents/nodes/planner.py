# -*- coding: utf-8 -*-
"""planner 节点：调用 planner LLM 产出结构化路由决策。"""
from __future__ import annotations

import logging
from contextlib import nullcontext

from langchain_core.messages import HumanMessage, SystemMessage

from app.application.agents.context_boundary import HISTORY_POLICY, historical_context_message
from app.application.agents.state import MainState
from app.application.agents.subagents.base import PlannerDecision
from app.application.agents.task_context import task_context
from app.infrastructure.llm import with_structured_output
from app.infrastructure.tracing import traced_span

logger = logging.getLogger(__name__)

# planner 的系统提示：只输出 JSON，路由到 direct/search/trade 三类执行路径。
_PLANNER_PROMPT = """你是路由规划器。判断用户意图：
- 简单咨询、闲聊、无需调用工具：direct
- 需要一次商品检索：search
- 需要多个互相独立的商品检索：search_parallel
- 下单/查单/取消：trade
子任务 instruction 必须自包含。
只输出 JSON，字段必须严格为：
{"route": "direct|search|search_parallel|trade", "reasoning": "简短理由", "tasks": [{"agent": "search_agent|trade_agent", "instruction": "自包含指令"}]}
direct/无任务时 tasks 为 []。不要输出额外文字。"""


def make_planner_node(planner_llm, settings, throttle=None, tracer=None):
    """构造 planner 节点；planner LLM / settings / 闸门 / tracer 全部显式传入。

    planner 用 json_mode 保证稳定产出 PlannerDecision。

    Args:
        planner_llm: planner LLM。
        settings: 运行时配置（读取 ``model_route_mode``）。
        throttle: 可选 LLM 并发闸门。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        ``async def planner_node(state) -> dict``。
    """
    # planner 用 json_mode 保证稳定产出 PlannerDecision；主 Agent 仍用 function calling。
    try:
        planner = with_structured_output(planner_llm, PlannerDecision, method="json_mode")
    except TypeError:
        # Test doubles / older wrappers may not accept the method keyword.
        planner = with_structured_output(planner_llm, PlannerDecision)

    async def planner_node(state: MainState) -> dict:
        """调用 planner LLM 产出结构化路由决策；失败时降级 direct。

        P10-5 A/B：``MODEL_ROUTE_MODE=direct`` 时跳过 planner LLM，直接回
        ``main_agent``，由主 Agent 自己用 function calling 决策工具。

        P12-1：``route=trade`` 时把完整交易任务列表写进 ``remaining_tasks``，
        由 ``route_after_planner`` 只派发第一个、``route_next_trade`` 逐个续派。

        Args:
            state: 父图 state。

        Returns:
            ``{'decision': dict, 'remaining_tasks': list}`` 局部更新。
        """
        route_mode = str(getattr(settings, "model_route_mode", "llm") or "llm").strip().lower()
        user_text = state["messages"][-1].content
        if route_mode in ("direct", "single", "off"):
            return {
                "decision": PlannerDecision(
                    route="direct",
                    reasoning=f"MODEL_ROUTE_MODE={route_mode}：跳过 planner LLM",
                    tasks=[],
                ).model_dump(),
                "remaining_tasks": [],
                "user_request": str(user_text),
            }
        slot = throttle.slot() if throttle is not None else nullcontext()
        try:
            async with traced_span(tracer, "planner", {"node": "planner"}) as span:
                async with slot:
                    decision = await planner.ainvoke([
                        SystemMessage(content=HISTORY_POLICY + _PLANNER_PROMPT),
                        historical_context_message("task_background", task_context(state)),
                        HumanMessage(content=str(user_text)),
                    ])
                if span is not None and getattr(span, "attrs", None) is not None:
                    span.attrs["route"] = getattr(decision, "route", "")
        except Exception:
            decision = PlannerDecision(route="direct", reasoning="planner 失败降级", tasks=[])
        update = {"decision": decision.model_dump(), "remaining_tasks": [], "user_request": str(user_text)}
        if decision.route == "trade":
            update["remaining_tasks"] = [task.model_dump() for task in decision.tasks]
        return update

    return planner_node

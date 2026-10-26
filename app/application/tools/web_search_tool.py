# -*- coding: utf-8 -*-
"""web_search_tool：把 Tavily 搜索适配器包装成 LangChain StructuredTool。

工具契约：
- 入参 ``{query, max_results=3}``；
- 返回 ``{"results": [...]}`` 或 ``{"error": "..."}``；
- 通过 broker 发布 ``tool.invoke`` / ``tool.result`` 事件；
- ``client is None`` 时返回 ``None``，由 bootstrap 决定不注册。
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.application.data_policy import DataPolicy
from app.infrastructure.context import ShoppingContext


class WebSearchArgs(BaseModel):
    """web_search_tool 的结构化入参。"""

    query: str = Field(min_length=1, max_length=1000, description="联网搜索词，尽量自包含、具体")
    max_results: int = Field(default=3, ge=1, le=10, description="返回结果条数，默认 3")


def _publish(broker, event_type: str, payload: dict) -> None:
    """向 broker 发布事件（broker 为空则跳过）。

    Args:
        broker: 事件总线。
        event_type: 事件类型。
        payload: 事件载荷。
    """
    if broker is not None:
        broker.publish(ShoppingContext.current_session_id(), event_type, payload)


def build_web_search_tool(client: Any, broker=None, data_policy=None) -> StructuredTool | None:
    """构建 Tavily WebSearch 工具。

    Args:
        client: 具备 ``async search(query, max_results) -> list[dict]`` 的客户端。
        broker: 可选事件总线，用于发布 ``tool.invoke`` / ``tool.result``。
        data_policy: 可选敏感数据策略；命中的查询在调用客户端前拒绝。

    Returns:
        ``StructuredTool``；当 ``client is None`` 时返回 ``None``。
    """
    if client is None:
        return None
    policy = data_policy if data_policy is not None else DataPolicy()

    async def web_search_tool(query: str, max_results: int = 3) -> dict:
        """调用 Tavily 客户端并返回 ``{'results': [...]}``。

        Args:
            query: 搜索词。
            max_results: 返回结果条数。

        Returns:
            ``{'results': [...]}``，搜索失败时为 ``{'error': ...}``。
        """
        if policy.contains_sensitive(query):
            denial = {"error": "Search query contains sensitive data.", "code": "sensitive_query_blocked"}
            _publish(broker, "tool.result", {"tool": "web_search_tool", **denial})
            return denial
        _publish(
            broker,
            "tool.invoke",
            {"tool": "web_search_tool", "args": {"query": query, "max_results": max_results}},
        )
        try:
            results = await client.search(query, max_results=max_results)
        except Exception as err:  # noqa: BLE001 —— 搜索失败不阻断 Agent，转 error dict
            safe_error = policy.sanitize_output(str(err))
            _publish(
                broker,
                "tool.result",
                {"tool": "web_search_tool", "error": safe_error},
            )
            return {"error": safe_error}

        _publish(
            broker,
            "tool.result",
            {"tool": "web_search_tool", "results": results},
        )
        return {"results": results}

    return StructuredTool.from_function(
        name="web_search_tool",
        description=(
            "联网搜索实时公开信息（时效性政策、新闻、汇率/税则背景等）；"
            "站内商品与订单检索不要使用本工具。"
        ),
        args_schema=WebSearchArgs,
        coroutine=web_search_tool,
    )

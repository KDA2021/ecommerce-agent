# -*- coding: utf-8 -*-
"""SSE 事件路由：``GET /commerce/events``。

订阅 broker 上 ``shopping_session_id`` 对应的事件队列，把 token.delta /
final.result / error / task.started 等事件实时推给前端。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.presentation.dependencies import COMMERCE_AUTH_AND_RATE_LIMIT, get_container


def build_events_router() -> APIRouter:
    """构造 SSE 事件路由。

    Returns:
        APIRouter（``/commerce/events``）。
    """
    router = APIRouter()

    @router.get(
        "/commerce/events",
        dependencies=COMMERCE_AUTH_AND_RATE_LIMIT,
    )
    async def events(shopping_session_id: str, request: Request) -> StreamingResponse:
        """SSE 事件流：订阅 broker 上该 session 的队列。

        Args:
            shopping_session_id: 会话 id。
            request: FastAPI 请求（取容器）。

        Returns:
            StreamingResponse。
        """
        broker = get_container(request).broker
        return StreamingResponse(
            broker.subscribe(shopping_session_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router

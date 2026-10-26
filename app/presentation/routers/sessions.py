# -*- coding: utf-8 -*-
"""会话历史路由：``GET /commerce/sessions`` + ``/{session_id}/history``（P10-4）。

路由只做 HTTP 层；checkpointer 优先 / trace 兜底的历史读取与序列化实现留在
``app.presentation.sessions``（fail-safe：读不到就返回空 + ``source=empty``）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.presentation.dependencies import COMMERCE_AUTH, get_container
from app.presentation.ownership import authorize_session, owned_sessions
from app.presentation.sessions import _trace_dir, collect_thread_history, summarize_trace_sessions


def build_sessions_router() -> APIRouter:
    """构造会话历史路由。

    Returns:
        APIRouter（``/commerce/sessions``、``/commerce/sessions/{session_id}/history``）。
    """
    router = APIRouter()

    @router.get("/commerce/sessions", dependencies=COMMERCE_AUTH)
    async def list_sessions(request: Request) -> dict:
        """列出 trace 目录里的会话（只读、fail-safe）。

        Args:
            request: FastAPI 请求。

        Returns:
            ``{"sessions": [...], "count": int}``。
        """
        container = get_container(request)
        sessions = summarize_trace_sessions(_trace_dir(container))
        allowed = await owned_sessions(request)
        if allowed is not None:
            sessions = [item for item in sessions if item.get("session_id") in allowed]
        return {"sessions": sessions, "count": len(sessions)}

    @router.get("/commerce/sessions/{session_id}/history", dependencies=COMMERCE_AUTH)
    async def session_history(session_id: str, request: Request) -> dict:
        """读取某会话的后端 thread 历史。

        Args:
            session_id: 会话 / thread id。
            request: FastAPI 请求。

        Returns:
            ``{"session_id", "source", "messages"}``。
        """
        await authorize_session(request, session_id)
        container = get_container(request)
        source, messages = await collect_thread_history(container, session_id)
        return {"session_id": session_id, "source": source, "messages": messages}

    return router

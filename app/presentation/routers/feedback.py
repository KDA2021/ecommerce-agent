# -*- coding: utf-8 -*-
"""用户反馈路由：``POST /commerce/feedback`` + ``GET /commerce/feedback``。

P10-1 用户反馈闭环：点赞 / 点踩落 MySQL；点踩同时回流
``data/badcases.jsonl`` 供离线评测。反馈是旁路能力，写失败不影响主流程。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.domain.feedback.feedback import UserFeedback
from app.presentation.auth import principal
from app.presentation.dependencies import COMMERCE_AUTH, get_container
from app.presentation.ownership import authorize_session


class FeedbackRequest(BaseModel):
    """POST /commerce/feedback 请求体（点赞 / 点踩）。"""

    session_id: str            # 会话 id
    score: int                 # 1=点赞 / -1=点踩
    query: str = ""            # 本轮用户问题
    trace_id: str = ""         # 本轮 trace id（前端从 final.result 事件取）
    reply_preview: str = ""    # 被评价回复的预览文本
    reason: str = ""           # 原因 code（点踩必填，支持英文 code 或中文 label）
    buyer_id: str = ""         # 买家 id（可选）


def build_feedback_router() -> APIRouter:
    """构造反馈路由。

    Returns:
        APIRouter（提交 / 查询）。
    """
    router = APIRouter()

    @router.post("/commerce/feedback", dependencies=COMMERCE_AUTH)
    async def submit_feedback(body: FeedbackRequest, request: Request) -> dict:
        """提交点赞 / 点踩反馈。

        Args:
            body: 反馈请求体。
            request: FastAPI 请求（取容器）。

        Returns:
            ``{'id': ..., 'status': 'ok', 'reason_label': ...}``。

        Raises:
            HTTPException: 存储未就绪返回 503；参数非法返回 422。
        """
        await authorize_session(request, body.session_id)
        user = principal(request)
        if user is not None:
            body = body.model_copy(update={"buyer_id": user.buyer_id})
        container = get_container(request)
        store = getattr(container, "feedback_store", None) if container is not None else None
        if store is None:
            raise HTTPException(status_code=503, detail="反馈存储未就绪")
        try:
            feedback = UserFeedback(
                session_id=body.session_id,
                score=body.score,
                query=body.query,
                trace_id=body.trace_id,
                reply_preview=body.reply_preview,
                reason=body.reason,
                buyer_id=body.buyer_id,
            )
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

        feedback_id = await store.add(feedback)
        # 点踩样本回流离线评测：写失败不影响反馈本身（writer 内部已兜底）。
        if feedback.score == -1:
            badcase_store = getattr(container, "badcase_store", None)
            if badcase_store is not None:
                await badcase_store.append(feedback)
        return {"id": feedback_id, "status": "ok", "reason_label": feedback.reason_text}

    @router.get("/commerce/feedback", dependencies=COMMERCE_AUTH)
    async def list_feedback(
        session_id: str,
        request: Request,
        limit: int = 100,
    ) -> dict:
        """按会话查询反馈。

        Args:
            session_id: 会话 id（query 参数）。
            request: FastAPI 请求（取容器）。
            limit: 最多返回条数。

        Returns:
            ``{'session_id', 'count', 'items'}``。

        Raises:
            HTTPException: 存储未就绪返回 503。
        """
        await authorize_session(request, session_id)
        container = get_container(request)
        store = getattr(container, "feedback_store", None) if container is not None else None
        if store is None:
            raise HTTPException(status_code=503, detail="反馈存储未就绪")
        items = await store.list_by_session(session_id, limit=max(limit, 1))
        return {
            "session_id": session_id,
            "count": len(items),
            "items": [item.to_dict() for item in items],
        }

    return router

# -*- coding: utf-8 -*-
"""会话成本 / 用量聚合路由：``GET /commerce/metrics``（P10-5）。

按 ``session_id`` 过滤或聚合最近 ``limit`` 行 session_metrics，同时回传当前
A/B 实验配置，供 Dashboard 展示。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.application.metrics.session_metrics import aggregate_session_metrics
from app.presentation.dashboard import experiment_config
from app.presentation.dependencies import COMMERCE_AUTH, get_container


def build_metrics_router() -> APIRouter:
    """构造会话指标聚合路由。

    Returns:
        APIRouter（``/commerce/metrics``）。
    """
    router = APIRouter()

    @router.get("/commerce/metrics", dependencies=COMMERCE_AUTH)
    async def commerce_metrics(
        request: Request,
        session_id: str | None = None,
        limit: int = 200,
    ) -> dict:
        """聚合 session_metrics。

        Args:
            request: FastAPI 请求（取容器）。
            session_id: 可选会话 id；缺省聚合所有会话。
            limit: 最多扫描行数（按自增 id 正序取最近 limit 行）。

        Returns:
            聚合 dict（sessions / turns / llm_calls / total_tokens / cache_hits /
            cost_estimate）+ recent 明细 + experiment 配置。

        Raises:
            HTTPException: 指标存储未就绪返回 503。
        """
        container = get_container(request)
        store = getattr(container, "metrics_store", None) if container is not None else None
        if store is None:
            raise HTTPException(status_code=503, detail="指标存储未就绪")
        bounded_limit = max(int(limit), 1)
        if session_id:
            rows = await store.list_by_session(session_id, limit=bounded_limit)
        else:
            rows = await store.list_all(limit=bounded_limit)
        metrics = aggregate_session_metrics(rows)
        return {
            **metrics,
            "session_id": session_id,
            "recent": [row.to_dict() for row in rows[-20:]],
            "experiment": experiment_config(container),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    return router

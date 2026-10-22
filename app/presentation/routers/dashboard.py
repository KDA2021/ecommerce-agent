# -*- coding: utf-8 -*-
"""产品指标 Dashboard 路由：``GET /commerce/dashboard``（P10-1）。

路由只做 HTTP 层；指标口径与聚合实现留在 ``app.presentation.dashboard``
（纯函数，便于离线测试与复用）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.presentation.dashboard import collect_dashboard
from app.presentation.dependencies import COMMERCE_AUTH, get_container


def build_dashboard_router() -> APIRouter:
    """构造 Dashboard 路由。

    Returns:
        APIRouter（``/commerce/dashboard``）。
    """
    router = APIRouter()

    @router.get("/commerce/dashboard", dependencies=COMMERCE_AUTH)
    async def dashboard(request: Request) -> dict:
        """返回产品指标聚合结果。

        Args:
            request: FastAPI 请求。

        Returns:
            指标 dict。
        """
        return await collect_dashboard(get_container(request))

    return router

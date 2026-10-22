# -*- coding: utf-8 -*-
"""Presentation 层 domain routers：按业务域拆分的 FastAPI 路由集合。

每个模块只负责一个域的路由定义（路径 / 方法 / 请求响应模型 / 依赖），
业务逻辑留在对应的 ``app.application`` 用例或 ``app.presentation`` 纯函数模块；
``app.presentation.server.create_app`` 只在这里取 router 并挂载。
"""
from __future__ import annotations

from app.presentation.routers.dashboard import build_dashboard_router
from app.presentation.routers.events import build_events_router
from app.presentation.routers.feedback import build_feedback_router
from app.presentation.routers.intents import build_intents_router
from app.presentation.routers.metrics import build_metrics_router
from app.presentation.routers.orders import build_orders_router
from app.presentation.routers.sessions import build_sessions_router

__all__ = [
    "build_dashboard_router",
    "build_events_router",
    "build_feedback_router",
    "build_intents_router",
    "build_metrics_router",
    "build_orders_router",
    "build_sessions_router",
]

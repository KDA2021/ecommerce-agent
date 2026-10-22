# -*- coding: utf-8 -*-
"""FastAPI 应用装配：lifespan + 按业务域挂载 routers + 请求关联 ID 中间件。

Presentation 层的路由实现已按业务域拆到 ``app/presentation/routers/``：
intents / orders / feedback / sessions / metrics / dashboard / events；共享依赖
（鉴权 / 限流组合、容器读取、request_id）在 ``app/presentation/dependencies.py``。

本模块只保留 ``create_app``、生命周期与 P15-3 日志中间件，不再定义任何业务路由。

P15-3 日志链路：

1. 导入本模块时调用一次 :func:`configure_logging`，把进程日志切成 JSON 结构化格式
   （``LOG_FORMAT=text`` 时退回可读文本，``LOG_LEVEL`` 控制级别）；
2. 每个请求经 :func:`_request_logging_middleware`：解析 / 生成 ``request_id`` →
   写进 ``request.state`` 与日志上下文 → 打 ``http.request`` start / end 两条日志，
   end 日志带 ``status`` / ``duration_ms``，异常日志带堆栈；
3. 之后业务代码里任何 ``logger.info(...)`` 都自动带上 ``request_id`` / ``trace_id``。
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.bootstrap import Container, build_container
from app.infrastructure.logging_config import (
    bind_request_context,
    configure_logging,
)
from app.infrastructure.settings import load_settings
from app.presentation.dependencies import REQUEST_ID_HEADER, effective_request_id
from app.presentation.routers import (
    build_dashboard_router,
    build_events_router,
    build_feedback_router,
    build_intents_router,
    build_metrics_router,
    build_orders_router,
    build_sessions_router,
)

# 兼容 re-export：旧代码 / 测试从 app.presentation.server 导入这些名称，继续可用。
from app.presentation.routers.feedback import FeedbackRequest  # noqa: F401
from app.presentation.routers.intents import (  # noqa: F401
    IntentRequest,
    IntentResponse,
    PreferenceHint,
    TaskStatusResponse,
    render_preference_directive,
)
from app.presentation.routers.orders import (  # noqa: F401
    CancelRequest,
    OrderConfirmRequest,
)

logger = logging.getLogger(__name__)

#: 进程级日志装配（模块导入即生效，幂等；uvicorn 启动时也会再次调用）。
configure_logging()


def _flush_handlers() -> None:
    """主动 flush 所有已装配的日志 handler（错误路径收紧落盘时机）。"""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - flush 失败不应影响请求
            pass


def _mount_router(app: FastAPI, router: APIRouter) -> None:
    """把 domain router 已构建好的路由挂到 app 上。

    FastAPI 0.141 起 ``include_router`` 只往 ``app.routes`` 里插入惰性的
    ``_IncludedRouter`` 占位（真正的 path 在请求匹配时才展开），这让"遍历
    ``app.routes`` 校验 /commerce/* 都挂了鉴权"的既有测试看不到任何业务路由。
    这里直接把 router 内已经构建完成的 ``APIRoute`` 对象接入 app 路由表：
    路由对象、依赖、response_model、path 正则全部原样复用，对外行为与拆分前
    逐条 ``@app.get/@app.post`` 注册完全一致。

    Args:
        app: FastAPI 应用。
        router: 某个业务域的 APIRouter。
    """
    app.router.routes.extend(router.routes)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：启动时按需装配容器，关闭时释放。

    Args:
        app: FastAPI 应用。
    """
    if app.state.container is None and app.state.build_on_startup:
        app.state.container = await build_container(load_settings())
    container = app.state.container
    if container is not None:
        # P11-4：背板监听随服务启动；direct 模式（无背板）是 no-op。
        broker = getattr(container, "broker", None)
        if broker is not None:
            await broker.start()
    yield
    if container is not None:
        await container.close()


async def _request_logging_middleware(request: Request, call_next):
    """为每个请求注入 ``request_id``，并打 start / end 两条结构化日志。

    - ``X-Request-ID`` 合法时透传（便于跨服务 / 网关串联），否则生成 ``uuid4().hex``；
    - ID 写进 ``request.state.request_id`` 与日志 ``ContextVar``，
      业务代码不必感知，之后所有日志自动带上；
    - 回程把 ``X-Request-ID`` 写回响应头，前端 / 排障可直接引用；
    - 未处理异常记录 ``exc`` 日志后原样抛出，交给 FastAPI 的异常处理链，
      保证对外行为与加中间件之前一致。

    Args:
        request: 当前请求。
        call_next: Starlette 的下游处理链。

    Returns:
        下游响应（已补 ``X-Request-ID`` 响应头）。
    """
    request_id = effective_request_id(request.headers, uuid.uuid4().hex)
    request.state.request_id = request_id
    started = time.perf_counter()
    with bind_request_context(request_id=request_id, path=request.url.path):
        logger.info(
            "http.request",
            extra={
                "event": "http.request.start",
                "method": request.method,
                "path": request.url.path,
            },
        )
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "http.request",
                extra={
                    "event": "http.request.error",
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            # 日志 handler 走 StreamHandler；正常路径靠行缓冲 / 采集器，异常路径
            # 直接刷一次，避免进程紧接着退出时最后一条错误日志还留在缓冲区。
            _flush_handlers()
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "http.request",
            extra={
                "event": "http.request.end",
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def create_app(container: Container | None = None, *, build_on_startup: bool = True) -> FastAPI:
    """构建 FastAPI 应用并挂载各业务域 routers。

    Args:
        container: 可选注入容器（测试用）。
        build_on_startup: 是否在 lifespan 中装配容器。

    Returns:
        FastAPI 应用。
    """
    app = FastAPI(title="ecommerce-agent", lifespan=_lifespan)
    app.state.container = container
    app.state.build_on_startup = build_on_startup
    # 中间件在最外层：request_id 覆盖包括 422 / 401 在内的所有响应。
    app.middleware("http")(_request_logging_middleware)

    @app.get("/health")
    async def health() -> dict:
        """健康检查（P11-3：/health 与 /docs 不鉴权，永远开放）。

        Returns:
            ``{'status': 'ok'}``。
        """
        return {"status": "ok"}

    # 挂载顺序与拆分前逐条注册的顺序一致，避免依赖路由匹配顺序的行为变化。
    # 鉴权 / 限流依赖由各 router 自己声明：业务入口鉴权，/health 不限流。
    _mount_router(app, build_events_router())
    _mount_router(app, build_intents_router())
    _mount_router(app, build_dashboard_router())
    _mount_router(app, build_sessions_router())
    _mount_router(app, build_orders_router())
    _mount_router(app, build_feedback_router())
    _mount_router(app, build_metrics_router())
    return app


app = create_app()

# -*- coding: utf-8 -*-
"""Presentation 层共享依赖：鉴权 / 限流组合与容器读取。

各 domain router 统一从这里取依赖，避免"哪个入口该鉴权、哪个该限流"散落在多个
路由文件里——新增业务路由时只须选用这里的常量，不会漏挂：

- ``COMMERCE_AUTH``：只鉴权。普通查询 / 状态写入入口；
- ``COMMERCE_AUTH_AND_RATE_LIMIT``：先鉴权再限流。对话（``/commerce/intents``）
  与 SSE（``/commerce/events``）这类高成本入口，未授权请求不消耗限流额度；

``/health`` 与 ``/docs`` 不在此列，永远开放 / 不限流。

P15-3 追加：请求关联 ID（``request_id``）的读取入口。中间件在
``app/presentation/server.py`` 里生成 / 透传 ID 并写进 ``request.state``，
路由需要时用 ``Depends(get_request_id)`` 拿到，或直接读 ``request.state.request_id``。
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import Depends, Request
from starlette.datastructures import Headers

from app.presentation.auth import require_api_key
from app.presentation.rate_limit import rate_limit

#: 请求关联 ID 的 HTTP 头：入口优先透传客户端 / 网关带来的值，回程原样带回。
REQUEST_ID_HEADER = "X-Request-ID"

#: 透传 request_id 的长度上限与字符集（只允许 URL / 日志安全的可见字符）。
MAX_REQUEST_ID_LENGTH = 128
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:\-]+")

#: 业务入口统一鉴权（``API_KEY`` 未配置时 ``require_api_key`` 内部 no-op）。
COMMERCE_AUTH = [Depends(require_api_key)]

#: 鉴权 + 滑动窗口限流；顺序固定为先鉴权后限流。
COMMERCE_AUTH_AND_RATE_LIMIT = [Depends(require_api_key), Depends(rate_limit)]


def effective_request_id(headers: Headers, generated: str) -> str:
    """选择本次请求真正使用的 request_id：优先透传合法请求头，否则用新生成的。

    合法性口径：去空白后非空、长度 ≤ :data:`MAX_REQUEST_ID_LENGTH`、且只含
    ``[A-Za-z0-9._:-]``。为什么要校验——request_id 会被写进日志行与响应头，
    放任意字符进来会让日志注入 / 响应头注入成为可能；不合法的头直接丢弃。

    Args:
        headers: 请求头（Starlette ``Headers`` 或同接口对象）。
        generated: 中间件生成的新 ID（通常是 ``uuid4().hex``）。

    Returns:
        最终使用的 request_id。
    """
    raw = ""
    try:
        raw = str(headers.get(REQUEST_ID_HEADER, "") or "").strip()
    except Exception:  # pragma: no cover - 头对象异常时退回生成值
        raw = ""
    if raw and len(raw) <= MAX_REQUEST_ID_LENGTH and _REQUEST_ID_PATTERN.fullmatch(raw):
        return raw
    return generated


def get_request_id(request: Request) -> str:
    """读取当前请求的关联 ID（中间件已写入 ``request.state``）。

    中间件总是先于路由执行，所以路由里通常拿到的就是 ``X-Request-ID``；
    直接调用本依赖而没有经过中间件时（单测直接 ``TestClient`` 调依赖）回退空串，
    不抛异常——日志字段允许缺失，不允许为了日志把请求打挂。

    Args:
        request: FastAPI 请求。

    Returns:
        request_id；无中间件上下文时为空串。
    """
    return str(getattr(request.state, "request_id", "") or "")


def get_container(request: Request) -> Any:
    """读取当前应用的装配容器。

    与旧代码里的 ``app.state.container`` 完全同源；容器未装配时返回 ``None``，
    由各路由保持既有的 fail 行为（503 / AttributeError 与重构前一致）。

    Args:
        request: FastAPI 请求。

    Returns:
        装配容器或测试替身 / ``None``。
    """
    return getattr(request.app.state, "container", None)

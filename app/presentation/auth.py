# -*- coding: utf-8 -*-
"""P11-3 API Key 鉴权：FastAPI 依赖。

设计约束（见 P11 设计说明第 4 节）：

- **可选开关**：``Settings.api_key``（来源 ``API_KEY``）为空 = 不鉴权，
  保持本地开发与既有测试的向后兼容；
- **两种携带方式**：``X-API-Key`` 请求头或 ``?api_key=`` 查询参数，任一正确即放行；
- **错误统一 401**：缺失或错误都返回 401，且用 ``secrets.compare_digest``
  做常量时间比较，避免时序侧信道；
- **只挂业务入口**：由 ``server.py`` 只挂到 ``/commerce/*``，
  ``/health`` 与 ``/docs`` 始终开放。

key 解析优先级：``request.app.state.container.settings.api_key``（生产路径，容器已
装配）→ 回退 ``load_settings()``（``create_app(container=...)`` 的测试替身没有
settings 时，仍能通过环境变量开启鉴权）。
"""
from __future__ import annotations

import secrets
from typing import Any, Callable

from fastapi import HTTPException, Request

from app.infrastructure.settings import load_settings

#: API Key 请求头名称。
API_KEY_HEADER = "X-API-Key"

#: 允许通过查询参数携带的 API Key 名称（供 SSE / 浏览器直连场景使用）。
API_KEY_QUERY_PARAM = "api_key"

#: 鉴权失败时回给客户端的质询头，提示应使用的鉴权方式。
WWW_AUTHENTICATE_VALUE = API_KEY_HEADER


def _normalize(value: Any) -> str:
    """归一化 API Key：None / 空白都按空串处理，并去掉首尾空白。

    Args:
        value: 原始配置值或请求携带值。

    Returns:
        归一化后的字符串。
    """
    if value is None:
        return ""
    return str(value).strip()


def resolve_api_key(container: Any) -> str:
    """解析当前生效的 API Key。

    生产路径优先读取容器里已装配的 ``Settings``；测试替身没有 settings（或没有
    ``api_key`` 字段）时回退到环境变量 / .env，保证配置语义一致。

    Args:
        container: 装配容器、测试替身或 None。

    Returns:
        配置的 API Key；未配置时为空串（表示不鉴权）。
    """
    settings = getattr(container, "settings", None)
    if settings is not None:
        configured = getattr(settings, "api_key", None)
        if configured is not None:
            return _normalize(configured)
    return _normalize(getattr(load_settings(), "api_key", ""))


def _matches(provided: str, expected: str) -> bool:
    """常量时间比较两个 API Key（按 UTF-8 字节比较，兼容非 ASCII 字符）。

    Args:
        provided: 请求携带的 key。
        expected: 配置的 key。

    Returns:
        是否匹配。
    """
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _provided_keys(request: Request) -> list[str]:
    """收集请求携带的候选 key：请求头优先，其次查询参数。

    Args:
        request: FastAPI 请求。

    Returns:
        归一化后的候选 key 列表（可能含空串，调用方需过滤）。
    """
    return [
        _normalize(request.headers.get(API_KEY_HEADER)),
        _normalize(request.query_params.get(API_KEY_QUERY_PARAM)),
    ]


def _verify_request(request: Request, expected: str) -> bool:
    """校验请求是否携带正确的 API Key。

    Args:
        request: FastAPI 请求。
        expected: 期望的 API Key（非空）。

    Returns:
        请求头或查询参数任一匹配即 True。
    """
    return any(
        candidate and _matches(candidate, expected) for candidate in _provided_keys(request)
    )


def _unauthorized() -> HTTPException:
    """构造统一的 401 响应。

    Returns:
        ``HTTPException(401)``，带 ``WWW-Authenticate`` 质询头。
    """
    return HTTPException(
        status_code=401,
        detail="无效或缺失的 API Key",
        headers={"WWW-Authenticate": WWW_AUTHENTICATE_VALUE},
    )


async def require_api_key(request: Request) -> None:
    """FastAPI 依赖：校验 ``X-API-Key`` / ``?api_key=``。

    未配置 ``API_KEY`` 时直接放行；配置后请求头或查询参数任一正确即放行，
    否则抛 ``HTTPException(401)``。

    Args:
        request: FastAPI 请求。

    Raises:
        HTTPException: 缺失或错误的 API Key 返回 401。
    """
    expected = resolve_api_key(getattr(request.app.state, "container", None))
    if not expected:
        return  # 未配置 = 不鉴权，向后兼容本地开发。
    if not _verify_request(request, expected):
        raise _unauthorized()


def build_api_key_dependency(api_key: str) -> Callable[..., Any]:
    """把鉴权依赖绑定到固定 key（测试 / 嵌入式调用）。

    Args:
        api_key: 期望的 API Key；空串表示不鉴权。

    Returns:
        可直接给 ``Depends`` 使用的异步依赖函数。
    """
    expected = _normalize(api_key)

    async def dependency(request: Request) -> None:
        if not expected:
            return
        if not _verify_request(request, expected):
            raise _unauthorized()

    return dependency

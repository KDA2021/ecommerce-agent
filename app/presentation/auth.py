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

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException, Request

from app.infrastructure.settings import load_settings

#: API Key 请求头名称。
API_KEY_HEADER = "X-API-Key"

#: 允许通过查询参数携带的 API Key 名称（供 SSE / 浏览器直连场景使用）。
API_KEY_QUERY_PARAM = "api_key"

#: 鉴权失败时回给客户端的质询头，提示应使用的鉴权方式。
WWW_AUTHENTICATE_VALUE = API_KEY_HEADER

SESSION_COOKIE = "ecommerce_session"
SESSION_TTL_SECONDS = 3600


@dataclass(frozen=True)
class Principal:
    """Server-resolved identity; a buyer ID supplied in JSON is not a principal."""

    buyer_id: str


def resolve_buyer_keys(container: Any) -> dict[str, str]:
    settings = getattr(container, "settings", None)
    raw = getattr(settings, "buyer_api_keys_json", None)
    if raw is None:
        raw = getattr(load_settings(), "buyer_api_keys_json", "")
    if not raw or not str(raw).strip():
        return {}
    try:
        keys = json.loads(raw)
        if not isinstance(keys, dict) or not keys:
            raise ValueError
        if any(
            not isinstance(buyer, str) or not buyer.strip() or buyer != buyer.strip() or len(buyer) > 64
            or not isinstance(key, str) or len(key) < 16
            for buyer, key in keys.items()
        ) or len(set(keys.values())) != len(keys):
            raise ValueError
        return keys
    except (TypeError, ValueError):
        raise HTTPException(status_code=503, detail="独立用户访问凭证配置无效") from None


def _signing_key(container: Any) -> bytes:
    # Private server-side credentials supply entropy; key rotation revokes old cookies.
    material = json.dumps(resolve_buyer_keys(container), sort_keys=True)
    material += "\n" + resolve_api_key(container)
    return hashlib.sha256(("ecommerce-cookie-v1\n" + material).encode()).digest()


def issue_session(container: Any, buyer_id: str) -> str:
    payload = json.dumps({"buyer_id": buyer_id, "exp": int(time.time()) + SESSION_TTL_SECONDS})
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(_signing_key(container), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _cookie_buyer(request: Request, container: Any) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    try:
        encoded, signature = cookie.rsplit(".", 1)
        expected = hmac.new(_signing_key(container), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if payload["exp"] <= time.time():
            return None
        buyer = payload["buyer_id"]
        keys = resolve_buyer_keys(container)
        if (keys and buyer in keys) or (not keys and resolve_api_key(container) and buyer == "buyer-1"):
            return buyer
    except (ValueError, TypeError, KeyError, UnicodeError):
        pass
    return None


def buyer_for_key(container: Any, provided: str) -> str | None:
    keys = resolve_buyer_keys(container)
    for buyer, key in keys.items():
        if _matches(provided, key):
            return buyer
    if not keys:
        expected = resolve_api_key(container)
        if expected and _matches(provided, expected):
            return "buyer-1"
    return None


def authenticated_buyer(request: Request, container: Any) -> str | None:
    for key in _provided_keys(request):
        if key:
            buyer = buyer_for_key(container, key)
            if buyer:
                return buyer
    return _cookie_buyer(request, container)


def principal(request: Request) -> Principal | None:
    return getattr(request.state, "principal", None)


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
    container = getattr(request.app.state, "container", None)
    keys = resolve_buyer_keys(container)
    expected = resolve_api_key(container)
    request.state.principal = None
    if not keys and not expected:
        return  # 未配置 = 不鉴权，向后兼容本地开发。
    buyer = authenticated_buyer(request, container)
    if not buyer:
        raise _unauthorized()
    if keys:
        request.state.principal = Principal(buyer)


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

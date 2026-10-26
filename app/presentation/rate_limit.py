# -*- coding: utf-8 -*-
"""P9-3 滑动窗口限流：FastAPI 依赖。

设计约束（见 P9 设计说明第 4 节）：

- **ZSET 滑动窗口**：复用 ``RedisCache.zadd_and_count``——先按 score 清理窗口外成员，
  再加入本次请求，最后返回窗口内总数；窗口随时间连续滑动，而不是固定分钟桶；
- **软阈值 / 硬上限**：窗口内计数在 ``rate_limit_per_minute`` 与 ``rate_limit_burst``
  之间时放行并回 ``Retry-After`` 提示；超过 ``rate_limit_burst`` 直接 429；
- **fail-open**：``REDIS_URL`` 为空（disabled）或 Redis 任何异常时一律放行，
  限流绝不能成为主链路的单点；
- **只挂业务入口**：由 ``server.py`` 只挂到 ``/commerce/intents`` 与 ``/commerce/events``，
  ``/health`` 永不限流。

限流维度由 ``rate_limit_scope`` 决定：

- ``buyer``：优先取显式 ``buyer_id``（query / JSON body），其次 ``shopping_session_id``，
  最后回退客户端 IP；
- ``ip``：直接按客户端 IP。

key 形如 ``rate:v1:{scope}:{identity}``；identity 里的分隔符会被转义，
避免不同买家拼出同一个 key。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable
from uuid import uuid4

from fastapi import HTTPException, Request, Response

logger = logging.getLogger(__name__)

#: ZSET 限流 key 前缀（带版本号，方便后续结构升级平滑换命名空间）。
RATE_LIMIT_KEY_PREFIX = "rate:v1"

#: 滑动窗口长度：固定 60s（“每分钟”语义）。
RATE_LIMIT_WINDOW_SECONDS = 60.0

# Settings 缺失/非法时的安全默认值，与 app/infrastructure/settings.py 保持一致。
DEFAULT_PER_MINUTE = 30
DEFAULT_BURST = 60
DEFAULT_SCOPE = "buyer"

_ALLOWED_SCOPES = ("buyer", "ip")


def _positive_int(value: Any, default: int) -> int:
    """把配置值收敛成正整数；非法或 <=0 时回退默认值。

    Args:
        value: 原始配置值（可能来自 Settings，也可能是测试替身）。
        default: 回退默认值。

    Returns:
        正整数。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalized_scope(settings: Any) -> str:
    """读取并归一化限流维度；未知值按 ``buyer`` 处理。

    Args:
        settings: 运行时配置对象（可为 None 或测试替身）。

    Returns:
        ``"buyer"`` 或 ``"ip"``。
    """
    raw = getattr(settings, "rate_limit_scope", DEFAULT_SCOPE)
    scope = str(raw or DEFAULT_SCOPE).strip().lower()
    return scope if scope in _ALLOWED_SCOPES else DEFAULT_SCOPE


def client_ip(request: Request) -> str:
    """取客户端 IP；缺失时返回 ``anonymous``。

    Args:
        request: FastAPI 请求。

    Returns:
        IP 字符串。
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "anonymous"


def _escape(identity: str) -> str:
    """转义 key 分隔符，保证 ``(scope, identity)`` 到 key 是单射。

    Args:
        identity: 未转义的 identity。

    Returns:
        转义后的字符串。
    """
    return identity.replace("%", "%25").replace(":", "%3A")


def rate_limit_key(scope: str, identity: str) -> str:
    """拼出限流 ZSET key。

    Args:
        scope: 限流维度（buyer / ip）。
        identity: 维度取值。

    Returns:
        ``rate:v1:{scope}:{identity}``。
    """
    return f"{RATE_LIMIT_KEY_PREFIX}:{scope}:{_escape(str(identity))}"


async def _body_buyer_id(request: Request) -> str | None:
    """从 JSON body 里尽力读取 ``buyer_id``（GET / 空 body 返回 None）。

    依赖里先读 body 是安全的：Starlette 会缓存 ``request._body``，
    后续 endpoint 的 pydantic 解析仍能读到同一份内容。

    Args:
        request: FastAPI 请求。

    Returns:
        buyer_id 或 None。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 空 body / 非 JSON 都按“没有 buyer_id”处理
        return None
    if isinstance(payload, dict):
        value = payload.get("buyer_id")
        if isinstance(value, str) and value:
            return value
    return None


async def resolve_identity(request: Request, scope: str) -> str:
    """按 scope 解析本次请求的限流 identity。

    Args:
        request: FastAPI 请求。
        scope: ``buyer`` / ``ip``。

    Returns:
        identity 字符串；无法识别时回退客户端 IP。
    """
    if scope == "ip":
        return client_ip(request)

    user = getattr(request.state, "principal", None)
    if user is not None:
        return user.buyer_id

    # buyer 维度：显式 buyer_id > 会话 id > 客户端 IP。
    buyer_id = request.query_params.get("buyer_id")
    if not buyer_id:
        buyer_id = await _body_buyer_id(request)
    if buyer_id:
        return str(buyer_id)

    session_id = request.query_params.get("shopping_session_id")
    if session_id:
        return f"session:{session_id}"
    return client_ip(request)


async def _oldest_score(redis_cache: Any, key: str) -> float | None:
    """尽力读取 ZSET 中最早的 score，用于计算精确 Retry-After。

    只是提示值：底层客户端不可用或任何异常都返回 ``None``，
    由调用方回退到整窗长度，不影响限流判定本身。

    Args:
        redis_cache: RedisCache 实例。
        key: 限流 key。

    Returns:
        最早 score；取不到返回 None。
    """
    client = getattr(redis_cache, "client", None)
    if client is None:
        return None
    try:
        rows = await client.zrange(key, 0, 0, withscores=True)
    except Exception as exc:  # noqa: BLE001 - 只是提示值，任何异常都吞掉
        logger.debug("rate_limit 读取最早成员失败 key=%s err=%s", key, exc)
        return None
    if not rows:
        return None
    row = rows[0]
    try:
        return float(row[1])
    except (IndexError, TypeError, ValueError):
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None


async def retry_after_seconds(redis_cache: Any, key: str, now: float | None = None) -> int:
    """计算 Retry-After：需要等窗口内最早那次请求滑出窗口。

    Args:
        redis_cache: RedisCache 实例。
        key: 限流 key。
        now: 当前时间戳；缺省取 ``time.time()``。

    Returns:
        正整数秒数；取不到最早成员时回退整窗长度。
    """
    current = time.time() if now is None else now
    oldest = await _oldest_score(redis_cache, key)
    if oldest is None:
        return max(1, int(math.ceil(RATE_LIMIT_WINDOW_SECONDS)))
    remaining = RATE_LIMIT_WINDOW_SECONDS - (current - oldest)
    return max(1, int(math.ceil(remaining)))


async def enforce_rate_limit(container: Any, request: Request, response: Response) -> None:
    """执行滑动窗口限流；通过返回 None，超限抛 ``HTTPException(429)``。

    fail-open 规则：
    - 容器/Redis 不可用（``None`` / disabled / 没有 ``zadd_and_count``）→ 放行；
    - Redis 命令异常 → ``zadd_and_count`` 自身返回 0 → 放行。

    Args:
        container: 装配容器（可为 None 或测试替身）。
        request: FastAPI 请求。
        response: FastAPI 响应对象；软阈值提示会写进它的 header。

    Raises:
        HTTPException: 窗口内计数超过 ``rate_limit_burst`` 时返回 429。
    """
    redis_cache = getattr(container, "redis_cache", None)
    zadd_and_count = getattr(redis_cache, "zadd_and_count", None)
    if redis_cache is None or not callable(zadd_and_count):
        return  # Redis 能力关闭 → 放行

    settings = getattr(container, "settings", None)
    scope = _normalized_scope(settings)
    per_minute = _positive_int(
        getattr(settings, "rate_limit_per_minute", DEFAULT_PER_MINUTE), DEFAULT_PER_MINUTE,
    )
    burst = _positive_int(
        getattr(settings, "rate_limit_burst", DEFAULT_BURST), DEFAULT_BURST,
    )

    identity = await resolve_identity(request, scope)
    key = rate_limit_key(scope, identity)
    # 成员唯一即可；score 由 RedisCache 统一写当前时间，窗口按 score 滑动。
    member = uuid4().hex

    try:
        count = int(await zadd_and_count(key, member, window_seconds=RATE_LIMIT_WINDOW_SECONDS))
    except Exception as exc:  # noqa: BLE001 - 依赖层再做一次兜底，绝不拖垮主链路
        logger.warning("rate_limit fail-open key=%s err=%s", key, exc)
        return
    if count <= 0:
        return  # disabled（0）或异常降级（0）= 放行

    if count > burst:
        retry_after = await retry_after_seconds(redis_cache, key)
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(retry_after)},
        )

    if count > per_minute:
        # 超过软阈值但仍在突发上限内：放行，只提示客户端放慢。
        retry_after = await retry_after_seconds(redis_cache, key)
        response.headers["Retry-After"] = str(retry_after)


async def rate_limit(request: Request, response: Response) -> None:
    """动态依赖：从 ``request.app.state.container`` 取容器并限流。

    这样 ``create_app(container=None)`` 也能在 lifespan 装配完成后自动生效。

    Args:
        request: FastAPI 请求。
        response: FastAPI 响应。
    """
    container = getattr(request.app.state, "container", None)
    await enforce_rate_limit(container, request, response)


def build_rate_limit_dependency(container: Any) -> Callable[..., Any]:
    """把限流依赖绑定到指定容器（测试 / 嵌入式调用）。

    Args:
        container: 装配容器或测试替身。

    Returns:
        可直接给 ``Depends`` 使用的异步依赖函数。
    """

    async def dependency(request: Request, response: Response) -> None:
        await enforce_rate_limit(container, request, response)

    return dependency

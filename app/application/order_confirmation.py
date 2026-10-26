# -*- coding: utf-8 -*-
"""交易确认握手与一次性授权，支持内存和 Redis 两种存储。

下单与取消工具登记服务端预览，发布 ``order.preview`` 后等待买家操作。
已鉴权确认接口调用 ``resolve``，工具再通过 ``consume`` 原子校验动作、
买家、会话、参数摘要及有效期。仅 confirmed → consumed 的胜出者可继续执行。
内存使用 asyncio.Lock；Redis 使用 WATCH/MULTI，供 web 与 worker 共享。
工具退出时清理条目，Redis 还使用 TTL 回收进程异常留下的记录。
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class ConfirmationStatus(str, Enum):
    """一次下单预览确认的生命周期状态。"""

    PENDING = "pending"        # 已发布预览，等待买家操作
    CONFIRMED = "confirmed"    # 买家确认，可以真实下单
    CANCELLED = "cancelled"    # 买家取消，绝不能下单
    TIMEOUT = "timeout"        # 等待超时，按取消处理
    CONSUMED = "consumed"      # 已领取执行权，不能重复执行


#: ``wait`` 的合法返回集合，方便调用方 / 测试直接断言。
WAIT_OUTCOMES = frozenset(
    {
        ConfirmationStatus.CONFIRMED.value,
        ConfirmationStatus.CANCELLED.value,
        ConfirmationStatus.TIMEOUT.value,
    },
)

#: ``resolve`` 接受的用户动作。
_RESOLVE_ACTIONS = {"confirm", "cancel"}

_AUTHORIZATION_FIELDS = {"action", "buyer_id", "session_id", "arguments_sha256"}


def _authorization_matches(payload: Any, expected: Any) -> bool:
    """Malformed or expired grants fail closed, even after user confirmation."""
    if not isinstance(expected, dict) or set(expected) != _AUTHORIZATION_FIELDS:
        return False
    if any(not isinstance(value, str) or not value for value in expected.values()):
        return False
    if not isinstance(payload, dict):
        return False
    authorization = payload.get("_authorization")
    if not isinstance(authorization, dict):
        return False
    if any(authorization.get(field) != expected[field] for field in _AUTHORIZATION_FIELDS):
        return False
    expires_at = authorization.get("expires_at")
    return (
        type(expires_at) in (int, float)
        and math.isfinite(expires_at)
        and expires_at > time.time()
    )


@dataclass
class _PendingPreview:
    """单个 token 的等待条目。"""

    token: str
    status: ConfirmationStatus = ConfirmationStatus.PENDING
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    resolved_at: Optional[float] = None
    resolution_event: asyncio.Event = field(default_factory=asyncio.Event)


class OrderConfirmationManager:
    """进程内确认握手：token → (asyncio.Event, 状态, 预览载荷)。"""

    def __init__(self) -> None:
        """初始化 token 表与并发保护锁。"""
        self._entries: dict[str, _PendingPreview] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # 写入侧：工具发布预览
    # ------------------------------------------------------------------ #
    async def create_preview(
        self,
        payload: dict[str, Any],
        *,
        token: Optional[str] = None,
    ) -> str:
        """登记一条待确认预览，返回 token。

        Args:
            payload: 预览载荷（items / shipping_address 等，不含单价）。
            token: 可选显式 token（测试注入确定性 token）；缺省生成 uuid4 hex。

        Returns:
            新 token。
        """
        resolved_token = token or uuid4().hex
        async with self._lock:
            if resolved_token in self._entries:
                raise ValueError(f"confirmation token already exists: {resolved_token}")
            entry = _PendingPreview(
                token=resolved_token,
                payload=copy.deepcopy(payload) if isinstance(payload, dict) else payload,
                created_at=asyncio.get_event_loop().time(),
            )
            self._entries[resolved_token] = entry
        return resolved_token

    async def consume(self, token: str, expected: dict) -> bool:
        """Atomically claim one confirmed, unexpired action grant exactly once."""
        async with self._lock:
            entry = self._entries.get(token)
            if entry is None or entry.status is not ConfirmationStatus.CONFIRMED:
                return False
            if not _authorization_matches(entry.payload, expected):
                return False
            entry.status = ConfirmationStatus.CONSUMED
            return True

    # ------------------------------------------------------------------ #
    # 决议侧：REST /commerce/orders/confirm
    # ------------------------------------------------------------------ #
    async def resolve(self, token: str, action: str) -> str:
        """按用户动作确认 / 取消某个 token。

        幂等：已决议的 token 重复 resolve 返回当前状态，不报错、不覆盖。
        未知 token 返回 ``"unknown"``。

        Args:
            token: ``create_preview`` 返回的 token。
            action: ``"confirm"`` 或 ``"cancel"``。

        Returns:
            ``pending`` / ``confirmed`` / ``cancelled`` / ``unknown``。
        """
        normalized = (action or "").strip().lower()
        if normalized not in _RESOLVE_ACTIONS:
            raise ValueError(f"非法动作 action={action!r}，只支持 confirm / cancel")

        async with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return "unknown"
            if entry.status is not ConfirmationStatus.PENDING:
                # 已经决议（或已超时清理前的竞态）：重复点击不改写第一次决议。
                return entry.status.value
            entry.status = (
                ConfirmationStatus.CONFIRMED
                if normalized == "confirm"
                else ConfirmationStatus.CANCELLED
            )
            entry.resolved_at = asyncio.get_event_loop().time()
            entry.resolution_event.set()
            return entry.status.value

    # ------------------------------------------------------------------ #
    # 等待侧：create_order 真正挂起
    # ------------------------------------------------------------------ #
    async def wait(self, token: str, timeout: float) -> str:
        """等待 token 被确认 / 取消，或超时。

        Args:
            token: ``create_preview`` 返回的 token。
            timeout: 最长等待秒数（<=0 视为立即超时）。

        Returns:
            ``confirmed`` / ``cancelled`` / ``timeout``。
        """
        async with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return ConfirmationStatus.TIMEOUT.value
            if entry.status is ConfirmationStatus.CONSUMED:
                return ConfirmationStatus.TIMEOUT.value
            if entry.status is not ConfirmationStatus.PENDING:
                return entry.status.value
            event = entry.resolution_event

        try:
            if timeout and timeout > 0:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                # 立即超时：与 asyncio.wait_for(..., 0) 语义保持一致。
                await asyncio.sleep(0)
                raise asyncio.TimeoutError
        except asyncio.TimeoutError:
            # 超时后主动清理，避免无人再 resolve 的 token 长期占用内存。
            await self.clear(token)
            return ConfirmationStatus.TIMEOUT.value

        async with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return ConfirmationStatus.TIMEOUT.value
            outcome = entry.status.value
            if entry.status is ConfirmationStatus.CONSUMED:
                return ConfirmationStatus.TIMEOUT.value
            if entry.status is ConfirmationStatus.PENDING:
                # 事件被 set 但状态未落库（不应发生）：按超时兜底。
                outcome = ConfirmationStatus.TIMEOUT.value
        if outcome == ConfirmationStatus.TIMEOUT.value:
            await self.clear(token)
        return outcome

    # ------------------------------------------------------------------ #
    # 清理与观测
    # ------------------------------------------------------------------ #
    async def clear(self, token: str) -> bool:
        """移除 token 条目（决议完成 / 超时后调用，避免表无界增长）。

        Args:
            token: 目标 token。

        Returns:
            是否真的移除了条目。
        """
        async with self._lock:
            return self._entries.pop(token, None) is not None

    async def status(self, token: str) -> str:
        """读取 token 当前状态。

        Args:
            token: 目标 token。

        Returns:
            ``pending`` / ``confirmed`` / ``cancelled`` / ``unknown``。
        """
        async with self._lock:
            entry = self._entries.get(token)
            return entry.status.value if entry is not None else "unknown"

    async def preview_payload(self, token: str) -> Optional[dict[str, Any]]:
        """读取预览载荷副本（调试 / 测试用）。

        Args:
            token: 目标 token。

        Returns:
            载荷副本；未知 token 返回 None。
        """
        async with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return None
            return copy.deepcopy(entry.payload)

    async def pending_tokens(self) -> list[str]:
        """列出当前仍待确认的 token。

        Returns:
            pending token 列表。
        """
        async with self._lock:
            return [
                token
                for token, entry in self._entries.items()
                if entry.status is ConfirmationStatus.PENDING
            ]


class RedisOrderConfirmationManager:
    """Shared confirmation state for web/worker processes in queue deployments.

    The first decision wins through WATCH/MULTI. Expiry bounds abandoned previews;
    a Redis error propagates instead of authorizing an unconfirmed order.
    """

    def __init__(self, client, ttl_seconds: int = 600):
        self._client = client
        self._ttl = max(1, int(ttl_seconds))

    def _key(self, token: str) -> str:
        return "order-confirmation:v1:" + token

    async def create_preview(self, payload: dict, *, token: str | None = None) -> str:
        from redis.exceptions import WatchError

        token = token or uuid4().hex
        key = self._key(token)
        serialized = json.dumps(payload, ensure_ascii=False)
        for _ in range(8):
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    if await pipe.exists(key):
                        raise ValueError(f"confirmation token already exists: {token}")
                    pipe.multi()
                    pipe.hset(key, mapping={"status": "pending", "payload": serialized})
                    pipe.expire(key, self._ttl)
                    await pipe.execute()
                    return token
                except WatchError:
                    continue
        raise RuntimeError("确认状态正被并发更新，请重试")

    async def consume(self, token: str, expected: dict) -> bool:
        """Claim a shared grant under WATCH so only one process can execute it."""
        from redis.exceptions import WatchError

        key = self._key(token)
        for _ in range(8):
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    status, serialized = await pipe.hmget(key, "status", "payload")
                    if status not in ("confirmed", b"confirmed") or serialized is None:
                        return False
                    try:
                        payload = json.loads(serialized)
                    except (ValueError, TypeError, UnicodeError):
                        return False
                    if not _authorization_matches(payload, expected):
                        return False
                    pipe.multi()
                    pipe.hset(key, "status", "consumed")
                    await pipe.execute()
                    return True
                except WatchError:
                    continue
        return False

    async def resolve(self, token: str, action: str) -> str:
        if action not in _RESOLVE_ACTIONS:
            raise ValueError("只支持 confirm / cancel")
        return await self._decide(token, "confirmed" if action == "confirm" else "cancelled")

    async def _decide(self, token: str, decision: str) -> str:
        from redis.exceptions import WatchError

        key = self._key(token)
        for _ in range(8):
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key)
                    status = await pipe.hget(key, "status")
                    if status is None:
                        return "unknown"
                    if isinstance(status, bytes):
                        status = status.decode()
                    if status != "pending":
                        return status
                    pipe.multi()
                    pipe.hset(key, "status", decision)
                    await pipe.execute()
                    return decision
                except WatchError:
                    continue
        raise RuntimeError("确认状态正被并发更新，请重试")

    async def wait(self, token: str, timeout: float) -> str:
        deadline = asyncio.get_running_loop().time() + max(0, timeout)
        while asyncio.get_running_loop().time() < deadline:
            status = await self.status(token)
            if status in ("confirmed", "cancelled"):
                return status
            if status == "consumed":
                return "timeout"
            if status == "unknown":
                return "timeout"
            await asyncio.sleep(min(0.1, max(0, deadline - asyncio.get_running_loop().time())))
        decided = await self._decide(token, "timeout")
        return "timeout" if decided in ("unknown", "consumed") else decided

    async def clear(self, token: str) -> bool:
        return bool(await self._client.delete(self._key(token)))

    async def status(self, token: str) -> str:
        result = await self._client.hget(self._key(token), "status")
        return result.decode() if isinstance(result, bytes) else result or "unknown"

    async def preview_payload(self, token: str) -> dict | None:
        result = await self._client.hget(self._key(token), "payload")
        return json.loads(result) if result is not None else None

    async def pending_tokens(self) -> list[str]:
        tokens = []
        async for key in self._client.scan_iter(match="order-confirmation:v1:*"):
            key = key.decode() if isinstance(key, bytes) else key
            token = key.rsplit(":", 1)[-1]
            if await self.status(token) == "pending":
                tokens.append(token)
        return tokens

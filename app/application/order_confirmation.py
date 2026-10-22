# -*- coding: utf-8 -*-
"""下单确认管理器：真实阻塞 create_order，直到买家确认 / 取消 / 超时。

背景（Task 2 修复）：上一版实现只在 ``tool.invoke`` 之后、``place.execute`` 之前
展示预览，但 ``create_order`` 已经进入执行流程，用户的「确认」按钮并不能真正拦下
下单动作。本模块把确认做成**进程内握手**：

1. ``create_order`` 进入工具后先 ``create_preview(payload)`` 拿到 token，
   通过 SSE 推送 ``order.preview``（带 token），然后 ``await wait(token, timeout)``
   真正挂起；
2. 前端 ``POST /commerce/orders/confirm {token, action}`` → ``resolve(token, action)``
   唤醒挂起的协程；
3. 只有 ``confirmed`` 才继续调用 ``PlaceOrderUseCase``；``cancelled`` / ``timeout``
   直接返回取消结果，``place`` 完全不会被触碰。

实现约束：
- 纯进程内、无外部依赖（不加表 / 不加 Redis），与单进程部署匹配；
- ``asyncio.Event`` 保证同一 token 的多个等待者共享一次决议；
- 条目在决议 / 超时后清理，避免 token 表无界增长；
- 所有方法都用 ``asyncio.Lock`` 保护，兼容并发确认与超时清理的竞争。
"""
from __future__ import annotations

import asyncio
import copy
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
            entry = _PendingPreview(
                token=resolved_token,
                payload=copy.deepcopy(payload) if isinstance(payload, dict) else payload,
                created_at=asyncio.get_event_loop().time(),
            )
            self._entries[resolved_token] = entry
        return resolved_token

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

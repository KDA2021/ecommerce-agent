# -*- coding: utf-8 -*-
"""ShoppingContext

用 ContextVar 保存当前任务的会话快照（shopping_session_id / buyer_id / locale / currency），
跨层透明传递：工具与子 Agent 执行时随时读取，无需层层透传参数。
多用户并发任务依赖 asyncio Task 级隔离，不会串台。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ShoppingContextSnapshot:
    """会话快照：session / buyer / locale / currency / trace_id。"""

    shopping_session_id: str
    buyer_id: str
    locale: str
    currency: str
    trace_id: str = ""


_current_snapshot: ContextVar[Optional[ShoppingContextSnapshot]] = ContextVar(
    "globex_shopping_context",
    default=None,
)


class ShoppingContext:
    """基于 ContextVar 的会话快照读写入口。"""

    @staticmethod
    def set(snapshot: ShoppingContextSnapshot):
        """设置当前任务快照。

        Args:
            snapshot: 会话快照。

        Returns:
            用于 reset 的 token。
        """
        return _current_snapshot.set(snapshot)

    @staticmethod
    def reset(token) -> None:
        """恢复 ``set`` 之前的快照。

        Args:
            token: ``set`` 返回的 token。
        """
        _current_snapshot.reset(token)

    @staticmethod
    def current() -> Optional[ShoppingContextSnapshot]:
        """读取当前快照。

        Returns:
            快照；无上下文时返回 None。
        """
        return _current_snapshot.get()

    @staticmethod
    def current_session_id() -> str:
        """读取当前 session_id，缺上下文时回退 anonymous。

        Returns:
            session_id。
        """
        snapshot = _current_snapshot.get()
        return snapshot.shopping_session_id if snapshot else "anonymous"

    @staticmethod
    def current_trace_id() -> str:
        """当前请求的 trace_id；无上下文时返回空串（调用方不应写空 trace_id 帧）。"""
        snapshot = _current_snapshot.get()
        return snapshot.trace_id if snapshot else ""

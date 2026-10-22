# -*- coding: utf-8 -*-
"""LangGraph MySQL checkpointer（asyncmy，单连接常驻）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver


@dataclass
class CheckpointerHandle:
    """持有 AsyncMySaver 及其连接上下文，便于统一关闭。"""

    saver: AsyncMySaver
    _cm: Any

    async def close(self) -> None:
        """关闭底层连接上下文。"""
        await self._cm.__aexit__(None, None, None)


async def build_checkpointer(connection_string: str) -> CheckpointerHandle:
    """建立 MySQL checkpointer 并执行 ``setup()`` 建表。

    Args:
        connection_string: MySQL 连接串。

    Returns:
        CheckpointerHandle。
    """
    cm = AsyncMySaver.from_conn_string(connection_string)
    saver = await cm.__aenter__()
    try:
        await saver.setup()
    except BaseException:
        await cm.__aexit__(None, None, None)
        raise
    return CheckpointerHandle(saver=saver, _cm=cm)

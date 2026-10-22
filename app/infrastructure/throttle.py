# -*- coding: utf-8 -*-
"""LLM 网关并发闸门：限制同一进程内同时在途的 LLM 调用数。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.infrastructure.tracing import traced_span


class GatewayThrottle:
    """基于 ``asyncio.Semaphore`` 的并发闸门。

    用法::

        async with throttle.slot():
            response = await llm.ainvoke(messages)

    P8：``tracer`` 可选；传入时在等待信号量的阶段记录 ``queue`` span，
    不把真正占用槽位的时间算进队列等待。
    """

    def __init__(self, max_concurrency: int, tracer=None) -> None:
        """初始化信号量。

        Args:
            max_concurrency: 最大并发数。
            tracer: 可选 LocalTraceLogger / span tracer。
        """
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._tracer = tracer

    @asynccontextmanager
    async def slot(self):
        """异步上下文管理器：占用一个并发槽位。

        Returns:
            ``async with`` 上下文。
        """
        async with traced_span(
            self._tracer,
            "queue",
            {"max_concurrency": self._max_concurrency},
        ):
            await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()

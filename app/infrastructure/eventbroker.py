# -*- coding: utf-8 -*-
"""进程内事件总线：按 session 路由到 SSE 队列（可选 Redis Pub/Sub 背板）。

两种运行形态：

- **direct（默认）**：``backplane=None``，行为与 P11-3 完全一致——
  publish 只写本进程的订阅队列 + trace；
- **多 worker（``QUEUE_MODE=redis``）**：注入 backplane 后，``publish`` 除了写本地队列，
  还会把同一份 payload 发到 ``ecommerce:events:{session_id}``；
  每个实例的 ``start()`` 会起一个后台监听循环，把**其他实例**发布的事件收回、
  只写本进程的订阅队列（不回写背板，避免广播风暴）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from typing import AsyncIterator

from app.application.metrics.session_metrics import record_event as record_metric_event
from app.infrastructure.context import ShoppingContext


class EventBroker:
    """进程内事件总线：按 session 广播 SSE 帧并旁路落 trace。"""

    def __init__(self, trace_logger=None, backplane=None) -> None:
        """初始化订阅表与可选 trace logger / 事件背板。

        Args:
            trace_logger: 可选本地 trace 记录器。
            backplane: 可选事件背板（``RedisEventBackplane``）；None = 单进程模式。
        """
        # 一个 session 可以同时有多个 SSE 订阅者，所以用 set 保存多队列。
        self._queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._trace_logger = trace_logger
        self._backplane = backplane
        self._backplane_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def trace_logger(self):
        """返回可选 trace logger，供 invoke_agent_stream 复用。"""
        return self._trace_logger

    @property
    def backplane(self):
        """返回可选事件背板；None = 单进程模式。"""
        return self._backplane

    # ------------------------------------------------------------------
    # 背板生命周期
    # ------------------------------------------------------------------
    async def start(self) -> bool:
        """启动背板监听（幂等）；无背板时是 no-op。

        必须先 ``await backplane.start()`` 建立 Pub/Sub 订阅，再起 fanout 任务；
        否则任务第一次跑 ``remote_events()`` 时 pubsub 还没建好，会直接空转退出。

        Returns:
            ``True`` 表示背板监听可用；直连模式 / 订阅失败返回 ``False``。
        """
        if self._backplane is None:
            return False
        started = await self._backplane.start()
        if not started:
            return False
        if self._backplane_task is not None and not self._backplane_task.done():
            return True
        self._stopping = False
        self._backplane_task = asyncio.create_task(self._forward_backplane_events())
        return True

    async def stop(self) -> None:
        """停止背板监听；幂等且无背板时是 no-op。"""
        self._stopping = True
        task, self._backplane_task = self._backplane_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    async def aclose(self) -> None:
        """停止背板监听并关闭背板连接（供 lifespan / worker 收尾调用）。"""
        await self.stop()
        backplane, self._backplane = self._backplane, None
        if backplane is None:
            return
        close = getattr(backplane, "close", None)
        if close is not None:
            await close()

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    def publish(self, session_id: str, event_type: str, payload: dict) -> None:
        """补 trace_id 后写 trace，并投递到该 session 的所有订阅队列。

        注入背板时同时发布到 Redis 频道，供其他 worker 实例的 SSE 订阅者消费。

        Args:
            session_id: 会话 id。
            event_type: 事件类型。
            payload: 事件载荷。
        """
        # P10-5：把事件同步给当前请求的成本采集器（未设置时 no-op）。
        record_metric_event(event_type, payload)
        snapshot = ShoppingContext.current()
        if isinstance(payload, dict) and snapshot is not None:
            correlation = {
                key: getattr(snapshot, key, "")
                for key in ("task_id", "client_request_id")
                if getattr(snapshot, key, "") and key not in payload
            }
            if correlation:
                payload = {**payload, **correlation}
        if isinstance(payload, dict) and "trace_id" not in payload:
            trace_id = ShoppingContext.current_trace_id()
            if trace_id:
                # 不改调用方原字典，SSE 帧与 trace 行共享同一份带 trace_id 的副本。
                payload = {**payload, "trace_id": trace_id}
        if self._trace_logger is not None:
            trace_id = payload.get("trace_id", "") if isinstance(payload, dict) else ""
            self._trace_logger.record_nowait(session_id, trace_id, event_type, payload)
        self._publish_local(session_id, event_type, payload)
        self._publish_backplane(session_id, event_type, payload)

    def publish_current(self, event_type: str, payload: dict) -> None:
        """用当前上下文的 session_id 发布事件。

        Args:
            event_type: 事件类型。
            payload: 事件载荷。
        """
        self.publish(ShoppingContext.current_session_id(), event_type, payload)

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    async def subscribe(self, session_id: str) -> AsyncIterator[str]:
        """订阅某 session 的 SSE 帧（异步生成器）。

        Args:
            session_id: 会话 id。

        Returns:
            形如 ``event: ...\ndata: ...\n\n`` 的帧字符串。
        """
        # 有界队列：消费慢时宁可丢帧也不无限堆积内存。
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues[session_id].add(queue)
        try:
            while True:
                event_type, payload = await queue.get()
                yield f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            # 客户端断开时移除订阅，避免向已关闭连接继续写。
            self._queues[session_id].discard(queue)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _publish_local(self, session_id: str, event_type: str, payload: dict) -> None:
        """把事件写进本进程该 session 的所有订阅队列（满则丢帧）。"""
        for queue in list(self._queues.get(session_id, ())):
            try:
                queue.put_nowait((event_type, payload))
            except asyncio.QueueFull:
                # 慢消费者队列满：丢弃该帧，不能让发布方被 SSE 阻塞。
                pass

    def _publish_backplane(self, session_id: str, event_type: str, payload: dict) -> None:
        """把事件发到 Redis 背板（best-effort，不阻塞同步 publish）。

        为什么用 ``create_task``：``publish`` 是同步方法，会被工具 / SSE 帧路径直接调用，
        不能 await；背板发布失败只丢一帧，由客户端重连兜底。
        """
        if self._backplane is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 无事件循环（同步测试 / CLI）：背板直接跳过，不影响本地 fanout。
            return
        loop.create_task(self._backplane.publish(session_id, event_type, dict(payload)))

    async def _forward_backplane_events(self) -> None:
        """后台循环：把背板收到的**其他实例**事件写进本进程订阅队列。

        背板是唯一消费者（``remote_events``），这里只做“按 session 分发”，
        不回写背板，避免事件在两个实例之间来回广播。
        """
        backplane = self._backplane
        if backplane is None:
            return
        while not self._stopping:
            try:
                stream = getattr(backplane, "remote_events", None)
                if stream is None:
                    # 测试替身 / 老接口：只保留本地 fanout，不追跨实例事件。
                    await asyncio.sleep(3600)
                    continue
                async for session_id, event_type, payload in stream():
                    if self._stopping:
                        return
                    self._publish_local(session_id, event_type, payload)
                # 事件流正常结束（订阅被释放 / 背板关闭）：不要立刻重开，
                # 加一个短退避，既避免忙轮询，也避免把日志刷爆。
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 背板抖动不能让监听循环退出
                await asyncio.sleep(0.2)

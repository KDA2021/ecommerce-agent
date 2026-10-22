# -*- coding: utf-8 -*-
"""P11-4 Redis Pub/Sub 事件背板：把 SSE 事件广播到所有 worker 实例。

单进程 SSE 的天然限制：``EventBroker`` 的订阅队列是**进程内** ``asyncio.Queue``，
任务被 A 实例的 worker 处理、买家的 SSE 连接挂在 B 实例时，事件永远到不了 B。

背板（backplane）用 Pub/Sub 解决：每个实例 ``PSUBSCRIBE ecommerce:events:*``，
``EventBroker.publish`` 除了写本地队列，还把同一份 payload 发到对应 session 的频道；
其他实例收到后只写**自己进程内**的订阅队列，形成跨实例 fanout。

为什么是 Pub/Sub 而不是 Stream：
- SSE 事件是**即时通知**，丢失一帧（客户端断线/实例重启）由客户端重连重放兜底，
  不需要持久化与 ack；Pub/Sub 的 fire-and-forget 语义刚好匹配，也没有 PEL 堆积问题。
- 本地 fanout 失败（队列满）只丢帧，和改造前的行为一致。

所有权约定（重要）：
- 一个背板实例只有**一条** pubsub 连接、**一个**消费方；
  ``remote_events()`` 是唯一的事件流入口（``EventBroker`` 用它做本地 fanout），
  ``listen()`` 是给单个订阅者用的便捷包装。两者不要同时跑，否则会争抢同一条连接。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

try:  # pragma: no cover - 依赖缺失时整体降级为 disabled
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: 频道前缀：``ecommerce:events:{session_id}``。
DEFAULT_CHANNEL_PREFIX = "ecommerce:events:"
#: 订阅模式：所有 session 的事件都由同一个 psubscribe 监听器收下来。
DEFAULT_CHANNEL_PATTERN = "ecommerce:events:*"
#: 每个 session 的本地转队列长度；满了丢最旧的帧（慢消费者不能拖垮发布方）。
DEFAULT_QUEUE_SIZE = 200
#: 单次取消息的等待毫秒数：监听循环靠它周期性检查停止标志。
DEFAULT_POLL_TIMEOUT_SECONDS = 0.2
_SOCKET_TIMEOUT_SECONDS = 2.0
#: 空轮询退避：无消息时睡多久（秒），避免忙循环饿死事件循环。
_IDLE_BACKOFF_SECONDS = 0.02


class RedisEventBackplane:
    """Redis Pub/Sub 事件背板（发布 + 订阅转发）。

    Attributes:
        enabled: ``True`` 表示客户端构造成功；``False`` 时所有方法 no-op。
    """

    def __init__(
        self,
        redis_url: str = "",
        *,
        client: Any | None = None,
        channel_prefix: str = DEFAULT_CHANNEL_PREFIX,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        decode_responses: bool = True,
    ) -> None:
        """创建背板；URL 为空或客户端构造失败时得到 disabled 实例。

        Args:
            redis_url: Redis 连接串；空字符串 = 背板关闭（direct 模式不构造）。
            client: 可选注入的异步客户端（测试用 fakeredis）。
            channel_prefix: 频道前缀。
            queue_size: 单个 session 本地转队列长度。
            poll_timeout_seconds: 监听循环取消息超时（秒）。
            decode_responses: 默认解码为 str；注入 client 时由调用方保证。
        """
        self._redis_url = redis_url or ""
        self._client: Any | None = None
        self.channel_prefix = channel_prefix
        self.queue_size = int(queue_size)
        self.poll_timeout_seconds = float(poll_timeout_seconds)

        self._pubsub: Any | None = None
        self._consume_task: asyncio.Task | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._channels: set[str] = set()
        self._stopping = False

        if client is not None:
            self._client = client
            return

        if not self._redis_url:
            logger.info("RedisEventBackplane disabled：REDIS_URL 为空")
            return

        if aioredis is None:  # pragma: no cover
            logger.warning("RedisEventBackplane disabled：redis 包不可用")
            return

        try:
            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=decode_responses,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("RedisEventBackplane disabled：初始化失败 %s", exc)
            self._client = None

    # ------------------------------------------------------------------
    # 只读属性 / 生命周期
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        client: Any | None = None,
    ) -> "RedisEventBackplane":
        """按 Settings 构造（读取 redis_url）。

        Args:
            settings: 运行时配置。
            client: 可选注入的异步客户端（测试用）。

        Returns:
            RedisEventBackplane。
        """
        return cls(getattr(settings, "redis_url", "") or "", client=client)

    @property
    def enabled(self) -> bool:
        """背板是否可用。"""
        return self._client is not None

    @property
    def client(self) -> Any | None:
        """底层异步客户端；disabled 为 ``None``。"""
        return self._client

    async def start(self) -> bool:
        """建立 Pub/Sub 订阅（幂等，不启动额外任务）。

        Returns:
            ``True`` 表示订阅已就绪；disabled / 订阅失败返回 ``False``。
        """
        if self._client is None:
            return False
        if self._pubsub is not None:
            return True
        try:
            self._pubsub = self._client.pubsub()
            await self._pubsub.psubscribe(DEFAULT_CHANNEL_PATTERN)
        except Exception as exc:  # noqa: BLE001 - 背板是旁路，订阅失败不阻断服务
            logger.warning("RedisEventBackplane 订阅失败：%s", exc)
            self._pubsub = None
            return False
        self._stopping = False
        return True

    async def stop(self) -> None:
        """停止消费并释放 pubsub；幂等且绝不抛异常。"""
        self._stopping = True
        task, self._consume_task = self._consume_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - 取消 / 循环异常都不该冒到调用方
                pass
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            try:
                # redis-py 5+ 用 aclose；旧版本只有 close（已废弃但仍在用）。
                aclose = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                if aclose is not None:
                    await aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("RedisEventBackplane pubsub 关闭失败 err=%s", exc)

    async def close(self) -> None:
        """停止消费并关闭底层客户端；幂等。"""
        await self.stop()
        client, self._client = self._client, None
        if client is None:
            return
        try:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - 兼容旧客户端
                await client.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("RedisEventBackplane.close 忽略异常 err=%s", exc)

    async def __aenter__(self) -> "RedisEventBackplane":
        """async with 入口：启动订阅并返回自身。"""
        await self.start()
        return self

    async def __aexit__(self, *_exc_info) -> None:
        """async with 出口：关闭背板。"""
        await self.close()

    def channel_for(self, session_id: str) -> str:
        """session_id → Pub/Sub 频道名。

        Args:
            session_id: 会话 id。

        Returns:
            频道名。
        """
        return f"{self.channel_prefix}{session_id}"

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    async def publish(self, session_id: str, event_type: str, payload: dict) -> bool:
        """把事件发布到该 session 的 Redis 频道。

        Args:
            session_id: 会话 id。
            event_type: 事件类型（SSE ``event:``）。
            payload: 事件载荷（JSON 可序列化）。

        Returns:
            ``True`` 表示发布成功；disabled / 异常返回 ``False``（只记 debug）。
        """
        if self._client is None:
            return False
        try:
            message = json.dumps(
                {"event": event_type, "payload": payload}, ensure_ascii=False, default=str,
            )
            await self._client.publish(self.channel_for(session_id), message)
            return True
        except Exception as exc:  # noqa: BLE001 - 背板是旁路，失败不能影响本进程 SSE
            logger.debug("RedisEventBackplane.publish 降级 session=%s err=%s", session_id, exc)
            return False

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    async def remote_events(self) -> AsyncIterator[tuple[str, str, dict]]:
        """统一事件流：产出其他实例发布的所有 session 事件（异步生成器）。

        这是背板连接的唯一消费者入口。生成器退出（正常返回 / 被取消 / 关闭）时
        自动关闭 pubsub 并清空本地转队列，下一次调用会重新订阅。

        Yields:
            ``(session_id, event_type, payload)``。
        """
        pubsub = self._pubsub
        if pubsub is None:
            return
        try:
            while not self._stopping:
                # 非阻塞取样 + 显式退避：不在这里做阻塞读。
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.0,
                    )
                except Exception as exc:  # noqa: BLE001 - 读失败按本轮无消息处理
                    logger.debug("RedisEventBackplane.get_message 失败 err=%s", exc)
                    message = None
                if message is None:
                    # 显式让出事件循环 + 退避：异步 pubsub 的 timeout 实现因客户端而异，
                    # 有些客户端（如 fakeredis）在无消息时立即返回 None，
                    # 若只 sleep(0) 会变成忙轮询，饿死同进程的其它任务。
                    await asyncio.sleep(_IDLE_BACKOFF_SECONDS)
                    continue
                decoded = _decode_message(message, self.channel_prefix)
                if decoded is not None:
                    yield decoded
        finally:
            if self._pubsub is pubsub:
                self._pubsub = None
                try:
                    aclose = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                    if aclose is not None:
                        await aclose()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("RedisEventBackplane pubsub 关闭失败 err=%s", exc)
            self._queues.clear()
            self._channels.clear()

    async def listen(self, session_id: str) -> AsyncIterator[tuple[str, dict]]:
        """订阅**某 session** 的跨实例事件（单订阅者便捷包装）。

        内部只保存一个本地转队列；多个订阅者请直接用 ``remote_events()``
        （``EventBroker`` 就是这么做的），或者各自建一个背板实例。

        Args:
            session_id: 会话 id。

        Yields:
            ``(event_type, payload)``；生成器被关闭时自动注销并释放连接。
        """
        if not await self.start():
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        channel = self.channel_for(session_id)
        self._queues[channel] = queue
        self._channels.add(channel)

        async def _drain() -> None:
            async for remote_session, event_type, payload in self.remote_events():
                if remote_session == session_id:
                    _put_drop_oldest(queue, (event_type, payload))

        task = asyncio.create_task(_drain())
        self._consume_task = task
        try:
            while True:
                yield await queue.get()
        finally:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
            self._queues.pop(channel, None)
            self._channels.discard(channel)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _dispatch(self, session_id: str, event_type: str, payload: dict) -> bool:
        """把一条事件写进对应 session 的本地转队列（无订阅者则丢弃）。"""
        queue = self._queues.get(self.channel_for(session_id))
        if queue is None:
            # 本实例没有该 session 的本地订阅读者：直接丢弃（Pub/Sub 语义）。
            return False
        return _put_drop_oldest(queue, (event_type, payload))


def _decode_message(message: Any, channel_prefix: str) -> tuple[str, str, dict] | None:
    """把 redis-py pubsub 消息解码成 ``(session_id, event_type, payload)``。

    Args:
        message: pubsub 消息 dict。
        channel_prefix: 频道前缀（用于反解 session_id）。

    Returns:
        解码结果；不是消息帧 / JSON 非法时返回 None。
    """
    if not isinstance(message, dict):
        return None
    if message.get("type") not in ("message", "pmessage"):
        return None
    channel = _as_str(message.get("channel"))
    if not channel.startswith(channel_prefix):
        return None
    session_id = channel[len(channel_prefix):]
    data = message.get("data")
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8")
        except Exception:  # pragma: no cover
            return None
    try:
        decoded = json.loads(data)
    except Exception:
        logger.debug("RedisEventBackplane 收到非 JSON 帧 channel=%s", channel)
        return None
    if not isinstance(decoded, dict):
        return None
    event_type = str(decoded.get("event", ""))
    if not event_type:
        return None
    payload = decoded.get("payload")
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return session_id, event_type, payload


def _put_drop_oldest(queue: asyncio.Queue, item: tuple[str, dict]) -> bool:
    """写本地队列；满时丢最旧一帧再放新帧（保证订阅方最终看到最新状态）。"""
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
            queue.put_nowait(item)
            return True
        except Exception:  # noqa: BLE001
            return False


def _as_str(value: Any) -> str:
    """把 Redis 返回的 bytes / str 归一化成 str。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)

# -*- coding: utf-8 -*-
"""P11-4 Redis Stream 任务队列：消费者组 + ack + pending 重投。

为什么用 Stream 而不是 List：
- List（``BLPOP``）的语义是 "取出即消失"，worker 处理到一半崩溃任务就丢了；
- Stream + 消费者组天然提供 **PEL（Pending Entries List）**：
  消息被 ``XREADGROUP`` 取走后进入 PEL，只有 ``XACK`` 才真正出队；
  worker 崩溃后其他 worker 用 ``XAUTOCLAIM`` / ``XPENDING`` 把超时消息重投，
  整体是 at-least-once —— 与 ``app/domain/queue/ports/task_queue.py`` 的端口契约一致。

设计取舍：

- **可选启用**：和其它 Redis 能力一致，URL 为空时构造 disabled 实例，
  主链路（``QUEUE_MODE=direct``）行为与 P11-3 完全一致；
- **失败语义分两类**：
  写路径（``enqueue``）在 Redis 不可用时 **fail loud**（抛 ``QueueTaskError``），
  否则 HTTP 会 "返回 task_id 但任务从未入队"，客户端永远等不到结果；
  读路径（``get_status`` / ``depth``）fail soft，返回 None / 0，供监控旁路使用。
- **状态与消息分离**：Stream 只放任务本体，状态写 ``ecommerce:task:{task_id}`` hash，
  客户端查状态不会重复消费消息。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable, Optional

from app.domain.queue.ports.task_queue import IntentTask, TaskStatus

try:  # pragma: no cover - 依赖缺失时整体降级为 disabled
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: 默认 Stream key：所有意图任务共用一条流。
DEFAULT_STREAM_KEY = "ecommerce:intents"
#: 默认消费者组：多个 worker 属于同一个组，消息只会投给其中一个（竞争消费）。
DEFAULT_GROUP = "ecommerce-workers"
#: 任务状态 hash 的 key 前缀。
DEFAULT_STATUS_PREFIX = "ecommerce:task:"
#: 状态 hash 的默认 TTL：4 小时足够客户端轮询，也避免任务状态无限堆积。
DEFAULT_STATUS_TTL_SECONDS = 4 * 60 * 60
#: 连接/读写超时压到 2s：Redis 卡住时不能拖死请求。
_SOCKET_TIMEOUT_SECONDS = 2.0


class QueueTaskError(RuntimeError):
    """队列写路径失败（``QUEUE_MODE=redis`` 但 Redis 不可用 / 命令失败）。"""


class RedisStreamTaskQueue:
    """Redis Stream 实现的任务队列（at-least-once）。

    Attributes:
        enabled: ``True`` 表示已构造出客户端；``False`` 表示队列能力关闭。
    """

    def __init__(
        self,
        redis_url: str = "",
        *,
        client: Any | None = None,
        stream_key: str = DEFAULT_STREAM_KEY,
        group: str = DEFAULT_GROUP,
        consumer: str = "",
        status_prefix: str = DEFAULT_STATUS_PREFIX,
        status_ttl_seconds: int = DEFAULT_STATUS_TTL_SECONDS,
        max_stream_length: int | None = 10_000,
        decode_responses: bool = True,
    ) -> None:
        """创建队列；URL 为空或客户端构造失败时得到 disabled 实例。

        Args:
            redis_url: Redis 连接串；空字符串 = 队列关闭（direct 模式不构造）。
            client: 可选注入的异步客户端（测试用 fakeredis）。
            stream_key: Stream key。
            group: 消费者组名。
            consumer: 本实例消费者名；空串时用 ``host-pid`` 保证多 worker 不重名。
            status_prefix: 任务状态 hash key 前缀。
            status_ttl_seconds: 状态 hash TTL（秒）；<=0 表示不过期。
            max_stream_length: ``XADD MAXLEN`` 近似裁剪上限；None = 不裁剪。
            decode_responses: 默认解码为 str；注入 client 时由调用方保证。
        """
        self._redis_url = redis_url or ""
        self._client: Any | None = None
        self.stream_key = stream_key
        self.group = group
        self.consumer = consumer or _default_consumer_name()
        self.status_prefix = status_prefix
        self.status_ttl_seconds = int(status_ttl_seconds)
        self.max_stream_length = max_stream_length
        self._decode = decode_responses
        self._group_ready = False

        if client is not None:
            self._client = client
            return

        if not self._redis_url:
            logger.info("RedisStreamTaskQueue disabled：REDIS_URL 为空")
            return

        if aioredis is None:  # pragma: no cover
            logger.warning("RedisStreamTaskQueue disabled：redis 包不可用")
            return

        try:
            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=decode_responses,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("RedisStreamTaskQueue disabled：初始化失败 %s", exc)
            self._client = None

    # ------------------------------------------------------------------
    # 生命周期 / 只读属性
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        client: Any | None = None,
        consumer: str = "",
    ) -> "RedisStreamTaskQueue":
        """按 Settings 构造（读取 redis_url / queue_stream_key / queue_group）。

        Args:
            settings: 运行时配置。
            client: 可选注入的异步客户端（测试用）。
            consumer: 可选消费者名。

        Returns:
            RedisStreamTaskQueue。
        """
        return cls(
            getattr(settings, "redis_url", "") or "",
            client=client,
            stream_key=getattr(settings, "queue_stream_key", DEFAULT_STREAM_KEY) or DEFAULT_STREAM_KEY,
            group=getattr(settings, "queue_group", DEFAULT_GROUP) or DEFAULT_GROUP,
            consumer=consumer,
        )

    @property
    def enabled(self) -> bool:
        """队列是否可用；disabled 时写路径抛 ``QueueTaskError``。"""
        return self._client is not None

    @property
    def redis_url(self) -> str:
        """构造时使用的 Redis URL。"""
        return self._redis_url

    @property
    def client(self) -> Any | None:
        """底层异步客户端；disabled 为 ``None``（测试/排障用）。"""
        return self._client

    async def __aenter__(self) -> "RedisStreamTaskQueue":
        """async with 入口，返回自身。"""
        return self

    async def __aexit__(self, *_exc_info) -> None:
        """async with 出口：关闭底层连接。"""
        await self.close()

    async def ensure_ready(self) -> bool:
        """建消费者组（幂等）并 PING；不可用时返回 ``False``。

        Returns:
            ``True`` 表示 Redis 可用且消费者组已就绪。
        """
        if self._client is None:
            return False
        try:
            await self.ensure_group()
            await self._client.ping()
            return True
        except Exception as exc:  # noqa: BLE001 - 就绪探测只做布尔判断
            logger.warning("RedisStreamTaskQueue 未就绪：%s", exc)
            return False

    async def close(self) -> None:
        """关闭底层客户端；幂等且绝不抛异常。"""
        client, self._client = self._client, None
        self._group_ready = False
        if client is None:
            return
        try:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - 兼容旧客户端
                await client.close()
        except Exception as exc:
            logger.debug("RedisStreamTaskQueue.close 忽略异常 err=%s", exc)

    async def ensure_group(self) -> None:
        """确保消费者组存在；``mkstream=True`` 让空库首次启动也能建组。

        Raises:
            QueueTaskError: Redis 不可用或建组失败（BUSYGROUP 除外，视为已就绪）。
        """
        client = self._require_client()
        if self._group_ready:
            return
        try:
            await client.xgroup_create(
                self.stream_key, self.group, id="0", mkstream=True,
            )
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP 是并发建组的正常结果
            if "BUSYGROUP" not in str(exc):
                raise QueueTaskError(f"建消费者组失败：{exc}") from exc
        self._group_ready = True

    # ------------------------------------------------------------------
    # 生产者
    # ------------------------------------------------------------------
    async def enqueue(self, task: IntentTask) -> str:
        """把任务写进 Stream。

        Args:
            task: 意图任务。

        Returns:
            Stream 消息 id（``enqueue`` 契约返回 ``None`` 的补充信息）。

        Raises:
            QueueTaskError: Redis 不可用或 XADD 失败。
        """
        client = self._require_client()
        fields = _task_fields(task)
        kwargs: dict[str, Any] = {}
        if self.max_stream_length is not None:
            kwargs["maxlen"] = int(self.max_stream_length)
            kwargs["approximate"] = True
        try:
            message_id = await client.xadd(self.stream_key, fields, **kwargs)
        except Exception as exc:
            raise QueueTaskError(f"任务入队失败：{exc}") from exc
        return _as_str(message_id)

    # ------------------------------------------------------------------
    # 消费者
    # ------------------------------------------------------------------
    async def read_new(self, count: int = 1, block_ms: int = 0) -> list[tuple[str, IntentTask]]:
        """读新消息（``>`` 语义，只投给本消费者一次）。

        Args:
            count: 最多读取条数。
            block_ms: 阻塞毫秒；0 = 不阻塞。

        Returns:
            ``[(message_id, task), ...]``；无消息或 Redis 不可用返回空列表。
        """
        if not await self._ready_for_read():
            return []
        return await self._xreadgroup(">", count=count, block_ms=block_ms)

    async def read_pending(
        self,
        count: int = 10,
        min_idle_ms: int = 0,
    ) -> list[tuple[str, IntentTask, int]]:
        """读本消费者组未 ack 的消息（PEL 巡检 / 手动重投用）。

        Args:
            count: 最多读取条数。
            min_idle_ms: 只返回空闲超过该毫秒数的消息（0 = 全部）。

        Returns:
            ``[(message_id, task, delivery_count), ...]``；异常时返回空列表。
        """
        if not await self._ready_for_read():
            return []
        try:
            # '0' = 从 PEL 头部开始扫；XPENDING 的扩展形式直接给出 idle / delivery。
            pending = await self._xreadgroup("0", count=count, block_ms=0)
        except Exception as exc:  # noqa: BLE001 - 巡检失败不应打断消费主循环
            logger.debug("RedisStreamTaskQueue.read_pending 失败 err=%s", exc)
            return []
        if not pending:
            return []
        meta = await self._pending_meta()
        result: list[tuple[str, IntentTask, int]] = []
        for message_id, task in pending:
            idle_ms, deliveries = meta.get(message_id, (0, 1))
            if min_idle_ms and idle_ms < min_idle_ms:
                continue
            result.append((message_id, task, deliveries))
        return result

    async def claim_stale(
        self,
        min_idle_ms: int = 60_000,
        count: int = 10,
    ) -> list[tuple[str, IntentTask, int]]:
        """把“取走但长时间没 ack”的消息重投给消费者组。

        优先用 ``XAUTOCLAIM``（Redis 6.2+ / fakeredis 支持）；客户端版本不支持时，
        退化为 ``XPENDING`` 巡检 + 重新 ``XREADGROUP``，保证能力不因版本丢失。

        Args:
            min_idle_ms: 判定“卡住”的空闲毫秒阈值。
            count: 最多重投条数。

        Returns:
            ``[(message_id, task, delivery_count), ...]``。
        """
        if not await self._ready_for_read():
            return []
        try:
            result = await self._xautoclaim(min_idle_ms=min_idle_ms, count=count)
        except Exception as exc:  # noqa: BLE001 - 版本/参数差异走 XPENDING 兜底
            logger.debug("RedisStreamTaskQueue XAUTOCLAIM 不可用，走 XPENDING 兜底：%s", exc)
            result = None
        if result:
            # 部分客户端（含 fakeredis）的 XAUTOCLAIM 不回传 delivery 次数：用 XPENDING 补齐。
            meta = await self._pending_meta()
            enriched: list[tuple[str, IntentTask, int]] = []
            for message_id, task, _deliveries in result:
                _idle, deliveries = meta.get(message_id, (0, _deliveries))
                enriched.append((message_id, task, deliveries))
            return enriched

        # XAUTOCLAIM 不可用（None / 版本差异）或没有可 claim 的消息：
        # 用 XPENDING 巡检超时条目，再自己重投。
        pending = await self.read_pending(count=count, min_idle_ms=min_idle_ms)
        if not pending:
            return []
        return await self._redeliver_pending(pending)

    async def _redeliver_pending(
        self,
        pending: list[tuple[str, IntentTask, int]],
    ) -> list[tuple[str, IntentTask, int]]:
        """``XAUTOCLAIM`` 的兜底重投。

        优先 ``XCLAIM``：把 PEL 里超时条目的所有权换到当前消费者，消息本体不动。
        客户端连 ``XCLAIM`` 都没有时，退化为 ``XADD`` 一份新 entry + ``XDEL`` 旧 entry
        —— 重新入队最可靠；``XDEL`` 保证 Stream 长度不膨胀，不会重复堆积。
        """
        message_ids = [message_id for message_id, _task, _deliveries in pending]
        claimed_ids = await self._xclaim(message_ids)
        if not claimed_ids:
            return await self._reinsert_pending(pending)
        return [
            (message_id, task, deliveries + 1)
            for message_id, task, deliveries in pending
        ]

    async def _reinsert_pending(
        self,
        pending: list[tuple[str, IntentTask, int]],
    ) -> list[tuple[str, IntentTask, int]]:
        """`XCLAIM` 不可用时的最后兜底：重新入队 + 删除旧 entry（长度守恒）。"""
        redelivered: list[tuple[str, IntentTask, int]] = []
        for message_id, task, deliveries in pending:
            try:
                new_id = await self.enqueue(task)
            except QueueTaskError as exc:
                logger.warning("pending 任务无法重投 id=%s err=%s", message_id, exc)
                continue
            await self._delete_entry(message_id)
            redelivered.append((new_id, task, deliveries + 1))
        return redelivered

    async def _delete_entry(self, message_id: str) -> None:
        """删除 Stream 里的旧 entry（重投兜底用，best-effort）。"""
        if self._client is None:
            return
        try:
            await self._client.xdel(self.stream_key, message_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("XDEL 失败 id=%s err=%s", message_id, exc)

    async def _xclaim(self, message_ids: list[str]) -> list[str]:
        """把超时消息 claim 到本消费者名下；返回成功 claim 的 message id。

        Returns:
            成功 claim 的 id 列表；客户端不支持 / 调用失败返回空列表。
        """
        client = self._client
        xclaim = getattr(client, "xclaim", None)
        if client is None or xclaim is None or not message_ids:
            return []
        try:
            result = await xclaim(
                self.stream_key,
                self.group,
                self.consumer,
                min_idle_time=0,
                message_ids=list(message_ids),
                idle=0,
                retrycount=1,
                justid=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("XCLAIM 不可用，走重新入队兜底：%s", exc)
            return []
        return [_as_str(item) for item in result or [] if item]

    async def ack(self, message_id: str) -> bool:
        """确认一条消息（从 PEL 移除）。

        Args:
            message_id: ``XADD`` 返回的消息 id。

        Returns:
            ``True`` 表示确认成功；消息不存在 / Redis 异常返回 ``False``。
        """
        if self._client is None:
            return False
        try:
            return bool(await self._client.xack(self.stream_key, self.group, message_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("RedisStreamTaskQueue.ack 失败 id=%s err=%s", message_id, exc)
            return False

    # ------------------------------------------------------------------
    # 任务状态
    # ------------------------------------------------------------------
    async def set_status(self, status: TaskStatus) -> None:
        """写任务状态 hash（失败只记日志，不影响任务执行）。

        Args:
            status: 任务状态。
        """
        if self._client is None:
            return
        key = self.status_key(status.task_id)
        mapping = {
            "task_id": status.task_id,
            "state": status.state,
            "final_text": status.final_text,
            "error": status.error,
            "queue_position": int(status.queue_position),
            "updated_at": str(time.time()),
        }
        try:
            await self._client.hset(key, mapping=mapping)
            if self.status_ttl_seconds > 0:
                await self._client.expire(key, self.status_ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - 状态是旁路，失败不能影响任务
            logger.warning("任务状态写入失败 task=%s err=%s", status.task_id, exc)

    async def get_status(self, task_id: str) -> Optional[TaskStatus]:
        """读任务状态；不存在 / 异常 / disabled 返回 ``None``。

        Args:
            task_id: 任务 id。

        Returns:
            TaskStatus 或 None。
        """
        if self._client is None:
            return None
        try:
            raw = await self._client.hgetall(self.status_key(task_id))
        except Exception as exc:  # noqa: BLE001
            logger.debug("任务状态读取失败 task=%s err=%s", task_id, exc)
            return None
        if not raw:
            return None
        fields = {_as_str(k): _as_str(v) for k, v in raw.items()}
        try:
            position = int(float(fields.get("queue_position") or 0))
        except (TypeError, ValueError):
            position = 0
        return TaskStatus(
            task_id=fields.get("task_id") or task_id,
            state=fields.get("state", ""),
            final_text=fields.get("final_text", ""),
            error=fields.get("error", ""),
            queue_position=position,
        )

    async def depth(self) -> int:
        """待消费任务数（Stream 当前长度）；异常 / disabled 返回 0。

        Returns:
            当前 Stream 中消息条数。
        """
        if self._client is None:
            return 0
        try:
            return int(await self._client.xlen(self.stream_key))
        except Exception as exc:  # noqa: BLE001
            logger.debug("队列深度读取失败 err=%s", exc)
            return 0

    def status_key(self, task_id: str) -> str:
        """任务状态 hash 的 Redis key。

        Args:
            task_id: 任务 id。

        Returns:
            ``{status_prefix}{task_id}``。
        """
        return f"{self.status_prefix}{task_id}"

    # ------------------------------------------------------------------
    # 内部：Redis 命令封装
    # ------------------------------------------------------------------
    def _require_client(self) -> Any:
        """返回客户端；disabled 时抛 ``QueueTaskError``（写路径 fail loud）。"""
        if self._client is None:
            raise QueueTaskError("队列不可用：REDIS_URL 为空或客户端初始化失败")
        return self._client

    async def _ready_for_read(self) -> bool:
        """读路径就绪判定：disabled 或建组失败都返回 ``False``（fail soft）。"""
        if self._client is None:
            return False
        try:
            await self.ensure_group()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("消费者组未就绪 err=%s", exc)
            return False

    async def _xreadgroup(
        self,
        start: str,
        *,
        count: int,
        block_ms: int,
    ) -> list[tuple[str, IntentTask]]:
        """执行 ``XREADGROUP`` 并归一化返回结构。

        Args:
            start: ``>``（新消息）或 ``0``（PEL 重读）。
            count: 最多条数。
            block_ms: 阻塞毫秒。

        Returns:
            ``[(message_id, task), ...]``。
        """
        client = self._client
        if client is None:
            return []
        kwargs: dict[str, Any] = {}
        if block_ms and block_ms > 0:
            kwargs["block"] = int(block_ms)
        try:
            raw = await client.xreadgroup(
                self.group, self.consumer, {self.stream_key: start}, count=count, **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - 读失败按“本轮无消息”处理
            logger.debug("XREADGROUP 失败 start=%s err=%s", start, exc)
            return []
        return _parse_stream_entries(raw)

    async def _pending_meta(self) -> dict[str, tuple[int, int]]:
        """读取 PEL 元信息：``message_id -> (idle_ms, delivery_count)``。"""
        client = self._require_client()
        try:
            rows = await client.xpending_range(
                self.stream_key, self.group, min="-", max="+", count=1000,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("XPENDING 读取失败 err=%s", exc)
            return {}
        meta: dict[str, tuple[int, int]] = {}
        for row in rows or []:
            if isinstance(row, dict):
                message_id = _as_str(row.get("message_id"))
                idle = int(row.get("time_since_delivered", 0) or 0)
                deliveries = int(row.get("times_delivered", 1) or 1)
            else:
                message_id = _as_str(row[0] if len(row) > 0 else "")
                idle = int(row[2] if len(row) > 2 else 0)
                deliveries = int(row[3] if len(row) > 3 else 1)
            if message_id:
                meta[message_id] = (idle, deliveries)
        return meta

    async def _xautoclaim(
        self,
        *,
        min_idle_ms: int,
        count: int,
    ) -> Optional[list[tuple[str, IntentTask, int]]]:
        """``XAUTOCLAIM`` 实现；客户端不支持该方法时返回 ``None`` 交给 fallback。"""
        client = self._client
        autoclaim = getattr(client, "xautoclaim", None)
        if client is None or autoclaim is None:
            return None
        try:
            raw = await autoclaim(
                self.stream_key,
                self.group,
                self.consumer,
                min_idle_time=int(min_idle_ms),
                start_id="0-0",
                count=int(count),
            )
        except TypeError as exc:
            # 参数名差异（start vs start_id）在 redis-py / fakeredis 版本间存在，
            # 属于“能力可用但调用方式不同”，交给 XPENDING 兜底。
            raise QueueTaskError(f"XAUTOCLAIM 参数不兼容：{exc}") from exc
        except Exception as exc:  # noqa: BLE001 - 命令不存在 / 服务端错误
            raise QueueTaskError(f"XAUTOCLAIM 失败：{exc}") from exc
        # redis-py 返回 (next_cursor, entries, deleted_ids)；老版本只有前两个。
        message_id_rows = []
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            message_id_rows = raw[1] or []
        return _parse_claim_entries(message_id_rows)

    async def _claim_via_pending(
        self,
        *,
        min_idle_ms: int,
        count: int,
    ) -> list[tuple[str, IntentTask, int]]:
        """``XPENDING`` 兜底重投：读空闲超阈值的消息并手动重新入队。

        手动重投 = 原消息 ``XACK`` 掉 + 重新 ``XADD``（拿到新 id），
        宁可产生一次重复投递（at-least-once 允许）也不能让任务永久卡在 PEL。
        """
        pending = await self.read_pending(count=count, min_idle_ms=min_idle_ms)
        reclaimed: list[tuple[str, IntentTask, int]] = []
        for message_id, task, deliveries in pending:
            try:
                new_id = await self.enqueue(task)
            except QueueTaskError as exc:
                logger.warning("pending 重投失败 id=%s err=%s", message_id, exc)
                continue
            await self.ack(message_id)
            reclaimed.append((new_id, task, deliveries))
        return reclaimed

    # ------------------------------------------------------------------
    # 内部：payload 编码
    # ------------------------------------------------------------------


def _task_fields(task: IntentTask) -> dict[str, str]:
    """把任务值对象拍平成 Stream field（值必须是字符串或 bytes）。"""
    fields = {key: _to_field_value(value) for key, value in task.to_dict().items()}
    return fields


def _task_from_fields(raw: dict) -> IntentTask:
    """从 Stream field 还原任务；兼容嵌套 JSON payload。"""
    fields = {_as_str(key): _as_str(value) for key, value in (raw or {}).items()}
    nested = fields.get("payload")
    if nested:
        try:
            decoded = json.loads(nested)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            return IntentTask.from_dict(decoded)
    return IntentTask.from_dict(fields)


def _to_field_value(value: Any) -> str:
    """把任意字段值转成可写入 Stream 的字符串（dict / list 走 JSON）。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _as_str(value: Any) -> str:
    """把 Redis 返回的 bytes / str 归一化成 str。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:  # pragma: no cover - 非 UTF-8 字段按原样兜底
            return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_mapping(raw: Any) -> dict[str, Any]:
    """把 ``{b'k': b'v'}`` 这类 mapping 的 key 统一解码成 str。"""
    return {_as_str(key): value for key, value in (raw or {}).items()}


def _parse_stream_entries(raw: Any) -> list[tuple[str, IntentTask]]:
    """归一化 ``XREADGROUP`` / ``XREAD`` 返回结构为 ``[(id, task), ...]``。"""
    entries: list[tuple[str, IntentTask]] = []
    if not raw:
        return entries
    for stream in raw if isinstance(raw, (list, tuple)) else [raw]:
        # redis-py 返回 (stream_key, [(id, {field: value}), ...])；
        # 有的客户端直接给 entries 列表。
        rows = stream[1] if isinstance(stream, (list, tuple)) and len(stream) == 2 else stream
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            message_id = _as_str(row[0])
            fields = _decode_mapping(row[1])
            try:
                entries.append((message_id, _task_from_fields(fields)))
            except Exception as exc:  # noqa: BLE001 - 坏消息不能毒死整个消费者
                logger.warning("队列消息反序列化失败 id=%s err=%s", message_id, exc)
    return entries


def _parse_claim_entries(rows: Iterable[Any]) -> list[tuple[str, IntentTask, int]]:
    """归一化 ``XAUTOCLAIM`` 返回的 entries 为 ``[(id, task, deliveries), ...]``。"""
    claimed: list[tuple[str, IntentTask, int]] = []
    for entry in rows or []:
        # redis-py: [(id, {field: value}, delivery_count), ...]
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            message_id = _as_str(entry[0])
            fields = _decode_mapping(entry[1])
            deliveries = int(entry[2]) if len(entry) > 2 and entry[2] is not None else 1
        else:
            continue
        try:
            claimed.append((message_id, _task_from_fields(fields), deliveries))
        except Exception as exc:  # noqa: BLE001
            logger.warning("重投消息反序列化失败 id=%s err=%s", message_id, exc)
    return claimed


def _default_consumer_name() -> str:
    """默认消费者名：``host-pid``，保证多进程 worker 不重名。"""
    import os
    import socket

    try:
        host = socket.gethostname() or "host"
    except Exception:  # pragma: no cover
        host = "host"
    return f"{host}-{os.getpid()}"

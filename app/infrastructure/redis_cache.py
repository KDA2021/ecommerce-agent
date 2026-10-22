# -*- coding: utf-8 -*-
"""可选 Redis 基础设施：语义缓存 / 滑动窗口限流 / 幂等快路径共用。

设计原则（见 P9 设计说明）：

- **可选启用**：``REDIS_URL`` 为空时构造 disabled 实例，主链路行为与 P8 完全一致；
- **故障旁路**：任何 Redis 异常都不允许冒出到业务层——
  读失败按未命中（``None``），写失败跳过（``False``），
  限流计数失败按 0（fail-open 放行），幂等 SETNX 失败按 True（视为抢到，回退 MySQL 唯一索引）；
- **MySQL 始终是事实源**：Redis 只做加速与辅助。

测试通过 ``client=`` 注入 ``fakeredis.aioredis.FakeRedis``，不依赖真实 Redis。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

try:  # pragma: no cover - 依赖缺失时整体降级为 disabled
    import redis.asyncio as aioredis
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 连接/读写超时压到 2s：Redis 卡住时也不能拖慢主链路。
_SOCKET_TIMEOUT_SECONDS = 2.0


class RedisCache:
    """异步 Redis 门面：所有方法都吞异常并返回安全的降级值。

    Attributes:
        enabled: ``True`` 表示已构造出客户端；``False`` 表示 Redis 能力整体关闭。
    """

    def __init__(
        self,
        redis_url: str = "",
        client: Any | None = None,
        *,
        decode_responses: bool = True,
    ) -> None:
        """创建门面；url 为空或客户端构造失败时得到 disabled 实例。

        Args:
            redis_url: Redis 连接串，例如 ``redis://localhost:6379/0``；空字符串 = 关闭。
            client: 可选注入的现成异步客户端（测试用 fakeredis；也可用于外部托管连接）。
            decode_responses: 默认解码为 str；注入 client 时由调用方自行保证。
        """
        self._redis_url = redis_url or ""
        self._client: Any | None = None

        if client is not None:
            # 注入路径：直接把 enabled 置 True，不触碰网络。
            self._client = client
            return

        if not self._redis_url:
            logger.info("RedisCache disabled：REDIS_URL 为空")
            return

        if aioredis is None:  # pragma: no cover
            logger.warning("RedisCache disabled：redis 包不可用")
            return

        try:
            self._client = aioredis.from_url(
                self._redis_url,
                decode_responses=decode_responses,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # URL 非法 / 依赖异常
            logger.warning("RedisCache disabled：初始化失败 %s", exc)
            self._client = None

    # ------------------------------------------------------------------
    # 只读属性
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Redis 是否可用；disabled 时所有方法走本地降级值。"""
        return self._client is not None

    @property
    def redis_url(self) -> str:
        """构造时使用的 Redis URL（可能为空字符串）。"""
        return self._redis_url

    @property
    def client(self) -> Any | None:
        """底层异步客户端；disabled 为 ``None``（测试/排障用）。"""
        return self._client

    # ------------------------------------------------------------------
    # JSON 缓存
    # ------------------------------------------------------------------
    async def get_json(self, key: str, default: Any = None) -> Any:
        """读取并反序列化 JSON；未命中/异常/disabled 返回 ``default``。"""
        raw = await self.get_raw(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except Exception:
            logger.debug("RedisCache.get_json 反序列化失败 key=%s", key)
            return default

    async def set_json(self, key: str, value: Any, ttl: float | None = None) -> bool:
        """序列化并写入 JSON；成功返回 ``True``，失败/disabled 返回 ``False``。

        Args:
            key: 缓存键。
            value: 任意可 JSON 序列化的值。
            ttl: 可选过期秒数（支持 float，<1s 时自动转毫秒）。
        """
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except Exception:
            logger.debug("RedisCache.set_json 序列化失败 key=%s", key)
            return False
        return await self._set_raw(key, payload, ttl)

    # ------------------------------------------------------------------
    # 原始字符串 / 幂等
    # ------------------------------------------------------------------
    async def get_raw(self, key: str) -> str | None:
        """读取原始字符串；未命中/异常/disabled 返回 ``None``。"""
        if self._client is None:
            return None
        try:
            value = await self._client.get(key)
        except Exception as exc:
            logger.debug("RedisCache.get_raw 降级 key=%s err=%s", key, exc)
            return None
        if value is None:
            return None
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except Exception:
                return None
        return str(value)

    async def delete(self, key: str) -> bool:
        """删除键；真正删到返回 ``True``，异常/disabled 返回 ``False``。"""
        if self._client is None:
            return False
        try:
            return bool(await self._client.delete(key))
        except Exception as exc:
            logger.debug("RedisCache.delete 降级 key=%s err=%s", key, exc)
            return False

    async def set_raw(self, key: str, value: str, ttl: float | None = None) -> bool:
        """写入原始字符串（不做 JSON 编码）；成功返回 ``True``。

        幂等快路径回填 ``key -> order_id`` 时用：必须存裸订单号，
        否则 ``get_raw`` 读出的是带引号的 JSON，无法直接当订单号回放。

        Args:
            key: 缓存键。
            value: 原始字符串值。
            ttl: 可选过期秒数（支持 float，<1s 自动转毫秒）。
        """
        return await self._set_raw(key, value, ttl)

    async def set_if_absent(self, key: str, value: str, ttl: float | None = None) -> bool:
        """SET NX 语义；返回 ``True`` 表示“抢到”（可以继续执行写操作）。

        失败语义按幂等快路径设计：**Redis 异常或 disabled 时一律返回 True**，
        让调用方走原有 MySQL 路径，由唯一索引兜底。
        """
        if self._client is None:
            return True
        try:
            kwargs = self._ttl_kwargs(ttl)
            result = await self._client.set(key, value, nx=True, **kwargs)
            return bool(result)
        except Exception as exc:
            logger.warning("RedisCache.set_if_absent 降级为抢到 key=%s err=%s", key, exc)
            return True

    # ------------------------------------------------------------------
    # 滑动窗口计数（限流）
    # ------------------------------------------------------------------
    async def zadd_and_count(
        self,
        key: str,
        member: str,
        window_seconds: float = 60.0,
    ) -> int:
        """ZSET 滑动窗口：清理过期成员、加入当前成员并返回窗口内总数。

        Args:
            key: 限流桶键（例如 ``rate:buyer-1``）。
            member: 本次请求的唯一成员（调用方应保证唯一，如 uuid）。
            window_seconds: 窗口长度（秒）。

        Returns:
            窗口内成员数。**异常或 disabled 时返回 0（fail-open 放行）**。
        """
        if self._client is None:
            return 0
        now = time.time()
        cutoff = now - float(window_seconds)
        try:
            pipe = self._client.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zadd(key, {member: now})
            pipe.zcard(key)
            pipe.expire(key, max(1, int(window_seconds) + 1))
            results = await pipe.execute()
            return int(results[2])
        except Exception as exc:
            logger.warning("RedisCache.zadd_and_count fail-open key=%s err=%s", key, exc)
            return 0

    # ------------------------------------------------------------------
    # 健康检查 / 释放
    # ------------------------------------------------------------------
    async def ping(self) -> bool:
        """PING 成功返回 ``True``；disabled 或异常返回 ``False``。"""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception as exc:
            logger.debug("RedisCache.ping 失败 err=%s", exc)
            return False

    async def close(self) -> None:
        """关闭底层客户端；幂等且绝不抛异常。"""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            aclose = getattr(client, "aclose", None)
            if aclose is not None:
                await aclose()
            else:  # pragma: no cover - 兼容旧客户端
                await client.close()
        except Exception as exc:
            logger.debug("RedisCache.close 忽略异常 err=%s", exc)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    async def _set_raw(self, key: str, value: str, ttl: float | None) -> bool:
        """写原始字符串，供 set_json 复用；失败返回 ``False``。"""
        if self._client is None:
            return False
        try:
            await self._client.set(key, value, **self._ttl_kwargs(ttl))
            return True
        except Exception as exc:
            logger.debug("RedisCache._set_raw 降级 key=%s err=%s", key, exc)
            return False

    @staticmethod
    def _ttl_kwargs(ttl: float | None) -> dict[str, Any]:
        """把秒级 TTL 转成 redis-py 的 ``ex`` / ``px`` 参数。"""
        if ttl is None:
            return {}
        seconds = float(ttl)
        if seconds <= 0:
            return {}
        if float(seconds).is_integer():
            return {"ex": int(seconds)}
        return {"px": max(1, int(seconds * 1000))}

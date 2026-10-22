# -*- coding: utf-8 -*-
"""P9-3 幂等快路径单测。

覆盖任务要求：
- SETNX 抢到 → 正常下单 → 回填 ``order_id``（TTL 300s）；
- 未抢到且 Redis 里已有 ``order_id`` → 直接回放，不再调用下单用例；
- 未抢到且 Redis 值为空 → 回退 MySQL ``find_by_idempotency_key``；
- 并发同幂等键只创建一单；
- Redis disabled / 异常时直接走 MySQL 路径（fail-safe）。

测试用 fakeredis 注入，不依赖真实 Redis；下单 / 查单用内存替身模拟
MySQL 的幂等键语义。
"""
from __future__ import annotations

import asyncio

import fakeredis.aioredis

import app.application.tools.order_tools as order_tools_module
from app.application.tools.order_tools import (
    IDEMPOTENCY_PENDING_MARKER,
    IDEMPOTENCY_TTL_SECONDS,
    _idempotency_key,
    _idempotency_redis_key,
    build_order_tools,
)
from app.application.usecases.order_usecases import OrderItemInput
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.redis_cache import RedisCache

_ITEMS = [OrderItemInput(product_id="P1001", sku_id="P1001-S1", quantity=1)]

_ARGS = {
    "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
    "shipping_address": {
        "recipient_name": "张三",
        "country": "CN",
        "state": "浙江",
        "city": "杭州",
        "address_line": "某路 1 号",
        "postal_code": "310000",
        "phone": "13800000000",
    },
}


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class _FakePlace:
    """下单替身：用 ``orders`` dict 模拟 MySQL ``find_by_idempotency_key``。"""

    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.orders: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def execute(self, buyer_id, items, shipping_address, idempotency_key=None) -> dict:
        async with self._lock:
            self.calls.append(idempotency_key)
            if idempotency_key and idempotency_key in self.orders:
                snapshot = dict(self.orders[idempotency_key])
                snapshot["idempotent_replay"] = True
                return snapshot
            snapshot = {"order_id": f"GBX-{len(self.orders) + 1:06d}", "status": "CONFIRMED"}
            if idempotency_key:
                self.orders[idempotency_key] = snapshot
            return {**snapshot, "idempotent_replay": False}


class _SlowPlace(_FakePlace):
    """第一次下单会停在 ``release`` 上，用来构造确定性并发窗口。"""

    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    async def execute(self, buyer_id, items, shipping_address, idempotency_key=None) -> dict:
        self._started.set()
        await self._release.wait()
        return await super().execute(
            buyer_id, items, shipping_address, idempotency_key=idempotency_key,
        )


class _FakeQuery:
    """查单替身：按 order_id 返回快照。"""

    def __init__(self, place: _FakePlace) -> None:
        self._place = place
        self.calls: list[str] = []

    async def execute(self, order_id: str) -> dict:
        self.calls.append(order_id)
        for snapshot in self._place.orders.values():
            if snapshot["order_id"] == order_id:
                return dict(snapshot)
        raise ValueError(f"订单不存在：{order_id}")


class _BoomClient:
    """所有命令抛异常的假 Redis 客户端。"""

    async def get(self, *args, **kwargs):
        raise RuntimeError("boom")

    async def set(self, *args, **kwargs):
        raise RuntimeError("boom")

    async def delete(self, *args, **kwargs):
        raise RuntimeError("boom")

    def pipeline(self):
        raise RuntimeError("boom")

    async def ping(self):
        raise RuntimeError("boom")

    async def aclose(self):
        return None


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _redis() -> RedisCache:
    return RedisCache("", client=fakeredis.aioredis.FakeRedis(decode_responses=True))


def _create_tool(place, query, redis_cache):
    tools = build_order_tools(place, query, None, redis_cache=redis_cache)
    return next(tool for tool in tools if tool.name == "create_order")


def _expected_idempotency_key(buyer_id: str = "buyer-1", session_id: str = "s1") -> str:
    return _idempotency_key(buyer_id, _ITEMS, session_id)


async def _invoke(create, buyer_id: str = "buyer-1", session_id: str = "s1") -> dict:
    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id=session_id,
            buyer_id=buyer_id,
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-1",
        ),
    )
    try:
        return await create.ainvoke(_ARGS)
    finally:
        ShoppingContext.reset(token)


# ----------------------------------------------------------------------
# 正常快路径：回填 + 回放
# ----------------------------------------------------------------------
async def test_success_backfills_order_id_with_ttl_and_second_call_replays():
    redis = _redis()
    fake_client = redis.client
    place = _FakePlace()
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)
    key = _idempotency_redis_key(_expected_idempotency_key())

    first = await _invoke(create)
    assert first["order_id"] == "GBX-000001"
    assert first["idempotent_replay"] is False
    assert len(place.calls) == 1

    # 占位值已被真实 order_id 覆盖，且 TTL 为 300s。
    assert await fake_client.get(key) == "GBX-000001"
    ttl = await fake_client.ttl(key)
    assert 0 < ttl <= IDEMPOTENCY_TTL_SECONDS

    # 第二次同上下文调用：命中 Redis 直接回放，不再进 place / MySQL。
    second = await _invoke(create)
    assert second["order_id"] == first["order_id"]
    assert second["idempotent_replay"] is True
    assert len(place.calls) == 1
    assert query.calls == ["GBX-000001"]


# ----------------------------------------------------------------------
# Redis 未命中 → 回退 MySQL find_by_idempotency_key
# ----------------------------------------------------------------------
async def test_redis_miss_falls_back_to_mysql_idempotency_key(monkeypatch):
    redis = _redis()
    fake_client = redis.client
    idempotency_key = _expected_idempotency_key()
    key = _idempotency_redis_key(idempotency_key)

    # MySQL 里已有同幂等键的订单（唯一索引的既有事实）。
    place = _FakePlace()
    place.orders[idempotency_key] = {"order_id": "GBX-000777", "status": "CONFIRMED"}
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)

    # 模拟“没抢到锁、但 Redis 值已过期/不可读”的并发窗口。
    async def _not_acquired(*args, **kwargs):
        return False

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(redis, "set_if_absent", _not_acquired)
    monkeypatch.setattr(redis, "get_raw", _missing)

    result = await _invoke(create)

    assert result["order_id"] == "GBX-000777"
    assert result["idempotent_replay"] is True
    # 走的正是 MySQL 幂等键路径，而不是 Redis order_id 回放。
    assert place.calls == [idempotency_key]
    assert query.calls == []
    # 拿到旧订单后仍回填 Redis，后续即可走快路径。
    assert await fake_client.get(key) == "GBX-000777"


async def test_pending_timeout_falls_back_to_mysql(monkeypatch):
    """等不到并发请求回填 order_id 时，也必须回退 MySQL 而不是报错。"""
    monkeypatch.setattr(order_tools_module, "IDEMPOTENCY_REPLAY_WAIT_ATTEMPTS", 2)
    monkeypatch.setattr(order_tools_module, "IDEMPOTENCY_REPLAY_WAIT_INTERVAL", 0.01)

    redis = _redis()
    place = _FakePlace()
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)
    idempotency_key = _expected_idempotency_key()

    # 抢锁失败，且 Redis 永远停在占位值（并发请求卡住 / 迟迟不回填）。
    async def _not_acquired(*args, **kwargs):
        return False

    async def _pending(*args, **kwargs):
        return IDEMPOTENCY_PENDING_MARKER

    monkeypatch.setattr(redis, "set_if_absent", _not_acquired)
    monkeypatch.setattr(redis, "get_raw", _pending)

    result = await _invoke(create)

    assert result["order_id"] == "GBX-000001"
    assert place.calls == [idempotency_key]
    assert query.calls == []


# ----------------------------------------------------------------------
# 并发：同幂等键只创建一单
# ----------------------------------------------------------------------
async def test_concurrent_same_key_creates_single_order():
    started = asyncio.Event()
    release = asyncio.Event()
    redis = _redis()
    place = _SlowPlace(started, release)
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)

    winner = asyncio.create_task(_invoke(create))
    await asyncio.wait_for(started.wait(), timeout=2)

    # 胜出者还在下单中；后来者 SETNX 失败、读到占位值并开始短暂轮询。
    loser = asyncio.create_task(_invoke(create))
    await asyncio.sleep(0.1)
    release.set()

    first, second = await asyncio.gather(winner, loser)

    assert first["order_id"] == second["order_id"]
    assert sorted([first["idempotent_replay"], second["idempotent_replay"]]) == [False, True]
    # 只有一个请求进入下单用例，另一个直接回放 Redis 里的 order_id。
    assert len(place.calls) == 1
    assert query.calls == [first["order_id"]]


# ----------------------------------------------------------------------
# fail-safe：Redis disabled / 异常
# ----------------------------------------------------------------------
async def test_disabled_redis_goes_straight_to_place_order():
    redis = RedisCache("")  # disabled
    place = _FakePlace()
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)

    result = await _invoke(create)

    assert result["order_id"] == "GBX-000001"
    assert len(place.calls) == 1


async def test_redis_error_is_treated_as_acquired():
    redis = RedisCache("redis://ignored:6379/0", client=_BoomClient())
    place = _FakePlace()
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)

    result = await _invoke(create)

    # Redis 故障时 SETNX 视为抢到，直接走 MySQL 下单路径，不阻断业务。
    assert result["order_id"] == "GBX-000001"
    assert len(place.calls) == 1


async def test_no_redis_cache_keeps_legacy_behavior():
    place = _FakePlace()
    query = _FakeQuery(place)
    create = _create_tool(place, query, None)

    result = await _invoke(create)

    assert result["order_id"] == "GBX-000001"
    assert len(place.calls) == 1


async def test_different_sessions_do_not_share_idempotency_key():
    redis = _redis()
    place = _FakePlace()
    query = _FakeQuery(place)
    create = _create_tool(place, query, redis)

    first = await _invoke(create, session_id="s1")
    second = await _invoke(create, session_id="s2")

    assert first["order_id"] != second["order_id"]
    assert len(place.calls) == 2

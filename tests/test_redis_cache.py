# -*- coding: utf-8 -*-
"""P9-1 RedisCache 单测。

全部通过 ``fakeredis`` 注入，不依赖真实 Redis；覆盖 TTL、SETNX、
ZSET 滑动窗口、异常旁路（fail-safe）与 disabled 行为。
"""
from __future__ import annotations

import time as time_module

import fakeredis.aioredis
import pytest

import app.infrastructure.redis_cache as redis_cache_module
from app.infrastructure.redis_cache import RedisCache


@pytest.fixture
def fake_client():
    """decode_responses=True 的异步 fakeredis 客户端。"""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def cache(fake_client):
    """注入 fakeredis 的 enabled 实例。"""
    return RedisCache("", client=fake_client)


# ----------------------------------------------------------------------
# 基础读写 / TTL
# ----------------------------------------------------------------------
async def test_json_roundtrip(cache):
    assert cache.enabled is True
    assert await cache.get_json("missing") is None
    assert await cache.set_json("k", {"a": 1, "b": [1, 2]}) is True
    assert await cache.get_json("k") == {"a": 1, "b": [1, 2]}


async def test_set_json_ttl_is_applied(cache, fake_client):
    assert await cache.set_json("k", "v", ttl=60) is True
    ttl = await fake_client.ttl("k")
    assert 0 < ttl <= 60

    # 未传 TTL 的键永久保留（ttl == -1）。
    assert await cache.set_json("forever", "v") is True
    assert await fake_client.ttl("forever") == -1


async def test_sub_second_ttl_uses_pexpire(cache, fake_client):
    assert await cache.set_json("k", "v", ttl=0.5) is True
    pttl = await fake_client.pttl("k")
    assert 0 < pttl <= 500


async def test_ttl_expires(cache, monkeypatch):
    """推进 fakeredis 的时钟，验证过期后按未命中处理。"""
    now = [1_700_000_000.0]
    monkeypatch.setattr(time_module, "time", lambda: now[0])

    assert await cache.set_json("k", "v", ttl=10) is True
    assert await cache.get_json("k") == "v"

    now[0] += 11
    assert await cache.get_json("k") is None


async def test_get_json_invalid_payload_returns_none(cache, fake_client):
    await fake_client.set("bad", "not-json")
    assert await cache.get_json("bad") is None
    assert await cache.get_raw("bad") == "not-json"


async def test_delete(cache):
    await cache.set_json("k", "v")
    assert await cache.delete("k") is True
    assert await cache.get_json("k") is None
    assert await cache.delete("k") is False


# ----------------------------------------------------------------------
# 幂等 SETNX
# ----------------------------------------------------------------------
async def test_set_if_absent_first_wins(cache, fake_client):
    assert await cache.set_if_absent("idem", "order-1") is True
    assert await cache.get_raw("idem") == "order-1"
    assert await cache.set_if_absent("idem", "order-2") is False
    assert await cache.get_raw("idem") == "order-1"


async def test_set_if_absent_with_ttl(cache, fake_client):
    assert await cache.set_if_absent("idem", "order-1", ttl=300) is True
    ttl = await fake_client.ttl("idem")
    assert 0 < ttl <= 300


# ----------------------------------------------------------------------
# ZSET 滑动窗口
# ----------------------------------------------------------------------
async def test_zadd_and_count_sliding_window(cache, fake_client, monkeypatch):
    now = [1_700_000_000.0]
    monkeypatch.setattr(time_module, "time", lambda: now[0])

    assert await cache.zadd_and_count("rate:b1", "m1") == 1
    assert await cache.zadd_and_count("rate:b1", "m2") == 2
    assert await cache.zadd_and_count("rate:b1", "m3") == 3

    # 窗口外成员被清理，只保留窗口内的新成员。
    now[0] += 61
    assert await cache.zadd_and_count("rate:b1", "m4", window_seconds=60) == 1
    assert await fake_client.zcard("rate:b1") == 1
    assert await fake_client.zscore("rate:b1", "m1") is None
    assert await fake_client.ttl("rate:b1") > 0


# ----------------------------------------------------------------------
# 异常旁路（fail-safe）
# ----------------------------------------------------------------------
class _BoomRedis:
    """所有命令都抛异常的假客户端，模拟 Redis 整体故障。"""

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
        raise RuntimeError("boom")


async def test_exceptions_fail_safe():
    cache = RedisCache("redis://ignored:6379/0", client=_BoomRedis())
    assert cache.enabled is True

    # 缓存/读写：失败按未命中/跳过。
    assert await cache.get_json("k") is None
    assert await cache.get_raw("k") is None
    assert await cache.set_json("k", {"a": 1}) is False
    assert await cache.delete("k") is False
    # 限流：失败 fail-open（计数 0 = 放行）。
    assert await cache.zadd_and_count("rate:b1", "m1") == 0
    # 幂等：失败视为“抢到”，交回 MySQL 唯一索引兜底。
    assert await cache.set_if_absent("idem", "order-1") is True
    # 健康检查失败但不抛。
    assert await cache.ping() is False
    # close 也不允许把异常冒到主链路。
    await cache.close()
    assert cache.enabled is False


# ----------------------------------------------------------------------
# disabled 行为
# ----------------------------------------------------------------------
async def test_disabled_when_url_empty():
    cache = RedisCache("")
    assert cache.enabled is False
    assert cache.client is None

    assert await cache.get_json("k") is None
    assert await cache.get_raw("k") is None
    assert await cache.set_json("k", "v") is False
    assert await cache.delete("k") is False
    # 限流放行、幂等让路给 MySQL。
    assert await cache.zadd_and_count("rate:b1", "m1") == 0
    assert await cache.set_if_absent("idem", "order-1") is True
    assert await cache.ping() is False
    await cache.close()


def test_disabled_when_init_fails(monkeypatch):
    def _boom(*args, **kwargs):
        raise ValueError("bad redis url")

    monkeypatch.setattr(redis_cache_module.aioredis, "from_url", _boom)
    cache = RedisCache("redis://localhost:6379/0")
    assert cache.enabled is False
    assert cache.client is None


def test_from_url_builds_enabled_client(monkeypatch, fake_client):
    captured: dict[str, object] = {}

    def _from_url(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return fake_client

    monkeypatch.setattr(redis_cache_module.aioredis, "from_url", _from_url)
    cache = RedisCache("redis://localhost:6379/0")
    assert cache.enabled is True
    assert cache.client is fake_client
    assert captured["url"] == "redis://localhost:6379/0"
    assert captured["kwargs"]["decode_responses"] is True


async def test_close_marks_disabled(cache):
    assert await cache.ping() is True
    await cache.close()
    assert cache.enabled is False
    assert await cache.ping() is False
    # 重复 close 幂等。
    await cache.close()

# -*- coding: utf-8 -*-
"""P9-2 语义缓存 Redis 热路径单测。

覆盖：
- Redis 精确 key 命中零 embedding；
- ``remember`` 同时写 Redis + Chroma（TTL 24h）；
- Chroma 命中回填 Redis；
- Redis disabled / 故障时完全回退原 Chroma 路径；
- buyer + scope 隔离、写操作 / 多轮问句不碰 Redis。

全部走 ``fakeredis`` 注入，不依赖真实 Redis。
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.infrastructure.cache.chroma_semantic_cache import SemanticCache
from app.infrastructure.redis_cache import RedisCache

_TTL_24H = 24 * 60 * 60


def _settings(tmp_path):
    """构造只关心语义缓存字段的 Settings。"""
    from app.infrastructure.settings import Settings
    return Settings(
        llm_base_url="", llm_api_key="", llm_model="", database_url="",
        data_dir=tmp_path, chroma_dir=tmp_path / "chroma",
        context_trigger_tokens=6000, context_keep_recent=8,
        semantic_cache_threshold=0.05, semantic_cache_bucket_limit=30,
        preference_top_k=5,
        reranker_enabled=True, reranker_model="BAAI/bge-reranker-base",
    )


class _FakeEmbedder:
    """确定性 embedder；``calls`` 用来断言是否真的调用了 embedding。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [1.0, 0.0, 0.0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0, 0.0] for _ in texts]


class _RecordingRedis:
    """包装 RedisCache，记录 get/set key，便于断言“零 Redis 访问”。"""

    def __init__(self, inner=None) -> None:
        self.inner = inner
        self.gets: list[str] = []
        self.sets: list[tuple] = []

    async def get_json(self, key, default=None):
        self.gets.append(key)
        if self.inner is None:
            return default
        return await self.inner.get_json(key, default)

    async def set_json(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        if self.inner is None:
            return False
        return await self.inner.set_json(key, value, ttl=ttl)


class _BoomRedis:
    """所有命令直接抛异常的假客户端（模拟 Redis 整体故障）。"""

    async def get_json(self, key, default=None):
        raise RuntimeError("boom")

    async def set_json(self, key, value, ttl=None):
        raise RuntimeError("boom")


@pytest.fixture
def fake_client():
    """decode_responses=True 的异步 fakeredis 客户端。"""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_cache(fake_client):
    """注入 fakeredis 的 enabled RedisCache。"""
    return RedisCache("", client=fake_client)


# ----------------------------------------------------------------------
# Redis 命中：零 embedding
# ----------------------------------------------------------------------
async def test_redis_hit_skips_embedder_and_writes_both(tmp_path, redis_cache, fake_client):
    embedder = _FakeEmbedder()
    recorder = _RecordingRedis(redis_cache)
    cache = SemanticCache(_settings(tmp_path), embedder, recorder)

    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)
    assert embedder.calls == 1
    # remember 同时写 Redis 与 Chroma。
    assert len(recorder.sets) == 1
    assert len(await fake_client.keys("sem:*")) == 1
    assert cache._collection.count() == 1

    # 归一化后一致（空白/句末标点不影响精确 key）→ 直接 Redis 命中，零 embedding。
    embedder.calls = 0
    hit = await cache.lookup("b1", " 推荐旅行三件套？ ", has_history=False)
    assert hit is not None and hit.reply == "这是缓存回复"
    assert hit.matched_query == "推荐旅行三件套"
    assert hit.similarity == 1.0
    assert embedder.calls == 0


async def test_remember_sets_24h_ttl(tmp_path, redis_cache, fake_client):
    cache = SemanticCache(_settings(tmp_path), _FakeEmbedder(), redis_cache)
    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)

    keys = await fake_client.keys("sem:*")
    assert len(keys) == 1
    ttl = await fake_client.ttl(keys[0])
    assert 0 < ttl <= _TTL_24H


async def test_corrupt_redis_value_falls_back_to_chroma(tmp_path, fake_client):
    """Redis value 结构非法（缺 reply）时按未命中继续走 Chroma。"""
    embedder = _FakeEmbedder()
    cache = SemanticCache(_settings(tmp_path), embedder, RedisCache("", client=fake_client))
    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)

    key = (await fake_client.keys("sem:*"))[0]
    await fake_client.set(key, '{"query": "推荐旅行三件套"}')

    embedder.calls = 0
    hit = await cache.lookup("b1", "推荐旅行三件套", has_history=False)
    assert hit is not None and hit.reply == "这是缓存回复"
    assert embedder.calls == 1
    # Chroma 命中后重新回填成合法 value。
    assert await fake_client.get(key) == '{"query": "推荐旅行三件套", "reply": "这是缓存回复"}'


# ----------------------------------------------------------------------
# Chroma 命中回填 Redis
# ----------------------------------------------------------------------
async def test_chroma_hit_backfills_redis(tmp_path, redis_cache, fake_client):
    embedder = _FakeEmbedder()
    recorder = _RecordingRedis(redis_cache)
    cache = SemanticCache(_settings(tmp_path), embedder, recorder)

    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)
    key = recorder.sets[0][0]
    await fake_client.delete(key)  # 模拟 Redis 过期 / 重启丢失

    embedder.calls = 0
    hit = await cache.lookup("b1", "推荐旅行三件套", has_history=False)
    assert hit is not None and hit.reply == "这是缓存回复"
    assert embedder.calls == 1                      # 只能走 Chroma，需要 embedding
    assert await fake_client.get(key) is not None   # 回填 Redis

    # 回填后再次查询走 Redis 精确命中，零 embedding。
    embedder.calls = 0
    hit2 = await cache.lookup("b1", "推荐旅行三件套", has_history=False)
    assert hit2 is not None and hit2.reply == "这是缓存回复"
    assert embedder.calls == 0


# ----------------------------------------------------------------------
# disabled / 故障回退
# ----------------------------------------------------------------------
async def test_disabled_redis_falls_back_to_chroma(tmp_path):
    """REDIS_URL 为空 → disabled，行为与 P8 的纯 Chroma 路径一致。"""
    embedder = _FakeEmbedder()
    disabled = RedisCache("")
    assert disabled.enabled is False
    cache = SemanticCache(_settings(tmp_path), embedder, disabled)

    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)
    hit = await cache.lookup("b1", "推荐旅行三件套", has_history=False)
    assert hit is not None and hit.reply == "这是缓存回复"
    assert embedder.calls == 2  # remember + lookup 各一次，全部走 Chroma


async def test_failing_redis_falls_back_to_chroma(tmp_path):
    """Redis 全部命令抛异常：写跳过、读按未命中，Chroma 仍然工作。"""
    embedder = _FakeEmbedder()
    cache = SemanticCache(
        _settings(tmp_path), embedder, RedisCache("redis://ignored:6379/0", client=_BoomRedis()),
    )

    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)
    assert embedder.calls == 1
    assert cache._collection.count() == 1  # Redis 写失败不影响 Chroma

    embedder.calls = 0
    hit = await cache.lookup("b1", "推荐旅行三件套", has_history=False)
    assert hit is not None and hit.reply == "这是缓存回复"
    assert embedder.calls == 1  # Redis 读失败 → Chroma


async def test_raw_redis_exception_never_escapes(tmp_path):
    """替身对象直接抛异常也不能冒泡到业务层（空 Chroma → None）。"""
    cache = SemanticCache(_settings(tmp_path), _FakeEmbedder(), _BoomRedis())
    assert await cache.lookup("b1", "推荐旅行三件套", has_history=False) is None
    # remember 也不允许抛。
    await cache.remember("b1", "推荐旅行三件套", "这是缓存回复", has_history=False)


# ----------------------------------------------------------------------
# buyer + scope 隔离
# ----------------------------------------------------------------------
async def test_scope_isolation_blocks_redis_and_chroma(tmp_path, redis_cache):
    embedder = _FakeEmbedder()
    cache = SemanticCache(_settings(tmp_path), embedder, redis_cache)

    await cache.remember("b1", "推荐旅行三件套", "scope-a 回复", has_history=False, scope="a" * 16)

    # 同 buyer 不同 scope：Redis key 不同 → miss；Chroma 过滤 scope 不同 → miss。
    embedder.calls = 0
    assert await cache.lookup("b1", "推荐旅行三件套", has_history=False, scope="b" * 16) is None
    assert embedder.calls == 1

    # 同 scope 不同 buyer：同样两个存储都不能串。
    embedder.calls = 0
    assert await cache.lookup("b2", "推荐旅行三件套", has_history=False, scope="a" * 16) is None
    assert embedder.calls == 1


async def test_distinct_buyer_scope_use_distinct_keys(tmp_path, fake_client):
    embedder = _FakeEmbedder()
    cache = SemanticCache(_settings(tmp_path), embedder, RedisCache("", client=fake_client))

    await cache.remember("b1", "推荐旅行三件套", "b1 回复", has_history=False, scope="s1")
    await cache.remember("b2", "推荐旅行三件套", "b2 回复", has_history=False, scope="s1")
    await cache.remember("b1", "推荐旅行三件套", "s2 回复", has_history=False, scope="s2")

    keys = sorted(await fake_client.keys("sem:*"))
    assert len(keys) == 3
    # key 里保留了 buyer 与 scope，便于排障。
    assert any("b1" in key and ":s1:" in key for key in keys)
    assert any("b2" in key and ":s1:" in key for key in keys)
    assert any("b1" in key and ":s2:" in key for key in keys)


# ----------------------------------------------------------------------
# 不安全问句 / 多轮问句不碰 Redis
# ----------------------------------------------------------------------
async def test_unsafe_query_never_touches_redis(tmp_path, redis_cache, fake_client):
    recorder = _RecordingRedis(redis_cache)
    cache = SemanticCache(_settings(tmp_path), _FakeEmbedder(), recorder)

    assert await cache.lookup("b1", "帮我下单", has_history=False) is None
    await cache.remember("b1", "帮我下单", "回复", has_history=False)

    assert recorder.gets == []
    assert recorder.sets == []
    assert await fake_client.keys("sem:*") == []


async def test_has_history_never_touches_redis(tmp_path, redis_cache):
    recorder = _RecordingRedis(redis_cache)
    cache = SemanticCache(_settings(tmp_path), _FakeEmbedder(), recorder)

    assert await cache.lookup("b1", "推荐旅行三件套", has_history=True) is None
    await cache.remember("b1", "推荐旅行三件套", "回复", has_history=True)
    assert recorder.gets == []
    assert recorder.sets == []


async def test_error_reply_never_writes_redis(tmp_path, redis_cache):
    recorder = _RecordingRedis(redis_cache)
    cache = SemanticCache(_settings(tmp_path), _FakeEmbedder(), recorder)

    await cache.remember("b1", "推荐旅行三件套", "[error] 失败", has_history=False)
    assert recorder.sets == []

# -*- coding: utf-8 -*-
"""P9-3 滑动窗口限流单测。

覆盖任务要求：
- 窗口内超过 ``rate_limit_burst`` 返回 429 + ``Retry-After``；
- 软阈值（per_minute ~ burst）放行但回 ``Retry-After`` 提示；
- 窗口按请求时间连续滑动（不是固定分钟桶）；
- Redis disabled / 异常 fail-open 放行；
- 只挂 ``/commerce/intents`` 与 ``/commerce/events``，``/health`` 永不限流。

限流依赖复用 ``RedisCache.zadd_and_count``，测试用 fakeredis / 桩对象注入，
不依赖真实 Redis。
"""
from __future__ import annotations

import time as time_module
from types import SimpleNamespace

import fakeredis.aioredis
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.infrastructure.redis_cache import RedisCache
from app.presentation.rate_limit import build_rate_limit_dependency
from app.presentation.server import create_app


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------
class _FakeSemanticCache:
    """不命中的语义缓存替身，让 intents 走到假 Agent。"""

    async def lookup(self, buyer_id, query, has_history, scope=""):
        return None

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        return None


class _FakePreferenceStore:
    async def list_by_buyer(self, buyer_id):
        return []


class _FakeBroker:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, session_id, event_type, payload):
        self.events.append((session_id, event_type, payload))


class _FakeAgent:
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, payload, config):
        self.calls += 1
        return {"messages": [AIMessage(content=self.reply)]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})


class _Container:
    """最小容器替身：满足 intents 路径，并暴露 settings / redis_cache。"""

    def __init__(self, *, settings, redis_cache=None, agent=None) -> None:
        self.settings = settings
        self.redis_cache = redis_cache
        self.agent = agent or _FakeAgent()
        self.semantic_cache = _FakeSemanticCache()
        self.preference_store = _FakePreferenceStore()
        self.broker = _FakeBroker()

    async def close(self) -> None:
        return None


class _StubCache:
    """固定计数桩：返回指定窗口计数，并记录被访问的 key。"""

    def __init__(self, count: int) -> None:
        self.count = count
        self.keys: list[str] = []

    async def zadd_and_count(self, key, member, window_seconds=60.0):
        self.keys.append(key)
        return self.count


class _RecordingCache:
    """记录 zadd 次数的桩，用于断言 /health 完全不碰限流。"""

    def __init__(self) -> None:
        self.calls = 0

    async def zadd_and_count(self, key, member, window_seconds=60.0):
        self.calls += 1
        return 1


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

    async def zrange(self, *args, **kwargs):
        raise RuntimeError("boom")

    async def aclose(self):
        return None


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
def _settings(**overrides) -> SimpleNamespace:
    values = {
        "rate_limit_per_minute": 30,
        "rate_limit_burst": 60,
        "rate_limit_scope": "buyer",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(container: _Container) -> TestClient:
    return TestClient(create_app(container=container, build_on_startup=False))


def _post_intent(client: TestClient, buyer_id: str = "buyer-1"):
    return client.post(
        "/commerce/intents",
        json={
            "shopping_session_id": "s-test",
            "buyer_id": buyer_id,
            "query": "推荐一个旅行三件套",
        },
    )


def _fake_redis() -> RedisCache:
    return RedisCache("", client=fakeredis.aioredis.FakeRedis(decode_responses=True))


# ----------------------------------------------------------------------
# 429 / Retry-After
# ----------------------------------------------------------------------
def test_burst_returns_429_with_retry_after():
    container = _Container(
        settings=_settings(rate_limit_per_minute=2, rate_limit_burst=3),
        redis_cache=_fake_redis(),
    )
    client = _client(container)

    for _ in range(3):
        assert _post_intent(client).status_code == 200

    response = _post_intent(client)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    # 被拒绝的请求不能进业务逻辑。
    assert container.agent.calls == 3


def test_soft_threshold_gets_retry_after_hint():
    container = _Container(
        settings=_settings(rate_limit_per_minute=1, rate_limit_burst=5),
        redis_cache=_fake_redis(),
    )
    client = _client(container)

    first = _post_intent(client)
    assert first.status_code == 200
    assert "Retry-After" not in first.headers

    second = _post_intent(client)
    assert second.status_code == 200
    assert int(second.headers["Retry-After"]) >= 1


def test_rate_limit_is_isolated_per_buyer():
    container = _Container(
        settings=_settings(rate_limit_per_minute=1, rate_limit_burst=1),
        redis_cache=_fake_redis(),
    )
    client = _client(container)

    assert _post_intent(client, buyer_id="buyer-1").status_code == 200
    assert _post_intent(client, buyer_id="buyer-1").status_code == 429
    # 另一个买家有自己的桶，不受影响。
    assert _post_intent(client, buyer_id="buyer-2").status_code == 200


# ----------------------------------------------------------------------
# 滑动窗口
# ----------------------------------------------------------------------
def test_window_slides_with_request_time(monkeypatch):
    clock = [1_700_000_000.0]
    monkeypatch.setattr(time_module, "time", lambda: clock[0])

    container = _Container(
        settings=_settings(rate_limit_per_minute=2, rate_limit_burst=3),
        redis_cache=_fake_redis(),
    )
    client = _client(container)

    for _ in range(3):
        assert _post_intent(client).status_code == 200
    assert _post_intent(client).status_code == 429

    # 只过了 30s：旧请求还在滑动窗口内，依旧拒绝（固定分钟桶则会误放行）。
    clock[0] += 30
    assert _post_intent(client).status_code == 429

    # 再过 31s（合计 61s）：最老的请求滑出窗口，恢复正常。
    clock[0] += 31
    assert _post_intent(client).status_code == 200


# ----------------------------------------------------------------------
# fail-open
# ----------------------------------------------------------------------
def test_redis_error_fails_open():
    container = _Container(
        settings=_settings(rate_limit_per_minute=1, rate_limit_burst=1),
        redis_cache=RedisCache("redis://ignored:6379/0", client=_BoomClient()),
    )
    client = _client(container)

    for _ in range(5):
        assert _post_intent(client).status_code == 200
    assert container.agent.calls == 5


def test_disabled_redis_fails_open():
    container = _Container(
        settings=_settings(rate_limit_per_minute=1, rate_limit_burst=1),
        redis_cache=RedisCache(""),
    )
    client = _client(container)

    for _ in range(5):
        assert _post_intent(client).status_code == 200
    assert container.agent.calls == 5


def test_missing_redis_cache_fails_open():
    container = _Container(
        settings=_settings(rate_limit_per_minute=1, rate_limit_burst=1),
        redis_cache=None,
    )
    client = _client(container)

    for _ in range(5):
        assert _post_intent(client).status_code == 200


# ----------------------------------------------------------------------
# 路由范围：/health 不限流；/commerce/events 限流
# ----------------------------------------------------------------------
def test_health_never_touches_rate_limiter():
    cache = _RecordingCache()
    container = _Container(
        settings=_settings(rate_limit_per_minute=1, rate_limit_burst=1),
        redis_cache=cache,
    )
    client = _client(container)

    for _ in range(10):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    assert cache.calls == 0


def test_events_endpoint_is_rate_limited():
    cache = _StubCache(count=99)
    container = _Container(
        settings=_settings(rate_limit_per_minute=30, rate_limit_burst=60),
        redis_cache=cache,
    )
    client = _client(container)

    response = client.get("/commerce/events", params={"shopping_session_id": "s1"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    # 没有 buyer_id 时按 session 维度限流。
    assert cache.keys == ["rate:v1:buyer:session%3As1"]


def test_build_rate_limit_dependency_blocks_over_burst():
    container = _Container(
        settings=_settings(rate_limit_per_minute=30, rate_limit_burst=60),
        redis_cache=_StubCache(count=99),
    )
    app = FastAPI()
    dependency = build_rate_limit_dependency(container)

    @app.get("/probe", dependencies=[Depends(dependency)])
    async def probe() -> dict:
        return {"ok": True}

    response = TestClient(app).get("/probe")
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1

# -*- coding: utf-8 -*-
"""P10-5 会话成本统计与 A/B 开关单测。

覆盖四层：

1. **纯函数**：token 估算 / 成本估算 / session_metrics 聚合 / 实验配置提取；
2. **采集器**：EventBroker 事件（token.delta / cache.hit）与 trace span 如何喂数据；
3. **API**：``POST /commerce/intents`` 每轮写一行 session_metrics、
   ``GET /commerce/metrics`` 聚合、``GET /commerce/dashboard`` 带实验配置；
4. **集成**：TEST_MYSQL_URL 存在时跑真实 MySQL session_metrics 写入 / 聚合
   （未设置则自动跳过）。
"""
from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.application.metrics.session_metrics import (
    MetricsStore,
    SessionMetrics,
    SessionMetricsCollector,
    aggregate_session_metrics,
    current_collector,
    estimate_cost,
    estimate_tokens,
    experiment_config_from_settings,
    reset_collector,
    set_collector,
)
from app.infrastructure.cache.chroma_semantic_cache import DisabledSemanticCache, SemanticHit
from app.infrastructure.eventbroker import EventBroker
from app.infrastructure.tracing import LocalTraceLogger

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class _FakeCache:
    """内存假语义缓存；hit 非空时模拟命中。"""

    def __init__(self, hit: SemanticHit | None = None) -> None:
        self.hit = hit
        self.lookups: list = []
        self.remembers: list = []

    async def lookup(self, buyer_id, query, has_history, scope=""):
        self.lookups.append((buyer_id, query, has_history, scope))
        return self.hit

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        self.remembers.append((buyer_id, query, reply, has_history, scope))


class _FakeAgent:
    """无 astream_events 的假 Agent：invoke_agent_stream 会降级回非流式。"""

    def __init__(self, reply: str = "测试回复", history: list | None = None) -> None:
        self.reply = reply
        self.history = list(history or [])
        self.calls: list = []

    async def ainvoke(self, payload, config):
        self.calls.append((payload, config))
        return {"messages": [AIMessage(content=self.reply)]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self.history)})


class _StreamingAgent(_FakeAgent):
    """假流式 Agent：产出 3 个 token delta。"""

    async def astream_events(self, payload, config, version="v2"):
        for token in ("你", "好", "呀"):
            yield {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content=token)}}
        self.history = [AIMessage(content=self.reply)]


class _MemoryMetricsStore(MetricsStore):
    """内存版 MetricsStore：API 测试用，不碰 MySQL。"""

    def __init__(self) -> None:
        self.rows: list[SessionMetrics] = []

    async def add(self, metrics: SessionMetrics) -> int:
        row = replace(metrics, id=len(self.rows) + 1)
        self.rows.append(row)
        return row.id

    async def list_by_session(self, session_id: str, limit: int = 100) -> list[SessionMetrics]:
        return [row for row in self.rows if row.session_id == session_id][:limit]

    async def list_all(self, limit: int | None = None) -> list[SessionMetrics]:
        return list(self.rows if limit is None else self.rows[:limit])


def _client(container) -> TestClient:
    from app.presentation.server import create_app

    return TestClient(create_app(container=container, build_on_startup=False))


def _intent_client(tmp_path, store, *, agent=None, cache=None, broker=None):
    container = SimpleNamespace(
        agent=agent or _FakeAgent(),
        semantic_cache=cache or _FakeCache(),
        broker=broker or EventBroker(),
        preference_store=None,
        metrics_store=store,
        settings=SimpleNamespace(data_dir=tmp_path),
    )
    return container, _client(container)


def _post_intent(client: TestClient, **overrides):
    payload = {
        "shopping_session_id": "s-metrics",
        "buyer_id": "buyer-1",
        "query": "推荐旅行三件套",
    }
    payload.update(overrides)
    return client.post("/commerce/intents", json=payload)


# --------------------------------------------------------------------------- #
# 1. 纯函数：成本 / token / 聚合 / 实验配置
# --------------------------------------------------------------------------- #
def test_estimate_tokens_matches_char_ratio():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(3) == 2          # ceil(3 / 1.5)
    assert estimate_tokens(100) == 67       # ceil(100 / 1.5)
    assert estimate_tokens(-10) == 0        # 负值夹到 0


def test_estimate_cost_is_token_price_based():
    assert estimate_cost(0, 0.002) == 0.0
    assert estimate_cost(1000, 0.002) == 0.002
    assert estimate_cost(500, 0.004) == 0.002
    assert estimate_cost(1234, 0.002) == round(1.234 * 0.002, 6)


def test_estimate_cost_reads_env_and_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PRICE_PER_1K_TOKENS", "0.01")
    assert estimate_cost(1000) == 0.01
    monkeypatch.setenv("LLM_PRICE_PER_1K_TOKENS", "not-a-number")
    # 非法单价回退默认值，指标旁路能力绝不抛异常。
    assert estimate_cost(1000) == 0.002


def test_session_metrics_validates_and_fills_created_at():
    metrics = SessionMetrics(
        session_id="s-1",
        turns=-1,
        llm_calls=-2,
        total_tokens=-3,
        cache_hits=-4,
        cost_estimate=-5.0,
    )
    # 负值无业务含义，统一夹到 0；created_at 自动补全。
    assert (metrics.turns, metrics.llm_calls, metrics.total_tokens) == (0, 0, 0)
    assert metrics.cache_hits == 0 and metrics.cost_estimate == 0.0
    assert metrics.created_at
    with pytest.raises(ValueError):
        SessionMetrics(session_id="")


def test_aggregate_session_metrics_sums_and_computes_rates():
    rows = [
        SessionMetrics(
            session_id="a", turns=1, llm_calls=2, total_tokens=100,
            cache_hits=0, cost_estimate=0.0002,
        ),
        SessionMetrics(
            session_id="a", turns=1, llm_calls=1, total_tokens=50,
            cache_hits=1, cost_estimate=0.0001,
        ),
        SessionMetrics(
            session_id="b", turns=1, llm_calls=0, total_tokens=20,
            cache_hits=0, cost_estimate=0.00004,
        ),
    ]
    agg = aggregate_session_metrics(rows)
    assert agg["sessions"] == 2
    assert agg["rows"] == 3
    assert agg["turns"] == 3
    assert agg["llm_calls"] == 3
    assert agg["total_tokens"] == 170
    assert agg["cache_hits"] == 1
    assert agg["cache_hit_rate"] == round(1 / 3, 4)
    assert agg["cost_estimate"] == round(0.00034, 6)
    assert agg["cost_currency"] == "USD"
    assert agg["avg_turns"] == 1.5


def test_aggregate_session_metrics_empty_is_all_zero():
    agg = aggregate_session_metrics([])
    assert agg["sessions"] == 0 and agg["rows"] == 0
    assert agg["turns"] == 0 and agg["total_tokens"] == 0
    assert agg["cache_hit_rate"] == 0.0 and agg["cost_estimate"] == 0.0


def test_experiment_config_from_settings_reads_toggles():
    settings = SimpleNamespace(
        semantic_cache_enabled=False,
        reranker_enabled=True,
        model_route_mode="direct",
    )
    assert experiment_config_from_settings(settings) == {
        "semantic_cache_enabled": False,
        "reranker_enabled": True,
        "model_route_mode": "direct",
    }


# --------------------------------------------------------------------------- #
# 2. 开关读取（Settings / .env.example）
# --------------------------------------------------------------------------- #
def test_settings_experiment_toggles_default_and_env(monkeypatch, tmp_path):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.delenv("SEMANTIC_CACHE_ENABLED", raising=False)
    monkeypatch.delenv("MODEL_ROUTE_MODE", raising=False)
    monkeypatch.delenv("RERANKER_ENABLED", raising=False)
    defaults = load_settings()
    assert defaults.semantic_cache_enabled is True
    assert defaults.model_route_mode == "llm"
    assert defaults.reranker_enabled is True

    monkeypatch.setenv("SEMANTIC_CACHE_ENABLED", "0")
    monkeypatch.setenv("MODEL_ROUTE_MODE", "DIRECT")
    monkeypatch.setenv("RERANKER_ENABLED", "0")
    toggled = load_settings()
    assert toggled.semantic_cache_enabled is False
    assert toggled.model_route_mode == "direct"   # 归一化小写
    assert toggled.reranker_enabled is False


def test_env_example_documents_experiment_toggles():
    source = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("SEMANTIC_CACHE_ENABLED", "RERANKER_ENABLED", "MODEL_ROUTE_MODE"):
        assert f"{key}=" in source, f".env.example 缺少 {key}"


async def test_disabled_semantic_cache_is_noop():
    cache = DisabledSemanticCache()
    assert await cache.lookup("b1", "推荐旅行三件套", has_history=False) is None
    assert await cache.remember("b1", "推荐旅行三件套", "回复", has_history=False) is None


# --------------------------------------------------------------------------- #
# 3. 采集器：事件 / span / build
# --------------------------------------------------------------------------- #
def test_collector_counts_llm_spans_only():
    collector = SessionMetricsCollector()
    for name in ("planner", "main_agent", "search_agent", "trade_agent", "compress", "product_search"):
        collector.record_span(name)
    assert collector.llm_calls == 5


def test_collector_build_prefers_stream_then_fallback():
    collector = SessionMetricsCollector()
    collector.record_span("planner")
    collector.record_span("main_agent")
    collector.record_span("rerank")  # 非 LLM span

    # 没有 token.delta 时用「问题 + 回复」字符数兜底。
    metrics = collector.build(
        session_id="s-1", trace_id="t-1", fallback_chars=30, price_per_1k_tokens=0.002,
    )
    assert metrics.llm_calls == 2
    assert metrics.total_tokens == estimate_tokens(30)
    assert metrics.cost_estimate == estimate_cost(estimate_tokens(30), 0.002)
    assert metrics.turns == 1

    # 有 token.delta 时以流式文本为准，忽略 fallback_chars。
    collector.record_event("token.delta", {"token": "abcd"})
    streamed = collector.build(session_id="s-1", fallback_chars=300)
    assert streamed.total_tokens == estimate_tokens(4)


def test_collector_records_cache_hit_and_ignores_other_events():
    collector = SessionMetricsCollector()
    collector.record_event("cache.hit", {"query": "q"})
    collector.record_event("final.result", {"reply": "r"})
    collector.record_event("token.delta", {"token": "你好"})
    assert collector.cache_hits == 1
    assert collector.stream_token_chars == 2


async def test_event_broker_feeds_current_collector():
    broker = EventBroker()
    collector = SessionMetricsCollector()
    token = set_collector(collector)
    try:
        broker.publish("s-1", "token.delta", {"token": "abc"})
        broker.publish("s-1", "cache.hit", {"query": "q"})
    finally:
        reset_collector(token)

    assert collector.stream_token_chars == 3
    assert collector.cache_hits == 1
    # 未设置采集器时 publish 不应报错（no-op）。
    broker.publish("s-1", "token.delta", {"token": "x"})


async def test_trace_span_feeds_llm_calls(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    collector = SessionMetricsCollector()
    token = set_collector(collector)
    try:
        async with logger.span("main_agent"):
            pass
        async with logger.span("product_search"):
            pass
    finally:
        reset_collector(token)

    assert collector.llm_calls == 1
    assert current_collector() is None


# --------------------------------------------------------------------------- #
# 4. API：每轮写一行 + 聚合 + Dashboard 实验配置
# --------------------------------------------------------------------------- #
def test_intents_writes_one_metrics_row_per_request(tmp_path):
    store = _MemoryMetricsStore()
    _, client = _intent_client(tmp_path, store, agent=_FakeAgent("这是回复"))

    response = _post_intent(client)
    assert response.status_code == 200
    assert len(store.rows) == 1

    row = store.rows[0]
    assert row.session_id == "s-metrics"
    assert row.turns == 1
    assert row.llm_calls == 0
    # 非流式路径用「问题 + 回复」字符数估算 token。
    expected_tokens = estimate_tokens(len("推荐旅行三件套") + len("这是回复"))
    assert row.total_tokens == expected_tokens
    assert row.cache_hits == 0
    assert row.cost_estimate == estimate_cost(expected_tokens)
    assert row.trace_id and len(row.trace_id) == 32
    assert row.created_at


def test_intents_streams_tokens_into_metrics(tmp_path):
    store = _MemoryMetricsStore()
    _, client = _intent_client(tmp_path, store, agent=_StreamingAgent("你好呀"))

    assert _post_intent(client).status_code == 200
    row = store.rows[0]
    # 3 个流式 token 字符 -> ceil(3 / 1.5) = 2 tokens。
    assert row.total_tokens == 2
    assert row.cache_hits == 0


def test_cache_hit_writes_metrics_with_cache_hits(tmp_path):
    store = _MemoryMetricsStore()
    hit = SemanticHit(reply="缓存回复", similarity=0.99, matched_query="推荐旅行三件套")
    cache = _FakeCache(hit)
    _, client = _intent_client(tmp_path, store, agent=_FakeAgent(), cache=cache)

    response = _post_intent(client)
    assert response.status_code == 200
    assert response.json()["reply"] == "缓存回复"
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row.cache_hits == 1
    assert row.llm_calls == 0


def test_metrics_endpoint_aggregates_and_filters_by_session(tmp_path):
    store = _MemoryMetricsStore()
    store.rows = [
        replace(SessionMetrics(session_id="a", turns=1, llm_calls=2, total_tokens=100,
                               cache_hits=0, cost_estimate=0.0002), id=1),
        replace(SessionMetrics(session_id="a", turns=1, llm_calls=1, total_tokens=50,
                               cache_hits=1, cost_estimate=0.0001), id=2),
        replace(SessionMetrics(session_id="b", turns=1, llm_calls=0, total_tokens=20,
                               cache_hits=0, cost_estimate=0.00004), id=3),
    ]
    container = SimpleNamespace(
        metrics_store=store,
        settings=SimpleNamespace(
            data_dir=tmp_path,
            semantic_cache_enabled=False,
            reranker_enabled=True,
            model_route_mode="direct",
        ),
    )
    client = _client(container)

    body = client.get("/commerce/metrics").json()
    assert body["sessions"] == 2
    assert body["turns"] == 3
    assert body["llm_calls"] == 3
    assert body["total_tokens"] == 170
    assert body["cache_hits"] == 1
    assert body["cost_estimate"] == round(0.00034, 6)
    assert body["experiment"] == {
        "semantic_cache_enabled": False,
        "reranker_enabled": True,
        "model_route_mode": "direct",
    }
    assert len(body["recent"]) == 3

    scoped = client.get("/commerce/metrics", params={"session_id": "a"}).json()
    assert scoped["sessions"] == 1
    assert scoped["rows"] == 2
    assert scoped["total_tokens"] == 150
    assert len(scoped["recent"]) == 2


def test_metrics_endpoint_returns_503_without_store():
    client = _client(SimpleNamespace(settings=None))
    response = client.get("/commerce/metrics")
    assert response.status_code == 503


def test_dashboard_returns_experiment_config(tmp_path):
    container = SimpleNamespace(
        feedback_store=None,
        engine=None,
        settings=SimpleNamespace(
            data_dir=tmp_path,
            semantic_cache_enabled=True,
            reranker_enabled=False,
            model_route_mode="llm",
        ),
    )
    client = _client(container)
    body = client.get("/commerce/dashboard").json()
    assert body["experiment"] == {
        "semantic_cache_enabled": True,
        "reranker_enabled": False,
        "model_route_mode": "llm",
    }


# --------------------------------------------------------------------------- #
# 5. 前端契约：Dashboard 展示实验配置 + 成本接口
# --------------------------------------------------------------------------- #
def test_frontend_exposes_experiment_config_and_metrics_api():
    api_source = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    dashboard_source = (FRONTEND / "src" / "Dashboard.tsx").read_text(encoding="utf-8")
    i18n_source = (FRONTEND / "src" / "i18n.ts").read_text(encoding="utf-8")

    assert "/commerce/metrics" in api_source
    assert "getSessionMetrics" in api_source
    assert "ExperimentConfig" in api_source

    assert "experiment-config" in dashboard_source
    assert "dashboard.experiment" in dashboard_source
    # 中英文两份字典都要有条目（每个 key 出现 2 次）。
    for key in (
        "dashboard.experiment",
        "dashboard.experimentHint",
        "dashboard.semanticCache",
        "dashboard.reranker",
        "dashboard.modelRoute",
        "dashboard.on",
        "dashboard.off",
    ):
        assert i18n_source.count(f'"{key}"') == 2, f"i18n 缺少中英对齐的 {key}"


# --------------------------------------------------------------------------- #
# 6. MySQL 集成（TEST_MYSQL_URL 未设置时跳过）
# --------------------------------------------------------------------------- #
pytestmark_mysql = pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"), reason="TEST_MYSQL_URL 未设置，跳过 MySQL 指标测试"
)


@pytestmark_mysql
async def test_mysql_metrics_store_write_and_aggregate():
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine
    from app.infrastructure.persistence.mysql_metrics_store import MySqlMetricsStore

    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    await bootstrap_schema(engine)
    store = MySqlMetricsStore(engine)
    session_id = f"s-mysql-metrics-{uuid.uuid4().hex[:8]}"
    first_id = second_id = 0
    try:
        first_id = await store.add(SessionMetrics(
            session_id=session_id,
            trace_id="trace-1",
            turns=1,
            llm_calls=2,
            total_tokens=120,
            cache_hits=0,
            cost_estimate=0.00024,
        ))
        assert first_id > 0

        rows = await store.list_by_session(session_id)
        assert len(rows) == 1
        assert rows[0].id == first_id
        assert rows[0].total_tokens == 120
        assert rows[0].trace_id == "trace-1"
        assert rows[0].created_at

        second_id = await store.add(SessionMetrics(
            session_id=session_id,
            turns=1,
            llm_calls=0,
            total_tokens=30,
            cache_hits=1,
            cost_estimate=0.00006,
        ))
        agg = await store.aggregate(session_id=session_id)
        assert agg["sessions"] == 1
        assert agg["rows"] == 2
        assert agg["turns"] == 2
        assert agg["llm_calls"] == 2
        assert agg["total_tokens"] == 150
        assert agg["cache_hits"] == 1
        assert agg["cost_estimate"] == 0.0003
    finally:
        for metrics_id in (first_id, second_id):
            if metrics_id:
                await store.delete(metrics_id)
        await engine.dispose()

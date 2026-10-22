# -*- coding: utf-8 -*-
"""P8：span JSONL 结构、可选 tracer 埋点与 latency report 聚合。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from app.application.agents.main_graph import build_main_graph, invoke_agent_stream
from app.application.agents.subagents import PlannerDecision, build_search_agent_graph
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.domain.catalog.ports.retrieval_ports import VectorHit
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.resilience import build_resilient_tool
from app.infrastructure.throttle import GatewayThrottle
from app.infrastructure.tracing import LocalTraceLogger
from scripts.latency_report import aggregate_spans, format_report, load_spans, percentile


class _RecordingSpan:
    def __init__(self, name: str, attrs: dict | None = None) -> None:
        self.name = name
        self.attrs = dict(attrs or {})
        self.status = "ok"


class RecordingTracer:
    """测试替身：记录进入顺序与结束状态，不落盘。"""

    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    @asynccontextmanager
    async def span(self, name: str, attrs: dict | None = None):
        record = _RecordingSpan(name, attrs)
        self.spans.append(record)
        try:
            yield record
        except BaseException:
            record.status = "error"
            raise

    def names(self) -> list[str]:
        return [span.name for span in self.spans]


def _context(trace_id: str = "trace-1", session_id: str = "s-1"):
    return ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id=session_id,
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id=trace_id,
        )
    )


def _span_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / "traces" / "s-1.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------------- #
# LocalTraceLogger.span：shape、parent/child、error
# --------------------------------------------------------------------------- #


async def test_span_writes_span_shape_with_duration_and_attrs(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    token = _context()
    try:
        async with logger.span("request", {"query": "旅行三件套"}) as span:
            await asyncio.sleep(0.01)
    finally:
        ShoppingContext.reset(token)

    line = _span_lines(tmp_path)[0]
    assert line["type"] == "span"
    assert line["trace_id"] == "trace-1"
    assert isinstance(line["span_id"], str) and line["span_id"]
    assert line["parent_span_id"] is None
    assert line["name"] == "request"
    assert line["status"] == "ok"
    # P11-3：span attrs 里的 query 文本同样脱敏，结构化字段（name/status/...）保留。
    assert line["attrs"] == {
        "query": {
            "sha256": hashlib.sha256("旅行三件套".encode("utf-8")).hexdigest()[:16],
            "length": 5,
        },
    }
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert line["span_id"] == span.span_id


async def test_span_records_parent_child_relationship(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    token = _context()
    try:
        async with logger.span("request"):
            async with logger.span("planner"):
                pass
            async with logger.span("main_agent"):
                pass
    finally:
        ShoppingContext.reset(token)

    by_name = {line["name"]: line for line in _span_lines(tmp_path)}
    request, planner, main_agent = by_name["request"], by_name["planner"], by_name["main_agent"]
    assert planner["parent_span_id"] == request["span_id"]
    assert main_agent["parent_span_id"] == request["span_id"]
    assert request["parent_span_id"] is None


async def test_span_marks_error_and_reraises(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    token = _context()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            async with logger.span("planner"):
                raise RuntimeError("boom")
    finally:
        ShoppingContext.reset(token)

    line = _span_lines(tmp_path)[0]
    assert line["status"] == "error"
    assert line["name"] == "planner"


# --------------------------------------------------------------------------- #
# latency report 聚合
# --------------------------------------------------------------------------- #


def test_percentile_uses_linear_interpolation_for_small_samples():
    assert percentile([100, 200, 300, 400], 50) == 250.0
    assert percentile([100, 200, 300, 400], 95) == 385.0
    assert percentile([100, 200, 300, 400], 99) == 397.0
    assert percentile([], 95) == 0.0


def test_load_spans_skips_event_and_malformed_lines(tmp_path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    path = trace_dir / "s1.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "token.delta", "payload": {"token": "x"}}),
                "{not-json}",
                json.dumps(
                    {
                        "type": "span",
                        "name": "request",
                        "duration_ms": 123.0,
                        "trace_id": "t1",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    spans = load_spans(trace_dir)

    assert len(spans) == 1
    assert spans[0]["name"] == "request"


def test_aggregate_spans_reports_request_ttft_and_per_name_stats():
    spans = [
        {"type": "span", "name": "request", "duration_ms": 100.0, "status": "ok"},
        {"type": "span", "name": "request", "duration_ms": 200.0, "status": "ok"},
        {"type": "span", "name": "request", "duration_ms": 300.0, "status": "ok"},
        {"type": "span", "name": "request", "duration_ms": 400.0, "status": "ok"},
        {"type": "span", "name": "ttft", "duration_ms": 50.0, "status": "ok"},
        {"type": "span", "name": "ttft", "duration_ms": 150.0, "status": "ok"},
        {"type": "span", "name": "planner", "duration_ms": 10.0, "status": "ok"},
        {"type": "span", "name": "planner", "duration_ms": 30.0, "status": "error"},
    ]

    summary = aggregate_spans(spans)

    assert summary["request"]["count"] == 4
    assert summary["request"]["p50"] == 250.0
    assert summary["request"]["p95"] == 385.0
    assert summary["request"]["p99"] == 397.0
    assert summary["ttft"]["count"] == 2
    assert summary["ttft"]["p50"] == 100.0
    assert summary["by_name"]["planner"]["count"] == 2
    assert summary["by_name"]["planner"]["avg"] == 20.0
    assert summary["by_name"]["planner"]["p95"] == 29.0
    assert summary["by_name"]["planner"]["errors"] == 1


def test_format_report_mentions_request_ttft_and_per_span_stats():
    spans = [
        {"type": "span", "name": "request", "duration_ms": 100.0, "status": "ok"},
        {"type": "span", "name": "ttft", "duration_ms": 50.0, "status": "ok"},
        {"type": "span", "name": "tool", "duration_ms": 20.0, "status": "error"},
    ]

    text = format_report(spans)

    assert "request" in text
    assert "ttft" in text
    assert "tool" in text
    assert "p50" in text
    assert "p95" in text
    assert "p99" in text
    assert "errors" in text


# --------------------------------------------------------------------------- #
# 可选 tracer 埋点
# --------------------------------------------------------------------------- #


async def test_gateway_throttle_records_queue_wait_span():
    tracer = RecordingTracer()
    throttle = GatewayThrottle(1, tracer=tracer)

    async with throttle.slot():
        pass

    assert tracer.names() == ["queue"]
    assert tracer.spans[0].attrs["max_concurrency"] == 1


async def test_catalog_search_records_embedding_vector_rerank_and_filter_spans():
    class _Embedder:
        async def embed(self, text):
            return [0.0]

        async def embed_batch(self, texts):
            return [[0.0] for _ in texts]

    class _Index:
        async def search(self, embedding, top_n):
            return [VectorHit(product_id="P1002", score=0.9), VectorHit(product_id="P1001", score=0.1)]

        async def ensure_ready(self, vector_dim):
            return None

        async def upsert_products(self, products, embeddings):
            return None

    class _Reranker:
        async def rerank(self, query, documents):
            return [5.0, 0.2]

    tracer = RecordingTracer()
    usecase = CatalogSearchUseCase(
        product_repo=InMemoryProductRepository(),
        embedder=_Embedder(),
        vector_index=_Index(),
        reranker=_Reranker(),
        tracer=tracer,
    )

    await usecase.execute(ProductSearchSpec(normalized_query="旅行三件套 抗造"))

    assert tracer.names()[:3] == ["embedding", "vector_recall", "rerank"]
    assert "filter" in tracer.names()


async def test_resilient_tool_records_tool_span_with_timeout_and_circuit_state():
    class _Args(BaseModel):
        payload: str = "x"

    async def _ok(**kwargs):
        return {"ok": True}

    tracer = RecordingTracer()
    base = StructuredTool.from_function(
        name="fake_tool",
        description="fake",
        args_schema=_Args,
        coroutine=_ok,
    )
    settings = SimpleNamespace(
        tool_timeout_seconds=15,
        tool_failure_threshold=3,
        tool_circuit_reset_seconds=60.0,
    )

    tool = build_resilient_tool(base, settings, None, tracer=tracer)
    result = await tool.ainvoke({"payload": "x"})

    assert result == {"ok": True}
    assert tracer.names() == ["tool"]
    assert tracer.spans[0].attrs["timeout_seconds"] == 15
    assert tracer.spans[0].attrs["circuit_state"] == "closed"


async def test_invoke_agent_stream_records_request_and_ttft_spans(tmp_path):
    class _Agent:
        async def astream_events(self, payload, config, version):
            assert version == "v2"
            yield {"event": "on_chain_start", "data": {}}
            await asyncio.sleep(0.01)
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": SimpleNamespace(content="你")},
            }
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": SimpleNamespace(content="好")},
            }

        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [AIMessage(content="你好")]})

    tracer = LocalTraceLogger(tmp_path)
    token = _context()
    try:
        reply = await invoke_agent_stream(_Agent(), "s-1", "hi", tracer=tracer)
    finally:
        ShoppingContext.reset(token)

    assert reply == "你好"
    lines = _span_lines(tmp_path)
    by_name = {line["name"]: line for line in lines}
    assert set(by_name) == {"request", "ttft"}
    assert by_name["ttft"]["parent_span_id"] == by_name["request"]["span_id"]
    assert by_name["ttft"]["duration_ms"] >= 0


async def test_build_main_graph_records_planner_compress_and_main_agent_spans():
    class _Planner:
        def with_structured_output(self, schema, method=None):
            return self

        async def ainvoke(self, messages):
            return PlannerDecision(route="direct", reasoning="direct", tasks=[])

    class _MainLlm:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="直接回答")

    tracer = RecordingTracer()
    graph = build_main_graph(
        SimpleNamespace(context_trigger_tokens=100_000, context_keep_recent=8),
        _MainLlm(),
        _Planner(),
        [],
        InMemorySaver(),
        tracer=tracer,
    )

    reply = await graph.ainvoke(
        {"messages": [HumanMessage(content="你好")]},
        config={"configurable": {"thread_id": "t1"}},
    )

    assert reply["messages"]
    names = tracer.names()
    assert "planner" in names
    assert "compress" in names
    assert "main_agent" in names


async def test_build_search_agent_graph_records_named_subagent_span():
    class _SearchLlm:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="检索完成")

    tracer = RecordingTracer()
    graph = build_search_agent_graph(_SearchLlm(), [], tracer=tracer)

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="父历史")],
            "subtask": {"agent": "search_agent", "instruction": "找旅行三件套"},
            "summary": "",
        }
    )

    assert tracer.names() == ["search_agent"]

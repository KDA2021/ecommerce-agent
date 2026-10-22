# -*- coding: utf-8 -*-
"""P7 WebSearch 单元测试：全部离线，不依赖真实 Tavily key。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.application.tools.web_search_tool import build_web_search_tool
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.websearch.tavily_search import TavilySearchClient


class _FakeBroker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, session_id: str, event_type: str, payload: dict) -> None:
        self.events.append((session_id, event_type, payload))


class _FakeSearchClient:
    def __init__(self, results=None, error=None) -> None:
        self.results = results or []
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, max_results: int = 3) -> list[dict]:
        self.calls.append((query, max_results))
        if self.error is not None:
            raise self.error
        return self.results


def _use_context(session_id: str = "s-web"):
    return ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id=session_id,
            buyer_id="buyer-web",
            locale="zh-CN",
            currency="CNY",
        )
    )


def test_build_web_search_tool_returns_none_without_client():
    assert build_web_search_tool(None) is None


async def test_web_search_tool_returns_results_and_publishes_invoke_and_result():
    results = [
        {"title": "T1", "url": "https://example.com/1", "snippet": "S1"},
    ]
    client = _FakeSearchClient(results=results)
    broker = _FakeBroker()
    tool = build_web_search_tool(client, broker=broker)
    assert tool is not None
    assert tool.name == "web_search_tool"

    token = _use_context()
    try:
        result = await tool.ainvoke({"query": "跨境 行李箱", "max_results": 2})
    finally:
        ShoppingContext.reset(token)

    assert client.calls == [("跨境 行李箱", 2)]
    assert result == {"results": results}
    assert broker.events == [
        (
            "s-web",
            "tool.invoke",
            {"tool": "web_search_tool", "args": {"query": "跨境 行李箱", "max_results": 2}},
        ),
        ("s-web", "tool.result", {"tool": "web_search_tool", "results": results}),
    ]


async def test_web_search_tool_defaults_max_results_to_three():
    client = _FakeSearchClient(results=[])
    tool = build_web_search_tool(client)
    assert tool is not None

    result = await tool.ainvoke({"query": "只传必填参数"})

    assert result == {"results": []}
    assert client.calls == [("只传必填参数", 3)]


async def test_web_search_tool_error_path_returns_error_and_publishes_result():
    client = _FakeSearchClient(error=RuntimeError("tavily down"))
    broker = _FakeBroker()
    tool = build_web_search_tool(client, broker=broker)
    assert tool is not None

    token = _use_context()
    try:
        result = await tool.ainvoke({"query": "会失败"})
    finally:
        ShoppingContext.reset(token)

    assert result == {"error": "tavily down"}
    assert broker.events == [
        (
            "s-web",
            "tool.invoke",
            {"tool": "web_search_tool", "args": {"query": "会失败", "max_results": 3}},
        ),
        ("s-web", "tool.result", {"tool": "web_search_tool", "error": "tavily down"}),
    ]


async def test_tavily_client_posts_expected_payload_and_maps_results():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T1", "url": "https://example.com/1", "content": "C1"},
                    {"title": "T2", "url": "https://example.com/2", "snippet": "S2"},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = TavilySearchClient("tvly-test", client=http_client)
        results = await client.search("旅行箱", max_results=2)

    assert captured == {
        "method": "POST",
        "url": "https://api.tavily.com/search",
        "payload": {"api_key": "tvly-test", "query": "旅行箱", "max_results": 2},
    }
    assert results == [
        {"title": "T1", "url": "https://example.com/1", "snippet": "C1"},
        {"title": "T2", "url": "https://example.com/2", "snippet": "S2"},
    ]


async def test_tavily_client_raises_non_2xx_with_status_in_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = TavilySearchClient("tvly-test", client=http_client)
        with pytest.raises(Exception) as exc_info:
            await client.search("旅行箱")

    assert "429" in str(exc_info.value)

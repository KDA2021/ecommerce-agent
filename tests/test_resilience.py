# -*- coding: utf-8 -*-
"""P4 工具韧性 + LLM 闸门测试：超时 / 熔断三态 / 成功重置 / 并发上限。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.infrastructure.resilience import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    build_resilient_tool,
)
from app.infrastructure.throttle import GatewayThrottle


class _FakeArgs(BaseModel):
    payload: str = "x"


class _Broker:
    """记录 publish_current 事件的测试替身。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish_current(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


def _settings(**overrides):
    base = {
        "tool_timeout_seconds": 15,
        "tool_failure_threshold": 3,
        "tool_circuit_reset_seconds": 60.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_tool(coroutine):
    return StructuredTool.from_function(
        name="fake_tool",
        description="fake tool for resilience tests",
        args_schema=_FakeArgs,
        coroutine=coroutine,
    )


async def test_tool_timeout_returns_error_and_publishes_event():
    broker = _Broker()

    async def slow_tool(**kwargs):
        await asyncio.sleep(0.2)
        return {"ok": True}

    tool = build_resilient_tool(
        _make_tool(slow_tool), _settings(tool_timeout_seconds=0.01), broker
    )

    result = await tool.ainvoke({"payload": "x"})

    assert result == {"error": "工具执行超时（>0.01s）"}
    assert "tool.timeout" in broker.types()


def test_three_consecutive_failures_open_breaker():
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60.0)

    for _ in range(3):
        assert breaker.allow() is True
        breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.allow() is False


def test_success_after_failures_resets_breaker():
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=60.0)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()

    assert breaker.failures == 0
    assert breaker.state == "closed"
    assert breaker.allow() is True

    # 重置后需要重新累计满阈值才会再次熔断
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


async def test_open_breaker_returns_error_without_calling_tool():
    broker = _Broker()
    calls = {"count": 0}

    async def failing_tool(**kwargs):
        calls["count"] += 1
        raise ValueError("boom")

    tool = build_resilient_tool(_make_tool(failing_tool), _settings(), broker)

    for _ in range(3):
        assert await tool.ainvoke({"payload": "x"}) == {"error": "boom"}
    assert calls["count"] == 3
    assert broker.types() == ["tool.error", "tool.error", "tool.error"]

    # 第四次调用应直接命中熔断，不再触碰底层工具
    assert await tool.ainvoke({"payload": "x"}) == {"error": "工具已熔断，请稍后重试"}
    assert calls["count"] == 3
    assert "circuit.open" in broker.types()


async def test_breaker_allows_half_open_probe_after_reset_window():
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=0.01)

    for _ in range(3):
        breaker.record_failure()
    assert breaker.allow() is False

    await asyncio.sleep(0.02)
    assert breaker.allow() is True
    assert breaker.state == "half_open"

    breaker.record_success()
    assert breaker.state == "closed"


async def test_registry_shares_one_breaker_per_tool_name():
    registry = CircuitBreakerRegistry(_settings())

    first = registry.breaker("product_search")
    second = registry.breaker("product_search")
    other = registry.breaker("query_order")

    assert first is second
    assert first is not other


async def test_gateway_throttle_limits_concurrency():
    throttle = GatewayThrottle(2)
    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with throttle.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(6)))

    assert peak == 2

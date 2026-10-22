# -*- coding: utf-8 -*-
"""P4 工具韧性：超时 + 熔断，包装成 LangChain StructuredTool。"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from langchain_core.tools import StructuredTool

from app.infrastructure.tracing import traced_span


class CircuitBreaker:
    """单个工具的熔断器：closed → open → half_open。

    连续失败达到阈值即熔断；熔断期间直接拒绝调用；超过冷却窗口后放行一次探测
    （half_open），探测成功恢复 closed，探测失败重新 open。
    """

    state = "closed"     # 状态机当前态：closed / open / half_open
    failures = 0         # 连续失败计数
    opened_at = None     # 进入 open 的时间戳，用于冷却判断

    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 60.0) -> None:
        """初始化阈值、冷却窗口与 closed 状态。

        Args:
            failure_threshold: 连续失败熔断阈值。
            reset_seconds: 熔断冷却秒数。
        """
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.state = "closed"
        self.failures = 0
        self.opened_at: Optional[float] = None

    def allow(self) -> bool:
        """判断本次调用是否放行；open 且未过冷却窗口则拒绝。

        Returns:
            是否放行。
        """
        if self.state == "closed":
            return True
        if self.state == "open":
            if self.opened_at is not None and time.time() - self.opened_at >= self.reset_seconds:
                # 冷却期已过：放行一次探测并转入 half_open。
                self.state = "half_open"
                return True
            # 冷却期内：直接拒绝，避免持续打到故障下游。
            return False
        # half_open：放行一次探测请求
        return True

    def record_success(self) -> None:
        """记录成功，重置失败计数并恢复 closed。"""
        self.failures = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self) -> None:
        """记录失败；达阈值或 half_open 探测失败则转 open。"""
        self.failures += 1
        # half_open 探测失败立即重新 open；closed 下需累计到阈值才熔断。
        if self.state == "half_open" or self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.time()


class CircuitBreakerRegistry:
    """按工具名维护独立的熔断器，同一工具共享同一条熔断状态。"""

    def __init__(
        self,
        settings=None,
        *,
        failure_threshold: int = 3,
        reset_seconds: float = 60.0,
    ) -> None:
        """可用 settings 覆盖阈值与冷却窗口。

        Args:
            settings: 可选运行时配置。
            failure_threshold: 默认熔断阈值。
            reset_seconds: 默认冷却秒数。
        """
        if settings is not None:
            failure_threshold = getattr(settings, "tool_failure_threshold", failure_threshold)
            reset_seconds = getattr(settings, "tool_circuit_reset_seconds", reset_seconds)
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, name: str) -> CircuitBreaker:
        """按工具名取（或创建）熔断器。

        Args:
            name: 工具名。

        Returns:
            CircuitBreaker。
        """
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                failure_threshold=self._failure_threshold,
                reset_seconds=self._reset_seconds,
            )
        return self._breakers[name]


def _mark_tool_span(span, breaker: CircuitBreaker, result) -> None:
    """把最终熔断状态 / error 写回 span；span 为 None 或替身时静默跳过。"""
    if span is None:
        return
    attrs = getattr(span, "attrs", None)
    if isinstance(attrs, dict):
        attrs["circuit_state"] = breaker.state
    if isinstance(result, dict) and "error" in result:
        setter = getattr(span, "set_status", None)
        if callable(setter):
            setter("error")
        if isinstance(attrs, dict):
            attrs.setdefault("error", result.get("error"))


def build_resilient_tool(
    base_tool,
    settings,
    broker,
    registry=None,
    tracer=None,
    *,
    timeout_seconds=None,
):
    """把业务工具包装成带超时与熔断的 StructuredTool。

    - 超时：``asyncio.wait_for``，超时记一次失败并返回错误 dict；
    - 熔断打开：直接返回错误 dict，不再调用底层工具；
    - 成功：重置失败计数；
    - 事件：仅在 broker 非 None 时发布。

    Args:
        base_tool: 原始 StructuredTool。
        settings: 运行时配置。
        broker: 事件总线，可为 None。
        registry: 可选熔断注册表。
        tracer: 可选 LocalTraceLogger / span tracer。
        timeout_seconds: 可选本工具的超时覆盖；缺省读
            ``settings.tool_timeout_seconds``。下单确认需要挂起等待买家操作
            （可能远长于普通工具超时），由 bootstrap 显式传
            ``order_confirm_timeout + buffer``。
    """
    if registry is None:
        registry = CircuitBreakerRegistry(settings)
    breaker = registry.breaker(base_tool.name)
    if timeout_seconds is None:
        timeout_seconds = getattr(settings, "tool_timeout_seconds", 30)

    def _publish(event_type: str, payload: dict) -> None:
        """仅在有 broker 时发布事件。

        Args:
            event_type: 事件类型。
            payload: 事件载荷。
        """
        if broker is not None:
            broker.publish_current(event_type, payload)

    async def _invoke(**kwargs):
        """执行带熔断 / 超时的底层工具，返回业务结果或 error dict。"""
        if not breaker.allow():
            # 熔断打开：不调用底层工具，直接返回错误并发布事件。
            _publish(
                "circuit.open",
                {"tool": base_tool.name, "cooldown_seconds": breaker.reset_seconds},
            )
            return {"error": "工具已熔断，请稍后重试"}
        try:
            result = await asyncio.wait_for(
                base_tool.ainvoke(kwargs), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            breaker.record_failure()
            _publish(
                "tool.timeout",
                {"tool": base_tool.name, "timeout_seconds": timeout_seconds},
            )
            return {"error": f"工具执行超时（>{timeout_seconds}s）"}
        except Exception as err:  # noqa: BLE001 —— 工具失败不阻断 Agent，返回 error dict
            breaker.record_failure()
            _publish("tool.error", {"tool": base_tool.name, "error": str(err)})
            return {"error": str(err)}
        breaker.record_success()
        return result

    async def guarded(**kwargs):
        """熔断 / 超时 / 异常统一转成 error dict，成功则重置失败计数。

        Args:
            **kwargs: 底层工具的调用参数。

        Returns:
            工具结果；熔断 / 超时 / 异常时为 ``{'error': ...}``。
        """
        attrs = {
            "tool": base_tool.name,
            "timeout_seconds": timeout_seconds,
            "circuit_state": breaker.state,
        }
        async with traced_span(tracer, "tool", attrs) as span:
            result = await _invoke(**kwargs)
            _mark_tool_span(span, breaker, result)
            return result

    return StructuredTool.from_function(
        name=base_tool.name,
        description=base_tool.description,
        args_schema=base_tool.args_schema,
        coroutine=guarded,
    )

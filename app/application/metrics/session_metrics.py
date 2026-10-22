# -*- coding: utf-8 -*-
"""P10-5 会话成本 / 用量指标：session_metrics 值对象、成本估算与进程内采集器。

设计口径（全部来自真实运行数据，不做假数）：

- **turns**：一次 ``/commerce/intents`` 请求记 1 轮；
- **llm_calls**：本轮产生 planner / compress / main_agent / search_agent / trade_agent
  span 的数量（每个 LLM 节点一个 span）；
- **total_tokens**：优先用流式 ``token.delta`` 的字符数按 ``1 token ≈ 1.5 字符`` 估算；
  非流式路径退回「问题 + 回复」字符数估算，只做量级参考；
- **cache_hits**：本轮语义缓存命中次数（0 或 1）；
- **cost_estimate**：``total_tokens / 1000 * 单价``，单价可用
  ``LLM_PRICE_PER_1K_TOKENS`` 覆盖，默认 0.002 USD / 1K tokens。

采集方式：请求开始时把 ``SessionMetricsCollector`` 放进 ContextVar，EventBroker
发布 ``token.delta`` / ``cache.hit`` 时通知它，LocalTraceLogger 记录 LLM span 时
通知它；请求结束时 ``build()`` 出一个 ``SessionMetrics`` 落库。ContextVar 天然按
asyncio 任务隔离，多用户并发不会串台。
"""
from __future__ import annotations

import math
import os
from abc import ABC, abstractmethod
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

#: 默认模型单价（USD / 1K tokens），可用环境变量覆盖；只用于成本量级估计。
DEFAULT_PRICE_PER_1K_TOKENS = 0.002

#: 中英混合文本的经验换算：1 token ≈ 1.5 字符。
CHARS_PER_TOKEN = 1.5

#: 判定为「LLM 调用」的 span 名称集合（与主图 / 子图节点名对应）。
LLM_SPAN_NAMES = frozenset({"planner", "main_agent", "compress", "search_agent", "trade_agent"})

#: 成本币种（单价按 USD 计）。
COST_CURRENCY = "USD"

#: 默认每会话聚合时最多扫描多少行，避免 Dashboard 拖垮数据库。
DEFAULT_AGGREGATE_LIMIT = 5000


def estimate_tokens(text_chars: int) -> int:
    """按字符数估算 token 数（中英混合经验值）。

    Args:
        text_chars: 文本字符数。

    Returns:
        估算 token 数（非负整数）。
    """
    return max(int(math.ceil(max(int(text_chars), 0) / CHARS_PER_TOKEN)), 0)


def estimate_cost(tokens: int, price_per_1k_tokens: float | None = None) -> float:
    """按每 1K token 单价估算成本（纯函数）。

    单价来源优先级：显式参数 > 环境变量 ``LLM_PRICE_PER_1K_TOKENS`` > 默认值。
    环境变量非法时回退默认值，绝不抛异常（指标是旁路能力）。

    Args:
        tokens: token 数。
        price_per_1k_tokens: 可选单价覆盖。

    Returns:
        估算成本（保留 6 位小数）。
    """
    price = DEFAULT_PRICE_PER_1K_TOKENS
    if price_per_1k_tokens is not None:
        price = float(price_per_1k_tokens)
    else:
        raw = os.getenv("LLM_PRICE_PER_1K_TOKENS", "").strip()
        if raw:
            try:
                price = float(raw)
            except ValueError:
                price = DEFAULT_PRICE_PER_1K_TOKENS
    return round(max(int(tokens), 0) / 1000.0 * price, 6)


@dataclass(frozen=True)
class SessionMetrics:
    """一轮请求结束时的成本 / 用量快照。"""

    session_id: str
    turns: int = 1
    llm_calls: int = 0
    total_tokens: int = 0
    cache_hits: int = 0
    cost_estimate: float = 0.0
    trace_id: str = ""
    created_at: str = ""
    id: int = 0

    def __post_init__(self) -> None:
        """校验并归一化字段，缺省时补创建时间。

        Raises:
            ValueError: session_id 为空。
        """
        if not self.session_id or not str(self.session_id).strip():
            raise ValueError("SessionMetrics.session_id required")
        # 负值没有业务含义，统一夹到 0，避免脏数据污染聚合。
        object.__setattr__(self, "turns", max(int(self.turns), 0))
        object.__setattr__(self, "llm_calls", max(int(self.llm_calls), 0))
        object.__setattr__(self, "total_tokens", max(int(self.total_tokens), 0))
        object.__setattr__(self, "cache_hits", max(int(self.cache_hits), 0))
        object.__setattr__(self, "cost_estimate", max(float(self.cost_estimate), 0.0))
        if not self.created_at:
            object.__setattr__(
                self,
                "created_at",
                datetime.now(timezone.utc).isoformat(),
            )

    def to_dict(self) -> dict:
        """序列化成 API 响应 dict。

        Returns:
            含全部指标字段的 dict。
        """
        return {
            "id": self.id,
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "turns": self.turns,
            "llm_calls": self.llm_calls,
            "total_tokens": self.total_tokens,
            "cache_hits": self.cache_hits,
            "cost_estimate": self.cost_estimate,
            "created_at": self.created_at,
        }


def aggregate_session_metrics(rows: list[SessionMetrics]) -> dict:
    """把 session_metrics 行聚合成 Dashboard 指标（纯函数）。

    Args:
        rows: ``SessionMetrics`` 列表（或带同名属性的测试替身）。

    Returns:
        聚合 dict：会话数 / 行数 / 轮数 / LLM 调用 / token / 缓存命中 / 成本。
    """
    normalized = list(rows or [])
    sessions = {
        str(getattr(row, "session_id", "") or "")
        for row in normalized
        if getattr(row, "session_id", "")
    }
    turns = sum(int(getattr(row, "turns", 0) or 0) for row in normalized)
    llm_calls = sum(int(getattr(row, "llm_calls", 0) or 0) for row in normalized)
    total_tokens = sum(int(getattr(row, "total_tokens", 0) or 0) for row in normalized)
    cache_hits = sum(int(getattr(row, "cache_hits", 0) or 0) for row in normalized)
    cost = sum(float(getattr(row, "cost_estimate", 0.0) or 0.0) for row in normalized)
    session_count = len(sessions)
    return {
        "sessions": session_count,
        "rows": len(normalized),
        "turns": turns,
        "llm_calls": llm_calls,
        "total_tokens": total_tokens,
        "cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / turns, 4) if turns else 0.0,
        "cost_estimate": round(cost, 6),
        "cost_currency": COST_CURRENCY,
        "avg_turns": round(turns / session_count, 2) if session_count else 0.0,
        "avg_tokens_per_turn": round(total_tokens / turns, 2) if turns else 0.0,
        "avg_cost_per_turn": round(cost / turns, 6) if turns else 0.0,
    }


def experiment_config_from_settings(settings) -> dict:
    """从 settings 提取当前 A/B 实验配置（纯函数）。

    Args:
        settings: Settings 或任何带同名字段的测试替身。

    Returns:
        ``{semantic_cache_enabled, reranker_enabled, model_route_mode}``。
    """
    return {
        "semantic_cache_enabled": bool(getattr(settings, "semantic_cache_enabled", True)),
        "reranker_enabled": bool(getattr(settings, "reranker_enabled", True)),
        "model_route_mode": str(getattr(settings, "model_route_mode", "llm") or "llm"),
    }


class MetricsStore(ABC):
    """session_metrics 持久化端口。"""

    @abstractmethod
    async def add(self, metrics: SessionMetrics) -> int:
        """写入一行指标，返回自增 id。

        Args:
            metrics: 指标值对象。

        Returns:
            新记录 id。
        """

    @abstractmethod
    async def list_by_session(self, session_id: str, limit: int = 100) -> list[SessionMetrics]:
        """按会话 id 查询指标（自增 id 正序）。

        Args:
            session_id: 会话 id。
            limit: 最多返回条数。

        Returns:
            指标列表。
        """

    @abstractmethod
    async def list_all(self, limit: int | None = None) -> list[SessionMetrics]:
        """查询全部指标（自增 id 正序）。

        Args:
            limit: 可选条数上限。

        Returns:
            指标列表。
        """

    async def aggregate(
        self,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """聚合指标；缺省聚合最近 ``limit`` 行，避免全表扫描。

        Args:
            session_id: 可选会话过滤。
            limit: 最多扫描行数。

        Returns:
            聚合 dict（见 ``aggregate_session_metrics``）。
        """
        scan_limit = int(limit) if limit else DEFAULT_AGGREGATE_LIMIT
        if session_id:
            rows = await self.list_by_session(session_id, limit=scan_limit)
        else:
            rows = await self.list_all(limit=scan_limit)
        return aggregate_session_metrics(rows)


class SessionMetricsCollector:
    """一次请求内的指标累加器（由 ContextVar 绑定到当前 asyncio 任务）。"""

    def __init__(self) -> None:
        """初始化零值累加器。"""
        self.turns = 0
        self.llm_calls = 0
        self.cache_hits = 0
        self.stream_token_chars = 0
        self._saw_token_delta = False

    def record_event(self, event_type: str, payload: object = None) -> None:
        """记录一条 EventBroker 事件。

        只关心两类事件：``token.delta``（累计流式 token 文本）与 ``cache.hit``。

        Args:
            event_type: 事件名。
            payload: 事件载荷（可能不是 dict）。
        """
        if event_type == "cache.hit":
            self.cache_hits += 1
        elif event_type == "token.delta":
            token = payload.get("token", "") if isinstance(payload, dict) else ""
            self.stream_token_chars += len(str(token or ""))
            self._saw_token_delta = True

    def record_span(self, name: str) -> None:
        """记录一个 span；LLM 节点名计入 llm_calls。

        Args:
            name: span 名称。
        """
        if name in LLM_SPAN_NAMES:
            self.llm_calls += 1

    def build(
        self,
        *,
        session_id: str,
        trace_id: str = "",
        turns: int | None = None,
        fallback_chars: int = 0,
        price_per_1k_tokens: float | None = None,
    ) -> SessionMetrics:
        """产出本轮 ``SessionMetrics``。

        Args:
            session_id: 会话 id。
            trace_id: 请求级 trace id。
            turns: 覆盖本轮轮数；缺省用累加值，再缺省 1。
            fallback_chars: 无流式 token 时用于估算的字符数（问题 + 回复）。
            price_per_1k_tokens: 可选单价覆盖。

        Returns:
            SessionMetrics。
        """
        # 有流式 token 就按流式文本估算；没有则退回问题 + 回复的字符数。
        token_chars = self.stream_token_chars if self._saw_token_delta else max(int(fallback_chars), 0)
        tokens = estimate_tokens(token_chars)
        return SessionMetrics(
            session_id=session_id,
            trace_id=trace_id or "",
            turns=1 if turns is None else int(turns),
            llm_calls=self.llm_calls,
            total_tokens=tokens,
            cache_hits=self.cache_hits,
            cost_estimate=estimate_cost(tokens, price_per_1k_tokens),
        )


_current_collector: ContextVar[Optional[SessionMetricsCollector]] = ContextVar(
    "session_metrics_collector",
    default=None,
)


def set_collector(collector: SessionMetricsCollector):
    """把采集器设为当前任务的采集器。

    Args:
        collector: 采集器。

    Returns:
        ContextVar token，供 ``reset_collector`` 恢复。
    """
    return _current_collector.set(collector)


def reset_collector(token) -> None:
    """恢复 ``set_collector`` 之前的采集器。

    Args:
        token: ``set_collector`` 返回的 token。
    """
    _current_collector.reset(token)


def current_collector() -> Optional[SessionMetricsCollector]:
    """读取当前任务的采集器；未设置时返回 None。

    Returns:
        采集器或 None。
    """
    return _current_collector.get()


def record_event(event_type: str, payload: object = None) -> None:
    """把事件转发给当前采集器；没有采集器时是零开销 no-op。

    Args:
        event_type: 事件名。
        payload: 事件载荷。
    """
    collector = _current_collector.get()
    if collector is not None:
        collector.record_event(event_type, payload)


def record_span(name: str) -> None:
    """把 span 名转发给当前采集器；没有采集器时是零开销 no-op。

    Args:
        name: span 名称。
    """
    collector = _current_collector.get()
    if collector is not None:
        collector.record_span(name)

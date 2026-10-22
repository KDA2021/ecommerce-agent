# -*- coding: utf-8 -*-
"""P10-1 产品指标 Dashboard 聚合 API（``GET /commerce/dashboard``）。

指标口径（全部来自真实数据，不做假数）：

- **会话数 / 轮数**：扫描 ``{data_dir}/traces/*.jsonl``；每个文件是一个会话，
  每个 ``final.result`` 事件代表一轮完成；
- **点赞 / 点踩 / 原因分布**：聚合 ``user_feedback``（MySQL，经 FeedbackStore 端口）；
- **平均轮数**：总轮数 / 会话数；
- **完成率**：至少有一轮 ``final.result`` 的会话 / 总会话数；
- **TTFT**：平均 ``span(name="ttft").duration_ms``（由 ``invoke_agent_stream`` 落 trace）；
- **缓存命中**：``cache.hit`` 事件数，命中率 = 命中数 / 轮数；
- **下单成功**：``orders.status == CONFIRMED`` 的订单数（真实数据库）；
- **成本估计**：按 trace 中 ``token.delta`` 的文本长度估算 token，再乘模型单价；
  只做量级参考，不要求精确（P10-5 会落 session_metrics 精确统计）。
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.metrics.session_metrics import (
    DEFAULT_PRICE_PER_1K_TOKENS,
    LLM_SPAN_NAMES,
    estimate_cost,
    estimate_tokens,
    experiment_config_from_settings,
)
from app.domain.feedback.feedback import REASON_LABELS
from app.domain.order.order import OrderStatus
from app.infrastructure.persistence.tables import OrderRow

#: 「完成一轮」的 trace 事件名。
FINAL_EVENT = "final.result"


def _blank_metrics() -> dict:
    """返回一份零值指标骨架，保证前端字段稳定。

    Returns:
        指标 dict。
    """
    return {
        "sessions": 0,
        "turns": 0,
        "avg_turns": 0.0,
        "completion_rate": 0.0,
        "likes": 0,
        "dislikes": 0,
        "reason_distribution": {code: 0 for code in REASON_LABELS},
        "ttft_ms": 0.0,
        "ttft_samples": 0,
        "cache_hits": 0,
        "cache_hit_rate": 0.0,
        "total_tokens_estimate": 0,
        "cost_estimate": 0.0,
        "cost_currency": "USD",
        "price_per_1k_tokens": DEFAULT_PRICE_PER_1K_TOKENS,
        "order_success": 0,
        "trace_sessions": 0,
        "errors": 0,
        "llm_calls": 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _iter_trace_files(trace_dir: Path):
    """遍历 trace 目录下的 JSONL 文件（不存在时返回空）。

    Args:
        trace_dir: traces 目录。

    Yields:
        Path。
    """
    if not trace_dir.is_dir():
        return
    yield from sorted(trace_dir.glob("*.jsonl"))


def summarize_traces(trace_dir: str | Path) -> dict:
    """扫描 trace JSONL，汇总会话 / 轮数 / TTFT / 缓存命中 / token 估算。

    Args:
        trace_dir: ``{data_dir}/traces`` 目录。

    Returns:
        原始聚合 dict（不含反馈与订单）。
    """
    trace_dir = Path(trace_dir)
    session_ids: set[str] = set()
    completed_sessions: set[str] = set()
    turns = 0
    cache_hits = 0
    errors = 0
    llm_calls = 0
    token_chars = 0
    ttft_samples: list[float] = []

    for path in _iter_trace_files(trace_dir):
        session_id = path.stem
        session_ids.add(session_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(line, dict):
                continue
            event_type = str(line.get("type", ""))
            payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
            if event_type == FINAL_EVENT:
                turns += 1
                completed_sessions.add(session_id)
            elif event_type == "cache.hit":
                cache_hits += 1
            elif event_type == "error":
                errors += 1
            elif event_type == "token.delta":
                token_chars += len(str(payload.get("token", "")))
            elif event_type == "span":
                name = str(line.get("name", ""))
                if name == "ttft":
                    try:
                        ttft_samples.append(float(line.get("duration_ms", 0.0)))
                    except (TypeError, ValueError):
                        pass
                if name in LLM_SPAN_NAMES:
                    llm_calls += 1

    total_tokens = estimate_tokens(token_chars)
    return {
        "sessions": len(session_ids),
        "completed_sessions": len(completed_sessions),
        "turns": turns,
        "cache_hits": cache_hits,
        "errors": errors,
        "llm_calls": llm_calls,
        "token_chars": token_chars,
        "total_tokens_estimate": total_tokens,
        "ttft_ms": round(sum(ttft_samples) / len(ttft_samples), 2) if ttft_samples else 0.0,
        "ttft_samples": len(ttft_samples),
    }


def summarize_feedback(items: list) -> dict:
    """聚合反馈列表：点赞 / 点踩 / 点踩原因分布。

    Args:
        items: UserFeedback 列表（或带 score / reason 属性的测试替身）。

    Returns:
        反馈聚合 dict。
    """
    likes = 0
    dislikes = 0
    reasons: Counter = Counter()
    for item in items or []:
        score = int(getattr(item, "score", 0))
        if score == 1:
            likes += 1
        elif score == -1:
            dislikes += 1
            code = str(getattr(item, "reason", "") or "")
            if code:
                reasons[code] += 1
    distribution = {code: 0 for code in REASON_LABELS}
    distribution.update(dict(reasons))
    return {"likes": likes, "dislikes": dislikes, "reason_distribution": distribution}


def aggregate_dashboard_metrics(
    trace_dir: str | Path,
    feedback_items: list | None = None,
    *,
    order_success: int = 0,
    price_per_1k_tokens: float | None = None,
) -> dict:
    """把 trace / 反馈 / 订单三路数据合成 Dashboard 指标（纯函数）。

    Args:
        trace_dir: trace 目录。
        feedback_items: 反馈列表。
        order_success: 下单成功订单数。
        price_per_1k_tokens: 可选单价覆盖。

    Returns:
        指标 dict。
    """
    trace_stats = summarize_traces(trace_dir)
    feedback_stats = summarize_feedback(list(feedback_items or []))

    feedback_sessions = {
        str(getattr(item, "session_id", "") or "")
        for item in (feedback_items or [])
        if getattr(item, "session_id", "")
    }
    sessions = max(trace_stats["sessions"], len(feedback_sessions))
    turns = trace_stats["turns"]
    tokens = trace_stats["total_tokens_estimate"]
    price = DEFAULT_PRICE_PER_1K_TOKENS
    if price_per_1k_tokens is not None:
        price = float(price_per_1k_tokens)
    else:
        try:
            price = float(os.getenv("LLM_PRICE_PER_1K_TOKENS") or DEFAULT_PRICE_PER_1K_TOKENS)
        except ValueError:
            price = DEFAULT_PRICE_PER_1K_TOKENS

    metrics = _blank_metrics()
    metrics.update(
        {
            "sessions": sessions,
            "turns": turns,
            "avg_turns": round(turns / sessions, 2) if sessions else 0.0,
            "completion_rate": (
                round(trace_stats["completed_sessions"] / sessions, 4) if sessions else 0.0
            ),
            "likes": feedback_stats["likes"],
            "dislikes": feedback_stats["dislikes"],
            "reason_distribution": feedback_stats["reason_distribution"],
            "ttft_ms": trace_stats["ttft_ms"],
            "ttft_samples": trace_stats["ttft_samples"],
            "cache_hits": trace_stats["cache_hits"],
            "cache_hit_rate": round(trace_stats["cache_hits"] / turns, 4) if turns else 0.0,
            "total_tokens_estimate": tokens,
            "cost_estimate": estimate_cost(tokens, price),
            "price_per_1k_tokens": price,
            "order_success": max(int(order_success), 0),
            "trace_sessions": trace_stats["sessions"],
            "errors": trace_stats["errors"],
            "llm_calls": trace_stats["llm_calls"],
        }
    )
    return metrics


async def count_successful_orders(engine) -> int:
    """统计 ``orders.status == CONFIRMED`` 的订单数。

    Args:
        engine: SQLAlchemy AsyncEngine；为 None 时返回 0。

    Returns:
        订单数。
    """
    if engine is None:
        return 0
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        return int(
            await db.scalar(
                select(func.count()).select_from(OrderRow).where(
                    OrderRow.status == OrderStatus.CONFIRMED.value,
                ),
            )
            or 0
        )


def _default_trace_dir(container) -> Path:
    """从容器 settings 推导 trace 目录，缺省回退项目 ``data/traces``。

    Args:
        container: 装配容器或测试替身。

    Returns:
        trace 目录 Path。
    """
    settings = getattr(container, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        from app.infrastructure.settings import PROJECT_ROOT

        data_dir = PROJECT_ROOT / "data"
    return Path(data_dir) / "traces"


async def collect_dashboard(container) -> dict:
    """读取容器依赖并产出 Dashboard 指标。

    Args:
        container: 装配容器或测试替身；缺依赖时对应指标按 0 处理。

    Returns:
        指标 dict。
    """
    feedback_items: list = []
    store = getattr(container, "feedback_store", None)
    if store is not None and hasattr(store, "list_all"):
        try:
            feedback_items = await store.list_all()
        except Exception:  # noqa: BLE001 - 指标接口不能因反馈表异常整体 500
            feedback_items = []

    order_success = 0
    engine = getattr(container, "engine", None)
    if engine is not None:
        try:
            order_success = await count_successful_orders(engine)
        except Exception:  # noqa: BLE001 - 指标接口 fail-safe
            order_success = 0

    metrics = aggregate_dashboard_metrics(
        _default_trace_dir(container),
        feedback_items,
        order_success=order_success,
    )
    # P10-5：Dashboard 展示当前 A/B 实验配置（Container 快照优先，回退 settings）。
    metrics["experiment"] = experiment_config(container)
    return metrics


def experiment_config(container) -> dict:
    """读取当前实验配置：容器快照优先，容器缺失时从 settings 推导。

    Args:
        container: 装配容器或测试替身（可为 None）。

    Returns:
        ``{semantic_cache_enabled, reranker_enabled, model_route_mode}``。
    """
    explicit = getattr(container, "experiment_config", None)
    if isinstance(explicit, dict) and explicit:
        return dict(explicit)
    return experiment_config_from_settings(getattr(container, "settings", None))


def build_dashboard_router() -> APIRouter:
    """兼容旧路径：Dashboard 路由已迁到 ``app.presentation.routers.dashboard``。

    本模块保留指标聚合纯函数（``aggregate_dashboard_metrics`` / ``collect_dashboard``
    等）供测试与 router 复用，仅路由器构造转发到 routers 包。

    Returns:
        APIRouter。
    """
    from app.presentation.routers.dashboard import build_dashboard_router as _build_router

    return _build_router()

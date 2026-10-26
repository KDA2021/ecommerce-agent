# -*- coding: utf-8 -*-
"""compress 节点：执行上下文压缩并发布 ``context.compressed`` 事件。"""
from __future__ import annotations

from contextlib import nullcontext

from app.application.agents.compression import compress_messages
from app.application.agents.state import MainState
from app.infrastructure.context import ShoppingContext
from app.infrastructure.tracing import traced_span


def make_compress_node(compression_llm, settings, broker=None, throttle=None, tracer=None):
    """构造压缩节点；LLM / settings / broker / 闸门 / tracer 全部显式传入。

    Args:
        compression_llm: 压缩摘要 LLM。
        settings: 运行时配置（读取 ``context_trigger_tokens`` / ``context_keep_recent``）。
        broker: 可选事件总线，用于发布 ``context.compressed``。
        throttle: 可选 LLM 并发闸门。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        ``async def compress_node(state) -> dict``。
    """

    async def compress_node(state: MainState) -> dict:
        """执行上下文压缩，并在真正压缩时发布 ``context.compressed`` 事件。

        Args:
            state: 父图 state。

        Returns:
            ``compress_messages`` 的 state 更新。
        """
        slot = throttle.slot() if throttle is not None else nullcontext()
        async with traced_span(tracer, "compress", {"node": "compress"}) as span:
            async with slot:
                result = await compress_messages(
                    compression_llm, state["messages"],
                    settings.context_trigger_tokens, settings.context_keep_recent,
                    prior_summary=state.get("summary", ""),
                )
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["compressed"] = bool(result.get("summary"))
        summary = result.get("summary")
        if broker is not None and summary:
            broker.publish(
                ShoppingContext.current_session_id(),
                "context.compressed",
                {
                    "summary_length": len(summary),
                    "removed_messages": len(result.get("messages", [])),
                },
            )
        return result

    return compress_node

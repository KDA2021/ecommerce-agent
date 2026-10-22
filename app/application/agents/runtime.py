# -*- coding: utf-8 -*-
"""主图运行时辅助：最终文本提取、非流式 / 流式执行入口。

从 ``main_graph.py`` 抽出；``main_graph.py`` 继续 re-export，保证旧导入路径
``from app.application.agents.main_graph import invoke_agent, invoke_agent_stream``
不变。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.infrastructure.tracing import traced_span


def _final_text(messages) -> str:
    """从消息列表反向取最后一条「非工具调用的文本消息」内容。"""
    for message in reversed(list(messages or [])):
        content = getattr(message, "content", "") or ""
        # 跳过带 tool_calls 的中间消息，只返回真正的自然语言答复。
        if content and not getattr(message, "tool_calls", None):
            return str(content)
    return ""


async def invoke_agent(agent, session_id: str, query: str) -> str:
    """非流式执行一次主图，返回最后一条非工具调用的文本。

    Args:
        agent: 编译后的主图。
        session_id: 会话 / 线程 id。
        query: 用户输入。

    Returns:
        最终回复文本。
    """
    config = {"configurable": {"thread_id": session_id}}
    result = await agent.ainvoke({"messages": [HumanMessage(content=query)]}, config=config)
    return _final_text(result.get("messages", []))


def _chunk_token(content) -> str:
    """把 `on_chat_model_stream` 的 chunk.content 归一化成可发布的字符串 token。"""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


async def invoke_agent_stream(
    agent, session_id: str, query: str, broker=None, tracer=None,
) -> str:
    """流式执行 Agent：逐 token 发布 ``token.delta``，返回最终文本。

    用 ``astream_events(version="v2")`` 消费事件流，只把非空 token 转成
    ``broker.publish(session_id, "token.delta", {"token": token})``；流结束后用
    ``aget_state`` 取最终消息，避免把工具调用/中间态文本当成答案返回。
    ``final.result`` 由 server 统一发布，这里只返回文本（与 ``invoke_agent`` 同语义）。

    对没有 ``astream_events`` 的旧版/测试替身 Agent 优雅降级回非流式 ``invoke_agent``。
    可选 ``tracer`` 存在时记录 ``request`` 总 span 与首个 token 的 ``ttft`` span；
    ``broker`` 带 ``trace_logger`` 时自动复用，保证生产路径无需额外接线。

    Args:
        agent: 编译后的主图。
        session_id: 会话 / 线程 id。
        query: 用户输入。
        broker: 可选事件总线，用于发布 token.delta。
        tracer: 可选 LocalTraceLogger / span tracer。

    Returns:
        最终回复文本（与 ``invoke_agent`` 同语义）。
    """
    if not hasattr(agent, "astream_events"):
        return await invoke_agent(agent, session_id, query)

    if tracer is None:
        tracer = getattr(broker, "trace_logger", None)

    config = {"configurable": {"thread_id": session_id}}
    async with traced_span(
        tracer,
        "request",
        {"session_id": session_id, "query_length": len(query)},
    ):
        # 手动管理 ttft span：首个 token 到达时立即关闭，用于统计首 token 延迟。
        ttft_span = (
            tracer.span("ttft", {"session_id": session_id})
            if tracer is not None
            else None
        )
        ttft_open = False
        try:
            if ttft_span is not None:
                await ttft_span.__aenter__()
                ttft_open = True
            async for event in agent.astream_events(
                {"messages": [HumanMessage(content=query)]}, config=config, version="v2"
            ):
                if event.get("event") != "on_chat_model_stream":
                    continue
                chunk = (event.get("data") or {}).get("chunk")
                token = _chunk_token(getattr(chunk, "content", ""))
                if not token:
                    continue
                if ttft_open:
                    await ttft_span.__aexit__(None, None, None)
                    ttft_open = False
                if broker is not None:
                    broker.publish(session_id, "token.delta", {"token": token})
        except BaseException as exc:  # noqa: BLE001 - 确保 ttft span 落盘后原样抛出
            if ttft_open:
                await ttft_span.__aexit__(type(exc), exc, exc.__traceback__)
                ttft_open = False
            raise
        finally:
            if ttft_open:
                await ttft_span.__aexit__(None, None, None)
                ttft_open = False

        # 流结束后重新取最终 state，避免把流中的工具调用文本当成答案。
        aget_state = getattr(agent, "aget_state", None)
        if aget_state is None:
            return ""
        state = await aget_state(config)
        values = getattr(state, "values", None) or {}
        return _final_text(values.get("messages", []))

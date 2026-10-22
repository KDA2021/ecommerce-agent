# -*- coding: utf-8 -*-
"""上下文压缩：超阈值时摘要早期消息，删除后保留最近 N 条。"""
from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph.message import RemoveMessage


def estimate_tokens(messages: list[Any]) -> int:
    """用「4 字符 ≈ 1 token」的启发式估算消息总 token 数。

    Args:
        messages: LangChain 消息列表。

    Returns:
        估算出的 token 数。
    """
    total = 0
    for message in messages:
        content = getattr(message, "content", "") or ""
        if isinstance(content, list):
            text = " ".join(str(part) for part in content)
        else:
            text = str(content)
        total += max(1, len(text) // 4)
    return total


async def compress_messages(compression_llm, messages, trigger_tokens, keep_recent):
    """超阈值时把早期消息摘要成 summary，并用 RemoveMessage 删除原文。

    Args:
        compression_llm: 用于生成摘要的 LLM。
        messages: 当前全部消息。
        trigger_tokens: 触发压缩的 token 阈值。
        keep_recent: 保留最近 N 条不压缩。

    Returns:
        state 更新 dict；无需压缩时返回空 dict。
    """
    if not messages or len(messages) <= keep_recent:
        return {}
    if estimate_tokens(messages) < trigger_tokens:
        return {}
    to_compress = messages[:-keep_recent]
    transcript = "\n".join(
        f"{getattr(m, 'type', 'msg')}: {getattr(m, 'content', '')}" for m in to_compress
    )
    prompt = f"""请把下面这段跨境电商对话压缩成中文摘要。必须逐字保留：
product_id、sku_id、订单号、金额与币种、买家明确要求。不要编造数字。
对话：
{transcript}"""
    response = await compression_llm.ainvoke([
        SystemMessage(content="你是上下文压缩器，只输出摘要。"),
        HumanMessage(content=prompt),
    ])
    summary = str(response.content)
    removals = []
    for m in to_compress:
        if getattr(m, "id", None) is None:
            m.id = str(uuid.uuid4())
        removals.append(RemoveMessage(id=m.id, content=""))
    return {"messages": removals, "summary": summary, "turn_compressed": True}

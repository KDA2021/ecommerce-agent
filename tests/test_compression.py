# -*- coding: utf-8 -*-
from langchain_core.messages import HumanMessage

from app.application.agents.compression import compress_messages


class _FakeLlm:
    async def ainvoke(self, messages):
        return type("R", (), {"content": "买家要无塑料的旅行三件套，预算200。"})()


async def test_no_compression_below_threshold():
    messages = [HumanMessage(content="你好")]
    result = await compress_messages(_FakeLlm(), messages, trigger_tokens=1000, keep_recent=2)
    assert result == {}


async def test_compression_returns_summary_and_removals():
    messages = [
        HumanMessage(content="长文本" * 500),
        HumanMessage(content="最近一条"),
    ]
    result = await compress_messages(_FakeLlm(), messages, trigger_tokens=10, keep_recent=1)
    assert result["summary"]
    assert result["turn_compressed"] is True
    assert len(result["messages"]) == 1  # 删掉最早一条

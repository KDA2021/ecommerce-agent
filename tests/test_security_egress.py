"""Sensitive data is filtered independently at trace, search, and answer sinks."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.application.agents.runtime import invoke_agent, invoke_agent_stream
from app.application.tools.web_search_tool import WebSearchArgs, build_web_search_tool
from app.infrastructure.tracing import LocalTraceLogger


class Broker:
    def __init__(self):
        self.events = []

    def publish(self, session_id, event_type, payload):
        self.events.append((session_id, event_type, payload))


class SearchClient:
    def __init__(self):
        self.calls = []

    async def search(self, query, max_results=3):
        self.calls.append((query, max_results))
        return []


def test_trace_recursively_redacts_shipping_and_credentials_without_mutation(tmp_path):
    payload = {
        "tool": "create_order", "status": "ok", "count": 2,
        "args": {"shipping": {"name": "张三", "phone": "13800138000",
                               "address": "北京市朝阳区测试路8号"}},
        "headers": {"Authorization": "Bearer opaque-secret", "Cookie": "sid=private"},
        "nested": [{"api_key": "private-key", "password": "private-pass"}],
    }
    original = copy.deepcopy(payload)
    logger = LocalTraceLogger(tmp_path)
    logger.record_nowait("s", "t", "tool.invoke", payload)
    raw = logger.path_for("s").read_text(encoding="utf-8")
    for secret in ("张三", "13800138000", "北京市朝阳区测试路8号", "opaque-secret",
                   "sid=private", "private-key", "private-pass"):
        assert secret not in raw
    assert payload == original
    saved = json.loads(raw)["payload"]
    assert (saved["tool"], saved["status"], saved["count"]) == ("create_order", "ok", 2)


def test_trace_hashes_entire_stream_token_instead_of_persisting_text(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    logger.record_nowait("s", "t", "token.delta", {"token": "private answer fragment"})
    payload = json.loads(logger.path_for("s").read_text(encoding="utf-8"))["payload"]
    assert payload["token"]["length"] == len("private answer fragment")
    assert "private answer fragment" not in json.dumps(payload)


@pytest.mark.parametrize("query", [
    "请搜索手机号 13800138000", "收货人：张三", "地址：北京市朝阳区测试路8号",
    "email buyer@example.com", "Authorization: Bearer opaque-secret",
    "api_key=very-private-key", "Cookie: sid=private",
    "OPENAI_API_KEY=private-provider-secret", "token=private-opaque-token",
])
async def test_external_search_blocks_sensitive_query_before_any_client_call(query):
    client, broker = SearchClient(), Broker()
    tool = build_web_search_tool(client, broker=broker)
    result = await tool.ainvoke({"query": query})
    assert not client.calls
    assert result["code"] == "sensitive_query_blocked"
    assert query not in json.dumps(broker.events, ensure_ascii=False)
    assert not any(kind == "tool.invoke" for _, kind, _ in broker.events)


@pytest.mark.parametrize("arguments", [
    {"query": ""}, {"query": "x" * 1001}, {"query": "ok", "max_results": 0},
    {"query": "ok", "max_results": 11},
])
def test_web_search_arguments_are_bounded(arguments):
    with pytest.raises(ValidationError):
        WebSearchArgs(**arguments)


async def test_benign_public_search_still_calls_client():
    client = SearchClient()
    result = await build_web_search_tool(client).ainvoke({"query": "旅行箱公开报价"})
    assert result == {"results": []}
    assert client.calls == [("旅行箱公开报价", 3)]


class StreamAgent:
    def __init__(self, reply, broker):
        self.reply, self.broker = reply, broker

    async def astream_events(self, payload, config, version):
        for piece in ("internal secret", "138001", "38000"):
            yield {"event": "on_chat_model_stream", "data": {"chunk": SimpleNamespace(content=piece)}}
            assert self.broker.events == [], "raw intermediate chunks must stay private"

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": [AIMessage(content=self.reply)]})


async def test_stream_releases_only_sanitized_final_state_and_returns_identical_text():
    broker = Broker()
    reply = await invoke_agent_stream(
        StreamAgent("联系 13800138000；api_key=very-private-key", broker), "s", "hi", broker,
    )
    assert "13800138000" not in reply
    assert "very-private-key" not in reply
    assert broker.events == [("s", "token.delta", {"token": reply})]


async def test_stream_with_no_final_state_releases_no_intermediate_text():
    broker = Broker()
    agent = StreamAgent("unused", broker)
    agent.aget_state = None
    assert await invoke_agent_stream(agent, "s", "hi", broker) == ""
    assert broker.events == []


class LegacyAgent:
    async def ainvoke(self, payload, config):
        return {"messages": [AIMessage(content="电话：13800138000")]}


@pytest.mark.parametrize("entrypoint", [invoke_agent, invoke_agent_stream])
async def test_non_stream_and_legacy_fallback_sanitize_final_answer(entrypoint):
    reply = await entrypoint(LegacyAgent(), "s", "hi")
    assert "13800138000" not in reply


async def test_configured_secrets_are_filtered_at_all_sinks(tmp_path):
    from app.application.data_policy import DataPolicy

    secret = "opaque-runtime-credential"
    policy = DataPolicy(secret_values=(secret,))
    broker, client = Broker(), SearchClient()
    tool = build_web_search_tool(client, broker=broker, data_policy=policy)
    assert (await tool.ainvoke({"query": secret}))["code"] == "sensitive_query_blocked"
    assert client.calls == []
    broker.events.clear()
    reply = await invoke_agent_stream(StreamAgent(secret, broker), "s", "hi", data_policy=policy)
    assert secret not in reply
    logger = LocalTraceLogger(tmp_path, data_policy=policy)
    logger.record_nowait("s", "t", "tool.result", {"nested": [secret]})
    assert secret not in logger.path_for("s").read_text(encoding="utf-8")


async def test_output_guard_sees_sanitized_final_text_before_any_publish():
    broker, guarded = Broker(), []

    async def guard(text):
        assert not broker.events
        assert "13800138000" not in text
        guarded.append(text)
        return text

    reply = await invoke_agent_stream(
        StreamAgent("phone 13800138000", broker), "s", "hi", broker, output_guard=guard,
    )
    assert guarded == [reply]
    assert broker.events == [("s", "token.delta", {"token": reply})]


@pytest.mark.parametrize("streaming", [True, False])
async def test_output_guard_failure_blocks_stream_and_legacy_fallback(streaming):
    broker = Broker()

    async def guard(text):
        raise ValueError("completion evidence denied")

    agent = StreamAgent("answer", broker) if streaming else LegacyAgent()
    with pytest.raises(ValueError, match="completion evidence denied"):
        await invoke_agent_stream(agent, "s", "hi", broker, output_guard=guard)
    assert broker.events == []


async def test_trace_span_error_redacts_injected_secret(tmp_path):
    from app.application.data_policy import DataPolicy

    secret = "private-span-credential"
    logger = LocalTraceLogger(tmp_path, data_policy=DataPolicy(secret_values=(secret,)))
    with pytest.raises(RuntimeError):
        async with logger.span("search", {"tool": "search"}):
            raise RuntimeError(f"upstream failed: {secret}")
    files = list(logger.trace_dir.glob("*.jsonl"))
    assert len(files) == 1
    assert secret not in files[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("query", [
    "北京市朝阳区测试路8号", "phone +1-415-555-0199", '"api_key": "json-private"',
])
async def test_external_search_blocks_address_international_phone_and_json_secret(query):
    client = SearchClient()
    result = await build_web_search_tool(client).ainvoke({"query": query})
    assert client.calls == []
    assert result["code"] == "sensitive_query_blocked"


def test_trace_redacts_provider_prefixed_credential_field(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    logger.record_nowait("s", "t", "tool.result", {"openai_api_key": "private-provider-key"})
    assert "private-provider-key" not in logger.path_for("s").read_text(encoding="utf-8")


@pytest.mark.parametrize("streaming", [True, False])
async def test_output_guard_replacement_is_sanitized_published_and_returned(streaming):
    broker = Broker()

    async def guard(text):
        assert not broker.events
        return "Replacement answer phone 13800138000"

    agent = StreamAgent("Original completion claim", broker) if streaming else LegacyAgent()
    reply = await invoke_agent_stream(agent, "s", "hi", broker, output_guard=guard)
    assert reply == "Replacement answer phone [REDACTED]"
    assert "Original completion claim" not in json.dumps(broker.events)
    if streaming:
        assert broker.events == [("s", "token.delta", {"token": reply})]
    else:
        assert broker.events == []


async def test_missing_final_state_still_returns_guard_replacement():
    broker = Broker()
    agent = StreamAgent("unused", broker)
    agent.aget_state = None

    async def guard(text):
        assert text == ""
        return "No completion evidence found."

    reply = await invoke_agent_stream(agent, "s", "hi", broker, output_guard=guard)
    assert reply == "No completion evidence found."
    assert broker.events == [("s", "token.delta", {"token": reply})]

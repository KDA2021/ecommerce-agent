# -*- coding: utf-8 -*-
"""P6：token 流式发布、本地 JSONL trace、trace_id 自动注入。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.application.agents.main_graph import invoke_agent_stream
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbroker import EventBroker
from app.infrastructure.tracing import LocalTraceLogger


class RecordingBroker:
    """只记录 publish 调用的最小 broker 替身。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, session_id: str, event_type: str, payload: dict) -> None:
        self.events.append((session_id, event_type, payload))


def _context(session_id: str = "s1", trace_id: str = "trace-abc"):
    return ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id=session_id,
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id=trace_id,
        )
    )


class FakeStreamAgent:
    """最小 astream_events(v2) 图替身：按序吐出 chunk，aget_state 给出最终文本。"""

    def __init__(self, chunks, reply: str = "最终回答") -> None:
        self.chunks = chunks
        self.reply = reply
        self.calls: list[tuple] = []

    async def astream_events(self, payload, config, version):
        self.calls.append((payload, config, version))
        yield {"event": "on_chain_start", "data": {}}
        for content in self.chunks:
            yield {
                "event": "on_chat_model_stream",
                "data": {"chunk": SimpleNamespace(content=content)},
            }
        yield {"event": "on_chain_end", "data": {}}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": [AIMessage(content=self.reply)]})


class LegacyAgent:
    """没有 astream_events 的旧 Agent：应降级回非流式 invoke_agent。"""

    def __init__(self, reply: str = "旧回复") -> None:
        self.reply = reply
        self.calls: list[tuple] = []

    async def ainvoke(self, payload, config):
        self.calls.append((payload, config))
        return {"messages": [AIMessage(content=self.reply)]}


# --------------------------------------------------------------------------- #
# invoke_agent_stream
# --------------------------------------------------------------------------- #


async def test_invoke_agent_stream_publishes_non_empty_token_delta():
    agent = FakeStreamAgent(["旅", "行", "", "三件套"])
    broker = RecordingBroker()

    reply = await invoke_agent_stream(agent, "s-stream", "推荐旅行三件套", broker)

    assert reply == "最终回答"
    assert agent.calls[0][1] == {"configurable": {"thread_id": "s-stream"}}
    assert agent.calls[0][2] == "v2"
    assert [event[1] for event in broker.events] == ["token.delta"] * 3
    assert [event[2]["token"] for event in broker.events] == ["旅", "行", "三件套"]
    assert all(event[0] == "s-stream" for event in broker.events)


async def test_invoke_agent_stream_normalizes_list_content():
    agent = FakeStreamAgent([[{"type": "text", "text": "你"}], "好"])
    broker = RecordingBroker()

    await invoke_agent_stream(agent, "s1", "hi", broker)

    assert [event[2]["token"] for event in broker.events] == ["你", "好"]


async def test_invoke_agent_stream_works_without_broker():
    agent = FakeStreamAgent(["无", "broker"])

    reply = await invoke_agent_stream(agent, "s1", "hi", None)

    assert reply == "最终回答"


async def test_invoke_agent_stream_falls_back_for_non_streaming_agent():
    agent = LegacyAgent()
    broker = RecordingBroker()

    reply = await invoke_agent_stream(agent, "s1", "hi", broker)

    assert reply == "旧回复"
    assert broker.events == []
    assert agent.calls[0][1] == {"configurable": {"thread_id": "s1"}}


async def test_invoke_agent_stream_against_real_langgraph():
    """不用假 event pipeline，验证 astream_events v2 在真实编译图上真能拿到 token 流。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class _State(TypedDict):
        messages: list

    model = GenericFakeChatModel(messages=iter([AIMessage(content="你好世界")]))

    async def chat_node(state):
        return {"messages": [await model.ainvoke(state["messages"])]}

    builder = StateGraph(_State)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    broker = RecordingBroker()
    reply = await invoke_agent_stream(graph, "real-thread", "hi", broker)

    assert reply == "你好世界"
    tokens = [event[2]["token"] for event in broker.events if event[1] == "token.delta"]
    assert tokens
    assert "".join(tokens) == "你好世界"


# --------------------------------------------------------------------------- #
# LocalTraceLogger
# --------------------------------------------------------------------------- #


async def test_local_trace_logger_writes_jsonl_lines(tmp_path):
    logger = LocalTraceLogger(tmp_path)

    await logger.record("s1", "trace-1", "token.delta", {"token": "你"})
    await logger.record("s1", "trace-1", "final.result", {"reply": "你好"})

    path = tmp_path / "traces" / "s1.jsonl"
    assert path.exists()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["trace_id"] == "trace-1"
    assert lines[0]["type"] == "token.delta"
    assert lines[0]["payload"] == {"token": "你"}
    assert isinstance(lines[0]["ts"], float)
    # P11-3：reply 明文不落盘，只留 sha256 前 16 位 + 长度。
    assert lines[1]["payload"] == {
        "reply": {
            "sha256": hashlib.sha256("你好".encode("utf-8")).hexdigest()[:16],
            "length": 2,
        },
    }


async def test_local_trace_logger_separates_sessions(tmp_path):
    logger = LocalTraceLogger(tmp_path)

    await logger.record("a", "t", "token.delta", {"token": "1"})
    await logger.record("b", "t", "token.delta", {"token": "2"})

    assert (tmp_path / "traces" / "a.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (tmp_path / "traces" / "b.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_local_trace_logger_record_nowait_is_best_effort(tmp_path):
    logger = LocalTraceLogger(tmp_path)

    logger.record_nowait("s1", "trace-1", "final.result", {"reply": "ok"})

    line = json.loads((tmp_path / "traces" / "s1.jsonl").read_text(encoding="utf-8"))
    assert line["type"] == "final.result"
    # P11-3：即使 best-effort 路径也先脱敏再落盘。
    assert line["payload"] == {
        "reply": {
            "sha256": hashlib.sha256(b"ok").hexdigest()[:16],
            "length": 2,
        },
    }


def test_local_trace_logger_sanitizes_session_id(tmp_path):
    logger = LocalTraceLogger(tmp_path)

    logger.record_nowait("../escape", "t", "final.result", {"reply": "ok"})

    assert not (tmp_path.parent / "escape.jsonl").exists()
    assert list((tmp_path / "traces").glob("*.jsonl"))


# --------------------------------------------------------------------------- #
# EventBroker：trace_id 合并 + 落盘
# --------------------------------------------------------------------------- #


async def test_event_broker_merges_context_trace_id_into_frame():
    broker = EventBroker()
    stream = broker.subscribe("s1")
    next_frame = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    token = _context("s1", "trace-123")
    try:
        broker.publish("s1", "final.result", {"reply": "ok"})
        frame = await asyncio.wait_for(next_frame, timeout=2)
    finally:
        await stream.aclose()
        ShoppingContext.reset(token)

    assert frame == 'event: final.result\ndata: {"reply": "ok", "trace_id": "trace-123"}\n\n'


async def test_event_broker_keeps_explicit_trace_id():
    broker = EventBroker()
    stream = broker.subscribe("s1")
    next_frame = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    token = _context("s1", "trace-from-context")
    try:
        broker.publish("s1", "final.result", {"reply": "ok", "trace_id": "trace-explicit"})
        frame = await asyncio.wait_for(next_frame, timeout=2)
    finally:
        await stream.aclose()
        ShoppingContext.reset(token)

    assert '"trace_id": "trace-explicit"' in frame
    assert "trace-from-context" not in frame


async def test_event_broker_leaves_frame_untouched_without_context():
    broker = EventBroker()
    stream = broker.subscribe("s1")
    next_frame = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    try:
        broker.publish("s1", "final.result", {"reply": "缓存回复"})
        frame = await asyncio.wait_for(next_frame, timeout=2)
    finally:
        await stream.aclose()

    assert frame == 'event: final.result\ndata: {"reply": "缓存回复"}\n\n'


async def test_event_broker_records_published_events_to_trace(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    broker = EventBroker(trace_logger=logger)
    token = _context("s1", "trace-9")
    try:
        broker.publish("s1", "token.delta", {"token": "你"})
        broker.publish("s1", "final.result", {"reply": "你好"})
    finally:
        ShoppingContext.reset(token)

    lines = [
        json.loads(line)
        for line in (tmp_path / "traces" / "s1.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [line["type"] for line in lines] == ["token.delta", "final.result"]
    assert all(line["trace_id"] == "trace-9" for line in lines)
    assert lines[0]["payload"] == {"token": "你", "trace_id": "trace-9"}


# --------------------------------------------------------------------------- #
# server：每请求生成 trace_id 且走流式
# --------------------------------------------------------------------------- #


class _FakeCache:
    async def lookup(self, buyer_id, query, has_history, scope=""):
        return None

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        return None


class _FakePreferenceStore:
    async def list_by_buyer(self, buyer_id):
        return []


class _StreamingServerAgent:
    """记录请求执行期间 ShoppingContext 里的 trace_id，模拟真实图吐 token。"""

    def __init__(self) -> None:
        self.seen_trace_ids: list[str] = []

    async def astream_events(self, payload, config, version):
        snapshot = ShoppingContext.current()
        self.seen_trace_ids.append(snapshot.trace_id if snapshot else "")
        yield {
            "event": "on_chat_model_stream",
            "data": {"chunk": SimpleNamespace(content="你好")},
        }

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": [AIMessage(content="你好")]})


def _post_intent(client: TestClient):
    return client.post(
        "/commerce/intents",
        json={
            "shopping_session_id": "s-test",
            "buyer_id": "buyer-1",
            "query": "推荐一个旅行三件套",
        },
    )


def _server_client(container):
    from app.presentation.server import create_app

    return TestClient(create_app(container=container, build_on_startup=False))


def test_submit_intent_streams_tokens_and_sets_uuid_trace_id():
    agent = _StreamingServerAgent()
    broker = RecordingBroker()
    container = SimpleNamespace(
        agent=agent,
        semantic_cache=_FakeCache(),
        broker=broker,
        preference_store=_FakePreferenceStore(),
    )
    client = _server_client(container)

    first = _post_intent(client)
    second = _post_intent(client)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reply"] == "你好"
    assert [event[1] for event in broker.events] == [
        "token.delta",
        "final.result",
        "token.delta",
        "final.result",
    ]
    assert [event[2]["token"] for event in broker.events if event[1] == "token.delta"] == [
        "你好",
        "你好",
    ]
    assert len(agent.seen_trace_ids) == 2
    assert agent.seen_trace_ids[0] != agent.seen_trace_ids[1]
    for trace_id in agent.seen_trace_ids:
        assert uuid.UUID(hex=trace_id)

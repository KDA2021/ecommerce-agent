"""Correlation must survive HTTP, queue serialization, and final SSE publication."""
import asyncio
from types import SimpleNamespace

from app.domain.queue.ports.task_queue import IntentTask
from app.infrastructure.cache.chroma_semantic_cache import DisabledSemanticCache
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbroker import EventBroker
from app.presentation.routers.intents import IntentRequest, _run_intent_direct


async def test_final_direct_event_keeps_request_identity_after_context_reset(monkeypatch):
    from app.presentation.routers import intents

    captured = []
    async def invoke(*_args, **_kwargs):
        return "hello"
    monkeypatch.setattr(intents, "invoke_agent_stream", invoke)
    broker = SimpleNamespace(publish=lambda sid, name, payload: captured.append((name, payload)))
    c = SimpleNamespace(agent=object(), broker=broker, semantic_cache=DisabledSemanticCache())
    body = IntentRequest(shopping_session_id="session", buyer_id="a", query="hi", client_request_id="req-123")
    result = await _run_intent_direct(c, body)
    final = [payload for name, payload in captured if name == "final.result"][-1]
    assert final["client_request_id"] == "req-123"
    assert final["trace_id"] == result.trace_id


def test_queue_roundtrip_keeps_client_request_id():
    task = IntentTask("task", "session", "a", "zh-CN", "CNY", "hi", client_request_id="req-42")
    assert IntentTask.from_dict(task.to_dict()).client_request_id == "req-42"


async def test_worker_broker_enriches_all_events_with_task_and_client_request_id():
    captured = []
    broker = EventBroker()
    stream = broker.subscribe("s")
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    token = ShoppingContext.set(ShoppingContextSnapshot("s", "a", "zh-CN", "CNY", "trace", "task", "req-42"))
    try:
        broker.publish("s", "token.delta", {"delta": "x"})
        captured.append(await asyncio.wait_for(next_event, 1))
    finally:
        ShoppingContext.reset(token)
        await stream.aclose()
    assert "req-42" in str(captured[0])
    assert "task" in str(captured[0])

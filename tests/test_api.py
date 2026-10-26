# -*- coding: utf-8 -*-
import asyncio
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.infrastructure.eventbroker import EventBroker


class FakeCache:
    def __init__(self, hit=None):
        self.hit = hit
        self.lookups: list = []
        self.remembers: list = []

    async def lookup(self, buyer_id, query, has_history, scope=""):
        self.lookups.append((buyer_id, query, has_history, scope))
        return self.hit

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        self.remembers.append((buyer_id, query, reply, has_history, scope))


class FakePreferenceStore:
    def __init__(self, preferences=None):
        self.preferences = list(preferences or [])

    async def list_by_buyer(self, buyer_id):
        return [p for p in self.preferences if p.buyer_id == buyer_id]


class FakeBroker:
    def __init__(self):
        self.events: list = []

    def publish(self, session_id, event_type, payload):
        self.events.append((session_id, event_type, payload))


class FakeAgent:
    def __init__(self, reply: str = "测试回复", history=None):
        self.reply = reply
        self.history = history or []
        self.calls: list = []

    async def ainvoke(self, payload, config):
        assert config == {"configurable": {"thread_id": "s-test"}}
        self.calls.append((payload, config))
        return {"messages": [AIMessage(content=self.reply)]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self.history)})


class BoomAgent:
    async def ainvoke(self, payload, config):
        raise RuntimeError("boom")


class FakeContainer:
    def __init__(self, agent, cache=None, broker=None, preference_store=None):
        self.agent = agent
        self.semantic_cache = cache or FakeCache()
        self.broker = broker or FakeBroker()
        self.preference_store = preference_store or FakePreferenceStore()


def _client(container) -> TestClient:
    from app.presentation.server import create_app

    return TestClient(create_app(container=container, build_on_startup=False))


def _post_intent(client: TestClient, **overrides):
    payload = {
        "shopping_session_id": "s-test",
        "buyer_id": "buyer-1",
        "query": "推荐一个旅行三件套",
    }
    payload.update(overrides)
    return client.post("/commerce/intents", json=payload)


def test_health_without_database():
    from app.presentation.server import create_app

    app = create_app(container=None, build_on_startup=False)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_submit_intent_returns_fake_agent_reply():
    container = FakeContainer(FakeAgent())
    client = _client(container)
    response = _post_intent(client)
    assert response.status_code == 200
    body = response.json()
    assert body["shopping_session_id"] == "s-test"
    assert body["reply"] == "测试回复"
    assert len(container.agent.calls) == 1
    assert body["trace_id"]
    assert container.broker.events == [("s-test", "final.result", {
        "reply": "测试回复", "trace_id": body["trace_id"], "client_request_id": "",
    })]
    assert container.semantic_cache.remembers == [
        ("buyer-1", "推荐一个旅行三件套", "测试回复", False, ""),
    ]


def test_semantic_cache_scope_changes_when_preferences_change():
    from app.domain.buyer.preference import BuyerPreference

    store = FakePreferenceStore()
    cache = FakeCache()
    container = FakeContainer(FakeAgent(), cache=cache, preference_store=store)
    client = _client(container)

    assert _post_intent(client).status_code == 200
    assert cache.lookups[-1] == ("buyer-1", "推荐一个旅行三件套", False, "")
    assert cache.remembers[-1][4] == ""

    store.preferences.append(
        BuyerPreference(buyer_id="buyer-1", kind="dislike", statement="不要塑料材质")
    )
    assert _post_intent(client).status_code == 200
    changed_scope = cache.lookups[-1][3]
    assert changed_scope
    assert changed_scope != ""
    assert cache.remembers[-1][4] == changed_scope

    store.preferences.clear()
    assert _post_intent(client).status_code == 200
    assert cache.lookups[-1][3] == ""


def test_cache_hit_bypasses_llm_and_publishes_events():
    hit = SimpleNamespace(reply="缓存回复", matched_query="推荐旅行三件套", similarity=0.98)
    cache = FakeCache(hit=hit)
    agent = FakeAgent(reply="不应被调用")
    container = FakeContainer(agent, cache=cache)
    client = _client(container)

    response = _post_intent(client)

    assert response.status_code == 200
    assert response.json()["reply"] == "缓存回复"
    assert agent.calls == []
    assert cache.lookups == [("buyer-1", "推荐一个旅行三件套", False, "")]
    assert cache.remembers == []
    assert [event[1] for event in container.broker.events] == ["cache.hit", "final.result"]
    assert container.broker.events[1][2] == {"reply": "缓存回复"}


def test_history_turn_skips_cache_lookup():
    cache = FakeCache()
    agent = FakeAgent(history=[AIMessage(content="上一轮回复")])
    container = FakeContainer(agent, cache=cache)
    client = _client(container)

    response = _post_intent(client)

    assert response.status_code == 200
    assert response.json()["reply"] == "测试回复"
    assert cache.lookups == []
    assert cache.remembers == []
    assert len(agent.calls) == 1


def test_submit_intent_publishes_error_frame_on_failure():
    container = FakeContainer(BoomAgent())
    client = _client(container)

    response = _post_intent(client)

    assert response.status_code == 500
    assert container.broker.events == [("s-test", "error", {"message": "boom"})]


async def test_event_broker_subscribe_formats_sse_frames():
    broker = EventBroker()
    stream = broker.subscribe("s1")
    next_frame = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    broker.publish("s1", "final.result", {"reply": "缓存回复"})
    frame = await asyncio.wait_for(next_frame, timeout=2)
    await stream.aclose()

    assert frame == 'event: final.result\ndata: {"reply": "缓存回复"}\n\n'


class OneShotBroker:
    """Test double used only because Starlette's TestClient buffers the full ASGI body.

    The production ``EventBroker.subscribe`` never ends, so a buffered TestClient
    request would deadlock. This keeps the real EventBroker publish -> queue ->
    frame path and closes the subscription after the first frame, letting the
    buffered response complete.
    """

    def __init__(self) -> None:
        self.broker = EventBroker()
        self._loop = None

    def publish_from_thread(self, session_id, event_type, payload):
        # asyncio.Queue is bound to the portal loop, so hop back onto that loop.
        self._loop.call_soon_threadsafe(self.broker.publish, session_id, event_type, payload)

    async def subscribe(self, session_id):
        self._loop = asyncio.get_running_loop()
        inner = self.broker.subscribe(session_id)
        try:
            yield await inner.__anext__()
        finally:
            await inner.aclose()


def test_events_endpoint_yields_sse_frames():
    broker = OneShotBroker()
    container = FakeContainer(FakeAgent(), broker=broker)
    client = _client(container)

    published = threading.Event()

    def _publisher() -> None:
        deadline = time.time() + 5
        while time.time() < deadline and not broker.broker._queues.get("s1"):
            time.sleep(0.01)
        broker.publish_from_thread("s1", "final.result", {"reply": "来自 SSE"})
        published.set()

    thread = threading.Thread(target=_publisher, daemon=True)
    thread.start()
    with client.stream(
        "GET", "/commerce/events", params={"shopping_session_id": "s1"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.read().decode("utf-8")
    thread.join(timeout=5)

    assert published.is_set()
    assert "event: final.result" in body
    assert '"reply": "来自 SSE"' in body

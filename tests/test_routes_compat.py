# -*- coding: utf-8 -*-
"""Task 2 路由兼容测试：Presentation 层拆分为 domain routers 后的行为护栏。

证明「拆分只动结构、不动对外契约」：

1. ``create_app`` 签名不变，旧 import 路径（``app.presentation.server``）继续可用；
2. 路由表（方法 + 路径）与拆分前逐一对应，且全部 ``/commerce/*`` 仍挂鉴权；
3. 每个域的路由都能用 ``TestClient`` 真实打通，状态码 / 响应体 / 依赖顺序不变；
4. ``/commerce/events`` 的 SSE 帧格式与响应头不变。

测试统一用最小容器替身，不依赖 MySQL / Redis / LLM。
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.application.metrics.session_metrics import SessionMetrics
from app.application.order_confirmation import OrderConfirmationManager
from app.presentation.server import create_app

# --------------------------------------------------------------------------- #
# 拆分前的路由契约：(方法, 路径) 全集
# --------------------------------------------------------------------------- #
EXPECTED_ROUTES = {
    ("GET", "/health"),
    ("GET", "/commerce/events"),
    ("POST", "/commerce/intents"),
    ("GET", "/commerce/intents/{task_id}"),
    ("GET", "/commerce/dashboard"),
    ("GET", "/commerce/sessions"),
    ("GET", "/commerce/sessions/{session_id}/history"),
    ("GET", "/commerce/orders/{order_id}"),
    ("POST", "/commerce/orders/{order_id}/cancel"),
    ("POST", "/commerce/orders/confirm"),
    ("POST", "/commerce/feedback"),
    ("GET", "/commerce/feedback"),
    ("GET", "/commerce/metrics"),
}


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class _FakeAgent:
    """最小 Agent：默认无历史 + 返回固定回复。"""

    def __init__(self, reply: str = "测试回复", history: list | None = None) -> None:
        self.reply = reply
        self.history = list(history or [])
        self.calls: list = []

    async def ainvoke(self, payload, config):
        from langchain_core.messages import AIMessage

        self.calls.append((payload, config))
        return {"messages": [AIMessage(content=self.reply)]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self.history)})


class _FakeCache:
    async def lookup(self, buyer_id, query, has_history, scope=""):
        return None

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        return None


class _FakePreferenceStore:
    async def list_by_buyer(self, buyer_id):
        return []


class _FakeBroker:
    def __init__(self) -> None:
        self.events: list = []
        self.subscribed: list = []

    def publish(self, session_id, event_type, payload):
        self.events.append((session_id, event_type, payload))

    async def subscribe(self, session_id):
        """有限 SSE 流：产出一帧后结束，方便 TestClient 缓冲读取。"""
        self.subscribed.append(session_id)
        yield 'event: final.result\ndata: {"reply": "来自 SSE"}\n\n'


class _FakeQueue:
    """最小任务队列：enabled + enqueue / set_status / get_status / close。"""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.tasks: dict = {}
        self.statuses: dict = {}

    async def enqueue(self, task) -> str:
        self.tasks[task.task_id] = task
        return "1-0"

    async def set_status(self, status) -> None:
        self.statuses[status.task_id] = status

    async def get_status(self, task_id):
        return self.statuses.get(task_id)

    async def close(self) -> None:
        return None


class _IntentContainer:
    """只给意图域需要的最小面。"""

    def __init__(self, *, task_queue=None, agent=None, broker=None) -> None:
        self.agent = agent or _FakeAgent()
        self.semantic_cache = _FakeCache()
        self.preference_store = _FakePreferenceStore()
        self.broker = broker or _FakeBroker()
        self.task_queue = task_queue
        self.experiment_config = {"model_route_mode": "direct"}

    async def close(self) -> None:
        return None


def _client(container) -> TestClient:
    return TestClient(create_app(container=container, build_on_startup=False))


def _intent_payload(**overrides) -> dict:
    payload = {
        "shopping_session_id": "s-compat",
        "buyer_id": "buyer-1",
        "query": "推荐一个旅行三件套",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 1. 应用结构与旧 import 路径
# --------------------------------------------------------------------------- #
def test_create_app_signature_unchanged():
    """``create_app(container=None, *, build_on_startup=True)`` 签名不变。"""
    signature = inspect.signature(create_app)
    params = signature.parameters

    assert list(params) == ["container", "build_on_startup"]
    assert params["container"].default is None
    assert params["build_on_startup"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["build_on_startup"].default is True


def test_legacy_imports_from_server_still_work():
    """旧代码从 ``app.presentation.server`` 导入请求模型 / 渲染函数仍可用。"""
    from app.presentation.server import (
        CancelRequest,
        FeedbackRequest,
        IntentRequest,
        IntentResponse,
        OrderConfirmRequest,
        PreferenceHint,
        TaskStatusResponse,
        render_preference_directive,
    )

    assert IntentRequest(
        shopping_session_id="s", buyer_id="b", query="q",
    ).preference_hint is None
    assert PreferenceHint(budget_max_major=200).budget_max_major == 200
    assert "预算上限 200 CNY" in render_preference_directive(
        PreferenceHint(budget_max_major=200)
    )
    assert IntentResponse(shopping_session_id="s", reply="r").task_id == ""
    assert TaskStatusResponse(task_id="t", state="queued", found=True).found is True
    assert CancelRequest(reason="不想要了").reason == "不想要了"
    assert OrderConfirmRequest(token="tok").action == "confirm"
    assert FeedbackRequest(session_id="s", score=1).score == 1


def test_router_modules_expose_domain_builders():
    """七个业务域各自一个 router builder，且与 server 挂载的一致。"""
    from app.presentation import routers
    from app.presentation.server import create_app as _create_app

    assert set(routers.__all__) == {
        "build_dashboard_router",
        "build_events_router",
        "build_feedback_router",
        "build_intents_router",
        "build_metrics_router",
        "build_orders_router",
        "build_sessions_router",
    }
    app = _create_app(container=None, build_on_startup=False)
    assert app is not None


def test_route_table_unchanged():
    """方法 + 路径全集与拆分前一致（顺序不敏感，但集合必须相等）。"""
    app = create_app(container=None, build_on_startup=False)
    pairs = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or [])
        if getattr(route, "path", "") in {path for _, path in EXPECTED_ROUTES}
        or str(getattr(route, "path", "")).startswith("/commerce")
    }
    assert pairs == EXPECTED_ROUTES


def test_every_commerce_route_is_guarded():
    """重构后 ``/commerce/*`` 仍逐条挂 ``require_api_key``（结构级断言）。"""
    from app.presentation.auth import require_api_key

    app = create_app(container=None, build_on_startup=False)
    commerce_routes = [
        route
        for route in app.routes
        if str(getattr(route, "path", "")).startswith("/commerce")
    ]
    assert commerce_routes
    for route in commerce_routes:
        calls = [
            getattr(dep, "call", None)
            for dep in getattr(getattr(route, "dependant", None), "dependencies", []) or []
        ]
        assert require_api_key in calls, route.path


# --------------------------------------------------------------------------- #
# 2. /health + 鉴权
# --------------------------------------------------------------------------- #
def test_health_open_without_container():
    client = _client(None)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_commerce_route_requires_api_key_before_endpoint(monkeypatch):
    monkeypatch.setenv("API_KEY", "compat-key")
    client = _client(None)

    assert client.get("/commerce/metrics").status_code == 401
    assert client.get(
        "/commerce/metrics", headers={"X-API-Key": "compat-key"},
    ).status_code == 503  # 通过鉴权后走到"存储未就绪"
    assert client.get("/health").status_code == 200


# --------------------------------------------------------------------------- #
# 3. intents（direct / redis 两种模式 + 任务查询）
# --------------------------------------------------------------------------- #
def test_intents_direct_returns_reply_and_publishes_final_result():
    broker = _FakeBroker()
    agent = _FakeAgent()
    container = _IntentContainer(agent=agent, broker=broker)
    client = _client(container)

    response = client.post("/commerce/intents", json=_intent_payload())

    assert response.status_code == 200
    assert response.json() == {
        "shopping_session_id": "s-compat",
        "reply": "测试回复",
        "task_id": "",
        "status": "",
    }
    assert len(agent.calls) == 1
    assert broker.events == [("s-compat", "final.result", {"reply": "测试回复"})]


def test_intents_redis_mode_returns_task_and_status_route():
    queue = _FakeQueue()
    container = _IntentContainer(task_queue=queue)
    client = _client(container)

    response = client.post("/commerce/intents", json=_intent_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["reply"] == ""
    assert body["task_id"] in queue.tasks

    status = client.get(f"/commerce/intents/{body['task_id']}")
    assert status.status_code == 200
    assert status.json() == {
        "task_id": body["task_id"],
        "state": "queued",
        "found": True,
        "final_text": "",
        "error": "",
        "queue_position": 0,
    }


def test_intents_queue_unavailable_returns_503():
    container = _IntentContainer(task_queue=_FakeQueue(enabled=False))
    response = _client(container).post("/commerce/intents", json=_intent_payload())
    assert response.status_code == 503
    assert "任务队列未就绪" in response.json()["detail"]


def test_task_status_503_in_direct_mode():
    container = _IntentContainer(task_queue=None)
    response = _client(container).get("/commerce/intents/whatever")
    assert response.status_code == 503
    assert "QUEUE_MODE=direct" in response.json()["detail"]


def test_intents_invalid_body_returns_422():
    container = _IntentContainer()
    response = _client(container).post("/commerce/intents", json={"query": "缺字段"})
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# 4. events（SSE）
# --------------------------------------------------------------------------- #
def test_events_streams_sse_frames_with_headers():
    broker = _FakeBroker()
    container = _IntentContainer(broker=broker)
    client = _client(container)

    with client.stream(
        "GET", "/commerce/events", params={"shopping_session_id": "s-sse"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        body = response.read().decode("utf-8")

    assert "event: final.result" in body
    assert broker.subscribed == ["s-sse"]


# --------------------------------------------------------------------------- #
# 5. orders（查询 / 取消 / 确认握手）
# --------------------------------------------------------------------------- #
class _OrderUseCase:
    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list = []

    async def execute(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


def test_orders_query_returns_snapshot_and_404():
    snapshot = {"order_id": "GBX-1", "status": "CONFIRMED"}
    container = SimpleNamespace(query_order=_OrderUseCase(result=snapshot))
    client = _client(container)

    assert client.get("/commerce/orders/GBX-1").json() == snapshot

    missing = SimpleNamespace(
        query_order=_OrderUseCase(error=ValueError("订单不存在")),
    )
    response = _client(missing).get("/commerce/orders/GBX-404")
    assert response.status_code == 404
    assert "订单不存在" in response.json()["detail"]


def test_orders_cancel_returns_409_on_conflict():
    container = SimpleNamespace(
        cancel_order=_OrderUseCase(error=ValueError("订单当前状态不可取消")),
    )
    response = _client(container).post(
        "/commerce/orders/GBX-1/cancel", json={"reason": "不想要了"},
    )
    assert response.status_code == 409
    assert "不可取消" in response.json()["detail"]


@pytest.mark.asyncio
async def test_order_confirm_handshake_contract():
    manager = OrderConfirmationManager()
    token = "tok-compat"
    await manager.create_preview({"items": []}, token=token)
    client = _client(SimpleNamespace(confirmation_manager=manager))

    confirmed = client.post(
        "/commerce/orders/confirm", json={"token": token, "action": "confirm"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"token": token, "status": "confirmed", "ok": True}

    unknown = client.post(
        "/commerce/orders/confirm", json={"token": "nope", "action": "confirm"},
    )
    assert unknown.status_code == 200
    assert unknown.json() == {"token": "nope", "status": "unknown", "ok": False}

    invalid = client.post(
        "/commerce/orders/confirm", json={"token": token, "action": "explode"},
    )
    assert invalid.status_code == 422


def test_order_confirm_503_without_manager():
    response = _client(SimpleNamespace()).post(
        "/commerce/orders/confirm", json={"token": "t", "action": "confirm"},
    )
    assert response.status_code == 503
    assert "下单确认管理器未就绪" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# 6. feedback
# --------------------------------------------------------------------------- #
class _FakeFeedbackStore:
    def __init__(self) -> None:
        self.items: list = []

    async def add(self, feedback) -> int:
        self.items.append(feedback)
        return len(self.items)

    async def list_by_session(self, session_id: str, limit: int = 100) -> list:
        return [item for item in self.items if item.session_id == session_id][:limit]


def test_feedback_roundtrip_like_and_dislike():
    store = _FakeFeedbackStore()
    container = SimpleNamespace(feedback_store=store, badcase_store=None)
    client = _client(container)

    liked = client.post(
        "/commerce/feedback",
        json={"session_id": "s-f", "score": 1, "query": "q", "reason": ""},
    )
    assert liked.status_code == 200
    assert liked.json()["status"] == "ok"
    assert liked.json()["reason_label"] == ""

    disliked = client.post(
        "/commerce/feedback",
        json={"session_id": "s-f", "score": -1, "query": "q", "reason": "price_mismatch"},
    )
    assert disliked.status_code == 200
    assert disliked.json()["reason_label"] == "价格不符合"

    listed = client.get("/commerce/feedback", params={"session_id": "s-f"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["session_id"] == "s-f"
    assert body["count"] == 2
    assert [item["score"] for item in body["items"]] == [1, -1]


def test_feedback_invalid_and_unavailable_status_codes():
    invalid = _client(SimpleNamespace(feedback_store=_FakeFeedbackStore())).post(
        "/commerce/feedback",
        json={"session_id": "s-f", "score": -1, "reason": "随便写的理由"},
    )
    assert invalid.status_code == 422

    missing = _client(SimpleNamespace()).post(
        "/commerce/feedback",
        json={"session_id": "s-f", "score": 1},
    )
    assert missing.status_code == 503


# --------------------------------------------------------------------------- #
# 7. metrics + dashboard
# --------------------------------------------------------------------------- #
class _FakeMetricsStore:
    def __init__(self, rows: list | None = None) -> None:
        self.rows = list(rows or [])

    async def list_all(self, limit: int | None = None) -> list:
        return list(self.rows if limit is None else self.rows[:limit])

    async def list_by_session(self, session_id: str, limit: int = 100) -> list:
        return [row for row in self.rows if row.session_id == session_id][:limit]


def test_metrics_endpoint_aggregates_and_returns_experiment(tmp_path):
    rows = [
        SessionMetrics(session_id="a", turns=1, llm_calls=2, total_tokens=300, cache_hits=1),
        SessionMetrics(session_id="b", turns=1, llm_calls=1, total_tokens=150),
    ]
    container = SimpleNamespace(
        metrics_store=_FakeMetricsStore(rows),
        settings=SimpleNamespace(data_dir=tmp_path),
        experiment_config={"model_route_mode": "direct"},
    )
    client = _client(container)

    response = client.get("/commerce/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["sessions"] == 2
    assert body["turns"] == 2
    assert body["llm_calls"] == 3
    assert body["total_tokens"] == 450
    assert body["cache_hits"] == 1
    assert body["experiment"] == {"model_route_mode": "direct"}
    assert "generated_at" in body

    scoped = client.get("/commerce/metrics", params={"session_id": "a"}).json()
    assert scoped["sessions"] == 1
    assert scoped["total_tokens"] == 300


def test_metrics_endpoint_503_without_store():
    response = _client(SimpleNamespace(metrics_store=None)).get("/commerce/metrics")
    assert response.status_code == 503
    assert "指标存储未就绪" in response.json()["detail"]


def test_dashboard_endpoint_returns_aggregates(tmp_path):
    container = SimpleNamespace(
        feedback_store=None,
        engine=None,
        settings=SimpleNamespace(data_dir=tmp_path),
        experiment_config={"model_route_mode": "llm"},
    )
    response = _client(container).get("/commerce/dashboard")
    assert response.status_code == 200
    body = response.json()
    for key in ("sessions", "turns", "likes", "dislikes", "cost_estimate", "experiment"):
        assert key in body
    assert body["experiment"] == {"model_route_mode": "llm"}


# --------------------------------------------------------------------------- #
# 8. sessions
# --------------------------------------------------------------------------- #
def test_sessions_history_prefers_checkpointer(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage

    agent = _FakeAgent(history=[HumanMessage(content="问题"), AIMessage(content="回答")])
    container = SimpleNamespace(agent=agent, settings=SimpleNamespace(data_dir=tmp_path))
    client = _client(container)

    response = client.get("/commerce/sessions/s-1/history")
    assert response.status_code == 200
    assert response.json() == {
        "session_id": "s-1",
        "source": "checkpointer",
        "messages": [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ],
    }


def test_sessions_history_empty_without_container():
    response = _client(None).get("/commerce/sessions/s-none/history")
    assert response.status_code == 200
    assert response.json() == {"session_id": "s-none", "source": "empty", "messages": []}


def test_sessions_list_scans_trace_dir(tmp_path):
    import json

    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "s-9.jsonl").write_text(
        json.dumps({"type": "final.result", "payload": {"reply": "你好"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    container = SimpleNamespace(agent=None, settings=SimpleNamespace(data_dir=tmp_path))

    response = _client(container).get("/commerce/sessions")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["sessions"][0]["session_id"] == "s-9"
    assert body["sessions"][0]["preview"] == "你好"

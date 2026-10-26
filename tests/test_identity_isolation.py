"""Authenticated principals, cookie sessions and cross-buyer access boundaries."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.presentation.server import create_app

KEYS = {"alice": "alice-personal-key-12345678", "bob": "bob-personal-key-1234567890"}


class OwnerStore:
    def __init__(self):
        self.owners = {"alice-session": "alice", "bob-session": "bob"}

    async def owner(self, session_id):
        return self.owners.get(session_id)

    async def claim(self, session_id, buyer_id):
        self.owners.setdefault(session_id, buyer_id)
        return self.owners[session_id] == buyer_id

    async def list_by_buyer(self, buyer_id):
        return [sid for sid, owner in self.owners.items() if owner == buyer_id]


def container():
    async def find(order_id):
        return SimpleNamespace(buyer_id="bob")

    async def query(order_id):
        return {"order_id": order_id, "buyer_id": "bob"}

    return SimpleNamespace(
        settings=SimpleNamespace(api_key="legacy-shared", buyer_api_keys_json=json.dumps(KEYS)),
        session_ownership=OwnerStore(),
        order_repo=SimpleNamespace(find_by_id=find),
        query_order=SimpleNamespace(execute=query),
    )


@pytest.fixture
def client():
    return TestClient(create_app(container=container(), build_on_startup=False))


def login(client, buyer="alice"):
    response = client.post("/auth/session", json={"api_key": KEYS[buyer]})
    assert response.status_code == 200
    return response


def test_session_discovery_and_cookie_login(client):
    assert client.get("/auth/session").json() == {
        "auth_required": True, "identity_mode": True, "buyer_id": None,
    }
    response = login(client)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/auth/session").json()["buyer_id"] == "alice"
    assert client.delete("/auth/session").status_code == 200
    assert client.get("/auth/session").json()["buyer_id"] is None


def test_identity_mode_rejects_wrong_and_shared_keys(client):
    assert client.post("/auth/session", json={"api_key": "wrong"}).status_code == 401
    response = client.get("/commerce/sessions", headers={"X-API-Key": "legacy-shared"})
    assert response.status_code == 401


def test_cookie_tampering_and_expiration_are_rejected(client, monkeypatch):
    import app.presentation.auth as auth

    login(client)
    original = client.cookies.get("ecommerce_session")
    client.cookies.clear()
    client.cookies.set("ecommerce_session", original[:-1] + ("a" if original[-1] != "a" else "b"))
    assert client.get("/commerce/sessions").status_code == 401
    client.cookies.clear()
    login(client)
    monkeypatch.setattr(auth.time, "time", lambda: 10**12)
    assert client.get("/commerce/sessions").status_code == 401


def test_intent_rejects_forged_buyer_and_foreign_session(client, monkeypatch):
    from app.presentation.routers import intents

    async def run(_container, body):
        return intents.IntentResponse(shopping_session_id=body.shopping_session_id, reply="ok")

    monkeypatch.setattr(intents, "_run_intent_direct", run)
    login(client)
    body = {"shopping_session_id": "new-alice", "buyer_id": "bob", "query": "hello"}
    assert client.post("/commerce/intents", json=body).status_code == 403
    body["buyer_id"] = "alice"
    assert client.post("/commerce/intents", json=body).status_code == 200
    assert client.app.state.container.session_ownership.owners["new-alice"] == "alice"
    body["shopping_session_id"] = "bob-session"
    assert client.post("/commerce/intents", json=body).status_code == 404


def test_foreign_order_and_history_are_hidden(client):
    login(client)
    assert client.get("/commerce/orders/GBX-2").status_code == 404
    assert client.post("/commerce/orders/GBX-2/cancel", json={"reason": "x"}).status_code == 404
    assert client.get("/commerce/sessions/bob-session/history").status_code == 404
    assert client.get("/commerce/feedback?session_id=bob-session").status_code == 404
    assert client.get("/commerce/metrics?session_id=bob-session").status_code == 404


def test_session_list_filters_by_authenticated_owner(client, monkeypatch):
    from app.presentation.routers import sessions

    monkeypatch.setattr(sessions, "_trace_dir", lambda _: None)
    monkeypatch.setattr(sessions, "summarize_trace_sessions", lambda _: [
        {"session_id": "alice-session"}, {"session_id": "bob-session"},
    ])
    login(client)
    response = client.get("/commerce/sessions")
    assert response.status_code == 200
    assert response.json()["sessions"] == [{"session_id": "alice-session"}]


@pytest.mark.parametrize("alias", ["victim..session", "VICTIM_SESSION", "victim:session", "victim."])
def test_session_filename_aliases_cannot_be_claimed(client, monkeypatch, alias):
    from app.presentation.routers import intents

    async def run(_container, body):
        return intents.IntentResponse(shopping_session_id=body.shopping_session_id, reply="ok")

    monkeypatch.setattr(intents, "_run_intent_direct", run)
    login(client)
    response = client.post("/commerce/intents", json={
        "shopping_session_id": alias, "buyer_id": "alice", "query": "hi",
    })
    assert response.status_code == 422


def test_malformed_identity_config_fails_closed():
    c = container()
    c.settings.buyer_api_keys_json = "not-json"
    response = TestClient(create_app(c, build_on_startup=False)).get("/commerce/sessions")
    assert response.status_code == 503


def test_task_status_and_confirmation_cannot_cross_buyer(client):
    async def status(_):
        return SimpleNamespace(task_id="bob-task", state="done", final_text="private", error="", queue_position=0)

    async def owner(_):
        return "bob"

    async def payload(_):
        return {"buyer_id": "bob", "items": []}

    c = client.app.state.container
    c.task_queue = SimpleNamespace(enabled=True, get_status=status, get_task_owner=owner)
    c.confirmation_manager = SimpleNamespace(preview_payload=payload)
    login(client)
    assert client.get("/commerce/intents/bob-task").status_code == 404
    assert client.post("/commerce/orders/confirm", json={"token": "bob-token", "action": "confirm"}).status_code == 404


def test_aggregate_metrics_include_only_owned_sessions(client):
    from app.application.metrics.session_metrics import SessionMetrics

    async def by_session(sid, limit=100):
        return [SessionMetrics(session_id=sid, turns=1)]

    async def all_rows(limit=100):
        pytest.fail("Strict identity mode must not query global metrics")

    client.app.state.container.metrics_store = SimpleNamespace(list_by_session=by_session, list_all=all_rows)
    login(client)
    data = client.get("/commerce/metrics").json()
    assert data["sessions"] == 1
    assert data["recent"][0]["session_id"] == "alice-session"


def test_dashboard_trace_filter_retains_own_data_and_demo_totals(tmp_path):
    from app.presentation.dashboard import summarize_traces

    for buyer in ("alice", "bob"):
        (tmp_path / f"{buyer}-session.jsonl").write_text(
            json.dumps({"type": "final.result", "payload": {"reply": "ok"}}), encoding="utf-8",
        )
    assert summarize_traces(tmp_path)["turns"] == 2
    assert summarize_traces(tmp_path, {"alice-session"})["turns"] == 1
    assert summarize_traces(tmp_path, set())["turns"] == 0


async def test_order_tools_reject_cross_buyer_even_when_agent_guesses_order_id():
    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )
    from tests.test_order_transactions import address, item

    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    place = PlaceOrderUseCase(products, orders)
    created = await place.execute("bob", [item()], address())
    tools = {tool.name: tool for tool in build_order_tools(place, QueryOrderUseCase(orders), CancelOrderUseCase(products, orders), require_confirmation=False)}
    token = ShoppingContext.set(ShoppingContextSnapshot("alice-session", "alice", "zh-CN", "CNY"))
    try:
        queried = await tools["query_order"].ainvoke({"order_id": created["order_id"]})
        cancelled = await tools["cancel_order"].ainvoke({"order_id": created["order_id"], "reason": "x"})
        assert "error" in queried
        assert "error" in cancelled
        assert (await orders.find_by_id(created["order_id"])).status.value == "CONFIRMED"
    finally:
        ShoppingContext.reset(token)

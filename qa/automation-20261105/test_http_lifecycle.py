import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from qa_harness import ALICE_KEY, CONTROL


def intent(c, sid="qa-session", query="qa:create"):
    # Each background request owns its HTTP client; no shared mutable Session.
    import requests

    with requests.Session() as client:
        client.trust_env = False
        return client.post(
            c.base + "/commerce/intents",
            headers={"X-API-Key": ALICE_KEY},
            json={"shopping_session_id": sid, "buyer_id": "alice", "query": query},
            timeout=8,
        )


def decision(c, token, action="confirm", client=None):
    return (client or c.alice).post(
        c.base + "/commerce/orders/confirm", json={"token": token, "action": action}, timeout=3
    )


def create(c, stream, sid="qa-session", action="confirm"):
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(intent, c, sid)
        preview = stream.get("order.preview")
        assert not pending.done()
        assert decision(c, preview["token"], action).status_code == 200
        result = pending.result()
    assert result.status_code == 200, result.text
    return json.loads(result.json()["reply"]), preview


def test_flow_01_create_query_cancel_and_stock(commerce, stream_factory):
    c = commerce
    stream = stream_factory("qa-session")
    order, _ = create(c, stream)
    assert order["verification_status"] == "VERIFIED"
    assert order["status"] == "CONFIRMED" and order["buyer_id"] == "alice"
    assert Decimal(str(order["total_amount_major"])) == sum(
        Decimal(str(x["unit_price_major"])) * x["quantity"] for x in order["lines"]
    )
    assert c.state()["stock"] == 49 and len(c.state()["orders"]) == 1
    fetched = c.alice.get(c.base + "/commerce/orders/" + order["order_id"], timeout=3)
    assert fetched.status_code == 200 and fetched.json()["status"] == "CONFIRMED"
    # Cancel tool also requires its own confirmation; no direct bypass.
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(intent, c, "qa-session", "qa:cancel:" + order["order_id"])
        preview = stream.get("order.preview")
        assert preview["tool"] == "cancel_order"
        assert decision(c, preview["token"]).json()["ok"] is True
        cancelled = json.loads(pending.result().json()["reply"])
    assert cancelled["status"] == "CANCELLED"
    assert c.state()["stock"] == 50
    assert (
        c.alice.get(c.base + "/commerce/orders/" + order["order_id"], timeout=3).json()["status"]
        == "CANCELLED"
    )


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_flow_02_unapproved_has_no_order_or_stock_effect(commerce, stream_factory, mode):
    c = commerce
    stream = stream_factory("qa-session")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(intent, c)
        preview = stream.get("order.preview")
        if mode == "cancel":
            assert decision(c, preview["token"], "cancel").json()["ok"] is True
        response = pending.result()
    body = json.loads(response.json()["reply"])
    assert body["executed"] is False
    assert body["reason"] == ("user_cancelled" if mode == "cancel" else "confirmation_timeout")
    assert c.state()["orders"] == [] and c.state()["stock"] == 50
    assert decision(c, preview["token"]).status_code == 404


def test_flow_03_same_session_retry_returns_same_order(commerce, stream_factory):
    c = commerce
    stream = stream_factory("qa-session")
    first, _ = create(c, stream)
    replay, _ = create(c, stream)
    assert replay["order_id"] == first["order_id"] and replay["idempotent_replay"] is True
    assert len(c.state()["orders"]) == 1 and c.state()["stock"] == 49


def test_flow_04_other_buyer_cannot_confirm_query_cancel_or_read_session(commerce, stream_factory):
    c = commerce
    stream = stream_factory("qa-session")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(intent, c)
        token = stream.get("order.preview")["token"]
        assert decision(c, token, client=c.bob).status_code == 404
        assert c.state()["orders"] == []
        assert decision(c, token).json()["ok"] is True
        order = json.loads(pending.result().json()["reply"])
    oid = order["order_id"]
    assert c.bob.get(c.base + "/commerce/orders/" + oid, timeout=3).status_code == 404
    assert (
        c.bob.post(
            c.base + f"/commerce/orders/{oid}/cancel", json={"reason": "foreign"}, timeout=3
        ).status_code
        == 404
    )
    assert c.bob.get(c.base + "/commerce/sessions/qa-session/history", timeout=3).status_code == 404
    assert c.state()["orders"][0]["status"] == "CONFIRMED" and c.state()["stock"] == 49


def test_flow_05_write_failure_rollback_then_safe_retry(commerce, stream_factory):
    c = commerce
    stream = stream_factory("qa-session")
    assert (
        c.alice.post(
            c.base + "/__qa/fault", headers={"X-QA-Control": CONTROL}, timeout=3
        ).status_code
        == 200
    )
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(intent, c)
        assert decision(c, stream.get("order.preview")["token"]).status_code == 200
        response = pending.result()
    assert response.status_code == 500
    assert c.state()["orders"] == [] and c.state()["stock"] == 50
    result, _ = create(c, stream)
    assert result["verification_status"] == "VERIFIED"
    assert len(c.state()["orders"]) == 1 and c.state()["stock"] == 49


def test_flow_06_cookie_login_logout_and_identity_mismatch(commerce):
    c = commerce
    # No X-API-Key: exercise actual server-issued HttpOnly cookie authentication.
    c.alice.headers.pop("X-API-Key")
    assert (
        c.alice.post(c.base + "/auth/session", json={"api_key": "wrong"}, timeout=3).status_code
        == 401
    )
    login = c.alice.post(c.base + "/auth/session", json={"api_key": ALICE_KEY}, timeout=3)
    assert login.json()["buyer_id"] == "alice"
    assert (
        "HttpOnly" in login.headers["set-cookie"]
        and "SameSite=strict" in login.headers["set-cookie"]
    )
    assert c.alice.get(c.base + "/auth/session", timeout=3).json()["buyer_id"] == "alice"
    response = c.alice.post(
        c.base + "/commerce/intents",
        json={"buyer_id": "bob", "shopping_session_id": "qa-session", "query": "qa:create"},
        timeout=3,
    )
    assert response.status_code == 403
    assert c.alice.delete(c.base + "/auth/session", timeout=3).status_code == 200
    assert c.alice.get(c.base + "/commerce/sessions", timeout=3).status_code == 401


@pytest.mark.parametrize("sid", ["../private", "UPPER", "has:colon"])
def test_flow_07_session_aliases_rejected(commerce, sid):
    response = commerce.alice.post(
        commerce.base + "/commerce/intents",
        json={"buyer_id": "alice", "shopping_session_id": sid, "query": "qa:create"},
        timeout=3,
    )
    assert response.status_code == 422
    assert commerce.state()["orders"] == []


def test_flow_08_sse_reconnect_receives_next_turn(commerce, stream_factory):
    c = commerce
    first = stream_factory("qa-session")
    result, _ = create(c, first)
    # Close the first actual HTTP connection; reconnect with a fresh client.
    first.close()
    second = type(first)(c, "qa-session")
    try:
        replay, _ = create(c, second)
        assert replay["order_id"] == result["order_id"]
        assert len(c.state()["orders"]) == 1
    finally:
        second.close()

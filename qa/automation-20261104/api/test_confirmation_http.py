"""Contract, boundary and state-transition cases; assertions use public HTTP."""

import re

import pytest

PATH = "/commerce/orders/confirm"


def post(api, payload, **kwargs):
    return api.http.post(api.base + PATH, json=payload, timeout=(2, 5), **kwargs)


def test_api_01_health(api_factory):
    api = api_factory()
    response = api.http.get(api.base + "/health", timeout=(2, 5))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("action,status", [("confirm", "confirmed"), ("cancel", "cancelled")])
def test_api_02_decision(api_factory, action, status):
    response = post(api_factory(), {"token": "qa-token", "action": action})
    assert response.status_code == 200
    assert response.json() == {"token": "qa-token", "status": status, "ok": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "qa-token"},
        {"token": "qa-token", "action": " CONFIRM "},
    ],
)
def test_api_03_default_and_normalization(api_factory, payload):
    response = post(api_factory(), payload)
    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"action": "confirm"},
        {"token": None},
        {"token": 4},
        {"token": "qa-token", "action": []},
        {"token": "qa-token", "action": "delete"},
        {"token": "qa-token", "action": ""},
        {"token": "qa-token", "action": None},
    ],
    ids=[
        "empty",
        "missing-token",
        "null-token",
        "numeric-token",
        "list-action",
        "illegal-action",
        "empty-action",
        "null-action",
    ],
)
def test_api_04_invalid_partition(api_factory, payload):
    api = api_factory()
    response = post(api, payload)
    assert response.status_code == 422
    assert "detail" in response.json()
    # Invalid input must not consume or resolve the valid preview.
    valid = post(api, {"token": "qa-token", "action": "cancel"})
    assert valid.json()["status"] == "cancelled"


@pytest.mark.parametrize("token", ["missing-token", ""])
def test_api_05_unknown_token(api_factory, token):
    response = post(api_factory(), {"token": token, "action": "confirm"})
    assert response.status_code == 200
    assert response.json() == {"token": token, "status": "unknown", "ok": False}


@pytest.mark.parametrize(
    "first,second,status",
    [
        ("confirm", "confirm", "confirmed"),
        ("confirm", "cancel", "confirmed"),
        ("cancel", "confirm", "cancelled"),
    ],
)
def test_api_06_first_decision_wins(api_factory, first, second, status):
    api = api_factory()
    original = post(api, {"token": "qa-token", "action": first})
    replay = post(api, {"token": "qa-token", "action": second})
    assert original.status_code == replay.status_code == 200
    assert original.json() == replay.json() == {"token": "qa-token", "status": status, "ok": True}


def test_api_07_dependency_unavailable(api_factory):
    response = post(api_factory(missing_manager=True), {"token": "qa-token"})
    assert response.status_code == 503
    assert "未就绪" in response.json()["detail"]


@pytest.mark.parametrize(
    "request_id,retained", [("a", True), ("a" * 128, True), ("a" * 129, False)]
)
def test_api_08_request_id_boundary(api_factory, request_id, retained):
    response = post(api_factory(), {"token": "qa-token"}, headers={"X-Request-ID": request_id})
    assert response.status_code == 200
    actual = response.headers["X-Request-ID"]
    if retained:
        assert actual == request_id
    else:
        assert re.fullmatch(r"[a-f0-9]{32}", actual)


@pytest.mark.parametrize(
    "key,status",
    [
        (None, 401),
        ("invalid-key", 401),
        ("qa-bob-key-0000001", 404),
        ("qa-alice-key-000001", 200),
    ],
    ids=["missing-credential", "wrong-credential", "other-owner", "correct-owner"],
)
def test_api_09_authorization(api_factory, key, status):
    api = api_factory(authenticated=True)
    response = post(api, {"token": "qa-token"}, headers={"X-API-Key": key} if key else {})
    assert response.status_code == status
    if status == 200:
        assert response.json()["status"] == "confirmed"
    else:
        # The rejected request must not prevent the owner making their decision.
        owner = post(
            api,
            {"token": "qa-token", "action": "cancel"},
            headers={"X-API-Key": "qa-alice-key-000001"},
        )
        assert owner.json()["status"] == "cancelled"

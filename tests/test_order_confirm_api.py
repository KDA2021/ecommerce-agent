# -*- coding: utf-8 -*-
"""POST /commerce/orders/confirm：下单确认握手的 REST 入口。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.application.order_confirmation import OrderConfirmationManager
from app.presentation.server import create_app


class _Container:
    """最小容器替身：路由只需要 ``confirmation_manager``。"""

    def __init__(self, manager) -> None:
        self.confirmation_manager = manager


def _client(manager=None) -> TestClient:
    return TestClient(
        create_app(container=_Container(manager), build_on_startup=False),
    )


@pytest.mark.asyncio
async def test_confirm_endpoint_resolves_token_and_returns_status():
    """POST {token, action=confirm} → resolve → confirmed。"""
    manager = OrderConfirmationManager()
    token = "tok-rest-confirm"
    await manager.create_preview({"items": []}, token=token)
    client = _client(manager)

    response = client.post(
        "/commerce/orders/confirm",
        json={"token": token, "action": "confirm"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"token": token, "status": "confirmed", "ok": True}
    assert await manager.status(token) == "confirmed"


@pytest.mark.asyncio
async def test_cancel_endpoint_marks_cancelled():
    """action=cancel → cancelled 且 ok=True（不会下单）。"""
    manager = OrderConfirmationManager()
    token = await manager.create_preview({"items": []})
    client = _client(manager)

    response = client.post(
        "/commerce/orders/confirm",
        json={"token": token, "action": "cancel"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"
    assert response.json()["ok"] is True


def test_confirm_endpoint_unknown_token_returns_ok_false():
    """未知 / 过期 token 返回 unknown + ok=False，不抛 500。"""
    client = _client(OrderConfirmationManager())
    response = client.post(
        "/commerce/orders/confirm",
        json={"token": "no-such-token", "action": "confirm"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"token": "no-such-token", "status": "unknown", "ok": False}


@pytest.mark.asyncio
async def test_confirm_endpoint_rejects_illegal_action_with_422():
    """非法 action 由管理器抛 ValueError，路由转 422。"""
    manager = OrderConfirmationManager()
    token = await manager.create_preview({"items": []})
    client = _client(manager)
    response = client.post(
        "/commerce/orders/confirm",
        json={"token": token, "action": "delete"},
    )
    assert response.status_code == 422, response.text


def test_confirm_endpoint_without_manager_returns_503():
    """未装配确认管理器的容器（旧部署 / 测试替身）返回 503 而不是 500。"""
    client = _client(None)
    response = client.post(
        "/commerce/orders/confirm",
        json={"token": "tok", "action": "confirm"},
    )
    assert response.status_code == 503, response.text

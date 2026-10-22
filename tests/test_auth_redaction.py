# -*- coding: utf-8 -*-
"""P11-3：API Key 鉴权（401 / 放行 / 关闭）+ trace query/reply 脱敏。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbroker import EventBroker
from app.infrastructure.tracing import LocalTraceLogger

API_KEY = "p11-secret-key"


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #


class _FakeQueryOrder:
    """最小订单查询 usecase：返回回显 order_id 的快照。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, order_id: str) -> dict:
        self.calls.append(order_id)
        return {"order_id": order_id, "status": "PAID"}


def _container(settings: object | None = None) -> SimpleNamespace:
    """构造只带 ``query_order`` 的容器替身。"""
    container = SimpleNamespace(query_order=_FakeQueryOrder())
    if settings is not None:
        container.settings = settings
    return container


def _client(container=None) -> TestClient:
    from app.presentation.server import create_app

    return TestClient(create_app(container=container, build_on_startup=False))


def _hashed(text: str) -> dict:
    """独立计算期望的脱敏标记（不依赖生产代码，避免自证）。"""
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "length": len(text),
    }


def _trace_lines(tmp_path, session_id: str = "s1") -> list[dict]:
    path = tmp_path / "traces" / f"{session_id}.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


# --------------------------------------------------------------------------- #
# 1. Settings
# --------------------------------------------------------------------------- #


def test_settings_api_key_defaults_to_empty(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.delenv("API_KEY", raising=False)
    assert load_settings().api_key == ""


def test_settings_api_key_reads_env(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("API_KEY", API_KEY)
    assert load_settings().api_key == API_KEY


# --------------------------------------------------------------------------- #
# 2. 鉴权：401 / 放行 / 关闭
# --------------------------------------------------------------------------- #


def test_missing_api_key_returns_401(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    client = _client(_container())

    response = client.get("/commerce/orders/GBX-1")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "X-API-Key"


def test_wrong_api_key_returns_401(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    client = _client(_container())

    response = client.get("/commerce/orders/GBX-1", headers={"X-API-Key": "wrong"})

    assert response.status_code == 401


def test_header_api_key_allows_commerce_route(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    container = _container()
    client = _client(container)

    response = client.get("/commerce/orders/GBX-1", headers={"X-API-Key": API_KEY})

    assert response.status_code == 200
    assert response.json()["order_id"] == "GBX-1"
    assert container.query_order.calls == ["GBX-1"]


def test_query_api_key_allows_commerce_route(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    container = _container()
    client = _client(container)

    response = client.get(f"/commerce/orders/GBX-2?api_key={API_KEY}")

    assert response.status_code == 200
    assert response.json()["order_id"] == "GBX-2"
    assert container.query_order.calls == ["GBX-2"]


def test_wrong_query_api_key_returns_401(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    client = _client(_container())

    response = client.get("/commerce/orders/GBX-3?api_key=nope")

    assert response.status_code == 401


def test_health_and_docs_stay_open_when_auth_enabled(monkeypatch):
    monkeypatch.setenv("API_KEY", API_KEY)
    client = _client(_container())

    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_auth_disabled_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    client = _client(_container())

    response = client.get("/commerce/orders/GBX-4")

    assert response.status_code == 200
    assert response.json()["order_id"] == "GBX-4"


def test_auth_disabled_when_api_key_blank(monkeypatch):
    monkeypatch.setenv("API_KEY", "")
    client = _client(_container())

    assert client.get("/commerce/orders/GBX-5").status_code == 200


def test_container_settings_api_key_takes_precedence(monkeypatch):
    """生产路径：容器已装配 Settings 时以它为准（不被环境变量覆盖）。"""
    monkeypatch.setenv("API_KEY", "env-key")
    container = _container(SimpleNamespace(api_key="container-key"))
    client = _client(container)

    assert client.get("/commerce/orders/GBX-6", headers={"X-API-Key": "env-key"}).status_code == 401
    assert (
        client.get("/commerce/orders/GBX-6", headers={"X-API-Key": "container-key"}).status_code
        == 200
    )


def test_container_settings_without_api_key_falls_back_to_env(monkeypatch):
    """旧测试替身 / 自定义 Settings：没有 api_key 字段时回退环境变量。"""
    monkeypatch.setenv("API_KEY", API_KEY)
    container = _container(SimpleNamespace(redis_url=""))
    client = _client(container)

    assert client.get("/commerce/orders/GBX-7").status_code == 401
    assert (
        client.get("/commerce/orders/GBX-7", headers={"X-API-Key": API_KEY}).status_code
        == 200
    )


def _route_has_api_key_dependency(route) -> bool:
    from app.presentation.auth import require_api_key

    dependant = getattr(route, "dependant", None)
    for dependency in getattr(dependant, "dependencies", []) or []:
        if getattr(dependency, "call", None) is require_api_key:
            return True
    return False


def test_every_commerce_route_is_guarded(monkeypatch):
    """/commerce/* 全覆盖：新加路由忘了挂依赖会在这里失败。"""
    from app.presentation.server import create_app

    app = create_app(container=_container(), build_on_startup=False)
    commerce_routes = [
        route
        for route in app.routes
        if str(getattr(route, "path", "")).startswith("/commerce")
    ]

    assert commerce_routes, "应至少注册一个 /commerce 路由"
    unguarded = [
        route.path for route in commerce_routes if not _route_has_api_key_dependency(route)
    ]
    assert unguarded == []


def test_commerce_routes_return_401_before_endpoint(monkeypatch):
    """未带 key 时直接 401，不进入业务逻辑（也不会因容器缺能力返回 500）。"""
    from app.presentation.server import create_app

    monkeypatch.setenv("API_KEY", API_KEY)
    client = TestClient(create_app(container=None, build_on_startup=False))

    assert client.get("/commerce/orders/GBX-1").status_code == 401
    assert client.get("/commerce/feedback", params={"session_id": "s"}).status_code == 401
    assert client.get("/commerce/metrics").status_code == 401
    assert client.get("/commerce/dashboard").status_code == 401
    assert client.get("/commerce/sessions").status_code == 401
    assert client.get("/commerce/events", params={"shopping_session_id": "s"}).status_code == 401


# --------------------------------------------------------------------------- #
# 3. trace 脱敏
# --------------------------------------------------------------------------- #


def test_trace_hashes_query_and_reply_without_plaintext(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    secret_query = "推荐一个旅行三件套（明文问题）"
    secret_matched = "推荐旅行三件套"
    secret_reply = "给你推荐 Nomadica（明文回复）"

    logger.record_nowait(
        "s1",
        "trace-1",
        "cache.hit",
        {"query": secret_query, "matched_query": secret_matched, "similarity": 0.93},
    )
    logger.record_nowait("s1", "trace-1", "final.result", {"reply": secret_reply})

    raw = (tmp_path / "traces" / "s1.jsonl").read_text(encoding="utf-8")
    assert secret_query not in raw
    assert secret_matched not in raw
    assert secret_reply not in raw

    lines = _trace_lines(tmp_path)
    assert lines[0]["payload"]["query"] == _hashed(secret_query)
    assert lines[0]["payload"]["matched_query"] == _hashed(secret_matched)
    # 结构化字段保留。
    assert lines[0]["payload"]["similarity"] == 0.93
    assert lines[1]["payload"]["reply"] == _hashed(secret_reply)


async def test_trace_redacts_nested_tool_args(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    secret = "帮我找防水登山鞋"

    await logger.record(
        "s1",
        "t",
        "tool.invoke",
        {"tool": "product_search", "args": {"query": secret, "top_k": 5}},
    )

    payload = _trace_lines(tmp_path)[0]["payload"]
    assert payload["tool"] == "product_search"
    assert payload["args"]["query"] == _hashed(secret)
    assert payload["args"]["top_k"] == 5


async def test_trace_redaction_does_not_mutate_caller_payload(tmp_path):
    """同一个 payload 还要走 SSE，脱敏必须只作用于落盘副本。"""
    logger = LocalTraceLogger(tmp_path)
    payload = {"reply": "明文回复", "trace_id": "t1"}

    await logger.record("s1", "t1", "final.result", payload)

    assert payload == {"reply": "明文回复", "trace_id": "t1"}


async def test_event_broker_keeps_sse_plaintext_but_redacts_trace(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    broker = EventBroker(trace_logger=logger)
    secret_reply = "SSE 明文回复"

    stream = broker.subscribe("s1")
    frame_future = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s1",
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-9",
        )
    )
    try:
        broker.publish("s1", "final.result", {"reply": secret_reply})
        frame = await asyncio.wait_for(frame_future, timeout=2)
    finally:
        await stream.aclose()
        ShoppingContext.reset(token)

    # SSE 客户端仍要拿到明文。
    assert secret_reply in frame
    # trace 里只留哈希与长度。
    raw = (tmp_path / "traces" / "s1.jsonl").read_text(encoding="utf-8")
    assert secret_reply not in raw
    assert json.loads(raw)["payload"]["reply"] == _hashed(secret_reply)


async def test_span_attrs_redacted_but_structured_fields_kept(tmp_path):
    logger = LocalTraceLogger(tmp_path)
    secret = "旅行三件套"
    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s1",
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-1",
        )
    )
    try:
        async with logger.span(
            "product_search",
            {"query": secret, "tool": "product_search", "candidate_count": 3},
        ):
            pass
    finally:
        ShoppingContext.reset(token)

    line = _trace_lines(tmp_path)[0]
    assert line["name"] == "product_search"
    assert line["status"] == "ok"
    assert line["attrs"]["tool"] == "product_search"
    assert line["attrs"]["candidate_count"] == 3
    assert line["attrs"]["query"] == _hashed(secret)

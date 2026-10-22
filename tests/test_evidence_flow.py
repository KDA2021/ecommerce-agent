# -*- coding: utf-8 -*-
"""P8-2 可信执行：幂等下单、任务证据、完成声明门控。"""
from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.application.evidence.completion_guard import (
    SAFE_COMPLETION_TEXT,
    CompletionGuard,
)
from app.application.evidence.task_state import (
    InMemoryTaskStateStore,
    TaskRecord,
    TaskStatus,
)
from app.application.tools.order_tools import build_order_tools
from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    OrderItemInput,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.domain.order.address import Address
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryOrderRepository,
    InMemoryProductRepository,
)


def _address() -> Address:
    return Address(
        recipient_name="张三",
        country="CN",
        state="浙江",
        city="杭州",
        address_line="西湖区某路 1 号",
        postal_code="310000",
        phone="13800000000",
    )


async def test_place_order_replays_same_idempotency_key_without_second_order():
    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    place = PlaceOrderUseCase(product_repo, order_repo)

    first = await place.execute(
        buyer_id="buyer-1",
        items=[OrderItemInput(product_id="P1001", sku_id="P1001-S1", quantity=1)],
        shipping_address=_address(),
        idempotency_key="key-1",
    )
    second = await place.execute(
        buyer_id="buyer-1",
        items=[OrderItemInput(product_id="P1001", sku_id="P1001-S1", quantity=1)],
        shipping_address=_address(),
        idempotency_key="key-1",
    )

    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert second["order_id"] == first["order_id"]
    assert len(order_repo._orders) == 1
    product = await product_repo.find_by_id("P1001")
    assert product.find_sku("P1001-S1").stock == 49


async def test_task_store_leases_and_fences_old_generation():
    store = InMemoryTaskStateStore()
    await store.put(
        TaskRecord(
            task_id="task-1",
            trace_id="trace-1",
            action="create_order",
            generation=1,
            status=TaskStatus.EXECUTED,
            idempotency_key="key-1",
            result_json='{"order_id": "GBX-000001"}',
        )
    )

    assert (await store.get("task-1")).generation == 1
    assert await store.lease("task-1", 2, lease_seconds=30.0) is True
    assert await store.complete("task-1", 1, status=TaskStatus.VERIFIED) is False
    assert (
        await store.complete(
            "task-1",
            2,
            status=TaskStatus.VERIFIED,
            result_json='{"order_id": "GBX-000001"}',
        )
        is True
    )

    verified = await store.list_verified_order_ids("trace-1")
    assert verified == ["GBX-000001"]
    records = await store.list_by_trace("trace-1")
    assert records[0].generation == 2
    assert records[0].status is TaskStatus.VERIFIED


async def test_task_store_put_ignores_older_generation():
    store = InMemoryTaskStateStore()
    await store.put(
        TaskRecord(
            task_id="task-2",
            trace_id="trace-2",
            action="subagent",
            generation=3,
            status=TaskStatus.EXECUTED,
            idempotency_key="key-2",
        )
    )
    await store.put(
        TaskRecord(
            task_id="task-2",
            trace_id="trace-2",
            action="subagent",
            generation=2,
            status=TaskStatus.FAILED,
            idempotency_key="key-2",
        )
    )

    record = await store.get("task-2")
    assert record.generation == 3
    assert record.status is TaskStatus.EXECUTED


class _RecordingTaskStore(InMemoryTaskStateStore):
    """记录 put/complete 调用顺序，用于断言 EXECUTED → VERIFIED。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, TaskStatus]] = []

    async def put(self, record: TaskRecord) -> None:
        self.calls.append(("put", record.status))
        await super().put(record)

    async def complete(
        self,
        task_id: str,
        generation: int,
        status: TaskStatus = TaskStatus.VERIFIED,
        result_json: str | None = None,
    ) -> bool:
        self.calls.append(("complete", status))
        return await super().complete(task_id, generation, status=status, result_json=result_json)


async def test_create_order_tool_replays_and_records_verified_evidence():
    product_repo = InMemoryProductRepository()
    order_repo = InMemoryOrderRepository()
    task_store = _RecordingTaskStore()
    place = PlaceOrderUseCase(product_repo, order_repo)
    query = QueryOrderUseCase(order_repo)
    cancel = CancelOrderUseCase(product_repo, order_repo)
    tools = build_order_tools(
        place,
        query,
        cancel,
        task_store=task_store,
        task_id_prefix="order",
    )
    create = next(tool for tool in tools if tool.name == "create_order")

    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-evidence",
            buyer_id="buyer-1",
            locale="zh-CN",
            currency="CNY",
            trace_id="trace-evidence-1",
        )
    )
    try:
        first = await create.ainvoke(
            {
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                "shipping_address": {
                    "recipient_name": "张三",
                    "country": "CN",
                    "city": "杭州",
                    "address_line": "某路 1 号",
                },
            }
        )
        second = await create.ainvoke(
            {
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                "shipping_address": {
                    "recipient_name": "张三",
                    "country": "CN",
                    "city": "杭州",
                    "address_line": "某路 1 号",
                },
            }
        )
    finally:
        ShoppingContext.reset(token)

    assert first["verification_status"] == "VERIFIED"
    assert first["idempotent_replay"] is False
    assert second["verification_status"] == "VERIFIED"
    assert second["idempotent_replay"] is True
    assert second["order_id"] == first["order_id"]
    assert len(order_repo._orders) == 1
    product = await product_repo.find_by_id("P1001")
    assert product.find_sku("P1001-S1").stock == 49

    assert task_store.calls[0] == ("put", TaskStatus.EXECUTED)
    assert task_store.calls[1] == ("complete", TaskStatus.VERIFIED)
    records = await task_store.list_by_trace("trace-evidence-1", TaskStatus.VERIFIED)
    assert records
    assert all(record.task_id.startswith("order:") for record in records)
    assert records[0].idempotency_key


def test_completion_guard_blocks_unverified_completion_claim():
    reply = "已完成下单，订单号 GBX-000001。"

    guarded = CompletionGuard.guard(reply, [])

    assert guarded == SAFE_COMPLETION_TEXT
    assert "GBX-000001" not in guarded


def test_completion_guard_blocks_variant_chinese_completion_claim():
    reply = "已经完成下单，订单号 GBX-000001。"

    assert CompletionGuard.guard(reply, []) == SAFE_COMPLETION_TEXT


def test_completion_guard_allows_verified_completion_claim():
    reply = "已完成下单，订单号 GBX-000001。"

    assert CompletionGuard.guard(reply, ["GBX-000001"]) == reply


def test_completion_guard_leaves_non_completion_reply_untouched():
    reply = "我建议你选择这款抗造轻便的旅行三件套。"

    assert CompletionGuard.guard(reply, []) == reply


class _GuardCache:
    async def lookup(self, buyer_id, query, has_history, scope=""):
        return None

    async def remember(self, buyer_id, query, reply, has_history, scope=""):
        return None


class _GuardPreferenceStore:
    async def list_by_buyer(self, buyer_id):
        return []


class _GuardBroker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, session_id, event_type, payload):
        self.events.append((session_id, event_type, payload))


class _GuardAgent:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def ainvoke(self, payload, config):
        return {"messages": [AIMessage(content=self.reply)]}

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": []})


class _EvidenceTaskStore:
    def __init__(self, verified: list[str] | None = None) -> None:
        self.verified = list(verified or [])
        self.trace_ids: list[str] = []

    async def verified_order_ids(self, trace_id: str) -> list[str]:
        self.trace_ids.append(trace_id)
        return list(self.verified)


def _guard_client(task_store, reply: str) -> TestClient:
    from app.presentation.server import create_app

    container = SimpleNamespace(
        agent=_GuardAgent(reply),
        semantic_cache=_GuardCache(),
        broker=_GuardBroker(),
        preference_store=_GuardPreferenceStore(),
        task_store=task_store,
    )
    return TestClient(create_app(container=container, build_on_startup=False))


def _post_guard_intent(client: TestClient):
    return client.post(
        "/commerce/intents",
        json={
            "shopping_session_id": "s-guard",
            "buyer_id": "buyer-1",
            "query": "帮我下单旅行三件套",
        },
    )


def test_server_blocks_completion_claim_without_trace_evidence():
    task_store = _EvidenceTaskStore()
    client = _guard_client(task_store, "已完成下单，订单号 GBX-000001。")

    response = _post_guard_intent(client)

    assert response.status_code == 200
    assert response.json()["reply"] == SAFE_COMPLETION_TEXT
    assert task_store.trace_ids
    assert task_store.trace_ids[-1]


def test_server_allows_completion_claim_with_trace_evidence():
    task_store = _EvidenceTaskStore(verified=["GBX-000001"])
    reply = "已完成下单，订单号 GBX-000001。"
    client = _guard_client(task_store, reply)

    response = _post_guard_intent(client)

    assert response.status_code == 200
    assert response.json()["reply"] == reply


@pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"),
    reason="TEST_MYSQL_URL 未设置，跳过 MySQL task store 集成测试",
)
async def test_mysql_task_store_generation_fencing_and_verified_orders():
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine
    from app.infrastructure.persistence.mysql_task_store import MySqlTaskStateStore

    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    await bootstrap_schema(engine)
    store = MySqlTaskStateStore(engine)
    task_id = f"test-task-{uuid.uuid4().hex}"
    trace_id = f"test-trace-{uuid.uuid4().hex}"
    try:
        await store.put(
            TaskRecord(
                task_id=task_id,
                trace_id=trace_id,
                action="create_order",
                generation=1,
                status=TaskStatus.EXECUTED,
                idempotency_key="mysql-key",
                result_json='{"order_id": "GBX-MYSQL"}',
            )
        )
        assert (await store.get(task_id)).status is TaskStatus.EXECUTED
        assert await store.lease(task_id, 2, lease_seconds=30.0) is True
        assert await store.complete(task_id, 1, status=TaskStatus.VERIFIED) is False
        assert (
            await store.complete(
                task_id,
                2,
                status=TaskStatus.VERIFIED,
                result_json='{"order_id": "GBX-MYSQL"}',
            )
            is True
        )
        assert await store.list_verified_order_ids(trace_id) == ["GBX-MYSQL"]
    finally:
        await engine.dispose()






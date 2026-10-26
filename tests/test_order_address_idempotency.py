"""Approved address changes must not replay a previous order from Redis."""
import asyncio
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

from app.application.order_confirmation import OrderConfirmationManager
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
from app.infrastructure.redis_cache import RedisCache


async def test_omitted_address_defaults_replay_the_same_approved_order():
    """Equivalent parsed addresses share one key despite JSON ordering/defaults."""
    from app.application.tools.order_tools import _idempotency_key
    from app.application.usecases.order_usecases import OrderItemInput

    items = [OrderItemInput("P1001", "P1001-S1", 1)]
    minimal = {"recipient_name": "Buyer", "country": "CN", "city": "HZ", "address_line": "1 Road"}
    explicit = {"phone": "", "postal_code": "", "state": "", **dict(reversed(list(minimal.items())))}
    assert _idempotency_key("alice", items, "session", minimal) == _idempotency_key(
        "alice", items, "session", explicit,
    )


@pytest.mark.parametrize("field,value", [
    ("recipient_name", "Other Buyer"),
    ("country", "US"),
    ("state", "Other State"),
    ("city", "Other City"),
    ("address_line", "2 New Road"),
    ("postal_code", "999999"),
    ("phone", "456"),
])
async def test_approved_address_change_uses_new_order_and_exact_retry_replays(field, value):
    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    manager = OrderConfirmationManager()
    previews = asyncio.Queue()

    class Broker:
        def publish(self, session_id, kind, payload):
            if kind == "order.preview":
                previews.put_nowait(payload)

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis = RedisCache("", client=client)
    tools = build_order_tools(
        PlaceOrderUseCase(products, orders), QueryOrderUseCase(orders),
        CancelOrderUseCase(products, orders), broker=Broker(), redis_cache=redis,
        confirmation_manager=manager,
        settings=SimpleNamespace(order_confirm_timeout_seconds=2),
    )
    create = next(tool for tool in tools if tool.name == "create_order")
    address = {
        "recipient_name": "Buyer", "country": "CN", "state": "ZJ", "city": "HZ",
        "address_line": "1 Old Road", "postal_code": "310000", "phone": "123",
    }

    async def approved_create(shipping):
        task = asyncio.create_task(create.ainvoke({
            "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
            "shipping_address": shipping,
        }))
        preview = await asyncio.wait_for(previews.get(), 1)
        assert preview["preview"]["shipping_address"] == shipping
        assert await manager.resolve(preview["token"], "confirm") == "confirmed"
        return await asyncio.wait_for(task, 1)

    context = ShoppingContext.set(ShoppingContextSnapshot("same-session", "alice", "zh", "CNY"))
    try:
        stock_before = (await products.find_by_id("P1001")).primary_sku().stock
        first = await approved_create(address)
        changed = {**address, field: value}
        second = await approved_create(changed)
        assert second["order_id"] != first["order_id"]
        assert second["idempotent_replay"] is False
        assert (await orders.find_by_id(second["order_id"])).shipping_address.__dict__ == changed
        assert (await products.find_by_id("P1001")).primary_sku().stock == stock_before - 2

        replay = await approved_create(changed)
        assert replay["order_id"] == second["order_id"]
        assert replay["shipping_address"] == second["shipping_address"]
        assert replay["idempotent_replay"] is True
        assert (await products.find_by_id("P1001")).primary_sku().stock == stock_before - 2
    finally:
        ShoppingContext.reset(context)
        await client.aclose()

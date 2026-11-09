import asyncio
from decimal import Decimal

import pytest

from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    OrderItemInput,
    PlaceOrderUseCase,
)
from app.domain.order.address import Address
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryOrderRepository,
    InMemoryProductRepository,
)

ADDRESS = Address("QA Buyer", "CN", "GD", "Guangzhou", "QA Road", "", "")


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
async def test_business_01_invalid_quantity_does_not_mutate_state(quantity):
    p, o = InMemoryProductRepository(), InMemoryOrderRepository()
    with pytest.raises(ValueError):
        await PlaceOrderUseCase(p, o).execute(
            "alice", [OrderItemInput("P1001", "P1001-S1", quantity)], ADDRESS, "qa-invalid"
        )
    assert (await p.find_by_id("P1001")).primary_sku().stock == 50
    assert await o.find_by_idempotency_key("qa-invalid") is None


@pytest.mark.parametrize(
    "second",
    [
        OrderItemInput("missing", "missing", 1),
        OrderItemInput("P1001", "missing", 1),
        OrderItemInput("P1001", "P1001-S1", 51),
    ],
)
async def test_business_02_later_line_failure_rolls_back_earlier_deduction(second):
    p, o = InMemoryProductRepository(), InMemoryOrderRepository()
    with pytest.raises(ValueError):
        await PlaceOrderUseCase(p, o).execute(
            "alice", [OrderItemInput("P1001", "P1001-S1", 1), second], ADDRESS, "qa-partial"
        )
    assert (await p.find_by_id("P1001")).primary_sku().stock == 50
    assert await o.find_by_idempotency_key("qa-partial") is None


async def test_business_03_duplicate_lines_amount_and_cancel_stock_conservation():
    p, o = InMemoryProductRepository(), InMemoryOrderRepository()
    order = await PlaceOrderUseCase(p, o).execute(
        "alice",
        [OrderItemInput("P1001", "P1001-S1", 2), OrderItemInput("P1001", "P1001-S1", 3)],
        ADDRESS,
        "qa-lines",
    )
    assert (await p.find_by_id("P1001")).primary_sku().stock == 45
    assert Decimal(str(order["total_amount_major"])) == sum(
        Decimal(str(x["unit_price_major"])) * x["quantity"] for x in order["lines"]
    )
    await CancelOrderUseCase(p, o).execute(order["order_id"], "cancel", buyer_id="alice")
    assert (await p.find_by_id("P1001")).primary_sku().stock == 50


async def test_business_04_two_independent_keys_are_two_legitimate_orders():
    p, o = InMemoryProductRepository(), InMemoryOrderRepository()
    place = PlaceOrderUseCase(p, o)
    results = await asyncio.gather(
        *(
            place.execute("alice", [OrderItemInput("P1001", "P1001-S1", 1)], ADDRESS, k)
            for k in ["qa-one", "qa-two"]
        )
    )
    assert len({r["order_id"] for r in results}) == 2
    assert (await p.find_by_id("P1001")).primary_sku().stock == 48

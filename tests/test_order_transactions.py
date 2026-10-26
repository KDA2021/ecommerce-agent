"""Order transaction regressions; MySQL tests always use a disposable database."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from app.application.usecases.order_usecases import (
    CancelOrderUseCase,
    OrderItemInput,
    PlaceOrderUseCase,
    QueryOrderUseCase,
)
from app.domain.order.address import Address
from app.infrastructure.persistence.in_memory_repositories import (
    InMemoryOrderRepository,
    InMemoryProductRepository,
)


def address():
    return Address("Buyer", "CN", "ZJ", "HZ", "1 Test Road", "310000", "123")


def item(quantity=1, product_id="P1001", sku_id="P1001-S1"):
    return OrderItemInput(product_id, sku_id, quantity)


class FailingOrderRepository(InMemoryOrderRepository):
    fail = False

    async def save(self, order):
        await super().save(order)
        if self.fail:
            raise RuntimeError("order write failed")


async def test_place_rolls_back_stock_and_order_when_order_save_fails():
    products, orders = InMemoryProductRepository(), FailingOrderRepository()
    original = (await products.find_by_id("P1001")).primary_sku().stock
    orders.fail = True
    with pytest.raises(RuntimeError, match="order write failed"):
        await PlaceOrderUseCase(products, orders).execute("a", [item()], address(), "rollback")
    assert (await products.find_by_id("P1001")).primary_sku().stock == original
    assert await orders.find_by_idempotency_key("rollback") is None


async def test_cancel_failure_rolls_back_order_status_and_stock():
    products, orders = InMemoryProductRepository(), FailingOrderRepository()
    result = await PlaceOrderUseCase(products, orders).execute("a", [item()], address())
    stock = (await products.find_by_id("P1001")).primary_sku().stock
    orders.fail = True
    with pytest.raises(RuntimeError, match="order write failed"):
        await CancelOrderUseCase(products, orders).execute(result["order_id"], "cancel")
    assert (await products.find_by_id("P1001")).primary_sku().stock == stock
    assert (await orders.find_by_id(result["order_id"])).status.value == "CONFIRMED"


async def test_query_and_cancel_reject_foreign_buyer():
    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    result = await PlaceOrderUseCase(products, orders).execute("a", [item()], address())
    with pytest.raises(PermissionError):
        await QueryOrderUseCase(orders).execute(result["order_id"], buyer_id="b")
    with pytest.raises(PermissionError):
        await CancelOrderUseCase(products, orders).execute(
            result["order_id"], "cancel", buyer_id="b"
        )
    assert (await orders.find_by_id(result["order_id"])).status.value == "CONFIRMED"


@pytest.mark.parametrize(
    "buyer,items,shipping",
    [
        ("b", [item()], address()),
        ("a", [item(2)], address()),
        ("a", [item()], Address("Other", "CN", "ZJ", "HZ", "2 Road", "", "")),
    ],
)
async def test_idempotency_key_rejects_different_request(buyer, items, shipping):
    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    place = PlaceOrderUseCase(products, orders)
    await place.execute("a", [item()], address(), "same-key")
    with pytest.raises((ValueError, PermissionError)):
        await place.execute(buyer, items, shipping, "same-key")


async def test_repeated_cancel_does_not_restock_twice():
    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    result = await PlaceOrderUseCase(products, orders).execute("a", [item()], address())
    cancel = CancelOrderUseCase(products, orders)
    first = await cancel.execute(result["order_id"], "cancel")
    second = await cancel.execute(result["order_id"], "cancel again")
    assert second == first
    assert (await products.find_by_id("P1001")).primary_sku().stock == 50


async def test_nonpositive_quantity_does_not_increase_stock():
    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    with pytest.raises(ValueError):
        await PlaceOrderUseCase(products, orders).execute("a", [item(-1)], address())
    assert (await products.find_by_id("P1001")).primary_sku().stock == 50


class YieldingProductRepository(InMemoryProductRepository):
    async def find_by_id(self, product_id):
        result = deepcopy(await super().find_by_id(product_id))
        await asyncio.sleep(0)
        return result

    async def save(self, product):
        await asyncio.sleep(0)
        await super().save(product)


async def test_concurrent_last_unit_allows_only_one_order():
    products, orders = YieldingProductRepository(), InMemoryOrderRepository()
    products._products["P1001"].primary_sku().stock = 1
    results = await asyncio.gather(
        *(
            PlaceOrderUseCase(products, orders).execute(str(i), [item()], address())
            for i in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert (await products.find_by_id("P1001")).primary_sku().stock == 0


async def test_concurrent_same_key_replays_once_even_with_last_unit():
    products, orders = YieldingProductRepository(), InMemoryOrderRepository()
    products._products["P1001"].primary_sku().stock = 1
    results = await asyncio.gather(
        *(
            PlaceOrderUseCase(products, orders).execute("a", [item()], address(), "same")
            for _ in range(3)
        )
    )
    assert len({r["order_id"] for r in results}) == 1
    assert sum(not r["idempotent_replay"] for r in results) == 1
    assert (await products.find_by_id("P1001")).primary_sku().stock == 0


@pytest.fixture()
async def isolated_mysql():
    """Never bootstrap, clear, or mutate the database named in TEST_MYSQL_URL."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.infrastructure.persistence.tables import Base

    if not os.getenv("TEST_MYSQL_URL"):
        pytest.skip("TEST_MYSQL_URL not configured")
    url = make_url(os.environ["TEST_MYSQL_URL"])
    if url.get_backend_name() != "mysql":
        pytest.fail("TEST_MYSQL_URL must use MySQL")
    database = "commerce_tx_test_" + uuid.uuid4().hex
    admin = create_async_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    engine = None
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE `{database}`"))
        engine = create_async_engine(url.set(database=database))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        await admin.dispose()


async def mysql_repositories(engine):
    from app.infrastructure.persistence.mysql_repositories import (
        MySqlOrderRepository,
        MySqlProductRepository,
    )
    from app.infrastructure.persistence.seed_products import build_seed_products

    products = MySqlProductRepository(engine)
    await products.insert_many(build_seed_products())
    product = await products.find_by_id("P1001")
    product.primary_sku().stock = 1
    await products.save(product)
    return products, MySqlOrderRepository(engine)


async def test_mysql_concurrent_last_unit_and_atomic_cancel(isolated_mysql):
    products, orders = await mysql_repositories(isolated_mysql)
    place = PlaceOrderUseCase(products, orders)
    results = await asyncio.gather(
        *(place.execute(str(i), [item()], address()) for i in range(5)), return_exceptions=True
    )
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1
    assert all(isinstance(r, (dict, ValueError)) for r in results)
    assert (await products.find_by_id("P1001")).primary_sku().stock == 0
    cancel = CancelOrderUseCase(products, orders)
    cancelled = await asyncio.gather(
        *(cancel.execute(winners[0]["order_id"], "cancel") for _ in range(4))
    )
    assert all(r["status"] == "CANCELLED" for r in cancelled)
    assert (await products.find_by_id("P1001")).primary_sku().stock == 1


async def test_mysql_concurrent_idempotency_replays_and_rejects_foreign_owner(isolated_mysql):
    products, orders = await mysql_repositories(isolated_mysql)
    place = PlaceOrderUseCase(products, orders)
    results = await asyncio.gather(
        *(place.execute("a", [item()], address(), "same") for _ in range(5))
    )
    assert len({r["order_id"] for r in results}) == 1
    assert sum(not r["idempotent_replay"] for r in results) == 1
    assert (await products.find_by_id("P1001")).primary_sku().stock == 0
    with pytest.raises(PermissionError):
        await place.execute("b", [item()], address(), "same")


def failing_factory(engine):
    from app.infrastructure.persistence.mysql_transactions import MySqlOrderTransactionFactory

    factory = MySqlOrderTransactionFactory(engine)

    @asynccontextmanager
    async def transaction():
        async with factory() as unit:
            original_save = unit.orders.save

            async def fail_after_save(order):
                await original_save(order)
                raise RuntimeError("order write failed")

            unit.orders.save = fail_after_save
            yield unit

    return transaction


async def test_mysql_order_failure_rolls_back_products(isolated_mysql):
    products, orders = await mysql_repositories(isolated_mysql)
    with pytest.raises(RuntimeError, match="order write failed"):
        await PlaceOrderUseCase(products, orders, failing_factory(isolated_mysql)).execute(
            "a", [item()], address(), "rollback"
        )
    assert (await products.find_by_id("P1001")).primary_sku().stock == 1
    assert await orders.find_by_idempotency_key("rollback") is None


async def test_mysql_cancel_failure_rolls_back_order_and_stock(isolated_mysql):
    products, orders = await mysql_repositories(isolated_mysql)
    result = await PlaceOrderUseCase(products, orders).execute("a", [item()], address())
    with pytest.raises(RuntimeError, match="order write failed"):
        await CancelOrderUseCase(products, orders, failing_factory(isolated_mysql)).execute(
            result["order_id"], "cancel"
        )
    assert (await products.find_by_id("P1001")).primary_sku().stock == 0
    assert (await orders.find_by_id(result["order_id"])).status.value == "CONFIRMED"


async def test_mysql_disjoint_products_unique_key_race_rolls_back_loser(isolated_mysql):
    from app.infrastructure.persistence.mysql_transactions import MySqlOrderTransactionFactory

    products, orders = await mysql_repositories(isolated_mysql)
    before = {
        pid: (await products.find_by_id(pid)).primary_sku().stock for pid in ("P1001", "P1002")
    }
    barrier = asyncio.Barrier(2)
    factory = MySqlOrderTransactionFactory(isolated_mysql)
    starts = []

    @asynccontextmanager
    async def synchronized_transaction():
        starts.append(1)
        async with factory() as unit:
            find = unit.orders.find_by_idempotency_key
            calls = 0

            async def find_after_barrier(key):
                nonlocal calls
                calls += 1
                result = await find(key)
                # Force both initial transactions to see absence after product
                # locks, so the database unique index resolves the collision.
                if calls == 2 and result is None:
                    await asyncio.wait_for(barrier.wait(), timeout=10)
                return result

            unit.orders.find_by_idempotency_key = find_after_barrier
            yield unit

    place = PlaceOrderUseCase(products, orders, synchronized_transaction)
    results = await asyncio.gather(
        place.execute("a", [item()], address(), "collision"),
        place.execute("a", [item(product_id="P1002", sku_id="P1002-S1")], address(), "collision"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert len(starts) == 3  # loser re-enters only after its transaction rolled back
    winner = next(r for r in results if isinstance(r, dict))["lines"][0]["product_id"]
    for pid, stock in before.items():
        assert (await products.find_by_id(pid)).primary_sku().stock == stock - int(pid == winner)


async def test_unknown_order_keeps_value_error_with_owner_validation():
    orders = InMemoryOrderRepository()
    with pytest.raises(ValueError, match="订单不存在"):
        await QueryOrderUseCase(orders).execute("unknown", buyer_id="a")
    with pytest.raises(ValueError, match="订单不存在"):
        await CancelOrderUseCase(InMemoryProductRepository(), orders).execute(
            "unknown", "cancel", buyer_id="a"
        )


async def test_cancel_missing_product_rolls_back_status_instead_of_losing_stock():
    products, orders = InMemoryProductRepository(), InMemoryOrderRepository()
    result = await PlaceOrderUseCase(products, orders).execute("a", [item()], address())
    products._products.pop("P1001")
    with pytest.raises(ValueError, match="商品不存在"):
        await CancelOrderUseCase(products, orders).execute(result["order_id"], "cancel")
    assert (await orders.find_by_id(result["order_id"])).status.value == "CONFIRMED"


async def test_mysql_opposite_multi_product_order_uses_consistent_stock_locks(isolated_mysql):
    products, orders = await mysql_repositories(isolated_mysql)
    first_product = await products.find_by_id("P1001")
    first_product.primary_sku().stock = 3
    await products.save(first_product)
    place = PlaceOrderUseCase(products, orders)
    first, second = item(), item(product_id="P1002", sku_id="P1002-S1")
    results = await asyncio.wait_for(
        asyncio.gather(
            place.execute("a", [first, second, first], address()),
            place.execute("b", [second, first], address()),
        ),
        timeout=10,
    )
    assert len({r["order_id"] for r in results}) == 2
    assert (await products.find_by_id("P1001")).primary_sku().stock == 0


async def test_mysql_empty_idempotency_key_behaves_as_no_key(isolated_mysql):
    products, orders = await mysql_repositories(isolated_mysql)
    product = await products.find_by_id("P1001")
    product.primary_sku().stock = 3
    await products.save(product)
    place = PlaceOrderUseCase(products, orders)
    first = await place.execute("a", [item()], address(), "")
    second = await place.execute("a", [item()], address(), "")
    assert first["order_id"] != second["order_id"]
    assert (await products.find_by_id("P1001")).primary_sku().stock == 1


async def test_mysql_missing_counter_never_overwrites_existing_order(isolated_mysql):
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    from app.infrastructure.persistence.tables import OrderCounterRow

    products, orders = await mysql_repositories(isolated_mysql)
    product = await products.find_by_id("P1001")
    product.primary_sku().stock = 3
    await products.save(product)
    place = PlaceOrderUseCase(products, orders)
    original = await place.execute("original", [item()], address())
    async with isolated_mysql.begin() as connection:
        await connection.execute(delete(OrderCounterRow))
    with pytest.raises((ValueError, IntegrityError)):
        await place.execute("replacement", [item()], address())
    assert (await orders.find_by_id(original["order_id"])).buyer_id == "original"
    assert (await products.find_by_id("P1001")).primary_sku().stock == 2

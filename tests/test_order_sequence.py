# tests/test_order_sequence.py
# -*- coding: utf-8 -*-
"""P11-1 订单号原子序列：MySQL / InMemory 两种实现的取号唯一性。

覆盖：
1. 订单号格式与解析（纯函数，无外部依赖）；
2. InMemory：并发 / 多线程取号唯一，且不会被手工注入的订单号撞号；
3. 计数器初始化 SQL 的两个关键保证（空库兜底 + 只增不减）；
4. MySQL 集成：10 并发取号唯一、按历史最大值初始化、并发下单端到端不撞号。
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.domain.order.order_id import (
    format_order_id,
    max_order_sequence,
    parse_order_sequence,
)
from app.infrastructure.persistence.in_memory_repositories import InMemoryOrderRepository


def _confirmed_order(order_id: str):
    """构造一条最小可保存订单（仅内存仓储用，不落库）。"""
    from app.domain.catalog.money import Money
    from app.domain.order.address import Address
    from app.domain.order.order import Order
    from app.domain.order.order_line import OrderLine

    return Order.place(
        order_id=order_id,
        buyer_id="buyer-seq",
        shipping_address=Address(
            recipient_name="张三", country="CN", state="浙江", city="杭州",
            address_line="西湖区某路 1 号", postal_code="310000", phone="13800000000",
        ),
        lines=[OrderLine(
            product_id="P1001", sku_id="P1001-S1", title="测试商品",
            unit_price=Money.of(19900, "CNY"), quantity=1,
        )],
    )


class TestOrderIdFormat:
    """订单号格式纯函数。"""

    def test_format_pads_to_six_digits(self) -> None:
        assert format_order_id(1) == "GBX-000001"
        assert format_order_id(42) == "GBX-000042"
        assert format_order_id(999999) == "GBX-999999"

    def test_format_keeps_growing_beyond_six_digits(self) -> None:
        # 超过 6 位不截断：宁可变长，也不能回绕撞历史号。
        assert format_order_id(1_000_000) == "GBX-1000000"

    def test_parse_roundtrip(self) -> None:
        assert parse_order_sequence("GBX-000042") == 42
        assert parse_order_sequence(format_order_id(7)) == 7

    def test_parse_skips_dirty_ids(self) -> None:
        assert parse_order_sequence("LEGACY-1") is None
        assert parse_order_sequence("GBX-abc") is None
        assert parse_order_sequence("") is None

    def test_max_order_sequence_ignores_dirty_ids(self) -> None:
        assert max_order_sequence(["GBX-000002", "LEGACY-99", "GBX-000010"]) == 10
        assert max_order_sequence([]) == 0
        assert max_order_sequence(["LEGACY-99"]) == 0


class TestInMemoryOrderSequence:
    """InMemory 仓储取号。"""

    async def test_first_id_is_one(self) -> None:
        repo = InMemoryOrderRepository()
        assert await repo.next_order_id() == "GBX-000001"
        assert await repo.next_order_id() == "GBX-000002"

    async def test_ten_concurrent_calls_are_unique(self) -> None:
        repo = InMemoryOrderRepository()
        order_ids = await asyncio.gather(*(repo.next_order_id() for _ in range(10)))
        assert len(set(order_ids)) == 10, f"并发取号出现重复：{order_ids}"

    async def test_threaded_calls_are_unique(self) -> None:
        """线程安全：32 个线程并发取号不重复（asyncio 单线程跑不出这个场景）。"""
        import threading

        repo = InMemoryOrderRepository()
        results: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            value = asyncio.run(repo.next_order_id())
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(set(results)) == 32, f"多线程取号出现重复：{sorted(results)}"

    async def test_sequence_advances_past_manually_saved_order(self) -> None:
        """旧测试替身兼容：手工注入 GBX-000500 后不能把同一个号再发一次。"""
        repo = InMemoryOrderRepository()
        await repo.save(_confirmed_order("GBX-000500"))
        assert await repo.next_order_id() == "GBX-000501"

    async def test_sequence_ignores_dirty_saved_order(self) -> None:
        repo = InMemoryOrderRepository()
        await repo.save(_confirmed_order("LEGACY-9"))
        assert await repo.next_order_id() == "GBX-000001"


class _FakeResult:
    """伪连接返回的标量结果。"""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeConnection:
    """记录 execute / scalar 调用，不连数据库。"""

    def __init__(self, scalar_by_fragment: dict[str, object]) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._scalar_by_fragment = scalar_by_fragment

    def _lookup(self, statement) -> object:
        sql = str(statement)
        for fragment, value in self._scalar_by_fragment.items():
            if fragment in sql:
                return value
        return None

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _FakeResult(self._lookup(statement))

    async def scalar(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return self._lookup(statement)

    async def commit(self) -> None:
        return None

    def executed_sql(self) -> str:
        return " ".join(sql for sql, _ in self.calls)

    def params_for(self, fragment: str) -> dict:
        for sql, params in self.calls:
            if fragment in sql:
                return params
        raise AssertionError(f"没有找到包含 {fragment!r} 的语句：{self.calls}")


class _FakeTransaction:
    """异步上下文管理器，模拟 ``engine.begin()``。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeEngine:
    """最小 engine 替身：只支持 ``begin()``。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction(self._connection)


class TestSeedOrderCounterSql:
    """初始化 SQL 的两个关键保证（用 engine 替身做契约断言，不连库）。"""

    async def test_seed_sql_is_null_safe_and_monotonic(self) -> None:
        from app.infrastructure.persistence.mysql_repositories import (
            ORDER_COUNTER_NAME,
            seed_order_counter,
        )

        connection = _FakeConnection({
            "SELECT COALESCE(MAX(": 46,                # 历史订单最大序号
            "SELECT next_value": 42,                   # 回读计数器
        })
        seeded = await seed_order_counter(_FakeEngine(connection))  # type: ignore[arg-type]

        sql = connection.executed_sql().upper()
        # 空库兜底：MAX() 为 NULL 时必须落成 0，而不是 NULL。
        assert "COALESCE(MAX(" in sql
        # 只读 GBX- 号段，避免把历史脏号算进序列。
        assert "LIKE 'GBX-%'" in sql
        # 只增不减：已有更大的计数器值不能被历史订单最大值改小。
        assert "ON DUPLICATE KEY UPDATE" in sql
        assert "GREATEST(ORDER_COUNTERS.NEXT_VALUE, NEW.NEXT_VALUE)" in sql
        # 避开 MySQL 8 已废弃的 VALUES(col) 写法。
        assert "VALUES(NEXT_VALUE)" not in sql
        # 序列名与历史最大值都参数化注入，不拼字符串。
        params = connection.params_for("INSERT INTO")
        assert params["name"] == ORDER_COUNTER_NAME
        assert params["historical_max"] == 46
        # 返回值来自库里的实际 next_value，而不是本地推算。
        assert seeded == 42


# --------------------------------------------------------------------------- #
# MySQL 集成用例：没有 TEST_MYSQL_URL 时整体跳过
# （与 tests/test_mysql_repositories.py 同一约定）
# --------------------------------------------------------------------------- #
@pytest.fixture()
async def engine():
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine

    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    await bootstrap_schema(engine)
    yield engine
    await engine.dispose()


@pytest.fixture()
async def seeded_counter(engine):
    """用例前后都把 ``order_counters`` 恢复成“按历史订单最大值”的状态。"""
    from app.infrastructure.persistence.mysql_repositories import seed_order_counter

    await seed_order_counter(engine)
    yield
    await seed_order_counter(engine)


async def _next_value(engine) -> int:
    """读当前计数器值。"""
    from sqlalchemy import text

    from app.infrastructure.persistence.mysql_repositories import ORDER_COUNTER_NAME

    async with engine.connect() as conn:
        value = await conn.scalar(
            text("SELECT next_value FROM order_counters WHERE counter_name = :name"),
            {"name": ORDER_COUNTER_NAME},
        )
    return int(value)


@pytest.mark.skipif(not os.getenv("TEST_MYSQL_URL"), reason="TEST_MYSQL_URL 未设置")
class TestMySqlOrderSequence:
    """MySQL ``order_counters`` 原子取号。"""

    async def test_ten_concurrent_calls_are_unique(self, engine, seeded_counter) -> None:
        from app.infrastructure.persistence.mysql_repositories import MySqlOrderRepository

        repo = MySqlOrderRepository(engine)
        order_ids = await asyncio.gather(*(repo.next_order_id() for _ in range(10)))
        assert len(set(order_ids)) == 10, f"并发取号出现重复：{order_ids}"

    async def test_ids_match_gbx_format(self, engine, seeded_counter) -> None:
        from app.infrastructure.persistence.mysql_repositories import MySqlOrderRepository

        order_id = await MySqlOrderRepository(engine).next_order_id()
        assert order_id.startswith("GBX-")
        assert parse_order_sequence(order_id) is not None

    async def test_counter_row_is_created_and_incremented(self, engine, seeded_counter) -> None:
        from app.infrastructure.persistence.mysql_repositories import MySqlOrderRepository

        repo = MySqlOrderRepository(engine)
        before = await _next_value(engine)
        order_id = await repo.next_order_id()
        after = await _next_value(engine)
        assert after == before + 1
        assert parse_order_sequence(order_id) == after

    async def test_next_id_is_above_all_existing_orders(self, engine, seeded_counter) -> None:
        """核心诉求：取号结果必须大于库里历史订单的最大号（不与历史 GBX- 冲突）。"""
        from sqlalchemy import text

        from app.infrastructure.persistence.mysql_repositories import MySqlOrderRepository

        async with engine.connect() as conn:
            max_existing = int(
                await conn.scalar(
                    text(
                        "SELECT COALESCE(MAX(CAST(SUBSTRING(order_id, 5) AS UNSIGNED)), 0) "
                        "FROM orders WHERE order_id LIKE 'GBX-%'",
                    ),
                )
                or 0,
            )
        order_id = await MySqlOrderRepository(engine).next_order_id()
        assert parse_order_sequence(order_id) > max_existing

    async def test_next_order_id_self_heals_missing_counter_row(self, engine) -> None:
        """计数器行被误删也不能退化成 GBX-000000。"""
        from sqlalchemy import text

        from app.infrastructure.persistence.mysql_repositories import (
            ORDER_COUNTER_NAME,
            MySqlOrderRepository,
            seed_order_counter,
        )

        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM order_counters WHERE counter_name = :name"),
                {"name": ORDER_COUNTER_NAME},
            )
        try:
            order_id = await MySqlOrderRepository(engine).next_order_id()
            assert order_id == "GBX-000001", f"缺行时应从 1 起发，实际={order_id}"
        finally:
            await seed_order_counter(engine)

    async def test_seed_uses_existing_max_order_id(self, engine) -> None:
        """初始化按现有订单号最大值：库里最大 460000 时，下一个必须是 460001。"""
        from sqlalchemy import text

        from app.infrastructure.persistence.mysql_repositories import (
            ORDER_COUNTER_NAME,
            MySqlOrderRepository,
            seed_order_counter,
        )

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO orders (order_id, buyer_id, status, currency, "
                    "total_amount_minor, shipping_address, created_at) VALUES "
                    "('GBX-460000', 'buyer-seed', 'CONFIRMED', 'CNY', 100, '{}', NOW()) AS new "
                    "ON DUPLICATE KEY UPDATE buyer_id = new.buyer_id",
                ),
            )
        try:
            assert await seed_order_counter(engine) >= 460000
            assert await _next_value(engine) == 460000
            assert await MySqlOrderRepository(engine).next_order_id() == "GBX-460001"
        finally:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM orders WHERE order_id = 'GBX-460000'"))
                await conn.execute(
                    text("DELETE FROM order_counters WHERE counter_name = :name"),
                    {"name": ORDER_COUNTER_NAME},
                )

    async def test_seed_never_moves_sequence_backwards(self, engine) -> None:
        """重复 / 并发启动：已有更大的计数器值不能被历史最大值改小。"""
        from sqlalchemy import text

        from app.infrastructure.persistence.mysql_repositories import (
            ORDER_COUNTER_NAME,
            seed_order_counter,
        )

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO order_counters (counter_name, next_value, updated_at) "
                    "VALUES (:name, 5000000, NOW()) "
                    "ON DUPLICATE KEY UPDATE next_value = 5000000",
                ),
                {"name": ORDER_COUNTER_NAME},
            )
        try:
            assert await seed_order_counter(engine) == 5000000
        finally:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM order_counters WHERE counter_name = :name"),
                    {"name": ORDER_COUNTER_NAME},
                )

    async def test_place_order_through_usecase_gets_unique_ids(
        self, engine, seeded_counter,
    ) -> None:
        """端到端：并发走 PlaceOrderUseCase，订单号与落库结果都不撞。"""
        from sqlalchemy import text

        from app.application.usecases.order_usecases import OrderItemInput, PlaceOrderUseCase
        from app.domain.order.address import Address
        from app.infrastructure.persistence.mysql_repositories import (
            MySqlOrderRepository,
            MySqlProductRepository,
        )
        from app.infrastructure.persistence.seed_products import build_seed_products

        product_repo = MySqlProductRepository(engine)
        if await product_repo.count() == 0:
            await product_repo.insert_many(build_seed_products())
        order_repo = MySqlOrderRepository(engine)
        place = PlaceOrderUseCase(product_repo, order_repo)
        address = Address(
            recipient_name="张三", country="CN", state="浙江", city="杭州",
            address_line="西湖区某路 1 号", postal_code="310000", phone="13800000000",
        )
        created: list[str] = []
        try:
            snapshots = await asyncio.gather(*(
                place.execute(
                    buyer_id=f"buyer-seq-{index}",
                    items=[OrderItemInput(product_id="P1001", sku_id="P1001-S1", quantity=1)],
                    shipping_address=address,
                )
                for index in range(5)
            ))
            created = [snapshot["order_id"] for snapshot in snapshots]
            assert len(set(created)) == len(created), f"并发下单撞号：{created}"
            for order_id in created:
                assert await order_repo.find_by_id(order_id) is not None
        finally:
            # 本用例只验证取号唯一性：把订单 / 库存恢复原状。
            async with engine.begin() as conn:
                for order_id in created:
                    await conn.execute(
                        text("DELETE FROM order_items WHERE order_id = :order_id"),
                        {"order_id": order_id},
                    )
                    await conn.execute(
                        text("DELETE FROM orders WHERE order_id = :order_id"),
                        {"order_id": order_id},
                    )
            product = await product_repo.find_by_id("P1001")
            sku = product.find_sku("P1001-S1")
            sku.restore_stock(len(created))
            await product_repo.save(product)

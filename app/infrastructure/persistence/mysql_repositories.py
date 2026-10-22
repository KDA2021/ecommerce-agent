# -*- coding: utf-8 -*-
"""MySQL 实现：商品仓储、订单仓储。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.product import Product
from app.domain.order.address import Address
from app.domain.order.order import Order, OrderStatus
from app.domain.order.order_id import format_order_id
from app.domain.order.order_line import OrderLine
from app.domain.order.ports.order_repository import OrderRepository
from app.infrastructure.persistence.mappers import product_from_dict, product_to_dict
from app.infrastructure.persistence.tables import (
    OrderCounterRow,
    OrderItemRow,
    OrderRow,
    ProductRow,
)

#: 订单号唯一序列名：命名空间预留，未来可按仓 / 渠道拆成多条序列。
ORDER_COUNTER_NAME = "order"


async def seed_order_counter(engine: AsyncEngine) -> int:
    """用现有订单号最大值初始化 ``order_counters`` 序列（启动时调用）。

    只增不减：``next_value = GREATEST(next_value, 历史最大序号)``。
    GREATEST / LEAST 方向很容易写反，这里盯死一条不变量：
    **历史订单最大值是下界，已有计数器值只能被抬高、不能被压低**。
    因此重复启动、多进程同时启动都不会把序列改小，也就不会与历史 GBX- 号段冲突。

    Args:
        engine: 数据库引擎。

    Returns:
        初始化后 ``next_value``（已发出的最大序号）。
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with engine.begin() as conn:
        historical_max = int(
            await conn.scalar(
                text(
                    "SELECT COALESCE(MAX(CAST(SUBSTRING(order_id, 5) AS UNSIGNED)), 0) "
                    "FROM orders WHERE order_id LIKE 'GBX-%'",
                ),
            )
            or 0,
        )
        # 用 VALUES 列表 + 行别名：MySQL 8.0.19+ 推荐写法，避开已废弃的 VALUES(col)。
        # 备注：`INSERT ... SELECT` 不支持行别名，因此这里拆成两步；两步之间的窗口
        # 只可能是“服务还没开始接单”的启动期，且 orders 主键是最终兜底。
        await conn.execute(
            text(
                "INSERT INTO order_counters (counter_name, next_value, updated_at) "
                "VALUES (:name, :historical_max, :now) AS new "
                "ON DUPLICATE KEY UPDATE "
                "next_value = GREATEST(order_counters.next_value, new.next_value)",
            ),
            {"name": ORDER_COUNTER_NAME, "historical_max": historical_max, "now": now},
        )
        value = int(
            await conn.scalar(
                text("SELECT next_value FROM order_counters WHERE counter_name = :name"),
                {"name": ORDER_COUNTER_NAME},
            )
            or 0,
        )
        await conn.commit()
    return value


def product_chroma_id(product_id: str) -> str:
    """按 product_id 生成确定性 UUID5，作为 Chroma 文档主键。

    Args:
        product_id: 商品 ID。

    Returns:
        chroma 文档 id。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"globex/product/{product_id}"))


def _naive_utc(dt: datetime) -> datetime:
    """aware datetime → UTC naive（MySQL DATETIME 不存时区）。

    Args:
        dt: aware datetime。

    Returns:
        naive datetime；入参为空则返回 None。
    """
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt else None


def _aware_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """UTC naive → aware datetime。

    Args:
        dt: naive datetime。

    Returns:
        aware datetime；入参为空则返回 None。
    """
    return dt.replace(tzinfo=timezone.utc) if dt else None


class MySqlProductRepository(ProductRepository):
    """MySQL 商品仓储（products 表，payload 为 JSON）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """初始化 sessionmaker。

        Args:
            engine: 数据库引擎。
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def find_by_id(self, product_id: str) -> Optional[Product]:
        """按商品 ID 查询。

        Args:
            product_id: 商品 ID。

        Returns:
            Product；未命中返回 None。
        """
        async with self._sessions() as db:
            row = await db.get(ProductRow, product_id)
        return product_from_dict(row.payload) if row else None

    async def find_by_ids(self, product_ids: list[str]) -> list[Product]:
        """批量按 ID 查询。

        Args:
            product_ids: 商品 ID 列表。

        Returns:
            命中的 Product 列表。
        """
        if not product_ids:
            return []
        async with self._sessions() as db:
            rows = (await db.scalars(select(ProductRow).where(ProductRow.product_id.in_(product_ids)))).all()
        return [product_from_dict(row.payload) for row in rows]

    async def list_all(self) -> list[Product]:
        """按商品 ID 升序返回全部商品。

        Returns:
            Product 列表。
        """
        async with self._sessions() as db:
            rows = (await db.scalars(select(ProductRow).order_by(ProductRow.product_id))).all()
        return [product_from_dict(row.payload) for row in rows]

    async def count(self) -> int:
        """返回商品总数。

        Returns:
            商品数。
        """
        async with self._sessions() as db:
            return int(await db.scalar(select(func.count()).select_from(ProductRow)) or 0)

    async def save(self, product: Product) -> None:
        """更新商品载荷与确定性 chroma_id（向量交给 Chroma）。

        Args:
            product: 商品聚合。
        """
        async with self._sessions() as db:
            row = await db.get(ProductRow, product.product_id)
            # 只更新已存在行：新增商品统一走 insert_many，避免这里静默插入脏数据。
            if row is not None:
                row.payload = product_to_dict(product)
                row.chroma_id = product_chroma_id(product.product_id)
                await db.commit()

    async def insert_many(self, products: list[Product]) -> None:
        """批量插入商品（IGNORE 已存在行）。

        Args:
            products: 商品列表。
        """
        if not products:
            return
        async with self._sessions() as db:
            for product in products:
                # IGNORE 让 seed 脚本可重复执行：已存在的 product_id 不覆盖。
                stmt = (
                    mysql_insert(ProductRow)
                    .values(
                        product_id=product.product_id,
                        payload=product_to_dict(product),
                        chroma_id=product_chroma_id(product.product_id),
                    )
                    .prefix_with("IGNORE")
                )
                await db.execute(stmt)
            await db.commit()


class MySqlOrderRepository(OrderRepository):
    """MySQL 订单仓储（orders + order_items）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """初始化 sessionmaker。

        Args:
            engine: 数据库引擎。
        """
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def save(self, order: Order) -> None:
        """upsert 订单头并重建订单行。

        Args:
            order: 订单聚合。
        """
        async with self._sessions() as db:
            # 先 upsert 订单头：重放同一 order_id 时覆盖为最新状态。
            await db.execute(
                mysql_insert(OrderRow)
                .values(
                    order_id=order.order_id,
                    buyer_id=order.buyer_id,
                    status=order.status.value,
                    currency=order.total_amount().currency,
                    total_amount_minor=order.total_amount().amount_in_minor_units,
                    shipping_address=_address_to_dict(order.shipping_address),
                    created_at=_naive_utc(order.created_at),
                    confirmed_at=_naive_utc(order.confirmed_at),
                    cancelled_at=_naive_utc(order.cancelled_at),
                    cancel_reason=order.cancel_reason,
                    idempotency_key=order.idempotency_key,
                )
                .on_duplicate_key_update(
                    status=order.status.value,
                    currency=order.total_amount().currency,
                    total_amount_minor=order.total_amount().amount_in_minor_units,
                    shipping_address=_address_to_dict(order.shipping_address),
                    confirmed_at=_naive_utc(order.confirmed_at),
                    cancelled_at=_naive_utc(order.cancelled_at),
                    cancel_reason=order.cancel_reason,
                    idempotency_key=order.idempotency_key,
                )
            )
            # 再删旧行重建：保证订单行与聚合一致（整单覆盖写）。
            await db.execute(delete(OrderItemRow).where(OrderItemRow.order_id == order.order_id))
            db.add_all(
                [
                    OrderItemRow(
                        order_id=order.order_id,
                        product_id=line.product_id,
                        sku_id=line.sku_id,
                        title=line.title,
                        unit_price_minor=line.unit_price.amount_in_minor_units,
                        currency=line.unit_price.currency,
                        quantity=line.quantity,
                    )
                    for line in order.lines
                ],
            )
            await db.commit()

    async def find_by_id(self, order_id: str) -> Optional[Order]:
        """按订单号查询订单聚合。

        Args:
            order_id: 订单号。

        Returns:
            Order；未命中返回 None。
        """
        async with self._sessions() as db:
            row = await db.get(OrderRow, order_id)
            if row is None:
                return None
            line_rows = (await db.scalars(select(OrderItemRow).where(OrderItemRow.order_id == order_id))).all()
        return _row_to_order(row, line_rows)

    async def find_by_idempotency_key(self, idempotency_key: str) -> Optional[Order]:
        """按幂等键查询订单聚合。

        Args:
            idempotency_key: 下单幂等键。

        Returns:
            Order；未命中返回 None。
        """
        async with self._sessions() as db:
            row = (
                await db.scalars(
                    select(OrderRow).where(OrderRow.idempotency_key == idempotency_key),
                )
            ).first()
            if row is None:
                return None
            line_rows = (
                await db.scalars(
                    select(OrderItemRow).where(OrderItemRow.order_id == row.order_id),
                )
            ).all()
        return _row_to_order(row, line_rows)

    async def next_order_id(self) -> str:
        """从 ``order_counters`` 原子取下一个订单号。

        核心是 ``UPDATE ... SET next_value = LAST_INSERT_ID(next_value + 1)``：
        “自增”和“读回新值”在一条语句内完成，InnoDB 行锁把并发取号串行化，
        ``LAST_INSERT_ID()`` 又是连接级的，所以不会像 ``count(*)+1`` 那样撞号。

        调用前先做一次幂等建行（``next_value = 0``）：即使 bootstrap 没跑过、
        或者计数器行被误删，取号也不会退化成 ``GBX-000000``。

        Returns:
            订单号，如 ``GBX-000047``。
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with self._sessions() as db:
            # 首次取号前保证序列行存在；已存在时 ON DUPLICATE KEY 什么都不改。
            await db.execute(
                mysql_insert(OrderCounterRow)
                .values(counter_name=ORDER_COUNTER_NAME, next_value=0, updated_at=now)
                .on_duplicate_key_update(counter_name=ORDER_COUNTER_NAME),
            )
            await db.execute(
                text(
                    f"UPDATE {OrderCounterRow.__tablename__} "
                    "SET next_value = LAST_INSERT_ID(next_value + 1), updated_at = :now "
                    "WHERE counter_name = :name",
                ),
                {"name": ORDER_COUNTER_NAME, "now": now},
            )
            sequence = int(await db.scalar(text("SELECT LAST_INSERT_ID()")))
            await db.commit()
        return format_order_id(sequence)


def _address_to_dict(address: Address) -> dict:
    """Address → 可存 JSON 的 dict。

    Args:
        address: 地址值对象。

    Returns:
        地址 dict。
    """
    return {
        "recipient_name": address.recipient_name,
        "country": address.country,
        "state": address.state,
        "city": address.city,
        "address_line": address.address_line,
        "postal_code": address.postal_code,
        "phone": address.phone,
    }


def _row_to_order(row: OrderRow, line_rows: list[OrderItemRow]) -> Order:
    """订单行 ORM 记录 → Order 聚合。

    Args:
        row: 订单头记录。
        line_rows: 订单行记录列表。

    Returns:
        Order。
    """
    from app.domain.catalog.money import Money

    return Order(
        order_id=row.order_id,
        buyer_id=row.buyer_id,
        shipping_address=Address(**row.shipping_address),
        lines=[
            OrderLine(
                product_id=line.product_id,
                sku_id=line.sku_id,
                title=line.title,
                unit_price=Money.of(line.unit_price_minor, line.currency),
                quantity=line.quantity,
            )
            for line in line_rows
        ],
        status=OrderStatus(row.status),
        created_at=_aware_utc(row.created_at),
        confirmed_at=_aware_utc(row.confirmed_at),
        cancelled_at=_aware_utc(row.cancelled_at),
        cancel_reason=row.cancel_reason,
        idempotency_key=row.idempotency_key,
    )

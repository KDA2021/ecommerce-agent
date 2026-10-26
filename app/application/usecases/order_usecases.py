"""Order operations execute within one atomic persistence transaction."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.order.address import Address
from app.domain.order.order import Order, OrderStatus
from app.domain.order.order_line import OrderLine
from app.domain.order.ports.order_repository import OrderRepository
from app.domain.order.ports.order_transaction import (
    OrderTransactionFactory,
    RetryableOrderTransactionError,
)


@dataclass(frozen=True)
class OrderItemInput:
    product_id: str
    sku_id: str
    quantity: int


def _transaction_factory(product_repo, order_repo, factory):
    if factory is not None:
        return factory
    provider = getattr(order_repo, "transaction_factory", None)
    if provider is None:
        raise ValueError("Order writes require an atomic transaction factory")
    return provider(product_repo)


def _check_owner(order: Order, buyer_id: Optional[str]) -> None:
    if buyer_id is not None and order.buyer_id != buyer_id:
        raise PermissionError("Order belongs to another buyer")


def _replay(order: Order, buyer_id: str, items: list[OrderItemInput], address: Address) -> dict:
    _check_owner(order, buyer_id)
    requested = Counter((i.product_id, i.sku_id, i.quantity) for i in items)
    saved = Counter((i.product_id, i.sku_id, i.quantity) for i in order.lines)
    if requested != saved or order.shipping_address != address:
        raise ValueError("Idempotency key already used with different order contents")
    return dict(order.snapshot(), idempotent_replay=True)


async def _locked_products(repo: ProductRepository, product_ids) -> dict:
    # Every writer takes product locks in the same order, including duplicate lines.
    products = {}
    for product_id in sorted(set(product_ids)):
        product = await repo.find_by_id(product_id)
        if product is None:
            raise ValueError(f"商品不存在：{product_id}")
        products[product_id] = product
    return products


class PlaceOrderUseCase:
    def __init__(
        self,
        product_repo: ProductRepository,
        order_repo: OrderRepository,
        transaction_factory: Optional[OrderTransactionFactory] = None,
    ) -> None:
        self._product_repo = product_repo
        self._order_repo = order_repo
        self._transactions = _transaction_factory(product_repo, order_repo, transaction_factory)

    async def execute(
        self,
        buyer_id: str,
        items: list[OrderItemInput],
        shipping_address: Address,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        idempotency_key = idempotency_key or None
        if not items:
            raise ValueError("PlaceOrder.items 不能为空")
        if not buyer_id:
            raise ValueError("Order.buyer_id required")
        if any(type(i.quantity) is not int or i.quantity <= 0 for i in items):
            raise ValueError("Order item quantity must be a positive integer")
        for attempt in range(3):
            try:
                async with self._transactions() as transaction:
                    if idempotency_key:
                        existing = await transaction.orders.find_by_idempotency_key(idempotency_key)
                        if existing is not None:
                            return _replay(existing, buyer_id, items, shipping_address)
                    products = await _locked_products(
                        transaction.products, (i.product_id for i in items)
                    )
                    # A competing request may have committed while we waited for stock locks.
                    if idempotency_key:
                        existing = await transaction.orders.find_by_idempotency_key(idempotency_key)
                        if existing is not None:
                            return _replay(existing, buyer_id, items, shipping_address)
                    lines = []
                    for item in items:
                        product = products[item.product_id]
                        sku = product.find_sku(item.sku_id)
                        if sku is None:
                            raise ValueError(f"Sku 不存在：{item.product_id}/{item.sku_id}")
                        sku.deduct_stock(item.quantity)
                        lines.append(
                            OrderLine(
                                product.product_id,
                                sku.sku_id,
                                f"{product.title}（{sku.spec}）",
                                sku.price,
                                item.quantity,
                            )
                        )
                    order = Order.place(
                        await transaction.orders.next_order_id(),
                        buyer_id,
                        shipping_address,
                        lines,
                        idempotency_key,
                    )
                    for product in products.values():
                        await transaction.products.save(product)
                    await transaction.orders.save(order)
                    snapshot = order.snapshot()
                    if idempotency_key:
                        snapshot["idempotent_replay"] = False
                return snapshot
            except RetryableOrderTransactionError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")


class QueryOrderUseCase:
    def __init__(self, order_repo: OrderRepository) -> None:
        self._order_repo = order_repo

    async def execute(self, order_id: str, buyer_id: Optional[str] = None) -> dict:
        order = await self._order_repo.find_by_id(order_id)
        if order is None:
            raise ValueError(f"订单不存在：{order_id}")
        _check_owner(order, buyer_id)
        return order.snapshot()


class CancelOrderUseCase:
    def __init__(
        self,
        product_repo: ProductRepository,
        order_repo: OrderRepository,
        transaction_factory: Optional[OrderTransactionFactory] = None,
    ) -> None:
        self._product_repo = product_repo
        self._order_repo = order_repo
        self._transactions = _transaction_factory(product_repo, order_repo, transaction_factory)

    async def execute(self, order_id: str, reason: str, buyer_id: Optional[str] = None) -> dict:
        for attempt in range(3):
            try:
                async with self._transactions() as transaction:
                    order = await transaction.orders.find_by_id(order_id)
                    if order is None:
                        raise ValueError(f"订单不存在：{order_id}")
                    _check_owner(order, buyer_id)
                    if order.status is OrderStatus.CANCELLED:
                        return order.snapshot()
                    products = await _locked_products(
                        transaction.products, (i.product_id for i in order.lines)
                    )
                    order.cancel(reason)
                    for line in order.lines:
                        sku = products[line.product_id].find_sku(line.sku_id)
                        if sku is None:
                            raise ValueError(f"Sku 不存在：{line.product_id}/{line.sku_id}")
                        sku.restore_stock(line.quantity)
                    for product in products.values():
                        await transaction.products.save(product)
                    await transaction.orders.save(order)
                    snapshot = order.snapshot()
                return snapshot
            except RetryableOrderTransactionError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")
